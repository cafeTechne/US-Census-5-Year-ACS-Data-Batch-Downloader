# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multilevel (mixed) models with ACS sampling error (PLAN_ROUND_10.md workstream D).

Two estimators share one interface, :func:`fit_multilevel`:

* **Unit-level / no known variance** (people within PUMAs, tracts within
  counties without SEs): ``statsmodels`` MixedLM, random intercepts and
  optional random slopes (``re_formula``), REML by default.

* **Area-level outcomes with known sampling variance** (``sampling_se``): a
  Fay-Herriot-style likelihood (Fay & Herriot 1979; Rao & Molina 2015 ch. 6)
  generalized to nested areas::

      y_ij = x_ij' beta + z_ij' b_j + v_ij + e_ij
      b_j ~ N(0, G)          group (e.g. county) random effects
      v_ij ~ N(0, sigma2_v)  area-level model error (``area_effect=True``)
      e_ij ~ N(0, D_ij)      ACS sampling error, D_ij = SE_ij^2 known

  so V_j = Z_j G Z_j' + diag(sigma2_v + D_ij), maximized by REML (or ML)
  over block-diagonal V. With one area per group, ``re_formula=None`` and
  ``area_effect=False`` this is exactly the Fay-Herriot model, and the
  estimates equal census.sae.fay_herriot (tested). With D = 0 it is the
  ordinary linear mixed model, and it reproduces statsmodels' documented
  MixedLM example (dietox; tested).

Interval targets (PLAN_ROUND_10 decision 4; census.models.INTERVAL_TARGETS):
"population" (default) uses ``se_model``, the GLS model SE, which with known
variances already includes ACS sampling error; "these_areas" uses
``se_sampling`` from 80 replicate refits (needs replicates) for the fixed
effects among these areas in this period. ``replicate_refit="gls"`` (default)
holds the variance components at their full-sample values in the refits
(beta_r = (X'V^-1X)^-1 X'V^-1 y_r, the linearization of the estimator);
``"full"`` re-estimates them in every refit. Coverage of both targets is
validated in census.simulation (studies ``multilevel_*``).
"""

import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import patsy
from scipy import optimize

from .analysis import formula_variables
from .models import (
    DEFAULT_CV_THRESHOLD, INTERVAL_TARGETS, _replicate_matrices, _screen_summary, _table,
)
from .results import SOURCE_MODEL, SOURCE_SAMPLING_MODEL, SOURCE_SAMPLING_REPLICATE, ModelResult
from .variance import REPLICATE_COUNT, REPLICATE_FACTOR

METHODS = ("REML", "ML")
REPLICATE_REFITS = ("gls", "full")
REFERENCES = [
    "Fay, R.E. & Herriot, R.A. (1979). JASA 74(366), 269-277.",
    "Rao, J.N.K. & Molina, I. (2015). Small Area Estimation, 2nd ed. Wiley, ch. 6 and 8.",
    "Lindstrom, M.J. & Bates, D.M. (1988). JASA 83(404), 1014-1022 (REML for linear mixed models).",
    "statsmodels MixedLM documentation, 'Linear Mixed Effects Models' example (dietox data, "
    "Lauridsen et al. 1999 via R geepack).",
]


# ---------------------------------------------------------------------------
# Block-diagonal GLS / REML engine (also used by census.did)
# ---------------------------------------------------------------------------


def _spd_inverse(L: np.ndarray) -> np.ndarray:
    """Inverse of V = L L' for a (m, n, n) stack of Cholesky factors.

    Forward substitution vectorized over the stack; numpy's batched inv/solve
    are 50-100x slower than batched Cholesky for small blocks on some builds.
    """
    m, n, _ = L.shape
    Linv = np.zeros_like(L)
    eye = np.eye(n)
    for i in range(n):
        acc = np.einsum("mk,mkj->mj", L[:, i, :i], Linv[:, :i, :]) if i else 0.0
        Linv[:, i, :] = (eye[i] - acc) / L[:, i, i][:, None]
    return np.einsum("mki,mkj->mij", Linv, Linv)


class BlockGLS:
    """Gaussian likelihood for y ~ N(X beta, blockdiag(V_j(theta))).

    Blocks of equal size are stacked so V is built and factored in batches.
    ``vfun(theta, cls)`` returns the (m, n, n) covariance stack for a size
    class ``cls`` (a dict with "D" (m, n), "Z" (m, n, q), "idx" (m, n), and
    any extra arrays added by the caller).
    """

    def __init__(self, groups: np.ndarray, y: np.ndarray, X: np.ndarray,
                 vfun: Callable[[np.ndarray, Dict[str, np.ndarray]], np.ndarray],
                 D: Optional[np.ndarray] = None, Z: Optional[np.ndarray] = None,
                 extra: Optional[Dict[str, np.ndarray]] = None):
        self.y = np.asarray(y, float)
        self.X = np.asarray(X, float)
        self.n, self.p = self.X.shape
        self.vfun = vfun
        D = np.zeros(self.n) if D is None else np.asarray(D, float)
        Z = np.ones((self.n, 1)) if Z is None else np.asarray(Z, float)
        codes, _ = pd.factorize(pd.Series(groups).astype(str), sort=False)
        order = np.argsort(codes, kind="stable")
        bounds = np.flatnonzero(np.diff(codes[order])) + 1
        members = np.split(order, bounds)
        by_size: Dict[int, List[np.ndarray]] = {}
        for m in members:
            by_size.setdefault(len(m), []).append(m)
        self.n_groups = len(members)
        self.group_sizes = np.array([len(m) for m in members])
        self.classes = []
        for size, ms in sorted(by_size.items()):
            idx = np.vstack(ms)
            cls = {"idx": idx, "D": D[idx], "Z": Z[idx]}
            for k, v in (extra or {}).items():
                cls[k] = np.asarray(v)[idx]
            self.classes.append(cls)

    def _solve(self, theta: np.ndarray, y: Optional[np.ndarray] = None, X: Optional[np.ndarray] = None,
               keep: bool = False) -> Dict[str, Any]:
        y = self.y if y is None else y
        X = self.X if X is None else X
        XtVX = np.zeros((self.p, self.p))
        XtVy = np.zeros(self.p)
        logdet = 0.0
        vinv = []
        parts = []
        for cls in self.classes:
            V = self.vfun(theta, cls)
            L = np.linalg.cholesky(V)  # raises LinAlgError when not positive definite
            logdet += 2.0 * float(np.log(np.diagonal(L, axis1=1, axis2=2)).sum())
            Xc, yc = X[cls["idx"]], y[cls["idx"]]
            Vi = _spd_inverse(L)
            ViX = Vi @ Xc
            Viy = np.einsum("mnk,mk->mn", Vi, yc)
            XtVX += np.einsum("mnp,mnq->pq", Xc, ViX)
            XtVy += np.einsum("mnp,mn->p", Xc, Viy)
            parts.append((yc, Viy, ViX))
            if keep:
                vinv.append(Vi)
        Q = np.linalg.inv(XtVX)
        beta = Q @ XtVy
        # r'V^-1 r = y'V^-1y - 2 b'X'V^-1y + b'X'V^-1Xb = y'V^-1y - b'X'V^-1y
        yVy = sum(float(np.einsum("mn,mn->", yc, Viy)) for yc, Viy, _ in parts)
        quad = yVy - float(beta @ XtVy)
        out = {"beta": beta, "Q": Q, "logdet": logdet, "quad": quad, "XtVX": XtVX}
        if keep:
            out["vinv"] = vinv
        return out

    def loglik(self, theta: np.ndarray, method: str = "REML", y: Optional[np.ndarray] = None) -> float:
        try:
            with np.errstate(all="ignore"):
                s = self._solve(theta, y)
        except np.linalg.LinAlgError:
            return -np.inf
        n_eff = self.n - (self.p if method == "REML" else 0)
        ll = -0.5 * (n_eff * np.log(2 * np.pi) + s["logdet"] + s["quad"])
        if method == "REML":
            ll -= 0.5 * float(np.linalg.slogdet(s["XtVX"])[1])
        return float(ll) if np.isfinite(ll) else -np.inf

    def gls(self, theta: np.ndarray, y: np.ndarray, X: Optional[np.ndarray] = None,
            vinv: Optional[List[np.ndarray]] = None) -> np.ndarray:
        """beta for new data with V fixed at theta (replicate refits)."""
        X = self.X if X is None else X
        if vinv is None:
            return self._solve(theta, y, X)["beta"]
        XtVX = np.zeros((self.p, self.p))
        XtVy = np.zeros(self.p)
        for cls, Vi in zip(self.classes, vinv):
            Xc, yc = X[cls["idx"]], y[cls["idx"]]
            ViX = Vi @ Xc
            XtVX += np.einsum("mnp,mnq->pq", Xc, ViX)
            XtVy += np.einsum("mnp,mn->p", ViX, yc)
        return np.linalg.solve(XtVX, XtVy)

    def sandwich_sampling_cov(self, vinv: List[np.ndarray], Q: np.ndarray) -> np.ndarray:
        """Q X'V^-1 diag(D) V^-1 X Q: covariance of beta from the known sampling error alone."""
        M = np.zeros((self.p, self.p))
        for cls, Vi in zip(self.classes, vinv):
            W = Vi @ self.X[cls["idx"]]  # (m, n, p)
            M += np.einsum("mnp,mn,mnq->pq", W, cls["D"], W)
        return Q @ M @ Q


# ---------------------------------------------------------------------------
# Known-variance multilevel model
# ---------------------------------------------------------------------------


def _unpack(theta: np.ndarray, q: int, area_effect: bool, c: float) -> Tuple[np.ndarray, float]:
    L = np.zeros((q, q))
    k = 0
    for i in range(q):
        for j in range(i + 1):
            L[i, j] = np.exp(theta[k]) if i == j else theta[k]
            k += 1
    G = c * (L @ L.T)
    s2 = c * float(np.exp(2 * theta[k])) if area_effect else 0.0
    return G, s2


def _known_vfun(q: int, area_effect: bool, c: float):
    def vfun(theta, cls):
        G, s2 = _unpack(theta, q, area_effect, c)
        Z = cls["Z"]
        V = np.einsum("mnq,qr,mkr->mnk", Z, G, Z)
        n = Z.shape[1]
        V[:, np.arange(n), np.arange(n)] += s2 + cls["D"]
        return V
    return vfun


def _n_theta(q: int, area_effect: bool) -> int:
    return q * (q + 1) // 2 + (1 if area_effect else 0)


def _fit_known(eng: BlockGLS, q: int, area_effect: bool, c: float, method: str,
               start: Optional[np.ndarray] = None) -> Dict[str, Any]:
    k = _n_theta(q, area_effect)
    diag_pos = []
    pos = 0
    for i in range(q):
        for j in range(i + 1):
            if i == j:
                diag_pos.append(pos)
            pos += 1
    log_pos = set(diag_pos) | ({k - 1} if area_effect else set())
    bounds = [(-12.0, 6.0) if i in log_pos else (-50.0, 50.0) for i in range(k)]
    starts = [start] if start is not None else []
    base = np.zeros(k)
    for i in log_pos:
        base[i] = np.log(np.sqrt(0.5))
    starts.append(base)
    alt = base.copy()
    for i in log_pos:
        alt[i] = np.log(0.1)
    starts.append(alt)
    best = None
    for s0 in starts:
        def nll(t):
            v = eng.loglik(t, method)
            return -v if np.isfinite(v) else 1e300
        res = optimize.minimize(nll, np.asarray(s0, float), method="L-BFGS-B", bounds=bounds)
        if best is None or res.fun < best.fun:
            best = res
    theta = best.x
    G, s2 = _unpack(theta, q, area_effect, c)
    return {"theta": theta, "G": G, "s2": s2, "loglik": -float(best.fun), "converged": bool(best.success),
            "message": str(best.message)}


def kenward_roger(eng: BlockGLS, theta: np.ndarray, q: int, include_s2: bool) -> Dict[str, Any]:
    """Kenward-Roger (1997) small-sample adjustment for the fixed effects.

    V = sum_k psi_k M_k + diag(D) is linear in the variance parameters psi
    (entries of G, and sigma2_v), so the KR second-derivative term vanishes.
    Returns the bias-adjusted covariance Phi_A = Phi + 2 Lambda (Kenward &
    Roger 1997, Biometrics 53:983-997, eq. 4-5) with W = inverse REML expected
    information, and Satterthwaite (1946) degrees of freedom per coefficient,
    df_j = 2 Phi_A,jj^2 / (g' W g), g_k = d Phi_jj / d psi_k (the scalar-contrast
    form used by e.g. R lmerTest/pbkrtest for single coefficients).
    """
    sol = eng._solve(theta, keep=True)
    Phi = sol["Q"]
    p = eng.p
    pairs = [(a, b) for a in range(q) for b in range(a + 1)]
    K = len(pairs) + (1 if include_s2 else 0)
    T1 = np.zeros((K, K))
    C = np.zeros((K, p, p))
    Qkl = np.zeros((K, K, p, p))
    for cls, Vi in zip(eng.classes, sol["vinv"]):
        Z = cls["Z"]
        n = Z.shape[1]
        Ms = []
        for a, b in pairs:
            M = np.einsum("mi,mj->mij", Z[:, :, a], Z[:, :, b])
            Ms.append(M + M.transpose(0, 2, 1) if a != b else M)
        if include_s2:
            Ms.append(np.broadcast_to(np.eye(n), (Z.shape[0], n, n)))
        ViX = Vi @ eng.X[cls["idx"]]
        A = [Vi @ M for M in Ms]
        MViX = [M @ ViX for M in Ms]
        for k in range(K):
            C[k] += np.einsum("mnp,mnq->pq", ViX, MViX[k])
            for l in range(k + 1):
                T1[k, l] += float(np.einsum("mij,mji->", A[k], A[l]))
                Qkl[k, l] += np.einsum("mnp,mnk,mkq->pq", MViX[k], Vi, MViX[l])
    info = np.zeros((K, K))
    for k in range(K):
        for l in range(k + 1):
            v = 0.5 * (T1[k, l] - 2 * np.trace(Phi @ Qkl[k, l]) + np.trace(Phi @ C[k] @ Phi @ C[l]))
            info[k, l] = info[l, k] = v
            if k != l:
                Qkl[l, k] = Qkl[k, l].T
    W = np.linalg.pinv(info)
    inner = np.zeros((p, p))
    for k in range(K):
        for l in range(K):
            inner += W[k, l] * (Qkl[k, l] - C[k] @ Phi @ C[l])
    Phi_A = Phi + 2 * Phi @ inner @ Phi
    dPhi = np.stack([Phi @ C[k] @ Phi for k in range(K)])  # d Phi / d psi_k
    df = np.empty(p)
    for j in range(p):
        g = dPhi[:, j, j]
        den = float(g @ W @ g)
        df[j] = 2 * Phi_A[j, j] ** 2 / den if den > 0 else np.inf
    return {"cov": Phi_A, "cov_unadjusted": Phi, "df": df, "var_cov_psi": W}


def _t_table(table: pd.DataFrame, df: np.ndarray, confidence_level: float) -> pd.DataFrame:
    """Replace normal intervals/p-values by t(df) for the population target."""
    from scipy import stats as sps
    out = table.copy()
    se = out["se_model"].to_numpy(float)
    est = out["estimate"].to_numpy(float)
    tq = sps.t.ppf(0.5 + confidence_level / 2, df)
    out["ci_lower"], out["ci_upper"] = est - tq * se, est + tq * se
    with np.errstate(divide="ignore", invalid="ignore"):
        out["p_value"] = 2 * sps.t.sf(np.abs(est / se), df)
    out["df"] = df
    return out


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def _design(formula: str, re_formula: Optional[str], data: pd.DataFrame):
    y, X = patsy.dmatrices(formula, data, return_type="dataframe", NA_action="raise")
    Z = patsy.dmatrix(re_formula or "1", data, return_type="dataframe", NA_action="raise")
    return y.iloc[:, 0].to_numpy(float), X, Z


def _complete(frame: pd.DataFrame, formula: str, re_formula: Optional[str], extra: Sequence[str]) -> pd.DataFrame:
    cols = set(formula_variables(formula, frame)) | set(extra)
    if re_formula:
        cols |= set(formula_variables("y ~ " + re_formula.lstrip("~ "), frame)) - {"y"}
    cols = [c for c in cols if c in frame.columns]
    return frame.dropna(subset=cols).reset_index(drop=True)


def fit_multilevel(
    formula: str,
    frame: pd.DataFrame,
    groups: str,
    re_formula: Optional[str] = None,
    sampling_se: Optional[Union[str, bool]] = None,
    area_effect: bool = True,
    replicates: Optional[pd.DataFrame] = None,
    target: str = "population",
    confidence_level: float = 0.90,
    method: str = "REML",
    replicate_refit: str = "gls",
    max_failed_replicates: int = 0,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
    small_sample: Optional[str] = "kenward_roger",
) -> ModelResult:
    """Random-intercept (and random-slope) model with optional known level-1 variance.

    ``groups``: grouping column (e.g. county GEOID for tracts, PUMA for people).
    ``re_formula``: random-effects formula, e.g. "~x" for a random slope on x
    (default random intercept only). ``sampling_se``: column holding the ACS
    SE of the outcome (``True`` means "<outcome>_se"); when given, the
    Fay-Herriot-style likelihood is used, otherwise statsmodels MixedLM.
    ``area_effect``: include an area-level model error sigma2_v beyond the
    sampling error (known-variance model only; turning it off assumes all
    within-group residual variation is sampling error).
    ``replicates``: long replicate frame (census.analysis.attach_replicates);
    every formula variable found there is replaced in each refit.
    ``target``: "population" (default), "these_areas" (needs replicates) or
    "conservative" (census.models.INTERVAL_TARGETS).
    ``small_sample``: "kenward_roger" (default) makes se_model the
    Kenward-Roger adjusted SE and bases population intervals and p-values on
    t with Satterthwaite df (column ``df``; unadjusted SEs in
    ``diagnostics['se_model_unadjusted']``); None keeps the asymptotic
    normal intervals. Full simulation (nominal 0.90): coverage 0.891 random
    intercept, 0.890 random slope with KR, vs 0.878 / 0.874 with normal
    intervals.
    """
    method = method.upper()
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if target not in INTERVAL_TARGETS:
        raise ValueError(f"target must be one of {INTERVAL_TARGETS}")
    if target == "these_areas" and (replicates is None or not len(replicates)):
        raise ValueError("target='these_areas' needs replicates (se_sampling)")
    if replicate_refit not in REPLICATE_REFITS:
        raise ValueError(f"replicate_refit must be one of {REPLICATE_REFITS}")
    outcome = formula.split("~")[0].strip()
    se_col = f"{outcome}_se" if sampling_se is True else (sampling_se or None)
    if se_col and se_col not in frame.columns:
        raise KeyError(f"sampling SE column {se_col!r} not in frame")
    if groups not in frame.columns:
        raise KeyError(f"groups column {groups!r} not in frame")
    data = _complete(frame, formula, re_formula, [groups] + ([se_col] if se_col else []))
    notes: List[str] = []
    if len(data) < len(frame):
        notes.append(f"{len(frame) - len(data)} row(s) with missing values dropped")
    y, Xdf, Zdf = _design(formula, re_formula, data)
    X, Z = Xdf.to_numpy(float), Zdf.to_numpy(float)
    terms = list(Xdf.columns)
    re_terms = list(Zdf.columns)
    q = Z.shape[1]
    g = data[groups].astype(str).to_numpy()
    diagnostics: Dict[str, Any] = {"reliability_screen": _screen_summary(data, formula, cv_threshold)}
    provenance: Dict[str, Any] = {"groups": groups, "re_formula": re_formula or "~1", "method": method,
                                  "references": REFERENCES}

    if se_col:
        D = data[se_col].to_numpy(float) ** 2
        if np.any(D < 0) or not np.isfinite(D).all():
            raise ValueError("sampling SEs must be finite and non-negative")
        ols = np.linalg.lstsq(X, y, rcond=None)[0]
        c = max(float(np.var(y - X @ ols, ddof=X.shape[1])) - float(D.mean()), 0.1 * float(np.var(y)), 1e-12)
        vfun = _known_vfun(q, area_effect, c)
        eng = BlockGLS(g, y, X, vfun, D=D, Z=Z)
        fit = _fit_known(eng, q, area_effect, c, method)
        theta = fit["theta"]
        sol = eng._solve(theta, keep=True)
        params = pd.Series(sol["beta"], index=terms)
        se_model = pd.Series(np.sqrt(np.diag(sol["Q"])), index=terms)
        sources = [SOURCE_MODEL, SOURCE_SAMPLING_MODEL]
        model_type = "multilevel_known_variance"
        vc = {"group_cov": pd.DataFrame(fit["G"], index=re_terms, columns=re_terms).to_dict(),
              "area_var": fit["s2"] if area_effect else None}
        diagnostics.update({"loglik": fit["loglik"], "converged": fit["converged"], "optimizer": fit["message"]})
        S = eng.sandwich_sampling_cov(sol["vinv"], sol["Q"])
        diagnostics["se_sampling_analytic"] = dict(zip(terms, np.sqrt(np.clip(np.diag(S), 0, None)).tolist()))
        notes.append("Fay-Herriot-style likelihood: sampling variances D = SE^2 treated as known; se_model "
                     "includes ACS sampling error of the outcome")
        if not fit["converged"]:
            notes.append(f"optimizer did not report convergence: {fit['message']}")
        state = {"eng": eng, "theta": theta, "vinv": sol["vinv"], "c": c}
        provenance.update({"variance_parameters": theta.tolist(), "variance_scale": c, "area_effect": area_effect})
    else:
        import statsmodels.formula.api as smf
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            md = smf.mixedlm(formula, data, groups=data[groups], re_formula=re_formula)
            res = md.fit(reml=(method == "REML"))
        params = res.fe_params.reindex(terms)
        se_model = res.bse_fe.reindex(terms)
        sources = [SOURCE_MODEL]
        model_type = "multilevel_mixedlm"
        G = np.asarray(res.cov_re, float)
        vc = {"group_cov": pd.DataFrame(G, index=re_terms, columns=re_terms).to_dict(), "residual_var": float(res.scale)}
        diagnostics.update({"loglik": float(res.llf), "converged": bool(res.converged)})
        conv = sorted({str(w.message).split("\n")[0] for w in caught if "converge" in str(w.message).lower()})
        if not res.converged or conv:
            notes.append("statsmodels MixedLM convergence warnings: " + "; ".join(conv[:3]))
        provenance["model_library"] = "statsmodels MixedLM"
        if replicates is None or not len(replicates):
            notes.append("no replicates and no sampling_se: SEs reflect model error only")
        # V = Z G Z' + scale I in the same parameterization, for GLS replicate refits.
        c = 1.0
        Lg = np.linalg.cholesky(G + 1e-12 * np.eye(q))
        theta = []
        for i in range(q):
            for j in range(i + 1):
                theta.append(np.log(Lg[i, j]) if i == j else Lg[i, j])
        theta.append(0.5 * np.log(res.scale))
        theta = np.asarray(theta)
        eng = BlockGLS(g, y, X, _known_vfun(q, True, c), D=np.zeros(len(y)), Z=Z)
        state = {"eng": eng, "theta": theta, "vinv": None, "c": c, "mixedlm": md}

    diagnostics["variance_components"] = vc
    diagnostics["n_groups"] = int(pd.Series(g).nunique())
    se_sampling = pd.Series(np.nan, index=terms)
    method_label = "full_sample_fit"
    if replicates is not None and len(replicates):
        se_sampling, rep_notes, rep_diag = _replicate_se(formula, re_formula, data, replicates, params, state,
                                                         se_col, area_effect, q, method, replicate_refit,
                                                         max_failed_replicates, groups)
        notes += rep_notes
        diagnostics.update(rep_diag)
        sources = sources + [SOURCE_SAMPLING_REPLICATE]
        method_label = f"replicate_refit_{replicate_refit}"
    kr = None
    if small_sample == "kenward_roger":
        include_s2 = area_effect if se_col else True
        try:
            kr = kenward_roger(state["eng"], state["theta"], q, include_s2)
        except np.linalg.LinAlgError:
            notes.append("Kenward-Roger adjustment failed (singular information); normal intervals used")
        if kr is not None:
            diagnostics["se_model_unadjusted"] = dict(zip(terms, se_model.tolist()))
            se_model = pd.Series(np.sqrt(np.diag(kr["cov"])), index=terms)
            diagnostics["df"] = dict(zip(terms, kr["df"].tolist()))
            diagnostics["small_sample"] = "kenward_roger (Kenward & Roger 1997) with Satterthwaite df"
    elif small_sample is not None:
        raise ValueError("small_sample must be 'kenward_roger' or None")
    table = _table(params, se_model, se_sampling, confidence_level, target)
    if kr is not None and target == "population":
        table = _t_table(table, kr["df"], confidence_level)
        notes.append("population intervals: Kenward-Roger adjusted SEs, t with Satterthwaite df")
    diagnostics["interval_target"] = target
    notes.append(f"intervals and p-values use the '{target}' target")
    return ModelResult(model_type=model_type, formula=formula + f" | {groups}" + (f" ({re_formula})" if re_formula else ""),
                       n_obs=int(len(y)), coefficients=table, method=method_label, uncertainty_sources=sources,
                       confidence_level=confidence_level, notes=notes, diagnostics=diagnostics, provenance=provenance)


def _replicate_se(formula, re_formula, data, replicates, params, state, se_col, area_effect, q, method,
                  replicate_refit, max_failed, groups):
    used = formula_variables(formula, data)
    mats, fixed = _replicate_matrices(data, replicates, used)
    if not mats:
        raise ValueError("no formula variable has replicates in the replicate frame")
    eng: BlockGLS = state["eng"]
    theta = state["theta"]
    vinv = state["vinv"]
    if replicate_refit == "gls" and vinv is None:
        vinv = eng._solve(theta, keep=True)["vinv"]
    covariate_reps = any(v in mats for v in used if v != formula.split("~")[0].strip())
    coefs, failed = [], 0
    for r in range(REPLICATE_COUNT):
        d = data.copy()
        for v, m in mats.items():
            d[v] = m[:, r]
        try:
            if replicate_refit == "gls":
                yr, Xr, _ = _design(formula, re_formula, d)
                b = eng.gls(theta, yr, Xr.to_numpy(float) if covariate_reps else None, vinv)
            elif se_col:
                yr, Xr, Zr = _design(formula, re_formula, d)
                e2 = BlockGLS(d[groups].astype(str).to_numpy(), yr, Xr.to_numpy(float),
                              _known_vfun(q, area_effect, state["c"]), D=d[se_col].to_numpy(float) ** 2,
                              Z=Zr.to_numpy(float))
                f2 = _fit_known(e2, q, area_effect, state["c"], method, start=theta)
                b = e2._solve(f2["theta"])["beta"]
            else:
                import statsmodels.formula.api as smf
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    b = smf.mixedlm(formula, d, groups=d[groups], re_formula=re_formula).fit(
                        reml=(method == "REML")).fe_params.reindex(params.index).to_numpy(float)
            if not np.isfinite(b).all():
                raise ValueError("non-finite coefficients")
            coefs.append(np.asarray(b, float))
        except Exception:  # noqa: BLE001 - refit failures are counted and reported
            failed += 1
    if failed > max_failed:
        raise RuntimeError(f"{failed} of 80 replicate refits failed (max_failed_replicates={max_failed})")
    B = np.vstack(coefs)
    b0 = params.to_numpy(float)
    se = pd.Series(np.sqrt(REPLICATE_FACTOR * ((B - b0) ** 2).sum(axis=0)), index=params.index)
    notes = []
    if replicate_refit == "gls":
        notes.append("replicate refits hold variance components at full-sample values (GLS linearization)")
    if failed:
        notes.append(f"{failed} replicate refit(s) failed and were omitted; se_sampling may be understated")
    if fixed:
        notes.append(f"rows without replicates held at full-sample values (understates sampling SE): {fixed}")
    return se, notes, {"replicate_failures": failed, "replicate_variables": sorted(mats)}
