# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.correlation (PLAN_ROUND_8.md, I2)."""

import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from census.correlation import correlation_matrix, fisher_z_interval, reliability
from census.multitest import adjust_pvalues
from census.variance import REPLICATE_COLUMNS


def _frame(n=60, seed=1):
    rng = np.random.default_rng(seed)
    a = rng.normal(size=n)
    b = 0.6 * a + rng.normal(scale=0.8, size=n)
    c = rng.normal(size=n)
    df = pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(n)], "a": a, "b": b, "c": c})
    for v in "abc":
        df[f"{v}_se"] = 0.2
    return df


@pytest.mark.parametrize("method", ["pearson", "spearman"])
def test_values_match_scipy(method):
    df = _frame()
    df.loc[3, "b"] = np.nan  # pairwise-complete handling
    res = correlation_matrix(df, ["a", "b", "c"], method=method)
    fn = stats.pearsonr if method == "pearson" else stats.spearmanr
    ok = df["a"].notna() & df["b"].notna()
    r, p = fn(df.loc[ok, "a"], df.loc[ok, "b"])
    assert res.matrix.loc["a", "b"] == pytest.approx(r, abs=1e-12)
    assert res.matrix.loc["b", "a"] == pytest.approx(r, abs=1e-12)
    assert res.p_values.loc["a", "b"] == pytest.approx(p, rel=1e-10)
    assert res.n.loc["a", "b"] == 59 and res.n.loc["a", "c"] == 60
    assert res.matrix.loc["a", "a"] == 1.0
    r2, _ = fn(df["a"], df["c"])
    assert res.matrix.loc["a", "c"] == pytest.approx(r2, abs=1e-12)


def test_fisher_z_interval_hand_formula():
    df = _frame()
    res = correlation_matrix(df, ["a", "b"], confidence_level=0.95)
    r, n = res.matrix.loc["a", "b"], 60
    z = math.atanh(r)
    half = stats.norm.ppf(0.975) / math.sqrt(n - 3)
    assert res.ci_lower.loc["a", "b"] == pytest.approx(math.tanh(z - half), abs=1e-12)
    assert res.ci_upper.loc["a", "b"] == pytest.approx(math.tanh(z + half), abs=1e-12)
    lo, hi = fisher_z_interval(0.3, 30, 0.90, "spearman")
    h = stats.norm.ppf(0.95) * math.sqrt(1.06 / 27)
    assert (lo, hi) == pytest.approx((math.tanh(math.atanh(0.3) - h), math.tanh(math.atanh(0.3) + h)))


def test_p_adjusted_uses_round7_holm():
    res = correlation_matrix(_frame(), ["a", "b", "c"], correction="holm")
    pairs = [("a", "b"), ("a", "c"), ("b", "c")]
    raw = [res.p_values.loc[x, y] for x, y in pairs]
    exp = adjust_pvalues(np.array(raw), "holm")
    got = [res.p_adjusted.loc[x, y] for x, y in pairs]
    assert got == pytest.approx(list(exp))
    assert correlation_matrix(_frame(), ["a", "b"], correction=None).p_adjusted is None


def test_replicate_se_hand_formula():
    df = _frame(n=40)
    rng = np.random.default_rng(7)
    rows = []
    for v in ("a", "b"):
        reps = df[v].to_numpy()[:, None] + rng.normal(scale=0.2, size=(40, 80))
        rep_df = pd.DataFrame(reps, columns=REPLICATE_COLUMNS)
        rep_df.insert(0, "variable", v)
        rep_df.insert(0, "GEOID", df["GEOID"])
        rows.append(rep_df)
    replicates = pd.concat(rows).sample(frac=1, random_state=3)  # order must not matter
    res = correlation_matrix(df, ["a", "b"], replicates=replicates)
    r = stats.pearsonr(df["a"], df["b"])[0]
    ra = rows[0][REPLICATE_COLUMNS].to_numpy()
    rb = rows[1][REPLICATE_COLUMNS].to_numpy()
    rr = np.array([stats.pearsonr(ra[:, k], rb[:, k])[0] for k in range(80)])
    expected = math.sqrt(4 / 80 * np.sum((rr - r) ** 2))
    assert res.se_replicate.loc["a", "b"] == pytest.approx(expected, rel=1e-10)
    assert res.se_replicate.loc["a", "a"] == 0
    assert "sampling_replicate" in res.provenance["uncertainty_sources"]


def test_disattenuation_recovers_true_correlation():
    rng = np.random.default_rng(2024)
    n, rho = 20000, 0.6
    cov = [[1, rho], [rho, 1]]
    tx, ty = rng.multivariate_normal([0, 0], cov, size=n).T
    sx, sy = 0.75, 0.5  # reliabilities 1/(1+0.5625)=0.64 and 1/(1+0.25)=0.8
    df = pd.DataFrame({"GEOID": np.arange(n).astype(str),
                       "x": tx + rng.normal(scale=sx, size=n), "x_se": sx,
                       "y": ty + rng.normal(scale=sy, size=n), "y_se": sy})
    res = correlation_matrix(df, ["x", "y"], disattenuate=True)
    rel = res.provenance["reliability"]
    assert rel["x"] == pytest.approx(0.64, abs=0.02)
    assert rel["y"] == pytest.approx(0.80, abs=0.02)
    observed = res.matrix.loc["x", "y"]
    assert observed == pytest.approx(rho * math.sqrt(0.64 * 0.8), abs=0.02)  # attenuated
    assert res.disattenuated.loc["x", "y"] == pytest.approx(rho, abs=0.02)
    assert res.disattenuated.loc["x", "y"] == pytest.approx(
        observed / math.sqrt(rel["x"] * rel["y"]))
    assert any("alongside" in n for n in res.notes)


def test_reliability_formula_and_errors():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert reliability(x, [0.5] * 4) == pytest.approx(1 - 0.25 / np.var(x, ddof=1))
    with pytest.raises(ValueError):
        correlation_matrix(_frame().drop(columns="a_se"), ["a", "b"], disattenuate=True)
    with pytest.raises(ValueError):
        correlation_matrix(_frame(), ["a"])
    with pytest.raises(ValueError):
        correlation_matrix(_frame(), ["a", "b"], method="kendall")


def test_to_dict_is_json_safe():
    import json
    res = correlation_matrix(_frame(), ["a", "b"], disattenuate=True)
    json.dumps(res.to_dict())
