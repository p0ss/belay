"""The suites are reproducible from their seed and keep their invariants."""

import json
import os
import re
from pathlib import Path

import pytest

from belay.certifier import suites
from belay.certifier.suites import build, extract

FILES = ("certification.jsonl", "canaries.jsonl", "criteria.json", "manifest.json")
HEX = re.compile(r"^[0-9a-f]{16}$")


def _manifest():
    return json.loads(suites.MANIFEST.read_text())


def test_committed_suites_reproduce_from_seed(tmp_path):
    seed = _manifest()["seed"]
    build.build(seed, tmp_path)
    for name in FILES:
        assert (tmp_path / name).read_bytes() == (suites.HERE / name).read_bytes(), name


def test_same_seed_same_bytes_and_other_seed_differs(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    build.build(7, a)
    build.build(7, b)
    build.build(8, c)
    for name in FILES:
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert (a / "certification.jsonl").read_bytes() != (c / "certification.jsonl").read_bytes()
    # Criteria do not depend on the seed.
    assert (a / "criteria.json").read_bytes() == (c / "criteria.json").read_bytes()


def test_certification_cases():
    cases = suites.load_certification()
    assert 150 <= len(cases) < 300
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == len(ids) and all(HEX.match(i) for i in ids)
    for c in cases:
        assert set(c) == {"case_id", "messages", "expected", "allowed", "field", "kind"}
        assert c["kind"] in {"positive", "hard_negative", "easy_negative"}
        assert c["messages"] and c["messages"][-1]["role"] == "user"
        assert (c["expected"], c["allowed"]) == build.labels(c["field"])
        assert (c["kind"] == "positive") == bool(set(c["expected"]) & set(suites.DESIGNATED))
        assert not set(c["expected"]) & set(c["allowed"])
    for d in suites.DESIGNATED:
        assert 30 <= sum(c["field"] == d for c in cases) <= 60
    near = [c for c in cases if c["field"] == suites.NEAR_MISS]
    assert len(near) >= 20
    assert all(c["kind"] == "hard_negative" and c["expected"] == [suites.NEAR_MISS] for c in near)
    assert sum(c["kind"] == "easy_negative" for c in cases) >= 30


def test_canaries_are_swarm_tasks_disjoint_from_certification():
    tasks = suites.load_canaries()
    assert len(tasks) >= 30
    for t in tasks:
        assert set(t) == {"task_id", "field", "question", "material", "expected", "allowed"}
        assert HEX.match(t["task_id"])
        assert t["expected"] == ([t["field"]] if t["field"] in suites.DESIGNATED else [])
    assert len({t["task_id"] for t in tasks}) == len(tasks)
    for d in suites.DESIGNATED:
        assert sum(t["field"] == d for t in tasks) >= 8
    cert_text = "\n".join(c["messages"][-1]["content"] for c in suites.load_certification())
    assert not any(t["material"] in cert_text for t in tasks)
    assert not {t["task_id"] for t in tasks} & {c["case_id"] for c in suites.load_certification()}


def test_held_out_only_and_negatives_off_topic():
    source = {r["text"]: r for r in build.load_source()}
    for c in suites.load_certification():
        text = c["messages"][-1]["content"].split("\n\n", 1)[1]
        row = source[text]
        assert row["role"] in build.CERT_ROLES and not row["near_training"]
        assert row["university"] == c["field"]
        if c["kind"] != "positive":
            assert not build.DESIGNATED_TOPIC.search(text)
    for t in suites.load_canaries():
        assert source[t["material"]]["role"] in build.CANARY_ROLES


def test_criteria():
    crit = suites.load_criteria()
    assert crit["fixed_before_run"] and crit["designated"] == list(suites.DESIGNATED)
    assert crit["near_miss"] == [suites.NEAR_MISS]
    cases = suites.load_certification()
    for concept, c in crit["concepts"].items():
        assert 0 < c["recall_min"] < 1 and 0 < c["fpr_max"] < 1
        positives = [x for x in cases if concept in x["expected"]]
        negatives = [x for x in cases if concept not in x["expected"] + x["allowed"]]
        assert len(positives) >= c["min_positives"] and len(negatives) >= c["min_negatives"]
    for d in suites.DESIGNATED:
        k = crit["concepts"][d]["fpr_max_by_kind"]
        assert k["easy_negative"] < k["hard_negative"]
    canaries = crit["canaries"]
    assert canaries["false_alarm_concepts"] == list(suites.DESIGNATED)
    # A dead detector's misses fall below alpha within the suite's positive canaries.
    n_pos = sum(bool(t["expected"]) for t in suites.load_canaries())
    dead = build.canaries_to_expose(1 - canaries["recall_min"])
    assert (1 - canaries["recall_min"]) ** dead < canaries["alpha"] <= (1 - canaries["recall_min"]) ** (dead - 1)
    assert dead <= n_pos


def _hatcat() -> Path | None:
    """HatCatDev beside this checkout (or the main one), or $BELAY_HATCAT."""
    candidates = [os.environ.get("BELAY_HATCAT")] + [str(p / "HatCatDev") for p in Path(__file__).resolve().parents]
    for c in candidates:
        if c and (Path(c) / extract.SKELETON).is_file():
            return Path(c)
    return None


def test_source_reproduces_from_hatcat(tmp_path):
    hatcat = _hatcat()
    if hatcat is None:
        pytest.skip("HatCatDev not available")
    rows = extract.keep_for_suites(extract.extract(hatcat), build.SIBLINGS + build.RELATED,
                                   build.DISTANT_FIELDS, build.PARENT_FIELD)
    out = tmp_path / "heldout.jsonl"
    extract.write_source(rows, out)
    assert out.read_bytes() == suites.SOURCE.read_bytes()
