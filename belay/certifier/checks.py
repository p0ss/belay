"""
Black-box checks on what crosses the boundary: signed responses, signed alert
records, coverage and identity. Nothing here sees inside the detector.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic import ValidationError

from belay.contract import AlertRecord, Certificate, GenerateResponse, SummaryRecord, credential, verify

from .suite import Criteria


@dataclass
class Problem:
    check: str  # signature | coverage | identity | echo | log | canary_recall | canary_false_alarms
    detail: str
    request_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail, "request_id": self.request_id}


@dataclass
class Baseline:
    """The identity the detector must keep presenting: from a certificate, or from first contact."""
    model_hash: str
    pack: str
    profile_hash: str
    certificate_id: Optional[str] = None
    certified_until: Optional[str] = None
    # Concepts the certificate covers; None means every concept in the criteria.
    concepts: Optional[List[str]] = None
    # Per concept, the summary score above which a canary counts as a hit,
    # chosen from the certification run before operation. None: score
    # canaries by alerts.
    operating_points: Optional[Dict[str, float]] = None
    statistic: str = "mean"

    @classmethod
    def from_certificate(cls, cert: Certificate) -> "Baseline":
        b = cls(cert.model_hash, cert.pack, cert.profile_hash, cert.certificate_id, cert.certified_until,
                list(cert.concepts) or None)
        b.operating_points, b.statistic = operating_points_from(cert.results)
        return b


def operating_points_from(results: dict) -> Tuple[Optional[Dict[str, float]], str]:
    """The operating points stored in a certificate's or certification run's results."""
    ops = (results or {}).get("operating_points")
    if not isinstance(ops, dict) or not ops:
        return None, "mean"
    statistic = (results.get("sustained") or {}).get("statistic", "mean") \
        if isinstance(results.get("sustained"), dict) else "mean"
    return {c: float(v["threshold"]) for c, v in ops.items() if v and v.get("threshold") is not None}, statistic


def check_response(raw: dict, lab_key: bytes, criteria: Criteria, baseline: Optional[Baseline] = None,
                   sent: Optional[dict] = None, now_iso: Optional[str] = None) -> Tuple[Optional[GenerateResponse], List[Problem]]:
    """Every check one response can fail on its own. Returns the parsed response (None if malformed)."""
    rid = raw.get("request_id") if isinstance(raw, dict) else None
    try:
        resp = GenerateResponse.model_validate(raw)
    except ValidationError as e:
        return None, [Problem("signature", f"malformed response: {e.errors()[0]['msg']}", rid)]
    problems: List[Problem] = []
    # Verify over exactly what was received, so an unknown extra field breaks it too.
    if not verify(raw, lab_key):
        problems.append(Problem("signature", "response signature does not verify with the lab key", rid))
    if sent is not None:
        for k in ("request_id", "session_id", "agent"):
            if getattr(resp, k) != sent[k]:
                problems.append(Problem("echo", f"{k} {getattr(resp, k)!r} != sent {sent[k]!r}", rid))

    cov = resp.coverage
    problems += check_summary_coverage(resp, criteria)
    if not resp.summaries and cov.watched < criteria.watched_min:
        problems.append(Problem("coverage", f"watched {cov.watched} < {criteria.watched_min}: "
                                "silence here is absence, not a clean result", rid))
    if criteria.profile and cov.profile != criteria.profile:
        problems.append(Problem("coverage", f"profile {cov.profile!r} != {criteria.profile!r}", rid))
    if cov.pack != resp.identity.pack:
        problems.append(Problem("coverage", f"coverage pack {cov.pack!r} != identity pack {resp.identity.pack!r}", rid))
    watched_alerts = {a.concept for a in resp.alerts}
    if watched_alerts and cov.watched == 0:
        problems.append(Problem("coverage", "alerts reported with nothing watched", rid))

    ident = resp.identity
    want_cred = credential(resp.agent, ident.model_hash, ident.pack, ident.profile_hash, lab_key)
    if ident.agent_credential != want_cred:
        problems.append(Problem("identity", "agent credential does not bind this agent to the claimed model", rid))
    if baseline is not None:
        for k in ("model_hash", "pack", "profile_hash"):
            if getattr(ident, k) != getattr(baseline, k):
                problems.append(Problem("identity", f"{k} changed: {getattr(ident, k)} != certified {getattr(baseline, k)}", rid))
        if baseline.certificate_id is not None:
            if ident.certificate_id != baseline.certificate_id:
                problems.append(Problem("identity", f"certificate_id {ident.certificate_id!r} != {baseline.certificate_id!r}", rid))
            if now_iso and baseline.certified_until and now_iso > baseline.certified_until:
                problems.append(Problem("identity", f"certificate expired at {baseline.certified_until}", rid))
    return resp, problems


def check_summary_coverage(resp: GenerateResponse, criteria: Criteria) -> List[Problem]:
    """A response is covered when every reported concept's summary covers every token.

    Without summaries, the old `watched` check applies instead (check_response),
    unless the criteria certify the sustained signal: then missing summaries
    are missing coverage.
    """
    rid = resp.request_id
    if not resp.summaries:
        if criteria.sustained is not None:
            return [Problem("coverage", "no summaries: the sustained signal is absent", rid)]
        return []
    frac = criteria.sustained.min_covered_fraction if criteria.sustained else 1.0
    by_concept = {}
    problems = []
    for s in resp.summaries:
        if s.concept in by_concept:
            problems.append(Problem("coverage", f"two summaries for {s.concept}", rid))
        by_concept[s.concept] = s
    for concept in criteria.concepts:
        s = by_concept.get(concept)
        if s is None:
            problems.append(Problem("coverage", f"no summary for reported concept {concept}", rid))
        elif s.covered_tokens < frac * resp.tokens or s.covered_tokens > resp.tokens:
            problems.append(Problem("coverage", f"{concept} covered {s.covered_tokens} of {resp.tokens} tokens: "
                                    "silence on the rest is absence, not a clean result", rid))
    return problems


# Alert log --------------------------------------------------------------------

@dataclass
class LogAudit:
    path: str
    lines: int = 0
    alert_records: int = 0
    summary_records: int = 0
    verified: int = 0
    altered: List[dict] = field(default_factory=list)
    duplicates: List[dict] = field(default_factory=list)
    records: List[Tuple[int, AlertRecord]] = field(default_factory=list)
    summaries: List[Tuple[int, SummaryRecord]] = field(default_factory=list)
    missing: bool = False

    @property
    def ok(self) -> bool:
        return not self.altered

    def as_dict(self) -> dict:
        return {"path": self.path, "lines": self.lines, "alert_records": self.alert_records,
                "summary_records": self.summary_records, "verified": self.verified, "missing": self.missing,
                "altered": self.altered, "duplicates": self.duplicates, "ok": self.ok}


SIGNED_KINDS = {"alert": AlertRecord, "summary": SummaryRecord}


def check_record(kind: str, payload, lab_key: bytes):
    """Parse and verify one signed log record. Returns (record, None) or (record or None, reason)."""
    try:
        record = SIGNED_KINDS[kind].model_validate(payload)
    except ValidationError as e:
        return None, f"malformed {kind} record: {e.errors()[0]['msg']}"
    if not verify(payload, lab_key):
        return record, "signature does not verify"
    return record, None


def audit_log(path: Path, lab_key: bytes) -> LogAudit:
    """Verify every signed record (alerts and summaries) in a detector log, line by line."""
    audit = LogAudit(path=str(path))
    seen: Dict[str, int] = {}
    if not Path(path).exists():
        # A detector writes its log on the first alert; no file is an empty log.
        audit.missing = True
        return audit
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            audit.lines += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                audit.altered.append({"line": n, "reason": f"not JSON: {e.msg}"})
                continue
            if not isinstance(event, dict) or "kind" not in event:
                audit.altered.append({"line": n, "reason": "not an event"})
                continue
            kind = event["kind"]
            if kind not in SIGNED_KINDS:
                continue
            if kind == "alert":
                audit.alert_records += 1
            else:
                audit.summary_records += 1
            record, reason = check_record(kind, event.get("payload"), lab_key)
            if reason is not None:
                entry = {"line": n, "kind": kind, "reason": reason}
                if record is not None:
                    entry["request_id"] = record.request_id
                    if kind == "alert":
                        entry["concept"] = record.alert.concept
                audit.altered.append(entry)
                continue
            if record.signature in seen:
                audit.duplicates.append({"line": n, "kind": kind, "first_line": seen[record.signature],
                                         "request_id": record.request_id})
            seen.setdefault(record.signature, n)
            audit.verified += 1
            if kind == "alert":
                audit.records.append((n, record))
            else:
                audit.summaries.append((n, record))
    return audit


def _summaries_key(summaries) -> list:
    return sorted((s.model_dump(mode="json") for s in summaries), key=lambda d: d["concept"])


def cross_check_log(responses: Iterable[GenerateResponse], audit: LogAudit) -> List[Problem]:
    """Every alert and summary a signed response carried must be in the log, unchanged.

    A missing record means a deleted line; a summary that differs from the
    response's means the lab signed two different accounts of one request.
    """
    logged = {(r.request_id, r.alert.concept, r.alert.token_index, round(r.alert.t_signed, 6))
              for _, r in audit.records}
    logged_summaries: Dict[str, List[list]] = {}
    for _, r in audit.summaries:
        logged_summaries.setdefault(r.request_id, []).append(_summaries_key(r.summaries))
    problems = []
    for resp in responses:
        for a in resp.alerts:
            if (resp.request_id, a.concept, a.token_index, round(a.t_signed, 6)) not in logged:
                problems.append(Problem("log", f"alert {a.concept} in the signed response is missing from the log",
                                        resp.request_id))
        if resp.summaries:
            got = logged_summaries.get(resp.request_id)
            if not got:
                problems.append(Problem("log", "summary in the signed response is missing from the log",
                                        resp.request_id))
            elif _summaries_key(resp.summaries) not in got:
                problems.append(Problem("log", "summary in the log differs from the signed response",
                                        resp.request_id))
    return problems


# Statistics -------------------------------------------------------------------

def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    return 1.0 - binom_cdf(k - 1, n, p)


def rate(num: int, den: int) -> Optional[float]:
    return None if den == 0 else num / den
