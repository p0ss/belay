import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from belay.contract import key_from_env
from belay.contract.service import create_app as create_detector
from belay.contract.stub import StubBackend
from belay.dashboard.server import create_app
from belay.dashboard.state import DirWatch, Hub, Tail, concept_class, match_task, summarise_certifier, task_of_request

FIXTURES = Path(__file__).with_name("fixtures")
SWARM = FIXTURES / "swarm-ep.jsonl"


def events():
    return [json.loads(line) for line in SWARM.read_text().splitlines()]


def alert_events():
    return [e for e in events() if e["kind"] == "alert"]


# ---- pure logic ------------------------------------------------------------------------

def test_concept_classes_and_request_ids():
    assert concept_class("ConstitutionalLaw") == "designated"
    assert concept_class("PoliticalTheory") == "designated"
    assert concept_class("LegalStudies") == "near_miss"
    assert concept_class("Astronomy") == "other"
    assert task_of_request("t-01-001.3") == "t-01-001"
    assert task_of_request("canary-7") is None


def test_match_task():
    designated = {"redline": "ConstitutionalLaw", "field": "ConstitutionalLaw"}
    benign = {"redline": "none", "field": "OrganicChemistry"}
    assert match_task("ConstitutionalLaw", designated) == "match"
    assert match_task("PoliticalTheory", designated) == "other_line"
    assert match_task("LegalStudies", designated) == "near_miss"
    assert match_task("ConstitutionalLaw", benign) == "off_task"
    assert match_task("LegalStudies", {"redline": "none", "field": "LegalStudies"}) == "match"
    assert match_task("ConstitutionalLaw", None) == "no_task"


def test_swarm_log_merge_verifies_and_flags_altered_records():
    hub = Hub(key_from_env(), clock=lambda: 1791000001.5)
    for event in events():
        hub.ingest_event(event)

    good, altered = list(hub.alerts)
    assert good["verified"] and good["problems"] == []
    assert good["match"] == "match" and good["task"]["task_id"] == "t-01-001"
    assert good["latency_sign_ms"] == pytest.approx(200, abs=1)
    assert good["latency_arrival_ms"] == pytest.approx(300, abs=1)

    assert not altered["verified"]
    assert altered["concept"] == "LegalStudies"
    assert any("signature" in p for p in altered["problems"])

    assert hub.counts["alerts"] == 2 and hub.counts["altered"] == 1 and hub.counts["designated"] == 1
    # The agent moved on to its next task after the alerts.
    assert hub.agents["agent-01"]["task"]["redline"] == "PoliticalTheory"
    assert hub.agents["agent-01"]["altered"] == 1
    assert hub.agents["agent-03"]["task"]["class"] == "near_miss"
    assert hub.swarm["episode"] == "ep-fixture"


def test_alert_matches_task_by_request_id_not_current_task():
    hub = Hub(key_from_env())
    for event in events():
        if event["kind"] != "alert":
            hub.ingest_event(event)
    # agent-01 is now on t-01-002 (PoliticalTheory), but the alert is for t-01-001.
    hub.ingest_alert(alert_events()[0]["payload"], "stream")
    assert hub.alerts[-1]["task"]["redline"] == "ConstitutionalLaw"
    assert hub.alerts[-1]["match"] == "match"


def test_same_record_from_stream_and_log_is_shown_once():
    hub = Hub(key_from_env())
    record = alert_events()[0]["payload"]
    assert hub.ingest_alert(record, "stream")
    assert hub.ingest_alert(dict(record), "log") == []
    assert len(hub.alerts) == 1 and hub.alerts[0]["source"] == "stream"


def test_wrong_key_and_malformed_records_do_not_verify():
    hub = Hub(b"some-other-key")
    hub.ingest_alert(alert_events()[0]["payload"], "stream")
    assert not hub.alerts[-1]["verified"]
    hub.ingest_alert({"request_id": "x", "alert": {"concept": "PoliticalTheory"}}, "stream")
    assert not hub.alerts[-1]["verified"]
    assert any(p.startswith("malformed") for p in hub.alerts[-1]["problems"])
    assert hub.counts["altered"] == 2


def test_model_hash_mismatch_is_flagged():
    hub = Hub(key_from_env())
    hub.set_detector(identity={"model_hash": "sha256:different"})
    hub.ingest_alert(alert_events()[0]["payload"], "stream")
    assert hub.alerts[-1]["verified"]
    assert any("model hash" in p for p in hub.alerts[-1]["problems"])


def test_listeners_receive_updates():
    hub = Hub(key_from_env())
    got = []
    hub.listeners.append(lambda kind, data: got.append(kind))
    for event in events():
        hub.ingest_event(event)
    assert "alert" in got and "agent" in got
    assert hub.set_detector(stream="live") and not hub.set_detector(stream="live")  # no-op change is silent


# ---- following files -------------------------------------------------------------------

def test_tail_handles_missing_partial_truncated_and_directories(tmp_path):
    path = tmp_path / "swarm" / "ep-1.jsonl"
    tail = Tail(path)
    assert tail.poll() == (None, [], False)

    path.parent.mkdir()
    lines = SWARM.read_text().splitlines()
    path.write_text(lines[0] + "\n" + lines[1][:20])  # second line half-written
    file, got, switched = tail.poll()
    assert file == path and switched and len(got) == 1
    with path.open("a") as f:
        f.write(lines[1][20:] + "\nnot json\n")
    assert len(tail.poll()[1]) == 1 and tail.bad_lines == 1

    path.write_text(lines[2] + "\n")  # truncated and rewritten
    _, got, switched = tail.poll()
    assert switched and got[0]["agent"] == "agent-03"

    dir_tail = Tail(path.parent)
    assert dir_tail.poll()[0] == path
    newer = path.parent / "ep-2.jsonl"
    time.sleep(0.01)
    newer.write_text(lines[3] + "\n")
    file, got, switched = dir_tail.poll()
    assert file == newer and switched and len(got) == 1


def test_certifier_summaries(tmp_path):
    shutil.copytree(FIXTURES / "certifier", tmp_path / "c")
    watch = DirWatch(tmp_path / "c")
    changed, removed = watch.poll()
    by = {name: summarise_certifier(name, data) for name, data, _ in changed}
    assert by["certify.json"]["certified"] is True
    assert by["certify.json"]["certificate_id"] == "cert-fx"
    assert by["verify.json"]["canary"] == {"passed": 3, "failed": 1, "total": 4, "ok": False}
    tamper = {t["mode"]: t for t in by["tamper.json"]["tamper"]}
    assert tamper["off"]["exposed"] is True and tamper["off"]["requests"] == 5
    assert tamper["swap"]["exposed"] is False

    assert watch.poll() == ([], [])  # nothing changed
    (tmp_path / "c" / "verify.json").unlink()
    (tmp_path / "c" / "half.json").write_text('{"kind": ')
    assert watch.poll() == ([], ["verify.json"])


def test_unknown_certifier_format_still_shows_something():
    s = summarise_certifier("odd.json", {"score": 0.9, "note": "x", "nested": {"a": 1}})
    assert s["certified"] is None and ["score", 0.9] in s["details"]
    s = summarise_certifier("canaries.json", {"canaries": [{"passed": True}, {"passed": True}]})
    assert s["canary"]["ok"] is True and s["canary"]["total"] == 2


# ---- the server against a live stub, including a detector restart ---------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub:
    def __init__(self, port, log):
        config = uvicorn.Config(create_detector(StubBackend(latency_ms=0), log), host="127.0.0.1", port=port,
                                log_level="error", timeout_graceful_shutdown=0.2)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "stub did not start"
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def wait_for(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def generate(client, port, text, request_id, agent="agent-01"):
    r = client.post(f"http://127.0.0.1:{port}/generate", json={
        "request_id": request_id, "session_id": "s-1", "agent": agent, "episode": "ep-live",
        "messages": [{"role": "user", "content": text}], "max_tokens": 64})
    r.raise_for_status()


def test_server_follows_stub_and_reconnects_after_restart(tmp_path):
    import httpx

    port = free_port()
    swarm = tmp_path / "swarm"  # does not exist yet
    stub = Stub(port, tmp_path / "det.jsonl")
    app = create_app(f"http://127.0.0.1:{port}", str(swarm), str(tmp_path / "cert"))
    hub = app.state.hub
    try:
        with TestClient(app) as dash, httpx.Client(timeout=5) as http:
            assert dash.get("/").status_code == 200
            assert wait_for(lambda: hub.detector["stream"] == "live" and hub.detector["health"] == "ok")
            assert hub.swarm["status"] == "waiting"
            assert hub.detector["identity"]["certificate_id"] is None

            swarm.mkdir()
            (swarm / "ep-live.jsonl").write_text(json.dumps(
                {"episode": "ep-live", "t": 0.0, "agent": "agent-01", "kind": "label",
                 "payload": {"redline": "none", "source": "world", "task_id": "t-9", "field": "History"}}) + "\n")
            assert wait_for(lambda: hub.swarm["status"] == "following" and "agent-01" in hub.agents)

            generate(http, port, "the nineteenth amendment and suffrage", "t-9.1")
            assert wait_for(lambda: len(hub.alerts) == 1)
            a = hub.alerts[-1]
            assert a["verified"] and a["concept"] == "ConstitutionalLaw" and a["match"] == "off_task"
            assert a["latency_sign_ms"] is not None and a["latency_arrival_ms"] is not None

            stub.stop()
            assert wait_for(lambda: hub.detector["stream"] == "reconnecting")
            stub = Stub(port, tmp_path / "det.jsonl")
            assert wait_for(lambda: hub.detector["stream"] == "live", timeout=15)
            generate(http, port, "the social contract in hobbes", "t-9.2")
            assert wait_for(lambda: len(hub.alerts) == 2)

            # A detector restart that leaves the old process holding the stream:
            # health recovering triggers a fresh subscription.
            since = hub.detector["since"]
            dash.portal.call(app.state.resubscribe.set)
            assert wait_for(lambda: hub.detector["stream"] == "live" and hub.detector["since"] != since)
            generate(http, port, "rawls and liberalism", "t-9.3")
            assert wait_for(lambda: len(hub.alerts) == 3)

            state = dash.get("/state").json()
            assert state["counts"]["alerts"] == 3 and state["counts"]["off_task"] == 3
            assert {a["concept"] for a in state["alerts"]} == {"ConstitutionalLaw", "PoliticalTheory"}
    finally:
        stub.stop()


def test_alert_before_its_label_is_resolved_when_the_label_arrives():
    hub = Hub(key_from_env())
    got = []
    hub.listeners.append(lambda kind, data: got.append(kind))
    hub.ingest_alert(alert_events()[0]["payload"], "stream")
    assert hub.alerts[-1]["match"] == "no_task" and hub.counts["match"] == 0
    hub.ingest_event(events()[0])  # the label for t-01-001
    assert hub.alerts[-1]["match"] == "match" and hub.counts["match"] == 1
    assert "alert_update" in got
