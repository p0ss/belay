"""
The university fields that swarm tasks are drawn from.

The catalogue in `data/fields.json` is extracted once from the university lens
pack's hierarchy in HatCatDev (layer 1 fields and their layer 2 subtopics, with
the pack's own definitions), so the field names match the pack's concepts. The
swarm only reads the committed catalogue; the pack is not needed at run time.

Regenerate (read-only on the pack):

    uv run python -m belay.swarm.fields --pack /path/to/gemma-4-e4b-it_university-v3-contrasts-bands
"""

from __future__ import annotations

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List

DATA = Path(__file__).resolve().parent / "data" / "fields.json"
PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "proxy-redlines.txt"
DEFAULT_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/"
                    "gemma-4-e4b-it_university-v3-contrasts-bands")

# Kept out of the task pool so every task is benign. The lenses for these
# fields still exist in the pack; the swarm just never asks about them.
EXCLUDED_FIELDS = {
    "PoliticalViolenceResearch", "CyberConflictAndInformationWarfare", "InformationSecurityCryptanalysis",
    "PsychologicalProfilingOfViolence", "ReactiveAggressionStudies",
}
EXCLUDED_SUBTOPIC = re.compile(r"Cyber|Terror|Offensive|Weapon|Arms|Security(Assessment|Analytics)|Extremis")


def extract(pack: Path) -> List[dict]:
    """Build the catalogue from a lens pack's hierarchy. Read-only on the pack."""
    hierarchy = pack / "hierarchy"
    layer0 = {c["sumo_term"]: c for c in json.loads((hierarchy / "layer0.json").read_text())["concepts"]}
    layer1 = json.loads((hierarchy / "layer1.json").read_text())["concepts"]
    layer2 = {c["sumo_term"]: c for c in json.loads((hierarchy / "layer2.json").read_text())["concepts"]}
    fields = []
    for c in layer1:
        name = c["sumo_term"]
        if name in EXCLUDED_FIELDS:
            continue
        subtopics = []
        for child in c.get("category_children", []):
            s = layer2.get(child)
            if not s or EXCLUDED_SUBTOPIC.search(child):
                continue
            subtopics.append({
                "name": child,
                "label": s.get("label") or child,
                "definition": s.get("definition", ""),
                "description": s.get("topic_description", ""),
            })
        if not subtopics:
            continue
        parent = (c.get("parent_concepts") or [""])[0]
        fields.append({
            "field": name,
            "label": c.get("label") or name,
            "pillar": layer0.get(parent, {}).get("label", parent),
            "definition": c.get("definition", ""),
            "subtopics": subtopics,
        })
    return fields


@lru_cache(maxsize=1)
def catalogue() -> List[dict]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def by_name() -> Dict[str, dict]:
    return {f["field"]: f for f in catalogue()}


@lru_cache(maxsize=1)
def designated(profile: Path = PROFILE) -> FrozenSet[str]:
    """Concepts in the watch profile: the proxy red lines and the near miss."""
    names = set()
    for line in profile.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return frozenset(names)


def redline_for(field: str) -> str:
    """The ground-truth label for a task in this field."""
    return field if field in designated() else "none"


def main() -> None:
    p = argparse.ArgumentParser(description="Extract the swarm's field catalogue from a university lens pack")
    p.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    p.add_argument("--out", type=Path, default=DATA)
    args = p.parse_args()
    fields = extract(args.pack)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # One field per line keeps diffs readable without indenting every key.
    lines = ",\n".join(json.dumps(f, ensure_ascii=False) for f in fields)
    args.out.write_text("[\n" + lines + "\n]\n", encoding="utf-8")
    print(f"{len(fields)} fields, {sum(len(f['subtopics']) for f in fields)} subtopics -> {args.out}")


if __name__ == "__main__":
    main()
