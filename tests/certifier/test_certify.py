import pytest

from belay.contract import Certificate, key_from_env, verify
from belay.contract.signing import CERTIFIER_KEY_ENV
from belay.contract.stub import StubBackend
from belay.certifier.certify import run_certification


def test_certifies_honest_stub_and_installs_certificate(stub, suite, tmp_path):
    client, log = stub()
    r = run_certification(client, suite, seed=1, concurrency=4, alerts_log=None, out_dir=tmp_path)
    assert r["passed"], r["problems"]
    for name, c in r["concepts"].items():
        assert c["passed"], (name, c)
    assert r["concepts"]["ConstitutionalLaw"]["recall"] == 1.0
    assert r["concepts"]["PoliticalTheory"]["fpr"] == 0.0

    cert = Certificate.model_validate(r["certificate"])
    assert verify(cert, key_from_env(CERTIFIER_KEY_ENV))
    assert not verify(cert, key_from_env())  # signed by the certifier, not the lab
    assert r["installed"]["status"] == 200
    assert (tmp_path / f"certificate-{cert.certificate_id}.json").exists()

    ident = client.post("/generate", json={"request_id": "x.0", "session_id": "s", "agent": "agent-1",
                                            "messages": [{"role": "user", "content": "hi"}]}).json()["identity"]
    assert ident["certificate_id"] == cert.certificate_id
    assert ident["model_hash"] == cert.model_hash


def test_requests_look_like_ordinary_traffic(stub, suite, tmp_path):
    client, log = stub()
    run_certification(client, suite, seed=2, out_dir=None, install=False, episode="ep-x")
    from belay.contract.events import read
    rids = [e["payload"]["request_id"] for e in read(log)]
    assert rids and all("." in r and not r.startswith("c0") for r in rids)
    assert len({r.split(".")[0] for r in rids}) == len(rids)


def test_alert_log_audited_during_certification(stub, suite, tmp_path):
    client, log = stub()
    r = run_certification(client, suite, seed=3, alerts_log=log, out_dir=None, install=False)
    assert r["passed"] and r["alerts_log"]["verified"] > 0


@pytest.mark.parametrize("tamper", ["off", "threshold", "swap"])
def test_tampered_stub_fails_certification(stub, suite, tmp_path, tamper):
    client, _ = stub(tamper)
    r = run_certification(client, suite, seed=1, out_dir=tmp_path)
    assert not r["passed"]
    assert r["certificate"] is None
    assert not r["concepts"]["ConstitutionalLaw"]["passed"] or not r["concepts"]["PoliticalTheory"]["passed"]


class Uncovered(StubBackend):
    """Honest about switching the lenses off: reports nothing watched."""
    def generate(self, request):
        res = super().generate(request)
        res.watched = 0
        return res


def test_absence_is_not_silence(stub, suite, tmp_path):
    client, _ = stub(backend=Uncovered("off", latency_ms=0))
    r = run_certification(client, suite, seed=1, out_dir=None)
    assert not r["passed"]
    assert r["checks"]["coverage"]["failures"] == len(suite.cases)
    assert r["silent_uncovered_responses"] == len(suite.cases)


def test_wrong_lab_key_fails_signatures(stub, suite):
    client, _ = stub()
    r = run_certification(client, suite, seed=1, out_dir=None, lab_key=b"not-the-lab-key")
    assert not r["passed"] and r["checks"]["signature"]["failures"] == len(suite.cases)
