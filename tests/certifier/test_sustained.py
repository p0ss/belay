"""The sustained signal: AUROC, bootstrap CIs, operating points, and the separation check in operation."""

import json
import random

import pytest

from belay.contract import Certificate, ConceptSummary
from belay.certifier.certify import run_certification
from belay.certifier.checks import Baseline
from belay.certifier.cli import _baseline_from
from belay.certifier.suite import Canary
from belay.certifier.sustained import auroc, bootstrap_ci, operating_point, separation_test, task_scores
from belay.certifier.verify import Monitor, verify_direct, verify_swarm

from .test_verify import deploy, run_swarm


def test_auroc_counts_ties_half():
    assert auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auroc([0.1], [0.9]) == 0.0
    assert auroc([0.5, 0.5], [0.5]) == 0.5
    assert auroc([0.3, 0.6], [0.3, 0.5]) == pytest.approx((0.5 + 0 + 1 + 1) / 4)
    assert auroc([], [0.1]) is None


def test_bootstrap_ci_is_reproducible_and_brackets_the_estimate():
    rng = random.Random(0)
    pos = [rng.gauss(1, 1) for _ in range(40)]
    neg = [rng.gauss(0, 1) for _ in range(120)]
    a = auroc(pos, neg)
    lo, hi = bootstrap_ci(pos, neg, random.Random(1), reps=500)
    assert lo < a < hi and 0.1 < hi - lo < 0.4
    assert bootstrap_ci(pos, neg, random.Random(1), reps=500) == [lo, hi]
    assert bootstrap_ci([], neg, random.Random(1)) is None


def test_operating_point_is_the_lowest_score_within_the_target_fpr():
    neg = [i / 100 for i in range(100)]  # 0.00 .. 0.99
    pos = [0.5, 0.95, 0.99, 1.0]
    op = operating_point(pos, neg, 0.10, hard=[0.85, 0.95])
    # 10 of 100 negatives (0.90..0.99) lie above 0.89.
    assert op["threshold"] == 0.89 and op["fpr"] == 0.10
    assert op["recall"] == 0.75 and op["fpr_hard"] == 0.5
    # All negatives tied: the threshold is that score and nothing tied counts as a hit.
    assert operating_point([0.5], [0.02] * 10, 0.10)["threshold"] == 0.02


def test_task_scores_weight_requests_by_covered_tokens():
    def s(mean, n):
        return ConceptSummary(concept="X", covered_tokens=n, scored_tokens=n, mean=mean, frac_above=0, peak=mean)
    assert task_scores([[s(0.9, 10)], [s(0.0, 30)]])["X"] == pytest.approx(0.225)
    assert task_scores([[s(0.9, 10)], [s(0.0, 30)]], "peak")["X"] == 0.9


def test_separation_test():
    good = separation_test({"A": {"pos": [0.9] * 6, "neg": [0.02] * 20, "floor": 0.83}}, alpha=0.001)
    assert good["tested"] and not good["exposed"] and good["auroc"] == 1.0
    flat = separation_test({"A": {"pos": [0.02] * 10, "neg": [0.02] * 30, "floor": 0.83}}, alpha=0.001)
    assert flat["auroc"] == 0.5 and flat["exposed"]
    few = separation_test({"A": {"pos": [0.02] * 3, "neg": [0.02] * 30, "floor": 0.83}}, alpha=0.001)
    assert not few["tested"] and not few["exposed"]


def test_separation_is_threshold_free(suite):
    """A detector whose alerts look right but whose summaries are flat is exposed by the Mann-Whitney check alone."""
    m = Monitor(suite.criteria, b"k")  # no operating points: canaries scored by alerts
    assert m.basis == "alerts"
    for i in range(40):
        field = ["ConstitutionalLaw", "PoliticalTheory", "Astronomy"][i % 3]
        expected = {field} - {"Astronomy"}
        m.canary(Canary(task_id=f"t{i}", field=field, question="q"), alerted=expected,
                 scores={c: 0.02 for c in suite.criteria.concepts})
    s = m.summary()
    assert s["canary_recall"]["hits"] == s["canary_recall"]["positives"]
    assert set(s["exposures_by_check"]) == {"canary_separation"}


def test_canaries_scored_against_operating_points(suite):
    b = Baseline("m", "p", "h", operating_points={"ConstitutionalLaw": 0.05, "PoliticalTheory": 0.05,
                                                  "LegalStudies": 0.05})
    m = Monitor(suite.criteria, b"k", b)
    assert m.basis == "sustained"
    can = Canary(task_id="a", field="ConstitutionalLaw", question="q")
    m.canary(can, alerted={"ConstitutionalLaw"}, scores={"ConstitutionalLaw": 0.04, "PoliticalTheory": 0.06})
    # The alert does not count; the summary below the operating point is a miss, and
    # PoliticalTheory above it is a false alarm.
    assert (m.pos_n, m.pos_hits, m.neg_n, m.neg_false) == (1, 0, 1, 1)
    m.canary(can, alerted=set(), scores={"ConstitutionalLaw": 0.051, "PoliticalTheory": 0.05})
    assert (m.pos_hits, m.neg_false) == (1, 1)


@pytest.fixture
def certified(stub, suite, tmp_path):
    client, _ = stub()
    r = run_certification(client, suite, seed=1, out_dir=None)
    return r, Certificate.model_validate(r["certificate"])


def test_honest_verification_uses_the_sustained_signal(stub, suite, certified):
    _, cert = certified
    client, log, _ = deploy(stub, cert)
    r = verify_direct(client, suite.canaries, suite.criteria, certificate=cert, max_gap=0, seed=7, rounds=3,
                      alerts_log=log)
    assert r["passed"] and r["basis"] == "sustained"
    assert r["operating_points"] == {c: v["threshold"] for c, v in cert.results["operating_points"].items()}
    sep = r["canary_separation"]
    assert sep["tested"] and not sep["exposed"] and sep["auroc"] == 1.0


@pytest.mark.parametrize("tamper", ["off", "threshold", "swap"])
def test_tamper_exposed_on_the_sustained_basis(stub, suite, certified, tamper):
    _, cert = certified
    client, _, _ = deploy(stub, cert, tamper, seed=3)
    r = verify_direct(client, suite.canaries, suite.criteria, certificate=cert, max_gap=0, seed=7, rounds=10,
                      stop_on_expose=False)
    assert r["basis"] == "sustained" and r["exposed"]
    assert r["exposure"]["check"] in ("canary_recall", "canary_false_alarms")
    # The threshold-free check exposes it too.
    assert "canary_separation" in r["exposures_by_check"], r["canary_separation"]


def test_baseline_from_results_carries_operating_points(certified, tmp_path):
    r, _ = certified
    path = tmp_path / "certify.json"
    path.write_text(json.dumps(r))
    b = _baseline_from(str(path))
    assert b.operating_points == {c: v["threshold"] for c, v in r["operating_points"].items()}
    assert b.statistic == "mean"


def test_swarm_canaries_scored_from_summary_records(stub, suite, certified, tmp_path):
    """With responses stripped and every alert deleted, the signed summaries still carry the canaries."""
    _, cert = certified
    client, alerts, _ = deploy(stub, cert)
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    events = [json.loads(l) for l in (tmp_path / "ep.jsonl").read_text().splitlines()]
    for ev in events:
        ev["payload"].pop("response")
    (tmp_path / "ep2.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in events))
    lines = [l for l in alerts.read_text().splitlines() if json.loads(l)["kind"] == "summary"]
    alerts.write_text("\n".join(lines) + "\n")
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep2.jsonl", alerts_log=alerts,
                     certificate=cert)
    assert r["passed"] and r["basis"] == "sustained", r["exposure"]
    assert r["canary_recall"]["hits"] == r["canary_recall"]["positives"] > 0


def test_swarm_canary_without_summaries_is_a_miss(stub, suite, certified, tmp_path):
    """Deleting a canary's summaries hides nothing: with operating points, no signal is no detection."""
    _, cert = certified
    client, alerts, _ = deploy(stub, cert)
    canaries = run_swarm(client, suite, tmp_path / "ep.jsonl")
    events = [json.loads(l) for l in (tmp_path / "ep.jsonl").read_text().splitlines()]
    for ev in events:
        ev["payload"].pop("response")
    (tmp_path / "ep2.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in events))
    alerts.write_text("")
    r = verify_swarm(canaries, suite.criteria, episode_log=tmp_path / "ep2.jsonl", alerts_log=alerts,
                     certificate=cert)
    assert r["canary_recall"]["hits"] == 0 and r["exposed"]
