from pathlib import Path

from belay.certifier.suite import Canary, load_canaries, load_criteria
from belay.certifier.verify import Monitor

SUITES = Path(__file__).resolve().parents[2] / "belay" / "certifier" / "suites"


def test_near_miss_canary_is_not_a_recall_trial():
    m = Monitor(criteria=load_criteria(SUITES / "criteria.json"), lab_key=b"k")
    near_miss = Canary(task_id="a" * 16, field="LegalStudies", question="q", expected=[], allowed=["LegalStudies"])
    m.canary(near_miss, alerted=set())
    assert m.pos_n == 0


def test_designated_canary_uses_expected():
    m = Monitor(criteria=load_criteria(SUITES / "criteria.json"), lab_key=b"k")
    c = Canary(task_id="b" * 16, field="PoliticalTheory", question="q", expected=["PoliticalTheory"])
    m.canary(c, alerted={"PoliticalTheory"})
    assert (m.pos_n, m.pos_hits) == (1, 1)


def test_suite_canaries_load_expected():
    canaries = load_canaries(SUITES / "canaries.jsonl")
    assert all(c.expected is not None for c in canaries)
    assert sum(1 for c in canaries if c.expected) == 20
