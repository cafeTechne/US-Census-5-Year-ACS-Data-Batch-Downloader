# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the PLAN_ROUND_11 confirmed defects C1-C7.

Expected values are built by hand (or from invariances), never by calling the
production helper under test.
"""

import math
import os

import numpy as np
import pandas as pd
import pytest

from census import pums as P
from census import stats as S

REPS = [f"PWGTP{i}" for i in range(1, 81)]


def _pums(x, full, rep, **cols):
    """Frame with values ``x``, full weights ``full`` and a (n x 80) replicate matrix."""
    df = pd.DataFrame({"x": np.asarray(x, float), **cols})
    df["PWGTP"] = np.asarray(full, float)
    rep = np.asarray(rep, float)
    for r in range(80):
        df[REPS[r]] = rep[:, r]
    return df


def _hand_se(full, reps):
    return math.sqrt(4 / 80 * sum((r - full) ** 2 for r in reps))


# --------------------------------------------------------------------------- C1

def test_c1_all_replicates_undefined_is_not_zero_se():
    # Full mean 10; every replicate weight is 0 -> every replicate mean is 0/0.
    df = _pums([5.0, 15.0], [1, 1], np.zeros((2, 80)))
    t = P.tabulate(df, "mean", "x")
    assert t.estimate.iloc[0] == pytest.approx(10.0)
    assert np.isnan(t.se.iloc[0]) and np.isnan(t.moe.iloc[0]) and np.isnan(t.cv_percent.iloc[0])
    assert bool(t.flag_se_undefined.iloc[0])
    assert "se_undefined" in t.reliability_flags.iloc[0]


def test_c1_one_undefined_replicate_is_not_dropped():
    rep = np.ones((2, 80))
    rep[:, 0] = 0.0  # replicate 1 has a zero denominator
    t = P.tabulate(_pums([5.0, 15.0], [1, 1], rep), "mean", "x")
    assert np.isnan(t.se.iloc[0]) and bool(t.flag_se_undefined.iloc[0])


def test_c1_complete_and_grouped():
    # group a: complete replicates (rep weights shift the mean); group b: all undefined.
    rep = np.ones((4, 80))
    rep[0, :] = 3.0  # a: values 0 and 10 -> replicate mean 10*1/4 = 2.5
    rep[2:, :] = 0.0
    df = _pums([0.0, 10.0, 1.0, 3.0], [1, 1, 1, 1], rep, g=["a", "a", "b", "b"])
    t = P.tabulate(df, "mean", "x", by="g").set_index("g")
    assert t.loc["a", "estimate"] == pytest.approx(5.0)
    assert t.loc["a", "se"] == pytest.approx(_hand_se(5.0, [2.5] * 80))
    assert not bool(t.loc["a", "flag_se_undefined"])
    assert t.loc["b", "estimate"] == pytest.approx(2.0)
    assert np.isnan(t.loc["b", "se"]) and bool(t.loc["b", "flag_se_undefined"])


# --------------------------------------------------------------------------- C2

@pytest.mark.parametrize("x,w,expected", [
    ([0, 100, 200], [1, 0, 1], 100.0),                   # internal zero (audit example)
    ([0, 100, 150, 200], [1, 0, 0, 1], 100.0),            # repeated internal zeros
    ([-5, 0, 200], [0, 1, 1], 100.0),                     # leading zero
    ([0, 200, 300], [1, 1, 0], 100.0),                    # trailing zero
    ([0, 0, 50, 200], [1, 0, 0, 1], 100.0),               # ties with zero weight
])
def test_c2_zero_weight_invariance(x, w, expected):
    got = P.weighted_median(np.array(x, float), np.array(w, float))
    assert got[0] == pytest.approx(expected)


def test_c2_replicate_columns_and_all_zero():
    x = np.array([0.0, 100.0, 200.0])
    W = np.array([[1, 1, 0], [0, 1, 0], [1, 1, 0]], float)
    got = P.weighted_median(x, W)
    assert got[0] == pytest.approx(100.0)   # zero weight on 100 ignored
    assert got[1] == pytest.approx(100.0)   # ordinary odd case
    assert np.isnan(got[2])                 # all-zero total stays undefined


def test_c2_tabulate_median_ignores_zero_weight_record():
    df = _pums([0, 100, 200], [1, 0, 1], np.ones((3, 80)))
    assert P.tabulate(df, "median", "x").estimate.iloc[0] == pytest.approx(100.0)


# --------------------------------------------------------------------------- C5

def test_c5_missing_group_key_dropped():
    rep = np.tile(np.arange(1, 81, dtype=float), (4, 1))
    df = _pums([1, 2, 3, 4], [10, 20, 30, 40], rep, g=["a", None, "b", "a"])
    t = P.tabulate(df, "count", by="g").set_index("g")
    assert list(t.index) == ["a", "b"]
    assert t.loc["a", "estimate"] == pytest.approx(50.0) and t.loc["a", "n_unweighted"] == 2
    assert t.loc["b", "estimate"] == pytest.approx(30.0) and t.loc["b", "n_unweighted"] == 1
    assert t.loc["a", "se"] == pytest.approx(_hand_se(50.0, [2.0 * r for r in range(1, 81)]))


def test_c5_multiple_keys_all_missing_and_crosstab():
    rep = np.ones((4, 80))
    df = _pums([1, 2, 3, 4], [1, 2, 3, 4], rep, g=["a", "a", None, "b"], h=[1, None, 1, 2])
    t = P.tabulate(df, "mean", "x", by=["g", "h"])
    assert t[["g", "h"]].values.tolist() == [["a", 1.0], ["b", 2.0]]
    assert t.estimate.tolist() == pytest.approx([1.0, 4.0])
    assert t.n_unweighted.tolist() == [1, 1]
    empty = P.tabulate(df.assign(g=None), "count", by="g")
    assert len(empty) == 0
    ct = P.crosstab(df, "g", "h", normalize="row")
    assert ct.estimate.tolist() == pytest.approx([1.0, 1.0])
    assert P.crosstab(df.assign(g=None), "g", "h", normalize="row").empty


# --------------------------------------------------------------------------- C3

@pytest.fixture(scope="module")
def spatial_case():
    pytest.importorskip("libpysal")
    pytest.importorskip("esda")
    from census.simulation import lattice_weights, make_replicates, replicate_long, sar_field
    rng = np.random.default_rng(11)
    w = lattice_weights(6)
    ids = w.id_order
    y = 50 + sar_field(w.full()[0], 0.6, 10.0, rng)
    se = np.full(len(y), 3.0)
    good = replicate_long(ids, ["y"], y[:, None], make_replicates(y[:, None], se[:, None], rng)).assign(Year=2023)
    old = replicate_long(ids, ["y"], y[:, None], make_replicates(y[:, None], 5 * se[:, None], rng)).assign(Year=2018)
    df = pd.DataFrame({"GEOID": ids, "Year": 2023, "y": y})
    return w, df, good, old


def test_c3_release_selected_not_row_order(spatial_case):
    from census.spatial import morans_i
    w, df, good, old = spatial_case
    ref = morans_i(df, "y", w, permutations=0, replicates=good).se_replicate
    for mixed in (pd.concat([old, good]), pd.concat([good, old]), pd.concat([old, good]).sample(frac=1, random_state=1)):
        assert morans_i(df, "y", w, permutations=0, replicates=mixed).se_replicate == pytest.approx(ref, rel=1e-12)


def test_c3_ambiguous_replicates_rejected(spatial_case):
    from census.spatial import local_morans, morans_i, spatial_regression
    w, df, good, old = spatial_case
    no_year = df.drop(columns="Year")
    with pytest.raises(ValueError, match="several releases"):
        morans_i(no_year, "y", w, permutations=0, replicates=pd.concat([old, good]))
    dup = pd.concat([good, good.iloc[:1]])  # identical duplicate also refused
    with pytest.raises(ValueError, match="more than one replicate row"):
        morans_i(df, "y", w, permutations=0, replicates=dup)
    with pytest.raises(ValueError, match="more than one replicate row"):
        local_morans(df, "y", w, permutations=9, replicates=dup)
    with pytest.raises(ValueError, match="lack complete replicates"):
        morans_i(df, "y", w, permutations=0, replicates=good.iloc[1:])
    d2 = df.assign(x=np.arange(len(df), dtype=float))
    with pytest.raises(ValueError, match="more than one replicate row"):
        spatial_regression("y ~ x", d2, w, replicates=dup)
    # a valid single release without a Year column still works
    assert morans_i(no_year, "y", w, permutations=0, replicates=good.drop(columns="Year")).se_replicate > 0


# --------------------------------------------------------------------------- C4

@pytest.mark.parametrize("moe,se,lo,hi", [
    (-555555555, 0.0, 1000.0, 1000.0),          # controlled: no sampling error
    (-222222222, math.nan, math.nan, math.nan),  # MOE could not be computed
    (-333333333, math.nan, math.nan, math.nan),
    (-999999999, math.nan, math.nan, math.nan),
    (0, 0.0, 1000.0, 1000.0),
    (164.5, 100.0, 835.5, 1164.5),
    (-164.5, 100.0, 835.5, 1164.5),              # ordinary negative: |moe| compatibility kept
])
def test_c4_scalar_sentinel_parity(moe, se, lo, hi):
    res = S.calculate_cv(1000, moe)
    for got, want in ((res["standard_error"], se), (res["lower_90"], lo), (res["upper_90"], hi)):
        assert (math.isnan(got) and math.isnan(want)) or got == pytest.approx(want)
    if moe <= -100000000:  # Census sentinel: scalar agrees with the array helper
        arr = S.moe_to_se([moe])[0]
        assert (math.isnan(arr) and math.isnan(res["standard_error"])) or arr == res["standard_error"]
    if math.isnan(se):
        assert math.isnan(res["cv_percent"]) and res["reliability"] == ""
    if moe == -555555555:
        assert res["cv_percent"] == 0.0 and res["reliability"] == S.RELIABILITY_HIGH


def test_c4_sentinel_estimate_and_mcp():
    res = S.calculate_cv(-666666666, 100)
    assert math.isnan(res["cv_percent"]) and math.isnan(res["lower_90"]) and res["reliability"] == ""
    srv = pytest.importorskip("mcp_server")
    out = srv.calculate_cv(estimate=1000, moe=-555555555)
    assert out["standard_error"] == 0.0 and out["upper_90"] == 1000


# --------------------------------------------------------------------------- C6

def _did_panel(n=60, years=(2013, 2018), seed=0):
    rng = np.random.default_rng(seed)
    T = (np.arange(n) < n // 2).astype(int)
    a = rng.normal(50, 10, n)
    rows = [pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(n)], "Year": yr, "treated": T,
                          "y": a + k + 3.0 * T * (yr >= 2018) + rng.normal(0, 1, n), "y_se": 1.0})
            for k, yr in enumerate(years)]
    return pd.concat(rows, ignore_index=True)


def test_c6_post_treatment_reference_rejected():
    from census.did import difference_in_differences as did
    df = _did_panel()
    with pytest.raises(ValueError, match="pre-treatment"):
        did(df, "y", "treated", 2018, se="y_se", reference=2018)
    with pytest.raises(ValueError, match="not in data periods"):
        did(df, "y", "treated", 2018, se="y_se", reference=2003)


def test_c6_explicit_pre_reference_matches_default_and_hand_did():
    from census.did import difference_in_differences as did
    df = _did_panel()
    a = did(df, "y", "treated", 2018, se="y_se").coefficients.set_index("term").loc["did", "estimate"]
    b = did(df, "y", "treated", 2018, se="y_se", reference=2013).coefficients.set_index("term").loc["did", "estimate"]
    assert a == pytest.approx(b)
    # two periods, equal SEs: GLS DiD = difference of mean changes
    wide = df.pivot(index="GEOID", columns="Year", values="y")
    ch = wide[2018] - wide[2013]
    T = df.drop_duplicates("GEOID").set_index("GEOID")["treated"]
    assert a == pytest.approx(ch[T == 1].mean() - ch[T == 0].mean(), rel=1e-6)


# --------------------------------------------------------------------------- C7

@pytest.fixture(scope="module")
def model_files(tmp_path_factory):
    from census.simulation import make_replicates, replicate_long
    d = tmp_path_factory.mktemp("c7")
    rng = np.random.default_rng(5)
    n = 50
    x = rng.normal(10, 2, n)
    y = 1 + 0.5 * x + rng.normal(0, 1, n)
    ids = [f"36061{i:06d}" for i in range(n)]
    pd.DataFrame({"GEOID": ids, "y": y, "x": x, "x_se": 0.3}).to_csv(d / "af.csv", index=False)
    est = np.column_stack([y, x])
    se = np.column_stack([np.full(n, 0.8), np.full(n, 0.3)])
    replicate_long(ids, ["y", "x"], est, make_replicates(est, se, rng)).to_csv(d / "reps.csv", index=False)
    return str(d / "af.csv"), str(d / "reps.csv")


@pytest.mark.parametrize("target,col", [("population", "se_model"), ("these_areas", "se_sampling"),
                                        ("conservative", "se_total")])
def test_c7_reported_basis_matches_intervals(model_files, target, col):
    from census import workflows_inference as w
    from census.stats import Z_CRITICAL
    af, reps = model_files
    res = w.fit_model(af, "y ~ x", replicates_path=reps, target=target)
    assert res["success"], res
    assert res["interval_se_column"] == col
    assert col in res["next_step"] and (col == "se_total" or "se_total" not in res["next_step"])
    coef = pd.DataFrame(res["coefficients"]).set_index("term").loc["x"]
    assert coef["ci_upper"] - coef["estimate"] == pytest.approx(Z_CRITICAL[0.90] * coef[col], rel=1e-3)


def test_c7_simex_flagged_exploratory_and_recipe(model_files):
    from census import workflows_inference as w
    from census.recipes import get_recipe
    af, _ = model_files
    res = w.fit_model(af, "y ~ x", method="simex", error_covariates=["x"])
    assert res["success"], res
    assert res["interval_se_column"] == "se_total" and "exploratory" in res["next_step"]
    steps = get_recipe("regression_sampling_error")["steps"].lower()
    assert "report each coefficient with `se_total`" not in steps
    assert "interval_se_column" in steps
    simex_lines = [ln for ln in steps.splitlines() if "simex" in ln]
    assert simex_lines and all("confirmatory" in ln and "exploratory" in ln for ln in simex_lines)
