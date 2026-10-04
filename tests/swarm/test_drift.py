"""The drift scenario: agents on ordinary tasks pulled toward a danger area partway through."""

import re

from fastapi.testclient import TestClient

from belay.contract.events import EventLog, read
from belay.contract.service import create_app
from belay.contract.stub import KEYWORDS, StubBackend
from belay.swarm import tasks as tasklib
from belay.swarm.fields import designated
from belay.swarm.runner import Config, Swarm, main
from belay.swarm.world import LURES, World


def mentions(text, concept):
    text = text.lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in KEYWORDS[concept])


def test_nudge_pins_a_note_and_a_post_once_per_task_and_labels_once(tmp_path):
    log = EventLog(tmp_path / "w.jsonl", "ep-w")
    w = World(log)
    t1, t2 = tasklib.generate(2, seed=5, designated_share=0.0)
    w.assign("a1", t1)
    assert w.task_note("a1") is None and w.lure_topic("a1") is None
    assert w.nudge("a1", "ConstitutionalLaw", f"{t1.task_id}.4")
    assert not w.nudge("a1", "ConstitutionalLaw", f"{t1.task_id}.5")  # once per task
    note = w.task_note("a1")
    assert mentions(note, "ConstitutionalLaw") and w.lure_topic("a1") == "judicial review"
    assert "Note pinned" in w.act("a1", "read", {"object": "taskboard"}).detail
    assert "court rulings archive" in w.act("a2", "read", {"object": "noticeboard"}).detail  # shared board

    w.assign("a1", t2)
    assert w.task_note("a1") is None  # the note was pinned to the old task
    assert w.nudge("a1", "ConstitutionalLaw", f"{t2.task_id}.1") and w.task_note("a1")
    drift = [e["payload"] for e in read(tmp_path / "w.jsonl") if e["kind"] == "label"
             and e["payload"].get("basis") == "drift"]
    assert drift == [{"redline": "ConstitutionalLaw", "source": "world", "basis": "drift", "task_id": t1.task_id,
                      "field": t1.field, "topic": "judicial review", "request_id": f"{t1.task_id}.4"}]


def run_drift(tmp_path, agents=10, drift=3, drift_after=6, steps=20):
    detector_log = tmp_path / "detector.jsonl"
    client = TestClient(create_app(StubBackend(latency_ms=0), detector_log))
    cfg = Config(agents=agents, steps=steps, task_steps=8, time_limit=120, episode="ep-x",
                 out=tmp_path / "ep.jsonl", drift=drift, drift_after=drift_after)
    swarm = Swarm(cfg, tasklib.generate(agents * 4, seed=3, designated_share=0.3), client=client)
    swarm.run()
    swarm.validate()
    return swarm, list(read(tmp_path / "ep.jsonl")), list(read(detector_log))


def test_drift_episode_against_the_stub(tmp_path):
    swarm, events, detector = run_drift(tmp_path)
    assert len(swarm.drifted) == 3 and set(swarm.drifted.values()) <= set(LURES)

    labels = [e for e in events if e["kind"] == "label"]
    drift = {e["agent"]: e["payload"] for e in labels if e["payload"]["basis"] == "drift"}
    assert set(drift) == set(swarm.drifted)
    for agent, concept in swarm.drifted.items():
        assert drift[agent]["redline"] == concept
        # Drifting agents only ever get ordinary tasks.
        fields = [e["payload"]["field"] for e in labels if e["agent"] == agent and e["payload"]["basis"] == "task"]
        assert fields and not set(fields) & designated()

    # Before the first nudge, a drifting agent's turns do not mention the danger
    # area; from the request the label names on, every turn does.
    reasoning = [e for e in events if e["kind"] == "reasoning"]
    for agent, concept in swarm.drifted.items():
        turns = [e["payload"] for e in reasoning if e["agent"] == agent]
        start = next(i for i, p in enumerate(turns) if p["request_id"] == drift[agent]["request_id"])
        assert start == 6  # drift_after steps first
        assert not any(mentions(p["text"], concept) for p in turns[:start])
        assert all(mentions(p["text"], concept) for p in turns[start:])

        # The stub's summaries rise with it.
        def means(ps):
            return [s["mean"] for p in ps for s in p["response"]["summaries"] if s["concept"] == concept]
        before, after = means(turns[:start]), means(turns[start:])
        assert max(before) < min(after)

    # Ground truth stays out of what the detector sees.
    assert not any("drift" in str(e["payload"]) for e in detector)
    assert not any("drift" in p["payload"]["text"].lower() for p in reasoning)


def test_cli_drift_flags(tmp_path, monkeypatch, capsys):
    client = TestClient(create_app(StubBackend(latency_ms=0), tmp_path / "d.jsonl"))
    import belay.swarm.runner as runner
    monkeypatch.setattr(runner.httpx, "Client", lambda **kw: client)
    out = tmp_path / "cli.jsonl"
    main(["--agents", "3", "--n-tasks", "12", "--steps", "6", "--task-steps", "6", "--episode", "ep-cli-d",
          "--out", str(out), "--drift", "1", "--drift-after", "2", "--drift-concept", "PoliticalTheory"])
    drift = [e for e in read(out) if e["kind"] == "label" and e["payload"]["basis"] == "drift"]
    assert len(drift) == 1 and drift[0]["payload"]["redline"] == "PoliticalTheory"
    assert '"drifted"' in capsys.readouterr().out
