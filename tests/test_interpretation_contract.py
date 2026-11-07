# SPDX-License-Identifier: AGPL-3.0-or-later
"""PLAN_ROUND_12 B1/B2: every consumer of a ModelResult names and uses the same
interval basis, confidence level and distribution.

Expected interval widths are built by hand (z or t quantile times the named
SE column), never by calling census.results.interval_description.
"""

import contextlib
import copy
import io
import json
import os

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

from census import figures as F
from census import report_tables as T
from census.manifest import load_manifest, methods_section
from census.results import ModelResult

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "manifests", "income_population_tracts.json")
TARGETS = [("population", "se_model"), ("these_areas", "se_sampling"), ("conservative", "se_total")]
Z90 = float(sps.norm.ppf(0.95))


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    from census.simulation import make_replicates, replicate_long
    d = tmp_path_factory.mktemp("b2")
    rng = np.random.default_rng(12)
    n = 60
    x = rng.normal(10, 2, n)
    y = 1 + 0.5 * x + rng.normal(0, 1, n)
    ids = [f"36061{i:06d}" for i in range(n)]
    pd.DataFrame({"GEOID": ids, "y": y, "x": x, "x_se": 0.3}).to_csv(d / "af.csv", index=False)
    est = np.column_stack([y, x])
    se = np.column_stack([np.full(n, 0.9), np.full(n, 0.4)])
    replicate_long(ids, ["y", "x"], est, make_replicates(est, se, rng)).to_csv(d / "reps.csv", index=False)
    return str(d / "af.csv"), str(d / "reps.csv"), d


def _x(coefs):
    return pd.DataFrame(coefs).set_index("term").loc["x"]


@pytest.mark.parametrize("target,col", TARGETS)
def test_consumers_agree_on_basis(files, target, col, tmp_path):
    from census import models, workflows_inference as w
    af, reps, _ = files
    res = models.fit_replicate("y ~ x", pd.read_csv(af, dtype={"GEOID": str}),
                               pd.read_csv(reps, dtype={"GEOID": str}), target=target)
    c = res.coefficients.set_index("term").loc["x"]
    se = {k: float(c[k]) for k in ("se_model", "se_sampling", "se_total")}
    assert len({round(v, 6) for v in se.values()}) == 3, se  # deliberately unequal
    half = Z90 * se[col]
    assert c["ci_upper"] - c["estimate"] == pytest.approx(half, rel=1e-6)

    d = res.to_dict()  # serialized result
    assert d["interval_se_column"] == col and d["interval"]["basis_column"] == col
    assert d["interval"]["distribution"] == "normal" and d["interval"]["level"] == pytest.approx(0.90)
    assert "not a causal effect" in d["interval"]["estimand"]
    assert col in d["summary"]

    tab = T.regression_table(res, correction=None)  # SE shown in the table
    row = tab.data.set_index("term").loc["x"]
    assert row["se_basis"] == col and row["se_reported"] == pytest.approx(se[col])
    assert row["distribution"] == "normal"
    assert f"({row['se_reported']:.3f})" in tab.display["Model"].tolist()
    assert any(col in n and "normal" in n for n in tab.notes)
    ci = T.regression_table(res, uncertainty="ci", correction=None)
    assert any(col in n for n in ci.notes) and any("90%" in n for n in ci.notes)
    assert col in ci.to_markdown() and col in ci.to_latex().replace(r"\_", "_")
    assert col in tab.to_csv()

    fig = F.coefficient_plot(res, out_dir=tmp_path, formats=("svg",))  # figure endpoints + caption
    v = fig.values.set_index("term").loc["x"]
    assert v["ci_upper"] - v["estimate"] == pytest.approx(half, rel=1e-6)
    assert v["estimate"] - v["ci_lower"] == pytest.approx(half, rel=1e-6)
    assert v["se_basis"] == col and v["se_reported"] == pytest.approx(se[col])
    assert col in fig.caption and "normal" in fig.caption
    others = {"se_model", "se_sampling"} - {col}
    assert not any(f"from {o}" in fig.caption for o in others)

    m = load_manifest(EXAMPLE)  # methods text
    m = copy.deepcopy(m)
    m["analyses"][0].setdefault("args", {})["target"] = target
    text = methods_section(m)
    line = next(ln for ln in text.splitlines() if ln.startswith("- **m1**"))
    assert f"({col})" in line and "normal" in line and "not a causal effect" in line

    wf = w.fit_model(af, "y ~ x", replicates_path=reps, target=target)  # shared workflow
    assert wf["success"], wf
    assert wf["interval_se_column"] == col and col in wf["next_step"] and "normal" in wf["next_step"]
    assert col in wf["summary"]
    wx = _x(wf["coefficients"])
    assert wx[col] == pytest.approx(se[col]) and wx["ci_upper"] == pytest.approx(c["ci_upper"])


def test_corrected_method_uses_se_total(files):
    from census import workflows_inference as w
    af, _, _ = files
    wf = w.fit_model(af, "y ~ x", method="regression_calibration", error_covariates=["x"])
    assert wf["success"], wf
    x = _x(wf["coefficients"])
    assert wf["interval_se_column"] == "se_total" == wf["interval"]["basis_column"]
    assert x["ci_upper"] - x["estimate"] == pytest.approx(Z90 * x["se_total"], rel=1e-6)
    assert "measurement error" in wf["interval"]["estimand"]


def test_cli_and_mcp_adapters_match_workflow(files):
    from census import workflows_inference as w
    from census.__main__ import main
    srv = pytest.importorskip("mcp_server")
    af, reps, _ = files
    kwargs = {"input_path": af, "formula": "y ~ x", "replicates_path": reps, "target": "these_areas"}
    direct = w.fit_model(**kwargs)
    via_mcp = srv.fit_model(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["tool", "fit_model", json.dumps(kwargs), "--json"])
    via_cli = json.loads(buf.getvalue())
    for out in (via_mcp, via_cli):
        assert out["success"]
        assert out["interval_se_column"] == direct["interval_se_column"] == "se_sampling"
        assert out["interval"]["basis_column"] == "se_sampling"
        assert out["next_step"] == direct["next_step"]
        assert out.get("warnings") == direct.get("warnings")
        assert out["notes"] == direct["notes"]
        a, b = _x(out["coefficients"]), _x(direct["coefficients"])
        for k in ("estimate", "se_sampling", "ci_lower", "ci_upper", "p_value"):
            assert a[k] == pytest.approx(b[k])


def test_multilevel_t_interval_reported_with_t(tmp_path):
    from census import workflows_microdata as wm
    from census.multilevel import fit_multilevel
    rng = np.random.default_rng(3)
    g = np.repeat(np.arange(8), 6)
    x = rng.normal(0, 1, len(g))
    y = 1 + 0.4 * x + rng.normal(0, 0.5, 8)[g] + rng.normal(0, 1, len(g))
    frame = pd.DataFrame({"y": y, "x": x, "county": g.astype(str)})
    res = fit_multilevel("y ~ x", frame, "county")
    c = res.coefficients.set_index("term").loc["x"]
    assert "df" in res.coefficients and np.isfinite(c["df"])
    tq = float(sps.t.ppf(0.95, c["df"]))
    assert c["ci_upper"] - c["estimate"] == pytest.approx(tq * c["se_model"], rel=1e-6)
    assert tq > Z90 * 1.01  # small df: a z reconstruction would be visibly too narrow

    d = res.to_dict()["interval"]
    assert d["distribution"] == "t" and d["df"]["x"] == pytest.approx(c["df"])
    tab = T.regression_table(res, correction=None)
    assert tab.data.set_index("term").loc["x", "distribution"] == "t"
    assert tab.data.set_index("term").loc["x", "df"] == pytest.approx(c["df"])
    assert any(" t " in n for n in tab.notes)
    fig = F.coefficient_plot(res, out_dir=tmp_path, formats=("svg",))
    v = fig.values.set_index("term").loc["x"]
    assert v["ci_upper"] - v["estimate"] == pytest.approx(tq * c["se_model"], rel=1e-6)
    assert v["distribution"] == "t" and "t (per-term df)" in fig.caption

    path = tmp_path / "ml.csv"
    frame.to_csv(path, index=False)
    out = wm.multilevel_model(str(path), "y ~ x", "county")
    assert out["success"], out
    assert out["interval_se_column"] == "se_model" and "t with each coefficient's df" in out["next_step"]
    assert "uncertainty_sources" not in out["next_step"]


def test_undefined_uncertainty_stays_undefined(tmp_path):
    coefs = pd.DataFrame({"term": ["Intercept", "x", "z"], "estimate": [1.0, 0.5, 0.2],
                          "se_model": [0.1, np.nan, 0.05], "se_sampling": [0.2, np.nan, 0.08],
                          "se_total": [0.22, np.nan, 0.09], "ci_lower": [1 - Z90 * 0.1, np.nan, 0.2 - Z90 * 0.05],
                          "ci_upper": [1 + Z90 * 0.1, np.nan, 0.2 + Z90 * 0.05],
                          "p_value": [0.0, np.nan, 0.0001]})
    res = ModelResult("ols", "y ~ x + z", 30, coefs, "replicate_refit", ["model", "sampling_replicate"],
                      diagnostics={"interval_target": "population"})
    info = res.to_dict()["interval"]
    assert info["undefined_terms"] == ["x"]
    tab = T.regression_table(res, correction=None)
    row = tab.data.set_index("term").loc["x"]
    assert not row["interval_defined"] and row["marker"] == ""
    disp = tab.display["Model"].tolist()
    assert "(SE undefined)" in disp and "(0.000)" not in disp
    assert "[undefined]" in T.regression_table(res, uncertainty="ci", correction=None).display["Model"].tolist()
    fig = F.coefficient_plot(res, out_dir=tmp_path, formats=("svg",))
    v = fig.values.set_index("term").loc["x"]
    assert np.isnan(v["ci_lower"]) and np.isnan(v["ci_upper"]) and not v["interval_defined"]
    assert "undefined" in fig.caption and "x" in fig.caption.split("undefined", 1)[1]
    from census.workflows_inference import interval_next_step
    assert "undefined for ['x']" in interval_next_step(info)
