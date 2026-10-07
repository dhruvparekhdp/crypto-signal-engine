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


def cluster_stats(r, clusters, n_boot: int = 2000, seed: int = 7) -> dict:
    """Mean R with significance that respects clustering (roadmap Q-1, plan O3 / R11).

    Trades opened in the same window across coins are one market bet (92-97% point the same way), so they are
    resampled together: a block bootstrap over clusters. Returns the mean, the naive t (trades independent), the
    cluster t (cluster sums as the unit), a 95% bootstrap interval and the one-sided bootstrap p of mean <= 0."""
    import numpy as np
    r = np.asarray(r, float)
    clusters = np.asarray(clusters)
    n = len(r)
    if n < 3:
        return {"n": n, "mean": float(r.mean()) if n else None}
    keys, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=r)
    counts = np.bincount(inv)
    k = len(keys)
    naive_t = r.mean() / (r.std(ddof=1) / math.sqrt(n)) if r.std(ddof=1) > 0 else float("nan")
    # cluster-robust t: mean of per-trade R with variance from cluster totals (CR0)
    resid = sums - counts * r.mean()
    se = math.sqrt((resid ** 2).sum()) / n
    cluster_t = r.mean() / se if se > 0 else float("nan")
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, k, size=(n_boot, k))
    boot = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {"n": n, "clusters": int(k), "mean": float(r.mean()), "naive_t": float(naive_t),
            "cluster_t": float(cluster_t), "ci_lo": float(lo), "ci_hi": float(hi),
            "p_boot": float(((boot <= 0).sum() + 1) / (n_boot + 1))}
