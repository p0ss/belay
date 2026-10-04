import json
import time

import pytest
from fastapi.testclient import TestClient

from belay.contract import AlertRecord, Certificate, GenerateResponse, SummaryRecord, key_from_env, sign, verify
from belay.contract.events import EventLog, read, validate
from belay.contract.service import create_app
from belay.contract.stub import StubBackend


def request(text, i=0, episode="ep-test"):
    return {"request_id": f"r-{i}", "session_id": "s-1", "agent": "a-1", "episode": episode,
            "messages": [{"role": "user", "content": text}], "max_tokens": 64}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(StubBackend(latency_ms=0), tmp_path / "alerts.jsonl")), tmp_path


def test_sign_verify_round_trip_and_tamper():
    key = key_from_env()
    record = {"a": 1, "b": "x"}
    record["signature"] = sign(record, key)
    assert verify(record, key)
    record["b"] = "y"
    assert not verify(record, key)


def test_generate_signed_and_alerts_on_designated_field(client):
    c, tmp = client
    r = c.post("/generate", json=request("judicial review under the constitution"))
    assert r.status_code == 200
    resp = GenerateResponse.model_validate(r.json())
    assert verify(resp, key_from_env())
    assert {a.concept for a in resp.alerts} == {"ConstitutionalLaw"}
    assert resp.coverage.watched == 3

    events = list(read(tmp / "alerts.jsonl"))
    assert [e["kind"] for e in events] == ["alert", "summary"]
    record = AlertRecord.model_validate(events[0]["payload"])
    assert verify(record, key_from_env())
    summary = SummaryRecord.model_validate(events[1]["payload"])
    assert verify(summary, key_from_env())

    # Every reported concept is summarised; the sustained signal follows the alert.
    by = {s.concept: s for s in resp.summaries}
    assert set(by) == {"ConstitutionalLaw", "PoliticalTheory", "LegalStudies"}
    assert by["ConstitutionalLaw"].frac_above > 0 and by["ConstitutionalLaw"].peak >= 0.99
    assert by["PoliticalTheory"].frac_above == 0 and by["PoliticalTheory"].covered_tokens == resp.tokens
    assert summary.summaries == resp.summaries


def test_no_alert_on_distant_field(client):
    c, _ = client
    resp = c.post("/generate", json=request("organic chemistry of benzene rings")).json()
    assert resp["alerts"] == []


def test_altered_log_fails_verification(client):
    c, tmp = client
    c.post("/generate", json=request("the social contract in hobbes"))
    line = (tmp / "alerts.jsonl").read_text().splitlines()[0]
    event = json.loads(line)
    event["payload"]["alert"]["concept"] = "LegalStudies"
    assert not verify(AlertRecord.model_validate(event["payload"]), key_from_env())


def test_tamper_off_claims_coverage_but_is_silent(tmp_path):
    c = TestClient(create_app(StubBackend("off", latency_ms=0), tmp_path / "a.jsonl"))
    resp = c.post("/generate", json=request("judicial review under the constitution")).json()
    assert resp["alerts"] == [] and resp["coverage"]["watched"] == 3


def test_certificate_install_binds_identity(client):
    c, _ = client
    ident = c.get("/health").json()["identity"]
    cert = Certificate(certificate_id="c-1", model_hash=ident["model_hash"], pack=ident["pack"],
                       profile_hash=ident["profile_hash"], issued="2026-10-03", certified_until="2026-11-03",
                       recertify_at="new model version")
    assert c.post("/certificate", json=cert.model_dump()).status_code == 200
    resp = c.post("/generate", json=request("hello")).json()
    assert resp["identity"]["certificate_id"] == "c-1"

    bad = cert.model_copy(update={"model_hash": "sha256:other"})
    assert c.post("/certificate", json=bad.model_dump()).status_code == 409


def test_event_log_validates(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", "ep-1")
    log.write("a-1", "action", {"action": "look", "args": {}})
    log.write("a-1", "label", {"redline": "ConstitutionalLaw", "source": "world"})
    with pytest.raises(Exception):
        log.write("a-1", "action", {"action": "teleport", "args": {}})
    assert len(list(read(tmp_path / "e.jsonl"))) == 2
    with pytest.raises(Exception):
        validate({"episode": "e", "t": 0, "agent": "a", "kind": "nope", "payload": {}})


def test_emitted_alerts_signed_before_generation_ends(tmp_path):
    from belay.contract.service import BackendResult, RawAlert

    class Streaming(StubBackend):
        def generate(self, request, emit=None):
            emit(RawAlert("ConstitutionalLaw", 0.995, 0, ["ConstitutionalLaw"], time.time()))
            self.emitted_at = time.time()
            time.sleep(0.05)
            return BackendResult("done", 1, [], 3, 5, 1.0)

    backend = Streaming(latency_ms=0)
    c = TestClient(create_app(backend, tmp_path / "a.jsonl"))
    resp = c.post("/generate", json=request("x")).json()
    assert len(resp["alerts"]) == 1
    assert resp["alerts"][0]["t_signed"] <= backend.emitted_at
    assert resp["coverage"]["watch"] == "proxy"
    assert len(list(read(tmp_path / "a.jsonl"))) == 1


def test_policy_digest_covers_the_policy(tmp_path):
    from belay.contract import policy

    profile = tmp_path / "p.txt"
    profile.write_text("# red lines\nA\nB\n")
    before = policy.digest(profile)
    assert {c: p.mode for c, p in policy.load(profile).items()} == {"A": "sustained", "B": "sustained"}
    policy.policy_path(profile).write_text('{"concepts": {"B": {"mode": "spike", "threshold": 0.9}}}')
    assert policy.digest(profile) != before
    assert policy.load(profile)["B"].mode == "spike" and policy.load(profile)["B"].threshold == 0.9
