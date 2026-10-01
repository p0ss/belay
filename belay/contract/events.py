"""
The event log: JSON Lines, one event per line, validated against
schemas/event.schema.json.
"""

from __future__ import annotations

import json
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional, Union

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "event.schema.json"

KINDS = ("action", "outcome", "message", "reasoning", "lens", "label", "alert")


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return jsonschema.Draft202012Validator(schema)


def validate(event: dict) -> None:
    """Raise jsonschema.ValidationError if the event breaks the schema."""
    _validator().validate(event)


class EventLog:
    """Appends validated events for one episode. Safe to share between threads."""

    def __init__(self, path: Union[str, Path], episode: str, t0: Optional[float] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.episode = episode
        self.t0 = time.time() if t0 is None else t0
        self._lock = threading.Lock()

    def write(self, agent: str, kind: str, payload: dict, t: Optional[float] = None) -> dict:
        event = {
            "episode": self.episode,
            "t": max(0.0, round((time.time() if t is None else t) - self.t0, 6)),
            "agent": agent,
            "kind": kind,
            "payload": payload,
        }
        validate(event)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return event


def read(path: Union[str, Path], validate_each: bool = True) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if validate_each:
                try:
                    validate(event)
                except jsonschema.ValidationError as e:
                    raise ValueError(f"{path}:{n}: {e.message}") from e
            yield event
