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

from belay.contract import AlertRecord, Certificate, GenerateResponse, credential, verify

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

    @classmethod
    def from_certificate(cls, cert: Certificate) -> "Baseline":
        return cls(cert.model_hash, cert.pack, cert.profile_hash, cert.certificate_id, cert.certified_until)


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
    if cov.watched < criteria.watched_min:
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


# Alert log --------------------------------------------------------------------

@dataclass
class LogAudit:
    path: str
    lines: int = 0
    alert_records: int = 0
    verified: int = 0
    altered: List[dict] = field(default_factory=list)
    duplicates: List[dict] = field(default_factory=list)
    records: List[Tuple[int, AlertRecord]] = field(default_factory=list)
    missing: bool = False

    @property
    def ok(self) -> bool:
        return not self.altered

    def as_dict(self) -> dict:
        return {"path": self.path, "lines": self.lines, "alert_records": self.alert_records,
                "verified": self.verified, "missing": self.missing, "altered": self.altered, "duplicates": self.duplicates,
                "ok": self.ok}


def audit_log(path: Path, lab_key: bytes) -> LogAudit:
    """Verify every signed alert record in a detector log, line by line."""
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
            if event["kind"] != "alert":
                continue
            audit.alert_records += 1
            payload = event.get("payload")
            try:
                record = AlertRecord.model_validate(payload)
            except ValidationError as e:
                audit.altered.append({"line": n, "reason": f"malformed alert record: {e.errors()[0]['msg']}"})
                continue
            if not verify(payload, lab_key):
                audit.altered.append({"line": n, "reason": "signature does not verify",
                                      "request_id": record.request_id, "concept": record.alert.concept})
                continue
            if record.signature in seen:
                audit.duplicates.append({"line": n, "first_line": seen[record.signature],
                                         "request_id": record.request_id})
            seen.setdefault(record.signature, n)
            audit.verified += 1
            audit.records.append((n, record))
    return audit


def cross_check_log(responses: Iterable[GenerateResponse], audit: LogAudit) -> List[Problem]:
    """Every alert a signed response carried must be in the log: a missing record means a deleted line."""
    logged = {(r.request_id, r.alert.concept, r.alert.token_index, round(r.alert.t_signed, 6))
              for _, r in audit.records}
    problems = []
    for resp in responses:
        for a in resp.alerts:
            if (resp.request_id, a.concept, a.token_index, round(a.t_signed, 6)) not in logged:
                problems.append(Problem("log", f"alert {a.concept} in the signed response is missing from the log",
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
