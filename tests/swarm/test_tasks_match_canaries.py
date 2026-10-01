"""Swarm tasks must look like the certifier's canaries, or the lab could pick
the canaries out of ordinary traffic."""

import json
import re
from statistics import median

from belay.certifier.suites.build import CANARY_QUESTIONS
from belay.swarm import tasks as tasklib
from belay.swarm.fields import designated

CANARIES = [json.loads(line) for line in tasklib.CANARIES.read_text().splitlines() if line.strip()]
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def test_question_set_is_the_canaries():
    assert set(tasklib.CANARY_QUESTIONS) == set(CANARY_QUESTIONS)


def test_tasks_look_like_canaries():
    swarm = tasklib.generate(400, seed=20261003)
    # Same question templates.
    assert {t.question for t in swarm} <= set(CANARY_QUESTIONS)
    assert {c["question"] for c in CANARIES} <= set(CANARY_QUESTIONS)
    # Material of the same shape: one description, median length within 20%.
    s_len = median(len(t.material) for t in swarm)
    c_len = median(len(c["material"]) for c in CANARIES)
    assert abs(s_len - c_len) <= 0.2 * c_len
    # Fields named the same way: pack University concepts, CamelCase, no spaces.
    held = {json.loads(line)["university"] for line in tasklib.HELDOUT.read_text().splitlines() if line.strip()}
    assert {t.field for t in swarm} <= held
    assert {c["field"] for c in CANARIES} <= held
    # Same id shape.
    assert all(HEX16.match(t.task_id) for t in swarm)
    assert all(HEX16.match(c["task_id"]) for c in CANARIES)


def test_canary_texts_kept_out_where_possible():
    pool = tasklib.heldout_pool()
    canary_texts = {c["material"] for c in CANARIES}
    marked = set(designated())
    for field, texts in pool.items():
        assert texts, field
        if field not in marked:
            assert not canary_texts & set(texts), field


def test_designated_share_still_holds():
    swarm = tasklib.generate(400, seed=1, designated_share=0.5)
    share = sum(t.field in designated() for t in swarm) / len(swarm)
    assert 0.4 < share < 0.6
    assert {"ConstitutionalLaw", "PoliticalTheory"} <= {t.field for t in swarm}


def test_hierarchy_source_still_available():
    old = tasklib.generate(5, seed=1, source="hierarchy")
    assert all(t.question not in CANARY_QUESTIONS for t in old)
