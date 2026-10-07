"""
Honest significance for permutation (null) tests: docs/INTEGRATION_PLAN.md Q3 / task 2.2.

Three rules, each fixing a way the old numbers flattered us:

* p = (beaten + 1) / (trials + 1). The real result counts as one of the draws, so a p-value is never 0 (with 200
  random trials the smallest honest answer is 1/201, not "0.000").
* Bonferroni: when N configurations were tried, one passes only if p <= alpha / N. N comes from the trial ledger
  (analysis/lab/ledger.py), not from a hard-coded guess. Conservative for correlated configs; accepted.
* Enough draws: with N tried, a p as small as alpha / N needs at least 20 * N / alpha random trials to be
  resolvable at all. Fewer than that and the verdict says so instead of passing or failing.
"""
from __future__ import annotations

import math


def perm_p(beaten: int, trials: int) -> float:
    """Permutation p-value counting the real result as one of the draws. 1.0 when nothing was drawn."""
    if trials <= 0:
        return 1.0
    return (beaten + 1) / (trials + 1)


def family_alpha(alpha: float, n_tried: int) -> float:
    return alpha / max(1, n_tried)


def min_trials(n_tried: int, alpha: float = 0.05) -> int:
    """Random trials needed before p can reach alpha / N."""
    return math.ceil(20 * max(1, n_tried) / alpha)


def judge(p: float, n_tried: int = 1, trials: int | None = None, alpha: float = 0.05) -> str:
    """Plain-English verdict. 'beats random' only past the Bonferroni line with enough draws to see it."""
    if p >= 0.5:
        return "no better than random"
    if p <= family_alpha(alpha, n_tried):
        if trials is not None and trials < min_trials(n_tried, alpha):
            return "too few trials"
        return "beats random"
    return "inconclusive"
