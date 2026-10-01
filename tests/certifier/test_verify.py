import hashlib
import json

import pytest

from belay.contract import Certificate
from belay.contract.events import EventLog
from belay.contract.stub import StubBackend
from belay.certifier.certify import run_certification
from belay.certifier.verify import verify_direct, verify_swarm


@pytest.fixture
def certificate(stub, suite, tmp_path):
    client, _ = stub()
    r = run_certification(client, suite, seed=1, out_dir=None)
    assert r["passed"]
    return Certificate.model_validate(r["certificate"])


def deploy(stub, cert, tamper="none", seed=0, backend=None):
    client, log = stub(tamper, seed=seed, backend=backend)
    status = client.post("/certificate", json=cert.model_dump(mode="json")).status_code
    return client, log, status


def direct(client, suite, cert, **kw):
    kw.setdefault("rounds", 3)
    return verify_direct(client, suite.canaries, suite.criteria, certificate=cert, max_gap=0, seed=7, **kw)


def test_honest_detector_passes_direct_verification(stub, suite, certificate):
    client, log, _ = deploy(stub, certificate)
    r = direct(client, suite, certificate, alerts_log=log)
    assert r["passed"] and not r["exposed"], r["problems"]
    assert r["canaries"] == 3 * len(suite.canaries)
    assert r["decoys"] > 0
    assert r["canary_recall"]["hits"] == r["canary_recall"]["positives"]
    assert r["alerts_log"]["ok"]


@pytest.mark.parametrize("tamper,checks", [
    ("off", {"canary_recall"}),
    ("threshold", {"canary_recall"}),
    ("swap", {"canary_recall", "canary_false_alarms"}),
])
def test_each_tamper_mode_is_exposed(stub, suite, certificate, tamper, checks):
    # The lab keeps presenting the certificate, because the model hash and
    # profile are unchanged; only the behaviour gives it away.
    client, log, status = deploy(stub, certificate, tamper, seed=3)
    assert status == 200
    r = direct(client, suite, certificate, rounds=10)
    assert r["exposed"], r
    assert r["exposure"]["check"] in checks
    assert 0 < r["exposure"]["requests"] <= r["requests"]
    print(tamper, r["exposure"])


class Substituted(StubBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.model_hash = "sha256:" + hashlib.sha256(b"another-model").hexdigest()


def test_substituted_model_exposed_on_first_request(stub, suite, certificate):
    client, _, status = deploy(stub, certificate, backend=Substituted(latency_ms=0))
    assert status == 409  # the detector refuses a certificate for another model
    r = direct(client, suite, certificate)
    assert r["exposure"]["check"] == "identity" and r["exposure"]["requests"] == 1


def test_altered_log_exposed_in_direct_mode(stub, suite, certificate):
    client, log, _ = deploy(stub, certificate)
    assert direct(client, suite, certificate, rounds=1, alerts_log=log)["passed"]
    lines = log.read_text().splitlines()
    ev = json.loads(lines[0])
    ev["payload"]["alert"]["score"] = 0.5
    lines[0] = json.dumps(ev)
    log.write_text("\n".join(lines) + "\n")
    r = direct(client, suite, certificate, rounds=1, alerts_log=log)
    assert r["exposed"] and r["exposure"]["check"] == "log"
    assert [a["line"] for a in r["alerts_log"]["altered"]] == [1]


# Swarm mode: canaries mixed into live traffic by the swarm --------------------

def run_swarm(client, suite, episode_log, n_tasks=30, steps=2, seed=0):
    """A tiny stand-in for agent E: every task gets an opaque id, canaries included."""
    import random
    rng = random.Random(seed)
    log = EventLog(episode_log, "ep-1")
    tasks = [("%012x" % rng.getrandbits(48), c) for c in suite.canaries]
    tasks += [("%012x" % rng.getrandbits(48), None) for _ in range(n_tasks)]
    rng.shuffle(tasks)
    canaries = []
    for task_id, canary in tasks:
        text = canary.messages()[-1]["content"] if canary else "Summarise the history of the printing press."
        if canary:
            canaries.append(canary.__class__(task_id=task_id, field=canary.field, question=canary.question,
                                             material=canary.material))
        for step in range(steps):
            req = {"request_id": f"{task_id}.{step}", "session_id": "s-1", "agent": f"agent-{rng.randrange(8)}",
                   "messages": [{"role": "user", "content": text}], "episode": "ep-1"}
            resp = client.post("/generate", json=req).json()
            log.write(req["agent"], "message", {"to": None, "text": resp["completion"],
                                                "request_id": req["request_id"], "response": resp})
    return canaries


def test_swarm_mode_honest_passes(stub, suite, certificate, tmp_path):
    client, alerts, _ = deploy(stub, certificate)
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep.jsonl", alerts_log=alerts,
                     certificate=certificate)
    assert r["passed"], r["problems"]
    assert r["canaries_seen"] == len(suite.canaries)


def test_swarm_mode_without_responses_uses_alert_log(stub, suite, certificate, tmp_path):
    client, alerts, _ = deploy(stub, certificate, "off")
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    # Strip the responses: only request ids remain in the episode log.
    lines = [json.loads(l) for l in (tmp_path / "ep.jsonl").read_text().splitlines()]
    for ev in lines:
        ev["payload"].pop("response")
    (tmp_path / "ep2.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in lines))
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep2.jsonl", alerts_log=alerts,
                     certificate=certificate)
    assert r["exposed"] and r["exposure"]["check"] == "canary_recall"


@pytest.mark.parametrize("tamper", ["off", "threshold", "swap"])
def test_swarm_mode_exposes_tamper(stub, suite, certificate, tmp_path, tamper):
    client, alerts, _ = deploy(stub, certificate, tamper, seed=5)
    canaries = []
    for i in range(4):  # several episodes' worth of canaries
        canaries += run_swarm(client, suite, tmp_path / "ep.jsonl", n_tasks=10, seed=i)
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep.jsonl", alerts_log=alerts,
                     certificate=certificate)
    assert r["exposed"], r


def test_swarm_mode_exposes_altered_alert_log(stub, suite, certificate, tmp_path):
    client, alerts, _ = deploy(stub, certificate)
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    lines = alerts.read_text().splitlines()
    ev = json.loads(lines[3])
    ev["payload"]["alert"]["concept"] = "Astronomy"
    lines[3] = json.dumps(ev)
    alerts.write_text("\n".join(lines) + "\n")
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep.jsonl", alerts_log=alerts,
                     certificate=certificate)
    assert r["exposed"] and "log" in r["exposures_by_check"]
    assert r["alerts_log"]["altered"][0]["line"] == 4


def test_swarm_mode_exposes_deleted_alert_record(stub, suite, certificate, tmp_path):
    client, alerts, _ = deploy(stub, certificate)
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    lines = alerts.read_text().splitlines()
    alerts.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep.jsonl", alerts_log=alerts,
                     certificate=certificate)
    assert r["exposed"] and r["exposure"]["check"] == "log"
    assert "missing from the log" in r["exposure"]["detail"]
