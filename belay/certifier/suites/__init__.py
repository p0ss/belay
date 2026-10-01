"""Certification and canary suites (certifier side only; the lab never reads this).

Files in this directory:

- `certification.jsonl`: labelled challenge cases, sent through `POST /generate`.
- `canaries.jsonl`: canary research tasks in the swarm task format, with the
  certifier's expected alerts.
- `criteria.json`: pass criteria, fixed before any run.
- `manifest.json`: seed, counts and hashes of the files above.

Rebuild with `python -m belay.certifier.suites.build --seed N`.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "data" / "heldout.jsonl"
CERTIFICATION = HERE / "certification.jsonl"
CANARIES = HERE / "canaries.jsonl"
CRITERIA = HERE / "criteria.json"
MANIFEST = HERE / "manifest.json"

DEFAULT_SEED = 20261003
DESIGNATED = ("ConstitutionalLaw", "PoliticalTheory")
NEAR_MISS = "LegalStudies"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_certification() -> list[dict]:
    return _jsonl(CERTIFICATION)


def load_canaries() -> list[dict]:
    return _jsonl(CANARIES)


def load_criteria() -> dict:
    return json.loads(CRITERIA.read_text())
