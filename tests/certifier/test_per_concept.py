"""Certification is per concept: a certificate covers the concepts that met their criteria."""

from belay.contract import Certificate
from belay.contract.stub import StubBackend
from belay.certifier.certify import run_certification
from belay.certifier.verify import verify_direct


class BlindToConstitutionalLaw(StubBackend):
    """A detector whose ConstitutionalLaw lens never fires: that concept fails, the rest pass."""

    def generate(self, request, emit=None):
        kept = []
        result = super().generate(request, emit=kept.append)
        result.alerts = [a for a in kept if a.concept != "ConstitutionalLaw"]
        return result


def test_partial_certificate_covers_only_concepts_that_passed(stub, suite, tmp_path):
    client, _ = stub(backend=BlindToConstitutionalLaw(latency_ms=0))
    r = run_certification(client, suite, seed=1, out_dir=tmp_path)
    assert not r["passed"]
    assert r["certificate_scope"] == "partial"
    assert r["uncertified_concepts"] == ["ConstitutionalLaw"]
    cert = Certificate.model_validate(r["certificate"])
    assert "ConstitutionalLaw" not in cert.concepts and "PoliticalTheory" in cert.concepts
    assert cert.uncertified == ["ConstitutionalLaw"]

    ident = client.get("/health").json()["identity"]
    assert ident["certificate_id"] == cert.certificate_id
    assert sorted(ident["certified_concepts"]) == sorted(cert.concepts)


def test_full_certificate_scope(stub, suite, tmp_path):
    client, _ = stub()
    r = run_certification(client, suite, seed=1, out_dir=tmp_path)
    assert r["passed"] and r["certificate_scope"] == "full" and r["uncertified_concepts"] == []


def test_no_certificate_without_a_certified_red_line(stub, suite, tmp_path):
    client, _ = stub("off")
    r = run_certification(client, suite, seed=1, out_dir=tmp_path)
    assert r["certificate"] is None and r["certificate_scope"] is None


def test_verification_holds_only_certified_concepts_to_criteria(stub, suite, tmp_path):
    client, _ = stub(backend=BlindToConstitutionalLaw(latency_ms=0))
    cert = Certificate.model_validate(run_certification(client, suite, seed=1, out_dir=tmp_path)["certificate"])
    r = verify_direct(client, suite.canaries, suite.criteria, certificate=cert, max_gap=0, seed=7)
    assert r["passed"], r.get("exposure")
    assert r["canary_recall"]["hits"] == r["canary_recall"]["positives"]

    # Held to every concept, the blind lens's canaries count as misses.
    strict = cert.model_copy(update={"concepts": []})
    r = verify_direct(client, suite.canaries, suite.criteria, certificate=strict, max_gap=0, seed=7)
    assert r["canary_recall"]["hits"] < r["canary_recall"]["positives"]
