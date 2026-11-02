# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fay-Herriot area-level small-area estimation (PLAN_ROUND_8.md workstream C).

Model (Fay & Herriot 1979, JASA 74:269-277)::

    y_i = theta_i + e_i,    e_i ~ N(0, D_i)   (D_i known: ACS SE squared)
    theta_i = x_i' beta + u_i,  u_i ~ N(0, A)  (A = sigma2_u)

EBLUP: theta~_i = gamma_i y_i + (1 - gamma_i) x_i' beta^, gamma_i = A / (A + D_i).

Estimation of A by REML (default), ML, or the Fay-Herriot moment method
(Rao & Molina 2015, *Small Area Estimation*, 2nd ed., Wiley, sec. 6.1.2).

Analytic MSE (Rao & Molina 2015, sec. 6.2.1): g1 + g2 + 2 g3 (minus a bias term
for ML/FH), with

* g1_i = gamma_i D_i
* g2_i = (1 - gamma_i)^2 x_i' (X'V^-1 X)^-1 x_i
* g3_i = D_i^2 / (A + D_i)^3 * Vbar(A^)

This is the Prasad & Rao (1990, JASA 85:163-171) second-order approximation;
under REML/ML the asymptotic variance Vbar = 2 / sum (A + D_i)^-2 and the
corrections of Datta & Lahiri (2000, Statistica Sinica 10:613-627) apply (REML
needs no bias term; ML subtracts b_ML(A) * (D_i/(A+D_i))^2). The FH moment
estimator uses Datta, Rao & Smith (2005, Biometrika 92:183-196). These are the
formulas implemented by the R package ``sae`` (Molina & Marhuenda 2015, The R
Journal 7(1):81-98), whose published ``milk`` example the tests reproduce.

Parametric bootstrap MSE: Gonzalez-Manteiga et al. (2008); Rao & Molina 2015
sec. 6.2.4.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import optimize, stats as sps

from census.results import (
    COEFFICIENT_COLUMNS, SMALL_AREA_COLUMNS, SOURCE_MODEL, SOURCE_SAMPLING_MODEL,
    ModelResult, SmallAreaResult,
)

VARIANCE_METHODS = ("REML", "ML", "FH")
MSE_METHODS = ("analytic", "bootstrap")
REFERENCES = [
    "Fay, R.E. & Herriot, R.A. (1979). JASA 74(366), 269-277.",
    "Prasad, N.G.N. & Rao, J.N.K. (1990). JASA 85(409), 163-171.",
    "Datta, G.S. & Lahiri, P. (2000). Statistica Sinica 10, 613-627.",
    "Datta, G.S., Rao, J.N.K. & Smith, D.D. (2005). Biometrika 92(1), 183-196.",
    "Rao, J.N.K. & Molina, I. (2015). Small Area Estimation, 2nd ed. Wiley, ch. 6.",
    "Molina, I. & Marhuenda, Y. (2015). sae: An R Package for Small Area Estimation. "
    "The R Journal 7(1), 81-98.",
]


# ---------------------------------------------------------------------------
# Core numerics on arrays
# ---------------------------------------------------------------------------


def _gls(A: float, y: np.ndarray, X: np.ndarray, D: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """beta^(A) and Q = (X'V^-1X)^-1."""
    w = 1.0 / (A + D)
    Q = np.linalg.inv(X.T @ (X * w[:, None]))
    beta = Q @ (X.T @ (w * y))
    return beta, Q


def _loglik_ml(A: float, y, X, D) -> float:
    beta, _ = _gls(A, y, X, D)
    v = A + D
    r = y - X @ beta
    return float(-0.5 * np.sum(np.log(2 * np.pi * v) + r * r / v))


def _loglik_reml(A: float, y, X, D) -> float:
    beta, Q = _gls(A, y, X, D)
    v = A + D
    r = y - X @ beta
    _, logdet_q = np.linalg.slogdet(Q)
    # -0.5 log|X'V^-1X| = +0.5 log|Q|; constants dropped.
    return float(-0.5 * np.sum(np.log(v)) + 0.5 * logdet_q - 0.5 * np.sum(r * r / v))


def _fh_moment_eq(A: float, y, X, D) -> float:
    beta, _ = _gls(A, y, X, D)
    r = y - X @ beta
    return float(np.sum(r * r / (A + D)) - (len(y) - X.shape[1]))


def _maximize(fun, upper: float) -> float:
    """Maximize a 1-D function of A on [0, upper]: log grid, then bounded refine."""
    grid = np.concatenate([[0.0], np.geomspace(upper * 1e-8, upper, 400)])
    vals = np.array([fun(a) for a in grid])
    k = int(np.argmax(vals))
    lo = grid[max(k - 1, 0)]
    hi = grid[min(k + 1, len(grid) - 1)]
    res = optimize.minimize_scalar(lambda a: -fun(a), bounds=(lo, hi), method="bounded",
                                   options={"xatol": 1e-14 * max(upper, 1.0), "maxiter": 1000})
    best = float(res.x)
    if fun(0.0) >= fun(best):
        return 0.0
    return best


def estimate_sigma2_u(y: np.ndarray, X: np.ndarray, D: np.ndarray, method: str = "REML") -> float:
    """Between-area variance A = sigma2_u by REML, ML or FH moments (truncated at 0)."""
    method = method.upper()
    if method not in VARIANCE_METHODS:
        raise ValueError(f"method must be one of {VARIANCE_METHODS}")
    m, p = X.shape
    if m <= p:
        raise ValueError(f"need more areas ({m}) than regression coefficients ({p})")
    upper = max(float(np.var(y, ddof=1)) * 10.0, float(np.max(D)) * 10.0, 1e-12)
    if method == "FH":
        if _fh_moment_eq(0.0, y, X, D) <= 0:
            return 0.0
        while _fh_moment_eq(upper, y, X, D) > 0:
            upper *= 10.0
        return float(optimize.brentq(_fh_moment_eq, 0.0, upper, args=(y, X, D), xtol=1e-15, rtol=1e-14))
    fun = _loglik_reml if method == "REML" else _loglik_ml
    return _maximize(lambda a: fun(a, y, X, D), upper)


def _fit_arrays(y, X, D, method: str) -> Dict[str, Any]:
    A = estimate_sigma2_u(y, X, D, method)
    beta, Q = _gls(A, y, X, D)
    synthetic = X @ beta
    gamma = A / (A + D) if A > 0 else np.zeros_like(D)
    gamma = np.where(D == 0, 1.0, gamma)
    eblup = gamma * y + (1 - gamma) * synthetic
    return {"A": A, "beta": beta, "Q": Q, "synthetic": synthetic, "gamma": gamma, "eblup": eblup}


def analytic_mse(A: float, X: np.ndarray, D: np.ndarray, method: str = "REML") -> np.ndarray:
    """Second-order MSE: Prasad-Rao (1990) / Datta-Lahiri (2000) / Datta-Rao-Smith (2005)."""
    method = method.upper()
    v = A + D
    m = len(D)
    _, Q = _gls(A, np.zeros(m), X, D)
    g1 = A * D / v
    g2 = (D / v) ** 2 * np.einsum("ij,jk,ik->i", X, Q, X)
    s1 = np.sum(1.0 / v)
    s2 = np.sum(1.0 / v ** 2)
    if method in ("REML", "ML"):
        var_a = 2.0 / s2
    else:
        var_a = 2.0 * m / s1 ** 2
    g3 = D ** 2 / v ** 3 * var_a
    mse = g1 + g2 + 2.0 * g3
    if method == "ML":
        w2 = 1.0 / v ** 2
        b = -np.trace(Q @ (X.T @ (X * w2[:, None]))) / s2
        mse = mse - b * (D / v) ** 2
    elif method == "FH":
        b = 2.0 * (m * s2 - s1 ** 2) / s1 ** 3
        mse = mse - b * (D / v) ** 2
    return mse


def bootstrap_mse(y, X, D, method: str, fit: Dict[str, Any], n_boot: int = 500,
                  seed: Optional[int] = None) -> np.ndarray:
    """Parametric bootstrap MSE (Gonzalez-Manteiga et al. 2008; Rao & Molina 2015 6.2.4)."""
    rng = np.random.default_rng(seed)
    m = len(y)
    acc = np.zeros(m)
    for _ in range(int(n_boot)):
        theta = fit["synthetic"] + rng.normal(0.0, math.sqrt(fit["A"]), m)
        ystar = theta + rng.normal(0.0, 1.0, m) * np.sqrt(D)
        acc += (_fit_arrays(ystar, X, D, method)["eblup"] - theta) ** 2
    return acc / int(n_boot)


# ---------------------------------------------------------------------------
# Frame interface
# ---------------------------------------------------------------------------


def _design(formula: str, frame: pd.DataFrame, se_col: str, geoid_col: str):
    import patsy

    if se_col not in frame.columns:
        raise KeyError(f"standard-error column {se_col!r} not in frame")
    se = pd.to_numeric(frame[se_col], errors="coerce")
    bad = ~np.isfinite(se.to_numpy(dtype=float)) | (se.to_numpy(dtype=float) < 0)
    if bad.any():
        ids = frame.loc[bad, geoid_col] if geoid_col in frame.columns else frame.index[bad]
        raise ValueError(
            "Fay-Herriot needs a defined sampling variance for every area; "
            f"{int(bad.sum())} area(s) have missing, negative or infinite {se_col!r}: "
            f"{list(map(str, list(ids)[:20]))}. Drop them explicitly (they keep their "
            "direct estimate status) or supply a variance."
        )
    try:
        yd, Xd = patsy.dmatrices(formula, frame, NA_action="raise", return_type="dataframe")
    except patsy.PatsyError as exc:
        raise ValueError(f"formula {formula!r} could not be built (missing values?): {exc}") from exc
    y = yd.iloc[:, 0].to_numpy(dtype=float)
    X = Xd.to_numpy(dtype=float)
    D = se.loc[yd.index].to_numpy(dtype=float) ** 2
    if np.linalg.matrix_rank(X) < X.shape[1]:
        raise ValueError("design matrix is rank deficient")
    ids = frame.loc[yd.index, geoid_col] if geoid_col in frame.columns else pd.Series(yd.index, index=yd.index)
    return y, X, D, list(Xd.columns), ids.astype(str).to_numpy(), yd.columns[0]


def _coef_frame(terms: List[str], beta, Q, z: float) -> pd.DataFrame:
    se = np.sqrt(np.diag(Q))
    zval = beta / se
    return pd.DataFrame({
        "term": terms, "estimate": beta, "se_model": np.nan, "se_sampling": np.nan,
        "se_total": se, "ci_lower": beta - z * se, "ci_upper": beta + z * se,
        "p_value": 2 * sps.norm.sf(np.abs(zval)), "z_value": zval,
    })[COEFFICIENT_COLUMNS + ["z_value"]]


def _goodness(A: float, y, X, D) -> Dict[str, float]:
    # As in sae::eblupFH: Gaussian log-likelihood at (A^, beta^), AIC/BIC with p+1 params.
    m, p = X.shape
    ll = _loglik_ml(A, y, X, D)
    return {"loglike": ll, "aic": -2 * ll + 2 * (p + 1), "bic": -2 * ll + (p + 1) * math.log(m)}


def fay_herriot(
    formula: str,
    frame: pd.DataFrame,
    se_col: str,
    *,
    method: str = "REML",
    mse_method: str = "analytic",
    n_boot: int = 500,
    seed: Optional[int] = None,
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
) -> SmallAreaResult:
    """Fit a Fay-Herriot model and return EBLUPs with MSEs (SMALL_AREA_COLUMNS).

    ``formula`` is a patsy formula whose left side is the direct-estimate
    column (e.g. ``"poverty_rate ~ unemployment + C(region)"``); ``se_col`` is
    the column of direct-estimate standard errors (D_i = se^2). Areas with an
    undefined sampling variance are refused (ValueError). ``method``: REML
    (default), ML or FH. ``mse_method``: ``analytic`` (Prasad-Rao with the
    Datta-Lahiri correction under REML/ML) or ``bootstrap`` (parametric).
    """
    method = method.upper()
    if method not in VARIANCE_METHODS:
        raise ValueError(f"method must be one of {VARIANCE_METHODS}")
    if mse_method not in MSE_METHODS:
        raise ValueError(f"mse_method must be one of {MSE_METHODS}")
    y, X, D, terms, ids, yname = _design(formula, frame, se_col, geoid_col)
    fit = _fit_arrays(y, X, D, method)
    A = fit["A"]
    notes: List[str] = []
    if A == 0.0:
        notes.append(
            "sigma2_u estimated at 0: every EBLUP equals the synthetic (regression) "
            "estimate (complete shrinkage). This is a known Fay-Herriot failure mode; "
            "the analytic MSE may understate uncertainty. Consider bootstrap MSE, "
            "another variance method, or reporting direct estimates."
        )
    if np.any(D == 0):
        notes.append(f"{int(np.sum(D == 0))} area(s) have zero sampling variance "
                     "(controlled estimates): EBLUP equals the direct estimate there.")
    if mse_method == "analytic":
        mse = analytic_mse(A, X, D, method)
        mse_label = {"REML": "Prasad-Rao with Datta-Lahiri (REML)",
                     "ML": "Prasad-Rao with Datta-Lahiri bias correction (ML)",
                     "FH": "Prasad-Rao with Datta-Rao-Smith bias correction (FH)"}[method]
    else:
        mse = bootstrap_mse(y, X, D, method, fit, n_boot=n_boot, seed=seed)
        mse_label = f"parametric bootstrap (B={int(n_boot)}, seed={seed})"
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    z = float(sps.norm.ppf(1 - (1 - level) / 2))
    rmse = np.sqrt(mse)
    est = pd.DataFrame({
        "GEOID": ids, "direct": y, "direct_se": np.sqrt(D), "synthetic": fit["synthetic"],
        "eblup": fit["eblup"], "mse": mse, "rmse": rmse,
        "ci_lower": fit["eblup"] - z * rmse, "ci_upper": fit["eblup"] + z * rmse,
        "gamma": fit["gamma"],
    })[SMALL_AREA_COLUMNS]
    provenance = {
        "formula": formula, "response": yname, "se_col": se_col, "n_areas": int(len(y)),
        "n_coefficients": int(X.shape[1]), **_goodness(A, y, X, D), "references": REFERENCES,
    }
    return SmallAreaResult(
        estimates=est, sigma2_u=float(A), coefficients=_coef_frame(terms, fit["beta"], fit["Q"], z),
        method="fay_herriot_eblup", variance_method=method, mse_method=mse_label,
        confidence_level=level, notes=notes, provenance=provenance,
    )


def fh_regression(
    formula: str,
    frame: pd.DataFrame,
    se_col: str,
    *,
    method: str = "REML",
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
) -> ModelResult:
    """Regression of a survey outcome with known sampling variance (FH model's beta).

    beta^ is the GLS estimate under V = diag(sigma2_u + se_i^2); its standard
    errors sqrt(diag((X'V^-1X)^-1)) include both the known ACS sampling error
    of the outcome and between-area model error (Fay & Herriot 1979; Rao &
    Molina 2015 sec. 6.1). They do not include uncertainty from estimating
    sigma2_u. Covariates are treated as error-free.
    """
    res = fay_herriot(formula, frame, se_col, method=method, confidence_level=confidence_level,
                      geoid_col=geoid_col)
    notes = ["se_total includes known outcome sampling variance and between-area variance "
             "sigma2_u (GLS); covariates are treated as measured without error."] + res.notes
    diag = {k: res.provenance[k] for k in ("loglike", "aic", "bic")}
    diag.update({"sigma2_u": res.sigma2_u, "variance_method": res.variance_method})
    return ModelResult(
        model_type="fay_herriot", formula=formula, n_obs=res.provenance["n_areas"],
        coefficients=res.coefficients, method=f"GLS with REML/ML/FH sigma2_u ({res.variance_method})",
        uncertainty_sources=[SOURCE_MODEL, SOURCE_SAMPLING_MODEL], confidence_level=res.confidence_level,
        notes=notes, diagnostics=diag,
        provenance={"se_col": se_col, "references": REFERENCES},
    )
