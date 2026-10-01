"""
Tamper with the alert log: rewrite one signed alert in place, keeping its old
signature, as a lab covering its tracks would. The log keeper should find it
because the signature no longer verifies.

    uv run python -m belay.detector.tamper_log runs/detector-alerts.jsonl \
        [--index N | --request-id R] [--concept C] [--set concept=LegalStudies --set score=0.1]

Picks the Nth alert event (0-based) among those matching --request-id and
--concept, and sets fields of its `alert` (default: concept=LegalStudies, a red
line downgraded to its near-miss sibling; if it already is LegalStudies, its
score drops below threshold instead). Writes the file back atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

NEAR_MISS = "LegalStudies"


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def default_changes(alert: dict) -> Dict[str, object]:
    if alert.get("concept") != NEAR_MISS:
        path = list(alert.get("path") or [])
        return {"concept": NEAR_MISS, "path": path[:-1] + [NEAR_MISS] if path else [NEAR_MISS]}
    return {"score": 0.5}


def tamper(path: Path, index: int = 0, request_id: Optional[str] = None, concept: Optional[str] = None,
           changes: Optional[Dict[str, object]] = None) -> dict:
    """Rewrite one alert event; returns {"line", "before", "after"}."""
    path = Path(path)
    lines: List[str] = path.read_text(encoding="utf-8").splitlines(keepends=True)
    seen = 0
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("kind") != "alert":
            continue
        payload = event["payload"]
        if request_id is not None and payload.get("request_id") != request_id:
            continue
        if concept is not None and payload["alert"].get("concept") != concept:
            continue
        if seen < index:
            seen += 1
            continue
        before = json.loads(json.dumps(payload["alert"]))
        payload["alert"].update(changes if changes is not None else default_changes(payload["alert"]))
        newline = "\n" if line.endswith("\n") else ""
        lines[n] = json.dumps(event, ensure_ascii=False) + newline
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, path)
        return {"line": n + 1, "before": before, "after": payload["alert"]}
    raise SystemExit(f"no matching alert in {path}")


def main() -> None:
    p = argparse.ArgumentParser(description="Rewrite one signed alert in a detector log, in place")
    p.add_argument("log", type=Path)
    p.add_argument("--index", type=int, default=0, help="which matching alert (0-based)")
    p.add_argument("--request-id")
    p.add_argument("--concept", help="only alerts for this concept")
    p.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="alert field to overwrite (JSON value or string); repeatable")
    args = p.parse_args()
    changes = None
    if args.set:
        changes = {}
        for item in args.set:
            field, _, value = item.partition("=")
            changes[field] = _value(value)
    result = tamper(args.log, args.index, args.request_id, args.concept, changes)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
