from belay.certifier.certify import concept_score


def test_concept_score_counts_by_role():
    concepts = {
        "ConstitutionalLaw": {"role": "designated", "passed": False},
        "PoliticalTheory": {"role": "designated", "passed": True},
        "LegalStudies": {"role": "near_miss", "passed": True},
    }
    s = concept_score(concepts)
    assert (s["passed"], s["total"], s["share"]) == (2, 3, 0.6667)
    assert s["by_role"]["designated"] == {"passed": 1, "total": 2, "share": 0.5}
    assert s["failed"] == ["ConstitutionalLaw"]
