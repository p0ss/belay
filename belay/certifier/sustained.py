"""
The sustained signal: per request, each reported concept's ConceptSummary.
Certification scores it threshold-free (AUROC between cases that should carry
a concept and cases that should not); verification scores canaries against an
operating point chosen from the certification run before operation, and
checks threshold-free that canary positives still score above negatives.

Pure Python: the certifier installs nothing beyond the contract's needs.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from statistics import NormalDist
from typing import Dict, Iterable, List, Optional, Sequence

from belay.contract import ConceptSummary

BOOTSTRAP_REPS = 2000


def statistic_of(summaries: Iterable[ConceptSummary], statistic: str = "mean") -> Dict[str, float]:
    """One request's score per concept."""
    return {s.concept: float(getattr(s, statistic)) for s in summaries}


def task_scores(requests: Sequence[Sequence[ConceptSummary]], statistic: str = "mean") -> Dict[str, float]:
    """A task's score per concept over all its requests.

    mean and frac_above are weighted by covered tokens, so the task's score is
    the signal over every token the task produced; peak is the highest peak.
    """
    num: Dict[str, float] = {}
    den: Dict[str, float] = {}
    peak: Dict[str, float] = {}
    for summaries in requests:
        for s in summaries:
            v = float(getattr(s, statistic))
            peak[s.concept] = max(peak.get(s.concept, 0.0), v)
            num[s.concept] = num.get(s.concept, 0.0) + v * s.covered_tokens
            den[s.concept] = den.get(s.concept, 0.0) + s.covered_tokens
    if statistic == "peak":
        return peak
    return {c: (num[c] / den[c] if den[c] else peak[c]) for c in num}


def auroc(pos: Sequence[float], neg: Sequence[float]) -> Optional[float]:
    """P(positive > negative), ties counted half (the Mann-Whitney U over m*n)."""
    if not pos or not neg:
        return None
    srt = sorted(neg)
    total = 0.0
    for p in pos:
        lo, hi = bisect_left(srt, p), bisect_right(srt, p)
        total += lo + 0.5 * (hi - lo)
    return total / (len(pos) * len(neg))


def bootstrap_ci(pos: Sequence[float], neg: Sequence[float], rng: random.Random,
                 reps: int = BOOTSTRAP_REPS, level: float = 0.95) -> Optional[List[float]]:
    """Percentile bootstrap CI for the AUROC, resampling positives and negatives separately."""
    if not pos or not neg:
        return None
    m, n = len(pos), len(neg)
    stats = sorted(auroc([pos[rng.randrange(m)] for _ in range(m)], [neg[rng.randrange(n)] for _ in range(n)])
                   for _ in range(reps))
    tail = (1 - level) / 2
    lo = stats[max(0, int(math.floor(tail * reps)))]
    hi = stats[min(reps - 1, int(math.ceil((1 - tail) * reps)) - 1)]
    return [round(lo, 4), round(hi, 4)]


def operating_point(pos: Sequence[float], neg: Sequence[float], target_fpr: float,
                    hard: Sequence[float] = ()) -> Optional[dict]:
    """The lowest score t, among the negatives' scores, with FPR (share of negatives above t) <= target.

    In operation a request is a hit when its score is strictly above t. Chosen
    from the certification run only, before any canary is scored.
    """
    if not neg:
        return None
    srt = sorted(neg)
    n = len(srt)
    threshold = srt[-1]
    for t in sorted(set(srt)):
        if (n - bisect_right(srt, t)) / n <= target_fpr:
            threshold = t
            break

    def above(xs):
        return round(sum(1 for x in xs if x > threshold) / len(xs), 4) if xs else None

    return {"threshold": threshold, "target_fpr": target_fpr, "fpr": above(neg), "fpr_hard": above(list(hard)),
            "recall": above(pos), "negatives": n, "positives": len(pos)}


def _hanley_mcneil_var(a: float, m: int, n: int) -> float:
    """Variance of the AUROC estimate at true AUROC a, m positives, n negatives (Hanley and McNeil 1982)."""
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    return (a * (1 - a) + (m - 1) * (q1 - a * a) + (n - 1) * (q2 - a * a)) / (m * n)


def separation_test(groups: Dict[str, dict], alpha: float, min_each: int = 5) -> dict:
    """Mann-Whitney test that canary positives still score above negatives, as certified.

    `groups` maps concept -> {"pos": [...], "neg": [...], "floor": auroc_min}.
    Concepts are pooled with weights m*n (a stratified Mann-Whitney). H0: the
    pooled AUROC is at least the pooled certified floor; the variance is the
    Hanley-McNeil variance at the floor. Exposed when z < the alpha quantile.
    """
    w_sum = auc_sum = floor_sum = var_sum = 0.0
    m_tot = n_tot = 0
    per = {}
    for concept, g in groups.items():
        m, n = len(g["pos"]), len(g["neg"])
        a = auroc(g["pos"], g["neg"])
        per[concept] = {"positives": m, "negatives": n, "auroc": None if a is None else round(a, 4),
                        "floor": g["floor"]}
        if a is None:
            continue
        w = m * n
        w_sum += w
        auc_sum += w * a
        floor_sum += w * g["floor"]
        var_sum += w * w * _hanley_mcneil_var(g["floor"], m, n)
        m_tot, n_tot = m_tot + m, n_tot + n
    out = {"positives": m_tot, "negatives": n_tot, "by_concept": per, "auroc": None, "floor": None,
           "z": None, "p": None, "alpha": alpha, "tested": False, "exposed": False}
    if not w_sum or m_tot < min_each or n_tot < min_each:
        return out
    a, floor = auc_sum / w_sum, floor_sum / w_sum
    se = math.sqrt(var_sum) / w_sum
    z = (a - floor) / se if se > 0 else (0.0 if a >= floor else -math.inf)
    p = NormalDist().cdf(z) if math.isfinite(z) else 0.0
    out.update(auroc=round(a, 4), floor=round(floor, 4), z=round(z, 3) if math.isfinite(z) else z,
               p=p, tested=True, exposed=p < alpha)
    return out
