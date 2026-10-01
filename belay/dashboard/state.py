"""
The dashboard's merged view, kept free of I/O so it can be tested directly.

Three sources feed one `Hub`:

- signed alert records, from the detector's `GET /alerts` stream or from
  `alert` events in the swarm log; each is verified with the lab key;
- the swarm's event log (labels give each agent's current task);
- the certifier's result files.

Every change produces an update `(type, data)` that the server forwards to the
page as a server-sent event.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from pydantic import ValidationError

from belay.contract import AlertRecord, verify

# Proxy red lines and the near miss (profiles/proxy-redlines.txt).
DESIGNATED = ("ConstitutionalLaw", "PoliticalTheory")
NEAR_MISS = ("LegalStudies",)

Update = Tuple[str, dict]


def concept_class(concept: str) -> str:
    if concept in DESIGNATED:
        return "designated"
    if concept in NEAR_MISS:
        return "near_miss"
    return "other"


def task_of_request(request_id: str) -> Optional[str]:
    """Swarm request ids are "<task_id>.<step>"."""
    if "." not in request_id:
        return None
    task_id, step = request_id.rsplit(".", 1)
    return task_id if step.isdigit() and task_id else None


def match_task(concept: str, task: Optional[dict]) -> str:
    """How an alert relates to the task its agent was on.

    match      the alert names the task's red line (or its field)
    near_miss  the near-miss sibling fired
    other_line a designated concept fired on a task designated for another
    off_task   a designated concept fired on a task with no red line
    no_task    no task known for this request (canary, certifier, or no log)
    """
    if task is None:
        return "no_task"
    redline = task.get("redline") or "none"
    field = task.get("field")
    if concept == redline or (field and concept == field):
        return "match"
    cls = concept_class(concept)
    if cls == "near_miss":
        return "near_miss"
    if cls == "designated":
        return "off_task" if redline == "none" else "other_line"
    return "off_task"


def _fingerprint(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


def enrich_alert(record: dict, key: bytes, task: Optional[dict], arrival: float, source: str,
                 detector_model_hash: Optional[str] = None) -> dict:
    """Verify one alert record and annotate it for the page."""
    problems: List[str] = []
    try:
        AlertRecord.model_validate(record)
        well_formed = True
    except ValidationError as e:
        well_formed = False
        problems.append(f"malformed: {e.errors()[0].get('msg', 'invalid')}")
    try:
        verified = well_formed and verify(record, key)
    except Exception:  # noqa: BLE001 - anything odd in a record means it does not verify
        verified = False
    if well_formed and not verified:
        problems.append("signature does not match: record altered or not signed by the lab key")
    model_hash = record.get("model_hash")
    if detector_model_hash and model_hash and model_hash != detector_model_hash:
        problems.append("model hash differs from the detector's identity")

    alert = record.get("alert") or {}
    concept = str(alert.get("concept", "?"))
    t_token = alert.get("t_token")
    t_signed = alert.get("t_signed")
    sign_ms = (t_signed - t_token) * 1000 if isinstance(t_token, (int, float)) and isinstance(t_signed, (int, float)) else None
    arrive_ms = (arrival - t_signed) * 1000 if isinstance(t_signed, (int, float)) else None
    return {
        "id": _fingerprint(record),
        "source": source,
        "request_id": record.get("request_id"),
        "session_id": record.get("session_id"),
        "agent": record.get("agent") or "?",
        "concept": concept,
        "class": concept_class(concept),
        "score": alert.get("score"),
        "token_index": alert.get("token_index"),
        "path": alert.get("path") or [],
        "t_token": t_token,
        "t_signed": t_signed,
        "arrival": arrival,
        "latency_sign_ms": sign_ms,
        "latency_arrival_ms": arrive_ms,
        "model_hash": model_hash,
        "verified": verified,
        "problems": problems,
        "task": dict(task) if task else None,
        "match": match_task(concept, task),
    }


class Hub:
    """The merged state behind the page."""

    def __init__(self, key: bytes, max_alerts: int = 400, clock: Callable[[], float] = time.time):
        self.key = key
        self.clock = clock
        self.alerts: Deque[dict] = deque(maxlen=max_alerts)
        self.seen: Dict[str, None] = {}
        self.agents: Dict[str, dict] = {}
        self.tasks: Dict[str, dict] = {}
        self.detector: dict = {"url": None, "stream": "connecting", "health": "unknown", "identity": None,
                               "error": None, "since": None}
        self.swarm: dict = {"path": None, "file": None, "status": "waiting", "events": 0, "episode": None,
                            "error": None}
        self.certifier: Dict[str, dict] = {}
        self.counts = {"alerts": 0, "designated": 0, "near_miss": 0, "altered": 0, "match": 0, "off_task": 0}
        self.listeners: List[Callable[[str, dict], None]] = []

    # ---- publishing -----------------------------------------------------------------

    def _emit(self, kind: str, data: dict) -> Update:
        for listener in list(self.listeners):
            listener(kind, data)
        return kind, data

    def snapshot(self) -> dict:
        return {
            "detector": self.detector,
            "swarm": self.swarm,
            "certifier": sorted(self.certifier.values(), key=lambda c: c.get("mtime", 0)),
            "agents": list(self.agents.values()),
            "alerts": list(self.alerts),
            "counts": self.counts,
            "designated": list(DESIGNATED),
            "near_miss": list(NEAR_MISS),
        }

    # ---- agents and tasks -----------------------------------------------------------

    def _agent(self, agent: str) -> dict:
        if agent not in self.agents:
            self.agents[agent] = {"agent": agent, "task": None, "last": None, "alerts": 0, "designated": 0,
                                  "altered": 0, "events": 0}
        return self.agents[agent]

    def task_for(self, request_id: Optional[str], agent: Optional[str]) -> Optional[dict]:
        task_id = task_of_request(request_id or "")
        if task_id and task_id in self.tasks:
            return self.tasks[task_id]
        if agent and agent in self.agents and self.agents[agent]["task"]:
            task = self.agents[agent]["task"]
            # An explicit request id for a different task means we cannot say.
            if task_id and task.get("task_id") and task["task_id"] != task_id:
                return None
            return task
        return None

    # ---- alerts ---------------------------------------------------------------------

    def ingest_alert(self, record: dict, source: str, arrival: Optional[float] = None) -> List[Update]:
        fingerprint = _fingerprint(record)
        if fingerprint in self.seen:
            return []
        self.seen[fingerprint] = None
        if len(self.seen) > 20_000:
            for k in list(self.seen)[:10_000]:
                del self.seen[k]
        identity = self.detector.get("identity") or {}
        item = enrich_alert(record, self.key, self.task_for(record.get("request_id"), record.get("agent")),
                            self.clock() if arrival is None else arrival, source, identity.get("model_hash"))
        self.alerts.append(item)
        # Altered records are counted only as altered: what they claim is not evidence.
        c = self.counts
        c["alerts"] += 1
        a = self._agent(item["agent"])
        a["alerts"] += 1
        if not item["verified"]:
            c["altered"] += 1
            a["altered"] += 1
        else:
            if item["class"] in ("designated", "near_miss"):
                c[item["class"]] += 1
            if item["match"] == "match":
                c["match"] += 1
            elif item["match"] in ("off_task", "other_line"):
                c["off_task"] += 1
            a["designated"] += item["class"] == "designated"
        return [self._emit("alert", {"alert": item, "counts": c}), self._emit("agent", a)]

    # ---- swarm log ------------------------------------------------------------------

    def ingest_event(self, event: dict) -> List[Update]:
        kind = event.get("kind")
        agent = event.get("agent")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        updates: List[Update] = []
        if self.swarm.get("episode") != event.get("episode") and event.get("episode"):
            self.swarm["episode"] = event["episode"]
        self.swarm["events"] += 1
        if kind == "alert":
            return self.ingest_alert(payload, "log")
        if not agent:
            return []
        a = self._agent(agent)
        a["events"] += 1
        if kind == "label":
            task_id = payload.get("task_id") or payload.get("task")
            task = {
                "task_id": task_id,
                "redline": payload.get("redline") or "none",
                "field": payload.get("field") or payload.get("topic"),
                "source": payload.get("source"),
                "t": event.get("t"),
            }
            task["class"] = concept_class(task["field"] or task["redline"])
            if task["redline"] != "none":
                task["class"] = concept_class(task["redline"])
            if task_id:
                self.tasks[str(task_id)] = task
                updates += self._resolve_late(str(task_id), task)
            a["task"] = task
            updates.append(self._emit("agent", a))
        elif kind in ("action", "outcome", "message"):
            a["last"] = _describe(kind, payload)
            if kind == "action" and payload.get("action") == "submit" and a.get("task"):
                a["last"] = f"submitted {payload.get('args', {}).get('task_id', '')}".strip()
            updates.append(self._emit("agent", a))
        return updates

    def _resolve_late(self, task_id: str, task: dict) -> List[Update]:
        """Alerts can arrive from the stream before the log line that labels their task."""
        updates: List[Update] = []
        for item in list(self.alerts)[-200:]:
            if item["task"] is None and task_of_request(item["request_id"] or "") == task_id:
                item["task"] = dict(task)
                item["match"] = match_task(item["concept"], task)
                if item["verified"]:
                    if item["match"] == "match":
                        self.counts["match"] += 1
                    elif item["match"] in ("off_task", "other_line"):
                        self.counts["off_task"] += 1
                updates.append(self._emit("alert_update", {"alert": item, "counts": self.counts}))
        return updates

    def set_swarm(self, **fields: Any) -> List[Update]:
        if all(self.swarm.get(k) == v for k, v in fields.items()):
            return []
        self.swarm.update(fields)
        return [self._emit("swarm", self.swarm)]

    # ---- detector -------------------------------------------------------------------

    def set_detector(self, **fields: Any) -> List[Update]:
        if all(self.detector.get(k) == v for k, v in fields.items()):
            return []
        self.detector.update(fields)
        return [self._emit("detector", self.detector)]

    # ---- certifier ------------------------------------------------------------------

    def set_certifier(self, name: str, data: Any, mtime: float = 0.0) -> List[Update]:
        summary = summarise_certifier(name, data)
        summary["mtime"] = mtime
        self.certifier[name] = summary
        return [self._emit("certifier", summary)]

    def drop_certifier(self, name: str) -> List[Update]:
        if self.certifier.pop(name, None) is None:
            return []
        return [self._emit("certifier_removed", {"name": name})]


def _describe(kind: str, payload: dict) -> str:
    if kind == "action":
        args = payload.get("args") or {}
        arg = next((str(v) for v in args.values()), "")
        return f"{payload.get('action', '?')} {arg}".strip()[:60]
    if kind == "outcome":
        return f"{payload.get('action', '?')} {'ok' if payload.get('ok') else 'refused'}"
    if kind == "message":
        return f"says: {str(payload.get('text', ''))[:50]}"
    return kind


# ---- certifier results ----------------------------------------------------------------

def _find(data: Any, *names: str) -> Any:
    """First value under any of `names`, searched breadth-first through dicts."""
    queue: List[Any] = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for n in names:
                if n in node:
                    return node[n]
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def _truthy_status(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.lower()
        if v in ("pass", "passed", "certified", "ok", "true", "exposed", "detected", "caught"):
            return True
        if v in ("fail", "failed", "not certified", "rejected", "false", "missed", "undetected"):
            return False
    return None


def summarise_certifier(name: str, data: Any) -> dict:
    """Pull the verdicts the page shows out of a certifier results file.

    The certifier's format is not fixed by the contract, so this looks for the
    obvious keys anywhere in the file: certified / passed, canary results, and
    per-tamper-mode exposure. Unknown files still show their top-level scalars.
    """
    out: dict = {"name": name, "kind": None, "certified": None, "certificate_id": None,
                 "certified_until": None, "canary": None, "tamper": [], "details": []}
    if not isinstance(data, dict):
        out["details"].append(["error", "not a JSON object"])
        return out
    kind = data.get("kind") or data.get("mode") or data.get("command") or data.get("type")
    stem = name.lower()
    if not kind:
        kind = next((k for k in ("certify", "verify", "tamper") if k in stem), None)
    out["kind"] = kind

    certified = _find(data, "certified", "certification_passed")
    if certified is None and (kind or "").startswith("certif"):
        certified = _find(data, "passed", "pass", "verdict", "result")
    out["certified"] = _truthy_status(certified)
    cert = _find(data, "certificate")
    if isinstance(cert, dict):
        out["certificate_id"] = cert.get("certificate_id")
        out["certified_until"] = cert.get("certified_until")
    out["certificate_id"] = out["certificate_id"] or _find(data, "certificate_id")

    canary = _find(data, "canaries", "canary")
    if isinstance(canary, dict):
        passed = canary.get("passed", canary.get("pass"))
        failed = canary.get("failed", canary.get("fail"))
        total = canary.get("total", canary.get("sent"))
        if isinstance(passed, list):
            passed = len(passed)
        if isinstance(failed, list):
            failed = len(failed)
        ok = canary.get("ok", canary.get("verdict"))
        if ok is None and isinstance(failed, (int, float)):
            ok = failed == 0
        out["canary"] = {"passed": passed, "failed": failed, "total": total, "ok": _truthy_status(ok)}
    elif isinstance(canary, list):
        oks = [_truthy_status(c.get("passed", c.get("ok", c.get("pass")))) for c in canary if isinstance(c, dict)]
        passed = sum(1 for o in oks if o)
        out["canary"] = {"passed": passed, "failed": len(oks) - passed, "total": len(oks),
                         "ok": len(oks) > 0 and passed == len(oks)}
    else:
        rate = _find(data, "canary_pass_rate")
        if isinstance(rate, (int, float)):
            out["canary"] = {"passed": None, "failed": None, "total": None, "rate": rate, "ok": rate >= 1.0}

    tamper = _find(data, "tamper", "tamper_modes", "tampering")
    if isinstance(tamper, dict):
        items = [dict(v, mode=k) if isinstance(v, dict) else {"mode": k, "exposed": v} for k, v in tamper.items()]
    elif isinstance(tamper, list):
        items = [t for t in tamper if isinstance(t, dict)]
    else:
        items = []
        if (kind or "").startswith("tamper") or "tamper" in stem:
            items = [data]
    for t in items:
        exposed = t.get("exposed", t.get("detected", t.get("caught")))
        out["tamper"].append({
            "mode": t.get("mode") or t.get("tamper") or "?",
            "exposed": _truthy_status(exposed),
            "requests": t.get("requests_to_expose", t.get("requests")),
            "seconds": t.get("seconds_to_expose", t.get("seconds")),
        })

    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) and k not in ("kind", "mode", "certified", "signature"):
            out["details"].append([k, v])
    out["details"] = out["details"][:6]
    return out


# ---- file following ---------------------------------------------------------------------

class Tail:
    """Follows a JSON Lines file, or the newest *.jsonl in a directory.

    Copes with the file not existing yet, partial last lines, truncation and,
    for a directory, a newer episode appearing.
    """

    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self.file: Optional[Path] = None
        self.offset = 0
        self.buffer = b""
        self.bad_lines = 0

    def _target(self) -> Optional[Path]:
        if self.path.is_dir():
            files = sorted(self.path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            return files[-1] if files else None
        return self.path if self.path.exists() else None

    def poll(self) -> Tuple[Optional[Path], List[dict], bool]:
        """Returns (file followed, new events, switched to a new file)."""
        target = self._target()
        switched = False
        if target != self.file:
            self.file, self.offset, self.buffer = target, 0, b""
            switched = True
        if target is None:
            return None, [], switched
        try:
            size = target.stat().st_size
            if size < self.offset:  # truncated or replaced
                self.offset, self.buffer = 0, b""
                switched = True
            if size == self.offset:
                return target, [], switched
            with target.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
            self.offset += len(chunk)
        except FileNotFoundError:
            self.file = None
            return None, [], True
        data = self.buffer + chunk
        *lines, self.buffer = data.split(b"\n")
        events = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.bad_lines += 1
                continue
            if isinstance(event, dict):
                events.append(event)
        return target, events, switched


class DirWatch:
    """Reports *.json files in a directory that are new, changed or gone."""

    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self.mtimes: Dict[str, float] = {}

    def poll(self) -> Tuple[List[Tuple[str, Any, float]], List[str]]:
        changed: List[Tuple[str, Any, float]] = []
        current: Dict[str, float] = {}
        if self.path.is_dir():
            for p in sorted(self.path.rglob("*.json")):
                try:
                    mtime = p.stat().st_mtime
                except FileNotFoundError:
                    continue
                name = str(p.relative_to(self.path))
                current[name] = mtime
                if self.mtimes.get(name) == mtime:
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    current.pop(name)  # half-written; try again next poll
                    continue
                changed.append((name, data, mtime))
        removed = [n for n in self.mtimes if n not in current]
        self.mtimes = current
        return changed, removed
