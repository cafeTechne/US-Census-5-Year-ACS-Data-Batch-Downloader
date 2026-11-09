# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 16 workstream A: validation of spatial regression under ACS sampling error (info only).

PLAN_ROUND_16 section 1. Seeded simulations on a 12 x 12 lattice with
known truth; every row has status ``info`` (gating is an owner decision).
Results and conclusions: docs/audits/spatial_regression_validation.md.

Studies (registered in census.simulation.STUDIES, group "spreg_validation";
run with ``scripts/inference_validation.py --studies NAME``):

* ``spatial_error_attenuation`` (A1, independent sampling errors):
  spatial error DGP y* = 10 + beta x + (I - lambda W)^-1 e at lambda 0,
  0.3, 0.6, weights rook / queen / kNN (k = SPREG16_KNN_K); published
  y = y* + d with independent d_i ~ N(0, s_i^2), s_i = CV_i x mean(y*), CVs
  from the tract profile scaled by 0, 0.5, 1, 2. Fitted: the tool default
  (spreg ML_Error, uncorrected) and the engine's existing spatial
  Fay-Herriot likelihood (census.sae_spatial) with
  V = sigma^2 [(I - lambda W)'(I - lambda W)]^-1 + diag(s^2), REML and ML,
  whose rho is the SEM's lambda corrected for independent outcome error.
  These rows call the shipped estimator itself,
  census.spatial.error_model_known_variances (the function behind
  ``spatial_regression(correct_outcome_error=True)``); :func:`me_sem_fit`
  is used only for the dense-Sigma oracle.
* ``spatial_error_attenuation_correlated`` (A1, correlated sampling
  errors): as above at CV x 1, with d ~ N(0, S R(gamma) S), R(gamma) the
  correlation matrix of a SAR(gamma) process on the same W (gamma 0.3, 0.6;
  a stated assumption, not an estimate of the ACS structure). Fitted: the
  uncorrected ML_Error, the spatial Fay-Herriot correction (which assumes
  independence), and an oracle given the true error covariance.
* ``spatial_dgp_recovery`` (A2/A3): SEM, SAR lag, spatial Durbin
  (y = rho W y + X beta + W X theta + e), an omitted spatially correlated
  covariate, and Juhl's (2021) omitted-variable DGP, with and without
  outcome sampling error; the tool's four estimators (lag GMM/ML, error
  ML/GMM) through spatial_regression. Reports recovery of the disturbance
  parameter (lambda), the spatial lag parameter (rho), beta, and the
  direct/indirect/total effects with their coverage (A2: lag-model
  effects from spatial_regression's effects table).
* ``common_factor_lr`` (A4): likelihood-ratio test of the SEM as the
  restricted SDM (theta = -rho beta; SDM by ML lag with W X, SEM by ML
  error) in Juhl's (2021, Political Analysis 29(2):193-211) design: size
  and power with chi-square(1) and Juhl's residual-bootstrap critical
  values. ``common_factor_lr_acs``: the same test under ACS-like outcome
  error.

Every (study, cell) draws from its own generator seeded from cfg.seed and the
cell's position, so results do not depend on run order or process.
Monte Carlo SEs: coverage and rates sqrt(p(1-p)/R); bias sd/sqrt(R); RMSE
by the delta method, SE(MSE)/(2 RMSE).
"""

import math
import warnings
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import linalg as sla, optimize, stats as sps

from .simulation import (FAST, FULL, NOMINAL, SimConfig, _geoids, acs_frame, draw_cv, lattice_weights,
                         sar_inverse)

SPREG16_SIDE = 12
SPREG16_KNN_K = 6
SPREG16_WEIGHTS = ("rook", "queen", "knn")
SPREG16_LAMBDAS = (0.0, 0.3, 0.6)
SPREG16_CV_SCALES = (0.0, 0.5, 1.0, 2.0)     # x tract-profile CVs (0 = no sampling error)
SPREG16_GAMMAS = (0.3, 0.6)                  # SAR strength of the sampling-error correlation
SPREG16_BOUNDARY = 0.98                      # |lambda| at or above: boundary estimate
SPREG16_LR_ALPHA = 0.05
SPREG16_LR_BOOT = 100                        # bootstrap draws per test (Juhl 2021 sec. 3.3)
SPREG16_LR_BOOT_REPS = 200                   # repetitions per cell that also run the bootstraps
# Juhl (2021) design: y = x beta + (I - rho W)^-1 (x gamma + v), beta 2, x, v ~ N(0, 1), queen W
SPREG16_JUHL_SIDES = (7, 10, 15, 20)
SPREG16_JUHL_RHOS = (0.0, 0.2, 0.4, 0.6, 0.8)
SPREG16_JUHL_GAMMAS_LR = (0.0, 0.5, 1.0)     # 0: SEM (H0 true); > 0: SDM-like (H0 false)
SPREG16_JUHL_BOOT_GAMMAS = (0.0, 0.5)        # cells that also run the bootstrap
SPREG16_JUHL_BETA = 2.0
SPREG16_ACS_LR_GAMMAS = (0.0, 0.25)          # ACS extension (12 x 12 queen, beta 0.5, x ~ N(50, 24^2))
SPREG16_ACS_LR_RHOS = (0.3, 0.6)
SPREG16_SDM_RHO = 0.4
SPREG16_SDM_THETA = 0.3
SPREG16_OMIT_CORR = 0.5                      # corr(x, z) for the omitted covariate z
SPREG16_OMIT_RHO = 0.7                       # SAR autocorrelation of z
SPREG16_EFFECT_DRAWS = 1000                  # draws for simulated (LeSage & Pace) effect intervals
# spreg's OLS-based RS tests (Koley & Bera 2024): none of them tests SEM vs SDM (the common factor)
SPREG16_RS_TESTS = {"lml": "rho = 0 (lag), assuming theta = 0", "lmwx": "theta = 0 (WX), assuming rho = 0",
                    "lmspdurbin": "rho = 0 and theta = 0 (joint)", "rlmwx": "theta = 0, robust to rho",
                    "rlmdurlag": "rho = 0, robust to theta"}
SPREG16_JUHL_RHO = 0.5                      # Juhl (2021) Eq. 5 DGP in the A3 set ...
SPREG16_JUHL_GAMMA = 0.25                    # ... with gamma / beta = 0.5
SPREG16_REPS_FULL = {"spatial_error_attenuation": 300, "spatial_error_attenuation_correlated": 300,
                     "spatial_dgp_recovery": 400, "common_factor_lr": 1000, "common_factor_lr_acs": 500}
SPREG16_REPS_FAST = {"spatial_error_attenuation": 3, "spatial_error_attenuation_correlated": 3,
                     "spatial_dgp_recovery": 3, "common_factor_lr": 6, "common_factor_lr_acs": 4}


# ---------------------------------------------------------------------------
# Weights and error structures
# ---------------------------------------------------------------------------


def lattice_w(side: int, kind: str, ids: Optional[Sequence[str]] = None):
    """Row-standardized libpysal W on a side x side lattice: rook, queen or kNN.

    kNN uses cell centres with a fixed tiny jitter (seed 0, 1e-6) so ties
    among equidistant cells are broken the same way on every machine.
    """
    import libpysal

    ids = list(ids) if ids is not None else _geoids(side * side)
    if kind == "rook":
        return lattice_weights(side, ids)
    r, c = np.divmod(np.arange(side * side), side)
    if kind == "queen":
        nb = {ids[i]: [ids[j] for j in range(side * side)
                       if j != i and abs(r[i] - r[j]) <= 1 and abs(c[i] - c[j]) <= 1] for i in range(side * side)}
        w = libpysal.weights.W(nb, id_order=ids, silence_warnings=True)
    elif kind == "knn":
        xy = np.column_stack([c, r]).astype(float) + 1e-6 * np.random.default_rng(0).standard_normal((side * side, 2))
        w = libpysal.weights.KNN(xy, k=SPREG16_KNN_K, ids=ids)
    else:
        raise ValueError(f"unknown lattice weights {kind!r}")
    w.transform = "r"
    return w


def sar_correlation(W: np.ndarray, gamma: float) -> np.ndarray:
    """Correlation matrix of a SAR(gamma) process on W: C = [(I - gW)'(I - gW)]^-1 scaled to unit diagonal."""
    B = np.eye(W.shape[0]) - gamma * W
    C = sla.cho_solve(sla.cho_factor(B.T @ B), np.eye(W.shape[0]))
    d = np.sqrt(np.diag(C))
    R = C / np.outer(d, d)
    return 0.5 * (R + R.T)


def neighbour_correlation(R: np.ndarray, W: np.ndarray) -> float:
    """Mean correlation over first-order neighbour pairs (W_ij > 0)."""
    return float(R[W > 0].mean())


# ---------------------------------------------------------------------------
# Estimators used only by the studies
# ---------------------------------------------------------------------------


def me_sem_fit(y: np.ndarray, X: np.ndarray, W: np.ndarray, Sigma: np.ndarray,
               starts: Sequence[float] = (0.0, 0.5), method: str = "REML") -> Dict[str, Any]:
    """REML/ML spatial error model with known measurement-error covariance ``Sigma`` (study-only).

    y = X beta + u + d, u = lambda W u + e, e ~ N(0, A I), d ~ N(0, Sigma),
    so V = A C(lambda) + Sigma with C = [(I - lambda W)'(I - lambda W)]^-1.
    With diagonal Sigma this is exactly the engine's spatial Fay-Herriot
    model (census.sae_spatial: same likelihoods, REML default or ML; Molina
    & Marhuenda 2015 eq. (6), independent sampling errors of known
    variance) and lambda is its rho; a dense Sigma is the study's oracle for
    correlated errors. beta by GLS (profiled); (log A, lambda) by L-BFGS-B
    with analytic scores, lambda in [-0.99, 0.99], from each lambda in
    ``starts``. SEs from the expected information 0.5 tr(M V_k M V_l),
    M = P (REML) or V^-1 (ML), as census.sae_spatial's rho_se, and
    (X'V^-1X)^-1 (beta).

    Its parameterization, bounds, starts and convergence rule differ from the
    shipped estimator (census.spatial.error_model_known_variances), so the A1
    diagonal-Sigma rows do not use it: here it is the dense-Sigma oracle (and
    the null fit of the common-factor bootstrap).
    """
    method = method.upper()
    if method not in ("REML", "ML"):
        raise ValueError("method must be REML or ML")
    n = len(y)
    X = np.column_stack([np.ones(n), X]) if not np.allclose(X[:, 0], 1) else X
    I = np.eye(n)
    lam_b = 0.99

    def state(A, lam):
        B = I - lam * W
        C = sla.cho_solve(sla.cho_factor(B.T @ B), I)
        C = 0.5 * (C + C.T)
        V = A * C + Sigma
        cf = sla.cho_factor(V)
        Vi = sla.cho_solve(cf, I)
        Vi = 0.5 * (Vi + Vi.T)
        ViX = Vi @ X
        Q = sla.inv(X.T @ ViX)
        beta = Q @ (ViX.T @ y)
        r = y - X @ beta
        Vr = Vi @ r
        dC = C @ (W + W.T - 2 * lam * W.T @ W) @ C
        logdet = 2.0 * float(np.sum(np.log(np.diag(cf[0]))))
        M = Vi - ViX @ Q @ ViX.T if method == "REML" else Vi
        return {"C": C, "Vi": Vi, "Q": Q, "beta": beta, "Vr": Vr, "dC": dC, "logdet": logdet, "r": r, "M": M}

    def nll(t):
        A, lam = math.exp(t[0]), float(t[1])
        try:
            s = state(A, lam)
        except (np.linalg.LinAlgError, sla.LinAlgError, ValueError):
            return 1e300, np.zeros(2)
        if method == "REML":
            ll = -0.5 * ((n - X.shape[1]) * math.log(2 * math.pi) + s["logdet"] - np.linalg.slogdet(s["Q"])[1]
                         + s["r"] @ s["Vr"])
        else:
            ll = -0.5 * (n * math.log(2 * math.pi) + s["logdet"] + s["r"] @ s["Vr"])
        g = []
        for Vk in (s["C"], A * s["dC"]):  # P y = V^-1 r (GLS residual r) under REML and ML
            g.append(-0.5 * np.sum(s["M"] * Vk.T) + 0.5 * s["Vr"] @ Vk @ s["Vr"])
        return -ll, -np.array([A * g[0], g[1]])

    var_y = float(np.var(y, ddof=1))
    a0 = math.log(max(var_y - float(np.mean(np.diag(Sigma))), 0.1 * var_y))
    best = None
    for l0 in starts:
        res = optimize.minimize(nll, np.array([a0, l0]), jac=True, method="L-BFGS-B",
                                bounds=[(math.log(var_y) - 25, math.log(var_y) + 5), (-lam_b, lam_b)],
                                options={"ftol": 1e-13, "gtol": 1e-8, "maxiter": 500})
        if best is None or res.fun < best.fun:
            best = res
    A, lam = math.exp(float(best.x[0])), float(best.x[1])
    s = state(A, lam)
    M = [s["M"] @ s["C"], s["M"] @ (A * s["dC"])]
    F = np.array([[0.5 * np.sum(M[a] * M[b].T) for b in range(2)] for a in range(2)])
    try:
        with warnings.catch_warnings():  # singular at the A = 0 boundary: se_lambda is then not meaningful
            warnings.simplefilter("ignore", sla.LinAlgWarning)
            Fi = sla.inv(F)
        se_lam = math.sqrt(Fi[1, 1]) if Fi[1, 1] > 0 else float("nan")
    except (np.linalg.LinAlgError, sla.LinAlgError, ValueError):
        se_lam = float("nan")
    return {"lambda": lam, "A": A, "beta": s["beta"], "se_beta": np.sqrt(np.diag(s["Q"])), "se_lambda": se_lam,
            "converged": bool(best.success) and np.isfinite(best.fun), "loglik": -float(best.fun),
            "boundary": abs(lam) >= SPREG16_BOUNDARY or A <= 1e-8 * var_y}


def _logdet_terms(eig: np.ndarray, a: float) -> float:
    """log|I - a W| from the eigenvalues of W (real part of the complex sum)."""
    return float(np.sum(np.log(np.abs(1.0 - a * eig))))


def sem_ml(y: np.ndarray, X: np.ndarray, Wd: np.ndarray, eig: np.ndarray) -> Dict[str, float]:
    """Concentrated ML of the spatial error model (same likelihood as spreg.ML_Error, method 'full').

    X includes the constant. The full log-likelihood includes the constants
    -n/2 (log 2 pi + 1), as in spreg; lambda bounded to (-1, 1).
    """
    n = len(y)
    Wy, WX = Wd @ y, Wd @ X

    def nll(lam):
        ys, xs = y - lam * Wy, X - lam * WX
        b = np.linalg.lstsq(xs, ys, rcond=None)[0]
        e = ys - xs @ b
        return 0.5 * n * math.log(float(e @ e) / n) - _logdet_terms(eig, lam)

    res = optimize.minimize_scalar(nll, bounds=(-1.0, 1.0), method="bounded", options={"xatol": 1e-7})
    lam = float(res.x)
    ys, xs = y - lam * Wy, X - lam * WX
    b = np.linalg.lstsq(xs, ys, rcond=None)[0]
    e = ys - xs @ b
    return {"lambda": lam, "beta": b, "sig2": float(e @ e) / n,
            "loglik": -float(res.fun) - 0.5 * n * (math.log(2 * math.pi) + 1)}


def sdm_ml(y: np.ndarray, X: np.ndarray, Wd: np.ndarray, eig: np.ndarray) -> Dict[str, Any]:
    """Concentrated ML of the spatial Durbin model y = rho W y + X beta + W X1 theta + e.

    X includes the constant; W X1 lags the non-constant columns (as spreg.ML_Lag
    with slx_lags=1). Same likelihood as spreg.ML_Lag (method 'full'), rho in (-1, 1).
    """
    n = len(y)
    Z = np.column_stack([X, Wd @ X[:, 1:]])
    Wy = Wd @ y
    b0 = np.linalg.lstsq(Z, y, rcond=None)[0]
    b1 = np.linalg.lstsq(Z, Wy, rcond=None)[0]
    e0, e1 = y - Z @ b0, Wy - Z @ b1

    def nll(rho):
        e = e0 - rho * e1
        return 0.5 * n * math.log(float(e @ e) / n) - _logdet_terms(eig, rho)

    res = optimize.minimize_scalar(nll, bounds=(-1.0, 1.0), method="bounded", options={"xatol": 1e-7})
    rho = float(res.x)
    e = e0 - rho * e1
    return {"rho": rho, "coef": b0 - rho * b1, "sig2": float(e @ e) / n,
            "loglik": -float(res.fun) - 0.5 * n * (math.log(2 * math.pi) + 1)}


def common_factor_lr(y: np.ndarray, X: np.ndarray, Wd: np.ndarray, eig: np.ndarray) -> Tuple[float, Dict, Dict]:
    """LR = 2 (logL_SDM - logL_SEM) for H0: theta = -rho beta (k - 1 restrictions); (LR, sdm, sem)."""
    sdm, sem = sdm_ml(y, X, Wd, eig), sem_ml(y, X, Wd, eig)
    return max(2.0 * (sdm["loglik"] - sem["loglik"]), 0.0), sdm, sem


# ---------------------------------------------------------------------------
# Truth and summaries
# ---------------------------------------------------------------------------


def true_effects(W: np.ndarray, rho: float, beta: float, theta: float = 0.0) -> Dict[str, float]:
    """Average direct/indirect/total effects of x for y = rho W y + x beta + W x theta + e.

    S = (I - rho W)^-1 (beta I + theta W): direct = tr(S)/n, total = 1'S1/n,
    indirect = total - direct (LeSage & Pace 2009). rho = 0, theta = 0 gives (beta, 0, beta).
    """
    n = W.shape[0]
    S = sar_inverse(W, rho) @ (beta * np.eye(n) + theta * W)
    direct, total = float(np.trace(S) / n), float(S.sum() / n)
    return {"direct": direct, "indirect": total - direct, "total": total}


def _metric_row(method: str, target: str, est: Sequence[float], truth: Optional[float],
                covered: Optional[Sequence[bool]], extra: Dict[str, Any], ses: Optional[Sequence[float]] = None
                ) -> Dict[str, Any]:
    """Info row: coverage (if intervals), bias, RMSE and their Monte Carlo SEs."""
    e = np.asarray(est, float)
    e = e[np.isfinite(e)]
    r = int(e.size)
    row: Dict[str, Any] = {"method": method, "target": target, "reps": r, "nominal": NOMINAL,
                           "coverage": float("nan"), "mc_se": float("nan"), "status": "info"}
    if covered is not None and len(covered):
        c = np.asarray(covered, bool)
        cov = float(c.mean())
        row.update(coverage=cov, mc_se=math.sqrt(cov * (1 - cov) / c.size), reps=int(c.size))
    row["mean_estimate"] = float(e.mean()) if r else float("nan")
    if truth is not None and r:
        d = e - truth
        mse = float(np.mean(d ** 2))
        rmse = math.sqrt(mse)
        row.update(truth=truth, bias=float(d.mean()),
                   bias_mc_se=float(d.std(ddof=1) / math.sqrt(r)) if r > 1 else float("nan"),
                   rmse=rmse, rmse_mc_se=(float(np.std(d ** 2, ddof=1) / math.sqrt(r)) / (2 * rmse)
                                          if r > 1 and rmse > 0 else float("nan")))
    else:
        row.update(truth=truth if truth is not None else float("nan"), bias=float("nan"))
    if ses is not None:
        s = np.asarray(ses, float)
        s = s[np.isfinite(s)]
        if s.size and r > 1 and e.std(ddof=1) > 0:
            row["se_sd_ratio"] = float(s.mean() / e.std(ddof=1))
    row.update(extra)
    return row


def _rate(method: str, target: str, hits: Sequence[bool], nominal_rate: float, extra: Dict[str, Any]) -> Dict[str, Any]:
    v = np.asarray(hits, bool)
    p = float(v.mean()) if v.size else float("nan")
    row = {"method": method, "target": target, "reps": int(v.size), "nominal": 1 - nominal_rate,
           "coverage": 1 - p, "mc_se": math.sqrt(p * (1 - p) / v.size) if v.size else float("nan"),
           "status": "info", "size": p, "nominal_size": nominal_rate}
    row.update(extra)
    return row


def _z() -> float:
    return float(sps.norm.ppf(0.5 + NOMINAL / 2))


# ---------------------------------------------------------------------------
# A1: lambda attenuation
# ---------------------------------------------------------------------------


def _uncorrected_sem(y: np.ndarray, x: np.ndarray, w) -> Tuple[float, float, float, float]:
    """The tool default for model='error' (spreg ML_Error, method 'full'): (beta, se, lambda, se)."""
    from .spatial import _spreg_fit

    _, b, se, lam = _spreg_fit(y[:, None], x[:, None], w, "error", "ml", ["x"], "y", "full", {})
    return float(b[1]), float(se[1]), lam, float(se[2])


def _shipped_correction(y: np.ndarray, x: np.ndarray, W: np.ndarray, s: np.ndarray, method: str
                        ) -> Tuple[float, float, float, float, bool]:
    """The tool's correct_outcome_error estimator (census.spatial.error_model_known_variances, diagonal s^2):
    (beta, se, lambda, se, boundary). Non-convergence and singular fits raise RuntimeError (a fit failure)."""
    from .spatial import error_model_known_variances

    f = error_model_known_variances(y, np.column_stack([np.ones(len(y)), x]), W, s ** 2, method)
    return (float(f["beta"][1]), float(math.sqrt(f["vcov_beta"][1, 1])), f["lambda"], f["se_lambda"],
            f["boundary"])


def _attenuation_cell(cfg: SimConfig, reps: int, seed: int, kind: str, lam: float, cv_scale: float,
                      gamma: Optional[float]) -> List[Dict[str, Any]]:
    side = SPREG16_SIDE
    m = side * side
    g = _geoids(m)
    w = lattice_w(side, kind, g)
    W = w.full()[0]
    Binv = sar_inverse(W, lam)
    mean_y = 10.0 + cfg.beta * cfg.x_mean
    Lr = np.linalg.cholesky(sar_correlation(W, gamma)) if gamma else None
    rng = np.random.default_rng(seed)
    z = _z()
    # (label, method, Sigma kind): the engine's spatial Fay-Herriot likelihood (diagonal Sigma)
    # by REML (its default) and ML, and the full-Sigma oracle under correlated errors
    corrected = []
    if cv_scale > 0:
        corrected += [("spatial Fay-Herriot REML rho (diagonal SE^2: independence assumed)", "REML", "diag"),
                      ("spatial Fay-Herriot ML rho (diagonal SE^2: independence assumed)", "ML", "diag")]
    if gamma:
        corrected.append(("oracle: SEM + known correlated error covariance (REML, true Sigma)", "REML", "full"))
    names = ["uncorrected ML_Error [tool default]"] + [c[0] for c in corrected]
    rec = {k: {"lam": [], "lam_se": [], "lam_cov": [], "b": [], "b_se": [], "b_cov": [], "fail": 0, "boundary": []}
           for k in names}
    for _ in range(reps):
        x = cfg.x_mean + cfg.x_sd * rng.standard_normal(m)
        ystar = 10.0 + cfg.beta * x + Binv @ (cfg.sigma_u * rng.standard_normal(m))
        s = draw_cv(m, "tract", rng) / 100 * mean_y * cv_scale
        zz = rng.standard_normal(m)
        d = s * (Lr @ zz if Lr is not None else zz)
        y = ystar + d
        fits: Dict[str, Tuple[float, float, float, float, bool]] = {}
        try:
            b, bse, lh, lse = _uncorrected_sem(y, x, w)
            fits[names[0]] = (b, bse, lh, lse, abs(lh) >= SPREG16_BOUNDARY)
        except Exception:  # noqa: BLE001 - counted and reported
            rec[names[0]]["fail"] += 1
        X = x[:, None]
        for k, meth, kind_s in corrected:
            try:
                if kind_s == "diag":  # the shipped estimator (correct_outcome_error=True), same function
                    fits[k] = _shipped_correction(y, x, W, s, meth)
                    continue
                Sig = (s[:, None] * Lr) @ (s[:, None] * Lr).T
                f = me_sem_fit(y, X, W, Sig, method=meth)
                if not f["converged"]:
                    raise ValueError("not converged")
                fits[k] = (float(f["beta"][1]), float(f["se_beta"][1]), f["lambda"], f["se_lambda"], f["boundary"])
            except Exception:  # noqa: BLE001 - counted and reported
                rec[k]["fail"] += 1
        for k, (b, bse, lh, lse, bd) in fits.items():
            r = rec[k]
            r["lam"].append(lh)
            r["lam_se"].append(lse)
            r["lam_cov"].append(bool(np.isfinite(lse) and abs(lh - lam) <= z * lse))
            r["b"].append(b)
            r["b_se"].append(bse)
            r["b_cov"].append(bool(abs(b - cfg.beta) <= z * bse))
            r["boundary"].append(bool(bd))
    rows = []
    errs = ("none" if cv_scale == 0 else f"independent, CV x {cv_scale:g}" if not gamma
            else f"correlated SAR(gamma={gamma:g}) on W, CV x {cv_scale:g}")
    ncorr = neighbour_correlation(sar_correlation(W, gamma), W) if gamma else 0.0
    for k in names:
        r = rec[k]
        bd = np.asarray(r["boundary"], bool)
        base = {"weights": kind, "lambda": lam, "cv_scale": cv_scale, "gamma": gamma or 0.0,
                "neighbour_error_corr": ncorr, "estimator": k, "fit_failures": r["fail"],
                "boundary_rate": float(bd.mean()) if bd.size else float("nan"),
                "boundary_mc_se": float(math.sqrt(bd.mean() * (1 - bd.mean()) / bd.size)) if bd.size else float("nan")}
        lab = f"{k}; {kind} W, lambda={lam:g}, sampling error {errs}"
        rows.append(_metric_row(lab, f"lambda ({lam:g}), disturbance parameter", r["lam"], lam, r["lam_cov"],
                                {**base, "term": "lambda", "parameter_class": "disturbance"}, r["lam_se"]))
        rows.append(_metric_row(lab, f"beta ({cfg.beta:g})", r["b"], cfg.beta, r["b_cov"],
                                {**base, "term": "x", "parameter_class": "substantive"}, r["b_se"]))
    return rows


def study_spatial_error_attenuation(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """A1 under independent sampling errors: W x lambda x CV scale (docstring of the module)."""
    rows: List[Dict[str, Any]] = []
    for wi, kind in enumerate(SPREG16_WEIGHTS):
        for li, lam in enumerate(SPREG16_LAMBDAS):
            for ci, cs in enumerate(SPREG16_CV_SCALES):
                seed = cfg.seed + 1600 + 100 * wi + 10 * li + ci
                rows += _attenuation_cell(cfg, reps, seed, kind, lam, cs, None)
    return rows


def study_spatial_error_attenuation_correlated(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """A1 under spatially correlated sampling errors (SAR(gamma) correlation on W, CV x 1)."""
    rows: List[Dict[str, Any]] = []
    for wi, kind in enumerate(SPREG16_WEIGHTS):
        for li, lam in enumerate(SPREG16_LAMBDAS):
            for gi, gam in enumerate(SPREG16_GAMMAS):
                seed = cfg.seed + 2000 + 100 * wi + 10 * li + gi
                rows += _attenuation_cell(cfg, reps, seed, kind, lam, 1.0, gam)
    return rows


# ---------------------------------------------------------------------------
# A2/A3: DGP set
# ---------------------------------------------------------------------------

# (label, kind, parameters); kind: sem, sar, sdm, omitted
SPREG16_DGPS = (
    ("SEM lambda=0.6", "sem", {"lam": 0.6}),
    ("SAR lag rho=0.3", "sar", {"rho": 0.3}),
    ("SAR lag rho=0.6", "sar", {"rho": 0.6}),
    (f"SDM rho={SPREG16_SDM_RHO:g} theta={SPREG16_SDM_THETA:g}", "sdm", {"rho": SPREG16_SDM_RHO,
                                                                        "theta": SPREG16_SDM_THETA}),
    (f"omitted SAR({SPREG16_OMIT_RHO:g}) covariate, corr(x, z)={SPREG16_OMIT_CORR:g}", "omitted", {}),
    # Juhl (2021) Eq. 5: a spatially clustered omitted variable correlated with x,
    # y = x beta + (I - rho W)^-1 (x gamma + v) = SDM with beta + gamma and theta = -rho beta
    (f"Juhl omitted-variable DGP rho={SPREG16_JUHL_RHO:g} gamma={SPREG16_JUHL_GAMMA:g}", "juhl",
     {"rho": SPREG16_JUHL_RHO, "gamma": SPREG16_JUHL_GAMMA}),
)


def _dgp_truth(cfg: SimConfig, kind: str, p: Dict[str, float], W: np.ndarray) -> Dict[str, float]:
    """True average effects of x in a DGP of SPREG16_DGPS."""
    if kind == "juhl":
        return true_effects(W, p["rho"], cfg.beta + p["gamma"], -p["rho"] * cfg.beta)
    return true_effects(W, p.get("rho", 0.0), cfg.beta, p.get("theta", 0.0))


def _dgp_draw(cfg: SimConfig, kind: str, p: Dict[str, float], W: np.ndarray, inv: Dict[str, np.ndarray],
              rng: np.random.Generator) -> Tuple[np.ndarray, np.ndarray]:
    m = W.shape[0]
    e = cfg.sigma_u * rng.standard_normal(m)
    if kind == "omitted":
        zf = inv["z"] @ rng.standard_normal(m)
        zf = (zf - zf.mean()) / zf.std()
        x = cfg.x_mean + cfg.x_sd * (SPREG16_OMIT_CORR * zf + math.sqrt(1 - SPREG16_OMIT_CORR ** 2)
                                     * rng.standard_normal(m))
        return x, 10.0 + cfg.beta * x + cfg.sigma_u * zf + e
    x = cfg.x_mean + cfg.x_sd * rng.standard_normal(m)
    if kind == "sem":
        return x, 10.0 + cfg.beta * x + inv["S"] @ e
    if kind == "sar":
        return x, inv["S"] @ (10.0 + cfg.beta * x + e)
    if kind == "juhl":
        return x, 10.0 + cfg.beta * x + inv["S"] @ (p["gamma"] * (x - cfg.x_mean) + e)
    return x, inv["S"] @ (10.0 + cfg.beta * x + p["theta"] * (W @ x) + e)


def study_spatial_dgp_recovery(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """A2/A3: the tool's lag and error models fitted to SEM, SAR, SDM and omitted-covariate DGPs.

    Rook 12 x 12 lattice; outcome sampling error from the tract CV profile
    (scenario 'acs') or none. Each (DGP, scenario) cell has its own seed.
    Lag-model effects and their 90% intervals come from spatial_regression's
    effects table; the error model's effects are beta (direct = total, indirect
    0 by construction), so its indirect effect has no interval.
    """
    from .spatial import SPATIAL_METHODS, SPATIAL_MODELS, DEFAULT_METHODS, spatial_regression

    side = SPREG16_SIDE
    m = side * side
    g = _geoids(m)
    w = lattice_weights(side, g)
    W = w.full()[0]
    eig = np.linalg.eigvals(W)
    rows: List[Dict[str, Any]] = []
    for di, (label, kind, p) in enumerate(SPREG16_DGPS):
        rho = p.get("rho", 0.0)
        inv = {"S": sar_inverse(W, p.get("lam", rho))}
        if kind == "omitted":
            inv["z"] = sar_inverse(W, SPREG16_OMIT_RHO)
        truth = _dgp_truth(cfg, kind, p, W)
        mean_y = float(np.mean(inv["S"] @ np.full(m, 10.0 + cfg.beta * cfg.x_mean + p.get("theta", 0.0) * cfg.x_mean))) \
            if kind in ("sar", "sdm") else 10.0 + cfg.beta * cfg.x_mean
        for si, scen in enumerate(("acs", "none")):
            rng = np.random.default_rng(cfg.seed + 2400 + 10 * di + si)
            rng_sim = np.random.default_rng([cfg.seed, 2400 + 10 * di + si, 1])  # effect-interval draws
            fits = [(mod, meth) for mod in SPATIAL_MODELS for meth in SPATIAL_METHODS]
            rec = {f: {"beta": [], "beta_cov": [], "sp": [], "sp_cov": [], "fail": 0,
                       **{e: [] for e in ("direct", "indirect", "total")},
                       **{f"{e}_cov": [] for e in ("direct", "indirect", "total")},
                       **{f"{e}_simcov": [] for e in ("direct", "indirect", "total")},
                       **{f"{e}_se": [] for e in ("direct", "indirect", "total")}} for f in fits}
            rs_rej: Dict[str, List[bool]] = {k: [] for k in SPREG16_RS_TESTS}
            for _ in range(reps):
                x, ystar = _dgp_draw(cfg, kind, p, W, inv, rng)
                ses: Dict[str, np.ndarray] = {}
                y = ystar
                if scen == "acs":
                    ses["y"] = draw_cv(m, "tract", rng) / 100 * mean_y
                    y = ystar + ses["y"] * rng.standard_normal(m)
                frame = acs_frame(g, {"y": y, "x": x}, ses)
                for k, rej in _rs_rejections(y, x, w).items():
                    rs_rej[k].append(rej)
                for f in fits:
                    mod, meth = f
                    r = rec[f]
                    try:
                        res = spatial_regression("y ~ x", frame, w, model=mod, method=meth)
                    except Exception:  # noqa: BLE001 - counted and reported
                        r["fail"] += 1
                        continue
                    tab = res.coefficients.set_index("term")
                    sp = "rho" if mod == "lag" else "lambda"
                    sp_truth = rho if (mod == "lag" and kind in ("sar", "sdm", "juhl")) else \
                        p.get("lam") if (mod == "error" and kind == "sem") else None
                    r["beta"].append(float(tab.loc["x", "estimate"]))
                    r["beta_cov"].append(bool(tab.loc["x", "ci_lower"] <= cfg.beta <= tab.loc["x", "ci_upper"]))
                    r["sp"].append(float(tab.loc[sp, "estimate"]))
                    if sp_truth is not None:
                        r["sp_cov"].append(bool(tab.loc[sp, "ci_lower"] <= sp_truth <= tab.loc[sp, "ci_upper"]))
                    if mod == "lag":
                        eff = res.effects.set_index("effect")
                        lo, hi = _simulated_effect_ci(res, eig, rng_sim)
                        for i, e in enumerate(("direct", "indirect", "total")):
                            r[e].append(float(eff.loc[e, "estimate"]))
                            r[f"{e}_se"].append(float(eff.loc[e, "se_model"]))
                            r[f"{e}_cov"].append(bool(eff.loc[e, "ci_lower"] <= truth[e] <= eff.loc[e, "ci_upper"]))
                            r[f"{e}_simcov"].append(bool(lo[i] <= truth[e] <= hi[i]))
                    else:
                        b = tab.loc["x"]
                        for e in ("direct", "total"):
                            r[e].append(float(b["estimate"]))
                            r[f"{e}_se"].append(float(b["se_model"]))
                            r[f"{e}_cov"].append(bool(b["ci_lower"] <= truth[e] <= b["ci_upper"]))
                        r["indirect"].append(0.0)
            scen_label = "ACS-like outcome error" if scen == "acs" else "no sampling error"
            for f in fits:
                mod, meth = f
                r = rec[f]
                dflt = meth == DEFAULT_METHODS[mod]
                est = f"spatial_regression {mod} {'ML' if meth == 'ml' else 'GMM'}{' [tool default]' if dflt else ''}"
                lab = f"{est}; DGP {label}, rook 12x12, {scen_label}"
                base = {"dgp": kind, "dgp_label": label, "scenario": scen, "model": mod, "estimator": meth,
                        "tool_default": dflt, "fit_failures": r["fail"]}
                sp = "rho" if mod == "lag" else "lambda"
                sp_truth = rho if (mod == "lag" and kind in ("sar", "sdm", "juhl")) else \
                    p.get("lam") if (mod == "error" and kind == "sem") else None
                pclass = "disturbance" if mod == "error" else "spatial lag (substantive spillover)"
                rows.append(_metric_row(lab, f"{sp} ({'no true value in this DGP' if sp_truth is None else sp_truth})",
                                        r["sp"], sp_truth, r["sp_cov"] if sp_truth is not None else None,
                                        {**base, "term": sp, "parameter_class": pclass}))
                rows.append(_metric_row(lab, f"beta coefficient ({cfg.beta:g}; the marginal effect only when "
                                             "rho = 0 and theta = 0)", r["beta"], cfg.beta, r["beta_cov"],
                                        {**base, "term": "x", "parameter_class": "coefficient"}))
                for e in ("direct", "indirect", "total"):
                    has_ci = mod == "lag" or e != "indirect"
                    rows.append(_metric_row(lab, f"{e} effect of x ({truth[e]:.3f})", r[e], truth[e],
                                            r[f"{e}_cov"] if has_ci else None,
                                            {**base, "term": f"x ({e})", "parameter_class": "substantive effect",
                                             "interval": "delta method [tool]" if mod == "lag" else "beta interval"},
                                            r[f"{e}_se"] if has_ci else None))
                    if mod == "lag":
                        rows.append(_metric_row(lab, f"{e} effect of x ({truth[e]:.3f}), simulated interval",
                                                r[e], truth[e], r[f"{e}_simcov"],
                                                {**base, "term": f"x ({e})", "parameter_class": "substantive effect",
                                                 "interval": f"simulated percentile ({SPREG16_EFFECT_DRAWS} draws)"}))
            for k, v in rs_rej.items():
                rows.append(_rate(f"OLS-based RS test {k} (spreg; Koley & Bera 2024); DGP {label}, rook 12x12, "
                                  f"{scen_label}", f"rejection rate at {SPREG16_LR_ALPHA:g} (H0: {SPREG16_RS_TESTS[k]})",
                                  v, SPREG16_LR_ALPHA, {"dgp": kind, "dgp_label": label, "scenario": scen,
                                                        "term": k, "parameter_class": "specification test"}))
    return rows


def _rs_rejections(y: np.ndarray, x: np.ndarray, w) -> Dict[str, bool]:
    """Rejections at SPREG16_LR_ALPHA of spreg's OLS-based RS tests (SPREG16_RS_TESTS)."""
    import spreg

    from .spatial import _silence_spreg_output

    _silence_spreg_output()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ols = spreg.OLS(y[:, None], x[:, None])
        lm = spreg.LMtests(ols, w, tests=["lml", "lmwx", "lmspdurbin", "rlmwx", "rlmdurlag"])
    return {k: float(np.ravel(getattr(lm, k))[1]) < SPREG16_LR_ALPHA for k in SPREG16_RS_TESTS}


def _simulated_effect_ci(res, eig: np.ndarray, rng: np.random.Generator) -> Tuple[np.ndarray, np.ndarray]:
    """LeSage & Pace (2009) simulated 90% intervals of (direct, indirect, total) for the first covariate.

    Draws (beta, rho) from N(estimate, coefficient_vcov) of the lag fit and
    takes percentiles of the effects; draws with |rho| >= 1 give NaN effects
    and are dropped.
    """
    cv = res.diagnostics["coefficient_vcov"]
    est = res.coefficients.set_index("term").loc[cv["terms"], "estimate"].to_numpy(float)
    draws = rng.multivariate_normal(est, np.asarray(cv["matrix"], float), size=SPREG16_EFFECT_DRAWS)
    b, r = draws[:, 1], draws[:, -1]
    ok = np.abs(r) < 1
    b, r = b[ok], r[ok]
    direct = np.real(np.mean(1.0 / (1.0 - r[:, None] * eig[None, :]), axis=1))  # census.spatial.lag_multipliers
    total = 1.0 / (1.0 - r)
    vals = np.column_stack([b * direct, b * (total - direct), b * total])
    q = (1 - NOMINAL) / 2
    return np.quantile(vals, q, axis=0), np.quantile(vals, 1 - q, axis=0)


# ---------------------------------------------------------------------------
# A4: common-factor LR test
# ---------------------------------------------------------------------------


def lr_residual_bootstrap(y: np.ndarray, X: np.ndarray, Wd: np.ndarray, eig: np.ndarray, lr: float,
                          sem: Dict[str, Any], rng: np.random.Generator, n_boot: int = SPREG16_LR_BOOT) -> bool:
    """Juhl (2021, sec. 3.3) residual bootstrap of the common-factor test: True if H0 is rejected.

    Fit the SEM (``sem``: sem_ml of the data); its innovations
    e = (I - lambda W)(y - X beta), centred, are resampled with replacement
    into n_boot samples y_b = X beta + (I - lambda W)^-1 e_b generated under
    the SEM; the statistic is recomputed on each and H0 rejected when the
    observed LR exceeds the (1 - alpha) quantile of the bootstrap LRs.
    """
    n = len(y)
    B = np.eye(n) - sem["lambda"] * Wd
    Binv = sla.inv(B)
    mu = X @ sem["beta"]
    e = B @ (y - mu)
    e = e - e.mean()
    stats_b = np.array([common_factor_lr(mu + Binv @ rng.choice(e, n, replace=True), X, Wd, eig)[0]
                        for _ in range(n_boot)])
    return bool(lr > np.quantile(stats_b, 1 - SPREG16_LR_ALPHA))


def _me_bootstrap(y: np.ndarray, X: np.ndarray, Wd: np.ndarray, eig: np.ndarray, lr: float, s: np.ndarray,
                  rng: np.random.Generator, n_boot: int = SPREG16_LR_BOOT) -> bool:
    """ADAPTATION (not in Juhl): parametric bootstrap under the SEM plus known independent sampling error.

    The null model is the spatial Fay-Herriot fit (me_sem_fit, REML, diagonal
    s^2); bootstrap samples are X beta + (I - lambda W)^-1 N(0, A I) + N(0, s^2).
    """
    n = len(y)
    f = me_sem_fit(y, X[:, 1:], Wd, np.diag(s ** 2))
    Binv = sar_inverse(Wd, f["lambda"])
    mu = X @ f["beta"]
    sd = math.sqrt(f["A"])
    stats_b = np.array([common_factor_lr(mu + Binv @ (sd * rng.standard_normal(n)) + s * rng.standard_normal(n),
                                         X, Wd, eig)[0] for _ in range(n_boot)])
    return bool(lr > np.quantile(stats_b, 1 - SPREG16_LR_ALPHA))


def _lr_rows(label: str, scen_label: str, h0: bool, base: Dict[str, Any], tests: Dict[str, List[bool]]
             ) -> List[Dict[str, Any]]:
    what = "size (rejection rate, H0 true)" if h0 else "power (rejection rate, H0 false)"
    names = {"chi2": "chi-square(1) critical value",
             "bootstrap_residual": f"Juhl residual bootstrap (B={SPREG16_LR_BOOT})",
             "bootstrap_me": f"ME-aware parametric bootstrap (B={SPREG16_LR_BOOT}; adaptation)"}
    return [_rate(f"common-factor LR, {names[k]}; {label}, {scen_label}", f"{what} at {SPREG16_LR_ALPHA:g}",
                  v, SPREG16_LR_ALPHA, {**base, "critical": k})
            for k, v in tests.items() if v]


def study_common_factor_lr(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """A4: size and power of the LR common-factor test in Juhl's (2021) design, no sampling error.

    y = x beta + (I - rho W)^-1 (x gamma + v), beta = 2, x, v ~ N(0, 1),
    row-standardized queen lattices of side 7, 10, 15, 20 (n 49-400),
    rho 0-0.8, gamma 0 (the SEM: H0 theta = -rho beta true) and 0.5, 1 (an
    SDM with beta + gamma and theta = -rho beta: H0 false). LR = 2 (logL_SDM
    - logL_SEM) by ML with the full Jacobian (sem_ml / sdm_ml, the
    likelihoods of spreg ML_Error and ML_Lag with slx_lags=1). Critical
    values: chi-square(1); on the first SPREG16_LR_BOOT_REPS repetitions of
    the gamma cells in SPREG16_JUHL_BOOT_GAMMAS also Juhl's residual
    bootstrap. Each (side, rho, gamma) cell has its own seed.
    """
    crit = float(sps.chi2.ppf(1 - SPREG16_LR_ALPHA, 1))
    rows: List[Dict[str, Any]] = []
    for zi, side in enumerate(SPREG16_JUHL_SIDES):
        w = lattice_w(side, "queen")
        W = w.full()[0]
        n = w.n
        eig = np.linalg.eigvals(W)
        for ri, rho in enumerate(SPREG16_JUHL_RHOS):
            S = sar_inverse(W, rho)
            for gi, gam in enumerate(SPREG16_JUHL_GAMMAS_LR):
                rng = np.random.default_rng(cfg.seed + 3000 + 100 * zi + 10 * ri + gi)
                tests: Dict[str, List[bool]] = {"chi2": [], "bootstrap_residual": []}
                lrs, fail = [], 0
                fitted: Dict[str, List[float]] = {"sem_lambda": [], "sem_beta": [], "sdm_rho": [], "sdm_beta": [],
                                                  "sdm_theta": []}
                for rep in range(reps):
                    x = rng.standard_normal(n)
                    y = SPREG16_JUHL_BETA * x + S @ (gam * x + rng.standard_normal(n))
                    X = np.column_stack([np.ones(n), x])
                    try:
                        lr, sdm, sem = common_factor_lr(y, X, W, eig)
                        for k, v in (("sem_lambda", sem["lambda"]), ("sem_beta", sem["beta"][1]),
                                     ("sdm_rho", sdm["rho"]), ("sdm_beta", sdm["coef"][1]),
                                     ("sdm_theta", sdm["coef"][2])):
                            fitted[k].append(float(v))
                        boot = (lr_residual_bootstrap(y, X, W, eig, lr, sem, rng)
                                if gam in SPREG16_JUHL_BOOT_GAMMAS and rep < SPREG16_LR_BOOT_REPS else None)
                    except Exception:  # noqa: BLE001 - counted and reported
                        fail += 1
                        continue
                    lrs.append(lr)
                    tests["chi2"].append(lr > crit)
                    if boot is not None:
                        tests["bootstrap_residual"].append(boot)
                label = f"Juhl DGP, queen {side}x{side} (n={n}), rho={rho:g}, gamma={gam:g}"
                base = {"design": "juhl", "n_areas": n, "rho": rho, "gamma": gam, "scenario": "none",
                        "h0_true": gam == 0, "fit_failures": fail, "lr_df": 1,
                        "lr_mean": float(np.mean(lrs)) if lrs else float("nan"),
                        # mean ML estimates: SEM lambda and beta vs the truth (rho, beta = 2) show how an
                        # SEM misreads an SDM-like DGP (Lacombe & LeSage 2015); SDM: beta + gamma, -rho beta
                        **{f"{k}_mean": float(np.mean(v)) if v else float("nan") for k, v in fitted.items()}}
                rows += _lr_rows(label, "no sampling error", gam == 0, base, tests)
    return rows


def study_common_factor_lr_acs(cfg: SimConfig, reps: int) -> List[Dict[str, Any]]:
    """A4 extension: the common-factor LR test with ACS-like outcome sampling error.

    Queen 12 x 12 lattice on the scale of the other studies: y* = 10 + beta x
    + (I - rho W)^-1 (gamma (x - x_mean) + e), beta = cfg.beta, x ~ N(x_mean,
    x_sd^2), e ~ N(0, sigma_u^2); gamma 0 (SEM, H0 true) or
    SPREG16_ACS_LR_GAMMAS[1] (H0 false); rho in SPREG16_ACS_LR_RHOS. The
    published y adds independent error with tract-profile CVs (scenario
    'acs') or none. Under 'acs' the SEM null is itself misspecified (the
    data follow an SEM plus independent noise). Critical values: chi-square(1),
    Juhl's residual bootstrap and, under 'acs', a parametric bootstrap from
    the spatial Fay-Herriot fit with the known SEs (an adaptation), both on
    the first SPREG16_LR_BOOT_REPS repetitions.
    """
    side = SPREG16_SIDE
    w = lattice_w(side, "queen")
    W = w.full()[0]
    m = w.n
    eig = np.linalg.eigvals(W)
    crit = float(sps.chi2.ppf(1 - SPREG16_LR_ALPHA, 1))
    mean_y = 10.0 + cfg.beta * cfg.x_mean
    rows: List[Dict[str, Any]] = []
    for ri, rho in enumerate(SPREG16_ACS_LR_RHOS):
        S = sar_inverse(W, rho)
        for gi, gam in enumerate(SPREG16_ACS_LR_GAMMAS):
            for si, scen in enumerate(("none", "acs")):
                rng = np.random.default_rng(cfg.seed + 3600 + 100 * ri + 10 * gi + si)
                tests: Dict[str, List[bool]] = {"chi2": [], "bootstrap_residual": [], "bootstrap_me": []}
                lrs, fail = [], 0
                for rep in range(reps):
                    x = cfg.x_mean + cfg.x_sd * rng.standard_normal(m)
                    ystar = 10.0 + cfg.beta * x + S @ (gam * (x - cfg.x_mean) + cfg.sigma_u * rng.standard_normal(m))
                    s = draw_cv(m, "tract", rng) / 100 * mean_y if scen == "acs" else np.zeros(m)
                    y = ystar + s * rng.standard_normal(m)
                    X = np.column_stack([np.ones(m), x])
                    try:
                        lr, _, sem = common_factor_lr(y, X, W, eig)
                        boot = rep < SPREG16_LR_BOOT_REPS
                        b_res = lr_residual_bootstrap(y, X, W, eig, lr, sem, rng) if boot else None
                        b_me = _me_bootstrap(y, X, W, eig, lr, s, rng) if boot and scen == "acs" else None
                    except Exception:  # noqa: BLE001 - counted and reported
                        fail += 1
                        continue
                    lrs.append(lr)
                    tests["chi2"].append(lr > crit)
                    if b_res is not None:
                        tests["bootstrap_residual"].append(b_res)
                    if b_me is not None:
                        tests["bootstrap_me"].append(b_me)
                label = f"queen {side}x{side}, rho={rho:g}, gamma={gam:g}"
                base = {"design": "acs_extension", "n_areas": m, "rho": rho, "gamma": gam, "scenario": scen,
                        "h0_true": gam == 0, "fit_failures": fail, "lr_df": 1,
                        "lr_mean": float(np.mean(lrs)) if lrs else float("nan")}
                rows += _lr_rows(label, "ACS-like outcome error" if scen == "acs" else "no sampling error",
                                 gam == 0, base, tests)
    return rows


SPREG_VALIDATION_STUDIES: Dict[str, Callable[[SimConfig, int], List[Dict[str, Any]]]] = {
    "spatial_error_attenuation": study_spatial_error_attenuation,
    "spatial_error_attenuation_correlated": study_spatial_error_attenuation_correlated,
    "spatial_dgp_recovery": study_spatial_dgp_recovery,
    "common_factor_lr": study_common_factor_lr,
    "common_factor_lr_acs": study_common_factor_lr_acs,
}


def spreg_validation_config(fast: bool = True, **overrides: Any) -> SimConfig:
    """Config that runs only the Round 16 workstream A studies."""
    base = FAST if fast else FULL
    reps = {k: 0 for k in base.reps}
    reps.update(SPREG16_REPS_FAST if fast else SPREG16_REPS_FULL)
    return replace(base, reps=reps, **overrides)
