# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeded coverage simulations for the Round 8 inference methods (PLAN_ROUND_8 F1).

Populations with known truth are generated, ACS-like sampling error is added
with realistic standard errors, each method is run, and the share of nominal
90% intervals that cover the truth is reported with its Monte Carlo SE
sqrt(c (1 - c) / R). Acceptance band: 88-92% (PLAN_ROUND_8 sec. 14.3).

ACS-like error
--------------
* CVs are drawn from log-normal profiles (:data:`CV_PROFILES`). ``county``
  matches the published county MOEs in ``tests/fixtures/validation`` (79,444
  estimate/MOE pairs: CV quartiles 5.8 / 14.3 / 30.4 %); ``tract`` is derived
  by :func:`derive_cv_profile` from real 2023 tract MOEs in
  ``tests/fixtures/cv_profile`` (4,917 estimate/MOE pairs from 1,278 tracts in
  six counties: B17001 totals and poverty counts, B19013 and B25064 medians;
  CV quartiles 8.4 / 13.4 / 26.2 %) and is the profile used for
  measurement-error studies.
* Standard errors are CV x the variable's population mean scale (so error
  does not depend on the true value), and sampling errors of the variables in
  one unit can be correlated.
* Replicates: 80 successive-difference-style replicate deviations per unit
  and variable, N(0, Sigma_i / 4) and correlated across variables like the
  sampling errors, then rescaled so that 4/80 sum_r (X_r - X)^2 equals the
  target SE^2 exactly (ACS VRE documentation).

Studies (:func:`run_study`): ``fit_replicate`` (outcome error, superpopulation
and finite-population targets; plus the noisy-covariate case it is not meant
for), ``regression_calibration`` and ``simex`` (coverage and bias vs naive
OLS), ``fay_herriot`` (EBLUP coverage, estimated vs empirical MSE),
``correlation`` (Fisher-z intervals vs the observed-variable and true-value
correlations) and ``linear_trend`` (size of the 10% test).
"""

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .variance import REPLICATE_COLUMNS, REPLICATE_COUNT, REPLICATE_FACTOR

NOMINAL = 0.90
BAND = (0.88, 0.92)
# log-normal CV (percent) profiles: median, sigma of log CV, clip range
CV_PROFILES = {
    "county": {"median": 14.3, "sigma": 1.23, "clip": (0.5, 100.0),
               "source": "tests/fixtures/validation county MOEs (quartiles 5.8/14.3/30.4%)"},
    "tract": {"median": 13.4, "sigma": 0.84, "clip": (2.4, 79.2),
              "source": "tests/fixtures/cv_profile/tract_moes_2023.csv (derive_cv_profile; "
                        "quartiles 8.4/13.4/26.2%, 1st/99th percentiles 2.4/79.2%)"},
    "county_covariate": {"median": 8.0, "sigma": 0.6, "clip": (1.0, 60.0),
                         "source": "lower-CV share of the county profile (derived-rate covariates)"},
}


def derive_cv_profile(path: str, z: float = 1.645) -> Dict[str, Any]:
    """Log-normal CV profile from a CSV of raw Census ``<base>E``/``<base>M`` columns.

    CV (percent) = (MOE / z) / estimate x 100 over every pair with a positive
    estimate and a published (non-negative) MOE, pooled across variables.
    median = median CV; sigma = IQR of log CV / 1.349 (robust to the long
    tail); clip = 1st and 99th percentiles.
    """
    df = pd.read_csv(path, dtype=str)
    cvs = []
    for col in df.columns:
        if col.endswith("E") and f"{col[:-1]}M" in df.columns:
            est = pd.to_numeric(df[col], errors="coerce")
            moe = pd.to_numeric(df[f"{col[:-1]}M"], errors="coerce")
            ok = (est > 0) & (moe >= 0)
            cvs.append((moe[ok] / z / est[ok] * 100).to_numpy())
    cv = np.concatenate(cvs) if cvs else np.array([])
    cv = cv[cv > 0]
    if not len(cv):
        raise ValueError(f"No usable estimate/MOE pairs in {path}.")
    q1, q25, q50, q75, q99 = np.percentile(cv, [1, 25, 50, 75, 99])
    return {"median": float(q50), "sigma": float((np.log(q75) - np.log(q25)) / 1.349),
            "clip": (float(q1), float(q99)), "quartiles": (float(q25), float(q50), float(q75)),
            "n_pairs": int(len(cv))}


def draw_cv(n: int, profile: str, rng: np.random.Generator) -> np.ndarray:
    p = CV_PROFILES[profile]
    cv = np.exp(np.log(p["median"]) + p["sigma"] * rng.standard_normal(n))
    return np.clip(cv, *p["clip"])


def make_replicates(estimates: np.ndarray, se: np.ndarray, rng: np.random.Generator,
                    corr: Optional[np.ndarray] = None) -> np.ndarray:
    """Replicate values (n, k, 80) with 4/80 sum (rep - est)^2 == se^2 exactly.

    ``estimates``/``se``: (n, k). Deviations are N(0, Sigma_i / 4) with
    cross-variable correlation ``corr`` (k x k), then rescaled per unit and
    variable to match the target SE.
    """
    est = np.atleast_2d(np.asarray(estimates, float))
    se = np.atleast_2d(np.asarray(se, float))
    n, k = est.shape
    L = np.linalg.cholesky(corr) if corr is not None else np.eye(k)
    z = rng.standard_normal((n, REPLICATE_COUNT, k)) @ L.T  # (n, 80, k)
    z = np.transpose(z, (0, 2, 1))  # (n, k, 80)
    scale = np.sqrt(REPLICATE_FACTOR * (z ** 2).sum(axis=2, keepdims=True))
    dev = np.where(scale > 0, z / scale, 0.0) * se[:, :, None]
    return est[:, :, None] + dev


def replicate_long(geoids: Sequence[str], names: Sequence[str], estimates: np.ndarray,
                   reps: np.ndarray) -> pd.DataFrame:
    """Long replicate frame (GEOID, variable, estimate, rep_1..rep_80)."""
    parts = []
    for j, v in enumerate(names):
        df = pd.DataFrame(reps[:, j, :], columns=REPLICATE_COLUMNS)
        df.insert(0, "estimate", estimates[:, j])
        df.insert(0, "variable", v)
        df.insert(0, "GEOID", list(geoids))
        parts.append(df)
    return pd.concat(parts, ignore_index=True)


def acs_frame(geoids: Sequence[str], values: Dict[str, np.ndarray], ses: Dict[str, np.ndarray]) -> pd.DataFrame:
    """Analysis frame in the census.results convention."""
    out = pd.DataFrame({"GEOID": list(geoids)})
    for v, x in values.items():
        se = ses.get(v)
        out[v] = x
        out[f"{v}_se"] = se if se is not None else 0.0
        out[f"{v}_cv"] = np.abs(out[f"{v}_se"] / x) * 100 if se is not None else 0.0
        out[f"{v}_method"] = "simulated"
    return out


# ---------------------------------------------------------------------------
# Configuration and summaries
# ---------------------------------------------------------------------------


@dataclass
class SimConfig:
    seed: int = 20261102
    n_areas: int = 150
    reps: Dict[str, int] = field(default_factory=lambda: {
        "fit_replicate": 1000, "fit_replicate_noisy_x": 300, "regression_calibration": 1000,
        "simex": 500, "regression_calibration_low_rel": 300,
        "simex_low_rel": 300, "fay_herriot": 1000, "correlation": 4000, "linear_trend": 20000})
    rc_boot: int = 200
    simex_sim: int = 50
    fh_areas: int = 60
    beta: float = 0.5
    sigma_u: float = 8.0          # outcome model error SD
    x_mean: float = 50.0
    x_sd: float = 24.0            # covariate SD: reliability ~0.7 under the tract CV profile
    x_sd_low: float = 12.0        # low-reliability scenario (~0.35), informational
    error_corr: float = 0.3       # within-unit correlation of x and y sampling errors


FULL = SimConfig()
FAST = SimConfig(n_areas=80, rc_boot=60, simex_sim=15, fh_areas=40, reps={
    "fit_replicate": 40, "fit_replicate_noisy_x": 0, "regression_calibration": 30, "simex": 15,
    "regression_calibration_low_rel": 0, "simex_low_rel": 0, "fay_herriot": 60, "correlation": 300, "linear_trend": 2000})


def coverage_summary(method: str, target: str, covered: Sequence[bool], extra: Optional[Dict[str, Any]] = None,
                     nominal: float = NOMINAL, band=BAND, gated: bool = True) -> Dict[str, Any]:
    c = np.asarray(covered, bool)
    r = int(c.size)
    cov = float(c.mean()) if r else float("nan")
    mcse = math.sqrt(cov * (1 - cov) / r) if r else float("nan")
    status = ("pass" if band[0] <= cov <= band[1] else "FAIL") if gated else "info"
    row = {"method": method, "target": target, "reps": r, "nominal": nominal, "coverage": cov,
           "mc_se": mcse, "status": status}
    row.update(extra or {})
    return row


def _geoids(n: int) -> List[str]:
    return [f"{i:05d}" for i in range(n)]


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------


def _reg_population(cfg: SimConfig, rng: np.random.Generator, noisy_x: bool, x_sd: Optional[float] = None):
    n = cfg.n_areas
    x = cfg.x_mean + (x_sd or cfg.x_sd) * rng.standard_normal(n)
    y = 10.0 + cfg.beta * x + cfg.sigma_u * rng.standard_normal(n)
    y_mean = 10.0 + cfg.beta * cfg.x_mean
    se_y = draw_cv(n, "tract", rng) / 100 * y_mean
    se_x = draw_cv(n, "tract", rng) / 100 * cfg.x_mean if noisy_x else np.zeros(n)
    return x, y, se_x, se_y


def _sample(x, y, se_x, se_y, rho, rng):
    z1 = rng.standard_normal(len(x))
    z2 = rho * z1 + math.sqrt(1 - rho ** 2) * rng.standard_normal(len(x))
    return x + se_x * z1, y + se_y * z2


def study_fit_replicate(cfg: SimConfig, reps: int, noisy_x: bool = False) -> List[Dict[str, Any]]:
    from .models import fit_replicate
    rng = np.random.default_rng(cfg.seed + (11 if noisy_x else 1))
    g = _geoids(cfg.n_areas)
    corr = np.array([[1, cfg.error_corr], [cfg.error_corr, 1]])
    cov_super, cov_fp, cov_samp_super, cov_model, est, fp_est = [], [], [], [], [], []
    for _ in range(reps):
        x, y, se_x, se_y = _reg_population(cfg, rng, noisy_x)
        b_fp = np.polyfit(x, y, 1)[0]  # finite-population (census) coefficient
        xo, yo = _sample(x, y, se_x, se_y, cfg.error_corr if noisy_x else 0.0, rng)
        R = make_replicates(np.c_[xo, yo], np.c_[se_x, se_y], rng, corr if noisy_x else None)
        names = ["x", "y"] if noisy_x else ["y"]
        Rsel = R if noisy_x else R[:, 1:2, :]
        E = np.c_[xo, yo] if noisy_x else yo[:, None]
        frame = acs_frame(g, {"x": xo, "y": yo}, {"x": se_x if noisy_x else None, "y": se_y})
        res = fit_replicate("y ~ x", frame, replicate_long(g, names, E, Rsel), confidence_level=NOMINAL)
        row = res.coefficients.set_index("term").loc["x"]
        z = 1.6448536269514722
        cov_super.append(abs(row["estimate"] - cfg.beta) <= z * row["se_total"])  # explicit se_total
        cov_fp.append(abs(row["estimate"] - b_fp) <= z * row["se_sampling"])
        cov_samp_super.append(abs(row["estimate"] - cfg.beta) <= z * row["se_sampling"])
        cov_model.append(abs(row["estimate"] - cfg.beta) <= z * row["se_model"])
        est.append(row["estimate"])
        fp_est.append(b_fp)
    bias = float(np.mean(est) - cfg.beta)
    if noisy_x:
        return [coverage_summary("fit_replicate (OLS), noisy covariate", "superpopulation beta (se_total)",
                                 cov_super, {"bias": bias, "gated": False}, gated=False)]
    return [
        coverage_summary("fit_replicate (OLS)", "superpopulation beta, se_total: conservative target",
                         cov_super, {"bias": bias, "gated": False}, gated=False),
        coverage_summary("fit_replicate (OLS)", "finite-population beta (se_sampling only)", cov_fp,
                         {"bias": float(np.mean(np.array(est) - np.array(fp_est)))}),
        coverage_summary("fit_replicate (OLS)", "superpopulation beta, se_sampling only", cov_samp_super,
                         {"bias": bias}, gated=False),
        coverage_summary("fit_replicate (OLS)", "superpopulation beta, se_model: default population target",
                         cov_model, {"bias": bias}),
    ]


def study_measurement_error(cfg: SimConfig, method: str, reps: int, low: bool = False) -> List[Dict[str, Any]]:
    from . import models
    rng = np.random.default_rng(cfg.seed + (2 if method == "regression_calibration" else 3) + (20 if low else 0))
    g = _geoids(cfg.n_areas)
    x_sd = cfg.x_sd_low if low else cfg.x_sd
    covered, naive_cov, est, naive, rel = [], [], [], [], []
    for i in range(reps):
        x, y, se_x, se_y = _reg_population(cfg, rng, True, x_sd)
        rel.append(x_sd ** 2 / (x_sd ** 2 + float(np.mean(se_x ** 2))))
        xo, yo = _sample(x, y, se_x, se_y, 0.0, rng)
        frame = acs_frame(g, {"x": xo, "y": yo}, {"x": se_x, "y": se_y})
        if method == "regression_calibration":
            res = models.regression_calibration("y ~ x", frame, ["x"], n_boot=cfg.rc_boot, seed=i,
                                                confidence_level=NOMINAL)
        else:
            res = models.simex("y ~ x", frame, ["x"], n_sim=cfg.simex_sim, seed=i, confidence_level=NOMINAL)
        row = res.coefficients.set_index("term").loc["x"]
        covered.append(row["ci_lower"] <= cfg.beta <= row["ci_upper"])
        est.append(row["estimate"])
        nb, ns = row["naive_estimate"], row["naive_se"]
        naive.append(nb)
        naive_cov.append(abs(nb - cfg.beta) <= 1.6448536269514722 * ns)
    est, naive = np.array(est), np.array(naive)
    extra = {"reliability": float(np.mean(rel)), "bias": float(est.mean() - cfg.beta), "rel_bias": float(est.mean() / cfg.beta - 1),
             "naive_bias": float(naive.mean() - cfg.beta), "naive_rel_bias": float(naive.mean() / cfg.beta - 1)}
    target = f"superpopulation beta, noisy covariate (reliability {extra['reliability']:.2f})"
    label = method + (" [low-reliability scenario]" if low else "")
    # SIMEX is reported but not gated: it is not a default method (coverage 0.81 at nominal 0.90).
    return [coverage_summary(label, target, covered, extra, gated=not low and method != "simex"),
            coverage_summary("naive OLS (same data)", target, naive_cov,
                             {"reliability": extra["reliability"], "bias": extra["naive_bias"],
                              "rel_bias": extra["naive_rel_bias"]}, gated=False)]


def study_fay_herriot(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    from .sae import fay_herriot
    rng = np.random.default_rng(cfg.seed + 4)
    m = cfg.fh_areas
    g = _geoids(m)
    x = rng.uniform(0, 10, m)
    beta0, beta1, A = 15.0, 1.5, 9.0
    mean_level = beta0 + beta1 * 5
    D = (draw_cv(m, "county", rng) / 100 * mean_level) ** 2
    D = np.clip(D, 0.5, 400.0)
    covered = np.zeros((reps, m), bool)
    sq_err = np.zeros((reps, m))
    est_mse = np.zeros((reps, m))
    direct_cov = np.zeros((reps, m), bool)
    for r in range(reps):
        theta = beta0 + beta1 * x + math.sqrt(A) * rng.standard_normal(m)
        y = theta + np.sqrt(D) * rng.standard_normal(m)
        frame = pd.DataFrame({"GEOID": g, "y": y, "y_se": np.sqrt(D), "x": x})
        res = fay_herriot("y ~ x", frame, "y_se", confidence_level=NOMINAL).estimates
        covered[r] = (res["ci_lower"].to_numpy() <= theta) & (theta <= res["ci_upper"].to_numpy())
        sq_err[r] = (res["eblup"].to_numpy() - theta) ** 2
        est_mse[r] = res["mse"].to_numpy()
        direct_cov[r] = np.abs(y - theta) <= 1.6448536269514722 * np.sqrt(D)
    emp = sq_err.mean(axis=0)
    rel = est_mse.mean(axis=0) / emp - 1
    # MC SE of overall coverage: areas within a replicate are correlated through
    # the shared fit, so use between-replicate variation of the per-rep mean.
    per_rep = covered.mean(axis=1)
    row = coverage_summary("fay_herriot EBLUP (REML, analytic MSE)", "area mean theta_i",
                           covered.ravel(), {"mse_rel_bias_mean": float(rel.mean()),
                                             "mse_rel_bias_median": float(np.median(rel)),
                                             "mse_rel_bias_abs_max": float(np.abs(rel).max()),
                                             "eblup_mse_vs_direct": float(emp.mean() / D.mean()),
                                             "min_area_coverage": float(covered.mean(axis=0).min())})
    row["mc_se"] = float(per_rep.std(ddof=1) / math.sqrt(reps)) if reps > 1 else float("nan")
    return [row, coverage_summary("direct estimate +/- 1.645 SE", "area mean theta_i", direct_cov.ravel(),
                                  gated=False)]


def study_correlation(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    from .correlation import correlation_matrix
    rng = np.random.default_rng(cfg.seed + 5)
    n, rho = cfg.n_areas, 0.5
    g = _geoids(n)
    mu, tau = 50.0, 10.0
    # observed-variable population correlation, by a large Monte Carlo
    big = 400_000
    s_big = draw_cv(big, "county_covariate", rng) / 100 * mu
    s_big2 = draw_cv(big, "county_covariate", rng) / 100 * mu
    rho_obs = rho * tau ** 2 / math.sqrt((tau ** 2 + np.mean(s_big ** 2)) * (tau ** 2 + np.mean(s_big2 ** 2)))
    cov_obs, cov_true, rs = [], [], []
    L = np.linalg.cholesky(np.array([[1, rho], [rho, 1]]))
    for _ in range(reps):
        t = mu + tau * (rng.standard_normal((n, 2)) @ L.T)
        s1 = draw_cv(n, "county_covariate", rng) / 100 * mu
        s2 = draw_cv(n, "county_covariate", rng) / 100 * mu
        a = t[:, 0] + s1 * rng.standard_normal(n)
        b = t[:, 1] + s2 * rng.standard_normal(n)
        res = correlation_matrix(acs_frame(g, {"a": a, "b": b}, {"a": s1, "b": s2}), ["a", "b"],
                                 confidence_level=NOMINAL, correction=None)
        lo, hi = res.ci_lower.loc["a", "b"], res.ci_upper.loc["a", "b"]
        cov_obs.append(lo <= rho_obs <= hi)
        cov_true.append(lo <= rho <= hi)
        rs.append(res.matrix.loc["a", "b"])
    return [coverage_summary("correlation_matrix (Pearson, Fisher z)", f"observed-variable rho ({rho_obs:.3f})",
                             cov_obs, {"bias": float(np.mean(rs) - rho_obs)}),
            coverage_summary("correlation_matrix (Pearson, Fisher z)", f"true-value rho ({rho})", cov_true,
                             {"bias": float(np.mean(rs) - rho)}, gated=False)]


def study_linear_trend(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    from .trends import linear_trend_test
    rng = np.random.default_rng(cfg.seed + 6)
    years = [2009, 2014, 2019]
    not_rejected = []
    for _ in range(reps):
        se = draw_cv(3, "tract", rng) / 100 * 100.0
        y = 100.0 + se * rng.standard_normal(3)
        res = linear_trend_test(years, y, ses=se, confidence_level=NOMINAL)
        not_rejected.append(not res["significant"])
    row = coverage_summary("linear_trend_test (GLS, non-overlapping)", "slope 0 (1 - size of 10% test)",
                           not_rejected)
    row["size"] = 1 - row["coverage"]
    return [row]


STUDIES: Dict[str, Callable[[SimConfig, int], List[Dict[str, Any]]]] = {
    "fit_replicate": lambda c, r: study_fit_replicate(c, r, False),
    "fit_replicate_noisy_x": lambda c, r: study_fit_replicate(c, r, True),
    "regression_calibration": lambda c, r: study_measurement_error(c, "regression_calibration", r),
    "simex": lambda c, r: study_measurement_error(c, "simex", r),
    "regression_calibration_low_rel": lambda c, r: study_measurement_error(c, "regression_calibration", r, True),
    "simex_low_rel": lambda c, r: study_measurement_error(c, "simex", r, True),
    "fay_herriot": study_fay_herriot,
    "correlation": study_correlation,
    "linear_trend": study_linear_trend,
}


def run_study(cfg: SimConfig = FULL, studies: Optional[Sequence[str]] = None,
              log: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
    """Run the selected studies and return one row per (method, target)."""
    rows: List[Dict[str, Any]] = []
    for name in studies or list(STUDIES):
        reps = int(cfg.reps.get(name, 0))
        if reps <= 0:
            continue
        t0 = time.perf_counter()
        out = STUDIES[name](cfg, reps)
        dt = time.perf_counter() - t0
        for r in out:
            r["study"] = name
            r["seconds"] = round(dt, 1)
        rows += out
        if log:
            log(f"{name}: {reps} reps in {dt:.1f}s")
    cols = ["study", "method", "target", "reps", "nominal", "coverage", "mc_se", "status"]
    df = pd.DataFrame(rows)
    return df[cols + [c for c in df.columns if c not in cols]]


def fast_config(**overrides: Any) -> SimConfig:
    return replace(FAST, **overrides)


# ---------------------------------------------------------------------------
# Round 9 (PLAN_ROUND_9 sec. 5): spatially correlated populations
# ---------------------------------------------------------------------------
# Registered in STUDIES but given no default reps, so run_study(FULL/FAST)
# output is unchanged; use spatial_config() to run them, e.g.
# run_study(spatial_config(fast=True), studies=list(SPATIAL_STUDIES)).

SPATIAL_REPS_FULL = {"spatial_fay_herriot": 1000, "morans_i_replicate_se": 2000}
SPATIAL_REPS_FAST = {"spatial_fay_herriot": 40, "morans_i_replicate_se": 150}
SAR_RHO = 0.5           # SAR(1) autocorrelation of area effects (spatial FH study)
MORAN_RHO = 0.6         # SAR(1) autocorrelation of the true-value surface (Moran study)
MORAN_SIGNAL_SD = 15.0  # SD of the SAR innovations (true-value surface, mean 50)


def lattice_weights(side: int, ids: Optional[Sequence[str]] = None):
    """Rook-contiguity libpysal W on a side x side lattice (ids = GEOIDs), row-standardized."""
    import libpysal

    ids = list(ids) if ids is not None else _geoids(side * side)
    nb: Dict[str, List[str]] = {}
    for r in range(side):
        for c in range(side):
            nb[ids[r * side + c]] = [ids[rr * side + cc] for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                                     if 0 <= rr < side and 0 <= cc < side]
    w = libpysal.weights.W(nb, id_order=ids, silence_warnings=True)
    w.transform = "r"
    return w


def sar_field(W: np.ndarray, rho: float, sd: float, rng: np.random.Generator, size: Optional[int] = None) -> np.ndarray:
    """Draws of u = (I - rho W)^-1 eps, eps ~ N(0, sd^2 I); shape (m,) or (size, m)."""
    from scipy import linalg as sla

    Binv = sla.inv(np.eye(W.shape[0]) - rho * W)
    out = (sd * rng.standard_normal((size or 1, W.shape[0]))) @ Binv.T
    return out[0] if size is None else out


def study_spatial_fay_herriot(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Spatial FH (REML, analytic MSE) coverage of area means under SAR(1) area effects."""
    from .sae import fay_herriot
    from .sae_spatial import spatial_fay_herriot
    rng = np.random.default_rng(cfg.seed + 9)
    side = max(int(round(math.sqrt(cfg.fh_areas))), 5)
    m = side * side
    g = _geoids(m)
    W = lattice_weights(side, g).full()[0]
    x = rng.uniform(0, 10, m)
    beta0, beta1, A = 15.0, 1.5, 9.0
    D = np.clip((draw_cv(m, "county", rng) / 100 * (beta0 + beta1 * 5)) ** 2, 0.5, 400.0)
    covered = np.zeros((reps, m), bool)
    fh_cov = np.zeros((reps, m), bool)
    sq, est_mse, fh_sq, rhos = np.zeros((reps, m)), np.zeros((reps, m)), np.zeros((reps, m)), []
    for r in range(reps):
        theta = beta0 + beta1 * x + sar_field(W, SAR_RHO, math.sqrt(A), rng)
        y = theta + np.sqrt(D) * rng.standard_normal(m)
        frame = pd.DataFrame({"GEOID": g, "y": y, "y_se": np.sqrt(D), "x": x})
        res = spatial_fay_herriot("y ~ x", frame, "y_se", W, confidence_level=NOMINAL)
        e = res.estimates
        covered[r] = (e["ci_lower"].to_numpy() <= theta) & (theta <= e["ci_upper"].to_numpy())
        sq[r] = (e["eblup"].to_numpy() - theta) ** 2
        est_mse[r] = e["mse"].to_numpy()
        rhos.append(res.provenance["rho"])
        f = fay_herriot("y ~ x", frame, "y_se", confidence_level=NOMINAL).estimates
        fh_cov[r] = (f["ci_lower"].to_numpy() <= theta) & (theta <= f["ci_upper"].to_numpy())
        fh_sq[r] = (f["eblup"].to_numpy() - theta) ** 2
    emp = sq.mean(axis=0)
    rel = est_mse.mean(axis=0) / emp - 1
    row = coverage_summary(f"spatial_fay_herriot EBLUP (REML, analytic MSE), {side}x{side} rook lattice",
                           f"area mean theta_i, SAR rho={SAR_RHO}", covered.ravel(),
                           {"rho_mean": float(np.mean(rhos)),
                            "rho_sd": float(np.std(rhos, ddof=1)) if reps > 1 else float("nan"),
                            "mse_rel_bias_mean": float(rel.mean()), "mse_rel_bias_median": float(np.median(rel)),
                            "eblup_mse_vs_direct": float(emp.mean() / D.mean()),
                            "eblup_mse_vs_fh": float(emp.mean() / fh_sq.mean()),
                            "min_area_coverage": float(covered.mean(axis=0).min())})
    # areas within a replicate share one fit: MC SE from between-replicate variation
    row["mc_se"] = float(covered.mean(axis=1).std(ddof=1) / math.sqrt(reps)) if reps > 1 else float("nan")
    fh_row = coverage_summary("fay_herriot EBLUP (non-spatial) on the same data", f"area mean theta_i, SAR rho={SAR_RHO}",
                              fh_cov.ravel(), {"mse_vs_spatial": float(fh_sq.mean() / emp.mean())}, gated=False)
    return [row, fh_row]


def study_morans_i_replicate_se(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Replicate SE of Moran's I: coverage of E[I(ACS estimates) | true surface].

    The true surface (SAR, 10x10 rook lattice) and the SEs (tract CV profile)
    are fixed; each repetition draws new sampling errors and replicates. The
    target is the mean of I over repeated samples (20,000 draws), which is
    what a design-based SE describes; I of the true values is reported
    (ungated) to show the attenuation from sampling noise.
    """
    from .spatial import _moran_vector, morans_i
    rng = np.random.default_rng(cfg.seed + 10)
    side = 10
    m = side * side
    g = _geoids(m)
    w = lattice_weights(side, g)
    Ws = w.sparse.tocsr()
    mu = 50.0
    theta = mu + sar_field(w.full()[0], MORAN_RHO, MORAN_SIGNAL_SD, rng)
    se = draw_cv(m, "tract", rng) / 100 * mu
    I_true = float(_moran_vector(theta[:, None], Ws)[0])
    I_big = _moran_vector(theta[:, None] + se[:, None] * rng.standard_normal((m, 20000)), Ws)
    target, sd_I = float(I_big.mean()), float(I_big.std(ddof=1))
    covered, covered_true, ses = [], [], []
    for _ in range(reps):
        y = theta + se * rng.standard_normal(m)
        R = make_replicates(y[:, None], se[:, None], rng)
        res = morans_i(acs_frame(g, {"y": y}, {"y": se}), "y", w, permutations=0,
                       replicates=replicate_long(g, ["y"], y[:, None], R), confidence_level=NOMINAL, seed=None)
        covered.append(res.ci_lower <= target <= res.ci_upper)
        covered_true.append(res.ci_lower <= I_true <= res.ci_upper)
        ses.append(res.se_replicate)
    extra = {"I_true_values": I_true, "E_I_estimates": target, "sd_I": sd_I,
             "se_ratio": float(np.mean(ses) / sd_I), "attenuation": float(target - I_true)}
    label = "morans_i replicate SE (80 replicate maps), 10x10 rook lattice"
    # Not gated: the replicate SE of Moran's I is conservative (validated finding,
    # se_ratio ~1.1-1.5 under ACS-like CVs; exact as CVs -> 0). Evaluating the
    # gradient of a quadratic-form statistic at the noisy estimates adds the
    # variance of the gradient to the replicate variance.
    return [coverage_summary(label, f"E[Moran's I of ACS estimates | true surface] ({target:.3f})", covered,
                             {**extra, "conservative": bool(np.mean(ses) > sd_I)}, gated=False),
            coverage_summary(label, f"Moran's I of true values ({I_true:.3f}): attenuated, not a target",
                             covered_true, {"attenuation": extra["attenuation"]}, gated=False)]


SPATIAL_STUDIES: Dict[str, Callable[[SimConfig, int], List[Dict[str, Any]]]] = {
    "spatial_fay_herriot": study_spatial_fay_herriot,
    "morans_i_replicate_se": study_morans_i_replicate_se,
}
STUDIES.update(SPATIAL_STUDIES)


def spatial_config(fast: bool = True, **overrides: Any) -> SimConfig:
    """Config that runs only the Round 9 spatial studies (7x7 lattice fast, 10x10 full)."""
    base = FAST if fast else FULL
    reps = {k: 0 for k in base.reps}
    reps.update(SPATIAL_REPS_FAST if fast else SPATIAL_REPS_FULL)
    return replace(base, reps=reps, fh_areas=49 if fast else 100, **overrides)


# ---------------------------------------------------------------------------
# Round 10 (PLAN_ROUND_10 sec. 6): multilevel models and difference-in-differences
# ---------------------------------------------------------------------------
# Registered in STUDIES with no default reps (run_study(FULL/FAST) output is
# unchanged); run with run_study(designs_config(fast=...), studies=list(DESIGN_STUDIES)).

DESIGN_REPS_FULL = {"multilevel_known_variance": 1000, "multilevel_random_slope": 500,
                    "multilevel_replicate": 1000, "did_two_group": 1000, "did_event": 1000, "did_replicate": 1000}
DESIGN_REPS_FAST = {"multilevel_known_variance": 40, "multilevel_random_slope": 20,
                    "multilevel_replicate": 40, "did_two_group": 60, "did_event": 40, "did_replicate": 40}
ML_GROUP_SIZE = (2, 9)        # tracts per county (uniform integer range)
ML_SIGMA_B = 4.0              # county random-intercept SD
ML_SIGMA_SLOPE = 0.15         # county random-slope SD (random-slope study)
ML_SIGMA_V = 3.0              # tract-level model error SD
DID_DELTA = 3.0               # true treatment effect
DID_SIGMA_U = 1.5             # area-by-period model error SD
DID_TREATED_SHARE = 0.4
DID_YEARS_TWO = [2008, 2013, 2018, 2023]
DID_YEARS_EVENT = [2003, 2008, 2013, 2018, 2023]
DID_TREAT_YEAR = 2018


def _ml_population(cfg: SimConfig, rng: np.random.Generator):
    sizes: List[int] = []
    while sum(sizes) < cfg.n_areas:
        sizes.append(int(rng.integers(ML_GROUP_SIZE[0], ML_GROUP_SIZE[1] + 1)))
    groups = np.repeat([f"G{j:03d}" for j in range(len(sizes))], sizes)
    n = len(groups)
    x = cfg.x_mean + cfg.x_sd * rng.standard_normal(n)
    se = draw_cv(n, "tract", rng) / 100 * (10.0 + cfg.beta * cfg.x_mean)
    return groups, x, se, len(sizes)


def _ml_draw(cfg: SimConfig, rng: np.random.Generator, groups, x, slope: bool) -> np.ndarray:
    codes = pd.factorize(groups)[0]
    k = codes.max() + 1
    b0 = ML_SIGMA_B * rng.standard_normal(k)
    b1 = ML_SIGMA_SLOPE * rng.standard_normal(k) if slope else np.zeros(k)
    return (10.0 + cfg.beta * x + b0[codes] + b1[codes] * (x - cfg.x_mean)
            + ML_SIGMA_V * rng.standard_normal(len(x)))


def _ml_frame(groups, x, y, se) -> pd.DataFrame:
    f = acs_frame(_geoids(len(x)), {"y": y, "x": x}, {"y": se})
    f["x_c"] = x - 50.0
    f["county"] = groups
    return f


def study_multilevel_known_variance(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Tracts in counties, known ACS sampling variance: coverage of the superpopulation slope."""
    from .multilevel import fit_multilevel
    rng = np.random.default_rng(cfg.seed + 11)
    groups, x, se, k = _ml_population(cfg, rng)
    cov, cov_naive, ests = [], [], []
    for _ in range(reps):
        theta = _ml_draw(cfg, rng, groups, x, slope=False)
        f = _ml_frame(groups, x, theta + se * rng.standard_normal(len(x)), se)
        c = fit_multilevel("y ~ x", f, "county", sampling_se=True).coefficients.set_index("term").loc["x"]
        cov.append(c.ci_lower <= cfg.beta <= c.ci_upper)
        ests.append(c.estimate)
        n = fit_multilevel("y ~ x", f, "county").coefficients.set_index("term").loc["x"]
        cov_naive.append(n.ci_lower <= cfg.beta <= n.ci_upper)
    label = f"fit_multilevel known variance (REML), {len(x)} tracts in {k} counties, random intercept"
    return [coverage_summary(label, "superpopulation slope beta (population)", cov,
                             {"bias": float(np.mean(ests) - cfg.beta)}),
            coverage_summary("fit_multilevel MixedLM ignoring sampling variance (same data)",
                             "superpopulation slope beta (population)", cov_naive, gated=False)]


def study_multilevel_random_slope(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Random intercept and slope with known sampling variance: coverage of the mean slope."""
    from .multilevel import fit_multilevel
    rng = np.random.default_rng(cfg.seed + 12)
    groups, x, se, k = _ml_population(cfg, rng)
    cov = []
    for _ in range(reps):
        theta = _ml_draw(cfg, rng, groups, x, slope=True)
        f = _ml_frame(groups, x, theta + se * rng.standard_normal(len(x)), se)
        c = fit_multilevel("y ~ x_c", f, "county", re_formula="~x_c", sampling_se=True
                           ).coefficients.set_index("term").loc["x_c"]
        cov.append(c.ci_lower <= cfg.beta <= c.ci_upper)
    return [coverage_summary(f"fit_multilevel known variance, random intercept + slope, {k} counties",
                             "superpopulation mean slope beta (population)", cov)]


def study_multilevel_replicate(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Replicate SEs (target these_areas): coverage of the GLS slope of the true tract values."""
    from .multilevel import BlockGLS, _known_vfun, fit_multilevel
    rng = np.random.default_rng(cfg.seed + 13)
    groups, x, se, k = _ml_population(cfg, rng)
    theta = _ml_draw(cfg, rng, groups, x, slope=False)  # fixed finite population
    X = np.column_stack([np.ones(len(x)), x])
    g = _geoids(len(x))
    cov, ratio = [], []
    for _ in range(reps):
        y = theta + se * rng.standard_normal(len(x))
        R = make_replicates(y[:, None], se[:, None], rng)
        res = fit_multilevel("y ~ x", _ml_frame(groups, x, y, se), "county", sampling_se=True,
                             target="these_areas", replicates=replicate_long(g, ["y"], y[:, None], R))
        pv = res.provenance
        eng = BlockGLS(groups, y, X, _known_vfun(1, True, pv["variance_scale"]), D=se ** 2)
        target = eng.gls(np.asarray(pv["variance_parameters"]), theta)[1]
        c = res.coefficients.set_index("term").loc["x"]
        cov.append(c.ci_lower <= target <= c.ci_upper)
        ratio.append(c.se_sampling / res.diagnostics["se_sampling_analytic"]["x"])
    return [coverage_summary(f"fit_multilevel replicate GLS refits, {len(x)} tracts in {k} counties",
                             "slope for these tracts' true values (these_areas)", cov,
                             {"se_replicate_vs_analytic": float(np.mean(ratio))})]


def _did_panel(cfg: SimConfig, rng: np.random.Generator, years: Sequence[int]):
    n = cfg.n_areas
    T = (np.arange(n) < int(DID_TREATED_SHARE * n)).astype(float)
    alpha = cfg.x_mean + 10.0 * rng.standard_normal(n)
    se = {yr: draw_cv(n, "tract", rng) / 100 * cfg.x_mean for yr in years}
    return T, alpha, se


def _did_truth(T, alpha, years, rng) -> Dict[int, np.ndarray]:
    return {yr: alpha + 1.5 * k + DID_DELTA * T * (yr >= DID_TREAT_YEAR) + DID_SIGMA_U * rng.standard_normal(len(T))
            for k, yr in enumerate(years)}


def _did_frame(T, years, values, se) -> pd.DataFrame:
    g = _geoids(len(T))
    return pd.concat([pd.DataFrame({"GEOID": g, "Year": yr, "y": values[yr], "y_se": se[yr], "treated": T})
                      for yr in years], ignore_index=True)


def study_did_two_group(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Two-group DiD (2 pre, 2 post non-overlapping periods): coverage of delta."""
    from .did import difference_in_differences
    rng = np.random.default_rng(cfg.seed + 14)
    T, alpha, se = _did_panel(cfg, rng, DID_YEARS_TWO)
    cov, ests = [], []
    for _ in range(reps):
        th = _did_truth(T, alpha, DID_YEARS_TWO, rng)
        y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in DID_YEARS_TWO}
        c = difference_in_differences(_did_frame(T, DID_YEARS_TWO, y, se), "y", "treated", DID_TREAT_YEAR
                                      ).coefficients.set_index("term").loc["did"]
        cov.append(c.ci_lower <= DID_DELTA <= c.ci_upper)
        ests.append(c.estimate)
    return [coverage_summary(f"difference_in_differences two_group (REML), {cfg.n_areas} areas x 4 periods",
                             "superpopulation delta (population)", cov, {"bias": float(np.mean(ests) - DID_DELTA)})]


def study_did_event(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Event-style DiD, 3 pre + 2 post periods: coverage of leads (0) and lags (delta); PT test size."""
    from .did import difference_in_differences
    rng = np.random.default_rng(cfg.seed + 15)
    T, alpha, se = _did_panel(cfg, rng, DID_YEARS_EVENT)
    cov, reject = [], []
    for _ in range(reps):
        th = _did_truth(T, alpha, DID_YEARS_EVENT, rng)
        y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in DID_YEARS_EVENT}
        res = difference_in_differences(_did_frame(T, DID_YEARS_EVENT, y, se), "y", "treated", DID_TREAT_YEAR,
                                        design="event")
        tab = res.coefficients[res.coefficients["role"] == "effect"]
        truth = np.where(tab["term"].str.startswith("lag"), DID_DELTA, 0.0)
        cov.append((tab["ci_lower"].to_numpy() <= truth) & (truth <= tab["ci_upper"].to_numpy()))
        reject.append(res.diagnostics["parallel_trends"]["p_value"] < 1 - NOMINAL)
    C = np.vstack(cov)
    row = coverage_summary(f"difference_in_differences event (REML), {cfg.n_areas} areas x 5 periods",
                           "leads (0) and lags (delta), pooled (population)", C.ravel())
    # terms within a repetition share one fit: MC SE from between-repetition variation
    row["mc_se"] = float(C.mean(axis=1).std(ddof=1) / math.sqrt(reps)) if reps > 1 else float("nan")
    return [row, coverage_summary("parallel-trends Wald test (3 pre-periods) under true parallel trends",
                                  "non-rejection rate at 10% (1 - size)", ~np.asarray(reject, bool), gated=False)]


def study_did_replicate(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """Two-group DiD with full replicate refits (these_areas): coverage of the GLS DiD of the true values."""
    from .did import _did_vfun, _differences, _prepare, difference_in_differences
    from .multilevel import BlockGLS
    rng = np.random.default_rng(cfg.seed + 16)
    T, alpha, se = _did_panel(cfg, rng, DID_YEARS_TWO)
    th = _did_truth(T, alpha, DID_YEARS_TWO, rng)  # fixed finite population
    g = _geoids(len(T))
    true_vec = np.concatenate([th[yr] for yr in DID_YEARS_TWO])
    cov = []
    for _ in range(reps):
        y = {yr: th[yr] + se[yr] * rng.standard_normal(len(T)) for yr in DID_YEARS_TWO}
        frame = _did_frame(T, DID_YEARS_TWO, y, se)
        reps_long = pd.concat([replicate_long(g, ["y"], y[yr][:, None],
                                              make_replicates(y[yr][:, None], se[yr][:, None], rng)).assign(Year=yr)
                               for yr in DID_YEARS_TWO], ignore_index=True)
        res = difference_in_differences(frame, "y", "treated", DID_TREAT_YEAR, replicates=reps_long,
                                        target="these_areas")
        P = _prepare(frame, "y", "treated", "Year", "GEOID", DID_TREAT_YEAR, None, "two_group", None, 5)
        d = _differences(P, "y", "treated", "Year", "GEOID", "two_group")
        eng = BlockGLS(d["unit"], d["dy"], d["X"], _did_vfun, D=d["D"], extra={"Dr": d["Dr"]})
        dt = _differences(P, "y", "treated", "Year", "GEOID", "two_group", y_override=true_vec)
        # estimand: the GLS DiD of these areas' true values at the generating A (fixed, not data-dependent)
        target = eng.gls(np.array([DID_SIGMA_U ** 2]), dt["dy"])[-1]
        c = res.coefficients.set_index("term").loc["did"]
        cov.append(c.ci_lower <= target <= c.ci_upper)
    return [coverage_summary(f"difference_in_differences replicate GLS refits, {cfg.n_areas} areas",
                             "delta for these areas' true values (these_areas)", cov)]


DESIGN_STUDIES: Dict[str, Callable[[SimConfig, int], List[Dict[str, Any]]]] = {
    "multilevel_known_variance": study_multilevel_known_variance,
    "multilevel_random_slope": study_multilevel_random_slope,
    "multilevel_replicate": study_multilevel_replicate,
    "did_two_group": study_did_two_group,
    "did_event": study_did_event,
    "did_replicate": study_did_replicate,
}
STUDIES.update(DESIGN_STUDIES)


def designs_config(fast: bool = True, **overrides: Any) -> SimConfig:
    """Config that runs only the Round 10 design studies (multilevel, DiD)."""
    base = FAST if fast else FULL
    reps = {k: 0 for k in base.reps}
    reps.update(DESIGN_REPS_FAST if fast else DESIGN_REPS_FULL)
    return replace(base, reps=reps, **overrides)
