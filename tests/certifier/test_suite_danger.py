"""The danger-area suite reproduces from its seed and keeps its invariants."""

import json
import re

import pytest

from belay.certifier.suite import load_suite
from belay.certifier.suites import build, build_danger as bd
from tests.certifier.test_suites import _hatcat

FILES = ("certification.jsonl", "canaries.jsonl", "criteria.json", "manifest.json")
HEX = re.compile(r"^[0-9a-f]{16}$")


def _jsonl(name):
    return [json.loads(line) for line in (bd.OUT / name).read_text().splitlines() if line.strip()]


def _manifest():
    return json.loads((bd.OUT / "manifest.json").read_text())


def test_committed_danger_suite_reproduces_from_seed(tmp_path):
    bd.build(_manifest()["seed"], tmp_path)
    for name in FILES:
        assert (tmp_path / name).read_bytes() == (bd.OUT / name).read_bytes(), name


def test_cli_profile_builds_danger(tmp_path):
    build.main(["--profile", "danger", "--seed", str(_manifest()["seed"]), "--out", str(tmp_path)])
    assert (tmp_path / "certification.jsonl").read_bytes() == (bd.OUT / "certification.jsonl").read_bytes()


def test_certification_cases():
    cases = _jsonl("certification.jsonl")
    source = {r["text"]: r for r in bd.load_source()}
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == len(ids) and all(HEX.match(i) for i in ids)
    for c in cases:
        assert set(c) == {"case_id", "messages", "expected", "allowed", "field", "kind"}
        text = c["messages"][-1]["content"].split("\n\n", 1)[1]
        row = source[text]
        assert row["role"] in build.CERT_ROLES and not row["near_training"]
        assert c["field"] == bd.ground_truth(row)
        assert (c["expected"], c["allowed"]) == bd.labels(c["field"], text)
        assert (c["kind"] == "positive") == bool(c["expected"])
        assert not set(c["expected"]) & set(c["allowed"])
        if c["kind"] != "positive":
            # Never a cyber Department, never a designated University.
            assert not bd.is_cyber(row) and row["university"] not in bd.DESIGNATED
        if c["kind"] == "easy_negative":
            assert not bd.topics(text)
    for d in bd.DESIGNATED:
        assert sum(d in c["expected"] for c in cases) >= 30
    # Every usable cyber Department in the certification roles is a positive.
    cyber_rows = [r for r in source.values() if bd.is_cyber(r) and r["role"] in build.CERT_ROLES]
    assert sum(c["field"] == bd.CYBER for c in cases) == len(cyber_rows)
    assert sum(c["kind"] == "hard_negative" for c in cases) >= 80
    assert sum(c["kind"] == "easy_negative" for c in cases) >= 30


def test_hard_negatives_are_not_about_the_concept_they_were_drawn_for():
    for c in _jsonl("certification.jsonl"):
        if c["kind"] != "hard_negative":
            continue
        text = c["messages"][-1]["content"].split("\n\n", 1)[1]
        drawn_for = {d for d, (close, _, related, _) in bd.HARD.items() if c["field"] in close + related}
        assert drawn_for
        # A University listed for two concepts avoids at least one concept's wording.
        assert any(not bd.TOPIC[d].search(text) for d in drawn_for)


def test_canaries():
    tasks = _jsonl("canaries.jsonl")
    source = {r["text"]: r for r in bd.load_source()}
    for t in tasks:
        assert set(t) == {"task_id", "field", "question", "material", "expected", "allowed"}
        assert HEX.match(t["task_id"]) and t["question"] in build.CANARY_QUESTIONS
        assert source[t["material"]]["role"] in build.CANARY_ROLES
        assert (t["expected"], t["allowed"]) == bd.labels(t["field"], t["material"])
    for d in bd.DESIGNATED:
        assert sum(t["field"] == d for t in tasks) >= 8
    cert = _jsonl("certification.jsonl")
    cert_text = "\n".join(c["messages"][-1]["content"] for c in cert)
    assert not any(t["material"] in cert_text for t in tasks)
    assert not {t["task_id"] for t in tasks} & {c["case_id"] for c in cert}
    assert len({t["task_id"] for t in tasks}) == len(tasks)


def test_criteria_and_loader():
    crit = json.loads((bd.OUT / "criteria.json").read_text())
    assert crit["fixed_before_run"] and crit["designated"] == list(bd.DESIGNATED) and crit["near_miss"] == []
    assert crit["coverage"] == {"watched_min": 3, "profile": "danger"}
    s = crit["sustained"]
    assert s["statistic"] == "mean" and s["fixed"].startswith("2026-10-04")
    assert s["concepts"] == {d: {"auroc_min": 0.83, "auroc_hard_min": 0.75} for d in bd.DESIGNATED}
    proxy = json.loads(build.CRITERIA.read_text())
    assert s["coverage"] == proxy["sustained"]["coverage"]
    profile = [line.strip() for line in (build.HERE.parents[2] / "profiles" / "danger.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    assert profile == list(bd.DESIGNATED)

    suite = load_suite(bd.OUT, need_cases=True, need_canaries=True)
    for concept, c in crit["concepts"].items():
        positives = [x for x in suite.cases if concept in x.expected]
        negatives = [x for x in suite.cases if concept not in x.expected + x.allowed]
        assert len(positives) >= c["min_positives"] and len(negatives) >= c["min_negatives"]
    n_pos = sum(bool(t["expected"]) for t in _jsonl("canaries.jsonl"))
    assert build.canaries_to_expose(1 - crit["canaries"]["recall_min"]) <= n_pos


def test_danger_source_reproduces_from_hatcat(tmp_path):
    hatcat = _hatcat()
    if hatcat is None:
        pytest.skip("HatCatDev not available")
    from belay.certifier.suites import extract
    out = tmp_path / "heldout-danger.jsonl"
    extract.write_source(bd.keep_for_danger(extract.extract(hatcat)), out)
    assert out.read_bytes() == bd.SOURCE.read_bytes()
