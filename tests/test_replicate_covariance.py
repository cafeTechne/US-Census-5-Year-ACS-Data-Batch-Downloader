# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.replicate_covariance (PLAN_ROUND_16.md workstream C).

Synthetic replicates with a known covariance: under the 4/80 SDR scaling the
deviation vectors d_r = X_r - X are drawn iid N(0, Sigma / 4), so
E[4/80 sum_r d_r d_r^T] = Sigma.
"""

import math

import numpy as np
import pytest
from scipy import sparse

from census import replicate_covariance as rc
from census.variance import REPLICATE_COUNT, replicate_variance


def _grid_adjacency(side):
    n = side * side
    rows, cols = [], []
    for r in range(side):
        for c in range(side):
            k = r * side + c
            if c + 1 < side:
                rows += [k, k + 1]
                cols += [k + 1, k]
            if r + 1 < side:
                rows += [k, k + side]
                cols += [k + side, k]
    return sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))


def _draw(sigma, rng, n_rep=REPLICATE_COUNT):
    """(n, R) deviations with E[4/R D D^T] = sigma, plus estimates and replicates."""
    chol = np.linalg.cholesky(sigma)
    d = (chol @ rng.standard_normal((sigma.shape[0], n_rep))) / 2.0
    est = rng.uniform(100, 200, size=sigma.shape[0])
    return d, est, est[:, None] + d


def _structured_sigma(side, rho1, rho2, sd=None):
    a = _grid_adjacency(side)
    orders = rc.contiguity_orders(a, max_order=2).toarray()
    r = np.eye(side * side) + rho1 * (orders == 1) + rho2 * (orders == 2)
    assert np.linalg.eigvalsh(r).min() > 0
    s = np.ones(side * side) if sd is None else sd
    return r * np.outer(s, s), orders


def test_covariance_diagonal_is_replicate_variance_and_rank_limited():
    rng = np.random.default_rng(1)
    est = rng.uniform(10, 50, 120)
    reps = est[:, None] + rng.normal(size=(120, REPLICATE_COUNT))
    cov = rc.replicate_covariance(est, reps)
    np.testing.assert_allclose(np.diag(cov), replicate_variance(est, reps))
    rank = rc.rank_report(reps - est[:, None])
    assert rank["rank"] == REPLICATE_COUNT == rank["max_rank"]
    assert np.linalg.matrix_rank(cov) == REPLICATE_COUNT
    corr = rc.replicate_correlation(est, reps)
    np.testing.assert_allclose(np.diag(corr), 1.0)


def test_covariance_is_unbiased_for_known_sigma():
    rng = np.random.default_rng(2)
    sigma = np.array([[4.0, 1.2, -0.8], [1.2, 1.0, 0.0], [-0.8, 0.0, 2.25]])
    acc = np.zeros_like(sigma)
    n_sim = 2000
    for _ in range(n_sim):
        _, est, reps = _draw(sigma, rng)
        acc += rc.replicate_covariance(est, reps)
    mean = acc / n_sim
    # SE of each mean entry ~ sqrt((s_ii s_jj + s_ij^2) / 80 / n_sim) <= 0.01
    np.testing.assert_allclose(mean, sigma, atol=0.05)


def test_zero_variance_area_gives_nan_correlation():
    est = np.array([1.0, 2.0, 3.0])
    reps = np.tile(est[:, None], (1, REPLICATE_COUNT)).astype(float)
    reps[0] += np.linspace(-1, 1, REPLICATE_COUNT)
    reps[1] += np.cos(np.arange(REPLICATE_COUNT))
    corr = rc.replicate_correlation(est, reps)
    assert np.isnan(corr[2]).all() and np.isnan(corr[:, 2]).all()
    assert np.isfinite(corr[0, 1])
    d = rc.replicate_deviations(est, reps)
    assert np.isnan(rc.pair_correlations(d, [0], [2]))[0]
    with pytest.raises(ValueError):
        rc.replicate_deviations(est, reps[:2])
    with pytest.raises(ValueError):
        rc.replicate_deviations([np.nan, 1, 2], reps)


def test_pair_correlations_match_matrix():
    rng = np.random.default_rng(3)
    sigma, _ = _structured_sigma(5, 0.2, 0.05)
    d, est, reps = _draw(sigma, rng)
    full = rc.replicate_correlation(est, reps)
    i, j = np.triu_indices(25, 1)
    np.testing.assert_allclose(rc.pair_correlations(d, i, j), full[i, j], atol=1e-12)


def test_null_noise_floor_matches_simulation():
    null = rc.null_correlation(80, alpha=0.05)
    assert null["sd"] == pytest.approx(1 / math.sqrt(80))
    assert null["critical_abs_r"] == pytest.approx(0.2194, abs=0.002)
    rng = np.random.default_rng(4)
    d = rng.normal(size=(4000, REPLICATE_COUNT)) * rng.uniform(0.5, 5, size=(4000, 1))
    i, j = np.arange(0, 4000, 2), np.arange(1, 4000, 2)
    r = rc.pair_correlations(d, i, j)
    assert np.std(r) == pytest.approx(null["sd"], rel=0.06)
    assert np.mean(np.abs(r) > null["critical_abs_r"]) == pytest.approx(0.05, abs=0.015)
    perm = rc.permutation_null(d, i, j, n_perm=10, seed=1)
    assert perm["sd"] == pytest.approx(null["sd"], rel=0.05)
    assert perm["critical_abs_r"] == pytest.approx(null["critical_abs_r"], abs=0.015)


def test_permutation_null_destroys_real_correlation():
    rng = np.random.default_rng(5)
    base = rng.normal(size=(1000, REPLICATE_COUNT))
    d = np.vstack([base, base + 0.3 * rng.normal(size=base.shape)])
    i, j = np.arange(1000), np.arange(1000, 2000)
    assert np.mean(rc.pair_correlations(d, i, j)) > 0.9
    perm = rc.permutation_null(d, i, j, n_perm=5)
    assert abs(perm["mean"]) < 0.01


def test_contiguity_orders_on_grid():
    a = _grid_adjacency(4)
    orders = rc.contiguity_orders(a, max_order=3).toarray()
    # node 0 (corner): 1 and 4 are order 1; 2, 5, 8 order 2; 3, 6, 9, 12 order 3
    assert orders[0, 1] == orders[0, 4] == 1
    assert orders[0, 2] == orders[0, 5] == orders[0, 8] == 2
    assert {orders[0, k] for k in (3, 6, 9, 12)} == {3}
    assert orders[0, 15] == 0 and orders[0, 0] == 0
    assert (orders == orders.T).all()
    np.testing.assert_array_equal(rc.pair_orders(rc.contiguity_orders(a, 3), [0, 0, 0], [1, 5, 15]), [1, 2, 0])


def test_class_summaries_recover_known_structure_and_se_is_calibrated():
    sigma, orders = _structured_sigma(12, 0.25, 0.08, sd=np.linspace(1, 20, 144))
    i, j = np.nonzero(np.triu(orders > 0))
    labels = orders[i, j]
    groups = (np.arange(144) // 12) // 3  # four blocks of three grid rows
    means, ses = {1: [], 2: []}, {1: [], 2: []}
    rng = np.random.default_rng(6)
    for rep in range(40):
        d, _, _ = _draw(sigma, rng)
        tab = rc.summarize_pair_correlations(d, i, j, labels, groups=groups, n_boot=60, seed=rep)
        for k in (1, 2):
            row = tab[tab["class"] == k].iloc[0]
            means[k].append(row["mean"])
            ses[k].append(row["se_boot"])
    for k, truth in ((1, 0.25), (2, 0.08)):
        assert np.mean(means[k]) == pytest.approx(truth, abs=0.01)
        # the replicate bootstrap SE matches the Monte Carlo spread of the class mean
        assert np.mean(ses[k]) == pytest.approx(np.std(means[k], ddof=1), rel=0.35)
    assert {"se_jackknife", "excess_sd", "share_beyond_critical", "ci_low", "ci_high"} <= set(tab.columns)


def test_split_half_reliability_separates_noise_from_signal():
    rng = np.random.default_rng(7)
    n_pairs = 600
    # pairs with true correlation +0.7 or -0.7 (signal) vs 0 (noise only)
    rho = np.where(np.arange(n_pairs) % 2 == 0, 0.7, -0.7)
    x = rng.normal(size=(n_pairs, REPLICATE_COUNT))
    y = rho[:, None] * x + np.sqrt(1 - rho[:, None] ** 2) * rng.normal(size=x.shape)
    d = np.vstack([x, y])
    i, j = np.arange(n_pairs), np.arange(n_pairs, 2 * n_pairs)
    signal = rc.split_half_agreement(d, i, j, split="odd_even").iloc[0]
    assert signal["pair_reliability_full"] > 0.9
    noise = rc.split_half_agreement(rng.normal(size=d.shape), i, j, split="random", seed=1).iloc[0]
    assert abs(noise["pair_reliability_half"]) < 0.12
    with pytest.raises(ValueError):
        rc.split_half_agreement(d, i, j, split="bad")


def test_aggregation_ratio_detects_controlled_totals():
    rng = np.random.default_rng(8)
    m, k = 10, 30
    groups = np.repeat(np.arange(k), m)
    free = rng.normal(size=(m * k, REPLICATE_COUNT))
    frame, summary = rc.aggregation_variance_ratio(free, groups, n_boot=50)
    assert summary["pooled_ratio"] == pytest.approx(1.0, abs=0.1)
    assert abs(summary["pooled_implied_mean_corr"]) < 0.02
    # group totals fixed (deviations sum to zero within each group): ratio 0, mean corr -1/(m-1)
    ctrl = free - np.repeat(free.reshape(k, m, -1).mean(axis=1), m, axis=0)
    frame, summary = rc.aggregation_variance_ratio(ctrl, groups, n_boot=50)
    assert summary["pooled_ratio"] == pytest.approx(0.0, abs=1e-12)
    assert summary["pooled_implied_mean_corr"] == pytest.approx(-1 / (m - 1), abs=0.01)
    assert len(frame) == k and frame["ratio"].max() < 1e-12


def test_ledoit_wolf_matches_direct_formula():
    rng = np.random.default_rng(9)
    d = rng.normal(size=(15, REPLICATE_COUNT)) * rng.uniform(1, 3, size=(15, 1))
    out = rc.ledoit_wolf_intensity(d)
    x = 2.0 * d.T
    z = x / np.sqrt(np.mean(x * x, axis=0))
    t, n = z.shape
    s = z.T @ z / t
    delta2 = np.sum((s - np.eye(n)) ** 2) / n
    beta2 = sum(np.sum((np.outer(zr, zr) - s) ** 2) for zr in z) / t ** 2 / n
    assert out["intensity"] == pytest.approx(min(1.0, min(beta2, delta2) / delta2))
    assert out["intensity"] > 0.6  # independent areas: shrink nearly all the way
    sigma, _ = _structured_sigma(5, 0.35, 0.1)
    dd, _, _ = _draw(sigma, rng)
    assert rc.ledoit_wolf_intensity(dd)["intensity"] < out["intensity"]


def test_lowrank_loglik_matches_dense():
    rng = np.random.default_rng(10)
    z_tr = rng.normal(size=(40, 30))
    z_te = rng.normal(size=(40, 30))
    a = 0.7
    r = a * np.eye(30) + (1 - a) * z_tr.T @ z_tr / 40
    dense, _ = rc._loglik_dense(r, z_te)
    assert rc._loglik_lowrank(a, z_tr, z_te) == pytest.approx(dense, rel=1e-9)


def test_holdout_comparison_prefers_true_structure():
    rng = np.random.default_rng(11)
    sigma, orders = _structured_sigma(10, 0.3, 0.1)
    d, _, _ = _draw(sigma, rng)
    cls = np.where(orders == 1, 0, np.where(orders == 2, 1, -1))
    np.fill_diagonal(cls, -1)
    out = rc.holdout_model_comparison(d, {"order12": cls})
    assert out["gain_nats_per_replicate"]["order12"] > 0.5
    rho1 = np.mean([p[0] for p in out["fitted_rho"]["order12"]])
    assert rho1 == pytest.approx(0.3, abs=0.05)
    # no structure: the structured model gains nothing material
    d0 = rng.normal(size=(100, REPLICATE_COUNT))
    out0 = rc.holdout_model_comparison(d0, {"order12": cls})
    assert out0["gain_nats_per_replicate"]["order12"] < 0.2
    assert out0["gain_nats_per_replicate"]["order12"] < out["gain_nats_per_replicate"]["order12"]


def _constrained(n, k, rng):
    """iid deviations made orthogonal to k random replicate-space directions (a design constraint)."""
    v = np.linalg.qr(rng.normal(size=(REPLICATE_COUNT, k)))[0].T  # (k, R) orthonormal
    g = rng.normal(size=(n, REPLICATE_COUNT))
    return g - (g @ v.T) @ v, v


def test_replicate_spectrum_detects_design_null_directions():
    rng = np.random.default_rng(12)
    d, v = _constrained(1500, 3, rng)
    spec = rc.replicate_spectrum(d)
    assert spec["n_null"] == 3 and spec["effective_replicates"] == 77
    cosines = np.linalg.svd(spec["null_directions"] @ v.T, compute_uv=False)
    assert cosines.min() > 0.999
    assert rc.replicate_spectrum(rng.normal(size=(1500, REPLICATE_COUNT)))["n_null"] == 0
    small = rc.replicate_spectrum(d[:100])
    assert small["n_null"] == 0 and "not detected" in small["note"]
    # fewer effective replicates widen the null spread of a pairwise r to ~1/sqrt(R - k)
    r = rc.pair_correlations(d, np.arange(0, 1500, 2), np.arange(1, 1500, 2))
    assert np.std(r) == pytest.approx(1 / math.sqrt(77), rel=0.08)


def test_holdout_removes_cross_half_dependence_from_design_directions():
    rng = np.random.default_rng(13)
    d, v = _constrained(1200, 3, rng)
    leaky = rc.holdout_model_comparison(d, {}, remove_directions=None)
    fixed = rc.holdout_model_comparison(d, {})
    explicit = rc.holdout_model_comparison(d, {}, remove_directions=v)
    # independent areas: only the cross-half leak lets Ledoit-Wolf beat independence
    assert leaky["gain_nats_per_replicate"]["ledoit_wolf"] > 0
    assert fixed["gain_nats_per_replicate"]["ledoit_wolf"] < 0
    assert explicit["gain_nats_per_replicate"]["ledoit_wolf"] < 0
    assert fixed["directions_removed"] == 4 and explicit["directions_removed"] == 3
