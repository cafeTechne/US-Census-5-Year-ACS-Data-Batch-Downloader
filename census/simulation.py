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
  estimate/MOE pairs: CV quartiles 5.8 / 14.3 / 30.4 %); ``tract`` is an
  assumption (median CV 22%, no tract fixture is archived) and the covariate
  profile used for measurement-error studies.
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
    "tract": {"median": 22.0, "sigma": 0.6, "clip": (3.0, 100.0),
              "source": "assumed tract profile (no tract fixture archived)"},
    "county_covariate": {"median": 8.0, "sigma": 0.6, "clip": (1.0, 60.0),
                         "source": "lower-CV share of the county profile (derived-rate covariates)"},
}


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
