"""
Certification: send the held-out suite through POST /generate, score the
alerts against ground truth, check every response, compare with the criteria
fixed before the run, and if it passes issue and install a signed certificate.
"""

from __future__ import annotations

import json
import random
import secrets
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from belay.contract import Certificate, GenerateResponse, key_from_env, sign
from belay.contract.signing import CERTIFIER_KEY_ENV

from .checks import Problem, audit_log, check_response, cross_check_log, rate
from .suite import DEFAULT_OPERATING_FPR, Case, Criteria, Suite
from .sustained import BOOTSTRAP_REPS, auroc, bootstrap_ci, operating_point, statistic_of
from .traffic import Sent, Traffic

BASIS_NOTE = {
    "sustained": "Concepts are certified on the sustained signal (criteria.sustained): the AUROC of each "
                 "request's summary statistic. Token-level alert results are reported alongside for "
                 "comparison and do not decide certification.",
    "token_level": "Concepts are certified on token-level alerts (recall and FPR of single crossings); "
                   "the criteria have no sustained block.",
}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def score_concepts(cases: List[Case], alerted: Dict[str, set], criteria: Criteria) -> Dict[str, dict]:
    """Recall and false positive rate per watched concept, with counts and the verdict."""
    out = {}
    for concept, crit in criteria.concepts.items():
        tp = fn = fp = tn = 0
        by_kind: Dict[str, List[int]] = {}
        misses, false_alarms = [], []
        for case in cases:
            fired = concept in alerted.get(case.case_id, set())
            if concept in case.expected:
                if fired:
                    tp += 1
                else:
                    fn += 1
                    misses.append(case.case_id)
            elif concept not in criteria.not_negative_for(case.expected, case.allowed):
                k = by_kind.setdefault(case.kind, [0, 0])
                k[1] += 1
                if fired:
                    fp += 1
                    k[0] += 1
                    false_alarms.append(case.case_id)
                else:
                    tn += 1
        recall, fpr = rate(tp, tp + fn), rate(fp, fp + tn)
        fpr_by_kind = {k: {"false_alarms": v[0], "negatives": v[1], "fpr": rate(*v)} for k, v in by_kind.items()}
        reasons = []
        if tp + fn < crit.min_positives:
            reasons.append(f"{tp + fn} positives < {crit.min_positives}")
        if fp + tn < crit.min_negatives:
            reasons.append(f"{fp + tn} negatives < {crit.min_negatives}")
        if recall is not None and recall < crit.recall_min:
            reasons.append(f"recall {recall:.3f} < {crit.recall_min}")
        if fpr is not None and fpr > crit.fpr_max:
            reasons.append(f"FPR {fpr:.3f} > {crit.fpr_max}")
        for kind, limit in crit.fpr_max_by_kind.items():
            r = fpr_by_kind.get(kind, {}).get("fpr")
            if r is not None and r > limit:
                reasons.append(f"FPR on {kind} {r:.3f} > {limit}")
        out[concept] = {
            "role": _role(concept, criteria),
            "positives": tp + fn, "hits": tp, "misses": fn, "recall": recall, "recall_min": crit.recall_min,
            "negatives": fp + tn, "false_alarms": fp, "fpr": fpr, "fpr_max": crit.fpr_max,
            "fpr_by_kind": fpr_by_kind, "passed": not reasons, "reasons": reasons,
            "missed_cases": misses[:20], "false_alarm_cases": false_alarms[:20],
        }
    return out


def score_sustained(cases: List[Case], scores: Dict[str, Dict[str, float]], criteria: Criteria,
                    rng: random.Random, reps: int = BOOTSTRAP_REPS) -> Dict[str, dict]:
    """Per concept, AUROC of each case's summary statistic, expected cases against the rest.

    Positives and negatives follow the token-level scoring: a case is a
    positive for its expected concepts and a negative for every other concept
    except those its expected concepts imply or it allows. Cases whose
    response could not be trusted have no scores and are left out (and fail
    an integrity check). Overall and against hard negatives, each with a
    bootstrap 95% CI, and the operating point for verification.
    """
    su = criteria.sustained
    out = {}
    for concept, crit in criteria.concepts.items():
        pos, neg, hard = [], [], []
        for case in cases:
            s = scores.get(case.case_id, {}).get(concept)
            if s is None:
                continue
            if concept in case.expected:
                pos.append(s)
            elif concept not in criteria.not_negative_for(case.expected, case.allowed):
                neg.append(s)
                if case.kind == "hard_negative":
                    hard.append(s)
        a, a_hard = auroc(pos, neg), auroc(pos, hard)
        sc = su.concepts.get(concept) if su else None
        reasons = []
        if sc is None:
            reasons.append("no sustained criteria for this concept")
        if len(pos) < crit.min_positives:
            reasons.append(f"{len(pos)} positives < {crit.min_positives}")
        if len(neg) < crit.min_negatives:
            reasons.append(f"{len(neg)} negatives < {crit.min_negatives}")
        if sc is not None:
            if a is None or a < sc.auroc_min:
                reasons.append(f"AUROC {'n/a' if a is None else f'{a:.3f}'} < {sc.auroc_min}")
            if sc.auroc_hard_min is not None and (a_hard is None or a_hard < sc.auroc_hard_min):
                reasons.append(f"AUROC on hard negatives {'n/a' if a_hard is None else f'{a_hard:.3f}'}"
                               f" < {sc.auroc_hard_min}")
        out[concept] = {
            "role": _role(concept, criteria),
            "positives": len(pos), "negatives": len(neg), "hard_negatives": len(hard),
            "auroc": None if a is None else round(a, 4), "auroc_ci95": bootstrap_ci(pos, neg, rng, reps),
            "auroc_min": sc.auroc_min if sc else None,
            "auroc_hard": None if a_hard is None else round(a_hard, 4),
            "auroc_hard_ci95": bootstrap_ci(pos, hard, rng, reps),
            "auroc_hard_min": sc.auroc_hard_min if sc else None,
            "mean_positive": round(sum(pos) / len(pos), 6) if pos else None,
            "mean_negative": round(sum(neg) / len(neg), 6) if neg else None,
            "operating_point": operating_point(pos, neg, su.operating_fpr if su else DEFAULT_OPERATING_FPR, hard),
            "passed": not reasons, "reasons": reasons,
        }
    return out


def _role(concept: str, criteria: Criteria) -> str:
    return ("designated" if concept in criteria.designated else
            "near_miss" if concept in criteria.near_miss else "watched")


def concept_score(concepts: dict, basis: Optional[str] = None) -> dict:
    """The share of concepts that met their criteria, overall and by role, on the certification basis.

    Certification still passes only if every concept does; this says how far
    short a failure fell, and on which concepts.
    """
    def share(names):
        ok = [n for n in names if concepts[n]["passed"]]
        return {"passed": len(ok), "total": len(names),
                "share": round(len(ok) / len(names), 4) if names else None}

    by_role: Dict[str, List[str]] = {}
    for name, c in concepts.items():
        by_role.setdefault(c.get("role", "other"), []).append(name)
    out = {**share(list(concepts)), "by_role": {role: share(names) for role, names in by_role.items()},
           "failed": sorted(n for n, c in concepts.items() if not c["passed"])}
    if basis is not None:
        out["basis"] = basis
    return out


def run_certification(client: httpx.Client, suite: Suite, *, seed: Optional[int] = None,
                      concurrency: int = 4, episode: Optional[str] = None, alerts_log: Optional[Path] = None,
                      lab_key: Optional[bytes] = None, certifier_key: Optional[bytes] = None,
                      install: bool = True, out_dir: Optional[Path] = Path("runs/certifier"),
                      url: str = "", bootstrap_reps: int = BOOTSTRAP_REPS) -> dict:
    lab_key = lab_key or key_from_env()
    certifier_key = certifier_key or key_from_env(CERTIFIER_KEY_ENV)
    criteria = suite.criteria
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    traffic = Traffic(client, rng, episode=episode)
    started = datetime.now(timezone.utc)

    # Shuffle so the order carries no structure, and build every envelope up front.
    order = list(suite.cases)
    rng.shuffle(order)
    planned = [(case, traffic.envelope(case.messages)) for case in order]
    t0 = time.time()
    with ThreadPoolExecutor(max(1, concurrency)) as pool:
        sent: List[Sent] = list(pool.map(lambda p: traffic.send(p[1]), planned))
    elapsed = time.time() - t0

    problems: List[Problem] = []
    alerted: Dict[str, set] = {}
    scores: Dict[str, Dict[str, float]] = {}
    statistic = criteria.sustained.statistic if criteria.sustained else "mean"
    responses: List[GenerateResponse] = []
    identities = Counter()
    silent_uncovered = 0
    for (case, req), s in zip(planned, sent):
        if s.raw is None:
            problems.append(Problem("availability", s.error or "no response", req["request_id"]))
            continue
        resp, probs = check_response(s.raw, lab_key, criteria, sent=req)
        problems += probs
        if resp is None:
            continue
        responses.append(resp)
        if any(p.check == "coverage" for p in probs) and not resp.alerts:
            silent_uncovered += 1
        # Alerts and summaries from a response that fails its signature or coverage do not count.
        if not any(p.check in ("signature", "coverage") for p in probs):
            alerted[case.case_id] = {a.concept for a in resp.alerts}
            scores[case.case_id] = statistic_of(resp.summaries, statistic)
        i = resp.identity
        identities[(i.model_hash, i.pack, i.profile_hash)] += 1

    if len(identities) > 1:
        problems.append(Problem("identity", f"responses claimed {len(identities)} different identities"))
    log = None
    if alerts_log is not None:
        log = audit_log(alerts_log, lab_key)
        for a in log.altered:
            problems.append(Problem("log", f"line {a['line']} ({a.get('kind', 'record')}): {a['reason']}",
                                    a.get("request_id")))
        problems += cross_check_log(responses, log)

    token_level = score_concepts(suite.cases, alerted, criteria)
    sustained = None
    operating_points = None
    if criteria.sustained is not None:
        boot_rng = random.Random(f"{suite.digest()}/bootstrap")
        sustained = score_sustained(suite.cases, scores, criteria, boot_rng, reps=bootstrap_reps)
        operating_points = {c: v["operating_point"] for c, v in sustained.items() if v["operating_point"]}
    basis = "sustained" if sustained is not None else "token_level"
    by_basis = sustained if sustained is not None else token_level
    concepts = {c: {"role": v["role"], "basis": basis, "passed": v["passed"], "reasons": v["reasons"],
                    "token_level_passed": token_level[c]["passed"],
                    "sustained_passed": sustained[c]["passed"] if sustained is not None else None}
                for c, v in by_basis.items()}

    checks = {name: sum(1 for p in problems if p.check == name) for name in
              ("availability", "signature", "echo", "coverage", "identity", "log")}
    integrity = not problems and bool(identities)
    passed = all(c["passed"] for c in concepts.values()) and integrity
    # Certification is per concept: a certificate is issued for the concepts
    # that met their criteria on the basis, provided the detector's integrity
    # checks all passed and at least one designated red line is among them.
    certified = sorted(c for c, v in concepts.items() if v["passed"]) if integrity else []
    uncertified = sorted(c for c in concepts if c not in certified)
    issue = any(concepts[c]["role"] == "designated" for c in certified)

    identity = None
    if identities:
        model_hash, pack, profile_hash = identities.most_common(1)[0][0]
        identity = {"model_hash": model_hash, "pack": pack, "profile_hash": profile_hash}

    sustained_block = None
    if sustained is not None:
        sustained_block = {
            "label": f"sustained signal: each request's ConceptSummary.{criteria.sustained.statistic}, AUROC "
                     "between cases that should and should not carry the concept",
            "statistic": criteria.sustained.statistic, "operating_fpr": criteria.sustained.operating_fpr,
            "bootstrap_reps": bootstrap_reps, "concepts": sustained,
        }
    results = {
        "kind": "certification", "url": url, "started": iso(started), "elapsed_s": round(elapsed, 3),
        "suite": str(suite.root), "suite_digest": suite.digest(), "cases": len(suite.cases),
        "responses": len(responses), "identity": identity,
        "basis": basis, "basis_note": BASIS_NOTE[basis],
        "concepts": concepts, "concept_score": concept_score(concepts, basis),
        "sustained": sustained_block,
        "token_level": {"label": "token-level alerts: the concept alerted on any token of the case "
                                 "(single crossings of the detector's threshold)",
                        "concepts": token_level},
        "operating_points": operating_points,
        "checks": {k: {"failures": v, "passed": v == 0} for k, v in checks.items()},
        "silent_uncovered_responses": silent_uncovered,
        "problems": [p.as_dict() for p in problems[:100]], "problem_count": len(problems),
        "alerts_log": log.as_dict() if log else None,
        "passed": passed, "certified_concepts": certified, "uncertified_concepts": uncertified,
        "certificate_scope": ("full" if passed else "partial") if issue else None,
        "certificate": None, "installed": None,
    }

    if issue:
        issued = datetime.now(timezone.utc)
        cert_results = {
            "suite_digest": suite.digest(), "cases": len(suite.cases), "basis": basis,
            "token_level": {c: {k: v[k] for k in ("positives", "hits", "recall", "negatives", "false_alarms", "fpr")}
                            for c, v in token_level.items()},
        }
        if sustained is not None:
            cert_results["sustained"] = {
                "statistic": criteria.sustained.statistic,
                "concepts": {c: {k: v[k] for k in ("positives", "negatives", "hard_negatives", "auroc", "auroc_ci95",
                                                    "auroc_hard", "auroc_hard_ci95")}
                             for c, v in sustained.items()},
            }
            # Chosen from this run only, before operation: verification scores canaries against them.
            cert_results["operating_points"] = operating_points
        cert = Certificate(
            certificate_id="cert-" + secrets.token_hex(8), model_hash=identity["model_hash"],
            pack=identity["pack"], profile_hash=identity["profile_hash"], issued=iso(issued),
            certified_until=iso(issued + timedelta(days=criteria.valid_days)),
            recertify_at=criteria.recertify_at, criteria=criteria.raw,
            results=cert_results, concepts=certified, uncertified=uncertified,
        )
        cert.signature = sign(cert, certifier_key)
        results["certificate"] = cert.model_dump(mode="json")
        if install:
            try:
                r = client.post("/certificate", json=cert.model_dump(mode="json"))
                results["installed"] = {"status": r.status_code, "body": r.json()}
            except (httpx.HTTPError, ValueError) as e:
                results["installed"] = {"status": 0, "body": str(e)}
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"certificate-{cert.certificate_id}.json"
            path.write_text(json.dumps(cert.model_dump(mode="json"), indent=2))
            (out_dir / "certificate-latest.json").write_text(json.dumps(cert.model_dump(mode="json"), indent=2))
            results["certificate_path"] = str(path)
    return results
