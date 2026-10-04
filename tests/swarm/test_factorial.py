"""The 2x2 factorial experiment: design, worlds, leakage and the analysis CLI, against the stub."""

import json
import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from belay.contract.events import read
from belay.contract.service import create_app
from belay.contract.stub import KEYWORDS, StubBackend
from belay.swarm import factorial
from belay.swarm.fields import by_name
from belay.swarm.runner import Config, Swarm, main
from belay.swarm.world import SCENARIOS


def mentions(text, concept):
    text = text.lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in KEYWORDS[concept])


class Recorder:
    """Wraps the stub's client and keeps every request body the swarm sends."""

    def __init__(self, client):
        self.client, self.bodies = client, []

    def post(self, url, json=None, **kw):
        self.bodies.append(json)
        return self.client.post(url, json=json, **kw)


def run(tmp_path, concept="LegalStudies", agents=16, steps=16, episode="ep-f", canaries=()):
    detector_log = tmp_path / "detector.jsonl"
    client = Recorder(TestClient(create_app(StubBackend(latency_ms=0), detector_log)))
    cfg = Config(agents=agents, steps=steps, task_steps=8, time_limit=120, episode=episode,
                 out=tmp_path / f"{episode}.jsonl", factorial=concept, scenario_every=4)
    tasks = factorial.make_tasks(concept, agents, seed=2, canaries=canaries)
    swarm = Swarm(cfg, tasks, client=client)
    swarm.run()
    swarm.validate()
    return swarm, list(read(cfg.out)), list(read(detector_log)), client.bodies


# --- the design ----------------------------------------------------------------

def test_groups_balanced_and_reproducible():
    names = [f"agent-{i:02d}" for i in range(1, 17)]
    a = factorial.assign_groups(names, "ep-1")
    assert a == factorial.assign_groups(names, "ep-1")
    assert Counter(a.values()) == {g: 4 for g in factorial.GROUPS}
    others = [factorial.assign_groups(names, f"ep-{i}") for i in range(2, 6)]
    assert any(o != a for o in others)  # the episode decides, not the agent's number


@pytest.mark.parametrize("concept", factorial.MONITORED)
def test_positive_tasks_never_in_or_near_the_monitored_field(concept):
    tasks = factorial.make_tasks(concept, 40, seed=1)
    pillar = by_name()[concept]["pillar"]
    for g in ("A", "B"):
        for t in tasks[g]:
            assert t.field in factorial.DEFAULT_POSITIVE_FIELDS
            assert by_name()[t.field]["pillar"] not in {pillar} | factorial.GOVERNANCE_PILLARS
            assert not factorial.NEAR.search(t.material)
            assert not any(mentions(t.material + " " + t.question, c) for c in KEYWORDS)
    for g in ("C", "D"):
        assert {t.field for t in tasks[g]} == {concept}
    ids = [t.task_id for ts in tasks.values() for t in ts]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("fields", [["LegalStudies"], ["PolicyAnalysisResearch"], ["CriminalJusticeAndPunishment"],
                                    ["PoliticalPartiesPartyIdentification"], ["NoSuchField"]])
def test_positive_fields_override_refuses_fields_near_the_concept(fields):
    with pytest.raises(ValueError):
        factorial.check_positive_fields("ConstitutionalLaw", fields)


def test_canaries_dealt_evenly_and_unmarked():
    canaries = factorial.make_tasks("PoliticalTheory", 3, seed=9)["A"] * 1
    canaries = canaries + factorial.make_tasks("PoliticalTheory", 1, seed=10)["C"]  # 4 canaries
    tasks = factorial.make_tasks("ConstitutionalLaw", 5, seed=1, canaries=canaries)
    assert all(len(ts) == 6 for ts in tasks.values())
    assert sorted(t.task_id for ts in tasks.values() for t in ts if t in canaries) == sorted(c.task_id for c in canaries)


@pytest.mark.parametrize("concept", factorial.MONITORED)
def test_scenario_content_leads_with_the_concept(concept):
    for note in SCENARIOS[concept]["notes"]:
        assert mentions(note.format(subject="pottery glazes")[:60], concept)
    assert all(mentions(p, concept) for p in SCENARIOS[concept]["posts"])
    assert mentions(SCENARIOS[concept]["terminal"], concept)


# --- an episode ------------------------------------------------------------------

def test_factorial_episode_against_the_stub(tmp_path):
    concept = "LegalStudies"
    swarm, events, detector, bodies = run(tmp_path, concept)
    groups = swarm.groups
    assert Counter(groups.values()) == {g: 4 for g in factorial.GROUPS}

    # Group labels for every agent, before any request.
    labels = [e for e in events if e["kind"] == "label" and e["payload"]["basis"] == "group"]
    assert {e["agent"]: e["payload"] for e in labels} == {a: factorial.group_label(concept, g)
                                                          for a, g in groups.items()}
    first_reasoning = next(i for i, e in enumerate(events) if e["kind"] == "reasoning")
    assert all(events.index(e) < first_reasoning for e in labels)

    reads = [e for e in events if e["kind"] == "outcome" and e["payload"]["action"] == "read"]
    by_group = {g: [a for a, x in groups.items() if x == g] for g in factorial.GROUPS}

    def board_reads(group):
        # A noticeboard read shows the last notes joined by " || ".
        found = [str(e["payload"]["detail"]) for e in reads if groups[e["agent"]] == group
                 and " || " in str(e["payload"]["detail"])]
        assert found
        return found

    # A and C are isolated: nothing a C agent posted is ever visible to A.
    c_posts = [e["payload"]["text"] for e in events if e["kind"] == "message" and groups.get(e["agent"]) == "C"]
    assert c_posts
    for detail in board_reads("A"):
        assert not any(a in detail for a in by_group["C"] + by_group["B"] + by_group["D"])
        assert not mentions(detail, concept)
    # B shares the negative world with D, and sees D's posts.
    assert any(any(f"{a}:" in detail for a in by_group["D"]) for detail in board_reads("B"))
    assert all(mentions(detail, concept) for detail in board_reads("B"))
    # Each world only ever saw its own agents.
    for wid, world in swarm.worlds.items():
        members = {a for a, g in groups.items() if factorial.WORLDS[g] == wid}
        assert set(world.agents) == members

    # Prompts: the negative scenario carries the concept from step 1; the
    # positive scenario never does in what the stub reads, nor anywhere at step 1.
    reasoning = [e for e in events if e["kind"] == "reasoning"]
    prompt_of = {b["request_id"]: b["messages"][-1]["content"] for b in bodies}
    for e in reasoning:
        g = groups[e["agent"]]
        prompt = prompt_of[e["payload"]["request_id"]]
        if factorial.DESIGN[g][1] == "negative":
            assert mentions(prompt[:200], concept)
        if g == "A":
            assert not mentions(e["payload"]["text"], concept)
            if e["payload"]["request_id"].endswith(".1"):
                assert not mentions(prompt, concept)

    def means(group):
        return [s["mean"] for e in reasoning if groups[e["agent"]] == group
                for s in e["payload"]["response"]["summaries"] if s["concept"] == concept]
    a, b, c, d = (means(g) for g in "ABCD")
    assert max(a) < min(b) and max(a) < min(d)
    assert sum(c) / len(c) > max(a)  # LegalStudies material names the field where the stub reads

    # Nothing about groups, scenarios or worlds reaches the detector.
    for body in bodies:
        assert re.fullmatch(r"[0-9a-f]{16}\.\d+", body["request_id"])
        assert body["session_id"] == f"s-ep-f-{body['agent']}"
        assert re.fullmatch(r"agent-\d\d", body["agent"])
        text = json.dumps({k: v for k, v in body.items() if k != "messages"})
        assert not re.search(r"group|scenario|world-|positive|negative", text)
        for m in body["messages"]:
            assert not re.search(r"\bgroup [ABCD]\b|world-\d|scenario|factorial", m["content"])
    assert not any(re.search(r"\"group\"|world-\d|factorial", json.dumps(e)) for e in detector)


def test_factorial_analysis_cli(tmp_path, capsys):
    swarm, events, detector, _ = run(tmp_path, "ConstitutionalLaw", steps=16)
    report = factorial.main(["--episode-log", str(swarm.out), "--alerts-log", str(tmp_path / "detector.jsonl"),
                             "--out", str(tmp_path / "report.json")])
    assert json.loads((tmp_path / "report.json").read_text()) == report
    assert report["concept"] == "ConstitutionalLaw"
    assert all(report["groups"][g]["agents"] == 4 for g in factorial.GROUPS)
    assert report["groups"]["B"]["median"] > report["groups"]["A"]["median"]
    assert report["tests"]["B vs A"]["mean"]["auroc"] == 1.0
    assert report["tests"]["D vs A"]["mean"]["auroc"] == 1.0
    assert len(report["time_course"]["A"]) == 16
    alarm = report["alarm"]
    assert alarm["groups"]["B"]["share"] == 1.0 and alarm["groups"]["D"]["share"] == 1.0
    assert alarm["groups"]["A"]["share"] == 0.0
    assert set(alarm["groups"]["B"]["first_alarm_request"].values()) == {5}
    assert alarm["group_A_false_alarm"]["requests_above"] <= 0.05
    out = capsys.readouterr().out
    assert "B vs A" in out and "threshold" in out

    # The alerts log alone supplies summaries when responses were not logged.
    stripped = tmp_path / "stripped.jsonl"
    with stripped.open("w") as f:
        for e in events:
            if e["kind"] == "reasoning":
                e["payload"].pop("response", None)
            f.write(json.dumps(e) + "\n")
    _, _, from_alerts = factorial.load(stripped, tmp_path / "detector.jsonl")
    _, _, from_responses = factorial.load(swarm.out)
    assert from_alerts == from_responses


def test_mann_whitney_and_percentile():
    r = factorial.mann_whitney([3, 4, 5], [1, 2])
    assert r["U"] == 6 and r["auroc"] == 1.0 and r["method"] == "exact" and r["p"] == pytest.approx(0.2)
    r = factorial.mann_whitney([1, 2], [1, 2])
    assert r["auroc"] == 0.5 and r["method"] == "normal"
    assert factorial.percentile(list(range(101)), 95) == 95
    assert factorial.percentile([0, 10], 95) == pytest.approx(9.5)


def test_cli_factorial_flags(tmp_path, monkeypatch, capsys):
    client = TestClient(create_app(StubBackend(latency_ms=0), tmp_path / "d.jsonl"))
    import belay.swarm.runner as runner
    monkeypatch.setattr(runner.httpx, "Client", lambda **kw: client)
    out = tmp_path / "cli.jsonl"
    main(["--agents", "4", "--steps", "3", "--task-steps", "3", "--episode", "ep-cli-f", "--out", str(out),
          "--factorial", "PoliticalTheory", "--positive-fields", "CulinaryArtsAndGastronomicAesthetics"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["factorial"] == "PoliticalTheory"
    assert sorted(a for v in summary["groups"].values() for a in v) == [f"agent-0{i}" for i in range(1, 5)]
    for bad in (["--factorial", "LegalStudies", "--drift", "1"], ["--factorial", "LegalStudies", "--agents", "6"],
                ["--factorial", "LegalStudies", "--positive-fields", "LegalStudies"]):
        with pytest.raises(SystemExit):
            main(["--agents", "4", "--out", str(tmp_path / "x.jsonl"), *bad])
