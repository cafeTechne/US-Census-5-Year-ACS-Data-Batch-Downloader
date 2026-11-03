# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spatial Fay-Herriot model with SAR(1) area effects (PLAN_ROUND_9 workstream C).

Model (Petrucci & Salvati 2006; Pratesi & Salvati 2008)::

    y = theta + e,        e ~ N(0, diag(D))        (D_i known: ACS SE squared)
    theta = X beta + u,   u = rho W u + eps,  eps ~ N(0, A I)

so u ~ N(0, G), G = A C, C = [(I - rho W)'(I - rho W)]^-1 and V = G + diag(D).
W is a row-standardized proximity matrix (zero diagonal), rho in (-1, 1).

* beta^ = (X'V^-1X)^-1 X'V^-1 y (GLS); EBLUP theta~ = X beta^ + G V^-1 (y - X beta^).
* (A, rho) by REML (default) or ML, maximized with analytic scores under
  bounds A >= 0, |rho| <= 0.999 from several starts.
* Analytic MSE (Pratesi & Salvati 2008, Statistical Methods & Applications
  17:113-141; Singh, Shukla & Kundu 2005): g1 + g2 + 2 g3 - g4, with
  g1 = diag(G - G V^-1 G), g2 = diag[(X - G V^-1 X) Q (X - G V^-1 X)'],
  g3_i = tr[L_i V L_i' I^-1], L_i the rows of d(G V^-1)/d(A, rho) for area i
  and I the REML information 0.5 tr(P V_k P V_l), g4 the second-order term
  in rho; under ML the first-order bias of (A^, rho^) is also removed. This
  is exactly the estimator of R ``sae::mseSFH``.
* Parametric bootstrap MSE (Molina, Salvati & Pratesi 2009, Journal of
  Statistical Computation and Simulation 79:1007-1020).

With rho fixed at 0 the model is the ordinary Fay-Herriot model
(census.sae.fay_herriot). Reproduces the published R ``sae::eblupSFH`` /
``mseSFH`` grapes example (Molina & Marhuenda 2015, The R Journal 7(1):81-98;
tests/fixtures/sae_spatial).
"""

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import linalg as sla, optimize, stats as sps

from .results import SMALL_AREA_COLUMNS, SmallAreaResult
from .sae import MSE_METHODS, _coef_frame, _design

SFH_VARIANCE_METHODS = ("REML", "ML")
RHO_BOUND = 0.999
REFERENCES = [
    "Petrucci, A. & Salvati, N. (2006). Small area estimation for spatial correlation in watershed "
    "erosion assessment. JABES 11(2), 169-182.",
    "Pratesi, M. & Salvati, N. (2008). Small area estimation: the EBLUP estimator based on spatially "
    "correlated random area effects. Statistical Methods & Applications 17, 113-141.",
    "Singh, B.B., Shukla, G.K. & Kundu, D. (2005). Spatio-temporal models in small area estimation. "
    "Survey Methodology 31(2), 183-195.",
    "Molina, I., Salvati, N. & Pratesi, M. (2009). Bootstrap for estimating the MSE of the spatial "
    "EBLUP. Journal of Statistical Computation and Simulation 79, 1007-1020.",
    "Molina, I. & Marhuenda, Y. (2015). sae: An R Package for Small Area Estimation. The R Journal 7(1), 81-98.",
]


# ---------------------------------------------------------------------------
# Numerics
# ---------------------------------------------------------------------------


class _State:
    """Matrices of the model at (A, rho)."""

    def __init__(self, A: float, rho: float, X: np.ndarray, D: np.ndarray, W: np.ndarray):
        m = len(D)
        I = np.eye(m)
        B = I - rho * W
        # scipy (LAPACK potrf/potri) rather than numpy.linalg.inv: the latter is
        # pathologically slow on these sparse-structured matrices in some builds.
        self.C = sla.cho_solve(sla.cho_factor(B.T @ B), I)
        self.C = 0.5 * (self.C + self.C.T)
        self.G = A * self.C
        self.V = self.G + np.diag(D)
        cf = sla.cho_factor(self.V)
        self.logdet_v = 2.0 * float(np.sum(np.log(np.diag(cf[0]))))
        self.Vi = sla.cho_solve(cf, I)
        self.Vi = 0.5 * (self.Vi + self.Vi.T)
        ViX = self.Vi @ X
        self.Q = sla.inv(X.T @ ViX)
        self.P = self.Vi - ViX @ self.Q @ ViX.T
        # dC/drho = C (W + W' - 2 rho W'W) C ; V_A = C, V_rho = A dC/drho
        self.Vrho = A * (self.C @ (W + W.T - 2 * rho * W.T @ W) @ self.C)
        self.A, self.rho = A, rho


def _loglik(theta: np.ndarray, y, X, D, W, method: str) -> Tuple[float, np.ndarray]:
    A, rho = float(theta[0]), float(theta[1])
    try:
        s = _State(A, rho, X, D, W)
    except (np.linalg.LinAlgError, sla.LinAlgError):
        return -np.inf, np.zeros(2)
    logdet_v = s.logdet_v
    m, p = X.shape
    if method == "REML":
        Py = s.P @ y
        _, logdet_q = np.linalg.slogdet(s.Q)
        ll = -0.5 * ((m - p) * math.log(2 * math.pi) + logdet_v - logdet_q + y @ Py)
        M, r = s.P, Py
    else:
        beta = s.Q @ (X.T @ (s.Vi @ y))
        r = s.Vi @ (y - X @ beta)
        ll = -0.5 * (m * math.log(2 * math.pi) + logdet_v + (y - X @ beta) @ r)
        M = s.Vi
    grad = np.array([-0.5 * np.sum(M * Vk.T) + 0.5 * r @ Vk @ r for Vk in (s.C, s.Vrho)])
    return float(ll), grad


def _fisher(s: _State, method: str) -> np.ndarray:
    M = s.P if method == "REML" else s.Vi
    MV = [M @ s.C, M @ s.Vrho]
    F = np.empty((2, 2))
    for a in range(2):
        for b in range(2):
            F[a, b] = 0.5 * np.sum(MV[a] * MV[b].T)
    return F


def estimate_variance_rho(y, X, D, W, method: str = "REML", fix_rho: Optional[float] = None,
                          start: Optional[Tuple[float, float]] = None) -> Tuple[float, float, bool]:
    """(A^, rho^, converged) maximizing the REML/ML likelihood under bounds.

    Several starts by default; ``start`` (used by the bootstrap) gives one warm start.
    """
    method = method.upper()
    if method not in SFH_VARIANCE_METHODS:
        raise ValueError(f"method must be one of {SFH_VARIANCE_METHODS}")
    upper = max(float(np.var(y, ddof=1)) * 10.0, float(np.max(D)) * 10.0, 1e-8)
    a0s = [max(float(np.var(y, ddof=1)) - float(np.mean(D)), upper * 1e-3), float(np.median(D))]
    rho_bounds = (fix_rho, fix_rho) if fix_rho is not None else (-RHO_BOUND, RHO_BOUND)
    rho0s = [fix_rho] if fix_rho is not None else [0.5, 0.0, -0.5]

    def nll(t):
        try:
            ll, g = _loglik(t, y, X, D, W, method)
        except (np.linalg.LinAlgError, sla.LinAlgError, ValueError):
            return 1e300, np.zeros(2)
        if not np.isfinite(ll):
            return 1e300, np.zeros(2)
        g = -g
        if fix_rho is not None:
            g[1] = 0.0
        return -ll, g

    starts = [(a0, r0) for a0 in a0s for r0 in rho0s]
    if start is not None:
        starts = [(max(float(start[0]), upper * 1e-6), fix_rho if fix_rho is not None else float(start[1]))]
    best = None
    for a0, r0 in starts:
        res = optimize.minimize(nll, np.array([a0, r0]), jac=True, method="L-BFGS-B",
                                bounds=[(0.0, upper), rho_bounds],
                                options={"ftol": 1e-15, "gtol": 1e-10, "maxiter": 2000})
        if best is None or res.fun < best.fun:
            best = res
    A, rho = float(best.x[0]), float(best.x[1])
    # L-BFGS-B often stops on its line search at the optimum with ftol this
    # tight; judge convergence by the projected, scaled score instead.
    _, g = nll(best.x)
    ok = True
    for k, (val, lo, hi, scale) in enumerate([(A, 0.0, upper, max(A, 1e-8)), (rho, *rho_bounds, 1.0)]):
        if fix_rho is not None and k == 1:
            continue
        at_lo, at_hi = val <= lo + 1e-10, val >= hi - 1e-10
        if (at_lo and g[k] >= 0) or (at_hi and g[k] <= 0):
            continue
        ok = ok and abs(g[k]) * scale < 1e-3
    return A, rho, bool(best.success or ok)


def _fit(y, X, D, W, method, fix_rho=None, start=None) -> Dict[str, Any]:
    A, rho, ok = estimate_variance_rho(y, X, D, W, method, fix_rho, start)
    s = _State(A, rho, X, D, W)
    beta = s.Q @ (X.T @ (s.Vi @ y))
    synthetic = X @ beta
    eblup = synthetic + s.G @ (s.Vi @ (y - synthetic))
    return {"A": A, "rho": rho, "converged": ok, "beta": beta, "Q": s.Q, "synthetic": synthetic,
            "eblup": eblup, "state": s}


def analytic_mse(fit: Dict[str, Any], X: np.ndarray, D: np.ndarray, W: np.ndarray, method: str = "REML",
                 fix_rho: Optional[float] = None) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """Second-order MSE of the spatial EBLUP, as in R ``sae::mseSFH``.

    mse = g1 + g2 + 2 g3 - g4 (Singh, Shukla & Kundu 2005; Pratesi & Salvati
    2008), with g3 from the REML information matrix 0.5 tr(P V_k P V_l) and g4
    the second-order term in rho; under ML the first-order bias of (A^, rho^)
    is also removed from g1 (minus b_ML' grad g1). With ``fix_rho`` only the
    sigma2_u terms apply and g4 = 0.
    """
    s: _State = fit["state"]
    A, rho = s.A, s.rho
    G, Vi, Q, V, C = s.G, s.Vi, s.Q, s.V, s.C
    m = len(D)
    GVi = G @ Vi
    g1 = np.diag(G - GVi @ G)
    Xa = X - GVi @ X
    g2 = np.einsum("ij,jk,ik->i", Xa, Q, Xa)
    derivs = [C, s.Vrho]  # dV/dA, dV/drho (= dG/dpsi)
    q = 1 if fix_rho is not None else 2
    derivs = derivs[:q]
    Fi = sla.inv(_fisher(s, "REML")[:q, :q])
    # rows of d(V^-1 G)/dpsi' : V^-1 G_k - V^-1 G_k V^-1 G
    Ls = [Vi @ Gk - Vi @ Gk @ Vi @ G for Gk in derivs]
    g3 = np.empty(m)
    for i in range(m):
        L = np.vstack([Lk[:, i] for Lk in Ls])  # (q, m)
        g3[i] = np.sum((L @ V @ L.T) * Fi.T)
    parts = {"g1": g1, "g2": g2, "g3": g3}
    mse = g1 + g2 + 2.0 * g3
    if q == 2:
        der_rho = 2 * rho * W.T @ W - W - W.T
        D12 = -(C @ der_rho @ C)
        D22 = 2 * A * C @ der_rho @ C @ der_rho @ C - 2 * A * C @ W.T @ W @ C
        PsiVi = D[:, None] * Vi  # diag(D) V^-1
        g4 = 0.5 * (np.einsum("ij,jk,ik->i", PsiVi, D12, PsiVi) * (Fi[0, 1] + Fi[1, 0])
                    + np.einsum("ij,jk,ik->i", PsiVi, D22, PsiVi) * Fi[1, 1])
        mse = mse - g4
        parts["g4"] = g4
    if method == "ML":
        ViX = Vi @ X
        h = np.array([-np.trace(Q @ ViX.T @ Vk @ ViX) for Vk in derivs])
        b = Fi @ h / 2.0
        ViC = Vi @ C
        grads = [np.diag(C - 2 * GVi @ C + A * GVi @ C @ ViC)]
        if q == 2:
            grads.append(np.diag(s.Vrho - 2 * GVi @ s.Vrho + A * GVi @ s.Vrho @ ViC))
        bias = b @ np.vstack(grads)
        mse = mse - bias
        parts["ml_bias"] = bias
    return mse, parts


def bootstrap_mse(y, X, D, W, method: str, fit: Dict[str, Any], n_boot: int = 500, seed: Optional[int] = None,
                  fix_rho: Optional[float] = None) -> np.ndarray:
    """Parametric bootstrap MSE (Molina, Salvati & Pratesi 2009)."""
    rng = np.random.default_rng(seed)
    m = len(y)
    Binv = sla.inv(np.eye(m) - fit["rho"] * W)
    acc = np.zeros(m)
    for _ in range(int(n_boot)):
        theta = fit["synthetic"] + Binv @ rng.normal(0.0, math.sqrt(fit["A"]), m)
        ystar = theta + rng.standard_normal(m) * np.sqrt(D)
        acc += (_fit(ystar, X, D, W, method, fix_rho, start=(fit["A"], fit["rho"]))["eblup"] - theta) ** 2
    return acc / int(n_boot)


# ---------------------------------------------------------------------------
# Frame interface
# ---------------------------------------------------------------------------


def proximity_matrix(weights, ids, row_standardize: bool = True) -> np.ndarray:
    """Dense proximity matrix in the order of ``ids`` from a libpysal W, ndarray or DataFrame.

    An ndarray is taken to be in ``ids`` order already. A DataFrame is
    reindexed by ``ids`` on both axes. Rows are row-standardized (islands keep
    a zero row) unless ``row_standardize=False``.
    """
    ids = [str(i) for i in ids]
    if hasattr(weights, "neighbors") and hasattr(weights, "id_order"):
        known = set(map(str, weights.id_order))
        miss = [i for i in ids if i not in known]
        if miss:
            raise ValueError(f"{len(miss)} area(s) missing from the weights: {miss[:20]}")
        pos = {i: k for k, i in enumerate(ids)}
        Wm = np.zeros((len(ids), len(ids)))
        for i in weights.id_order:
            si = str(i)
            if si not in pos:
                continue
            for j, wij in zip(weights.neighbors[i], weights.weights[i]):
                if str(j) in pos:
                    Wm[pos[si], pos[str(j)]] = wij
    elif isinstance(weights, pd.DataFrame):
        Wd = weights.copy()
        Wd.index, Wd.columns = Wd.index.astype(str), Wd.columns.astype(str)
        Wm = Wd.reindex(index=ids, columns=ids).to_numpy(float)
        if not np.isfinite(Wm).all():
            raise ValueError("proximity DataFrame does not cover every area")
    else:
        Wm = np.asarray(weights, float)
        if Wm.shape != (len(ids), len(ids)):
            raise ValueError(f"proximity matrix shape {Wm.shape} does not match {len(ids)} areas")
        Wm = Wm.copy()
    if np.any(np.diag(Wm) != 0):
        raise ValueError("proximity matrix must have a zero diagonal")
    if np.any(Wm < 0):
        raise ValueError("proximity matrix must be non-negative")
    if row_standardize:
        rs = Wm.sum(axis=1, keepdims=True)
        Wm = np.divide(Wm, rs, out=np.zeros_like(Wm), where=rs > 0)
    return Wm


def spatial_fay_herriot(
    formula: str,
    frame: pd.DataFrame,
    se_col: str,
    weights,
    *,
    method: str = "REML",
    mse_method: str = "analytic",
    n_boot: int = 500,
    seed: Optional[int] = None,
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
    fix_rho: Optional[float] = None,
    row_standardize: bool = True,
) -> SmallAreaResult:
    """Spatial Fay-Herriot EBLUPs with MSEs (SMALL_AREA_COLUMNS).

    Like census.sae.fay_herriot, plus ``weights`` (libpysal W keyed by GEOID,
    a DataFrame indexed/columned by GEOID, or an ndarray in frame row order),
    row-standardized by default. ``method``: REML (default) or ML for
    (sigma2_u, rho). ``mse_method``: ``analytic`` (Pratesi-Salvati second-order
    approximation; ML with bias correction) or ``bootstrap`` (parametric).
    ``fix_rho`` holds rho fixed (``fix_rho=0`` gives ordinary Fay-Herriot).
    The estimated rho is in ``provenance['rho']`` and in the notes; ``gamma`` is
    the diagonal of G V^-1 (the weight on the area's own direct estimate).
    """
    method = method.upper()
    if method not in SFH_VARIANCE_METHODS:
        raise ValueError(f"method must be one of {SFH_VARIANCE_METHODS}")
    if mse_method not in MSE_METHODS:
        raise ValueError(f"mse_method must be one of {MSE_METHODS}")
    if fix_rho is not None and not -1 < fix_rho < 1:
        raise ValueError("fix_rho must lie in (-1, 1)")
    y, X, D, terms, ids, yname = _design(formula, frame, se_col, geoid_col)
    m, p = X.shape
    if m <= p + 1:
        raise ValueError(f"need more areas ({m}) than parameters ({p + 2})")
    W = proximity_matrix(weights, ids, row_standardize)
    islands = int(np.sum(W.sum(axis=1) == 0))
    fit = _fit(y, X, D, W, method, fix_rho)
    A, rho = fit["A"], fit["rho"]
    notes: List[str] = [f"spatial autocorrelation of area effects rho = {rho:.4f}"
                        + (" (fixed)" if fix_rho is not None else f" ({method})")]
    if not fit["converged"]:
        notes.append("optimizer did not report convergence; check the likelihood surface")
    if A == 0.0:
        notes.append("sigma2_u estimated at 0: EBLUPs equal the synthetic estimates (complete shrinkage); "
                     "rho is not identified and the analytic MSE may understate uncertainty")
    if fix_rho is None and abs(rho) >= RHO_BOUND - 1e-6:
        notes.append(f"rho at the bound +/-{RHO_BOUND}: the SAR model is near-singular")
    if islands:
        notes.append(f"{islands} island(s) (no neighbors): their effects are independent of other areas")
    if mse_method == "analytic":
        mse, _ = analytic_mse(fit, X, D, W, method, fix_rho)
        mse_label = "Pratesi-Salvati / Singh et al. analytic (g1+g2+2g3-g4" + (", ML bias-corrected)" if method == "ML" else ")")
    else:
        mse = bootstrap_mse(y, X, D, W, method, fit, n_boot=n_boot, seed=seed, fix_rho=fix_rho)
        mse_label = f"parametric bootstrap (B={int(n_boot)}, seed={seed})"
    if np.any(mse < 0):
        notes.append(f"{int(np.sum(mse < 0))} negative analytic MSE(s) truncated at 0")
        mse = np.maximum(mse, 0.0)
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    z = float(sps.norm.ppf(1 - (1 - level) / 2))
    rmse = np.sqrt(mse)
    s: _State = fit["state"]
    gamma = np.diag(s.G @ s.Vi)
    est = pd.DataFrame({
        "GEOID": ids, "direct": y, "direct_se": np.sqrt(D), "synthetic": fit["synthetic"],
        "eblup": fit["eblup"], "mse": mse, "rmse": rmse,
        "ci_lower": fit["eblup"] - z * rmse, "ci_upper": fit["eblup"] + z * rmse, "gamma": gamma,
    })[SMALL_AREA_COLUMNS]
    ll_ml, _ = _loglik(np.array([A, rho]), y, X, D, W, "ML")
    k = p + 2 if fix_rho is None else p + 1
    Fi = sla.inv(_fisher(s, method)) if A > 0 else np.full((2, 2), np.nan)
    provenance = {
        "formula": formula, "response": yname, "se_col": se_col, "n_areas": int(m), "n_coefficients": int(p),
        "rho": float(rho), "rho_se": float(math.sqrt(Fi[1, 1])) if fix_rho is None and np.isfinite(Fi[1, 1]) else None,
        "sigma2_u_se": float(math.sqrt(Fi[0, 0])) if np.isfinite(Fi[0, 0]) else None,
        "rho_fixed": fix_rho is not None, "islands": islands,
        "loglike": ll_ml, "aic": -2 * ll_ml + 2 * k, "bic": -2 * ll_ml + k * math.log(m),
        "references": REFERENCES,
    }
    return SmallAreaResult(
        estimates=est, sigma2_u=float(A), coefficients=_coef_frame(terms, fit["beta"], fit["Q"], z),
        method="spatial_fay_herriot_eblup", variance_method=method, mse_method=mse_label,
        confidence_level=level, notes=notes, provenance=provenance,
    )
