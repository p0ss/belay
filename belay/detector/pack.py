"""
Reading a band lens pack and a watch profile from files, without torch.

A pack holds `layer<n>/<Concept>@L<model_layer>.pt` probes and a
`hierarchy/hierarchy.json` whose keys are `Concept:layer`. A watch profile is a
list of concepts, one per line, `#` for comments; watching a concept also
watches every concept beneath it that has a lens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

Key = Tuple[str, int]  # (concept, hierarchy layer)

_PROBE = re.compile(r"^(?P<term>.+)@L(?P<model_layer>\d+)\.pt$")


def read_profile(path: Path) -> List[str]:
    """Concepts in a profile file, the same rule as HAT's WatchProfile.from_file."""
    lines = Path(path).read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def lens_files(pack_dir: Path) -> Dict[Key, List[Path]]:
    """Each concept with a lens, and its probe files."""
    out: Dict[Key, List[Path]] = {}
    for layer_dir in sorted(Path(pack_dir).glob("layer[0-9]*")):
        if not layer_dir.is_dir():
            continue
        layer = int(layer_dir.name[len("layer"):])
        for f in sorted(layer_dir.iterdir()):
            m = _PROBE.match(f.name)
            if m:
                out.setdefault((m["term"], layer), []).append(f)
    return out


def _key(text: str) -> Key:
    term, _, layer = text.rpartition(":")
    return term, int(layer)


class Hierarchy:
    def __init__(self, pack_dir: Path):
        data = json.loads((Path(pack_dir) / "hierarchy" / "hierarchy.json").read_text())
        self.parent: Dict[Key, Key] = {_key(c): _key(p) for c, p in data["child_to_parent"].items()}
        self.roots: List[Key] = [_key(r) for r in data.get("root_concepts", [])]

    def path(self, key: Key) -> List[str]:
        """Concept names from the root down to `key`."""
        out, seen = [key[0]], {key}
        while key in self.parent and self.parent[key] not in seen:
            key = self.parent[key]
            seen.add(key)
            out.insert(0, key[0])
        return out


def watched_keys(concepts: Sequence[str], lensed: Iterable[Key], hierarchy: Hierarchy) -> List[Key]:
    """Concepts with a lens whose path passes through a profile concept."""
    want = set(concepts)
    return sorted(k for k in lensed if want.intersection(hierarchy.path(k)))


def full_profile_text(hierarchy: Hierarchy, lensed: Iterable[Key]) -> str:
    """The profile that watches the whole pack: every root concept that has a lens."""
    lensed = set(lensed)
    roots = sorted(r[0] for r in hierarchy.roots if r in lensed) or sorted({k[0] for k in lensed if k[1] == 0})
    return "# Whole pack watched: every root concept.\n" + "".join(f"{r}\n" for r in roots)
