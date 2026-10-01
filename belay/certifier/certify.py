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
from .suite import Case, Criteria, Suite
from .traffic import Sent, Traffic


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
            "role": "designated" if concept in criteria.designated else
                    "near_miss" if concept in criteria.near_miss else "watched",
            "positives": tp + fn, "hits": tp, "misses": fn, "recall": recall, "recall_min": crit.recall_min,
            "negatives": fp + tn, "false_alarms": fp, "fpr": fpr, "fpr_max": crit.fpr_max,
            "fpr_by_kind": fpr_by_kind, "passed": not reasons, "reasons": reasons,
            "missed_cases": misses[:20], "false_alarm_cases": false_alarms[:20],
        }
    return out


def run_certification(client: httpx.Client, suite: Suite, *, seed: Optional[int] = None,
                      concurrency: int = 4, episode: Optional[str] = None, alerts_log: Optional[Path] = None,
                      lab_key: Optional[bytes] = None, certifier_key: Optional[bytes] = None,
                      install: bool = True, out_dir: Optional[Path] = Path("runs/certifier"),
                      url: str = "") -> dict:
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
        if resp.coverage.watched < criteria.watched_min and not resp.alerts:
            silent_uncovered += 1
        # Alerts from a response that fails its signature or coverage do not count.
        if not any(p.check in ("signature", "coverage") for p in probs):
            alerted[case.case_id] = {a.concept for a in resp.alerts}
        i = resp.identity
        identities[(i.model_hash, i.pack, i.profile_hash)] += 1

    if len(identities) > 1:
        problems.append(Problem("identity", f"responses claimed {len(identities)} different identities"))
    log = None
    if alerts_log is not None:
        log = audit_log(alerts_log, lab_key)
        for a in log.altered:
            problems.append(Problem("log", f"line {a['line']}: {a['reason']}", a.get("request_id")))
        problems += cross_check_log(responses, log)

    concepts = score_concepts(suite.cases, alerted, criteria)
    checks = {name: sum(1 for p in problems if p.check == name) for name in
              ("availability", "signature", "echo", "coverage", "identity", "log")}
    passed = all(c["passed"] for c in concepts.values()) and not problems and bool(identities)

    identity = None
    if identities:
        model_hash, pack, profile_hash = identities.most_common(1)[0][0]
        identity = {"model_hash": model_hash, "pack": pack, "profile_hash": profile_hash}

    results = {
        "kind": "certification", "url": url, "started": iso(started), "elapsed_s": round(elapsed, 3),
        "suite": str(suite.root), "suite_digest": suite.digest(), "cases": len(suite.cases),
        "responses": len(responses), "identity": identity,
        "concepts": concepts, "checks": {k: {"failures": v, "passed": v == 0} for k, v in checks.items()},
        "silent_uncovered_responses": silent_uncovered,
        "problems": [p.as_dict() for p in problems[:100]], "problem_count": len(problems),
        "alerts_log": log.as_dict() if log else None,
        "passed": passed, "certificate": None, "installed": None,
    }

    if passed:
        issued = datetime.now(timezone.utc)
        cert = Certificate(
            certificate_id="cert-" + secrets.token_hex(8), model_hash=identity["model_hash"],
            pack=identity["pack"], profile_hash=identity["profile_hash"], issued=iso(issued),
            certified_until=iso(issued + timedelta(days=criteria.valid_days)),
            recertify_at=criteria.recertify_at, criteria=criteria.raw,
            results={"suite_digest": suite.digest(), "cases": len(suite.cases),
                     "concepts": {c: {k: v[k] for k in ("positives", "hits", "recall", "negatives",
                                                         "false_alarms", "fpr")}
                                  for c, v in concepts.items()}},
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
