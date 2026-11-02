# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fay-Herriot tests: published sae `milk` reference, analytic special cases, guardrails."""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from census.results import SMALL_AREA_COLUMNS, ModelResult, SmallAreaResult
from census.sae import analytic_mse, fay_herriot, fh_regression

FIX = Path(__file__).parent / "fixtures" / "sae"
TERMS = {"Intercept": "(Intercept)", "C(MajorArea)[T.2]": "as.factor(MajorArea)2",
         "C(MajorArea)[T.3]": "as.factor(MajorArea)3", "C(MajorArea)[T.4]": "as.factor(MajorArea)4"}


@pytest.fixture(scope="module")
def milk():
    d = pd.read_csv(FIX / "milk.csv")
    d["GEOID"] = d["SmallArea"].astype(str)
    return d


@pytest.fixture(scope="module")
def ref():
    return json.loads((FIX / "milk_reference.json").read_text())


def test_milk_reml_reproduces_published_sae(milk, ref):
    pub = ref["REML"]["sae_eblupFH"]
    res = fay_herriot("yi ~ C(MajorArea)", milk, "SD")
    assert isinstance(res, SmallAreaResult)
    # sae stops Fisher scoring at 1e-4; metafor/S-Plus/SAS agree to ~3e-7.
    assert res.sigma2_u == pytest.approx(pub["refvar"], abs=5e-7)
    assert res.sigma2_u == pytest.approx(ref["REML"]["metafor_rma_tau2"], abs=5e-7)
    assert round(res.sigma2_u, 5) == ref["REML"]["sas_proc_mixed_covparm"]
    coef = res.coefficients.set_index("term")
    for ours, theirs in TERMS.items():
        assert coef.loc[ours, "estimate"] == pytest.approx(pub["estcoef"][theirs]["beta"], abs=2e-6)
        assert coef.loc[ours, "se_total"] == pytest.approx(pub["estcoef"][theirs]["std.error"], abs=2e-6)
        assert coef.loc[ours, "p_value"] == pytest.approx(pub["estcoef"][theirs]["pvalue"], rel=1e-4)
    assert res.provenance["loglike"] == pytest.approx(pub["goodness"]["loglike"], abs=5e-5)
    assert res.provenance["aic"] == pytest.approx(pub["goodness"]["AIC"], abs=1e-4)
    assert res.provenance["bic"] == pytest.approx(pub["goodness"]["BIC"], abs=1e-4)


def test_milk_ml_reproduces_published(milk, ref):
    res = fay_herriot("yi ~ C(MajorArea)", milk, "SD", method="ML")
    assert res.sigma2_u == pytest.approx(ref["ML"]["nlme_lme_variance"], abs=2e-7)
    assert res.sigma2_u == pytest.approx(ref["ML"]["metafor_rma_tau2"], abs=2e-7)


def test_output_columns_and_eblup_identity(milk):
    res = fay_herriot("yi ~ C(MajorArea)", milk, "SD")
    e = res.estimates
    assert list(e.columns) == SMALL_AREA_COLUMNS
    assert np.allclose(e["gamma"], res.sigma2_u / (res.sigma2_u + e["direct_se"] ** 2))
    assert np.allclose(e["eblup"], e["gamma"] * e["direct"] + (1 - e["gamma"]) * e["synthetic"])
    assert np.allclose(e["ci_upper"] - e["eblup"], 1.6448536 * e["rmse"], atol=1e-6)
    # EBLUP MSE below the direct variance in every area (paper's Figure 1 claim).
    assert (e["mse"] < e["direct_se"] ** 2).all()
    json.dumps(res.to_dict())


@pytest.mark.parametrize("method", ["REML", "ML", "FH"])
def test_balanced_closed_forms(method):
    rng = np.random.default_rng(1)
    m, D = 30, 0.5
    y = rng.normal(10, 1.5, m)
    df = pd.DataFrame({"GEOID": [str(i) for i in range(m)], "y": y, "se": math.sqrt(D)})
    res = fay_herriot("y ~ 1", df, "se", method=method)
    s2 = np.var(y, ddof=1)
    expect = max(0.0, (s2 * (m - 1) / m if method == "ML" else s2) - D)
    assert res.sigma2_u == pytest.approx(expect, rel=1e-7)
    A = res.sigma2_u
    if method == "REML":
        g1, g2, g3 = A * D / (A + D), (D / (A + D)) ** 2 * (A + D) / m, 2 * D ** 2 / (m * (A + D))
        assert np.allclose(res.estimates["mse"], g1 + g2 + 2 * g3, rtol=1e-10)
    assert np.allclose(res.estimates["synthetic"], y.mean())


def test_sigma2_zero_guardrail():
    df = pd.DataFrame({"GEOID": list("abcdefgh"), "x": np.arange(8.0)})
    df["y"] = 2 + 0.5 * df["x"] + np.array([0.01, -0.01] * 4)
    df["se"] = 1.0
    res = fay_herriot("y ~ x", df, "se")
    assert res.sigma2_u == 0.0
    assert np.allclose(res.estimates["eblup"], res.estimates["synthetic"])
    assert any("sigma2_u estimated at 0" in n for n in res.notes)


def test_undefined_sampling_variance_refused(milk):
    bad = milk.copy()
    bad.loc[3, "SD"] = np.nan
    with pytest.raises(ValueError, match="sampling variance"):
        fay_herriot("yi ~ C(MajorArea)", bad, "SD")
    bad.loc[3, "SD"] = -1
    with pytest.raises(ValueError, match="sampling variance"):
        fay_herriot("yi ~ C(MajorArea)", bad, "SD")


def test_bootstrap_mse_close_to_analytic(milk):
    a = fay_herriot("yi ~ C(MajorArea)", milk, "SD")
    b = fay_herriot("yi ~ C(MajorArea)", milk, "SD", mse_method="bootstrap", n_boot=200, seed=7)
    ratio = (b.estimates["mse"] / a.estimates["mse"]).mean()
    assert 0.8 < ratio < 1.2
    assert "bootstrap" in b.mse_method


def test_eblup_mse_tracks_simulated_truth():
    """Seeded simulation: the analytic REML MSE averages to the empirical MSE."""
    rng = np.random.default_rng(42)
    m = 40
    x = rng.uniform(0, 1, m)
    D = rng.uniform(0.2, 1.0, m)
    X = np.column_stack([np.ones(m), x])
    A = 0.5
    err, est = np.zeros(m), np.zeros(m)
    reps = 300
    for _ in range(reps):
        theta = 1 + 2 * x + rng.normal(0, math.sqrt(A), m)
        df = pd.DataFrame({"GEOID": range(m), "x": x, "se": np.sqrt(D),
                           "y": theta + rng.normal(0, 1, m) * np.sqrt(D)})
        r = fay_herriot("y ~ x", df, "se")
        err += (r.estimates["eblup"].to_numpy() - theta) ** 2
        est += r.estimates["mse"].to_numpy()
    assert (est / reps).mean() == pytest.approx((err / reps).mean(), rel=0.1)
    assert analytic_mse(A, X, D, "REML").shape == (m,)


def test_fh_regression_model_result(milk):
    mr = fh_regression("yi ~ C(MajorArea)", milk, "SD")
    assert isinstance(mr, ModelResult)
    assert mr.n_obs == 43 and mr.model_type == "fay_herriot"
    assert mr.coefficients.loc[0, "estimate"] == pytest.approx(0.9681890, abs=2e-6)
    assert mr.diagnostics["sigma2_u"] == pytest.approx(0.01855022, abs=5e-7)
    json.dumps(mr.to_dict())
