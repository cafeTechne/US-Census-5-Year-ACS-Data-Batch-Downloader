# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sampling covariance between areas' ACS estimates from the 80 VRE replicates.

PLAN_ROUND_16.md workstream C. The successive-difference replication (SDR)
variance of one estimate, Var(X) = 4/80 sum_r (X_r - X)^2 (census.variance),
extends to the covariance of two estimates published in the same release:

    Cov(X, Y) = 4/80 * sum_{r=1..80} (X_r - X)(Y_r - Y)

(ACS Variance Replicate Estimate Tables documentation, formula (1) applied to
a sum; census.replicate.VRE_DOCUMENTATION). The 4/80 factor cancels in a
correlation, which is the uncentered cosine of the two replicate-deviation
vectors d_i = X_{i,r} - X_i.

What 80 replicates can and cannot say
-------------------------------------
* The replicate covariance matrix of n areas is 4/80 D D^T with D the (n, 80)
  deviation matrix, so its rank is at most 80. For hundreds or thousands of
  tracts it is a rank-limited, finite-sample estimate, not the covariance.
* A single pairwise correlation from 80 replicates is very noisy: if one
  area's deviation vector is spherically symmetric in R^80 and independent of
  the other, (1 + r) / 2 ~ Beta(79/2, 79/2), so SD(r) = 1/sqrt(80) = 0.112
  and |r| > 0.22 happens 5% of the time (:func:`null_correlation`). Pairwise
  correlations are therefore never reported as individually established; only
  structured summaries over many pairs are (:func:`summarize_pair_correlations`).
* Uncertainty of those summaries comes from (a) the 80 replicates, by a
  bootstrap over replicate columns (an approximation: SDR replicate
  deviations are close to, but not exactly, iid across replicates), and (b)
  the areas, by a delete-one-group jackknife (e.g. counties) that drops every
  pair touching the group. :func:`split_half_agreement` measures how well the
  per-pair correlations reproduce between two disjoint halves of the
  replicates (reliability); :func:`holdout_model_comparison` scores structured
  and shrinkage covariance models on replicates held out of the fit.

Everything here is pure numpy/scipy; deviations are passed in, so the module
does not depend on census.replicate or on the network.
"""

import math
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import sparse
from scipy import stats as sps

from census.variance import REPLICATE_COUNT, REPLICATE_FACTOR

__all__ = [
    "replicate_deviations",
    "replicate_covariance",
    "replicate_correlation",
    "pair_correlations",
    "null_correlation",
    "permutation_null",
    "contiguity_orders",
    "pair_orders",
    "summarize_pair_correlations",
    "split_half_agreement",
    "aggregation_variance_ratio",
    "ledoit_wolf_intensity",
    "holdout_model_comparison",
    "rank_report",
]


# ---------------------------------------------------------------------------
# Deviations, covariance, correlation
# ---------------------------------------------------------------------------


def replicate_deviations(estimates: Any, replicates: Any) -> np.ndarray:
    """(n, R) replicate deviations X_{i,r} - X_i.

    ``estimates`` has shape (n,), ``replicates`` (n, R). Non-finite values are
    refused (drop such areas first).
    """
    est = np.asarray(estimates, dtype=float).reshape(-1)
    reps = np.asarray(replicates, dtype=float)
    if reps.ndim != 2 or reps.shape[0] != est.size:
        raise ValueError(f"replicates must have shape (n, R) with n = {est.size}, got {reps.shape}")
    if not (np.isfinite(est).all() and np.isfinite(reps).all()):
        raise ValueError("estimates and replicates must be finite; drop incomplete areas first")
    return reps - est[:, None]


def _factor(n_replicates: int) -> float:
    # 4/80 for the ACS; for a subset of R replicates the same per-replicate
    # scaling (4/R) keeps the estimator unbiased under the SDR design.
    return 4.0 / n_replicates


def replicate_covariance(estimates: Any, replicates: Any) -> np.ndarray:
    """(n, n) replicate covariance 4/R * D D^T (R = 80 for ACS VRE files).

    Its diagonal is census.variance.replicate_variance. Rank <= R.
    """
    d = replicate_deviations(estimates, replicates)
    return _factor(d.shape[1]) * (d @ d.T)


def replicate_correlation(estimates: Any, replicates: Any) -> np.ndarray:
    """(n, n) replicate correlation; rows/columns of zero-variance areas are NaN."""
    d = replicate_deviations(estimates, replicates)
    norm = np.sqrt(np.sum(d * d, axis=1))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = d / norm[:, None]
        c = z @ z.T
    c[norm == 0, :] = np.nan
    c[:, norm == 0] = np.nan
    return np.clip(c, -1.0, 1.0)


def pair_correlations(deviations: np.ndarray, i: Any, j: Any, weights: Optional[np.ndarray] = None) -> np.ndarray:
    """Replicate correlation of the pairs (i[k], j[k]), without the n x n matrix.

    ``weights`` (length R) reweights replicate columns (bootstrap counts).
    Pairs with a zero-variance member give NaN.
    """
    d = np.asarray(deviations, dtype=float)
    i = np.asarray(i, dtype=np.int64)
    j = np.asarray(j, dtype=np.int64)
    w = np.ones(d.shape[1]) if weights is None else np.asarray(weights, dtype=float)
    num = np.einsum("kr,kr,r->k", d[i], d[j], w)
    ss = (d * d) @ w
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.clip(num / np.sqrt(ss[i] * ss[j]), -1.0, 1.0)


# ---------------------------------------------------------------------------
# Noise floor
# ---------------------------------------------------------------------------


def null_correlation(n_replicates: int = REPLICATE_COUNT, alpha: float = 0.05) -> Dict[str, float]:
    """Null distribution of one pairwise replicate correlation.

    If one deviation vector is spherically symmetric in R^R and independent of
    the other, the (uncentered) correlation is a coordinate of a uniform point
    on the unit sphere: (1 + r)/2 ~ Beta((R-1)/2, (R-1)/2), mean 0, SD
    1/sqrt(R). Returns the SD and the two-sided ``alpha`` critical |r|. Real
    SDR deviations of small areas can have fewer effective dimensions, so
    check this against :func:`permutation_null` and against pairs known to be
    independent (e.g. areas in different states).
    """
    a = (n_replicates - 1) / 2.0
    crit = 2.0 * float(sps.beta.ppf(1.0 - alpha / 2.0, a, a)) - 1.0
    return {"n_replicates": int(n_replicates), "sd": 1.0 / math.sqrt(n_replicates), "alpha": float(alpha),
            "critical_abs_r": crit}


def permutation_null(deviations: np.ndarray, i: Any, j: Any, n_perm: int = 20, seed: int = 0,
                     alpha: float = 0.05) -> Dict[str, float]:
    """Empirical null for pairwise correlations: permute replicate indices of j.

    Each draw applies one random derangement (a permutation without fixed
    points, so no replicate stays paired with itself) of the R replicate
    columns to the second member of every pair, which destroys any real
    covariance but keeps each area's own deviation distribution. Returns mean, SD and the two-sided
    ``alpha`` quantile of |r| over all pairs and draws.
    """
    d = np.asarray(deviations, dtype=float)
    i = np.asarray(i, dtype=np.int64)
    j = np.asarray(j, dtype=np.int64)
    rng = np.random.default_rng(seed)
    out = []
    norm = np.sqrt(np.sum(d * d, axis=1))
    ok = (norm[i] > 0) & (norm[j] > 0)
    i, j = i[ok], j[ok]
    zi = d[i] / norm[i, None]
    zj = d[j] / norm[j, None]
    for _ in range(int(n_perm)):
        p = rng.permutation(d.shape[1])
        while np.any(p == np.arange(p.size)):
            p = rng.permutation(d.shape[1])
        out.append(np.sum(zi * zj[:, p], axis=1))
    r = np.concatenate(out) if out else np.array([])
    return {"n_pairs": int(i.size), "n_perm": int(n_perm), "mean": float(np.mean(r)) if r.size else math.nan,
            "sd": float(np.std(r)) if r.size else math.nan,
            "critical_abs_r": float(np.quantile(np.abs(r), 1.0 - alpha)) if r.size else math.nan}


# ---------------------------------------------------------------------------
# Pair construction
# ---------------------------------------------------------------------------


def _adjacency(neighbors: Any, n: Optional[int] = None) -> sparse.csr_matrix:
    if sparse.issparse(neighbors):
        a = sparse.csr_matrix(neighbors, dtype=bool)
    elif hasattr(neighbors, "sparse") and hasattr(neighbors, "id_order"):  # libpysal W
        a = sparse.csr_matrix(neighbors.sparse, dtype=bool)
    else:
        a = sparse.csr_matrix(np.asarray(neighbors), dtype=bool)
    if a.shape[0] != a.shape[1] or (n is not None and a.shape[0] != n):
        raise ValueError(f"adjacency must be square n x n, got {a.shape}")
    a = ((a + a.T) > 0).tolil()
    a.setdiag(False)
    a = a.tocsr()
    a.eliminate_zeros()
    return a


def contiguity_orders(neighbors: Any, max_order: int = 3) -> sparse.csr_matrix:
    """Sparse matrix of graph distance (contiguity order) 1..max_order between areas.

    ``neighbors``: a libpysal W, a sparse or dense adjacency matrix. Entry
    (i, j) is k when j is a k-th order neighbour of i (shortest path of k
    contiguity steps); pairs further apart (or disconnected) are absent.
    """
    a = _adjacency(neighbors)
    n = a.shape[0]
    a = a.astype(np.int32)
    reach = sparse.identity(n, dtype=np.int32, format="csr")
    order = sparse.csr_matrix((n, n), dtype=np.int16)
    for k in range(1, int(max_order) + 1):
        nxt = reach + reach @ a
        nxt.data = (nxt.data > 0).astype(np.int32)
        new = (nxt - reach).tocsr()
        new.data = (new.data > 0).astype(np.int32)
        new.eliminate_zeros()
        order = order + new.astype(np.int16) * k
        reach = nxt.tocsr()
    order.eliminate_zeros()
    return order.tocsr()


def pair_orders(order_matrix: sparse.spmatrix, i: Any, j: Any) -> np.ndarray:
    """Contiguity order of each pair (0 = beyond the matrix's max order)."""
    m = sparse.csr_matrix(order_matrix)
    return np.asarray(m[np.asarray(i), np.asarray(j)]).reshape(-1).astype(int)


# ---------------------------------------------------------------------------
# Structured summaries and their reliability
# ---------------------------------------------------------------------------


def _bootstrap_weights(rng: np.random.Generator, n_replicates: int) -> np.ndarray:
    return rng.multinomial(n_replicates, np.full(n_replicates, 1.0 / n_replicates)).astype(float)


def _group_jackknife(r: np.ndarray, gi: np.ndarray, gj: np.ndarray) -> float:
    groups = np.unique(np.concatenate([gi, gj]))
    if groups.size < 3:
        return math.nan
    means = []
    for g in groups:
        keep = (gi != g) & (gj != g)
        if keep.any():
            means.append(float(np.mean(r[keep])))
    if len(means) < 3:
        return math.nan
    m = np.asarray(means)
    k = m.size
    return float(math.sqrt((k - 1) / k * np.sum((m - m.mean()) ** 2)))


def summarize_pair_correlations(
    deviations: np.ndarray,
    i: Any,
    j: Any,
    labels: Any,
    *,
    groups: Optional[Any] = None,
    n_boot: int = 200,
    seed: int = 0,
    alpha: float = 0.05,
) -> pd.DataFrame:
    """Mean/median replicate correlation per pair class, with its uncertainty.

    ``labels[k]`` is the class of pair (i[k], j[k]) (e.g. contiguity order,
    within/across county). ``groups`` (length n, e.g. county of each area)
    enables a delete-one-group jackknife SE of the class mean. Columns:

    * ``mean``, ``median``, ``sd`` of the pairwise correlations;
    * ``se_boot`` / ``se_boot_median``: bootstrap over the R replicate columns
      (``n_boot`` draws; replicate-sampling uncertainty);
    * ``se_jackknife``: delete-one-group jackknife (area heterogeneity plus
      replicate noise), NaN without ``groups``;
    * ``se`` = the larger of the two, ``ci_low``/``ci_high`` = mean -/+ z se;
    * ``null_sd`` = 1/sqrt(R) and ``excess_sd`` = sqrt(max(0, sd^2 - null_sd^2)),
      the spread of the pairs' correlations beyond pure replicate noise;
    * ``share_beyond_critical``: share of pairs with |r| above the null
      two-sided ``alpha`` critical value (``alpha`` expected under the null).
    """
    d = np.asarray(deviations, dtype=float)
    i = np.asarray(i, dtype=np.int64)
    j = np.asarray(j, dtype=np.int64)
    labels = np.asarray(labels)
    if not (i.size == j.size == labels.size):
        raise ValueError("i, j and labels must have the same length")
    n_rep = d.shape[1]
    null = null_correlation(n_rep, alpha)
    z = float(sps.norm.ppf(1.0 - alpha / 2.0))
    g = None if groups is None else np.asarray(groups)
    rng = np.random.default_rng(seed)
    boot_w = [_bootstrap_weights(rng, n_rep) for _ in range(int(n_boot))]
    rows = []
    for lab in pd.unique(labels):
        sel = labels == lab
        ii, jj = i[sel], j[sel]
        r = pair_correlations(d, ii, jj)
        ok = np.isfinite(r)
        ii, jj, r = ii[ok], jj[ok], r[ok]
        if not r.size:
            continue
        bm, bmed = [], []
        if boot_w:
            prod = d[ii] * d[jj]
            sq = d * d
            for w in boot_w:
                ss = sq @ w
                with np.errstate(divide="ignore", invalid="ignore"):
                    rb = (prod @ w) / np.sqrt(ss[ii] * ss[jj])
                rb = rb[np.isfinite(rb)]
                bm.append(float(np.mean(rb)))
                bmed.append(float(np.median(rb)))
        se_boot = float(np.std(bm, ddof=1)) if len(bm) > 1 else math.nan
        se_boot_med = float(np.std(bmed, ddof=1)) if len(bmed) > 1 else math.nan
        se_jk = _group_jackknife(r, g[ii], g[jj]) if g is not None else math.nan
        se = float(np.nanmax([se_boot, se_jk])) if np.isfinite([se_boot, se_jk]).any() else math.nan
        mean = float(np.mean(r))
        sd = float(np.std(r, ddof=1)) if r.size > 1 else math.nan
        rows.append({
            "class": lab, "n_pairs": int(r.size), "n_areas": int(np.unique(np.concatenate([ii, jj])).size),
            "mean": mean, "median": float(np.median(r)), "sd": sd,
            "se_boot": se_boot, "se_boot_median": se_boot_med, "se_jackknife": se_jk, "se": se,
            "ci_low": mean - z * se, "ci_high": mean + z * se,
            "null_sd": null["sd"],
            "excess_sd": float(math.sqrt(max(0.0, sd ** 2 - null["sd"] ** 2))) if np.isfinite(sd) else math.nan,
            "share_beyond_critical": float(np.mean(np.abs(r) > null["critical_abs_r"])),
        })
    return pd.DataFrame(rows)


def split_half_agreement(
    deviations: np.ndarray,
    i: Any,
    j: Any,
    labels: Optional[Any] = None,
    *,
    split: str = "random",
    seed: int = 0,
) -> pd.DataFrame:
    """Agreement of pairwise correlations between two disjoint halves of the replicates.

    ``split``: ``random`` (seeded), ``first_last`` (1-40 vs 41-80) or
    ``odd_even``. Per class: the two halves' class means, and the Pearson
    correlation across pairs of the half-A and half-B pairwise correlations
    (``pair_reliability_half``) with its Spearman-Brown step-up to all R
    replicates (``pair_reliability_full``). A reliability near 0 means the
    individual pairwise correlations are mostly replicate noise even if the
    class mean is well determined.
    """
    d = np.asarray(deviations, dtype=float)
    n_rep = d.shape[1]
    if split == "random":
        perm = np.random.default_rng(seed).permutation(n_rep)
        a, b = np.sort(perm[: n_rep // 2]), np.sort(perm[n_rep // 2:])
    elif split == "first_last":
        a, b = np.arange(n_rep // 2), np.arange(n_rep // 2, n_rep)
    elif split == "odd_even":
        a, b = np.arange(0, n_rep, 2), np.arange(1, n_rep, 2)
    else:
        raise ValueError("split must be 'random', 'first_last' or 'odd_even'")
    i = np.asarray(i, dtype=np.int64)
    j = np.asarray(j, dtype=np.int64)
    labels = np.zeros(i.size, dtype=int) if labels is None else np.asarray(labels)
    ra = pair_correlations(d[:, a], i, j)
    rb = pair_correlations(d[:, b], i, j)
    rows = []
    for lab in pd.unique(labels):
        sel = (labels == lab) & np.isfinite(ra) & np.isfinite(rb)
        x, y = ra[sel], rb[sel]
        rel = float(np.corrcoef(x, y)[0, 1]) if x.size > 2 and x.std() > 0 and y.std() > 0 else math.nan
        full = 2 * rel / (1 + rel) if np.isfinite(rel) and rel > -1 else math.nan
        rows.append({"class": lab, "split": split, "n_pairs": int(x.size),
                     "mean_half_a": float(np.mean(x)) if x.size else math.nan,
                     "mean_half_b": float(np.mean(y)) if y.size else math.nan,
                     "pair_reliability_half": rel, "pair_reliability_full": full})
    return pd.DataFrame(rows)


def aggregation_variance_ratio(deviations: np.ndarray, groups: Any, *, n_boot: int = 200,
                               seed: int = 0) -> Tuple[pd.DataFrame, Dict[str, float]]:
    """Var(sum of areas in a group) / sum of the areas' variances, per group.

    For counts that add up (tracts in a county) the ratio is
    1 + sum_{i != j} Cov_ij / sum_i Var_i: below 1 means the areas' sampling
    errors are negatively correlated on balance (e.g. weighting controls that
    fix the group total), above 1 positively. ``implied_mean_corr`` is
    sum_{i != j} Cov_ij / sum_{i != j} SD_i SD_j. This summary pools all pairs
    of a group in one number, so it is far less noisy than any pairwise
    correlation. Returns the per-group frame and a pooled summary (ratio of
    summed variances) with a replicate-bootstrap SE.
    """
    d = np.asarray(deviations, dtype=float)
    g = np.asarray(groups)
    n_rep = d.shape[1]
    f = _factor(n_rep)
    codes, uniq = pd.factorize(g)
    k = uniq.size
    gsum = np.zeros((k, n_rep))
    np.add.at(gsum, codes, d)
    var_i = f * np.sum(d * d, axis=1)
    sd_i = np.sqrt(var_i)
    var_sum = f * np.sum(gsum * gsum, axis=1)
    sum_var = np.bincount(codes, weights=var_i, minlength=k)
    sum_sd = np.bincount(codes, weights=sd_i, minlength=k)
    sum_sd2 = np.bincount(codes, weights=sd_i ** 2, minlength=k)
    cross_sd = sum_sd ** 2 - sum_sd2
    n_areas = np.bincount(codes, minlength=k)
    with np.errstate(divide="ignore", invalid="ignore"):
        frame = pd.DataFrame({
            "group": uniq, "n_areas": n_areas, "var_of_sum": var_sum, "sum_of_var": sum_var,
            "ratio": var_sum / sum_var, "implied_mean_corr": (var_sum - sum_var) / cross_sd,
        })
    multi = n_areas > 1

    def pooled(w: np.ndarray) -> Tuple[float, float]:
        vs = np.sum((gsum * gsum) @ w * multi)
        sv = np.sum(((d * d) @ w) * multi[codes])
        ss = np.sqrt((d * d) @ w)
        ssum = np.bincount(codes, weights=ss, minlength=k)
        ssq = np.bincount(codes, weights=ss ** 2, minlength=k)
        cs = np.sum((ssum ** 2 - ssq) * multi)
        return float(vs / sv), float((vs - sv) / cs)

    ratio, corr = pooled(np.ones(n_rep))
    rng = np.random.default_rng(seed)
    boots = np.array([pooled(_bootstrap_weights(rng, n_rep)) for _ in range(int(n_boot))]).reshape(-1, 2)
    summary = {
        "n_groups": int(multi.sum()), "n_areas": int(n_areas[multi].sum()),
        "pooled_ratio": ratio, "pooled_ratio_se_boot": float(np.std(boots[:, 0], ddof=1)) if len(boots) > 1 else math.nan,
        "pooled_implied_mean_corr": corr,
        "pooled_implied_mean_corr_se_boot": float(np.std(boots[:, 1], ddof=1)) if len(boots) > 1 else math.nan,
        "median_group_ratio": float(np.median(frame["ratio"][multi])) if multi.any() else math.nan,
    }
    return frame, summary


# ---------------------------------------------------------------------------
# Regularized and structured covariance models
# ---------------------------------------------------------------------------


def _standardized(d: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Observations x_r = 2 d_r (E x x^T = Sigma under 4/R scaling), standardized.

    Returns (T, n) standardized observations and the per-area SDs.
    """
    n_rep = d.shape[1]
    x = math.sqrt(_factor(n_rep) * n_rep) * d.T  # sqrt(4) * d for any R
    sd = np.sqrt(np.mean(x * x, axis=0))
    if np.any(sd == 0):
        raise ValueError("zero-variance area(s); drop them before fitting covariance models")
    return x / sd, sd


def ledoit_wolf_intensity(deviations: np.ndarray) -> Dict[str, float]:
    """Ledoit-Wolf (2004) shrinkage of the replicate correlation toward the identity.

    Known-mean (deviations are centred on the full-sample estimate) version on
    standardized observations z_r (T = R observations of n areas): sample
    correlation S = Z^T Z / T, target I, intensity
    min(1, beta^2 / delta^2) with delta^2 = ||S - I||_F^2 / n and
    beta^2 = sum_r ||z_r z_r^T - S||_F^2 / (T^2 n). Computed through the
    T x T Gram matrix, so n can be large. Intensity near 1 means the data
    cannot support the unrestricted off-diagonal structure.
    """
    z, _ = _standardized(np.asarray(deviations, dtype=float))
    t, n = z.shape
    gram = z @ z.T  # (T, T)
    s_norm2 = float(np.sum(gram ** 2)) / t ** 2  # ||S||_F^2
    delta2 = (s_norm2 - 2.0 * float(np.trace(gram)) / t + n) / n  # ||S - I||^2 / n (diag(S) = 1)
    zz = np.diag(gram)  # ||z_r||^2
    quad = np.sum(gram ** 2, axis=1) / t  # z_r^T S z_r
    beta2 = float(np.sum(zz ** 2 - 2.0 * quad + s_norm2)) / (t ** 2 * n)
    beta2 = min(beta2, delta2)
    intensity = 1.0 if delta2 <= 0 else beta2 / delta2
    return {"intensity": float(intensity), "delta2": float(delta2), "beta2": float(beta2), "n": int(n), "T": int(t)}


def _loglik_dense(r_mat: np.ndarray, z_test: np.ndarray) -> Tuple[float, bool]:
    """Mean over test observations of -0.5 (logdet R + z^T R^-1 z); projects to PD if needed."""
    projected = False
    try:
        chol = np.linalg.cholesky(r_mat)
    except np.linalg.LinAlgError:
        vals, vecs = np.linalg.eigh(r_mat)
        vals = np.clip(vals, 1e-3, None)
        r_mat = (vecs * vals) @ vecs.T
        s = np.sqrt(np.diag(r_mat))
        r_mat = r_mat / np.outer(s, s)
        chol = np.linalg.cholesky(r_mat)
        projected = True
    logdet = 2.0 * float(np.sum(np.log(np.diag(chol))))
    sol = np.linalg.solve(chol, z_test.T)  # L^-1 z
    quad = np.sum(sol * sol, axis=0)
    return float(np.mean(-0.5 * (logdet + quad))), projected


def _loglik_lowrank(a: float, z_train: np.ndarray, z_test: np.ndarray) -> float:
    """Test log-likelihood for R = a I + (1 - a) Z^T Z / T via Woodbury."""
    t, n = z_train.shape
    b = 1.0 - a
    a = max(a, 1e-9)
    if b <= 0:
        return float(np.mean(-0.5 * np.sum(z_test * z_test, axis=1)))
    m = np.eye(t) * (a * t / b) + z_train @ z_train.T
    sign, logdet_m = np.linalg.slogdet(m)
    logdet = n * math.log(a) + logdet_m - t * math.log(a * t / b)
    u = z_train @ z_test.T  # (T, m)
    quad = (np.sum(z_test * z_test, axis=1) - np.sum(u * np.linalg.solve(m, u), axis=0)) / a
    return float(np.mean(-0.5 * (logdet + quad)))


def holdout_model_comparison(
    deviations: np.ndarray,
    models: Mapping[str, np.ndarray],
    *,
    split: str = "random",
    seed: int = 0,
) -> Dict[str, Any]:
    """Score covariance models on replicates held out of the fit.

    Replicates are split in two halves (``split`` as in
    :func:`split_half_agreement`); each model is fitted on one half and scored
    on the other (both directions, averaged) by the Gaussian log-likelihood of
    the held-out standardized replicate observations, relative to the
    independence (diagonal) model, in nats per replicate. Per-area SDs come
    from the training half for every model.

    ``models`` maps a name to an (n, n) integer class matrix: off-diagonal
    entries >= 0 share one correlation parameter per class (estimated as the
    training-half mean correlation of the class, the least-squares fit of the
    structure); -1 fixes the correlation at 0; the diagonal is ignored.
    A Ledoit-Wolf shrinkage model (identity target) is always included.
    A fitted structure that is not positive definite is projected (eigenvalues
    clipped at 1e-3) and flagged.
    """
    d = np.asarray(deviations, dtype=float)
    n, n_rep = d.shape
    if split == "random":
        perm = np.random.default_rng(seed).permutation(n_rep)
        halves = (np.sort(perm[: n_rep // 2]), np.sort(perm[n_rep // 2:]))
    elif split == "first_last":
        halves = (np.arange(n_rep // 2), np.arange(n_rep // 2, n_rep))
    elif split == "odd_even":
        halves = (np.arange(0, n_rep, 2), np.arange(1, n_rep, 2))
    else:
        raise ValueError("split must be 'random', 'first_last' or 'odd_even'")
    scores: Dict[str, list] = {name: [] for name in list(models) + ["ledoit_wolf"]}
    params: Dict[str, list] = {name: [] for name in models}
    flags: Dict[str, bool] = {name: False for name in models}
    lw_int = []
    base = []
    for tr, te in (halves, halves[::-1]):
        z_tr, sd = _standardized(d[:, tr])
        x_te = math.sqrt(4.0) * d[:, te].T
        z_te = x_te / sd
        diag = float(np.mean(-0.5 * np.sum(z_te * z_te, axis=1)))
        base.append(diag)
        c_tr = (z_tr.T @ z_tr) / z_tr.shape[0]
        for name, cls in models.items():
            cls = np.asarray(cls)
            if cls.shape != (n, n):
                raise ValueError(f"class matrix for {name!r} must be {n} x {n}")
            off = ~np.eye(n, dtype=bool)
            codes = cls[off]
            vals = c_tr[off]
            use = codes >= 0
            k = int(codes[use].max()) + 1 if use.any() else 0
            rho = (np.bincount(codes[use], weights=vals[use], minlength=k)
                   / np.maximum(np.bincount(codes[use], minlength=k), 1)) if k else np.zeros(0)
            r_mat = np.zeros((n, n))
            r_mat[off] = np.where(use, rho[np.clip(codes, 0, None)] if k else 0.0, 0.0)
            np.fill_diagonal(r_mat, 1.0)
            ll, projected = _loglik_dense(r_mat, z_te)
            scores[name].append(ll - diag)
            params[name].append(rho.tolist())
            flags[name] = flags[name] or projected
        lw = ledoit_wolf_intensity(d[:, tr])
        lw_int.append(lw["intensity"])
        scores["ledoit_wolf"].append(_loglik_lowrank(lw["intensity"], z_tr, z_te) - diag)
    return {
        "n_areas": int(n), "n_replicates": int(n_rep), "split": split,
        "gain_nats_per_replicate": {k: float(np.mean(v)) for k, v in scores.items()},
        "gain_by_direction": {k: [float(x) for x in v] for k, v in scores.items()},
        "fitted_rho": params, "projected_to_pd": flags,
        "ledoit_wolf_intensity": [float(x) for x in lw_int],
    }


def rank_report(deviations: np.ndarray) -> Dict[str, Any]:
    """How much an unrestricted covariance could be estimated from these replicates."""
    d = np.asarray(deviations, dtype=float)
    n, n_rep = d.shape
    sv = np.linalg.svd(d, compute_uv=False)
    tol = sv.max() * max(n, n_rep) * np.finfo(float).eps if sv.size else 0.0
    rank = int(np.sum(sv > tol))
    energy = np.cumsum(sv ** 2) / np.sum(sv ** 2) if sv.size else np.array([])
    return {
        "n_areas": int(n), "n_replicates": int(n_rep), "rank": rank,
        "max_rank": int(min(n, n_rep)),
        "unrestricted_correlations": int(n * (n - 1) // 2),
        "replicate_values": int(n * n_rep),
        "share_zero_eigenvalues": float(1.0 - rank / n) if n else math.nan,
        "top5_share": float(energy[min(4, energy.size - 1)]) if energy.size else math.nan,
    }
