# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.report_tables (PLAN_ROUND_8.md, I3)."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from census import figures as F
from census import report_tables as T
from census.correlation import correlation_matrix
from census.multitest import adjust_pvalues, batch_z_test
from census.results import ModelResult


def _model(method="replicate"):
    co = pd.DataFrame({"term": ["Intercept", "x1", "x2"], "estimate": [1.0, 0.5, -0.2],
                       "se_model": [0.1, 0.1, 0.1], "se_sampling": [0.05, 0.1, 0.1],
                       "se_total": [0.12, 0.15, 0.141], "ci_lower": [0.8, 0.25, -0.43],
                       "ci_upper": [1.2, 0.75, 0.03], "p_value": [0.0001, 0.004, 0.03]})
    return ModelResult("ols", "y ~ x1 + x2", 250, co, method, ["model", "sampling_replicate"])


def test_regression_table_markers_after_correction():
    t = T.regression_table({"M1": _model()}, correction="bonferroni", citation=T.data_citation([2023], ["B17001"]))
    d = t.data.set_index("term")
    exp = adjust_pvalues(np.array([0.004, 0.03]), "bonferroni")  # intercept excluded from family
    assert d.loc["x1", "p_adjusted"] == pytest.approx(exp[0])
    assert d.loc["x2", "p_adjusted"] == pytest.approx(exp[1])
    assert d.loc["x2", "marker"] == "*"  # raw p .03 would be **; adjusted .06 is *
    assert d.loc["x1", "marker"] == "***"
    disp = t.display
    assert list(disp["Term"]) == ["Intercept", "", "x1", "", "x2", "", "N"]
    assert disp["M1"].tolist()[2:4] == ["0.500***", "(0.150)"]
    assert disp["M1"].iloc[-1] == "250"
    tex = t.to_latex()
    assert r"\toprule" in tex and r"\midrule" in tex and r"\bottomrule" in tex
    assert "sampling\\_replicate" in tex and "2019-2023" in tex
    md = t.to_markdown()
    assert "| x1 | 0.500*** |" in md and "bonferroni-adjusted" in md
    csv = pd.read_csv(pd.io.common.StringIO(t.to_csv()))
    assert {"se_model", "se_sampling", "se_total", "ci_lower", "ci_upper", "p_adjusted"} <= set(csv.columns)


def test_regression_table_ci_and_two_models():
    t = T.regression_table({"Naive": _model("ols"), "Corrected": _model()}, uncertainty="ci",
                           correction=None)
    assert t.display.columns.tolist() == ["Term", "Naive", "Corrected"]
    assert t.display["Corrected"].iloc[3] == "[0.250, 0.750]"
    assert t.data.set_index(["model", "term"]).loc[("Naive", "x2"), "marker"] == "**"


def test_descriptive_table():
    df = pd.DataFrame({"v": [10.0, 20.0, 30.0, 40.0], "v_se": [1.0, 2.0, 15.0, 4.0]})
    t = T.descriptive_table(df, ["v"], labels={"v": "Poverty (%)"})
    r = t.data.iloc[0]
    assert r["n"] == 4 and r["mean"] == 25 and r["sd"] == pytest.approx(np.std([10, 20, 30, 40], ddof=1))
    assert r["share_cv_above_30"] == 0.25  # CVs 10, 10, 50, 10
    assert t.display["Variable"].iloc[0] == "Poverty (%)"


def test_comparison_table():
    df = pd.DataFrame({"NAME": ["X", "Y"], "e1": [10.0, 10.0], "m1": [1.0, 5.0],
                       "e2": [5.0, 9.0], "m2": [1.0, 5.0]})
    z = batch_z_test(df, "e1", "m1", "e2", "m2")
    t = T.comparison_table(z)
    assert t.data["ci_upper"].iloc[0] == pytest.approx(5 + 1.645 * z["se_difference"].iloc[0])
    assert t.display["Difference"].iloc[0].endswith("***") and t.display["Difference"].iloc[1] == "1.00"
    assert "across 2 tests" in t.to_markdown()


def test_correlation_table():
    rng = np.random.default_rng(1)
    a = rng.normal(size=40)
    df = pd.DataFrame({"a": a, "b": a + rng.normal(size=40), "c": rng.normal(size=40)})
    res = correlation_matrix(df, ["a", "b", "c"])
    t = T.correlation_table(res)
    assert t.display.iloc[1, 1].startswith(f"{res.matrix.loc['b', 'a']:.2f}")
    assert t.display.iloc[0, 2] == "" and len(t.data) == 3


def test_latex_escape():
    assert T.latex_escape("a_b & 50%") == r"a\_b \& 50\%"


def test_results_bundle(tmp_path):
    est = pd.DataFrame({"NAME": ["A", "B"], "est": [1.0, 2.0], "moe": [0.1, 0.2]})
    pre = F.estimates_with_intervals(est, "NAME", "est", moe_col="moe", out_dir=tmp_path / "scratch",
                                     formats=("svg",), name="pre")
    m = T.results_bundle(
        tmp_path / "bundle", tables={"reg": T.regression_table(_model())},
        figures={"est": (F.estimates_with_intervals,
                         dict(frame=est, label_col="NAME", estimate_col="est", moe_col="moe",
                              formats=("svg",))),
                 "copied": pre},
        methods="We fit OLS with replicate SEs.", results={"m": _model()},
        provenance={"manifest_hash": "abc"}, citation=T.data_citation([2023], ["B01001"]))
    root = tmp_path / "bundle"
    for rel in ("tables/reg.tex", "tables/reg.md", "tables/reg.csv", "figures/est.svg",
                "figures/est.csv", "figures/est.json", "figures/copied.svg", "figures/copied.caption.txt",
                "captions.md", "methods.md", "results/m.json", "bundle.json"):
        assert (root / rel).exists(), rel
    manifest = json.loads((root / "bundle.json").read_text())
    assert manifest["provenance"]["manifest_hash"] == "abc"
    assert manifest["files"]["tables/reg.md"] == hashlib.sha256((root / "tables/reg.md").read_bytes()).hexdigest()
    assert "B01001" in (root / "tables/reg.md").read_text()
    assert "Figure: est" in (root / "captions.md").read_text()
    assert m["figures"] == ["copied", "est"]
