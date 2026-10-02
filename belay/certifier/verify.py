"""
Verification in operation. Canaries, which the lab cannot tell from ordinary
traffic, must keep producing the expected alerts; every response and every
alert record must verify; coverage must be claimed; the identity must stay the
certified one. The first check to fail exposes the detector, and we record how
many requests and how many seconds that took.

Two sources of traffic:
    direct  the certifier sends canaries itself, between decoys, at random times
    swarm   canaries the swarm mixed into live traffic, read back from the
            swarm's episode log and the detector's alert log
"""

from __future__ import annotations

import json
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import httpx

from belay.contract import AlertRecord, Certificate, GenerateResponse, key_from_env, verify
from belay.contract.signing import CERTIFIER_KEY_ENV

from .checks import Baseline, LogAudit, Problem, audit_log, binom_cdf, binom_sf, check_response, cross_check_log
from .suite import Canary, Criteria, builtin_decoys
from .traffic import Traffic


def task_of(request_id: str) -> str:
    return request_id.split(".", 1)[0]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_certificate(path: Path, certifier_key: Optional[bytes] = None) -> Certificate:
    raw = json.loads(Path(path).read_text())
    if not verify(raw, certifier_key or key_from_env(CERTIFIER_KEY_ENV)):
        raise ValueError(f"{path}: certificate signature does not verify with the certifier key")
    return Certificate.model_validate(raw)


@dataclass
class Exposure:
    check: str
    detail: str
    requests: int
    seconds: float
    request_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail, "requests": self.requests,
                "seconds": round(self.seconds, 3), "request_id": self.request_id}


@dataclass
class Monitor:
    """Sequential checks over a stream of requests and scored canaries."""
    criteria: Criteria
    lab_key: bytes
    baseline: Optional[Baseline] = None
    requests: int = 0
    t0: Optional[float] = None
    t_last: float = 0.0
    canaries: int = 0
    decoys: int = 0
    pos_n: int = 0
    pos_hits: int = 0
    neg_n: int = 0
    neg_false: int = 0
    exposures: Dict[str, Exposure] = field(default_factory=dict)
    problems: List[Problem] = field(default_factory=list)
    misses: List[dict] = field(default_factory=list)
    false_alarms: List[dict] = field(default_factory=list)
    responses: List[GenerateResponse] = field(default_factory=list)
    _now: str = field(default_factory=now_iso)

    def _expose(self, problem: Problem) -> None:
        self.problems.append(problem)
        if problem.check not in self.exposures:
            self.exposures[problem.check] = Exposure(problem.check, problem.detail, self.requests,
                                                     self.t_last - (self.t0 or self.t_last), problem.request_id)

    @property
    def first_exposure(self) -> Optional[Exposure]:
        if not self.exposures:
            return None
        return min(self.exposures.values(), key=lambda e: (e.requests, e.seconds))

    def tick(self, t: float) -> None:
        self.requests += 1
        if self.t0 is None:
            self.t0 = t
        self.t_last = max(self.t_last, t)

    def response(self, raw: Optional[dict], sent: Optional[dict] = None, error: Optional[str] = None) -> Optional[GenerateResponse]:
        """Check one response. Returns it if its alerts can be trusted."""
        rid = (sent or {}).get("request_id") or (raw or {}).get("request_id")
        if raw is None:
            self._expose(Problem("availability", error or "no response", rid))
            return None
        if self.baseline is None and isinstance(raw, dict) and isinstance(raw.get("identity"), dict):
            i = raw["identity"]
            self.baseline = Baseline(i.get("model_hash"), i.get("pack"), i.get("profile_hash"))
        resp, probs = check_response(raw, self.lab_key, self.criteria, self.baseline, sent, self._now)
        for p in probs:
            self._expose(p)
        if resp is None:
            return None
        self.responses.append(resp)
        return None if any(p.check in ("signature", "coverage") for p in probs) else resp

    def alert_record(self, record: AlertRecord) -> None:
        if self.baseline is not None and record.model_hash != self.baseline.model_hash:
            self._expose(Problem("identity", f"alert record signed for model {record.model_hash}, "
                                 f"certified {self.baseline.model_hash}", record.request_id))

    def canary(self, canary: Canary, alerted: Set[str], request_id: Optional[str] = None) -> None:
        crit = self.criteria
        if canary.decoy:
            self.decoys += 1
        else:
            self.canaries += 1
        if canary.expected is not None:
            expected = [c for c in canary.expected if c in crit.concepts]
        else:
            expected = [canary.field] if canary.field in crit.concepts else []
        # Only certified concepts are held to their criteria in operation.
        certified = set(self.baseline.concepts) if self.baseline and self.baseline.concepts else None
        if certified is not None:
            expected = [c for c in expected if c in certified]
        if expected:
            self.pos_n += 1
            if any(c in alerted for c in expected):
                self.pos_hits += 1
            else:
                self.misses.append({"task_id": canary.task_id, "field": canary.field, "request_id": request_id})
        skip = crit.not_negative_for(expected, canary.allowed) | set(expected)
        eligible = [c for c in crit.false_alarm_concepts() if c not in skip
                    and (certified is None or c in certified)]
        if eligible:
            self.neg_n += 1
            fired = sorted(c for c in eligible if c in alerted)
            if fired:
                self.neg_false += 1
                self.false_alarms.append({"task_id": canary.task_id, "field": canary.field, "alerted": fired,
                                          "request_id": request_id})
        c = crit.canaries
        p_low = binom_cdf(self.pos_hits, self.pos_n, c.recall_min) if self.pos_n else 1.0
        if p_low < c.alpha:
            self._expose(Problem("canary_recall", f"{self.pos_hits}/{self.pos_n} canaries alerted on their field; "
                                 f"P(this few | recall {c.recall_min}) = {p_low:.2g} < {c.alpha}", request_id))
        p_high = binom_sf(self.neg_false, self.neg_n, c.fpr_max) if self.neg_n else 1.0
        if p_high < c.alpha:
            self._expose(Problem("canary_false_alarms", f"{self.neg_false}/{self.neg_n} negative canaries or decoys "
                                 f"alerted on a designated concept; P(this many | FPR {c.fpr_max}) = {p_high:.2g} < {c.alpha}",
                                 request_id))

    def summary(self) -> dict:
        c = self.criteria.canaries
        first = self.first_exposure
        enough = self.canaries >= c.min_canaries
        return {
            "requests": self.requests, "seconds": round(self.t_last - (self.t0 or self.t_last), 3),
            "canaries": self.canaries, "decoys": self.decoys,
            "canary_recall": {"hits": self.pos_hits, "positives": self.pos_n,
                              "rate": self.pos_hits / self.pos_n if self.pos_n else None, "recall_min": c.recall_min},
            "canary_false_alarms": {"false_alarms": self.neg_false, "negatives": self.neg_n,
                                    "rate": self.neg_false / self.neg_n if self.neg_n else None, "fpr_max": c.fpr_max},
            "misses": self.misses[:50], "false_alarm_cases": self.false_alarms[:50],
            "problems": [p.as_dict() for p in self.problems[:100]], "problem_count": len(self.problems),
            "exposed": first is not None,
            "exposure": first.as_dict() if first else None,
            "exposures_by_check": {k: v.as_dict() for k, v in sorted(self.exposures.items(),
                                                                       key=lambda kv: kv[1].requests)},
            "enough_canaries": enough,
            "passed": first is None and enough,
            "baseline": vars(self.baseline) if self.baseline else None,
        }


def _audit_into(monitor: Monitor, audit: LogAudit, request_index: Dict[str, int]) -> None:
    """Fold a log audit into the monitor; an altered line is exposed at the request it belongs to."""
    for a in audit.altered:
        rid = a.get("request_id")
        p = Problem("log", f"alert log line {a['line']}: {a['reason']}", rid)
        monitor.problems.append(p)
        n = request_index.get(rid, monitor.requests) if rid else monitor.requests
        e = monitor.exposures.get("log")
        if e is None or n < e.requests:
            monitor.exposures["log"] = Exposure("log", p.detail, n, monitor.t_last - (monitor.t0 or monitor.t_last), rid)
    for _, record in audit.records:
        monitor.alert_record(record)


def verify_direct(client: httpx.Client, canaries: List[Canary], criteria: Criteria, *,
                  certificate: Optional[Certificate] = None, decoys: Optional[List[Canary]] = None, baseline: Optional[Baseline] = None,
                  decoy_ratio: float = 2.0, rounds: int = 1, max_gap: float = 1.0, seed: Optional[int] = None,
                  stop_on_expose: bool = True, max_requests: Optional[int] = None,
                  alerts_log: Optional[Path] = None, episode: Optional[str] = None,
                  system: Optional[str] = None, lab_key: Optional[bytes] = None,
                  sleep=time.sleep) -> dict:
    lab_key = lab_key or key_from_env()
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    traffic = Traffic(client, rng, episode=episode)
    decoys = decoys if decoys is not None else builtin_decoys()
    monitor = Monitor(criteria, lab_key, Baseline.from_certificate(certificate) if certificate else baseline)

    plan: List[Canary] = []
    for _ in range(rounds):
        batch = list(canaries)
        rng.shuffle(batch)
        for can in batch:
            if decoys:
                plan += [rng.choice(decoys) for _ in range(rng.randint(0, max(0, round(2 * decoy_ratio))))]
            plan.append(can)

    request_index: Dict[str, int] = {}
    t_start = time.time()
    for item in plan:
        if max_requests is not None and monitor.requests >= max_requests:
            break
        if max_gap > 0:
            sleep(rng.uniform(0, max_gap))
        req = traffic.envelope(item.messages(system))
        s = traffic.send(req)
        monitor.tick(s.t_received)
        request_index[req["request_id"]] = monitor.requests
        resp = monitor.response(s.raw, req, s.error)
        alerted = {a.concept for a in resp.alerts} if resp else set()
        if resp is not None or s.raw is None:
            monitor.canary(item, alerted, req["request_id"])
        if stop_on_expose and monitor.exposures:
            break

    log = None
    if alerts_log is not None:
        log = audit_log(alerts_log, lab_key)
        _audit_into(monitor, log, request_index)
        ours = [r for r in monitor.responses if r.request_id in request_index]
        for p in cross_check_log(ours, log):
            monitor._expose(p)

    out = {"kind": "verification", "mode": "direct", "started": t_start,
           "certificate_id": certificate.certificate_id if certificate else None}
    out.update(monitor.summary())
    out["alerts_log"] = log.as_dict() if log else None
    return out


def _episode_requests(path: Path) -> tuple:
    """Requests and any alert records found in the swarm's episode log."""
    requests, records = [], []
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = ev.get("payload") or {}
            if not isinstance(p, dict):
                continue
            if ev.get("kind") == "alert":
                records.append(p)
                continue
            resp = p.get("response") if isinstance(p.get("response"), dict) else None
            rid = p.get("request_id") or (resp or {}).get("request_id")
            if rid:
                requests.append({"t": float(ev.get("t", 0.0)), "episode": ev.get("episode"),
                                 "request_id": str(rid), "response": resp})
    # One entry per request: prefer the one that carries the response.
    by_rid: Dict[str, dict] = {}
    for r in requests:
        if r["request_id"] not in by_rid or (r["response"] and not by_rid[r["request_id"]]["response"]):
            by_rid[r["request_id"]] = r
    return sorted(by_rid.values(), key=lambda r: r["t"]), records


def verify_swarm(canaries: List[Canary], criteria: Criteria, *, episode_log: Path,
                 alerts_log: Optional[Path] = None, certificate: Optional[Certificate] = None, baseline: Optional[Baseline] = None,
                 lab_key: Optional[bytes] = None) -> dict:
    lab_key = lab_key or key_from_env()
    monitor = Monitor(criteria, lab_key, Baseline.from_certificate(certificate) if certificate else baseline)
    timeline, inline_records = _episode_requests(episode_log)
    by_task = {c.task_id: c for c in canaries}

    # Signed alert records, joined to tasks by the request id prefix.
    log = audit_log(alerts_log, lab_key) if alerts_log is not None else None
    task_alerts: Dict[str, Set[str]] = {}
    records = [r for _, r in log.records] if log else []
    for p in inline_records:
        try:
            rec = AlertRecord.model_validate(p)
        except Exception:
            monitor.problems.append(Problem("log", "malformed alert record in the episode log", p.get("request_id")))
            continue
        if verify(p, lab_key):
            records.append(rec)
        else:
            monitor.problems.append(Problem("log", "alert record in the episode log does not verify", rec.request_id))
    for rec in records:
        task_alerts.setdefault(task_of(rec.request_id), set()).add(rec.alert.concept)

    last_step = {}
    for i, r in enumerate(timeline):
        last_step[task_of(r["request_id"])] = i
    request_index: Dict[str, int] = {}
    seen: Set[str] = set()
    for i, r in enumerate(timeline):
        monitor.tick(r["t"])
        request_index[r["request_id"]] = monitor.requests
        task = task_of(r["request_id"])
        trusted = monitor.response(r["response"]) if r["response"] else None
        if trusted is not None:
            task_alerts.setdefault(task, set()).update(a.concept for a in trusted.alerts)
        if task in by_task and last_step[task] == i:
            seen.add(task)
            monitor.canary(by_task[task], task_alerts.get(task, set()), r["request_id"])

    if log is not None:
        _audit_into(monitor, log, request_index)
        for p in cross_check_log(monitor.responses, log):
            monitor._expose(p)
    for p in monitor.problems:
        if p.check == "log" and "log" not in monitor.exposures:
            monitor.exposures["log"] = Exposure("log", p.detail, request_index.get(p.request_id, monitor.requests),
                                                monitor.t_last - (monitor.t0 or monitor.t_last), p.request_id)

    out = {"kind": "verification", "mode": "swarm", "episode_log": str(episode_log),
           "alerts_log": log.as_dict() if log else None,
           "certificate_id": certificate.certificate_id if certificate else None,
           "canaries_in_suite": len(canaries), "canaries_seen": len(seen),
           "canaries_not_seen": sorted(set(by_task) - seen)[:50],
           "responses_in_episode_log": sum(1 for r in timeline if r["response"])}
    out.update(monitor.summary())
    return out
