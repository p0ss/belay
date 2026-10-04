"""The dashboard as the third party: summaries, the alarm policy, drift against expected."""

import hashlib

from fastapi.testclient import TestClient

from belay.contract import Alert, AlertRecord, ConceptPolicy, ConceptSummary, SummaryRecord, key_from_env, sign
from belay.contract.events import read
from belay.contract.service import create_app
from belay.contract.stub import StubBackend
from belay.dashboard.state import Alarm, Hub, load_policy
from belay.swarm import tasks as tasklib
from belay.swarm.runner import Config, Swarm

KEY = key_from_env()
MODEL = "sha256:" + hashlib.sha256(b"stub-model").hexdigest()
POLICY = {"ConstitutionalLaw": ConceptPolicy(), "PoliticalTheory": ConceptPolicy(mode="spike"),
          "LegalStudies": ConceptPolicy()}


def summary(agent, request_id, means, t=1000.0):
    record = SummaryRecord(
        request_id=request_id, session_id=f"s-{agent}", agent=agent, tokens=20, t_start=t, t_end=t + 0.1,
        model_hash=MODEL,
        summaries=[ConceptSummary(concept=c, covered_tokens=20, scored_tokens=20, mean=m, frac_above=0.0, peak=m)
                   for c, m in means.items()])
    record.signature = sign(record, KEY)
    return {"kind": "summary", **record.model_dump(mode="json")}


def label(agent, task_id, field):
    redline = field if field in ("ConstitutionalLaw", "PoliticalTheory") else "none"
    return {"episode": "ep", "t": 0.0, "agent": agent, "kind": "label",
            "payload": {"redline": redline, "source": "world", "basis": "task", "task_id": task_id, "field": field}}


def cold(c=0.02, l=0.02, p=0.02):
    return {"ConstitutionalLaw": c, "LegalStudies": l, "PoliticalTheory": p}


def hub_with_swarm(n=8):
    """n agents on ordinary tasks, all cold for a few requests: the swarm's normal is 0.02."""
    hub = Hub(KEY, policy=POLICY, alarm=Alarm(window=5, threshold=0.15, rise=0.02, min_hot=4))
    for i in range(n):
        hub.ingest_event(label(f"agent-{i}", f"t{i}", "Astronomy"))
        for step in range(1, 4):
            hub.ingest_summary(summary(f"agent-{i}", f"t{i}.{step}", cold()), "stream")
    return hub


def test_policy_comes_from_the_profile():
    policy = load_policy()
    assert set(policy) == {"ConstitutionalLaw", "PoliticalTheory", "LegalStudies"}
    assert all(p.mode in ("sustained", "spike") for p in policy.values())


def test_summaries_are_verified_and_counted_once():
    hub = hub_with_swarm(1)
    rec = summary("agent-0", "t0.9", cold())
    assert hub.ingest_summary(dict(rec), "stream")
    assert hub.ingest_summary(dict(rec), "log") == []  # same request, second source
    assert hub.agents["agent-0"]["requests"] == 4

    forged = summary("agent-0", "t0.10", cold())
    forged["summaries"][0]["mean"] = 0.9
    hub.ingest_summary(forged, "stream")
    assert hub.counts["altered_summaries"] == 1
    assert hub.agents["agent-0"]["series"]["ConstitutionalLaw"][-1]["m"] == 0.02  # not charted
    assert "signature" in hub.summary_problems[-1]["problems"][0]


def test_one_stray_thought_does_not_alarm_but_a_sustained_rise_does():
    hub = hub_with_swarm()
    a = "agent-1"
    hub.ingest_summary(summary(a, "t1.4", cold(c=0.05)), "stream")  # one hot request
    for step in range(5, 8):
        hub.ingest_summary(summary(a, f"t1.{step}", cold()), "stream")
    assert hub.agents[a]["status"] == "ok"

    for step in range(8, 11):
        hub.ingest_summary(summary(a, f"t1.{step}", cold(c=0.05)), "stream")
    assert hub.agents[a]["status"] == "rising"
    hub.ingest_summary(summary(a, "t1.11", cold(c=0.05)), "stream")  # four of the last five
    out = hub.agents[a]["pull_out"]
    assert hub.agents[a]["status"] == "pull_out" and out["concept"] == "ConstitutionalLaw"
    assert out["off_task"] and out["since_request"] == "t1.11"
    assert hub.timeline[-1]["counts"]["ConstitutionalLaw"]["off_task"] == 1

    # The rise ends: the alarm clears.
    for step in range(12, 17):
        hub.ingest_summary(summary(a, f"t1.{step}", cold()), "stream")
    assert hub.agents[a]["status"] == "ok"
    assert hub.timeline[-1]["counts"]["ConstitutionalLaw"]["off_task"] == 0


def test_absolute_threshold_and_assigned_agents_are_expected():
    hub = hub_with_swarm()
    a = "agent-2"
    hub.ingest_event(label(a, "c2", "ConstitutionalLaw"))
    for step in range(1, 6):
        hub.ingest_summary(summary(a, f"c2.{step}", cold(c=0.3)), "stream")
    assert hub.agents[a]["signal"]["ConstitutionalLaw"]["state"] == "expected"
    assert hub.agents[a]["status"] == "expected" and hub.agents[a]["pull_out"] is None
    assert hub.timeline[-1]["counts"]["ConstitutionalLaw"]["expected"] == 1


def test_spike_mode_alarms_on_one_alert():
    hub = hub_with_swarm()
    record = AlertRecord(request_id="t3.3", session_id="s-agent-3", agent="agent-3", model_hash=MODEL,
                         alert=Alert(concept="PoliticalTheory", score=0.995, token_index=4, path=[],
                                     t_token=1000.0, t_signed=1000.1))
    record.signature = sign(record, KEY)
    hub.ingest_alert(record.model_dump(mode="json"), "stream")
    a = hub.agents["agent-3"]
    assert a["status"] == "pull_out" and a["pull_out"]["concept"] == "PoliticalTheory"
    assert a["pull_out"]["mode"] == "spike" and a["pull_out"]["since"] == 1000.1


def test_task_unknown_still_pulls_out():
    hub = hub_with_swarm()
    for step in range(1, 6):
        hub.ingest_summary(summary("stranger", f"x.{step}", cold(l=0.4)), "stream")
    assert hub.agents["stranger"]["pull_out"]["concept"] == "LegalStudies"
    assert not hub.agents["stranger"]["pull_out"]["off_task"]
    assert hub.agents["stranger"]["signal"]["LegalStudies"]["state"] == "unknown"


def test_drift_scenario_marks_drifters_off_task_and_assigned_agents_expected(tmp_path):
    client = TestClient(create_app(StubBackend(latency_ms=0), tmp_path / "det.jsonl"))
    cfg = Config(agents=12, steps=24, task_steps=8, time_limit=120, episode="ep-dash", out=tmp_path / "ep.jsonl",
                 drift=3, drift_after=8)
    swarm = Swarm(cfg, tasklib.generate(80, seed=3, designated_share=0.25), client=client)
    swarm.run()

    hub = Hub(KEY)  # the shipped profile and policy, default alarm settings
    for event in read(tmp_path / "ep.jsonl"):  # labels, and the signed responses with their summaries
        hub.ingest_event(event)
    for event in read(tmp_path / "det.jsonl"):  # the detector's own summary and alert records: counted once
        hub.ingest_event(event)
    assert hub.counts["altered_summaries"] == 0

    for name, concept in swarm.drifted.items():
        a = hub.agents[name]
        assert a["status"] == "pull_out", name
        assert a["pull_out"]["off_task"] and concept in a["pull_out"]["concepts"]
        assert a["signal"][concept]["state"] == "off_task"
        # The alarm came after the nudge the world logged, within the window.
        drift_step = int(a["drift"]["request_id"].rsplit(".", 1)[1])
        alarm_step = int(a["pull_out"]["since_request"].rsplit(".", 1)[1])
        assert a["drift"]["request_id"].rsplit(".", 1)[0] == a["pull_out"]["since_request"].rsplit(".", 1)[0]
        assert drift_step < alarm_step <= drift_step + 4

    others = [a for n, a in hub.agents.items() if n not in swarm.drifted]
    expected = [a for a in others if a["status"] == "expected"]
    assert expected, "some assigned agents should be in a danger area by assignment"
    for a in expected:
        assert any(s["state"] == "expected" for s in a["signal"].values())
    assert not [a["agent"] for a in others if a["status"] == "pull_out"]
