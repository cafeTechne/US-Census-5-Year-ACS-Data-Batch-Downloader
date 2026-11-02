# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content tests for census.figures (PLAN_ROUND_8.md, I1). No image baselines."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from census import figures as F
from census.correlation import correlation_matrix
from census.multitest import batch_z_test
from census.results import ModelResult, SmallAreaResult

SRC = "ACS 5-year 2019-2023, table B17001"


def _estimates():
    return pd.DataFrame({"NAME": ["A", "B", "C", "D"], "est": [30.0, 10.0, 20.0, 5.0],
                         "moe": [3.29, 1.645, 16.45, 0.329]})


def _errorbar_points(ax):
    """(x, y) of every errorbar marker line in the axes."""
    pts = []
    for c in ax.containers:
        line = c.lines[0]
        pts += list(zip(line.get_xdata(), line.get_ydata()))
    return sorted(pts, key=lambda p: p[1])


def _model(method, est, se):
    co = pd.DataFrame({"term": ["Intercept", "x"], "estimate": [1.0, est], "se_model": [0.1, se],
                       "se_sampling": [0.0, 0.0], "se_total": [0.1, se],
                       "ci_lower": [0.8, est - 1.645 * se], "ci_upper": [1.2, est + 1.645 * se],
                       "p_value": [0.001, 0.01]})
    return ModelResult("ols", "y ~ x", 100, co, method, ["model"], provenance={"id": method})


def test_estimates_with_intervals_content(tmp_path):
    out = F.estimates_with_intervals(_estimates(), "NAME", "est", moe_col="moe",
                                     out_dir=tmp_path, data_source=SRC, xlabel="Poverty rate (%)")
    v = out.values
    assert list(v["label"]) == ["D", "B", "C", "A"]  # sorted by estimate
    a = v.set_index("label").loc["A"]
    assert a["ci_lower"] == pytest.approx(30 - 3.29) and a["ci_upper"] == pytest.approx(30 + 3.29)
    assert bool(v.set_index("label").loc["C", "low_reliability"])  # CV = 50%
    ax = out.figure.axes[0]
    assert ax.get_xlabel() == "Poverty rate (%)"
    assert [t.get_text() for t in ax.get_yticklabels()] == ["D", "B", "C", "A"]
    pts = _errorbar_points(ax)
    assert [p[0] for p in pts] == [5.0, 10.0, 20.0, 30.0]
    # interval endpoints drawn: the errorbar segments span the CI
    segs = np.concatenate([c.lines[2][0].get_segments() for c in ax.containers])
    spans = sorted((round(s[0][0], 6), round(s[1][0], 6)) for s in segs)
    assert (round(30 - 3.29, 6), round(30 + 3.29, 6)) in spans
    assert "90% confidence intervals" in out.caption and SRC in out.caption
    assert "CV" in out.caption or "coefficients of variation" in out.caption
    for k in ("pdf", "svg", "eps", "png", "csv", "caption", "provenance"):
        assert Path(out.paths[k]).exists()
    csv = pd.read_csv(out.paths["csv"])
    assert list(csv["estimate"]) == [5.0, 10.0, 20.0, 30.0]
    side = json.loads(Path(out.paths["provenance"]).read_text())
    assert side["data_source"] == SRC and side["confidence_level"] == 0.9
    assert set(side["files"]) >= {"estimates.svg", "estimates.pdf", "estimates.csv"}


def test_byte_identical_regeneration(tmp_path):
    outs = [F.estimates_with_intervals(_estimates(), "NAME", "est", moe_col="moe",
                                       out_dir=tmp_path / str(i), result_id="r1") for i in (1, 2)]
    for fmt in ("svg", "pdf", "eps", "png", "csv", "provenance"):
        assert Path(outs[0].paths[fmt]).read_bytes() == Path(outs[1].paths[fmt]).read_bytes(), fmt
    svg = Path(outs[0].paths["svg"]).read_text()
    assert "<dc:date>" not in svg
    assert b"CreationDate" not in Path(outs[0].paths["pdf"]).read_bytes()


def test_journal_widths(tmp_path):
    one = F.estimates_with_intervals(_estimates(), "NAME", "est", moe_col="moe", out_dir=tmp_path,
                                     formats=("svg",), name="a")
    two = F.estimates_with_intervals(_estimates(), "NAME", "est", moe_col="moe", out_dir=tmp_path,
                                     formats=("svg",), name="b", width="two", height=3)
    assert one.figure.get_size_inches()[0] == pytest.approx(3.5)
    assert tuple(two.figure.get_size_inches()) == pytest.approx((7.0, 3.0))


def test_coefficient_plot_naive_vs_corrected(tmp_path):
    res = {"Naive": _model("ols_naive", 0.5, 0.1), "Corrected": _model("replicate", 0.8, 0.2)}
    out = F.coefficient_plot(res, out_dir=tmp_path, formats=("svg",), data_source=SRC)
    v = out.values
    assert set(v["term"]) == {"x"}  # intercept dropped
    corr = v[v["model"] == "Corrected"].iloc[0]
    assert corr["ci_lower"] == pytest.approx(0.8 - 0.329) and corr["ci_upper"] == pytest.approx(0.8 + 0.329)
    xs = sorted(p[0] for p in _errorbar_points(out.figure.axes[0]))
    assert xs == pytest.approx([0.5, 0.8])
    assert [t.get_text() for t in out.figure.axes[0].get_legend().get_texts()] == ["Naive", "Corrected"]
    assert "ols_naive" in out.caption and "replicate" in out.caption


def test_correlation_heatmap(tmp_path):
    rng = np.random.default_rng(0)
    a = rng.normal(size=50)
    df = pd.DataFrame({"a": a, "b": a + rng.normal(scale=0.3, size=50), "c": rng.normal(size=50)})
    res = correlation_matrix(df, ["a", "b", "c"])
    out = F.correlation_heatmap(res, out_dir=tmp_path, formats=("svg",), data_source=SRC)
    img = out.figure.axes[0].get_images()[0].get_array()
    np.testing.assert_allclose(np.asarray(img), res.matrix.to_numpy())
    texts = [t.get_text() for t in out.figure.axes[0].texts]
    assert f"{res.matrix.loc['a', 'b']:.2f}*" in texts
    row = out.values[(out.values["var1"] == "a") & (out.values["var2"] == "b")].iloc[0]
    assert row["ci_lower"] == pytest.approx(res.ci_lower.loc["a", "b"])
    assert "Fisher-z 90%" in out.caption and "(corrected)" in out.caption


def test_forest_plot_from_batch_z_test(tmp_path):
    df = pd.DataFrame({"NAME": ["X", "Y", "Z"], "e1": [10.0, 10.0, 10.0], "m1": [1.0, 5.0, 1.0],
                       "e2": [5.0, 9.0, 10.5], "m2": [1.0, 5.0, 1.0]})
    z = batch_z_test(df, "e1", "m1", "e2", "m2")
    out = F.forest_plot(z, out_dir=tmp_path, formats=("svg",), data_source=SRC)
    v = out.values.set_index("label")
    se = z.set_index("NAME")["se_difference"]
    assert v.loc["X", "ci_upper"] == pytest.approx(5 + 1.645 * se["X"])
    assert bool(v.loc["X", "significant_adjusted"]) and not bool(v.loc["Y", "significant_adjusted"])
    assert "holm correction across 3 tests" in out.caption


def test_shrinkage_plot(tmp_path):
    est = pd.DataFrame({"GEOID": ["01", "02", "03"], "direct": [10.0, 20.0, 30.0],
                        "direct_se": [2.0, 4.0, 1.0], "synthetic": [15.0, 18.0, 28.0],
                        "eblup": [12.0, 19.0, 29.8], "mse": [1.0, 2.0, 0.8],
                        "rmse": [1.0, 1.414, 0.894], "ci_lower": [10.4, 16.7, 28.3],
                        "ci_upper": [13.6, 21.3, 31.3], "gamma": [0.6, 0.3, 0.9]})
    res = SmallAreaResult(est, 4.0, pd.DataFrame(), "fay_herriot", "REML", "prasad_rao")
    out = F.shrinkage_plot(res, out_dir=tmp_path, formats=("svg",))
    v = out.values.set_index("label")
    assert v.loc["02", "shift"] == pytest.approx(-1.0)
    assert v.loc["01", "direct_ci_lower"] == pytest.approx(10 - 1.645 * 2)
    assert v.loc["03", "eblup_ci_upper"] == 31.3
    assert "REML" in out.caption and "prasad_rao" in out.caption


def test_trend_plot_flags_overlap(tmp_path):
    df = pd.DataFrame({"year": [2013, 2018, 2023], "est": [1.0, 2.0, 3.0], "se": [0.1, 0.1, 0.1]})
    out = F.trend_plot(df, "year", "est", se_col="se", trend_test={"slope": 0.2, "p_value": 0.01},
                       out_dir=tmp_path, formats=("svg",))
    assert "slope = 0.2" in out.caption and "overlap" not in out.caption
    line = out.figure.axes[0].containers[0].lines[0]
    assert list(line.get_xdata()) == [2013, 2018, 2023]
    df2 = pd.DataFrame({"year": [2021, 2022], "est": [1.0, 2.0], "se": [0.1, 0.1]})
    out2 = F.trend_plot(df2, "year", "est", se_col="se", out_dir=tmp_path, name="t2", formats=("svg",))
    assert "overlap" in out2.caption


def test_reliability_profile(tmp_path):
    df = pd.DataFrame({"level": ["county"] * 4 + ["tract"] * 4,
                       "cv": [5.0, 8.0, 10.0, 14.0, 20.0, 35.0, 40.0, 25.0]})
    out = F.reliability_profile_plot(df, "level", "cv", out_dir=tmp_path, formats=("svg",))
    v = out.values.set_index("level")
    assert v.loc["tract", "share_cv_above_30"] == 0.5 and v.loc["county", "median_cv"] == 9.0
    ax = out.figure.axes[0]
    assert [t.get_text() for t in ax.get_xticklabels()] == ["county", "tract"]
    assert sorted(l.get_ydata()[0] for l in ax.lines if l.get_label().startswith("CV =")) == [12, 30]


def test_bad_format(tmp_path):
    with pytest.raises(ValueError):
        F.estimates_with_intervals(_estimates(), "NAME", "est", moe_col="moe", out_dir=tmp_path,
                                   formats=("jpg",))
