import pytest

from belay.contract import Certificate, key_from_env, verify
from belay.contract.signing import CERTIFIER_KEY_ENV
from belay.contract.stub import StubBackend
from belay.certifier.certify import run_certification


def test_certifies_honest_stub_and_installs_certificate(stub, suite, tmp_path):
    client, log = stub()
    r = run_certification(client, suite, seed=1, concurrency=4, alerts_log=None, out_dir=tmp_path)
    assert r["passed"], r["problems"]
    assert r["basis"] == "sustained" and r["concept_score"]["basis"] == "sustained"
    for name, c in r["concepts"].items():
        assert c["passed"] and c["basis"] == "sustained", (name, c)
    # Token-level results are kept, labelled, alongside.
    tl = r["token_level"]["concepts"]
    assert tl["ConstitutionalLaw"]["recall"] == 1.0
    assert tl["PoliticalTheory"]["fpr"] == 0.0
    su = r["sustained"]["concepts"]
    for name, c in su.items():
        assert c["auroc"] == 1.0 and c["auroc_hard"] == 1.0 and c["auroc_ci95"] == [1.0, 1.0], (name, c)
    # Stub: 0.995 at keyword tokens, 0.02 elsewhere, so every negative scores 0.02.
    op = r["operating_points"]["ConstitutionalLaw"]
    assert op["threshold"] == 0.02 and op["fpr"] == 0.0 and op["recall"] == 1.0 and op["target_fpr"] == 0.1

    cert = Certificate.model_validate(r["certificate"])
    assert cert.results["basis"] == "sustained"
    assert cert.results["operating_points"] == r["operating_points"]
    assert cert.results["sustained"]["concepts"]["PoliticalTheory"]["auroc"] == 1.0
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
    events = list(read(log))
    assert all(e["payload"]["request_id"] for e in events)
    # One summary per request: every request has its own opaque task id.
    rids = [e["payload"]["request_id"] for e in events if e["kind"] == "summary"]
    assert len(rids) == len(suite.cases)
    assert all("." in r and not r.startswith("c0") for r in rids)
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
    """Honest about switching the lenses off: reports nothing watched and no token covered."""
    def generate(self, request):
        res = super().generate(request)
        res.watched = 0
        res.summaries = [s.model_copy(update={"covered_tokens": 0, "scored_tokens": 0, "mean": 0.0,
                                              "frac_above": 0.0, "peak": 0.0, "peak_token": None})
                         for s in res.summaries]
        return res


class PartlyCovered(StubBackend):
    """Claims summaries, but one concept covers only half the tokens."""
    def generate(self, request):
        res = super().generate(request)
        res.summaries = [s.model_copy(update={"covered_tokens": s.covered_tokens // 2})
                         if s.concept == "PoliticalTheory" else s for s in res.summaries]
        return res


class NoSummaries(StubBackend):
    """An older detector: no summaries, so coverage is judged by `watched`."""
    def __init__(self, *a, watched=3, **kw):
        super().__init__(*a, **kw)
        self._watched = watched

    def generate(self, request):
        res = super().generate(request)
        res.summaries = []
        res.watched = self._watched
        return res


def test_absence_is_not_silence(stub, suite, tmp_path):
    client, _ = stub(backend=Uncovered("off", latency_ms=0))
    r = run_certification(client, suite, seed=1, out_dir=None)
    assert not r["passed"]
    assert r["checks"]["coverage"]["failures"] >= len(suite.cases)
    assert r["silent_uncovered_responses"] == len(suite.cases)
    assert any("covered 0 of" in p["detail"] for p in r["problems"])


def test_coverage_needs_every_token_of_every_concept(stub, suite):
    client, _ = stub(backend=PartlyCovered(latency_ms=0))
    r = run_certification(client, suite, seed=1, out_dir=None)
    assert not r["passed"] and r["certificate"] is None
    assert r["checks"]["coverage"]["failures"] == len(suite.cases)
    assert all("PoliticalTheory covered" in p["detail"] for p in r["problems"] if p["check"] == "coverage")


def test_responses_without_summaries(stub, suite):
    import dataclasses
    # With a sustained block, a response without summaries is missing coverage.
    client, _ = stub(backend=NoSummaries(latency_ms=0))
    r = run_certification(client, suite, seed=1, out_dir=None)
    assert r["checks"]["coverage"]["failures"] == len(suite.cases)
    assert "no summaries" in r["problems"][0]["detail"]
    # Without one, the old `watched` check applies and certification is token-level.
    token_only = dataclasses.replace(suite, criteria=dataclasses.replace(suite.criteria, sustained=None))
    r = run_certification(client, token_only, seed=1, out_dir=None)
    assert r["passed"] and r["basis"] == "token_level" and r["sustained"] is None
    client, _ = stub(backend=NoSummaries(latency_ms=0, watched=1))
    r = run_certification(client, token_only, seed=1, out_dir=None)
    assert r["checks"]["coverage"]["failures"] == len(suite.cases)
    assert "watched 1 < 3" in r["problems"][0]["detail"]


def test_wrong_lab_key_fails_signatures(stub, suite):
    client, _ = stub()
    r = run_certification(client, suite, seed=1, out_dir=None, lab_key=b"not-the-lab-key")
    assert not r["passed"] and r["checks"]["signature"]["failures"] == len(suite.cases)
