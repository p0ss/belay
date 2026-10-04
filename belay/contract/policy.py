"""
The belay an agent or its operator sets before it starts: which concepts are
reported, and how sensitive each one's alarm is.

A report profile is a text file of concept names (profiles/*.txt). Beside it,
an optional `<profile>.policy.json` maps concepts to a ConceptPolicy; any
concept it does not name is sustained, threshold 0.99. The certified profile
hash covers both files, so changing a concept's sensitivity needs a new
certificate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List

from .models import ConceptPolicy


def concepts(profile: Path) -> List[str]:
    lines = Path(profile).read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def policy_path(profile: Path) -> Path:
    profile = Path(profile)
    return profile.with_name(profile.stem + ".policy.json")


def load(profile: Path) -> Dict[str, ConceptPolicy]:
    """The policy for every concept in the profile."""
    raw = {}
    path = policy_path(profile)
    if path.exists():
        raw = json.loads(path.read_text()).get("concepts", {})
    return {c: ConceptPolicy(**raw.get(c, {})) for c in concepts(profile)}


def digest(profile: Path) -> str:
    """sha256 over the profile and its policy: what a certificate binds."""
    h = hashlib.sha256(Path(profile).read_bytes())
    path = policy_path(profile)
    if path.exists():
        h.update(b"\0policy\0" + path.read_bytes())
    return "sha256:" + h.hexdigest()
