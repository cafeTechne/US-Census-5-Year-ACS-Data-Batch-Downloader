# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression with known ACS sampling error (PLAN_ROUND_8 workstream B).

Inputs follow the analysis-frame convention of :mod:`census.results` (built by
:func:`census.analysis.analysis_frame`); results are
:class:`census.results.ModelResult`.

B1. Replicate inference (preferred), :func:`fit_replicate`. The model is fit
    on the full-sample estimates and refit on each of the 80 replicate data
    sets (every ACS variable replaced by its r-th replicate value). Then

        Var_sampling(b) = 4/80 * sum_r (b_r - b)^2

    the successive-difference replication formula of the ACS Variance
    Replicate Estimate tables (U.S. Census Bureau, "Variance Replicate
    Estimate Tables Documentation";
    https://www.census.gov/programs-surveys/acs/data/variance-tables.html; and
    ACS Design and Methodology, 2014, ch. 13). Following decision 2 of
    PLAN_ROUND_8, ``se_total = sqrt(se_model^2 + se_sampling^2)``: the model
    SE of the full fit plus the replicate sampling SE. Because the model
    residual variance already absorbs some outcome sampling noise, the total
    is conservative; both components are always reported.

B2. Model-based corrections for covariates measured with known error
    (when replicates are unavailable):

    * :func:`regression_calibration`: each noisy covariate is replaced by its
      best linear predictor given all observed covariates, with
      heteroscedastic known error variances s_i^2 (Carroll, Ruppert,
      Stefanski & Crainiceanu, "Measurement Error in Nonlinear Models: A
      Modern Perspective", 2nd ed., 2006, ch. 4). SEs by row bootstrap that
      re-estimates the calibration (Carroll et al. 2006, sec. 4.6 / App. B.5).
    * :func:`simex`: simulation-extrapolation (Cook & Stefanski 1994, JASA
      89:1314-1328), heteroscedastic form W + sqrt(lambda) s_i Z
      (Devanarayan & Stefanski 2002, Stat. Prob. Letters 59:219-225),
      quadratic extrapolation to lambda = -1; variance by the SIMEX
      "measurement-error jackknife" (Stefanski & Cook 1995, JASA 90:1247-1256).

    Both always report the naive fit alongside (``naive_estimate``,
    ``naive_se`` columns and ``diagnostics["naive"]``).

    Outcomes with known sampling variance (Fay-Herriot / meta-regression
    structure, weights 1/(sigma_u^2 + s_i^2)) are handled by
    ``census.sae.fh_regression``, not here.

B3. Models: ``ols``, ``wls`` (``weights`` column), ``binomial`` (GLM for
    proportions in [0, 1], trials = a column or the effective sample size
    p(1-p)/SE(p)^2 (Kish 1965 design-effect logic)), ``poisson`` (GLM with
    ``exposure``), ``negbin`` (statsmodels NegativeBinomial with ``exposure``).
    Fitting uses statsmodels formula models (patsy).
B4. Numeric diagnostics: share of residual variance attributable to known
    outcome sampling error, influence of high-CV observations, coefficient
    stability across replicates, and the reliability screen (CV > 30% kept and
    reported, decision 4).
"""

import math
import warnings
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy import stats as sps

from .analysis import formula_variables
from .results import (
    COEFFICIENT_COLUMNS,
    TARGET_SE,
    interval_description,
    SOURCE_MODEL,
    SOURCE_SAMPLING_MODEL,
    SOURCE_SAMPLING_REPLICATE,
    ModelResult,
    check_refit_count,
    check_replicate_policy,
    precheck_replicates,
    replicate_completeness,
    stochastic_variables,
)
from .variance import REPLICATE_COLUMNS, REPLICATE_COUNT, REPLICATE_FACTOR

MODELS = ("ols", "wls", "binomial", "poisson", "negbin")
DEFAULT_CV_THRESHOLD = 30.0
FH_POINTER = (
    "for an outcome with known sampling variance use census.sae.fh_regression "
    "(Fay-Herriot weights 1/(sigma_u^2 + s_i^2))"
)


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _outcome(formula: str) -> str:
    return formula.split("~", 1)[0].strip()


def _fit(
    formula: str,
    data: pd.DataFrame,
    model: str,
    weights: Optional[str] = None,
    exposure: Optional[str] = None,
    trials: Optional[np.ndarray] = None,
) -> Any:
    model = model.lower()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if model == "ols":
            return smf.ols(formula, data, missing="drop").fit()
        if model == "wls":
            if not weights:
                raise ValueError("model='wls' needs weights=<column>")
            return smf.wls(formula, data, weights=data[weights], missing="drop").fit()
        if model == "binomial":
            if trials is None:
                raise ValueError("model='binomial' needs trials")
            return smf.glm(formula, data, family=sm.families.Binomial(),
                           var_weights=np.asarray(trials, float), missing="drop").fit()
        expo = data[exposure].to_numpy(float) if exposure else None
        if model == "poisson":
            return smf.glm(formula, data, family=sm.families.Poisson(), exposure=expo, missing="drop").fit()
        if model == "negbin":
            return smf.negativebinomial(formula, data, exposure=expo, missing="drop").fit(disp=0, maxiter=200)
    raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")


def _complete_rows(frame: pd.DataFrame, formula: str, extra: Sequence[Optional[str]]) -> pd.DataFrame:
    cols = formula_variables(formula, frame) + [c for c in extra if c]
    num = [c for c in cols if pd.api.types.is_numeric_dtype(frame[c])]
    mask = np.isfinite(frame[num].to_numpy(float)).all(axis=1) if num else np.ones(len(frame), bool)
    mask &= frame[[c for c in cols if c not in num]].notna().all(axis=1).to_numpy() if len(cols) > len(num) else True
    return frame.loc[mask].reset_index(drop=True)


def effective_trials(p: np.ndarray, se: np.ndarray) -> np.ndarray:
    """Effective sample size p(1-p)/SE^2 of a proportion estimate (NaN when undefined)."""
    p, se = np.asarray(p, float), np.asarray(se, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        n = p * (1 - p) / se ** 2
    return np.where((p > 0) & (p < 1) & (se > 0) & np.isfinite(n), n, np.nan)


def _prepare(frame, formula, model, weights, exposure, trials):
    data = _complete_rows(frame, formula, [weights, exposure, trials if isinstance(trials, str) else None])
    n_trials = None
    notes: List[str] = []
    if model == "binomial":
        y = _outcome(formula)
        if isinstance(trials, str):
            n_trials = data[trials].to_numpy(float)
        else:
            if f"{y}_se" not in data:
                raise ValueError(f"binomial model needs trials=<column> or a '{y}_se' column for effective trials")
            n_trials = effective_trials(data[y].to_numpy(float), data[f"{y}_se"].to_numpy(float))
            notes.append("binomial trials = effective sample size p(1-p)/SE(p)^2 of the outcome")
        if np.nanmax(data[y].to_numpy(float)) > 1 or np.nanmin(data[y].to_numpy(float)) < 0:
            raise ValueError("binomial outcome must be a proportion in [0, 1] (divide percents by 100)")
        keep = np.isfinite(n_trials) & (n_trials > 0)
        if (~keep).any():
            notes.append(f"{int((~keep).sum())} row(s) dropped: effective trials undefined (p in {{0,1}} or SE 0/missing)")
        data, n_trials = data.loc[keep].reset_index(drop=True), n_trials[keep]
    return data, n_trials, notes


def _z(confidence_level: float) -> float:
    return float(sps.norm.ppf(0.5 + confidence_level / 2.0))


# Which standard error the interval and p-value use (validated in
# docs/audits/inference_validation.md, 1,000 simulated datasets, nominal 90%):
#   "population"   se_model:    coverage 0.896 for the superpopulation coefficient.
#                  The model residual already absorbs outcome sampling error.
#   "these_areas"  se_sampling: coverage 0.895 for the coefficient among these
#                  areas in this period (finite population), from replicates.
#   "conservative" se_total = sqrt(se_model^2 + se_sampling^2): coverage 0.961;
#                  double-counts outcome sampling error (PLAN_ROUND_8 decision 2,
#                  revised after validation).
INTERVAL_TARGETS = ("population", "these_areas", "conservative")


_TARGET_SE = TARGET_SE


def interval_se_column(result: Any) -> str:
    """The SE column that a result's p-values and intervals are based on.

    fit_replicate records ``diagnostics['interval_target']``; corrected
    methods (regression calibration, SIMEX) and Fay-Herriot use se_total.
    See census.results.interval_description for the full interpretation.
    """
    return interval_description(result)["basis_column"]


def _table(params: pd.Series, se_model: pd.Series, se_sampling: pd.Series, confidence_level: float,
           target: str = "population") -> pd.DataFrame:
    se_total = np.sqrt(se_model.to_numpy(float) ** 2 + np.nan_to_num(se_sampling.to_numpy(float)) ** 2)
    se_total = np.where(np.isnan(se_model.to_numpy(float)), np.nan, se_total)
    basis = {"population": se_model.to_numpy(float), "these_areas": se_sampling.to_numpy(float),
             "conservative": se_total}[target]
    z = _z(confidence_level)
    est = params.to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = 2 * sps.norm.sf(np.abs(est / basis))
    return pd.DataFrame({
        "term": list(params.index), "estimate": est,
        "se_model": se_model.to_numpy(float), "se_sampling": se_sampling.to_numpy(float),
        "se_total": se_total, "ci_lower": est - z * basis, "ci_upper": est + z * basis,
        "p_value": p,
    })[COEFFICIENT_COLUMNS]


# ---------------------------------------------------------------------------
# B2. reliability screen
# ---------------------------------------------------------------------------


def reliability_screen(
    frame: pd.DataFrame,
    variables: Optional[Union[str, Sequence[str]]] = None,
    threshold: float = DEFAULT_CV_THRESHOLD,
) -> pd.DataFrame:
    """Observations whose CV (percent) exceeds ``threshold`` (Census: >30% is low reliability).

    ``variables``: names, or a formula whose frame variables are screened.
    Rows are reported, never dropped (PLAN_ROUND_8 decision 4).
    """
    if isinstance(variables, str):
        variables = formula_variables(variables, frame) if "~" in variables else [variables]
    names = [v for v in (variables or [c for c in frame.columns if f"{c}_cv" in frame.columns])
             if f"{v}_cv" in frame.columns]
    keys = [c for c in ("GEOID", "Year", "NAME") if c in frame.columns]
    parts = []
    for v in names:
        cv = frame[f"{v}_cv"].to_numpy(float)
        hit = cv > threshold
        if hit.any():
            part = frame.loc[hit, keys].copy()
            part["variable"] = v
            part["estimate"] = frame.loc[hit, v].to_numpy(float)
            part["se"] = frame.loc[hit, f"{v}_se"].to_numpy(float)
            part["cv"] = cv[hit]
            parts.append(part)
    cols = keys + ["variable", "estimate", "se", "cv"]
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    out.attrs["threshold"] = threshold
    return out


def _screen_summary(frame: pd.DataFrame, formula: str, threshold: float) -> Dict[str, Any]:
    screen = reliability_screen(frame, formula, threshold)
    counts = screen["variable"].value_counts().to_dict() if len(screen) else {}
    return {"threshold_cv_percent": threshold, "rows_flagged_by_variable": counts,
            "n_flags": int(len(screen))}


# ---------------------------------------------------------------------------
# B1. replicate refits
# ---------------------------------------------------------------------------


def _replicate_matrices(data: pd.DataFrame, replicates: pd.DataFrame, used: Sequence[str]) -> Tuple[Dict[str, np.ndarray], Dict[str, int]]:
    keys = ["GEOID"] + (["Year"] if "Year" in data.columns and "Year" in replicates.columns else [])
    idx = pd.MultiIndex.from_frame(data[keys]) if len(keys) > 1 else pd.Index(data["GEOID"].astype(str))
    mats: Dict[str, np.ndarray] = {}
    fixed: Dict[str, int] = {}
    for v in used:
        sub = replicates[replicates["variable"] == v]
        if not len(sub):
            continue
        sub = sub.assign(GEOID=sub["GEOID"].astype(str)).set_index(keys)
        m = sub[REPLICATE_COLUMNS].reindex(idx).to_numpy(float, copy=True)  # (n, 80)
        miss = ~np.isfinite(m).all(axis=1)
        if miss.any():  # hold the full-sample value fixed where no replicates exist
            m[miss] = data[v].to_numpy(float)[miss, None]
            fixed[v] = int(miss.sum())
        mats[v] = m
    return mats, fixed


def fit_replicate(
    formula: str,
    frame: pd.DataFrame,
    replicates: Optional[pd.DataFrame] = None,
    model: str = "ols",
    weights: Optional[str] = None,
    exposure: Optional[str] = None,
    trials: Optional[str] = None,
    confidence_level: float = 0.90,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
    max_failed_replicates: int = 0,
    target: str = "population",
    replicate_policy: str = "flag",
) -> ModelResult:
    """Fit a statsmodels formula model with ACS replicate sampling SEs.

    ``replicates`` is the long frame from
    :func:`census.analysis.attach_replicates`. Every formula variable present
    there is replaced by its replicate values in each of the 80 refits;
    others (and weights, exposure, trials) stay at their full-sample values.
    Without replicates only model SEs are reported, with a note pointing to
    :func:`regression_calibration`, :func:`simex`, or census.sae.fh_regression.
    ``target`` chooses the SE behind ``ci_lower``/``ci_upper``/``p_value``
    (see INTERVAL_TARGETS): "population" (default, se_model) for the general
    relationship; "these_areas" (se_sampling, needs replicates) for the
    relationship among these areas given ACS sampling error; "conservative"
    (se_total). All three SEs are always reported. ``replicate_policy``
    (census.results.replicate_completeness): "flag" (default, exploratory)
    keeps the estimate when refits failed (up to ``max_failed_replicates``),
    rows lack replicates, or a variable with an SE column has no replicates,
    and records diagnostics["replicates_incomplete"]; "strict" (confirmatory,
    used by census.manifest) raises ValueError instead. Replicates do not correct
    attenuation from noisy covariates; use regression_calibration for that.
    """
    model = model.lower()
    check_replicate_policy(replicate_policy)
    if target not in INTERVAL_TARGETS:
        raise ValueError(f"target must be one of {INTERVAL_TARGETS}")
    if target == "these_areas" and (replicates is None or not len(replicates)):
        raise ValueError("target='these_areas' needs replicates (se_sampling)")
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")
    data, n_trials, notes = _prepare(frame, formula, model, weights, exposure, trials)
    full = _fit(formula, data, model, weights, exposure, n_trials)
    rows = np.asarray(full.model.data.row_labels if full.model.data.row_labels is not None else data.index)
    if len(rows) != len(data):
        notes.append(f"{len(data) - len(rows)} row(s) dropped by the formula (non-finite transforms)")
    params, bse = full.params, full.bse
    used = formula_variables(formula, data)
    diagnostics: Dict[str, Any] = {"reliability_screen": _screen_summary(data, formula, cv_threshold)}
    provenance: Dict[str, Any] = {"model_library": "statsmodels", "rows_used": int(len(rows))}
    se_sampling = pd.Series(np.nan, index=params.index)
    sources = [SOURCE_MODEL]
    method = "full_sample_fit"

    if replicates is not None and len(replicates):
        mats, fixed = _replicate_matrices(data, replicates, used)
        if not mats:
            raise ValueError("no formula variable has replicates in the replicate frame")
        missing = [v for v in used if v not in mats]
        stoch_missing = stochastic_variables(frame, missing)
        precheck_replicates(replicate_policy, rows_held_fixed=fixed, variables_without_replicates=stoch_missing,
                            context="fit_replicate")
        coefs, failed = [], 0
        for r in range(REPLICATE_COUNT):
            d = data.copy()
            for v, m in mats.items():
                d[v] = m[:, r]
            try:
                res = _fit(formula, d, model, weights, exposure, n_trials)
                b = res.params.reindex(params.index).to_numpy(float)
                if not np.isfinite(b).all():
                    raise ValueError("non-finite coefficients")
                coefs.append(b)
            except Exception:  # noqa: BLE001 - any refit failure is counted and reported
                failed += 1
        replicate_completeness(replicate_policy, failed_refits=failed, rows_held_fixed=fixed,
                               variables_without_replicates=stoch_missing,
                               context="fit_replicate", diagnostics=diagnostics, notes=notes)
        check_refit_count(failed, len(coefs), max_failed_replicates, "fit_replicate")
        B = np.vstack(coefs)  # (n_ok, k)
        b0 = params.to_numpy(float)
        se_sampling = pd.Series(np.sqrt(REPLICATE_FACTOR * ((B - b0) ** 2).sum(axis=0)), index=params.index)
        sources.append(SOURCE_SAMPLING_REPLICATE)
        method = "replicate_refit"
        fixed_vars = [v for v in missing if v not in stoch_missing]
        if fixed_vars:   # no SE column: genuinely fixed (stochastic ones are in REPLICATES INCOMPLETE)
            notes.append(f"variables without replicates or SEs treated as fixed: {fixed_vars}")
        diagnostics["replicate_stability"] = {
            t: {"sd": float(B[:, j].std(ddof=1)) if len(B) > 1 else np.nan,
                "share_same_sign": float(np.mean(np.sign(B[:, j]) == np.sign(b0[j])))}
            for j, t in enumerate(params.index)
        }
        diagnostics["replicate_failures"] = failed
        provenance["replicate_variables"] = sorted(mats)
        notes.append("se_total = sqrt(se_model^2 + se_sampling^2) is reported but conservative (validated "
                     "coverage 0.961 at nominal 0.90): the model residual already absorbs outcome sampling error")
    else:
        notes.append("no replicates: SEs reflect model error only, not ACS sampling error of inputs; use "
                     "replicates, regression_calibration/simex for noisy covariates, or " + FH_POINTER)

    table = _table(params, bse, se_sampling, confidence_level, target)
    notes.append(f"intervals and p-values use the '{target}' target ({TARGET_SE[target]})")
    diagnostics["interval_target"] = target
    diagnostics.update(_numeric_diagnostics(formula, data, full, model, table, weights, exposure,
                                            n_trials, cv_threshold))
    return ModelResult(model_type=model, formula=formula, n_obs=int(full.nobs), coefficients=table,
                       method=method, uncertainty_sources=sources, confidence_level=confidence_level,
                       notes=notes, diagnostics=diagnostics, provenance=provenance)


def _numeric_diagnostics(formula, data, full, model, table, weights, exposure, n_trials, threshold) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    y = _outcome(formula)
    rows = full.model.data.row_labels
    used = data.loc[rows] if rows is not None else data
    if model in ("ols", "wls") and f"{y}_se" in used.columns:
        s2 = np.nanmean(used[f"{y}_se"].to_numpy(float) ** 2)
        r2 = float(np.mean(np.asarray(full.resid) ** 2))
        out["sampling_share_of_residual_variance"] = float(s2 / r2) if r2 > 0 else np.nan
    cv_cols = [f"{v}_cv" for v in formula_variables(formula, data) if f"{v}_cv" in data.columns]
    if cv_cols:
        high = (data[cv_cols].to_numpy(float) > threshold).any(axis=1)
        k = len(table)
        info: Dict[str, Any] = {"n_high_cv_rows": int(high.sum())}
        if 0 < high.sum() and (~high).sum() > k + 1:
            try:
                res = _fit(formula, data.loc[~high].reset_index(drop=True), model, weights, exposure,
                           None if n_trials is None else n_trials[~high])
                b = res.params.reindex(table["term"]).to_numpy(float)
                shift = (b - table["estimate"].to_numpy(float)) / table["se_total"].to_numpy(float)
                info["coef_shift_in_se_total_without_high_cv"] = dict(zip(table["term"], shift.tolist()))
            except Exception as exc:  # noqa: BLE001
                info["error"] = str(exc)
        out["high_cv_influence"] = info
    return out


# ---------------------------------------------------------------------------
# B2. measurement-error corrections for covariates
# ---------------------------------------------------------------------------


def _error_spec(frame: pd.DataFrame, error_covariates: Union[Sequence[str], Mapping[str, str]]) -> Dict[str, str]:
    spec = dict(error_covariates) if isinstance(error_covariates, Mapping) else {
        v: f"{v}_se" for v in error_covariates}
    for v, s in spec.items():
        if v not in frame.columns or s not in frame.columns:
            raise KeyError(f"error covariate {v!r} needs columns {v!r} and {s!r}")
    if not spec:
        raise ValueError("error_covariates is empty")
    return spec


def _calibrate(data: pd.DataFrame, spec: Dict[str, str], others: List[str]) -> pd.DataFrame:
    """Replace each W by E[X | W, Z] (Carroll et al. 2006, eq. 4.4, heteroscedastic Sigma_uu,i)."""
    ws = list(spec)
    obs = data[ws + others].to_numpy(float)  # (n, k+m)
    s2 = data[[spec[w] for w in ws]].to_numpy(float) ** 2  # (n, k)
    mu = obs.mean(axis=0)
    cov = np.atleast_2d(np.cov(obs, rowvar=False))
    k = len(ws)
    sig = cov.copy()
    sig[np.arange(k), np.arange(k)] -= s2.mean(axis=0)  # Sigma_xx = Sigma_ww - mean Sigma_uu
    floor = 1e-6 * np.maximum(cov[np.arange(k), np.arange(k)], 1e-12)
    sig[np.arange(k), np.arange(k)] = np.maximum(sig[np.arange(k), np.arange(k)], floor)
    C = sig[:k, :]  # Cov(X, (W, Z))
    out = data.copy()
    centered = obs - mu
    xhat = np.empty((len(data), k))
    for i in range(len(data)):
        S = sig.copy()
        S[np.arange(k), np.arange(k)] += s2[i]
        xhat[i] = mu[:k] + C @ np.linalg.solve(S, centered[i])
    for j, w in enumerate(ws):
        out[w] = xhat[:, j]
    return out


def _corrected_result(formula, model, naive, est, se_total, confidence_level, method, notes, diagnostics, n) -> ModelResult:
    se_model = naive.bse.reindex(est.index)
    se_samp = pd.Series(np.sqrt(np.maximum(se_total.to_numpy(float) ** 2 - se_model.to_numpy(float) ** 2, 0.0)),
                        index=est.index)
    table = _table(est, se_model, se_samp, confidence_level)
    # keep the reported total exactly (it is not reconstructed from components)
    z = _z(confidence_level)
    table["se_total"] = se_total.to_numpy(float)
    table["ci_lower"] = table["estimate"] - z * table["se_total"]
    table["ci_upper"] = table["estimate"] + z * table["se_total"]
    with np.errstate(divide="ignore", invalid="ignore"):
        table["p_value"] = 2 * sps.norm.sf(np.abs(table["estimate"] / table["se_total"]))
    table["naive_estimate"] = naive.params.reindex(est.index).to_numpy(float)
    table["naive_se"] = se_model.to_numpy(float)
    diagnostics["naive"] = pd.DataFrame({"term": list(naive.params.index), "estimate": naive.params.to_numpy(float),
                                         "se_model": naive.bse.to_numpy(float)})
    notes = notes + ["se_model is the naive-fit model SE; se_sampling = sqrt(max(se_total^2 - se_model^2, 0)); "
                     "naive_estimate shows the uncorrected (attenuated) coefficients"]
    return ModelResult(model_type=model, formula=formula, n_obs=n, coefficients=table, method=method,
                       uncertainty_sources=[SOURCE_MODEL, SOURCE_SAMPLING_MODEL],
                       confidence_level=confidence_level, notes=notes, diagnostics=diagnostics)


def _setup(formula, frame, error_covariates, model, weights, exposure, trials):
    model = model.lower()
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")
    spec = _error_spec(frame, error_covariates)
    data, n_trials, notes = _prepare(frame, formula, model, weights, exposure, trials)
    keep = np.isfinite(data[list(spec.values())].to_numpy(float)).all(axis=1)
    data = data.loc[keep].reset_index(drop=True)
    if n_trials is not None:
        n_trials = n_trials[keep]
    y = _outcome(formula)
    others = [v for v in formula_variables(formula, data)
              if v not in spec and v != y and v not in (weights, exposure)
              and pd.api.types.is_numeric_dtype(data[v])]
    return model, spec, data, n_trials, notes, others


def regression_calibration(
    formula: str,
    frame: pd.DataFrame,
    error_covariates: Union[Sequence[str], Mapping[str, str]],
    model: str = "ols",
    weights: Optional[str] = None,
    exposure: Optional[str] = None,
    trials: Optional[str] = None,
    confidence_level: float = 0.90,
    n_boot: int = 200,
    seed: Optional[int] = 0,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
) -> ModelResult:
    """Regression calibration for covariates with known sampling SEs.

    ``error_covariates``: names (SE column ``<name>_se``) or ``{name: se_column}``.
    Each W is replaced by E[X | W, Z] = mu + Cov(X,(W,Z)) Var_i(W,Z)^{-1} ((W,Z) - mu),
    where Var_i adds row i's known error variance s_i^2 to the W block and
    Var(X) = Var(W) - mean(s^2) (method of moments). Other numeric formula
    variables are the error-free Z. Source: Carroll, Ruppert, Stefanski &
    Crainiceanu (2006), "Measurement Error in Nonlinear Models", 2nd ed., ch. 4.
    Exact for linear models; an approximation for GLMs (ch. 4.8). SEs from
    ``n_boot`` row-bootstrap replicates that re-estimate the calibration.
    """
    model, spec, data, n_trials, notes, others = _setup(formula, frame, error_covariates, model, weights, exposure, trials)
    naive = _fit(formula, data, model, weights, exposure, n_trials)
    cal = _fit(formula, _calibrate(data, spec, others), model, weights, exposure, n_trials)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(data), len(data))
        d = data.iloc[idx].reset_index(drop=True)
        try:
            res = _fit(formula, _calibrate(d, spec, others), model, weights, exposure,
                       None if n_trials is None else n_trials[idx])
            boots.append(res.params.reindex(cal.params.index).to_numpy(float))
        except Exception:  # noqa: BLE001
            continue
    B = np.vstack(boots)
    se_total = pd.Series(B.std(axis=0, ddof=1), index=cal.params.index)
    diagnostics = {"reliability_screen": _screen_summary(data, formula, cv_threshold),
                   "bootstrap_replicates": len(boots),
                   "reliability_ratio": {w: float(1 - np.mean(data[s] ** 2) / np.var(data[w], ddof=1))
                                         for w, s in spec.items()}}
    notes = notes + [f"regression calibration of {list(spec)} given {others}; se_total from {len(boots)} "
                     "row-bootstrap refits including calibration", FH_POINTER]
    return _corrected_result(formula, model, naive, cal.params, se_total, confidence_level,
                             "regression_calibration", notes, diagnostics, int(cal.nobs))


def simex(
    formula: str,
    frame: pd.DataFrame,
    error_covariates: Union[Sequence[str], Mapping[str, str]],
    model: str = "ols",
    weights: Optional[str] = None,
    exposure: Optional[str] = None,
    trials: Optional[str] = None,
    confidence_level: float = 0.90,
    lambdas: Sequence[float] = (0.5, 1.0, 1.5, 2.0),
    n_sim: int = 100,
    extrapolation: str = "quadratic",
    seed: Optional[int] = 0,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
) -> ModelResult:
    """SIMEX for covariates with known (heteroscedastic) sampling SEs.

    NOT VALIDATED as a default: in docs/audits/inference_validation.md, quadratic
    SIMEX under-corrects attenuation and its 90% intervals covered 0.81 at
    covariate reliability 0.71 (0.29 at reliability 0.38). Prefer
    :func:`regression_calibration` (coverage 0.89, bias removed). Results
    carry a note saying so.

    For each lambda, ``n_sim`` data sets W + sqrt(lambda) * s_i * Z (Z ~ N(0,1))
    are refit; mean coefficients are extrapolated (``quadratic`` or
    ``linear`` in lambda, through lambda = 0, the naive fit) to lambda = -1
    (Cook & Stefanski 1994; heteroscedastic form Devanarayan & Stefanski
    2002). Variance: tau^2(lambda) = mean model variance - between-simulation
    variance, extrapolated to -1 (Stefanski & Cook 1995). Quadratic
    extrapolation is approximate and typically leaves some residual
    attenuation when reliability is low.
    """
    model, spec, data, n_trials, notes, _ = _setup(formula, frame, error_covariates, model, weights, exposure, trials)
    if extrapolation not in ("quadratic", "linear"):
        raise ValueError("extrapolation must be 'quadratic' or 'linear'")
    naive = _fit(formula, data, model, weights, exposure, n_trials)
    terms = naive.params.index
    rng = np.random.default_rng(seed)
    lam = [0.0] + [float(x) for x in lambdas]
    means = [naive.params.to_numpy(float)]
    taus = [naive.bse.to_numpy(float) ** 2]
    failed = 0
    for lv in lambdas:
        bs, vs = [], []
        for _ in range(n_sim):
            d = data.copy()
            for w, s in spec.items():
                d[w] = d[w].to_numpy(float) + math.sqrt(lv) * d[s].to_numpy(float) * rng.standard_normal(len(d))
            try:
                res = _fit(formula, d, model, weights, exposure, n_trials)
                bs.append(res.params.reindex(terms).to_numpy(float))
                vs.append(res.bse.reindex(terms).to_numpy(float) ** 2)
            except Exception:  # noqa: BLE001
                failed += 1
        bs_arr = np.vstack(bs)
        means.append(bs_arr.mean(axis=0))
        taus.append(np.vstack(vs).mean(axis=0) - bs_arr.var(axis=0, ddof=1))
    deg = 2 if extrapolation == "quadratic" else 1
    lam_a = np.asarray(lam)
    M, T = np.vstack(means), np.vstack(taus)
    est = np.array([np.polyval(np.polyfit(lam_a, M[:, j], deg), -1.0) for j in range(M.shape[1])])
    var = np.array([np.polyval(np.polyfit(lam_a, T[:, j], deg), -1.0) for j in range(T.shape[1])])
    if (var <= 0).any():
        notes.append("extrapolated SIMEX variance non-positive for some terms; se_total set to NaN there")
    se_total = pd.Series(np.sqrt(np.where(var > 0, var, np.nan)), index=terms)
    diagnostics = {"reliability_screen": _screen_summary(data, formula, cv_threshold),
                   "simex_path": pd.DataFrame(M, columns=list(terms)).assign(**{"lambda": lam_a}),
                   "failed_simulations": failed}
    notes = notes + [f"SIMEX ({extrapolation} extrapolation, lambdas {lam}, {n_sim} simulations each) for "
                     f"{list(spec)}", FH_POINTER,
                     "SIMEX is not validated for confirmatory use: simulated 90% interval coverage was 0.81 "
                     "(docs/audits/inference_validation.md); prefer regression_calibration"]
    return _corrected_result(formula, model, naive, pd.Series(est, index=terms), se_total, confidence_level,
                             "simex", notes, diagnostics, int(naive.nobs))
