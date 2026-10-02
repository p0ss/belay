from belay.dashboard.state import summarise_certifier

VERIFY = {
    "kind": "verification", "mode": "swarm", "passed": False, "exposed": True,
    "canary_recall": {"hits": 0, "positives": 5, "rate": 0.0, "recall_min": 0.8},
    "canary_false_alarms": {"false_alarms": 0, "negatives": 7, "rate": 0.0, "fpr_max": 0.1},
    "exposure": {"check": "canary_recall", "detail": "0/5", "requests": 189, "seconds": 0.28},
    "certificate_id": "cert-1",
}


def test_reads_certifier_verify_results():
    out = summarise_certifier("verify-x.json", VERIFY)
    assert out["canary"] == {"passed": 0, "failed": 5, "total": 12, "ok": False}
    assert out["tamper"] == [{"mode": "canary_recall", "exposed": True, "requests": 189, "seconds": 0.28}]
    assert out["certificate_id"] == "cert-1"


def test_passes_concept_score_through():
    data = {"kind": "certification", "passed": False,
            "concept_score": {"passed": 2, "total": 3, "share": 0.6667, "failed": ["ConstitutionalLaw"]}}
    assert summarise_certifier("certify.json", data)["concept_score"]["passed"] == 2
