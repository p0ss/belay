"""Extract the university pack's held-out texts from HatCatDev (read-only).

The university concept packs hold out 30% of Departments (skeleton level 4) by
an MD5 hash of their tree path (HatCatDev `scripts/build_skeleton_concept_pack.py`,
`is_held_out`, `--holdout-level 4 --holdout-fraction 0.3`). No lens trained on
them at any depth. The held-out text is each Department's cleaned topic
description (`clean_scope`).

HatCatDev then used part of that split:

- `scripts/eval_lens_confusion.py` (`collect_split`) samples up to 20 held-out
  Departments per University with one `random.Random(0)`, in skeleton order;
- `scripts/calibrate_band_probes.py` uses the even-indexed half of each sample
  as unlabelled background for per-probe percentile calibration
  (`probe_calibration.json`, 1,630 texts);
- the odd-indexed half is the test half behind the published AUROCs.

This module reproduces that sampling exactly and tags every held-out text with
its role, so the suites can choose:

- ``unused``: held out of training, never sampled for calibration or evaluation;
- ``eval_test``: the odd half, scored in HatCatDev's evaluation only;
- ``calibration``: the even half, seen (unlabelled) by probe calibration.

It also flags ``near_training`` texts: the skeleton repeats generic
Departments, so a few held-out descriptions share their opening with text the
lenses trained on. The suites exclude them.

The functions `is_held_out`, `clean_scope` and the sampling loop are copied, not
imported, so the certifier never imports HatCatDev or torch.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict
from pathlib import Path

PACK_ID = "gemma-4-e4b-it_university-v3-contrasts-bands"
CONCEPT_PACK = "university-v3"  # same split; used by calibration and evaluation
TRAINED_CONCEPT_PACK = "university-v3-contrasts"  # what the lens pack trained on
NEAR_PREFIX = 60
SKELETON = "results/ontology_skeleton_v2.json"
HOLDOUT_LEVEL = 4
HOLDOUT_FRACTION = 0.3
EVAL_PER_CONCEPT = 20  # --max-per-concept / --per-concept used in HatCatDev

# --- copied from HatCatDev scripts/build_skeleton_concept_pack.py ----------

INSTITUTION = (r"(?:school|university|college|institute|institution|faculty|department|center|centre|"
               r"laboratory|lab|academy|atelier|program|programme|field|track)")
FRAMING_VERBS = (
    r"(?:(?:primarily|specifically|broadly)\s+)?"
    r"(?:focuses on|explores|examines|investigates|studies|analy[sz]es|speciali[sz]es in|delves into|covers|"
    r"is dedicated to|dedicates itself to|cent(?:er|re)s on|concentrates on|is devoted to|deals with|encompasses|"
    r"teaches(?:\s+students)?(?:\s+how)?(?:\s+to)?|trains\s+(?:[\w-]+\s+){0,3}?(?:in|to)|"
    r"provides (?:training|instruction|education) (?:in|on))\s+"
)
STUDENTS = (r"^students\s+(?:will\s+)?(?:learn|gain|develop|explore|study|master|acquire|examine|analy[sz]e|"
            r"investigate|engage with|delve into|practice)(?:\s+how)?(?:\s+to|\s+about|\s+in)?\s+")


def is_held_out(path: str, fraction: float) -> bool:
    return fraction > 0 and int(hashlib.md5(path.encode()).hexdigest(), 16) % 1000 < fraction * 1000


def clean_scope(scope: str) -> str:
    s = re.sub(rf"^(?:this|the)\s+(?:[\w-]+\s+)?{INSTITUTION}\s+", "", scope.strip(), flags=re.I)
    s = re.sub(rf"^{FRAMING_VERBS}", "", s, flags=re.I)
    s = re.sub(r"^and\s+", "", s, flags=re.I)
    s = re.sub(STUDENTS, "", s, flags=re.I)
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------------------


def _strip_layer(name: str) -> str:
    return name.split(":")[0]


def _strings(obj):
    """Every string in a JSON value, joined (lists and dicts are often stored as strings too)."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return "\n".join(_strings(v) for v in obj)
    return ""


def extract(hatcat: Path) -> list[dict]:
    """Every held-out Department under a trained University, with its role."""
    hatcat = Path(hatcat)
    skeleton = json.loads((hatcat / SKELETON).read_text())
    hier = hatcat / "concept_packs" / CONCEPT_PACK / "hierarchy"
    builder = json.loads((hier.parent / "pack.json").read_text())["ontology_stack"]["hierarchy_builder"]
    assert builder["holdout"] == {"level": HOLDOUT_LEVEL, "fraction": HOLDOUT_FRACTION}, builder["holdout"]

    universities = json.loads((hier / "layer1.json").read_text())["concepts"]
    owner_of_path = {c["node_path"]: c["sumo_term"] for c in universities}
    field_of = {}
    for c in universities:
        parents = c["parent_concepts"]
        parents = json.loads(parents.replace("'", '"')) if isinstance(parents, str) else parents
        field_of[c["sumo_term"]] = parents[0]

    # The lens pack's calibration records how many held-out texts it used as background.
    calibration = json.loads((hatcat / "lens_packs" / PACK_ID / "probe_calibration.json").read_text())["background"]
    assert calibration["concept_pack"] == CONCEPT_PACK and calibration["level"] == HOLDOUT_LEVEL, calibration

    # Same traversal as collect_split, keeping the node paths alongside the texts.
    candidates: dict[str, list[dict]] = defaultdict(list)

    def collect(node, parent=None, owner=None, school=None):
        path = f"{parent}/{node['id']}" if parent else node["id"]
        owner = owner_of_path.get(path, owner)
        if node["level"] == HOLDOUT_LEVEL - 1:
            school = node["id"]
        if node["level"] == HOLDOUT_LEVEL:
            if owner and is_held_out(path, HOLDOUT_FRACTION) and node.get("scope"):
                candidates[owner].append({"path": path, "school": school, "text": clean_scope(node["scope"])})
            return
        for child in node.get("children", []):
            collect(child, path, owner, school)

    for root in skeleton["roots"]:
        collect(root)

    # The skeleton repeats generic Departments, so some held-out descriptions
    # (near-)duplicate text the lenses trained on. Flag any whose first 60
    # characters occur anywhere in the trained concept pack.
    trained_text = "\n".join(
        _strings(json.loads((hatcat / "concept_packs" / TRAINED_CONCEPT_PACK / "hierarchy" / f"layer{i}.json").read_text()))
        for i in range(4)
    )

    rng = random.Random(0)
    rows = []
    for owner, items in candidates.items():
        # rng.sample's choice depends only on the population size, so sampling
        # indices reproduces HatCatDev's sample of texts.
        picked = rng.sample(range(len(items)), min(EVAL_PER_CONCEPT, len(items)))
        role = {i: "unused" for i in range(len(items))}
        for j, i in enumerate(picked):
            role[i] = "calibration" if j % 2 == 0 else "eval_test"
        for i, item in enumerate(items):
            rows.append({
                "university": owner,
                "field": _strip_layer(field_of[owner]),
                "school": item["school"],
                "path": item["path"],
                "role": role[i],
                "near_training": item["text"][:NEAR_PREFIX] in trained_text,
                "text": item["text"],
            })
    n_cal = sum(r["role"] == "calibration" for r in rows)
    if n_cal != calibration["n_texts"]:
        raise RuntimeError(f"reproduced {n_cal} calibration texts, the pack records {calibration['n_texts']}")
    return rows


def write_source(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def keep_for_suites(rows: list[dict], universities: list[str], fields: list[str], parent_field: str) -> list[dict]:
    """The rows the suites can use: no near-training text, no text filed under two
    Universities, and only the Universities and Fields the suites draw from."""
    owners = defaultdict(set)
    for r in rows:
        owners[r["text"]].add(r["university"])
    keep_unis = set(universities)
    keep_fields = set(fields) | {parent_field}
    return sorted(
        (r for r in rows
         if not r["near_training"] and len(owners[r["text"]]) == 1
         and (r["university"] in keep_unis or r["field"] in keep_fields)),
        key=lambda r: r["path"],
    )
