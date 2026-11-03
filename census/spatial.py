# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spatial weights, autocorrelation and spatial regression with ACS sampling error.

PLAN_ROUND_9.md workstream C. Built on PySAL (``libpysal``, ``esda``, ``spreg``).

* :func:`spatial_weights` - queen/rook contiguity, k-nearest neighbors and
  distance bands from a GeoDataFrame, with an island (no-neighbor) report and
  optional caching (in process, or GAL files in a directory).
* :func:`morans_i` - global Moran's I with permutation inference (esda) and,
  given the 80 ACS replicate maps, a design-based SE for I:
  I is recomputed on each replicate map and Var(I) = 4/80 sum_r (I_r - I)^2
  (ACS successive-difference replication; census.variance). The replicate SE
  describes how much I would vary over repeated ACS samples of these areas;
  it does not remove the attenuation of I by sampling noise (noisy estimates
  pull I toward E[I] = -1/(n-1)). Validated by simulation as conservative:
  the SE is exact as CVs shrink but 10-50% too large under ACS-like CVs. Testing on Fay-Herriot EBLUPs
  (census.sae / census.sae_spatial) is the recommended sensitivity analysis.
* :func:`local_morans` - LISA (Anselin 1995) with conditional permutation
  p-values corrected for multiple comparisons (census.multitest; BH by
  default), plus the share of replicate maps that keep each area's quadrant.
* :func:`spatial_regression` - spatial lag / spatial error models by ML or
  GMM (spreg), returned as :class:`census.results.ModelResult` with the OLS
  Lagrange-multiplier diagnostics that suggest which model fits, and replicate
  refits (80) for ACS sampling SEs.

References: Moran (1950) Biometrika 37:17-23; Anselin (1995) Geographical
Analysis 27:93-115; Anselin, Bera, Florax & Yoon (1996) Regional Science and
Urban Economics 26:77-104; Kelejian & Prucha (1998, 2010); Rey & Anselin (2007)
PySAL; ACS Variance Replicate Estimate Tables documentation.
"""

import hashlib
import math
import os
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd
from scipy import sparse, stats as sps

from .results import SOURCE_MODEL, SOURCE_SAMPLING_REPLICATE, ModelResult, _jsonable
from .variance import REPLICATE_COLUMNS, REPLICATE_COUNT, REPLICATE_FACTOR

WEIGHT_KINDS = ("queen", "rook", "knn", "distance")
SPATIAL_MODELS = ("lag", "error")
SPATIAL_METHODS = ("ml", "gm")
_CACHE: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


def _cache_key(gdf, id_col: str, kind: str, params: Dict[str, Any]) -> str:
    h = hashlib.sha1()
    h.update(repr((kind, sorted(params.items()), str(gdf.crs))).encode())
    h.update("\x1f".join(map(str, gdf[id_col])).encode())
    for wkb in gdf.geometry.to_wkb():
        h.update(wkb)
    return h.hexdigest()[:20]


def weights_report(w) -> Dict[str, Any]:
    """Neighbor-count summary and island (no-neighbor) list for a libpysal W."""
    card = np.array([w.cardinalities[i] for i in w.id_order])
    islands = list(map(str, w.islands))
    return {
        "n": int(w.n), "transform": str(w.transform), "n_islands": len(islands),
        "islands": islands[:100], "islands_truncated": len(islands) > 100,
        "neighbors_min": int(card.min()) if len(card) else 0,
        "neighbors_mean": float(card.mean()) if len(card) else 0.0,
        "neighbors_max": int(card.max()) if len(card) else 0,
        "n_components": int(getattr(w, "n_components", 1)),
        "pct_nonzero": float(w.pct_nonzero),
    }


def spatial_weights(
    gdf,
    kind: str = "queen",
    *,
    k: int = 5,
    threshold: Optional[float] = None,
    id_col: str = "GEOID",
    transform: str = "r",
    cache: Union[None, bool, str, os.PathLike] = None,
):
    """Build spatial weights from a GeoDataFrame (libpysal).

    ``kind``: ``queen`` / ``rook`` contiguity, ``knn`` (``k`` nearest
    centroids) or ``distance`` (binary band with ``threshold`` in CRS units).
    knn and distance need a projected CRS (e.g. ``gdf.to_crs(5070)``) and
    raise on geographic coordinates. Weights are row-standardized by default
    (``transform='r'``). ``cache``: ``True`` keeps weights in process;
    a directory path stores GAL files keyed by a hash of ids, geometry, kind,
    parameters and CRS. The returned W carries ``w.census_report``
    (:func:`weights_report` plus kind, parameters, CRS and cache status);
    islands (areas without neighbors) are listed there and are excluded from
    nothing silently - downstream statistics note them.
    """
    import libpysal.weights as lw

    kind = kind.lower()
    if kind not in WEIGHT_KINDS:
        raise ValueError(f"kind must be one of {WEIGHT_KINDS}")
    if id_col not in gdf.columns:
        raise KeyError(f"id column {id_col!r} not in frame")
    if gdf[id_col].duplicated().any():
        raise ValueError(f"duplicate ids in {id_col!r}")
    if gdf.geometry.isna().any() or gdf.geometry.is_empty.any():
        bad = gdf.loc[gdf.geometry.isna() | gdf.geometry.is_empty, id_col].astype(str).tolist()
        raise ValueError(f"{len(bad)} area(s) have missing/empty geometry: {bad[:20]}")
    params: Dict[str, Any] = {}
    if kind == "knn":
        params["k"] = int(k)
    if kind == "distance":
        if threshold is None:
            raise ValueError("kind='distance' needs threshold (CRS units)")
        params["threshold"] = float(threshold)
    if kind in ("knn", "distance") and gdf.crs is not None and gdf.crs.is_geographic:
        raise ValueError(f"kind={kind!r} needs a projected CRS (distances in degrees are not meaningful); "
                         "reproject first, e.g. gdf.to_crs(5070) for CONUS Albers")
    gdf = gdf.assign(**{id_col: gdf[id_col].astype(str)})
    key = _cache_key(gdf, id_col, kind, params) if cache else None
    cache_status = "off"
    w = None
    if cache is True and key in _CACHE:
        w, cache_status = _CACHE[key], "memory_hit"
    elif cache not in (None, False, True):
        path = os.path.join(os.fspath(cache), f"weights_{kind}_{key}.gal")
        if os.path.exists(path):
            import libpysal
            w = libpysal.io.open(path, "r").read()
            # GAL ids are read back as strings in file order; restore frame order.
            w = lw.W({i: w.neighbors[i] for i in gdf[id_col]}, id_order=list(gdf[id_col]), silence_warnings=True)
            cache_status = "disk_hit"
    if w is None:
        ids = list(gdf[id_col])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if kind == "queen":
                w = lw.Queen.from_dataframe(gdf, ids=ids, use_index=False, silence_warnings=True)
            elif kind == "rook":
                w = lw.Rook.from_dataframe(gdf, ids=ids, use_index=False, silence_warnings=True)
            elif kind == "knn":
                w = lw.KNN.from_dataframe(gdf, k=int(k), ids=ids, use_index=False)
            else:
                w = lw.DistanceBand.from_dataframe(gdf, threshold=float(threshold), ids=ids, use_index=False,
                                                   binary=True, silence_warnings=True)
        if cache is True:
            _CACHE[key] = w
            cache_status = "memory_stored"
        elif cache not in (None, False):
            import libpysal
            os.makedirs(os.fspath(cache), exist_ok=True)
            path = os.path.join(os.fspath(cache), f"weights_{kind}_{key}.gal")
            w0 = lw.W(dict(w.neighbors), id_order=list(w.id_order), silence_warnings=True)
            fh = libpysal.io.open(path, "w")
            try:
                fh.write(w0)
            finally:
                fh.close()
            cache_status = "disk_stored"
    # Work on a copy so transforms on a cached object do not leak.
    w = lw.W(dict(w.neighbors), id_order=list(w.id_order), silence_warnings=True)
    w.transform = transform
    report = weights_report(w)
    report.update({"kind": kind, "params": params, "crs": str(gdf.crs) if gdf.crs is not None else None,
                   "cache": cache_status, "cache_key": key, "id_col": id_col})
    w.census_report = report
    return w


# ---------------------------------------------------------------------------
# Alignment helpers
# ---------------------------------------------------------------------------


def _aligned(data: pd.DataFrame, var: Optional[str], w, geoid_col: str) -> pd.DataFrame:
    """Rows of ``data`` reordered to ``w.id_order`` (refuses missing/extra/non-finite)."""
    if var is not None and var not in data.columns:
        raise KeyError(f"{var!r} not in frame")
    ids = [str(i) for i in w.id_order]
    d = pd.DataFrame(data).assign(_gid=data[geoid_col].astype(str)) if geoid_col in data.columns else None
    if d is None:
        raise KeyError(f"id column {geoid_col!r} not in frame")
    if d["_gid"].duplicated().any():
        raise ValueError(f"duplicate {geoid_col} values in frame")
    d = d.set_index("_gid")
    missing = [i for i in ids if i not in d.index]
    if missing:
        raise ValueError(f"{len(missing)} weights id(s) missing from the frame: {missing[:20]}")
    extra = sorted(set(d.index) - set(ids))
    d = d.loc[ids]
    vals = pd.to_numeric(d[var], errors="coerce").to_numpy(float) if var is not None else np.zeros(len(d))
    if not np.isfinite(vals).all():
        bad = list(np.asarray(ids)[~np.isfinite(vals)][:20])
        raise ValueError(f"{var!r} is missing/non-finite for {int((~np.isfinite(vals)).sum())} area(s): {bad}. "
                         "Subset the frame and rebuild the weights (libpysal.weights.w_subset) explicitly.")
    d.attrs["extra_rows_dropped"] = extra
    return d


def _replicate_matrix(replicates: pd.DataFrame, var: str, ids: Sequence[str], geoid_col: str) -> np.ndarray:
    sub = replicates[replicates["variable"] == var]
    if not len(sub):
        raise ValueError(f"no replicates for variable {var!r}")
    sub = sub.assign(_gid=sub[geoid_col].astype(str)).drop_duplicates("_gid").set_index("_gid")
    m = sub[REPLICATE_COLUMNS].reindex(list(ids)).to_numpy(float)
    bad = ~np.isfinite(m).all(axis=1)
    if bad.any():
        raise ValueError(f"{int(bad.sum())} area(s) lack complete replicates for {var!r}: "
                         f"{list(np.asarray(ids)[bad][:20])}")
    return m  # (n, 80)


def _moran_vector(Y: np.ndarray, W: sparse.spmatrix) -> np.ndarray:
    """Moran's I for each column of Y (n, k) under sparse weights W (as esda)."""
    Z = Y - Y.mean(axis=0, keepdims=True)
    num = np.sum(Z * (W @ Z), axis=0)
    den = np.sum(Z * Z, axis=0)
    return Y.shape[0] / W.sum() * num / den


def _z(level: float) -> float:
    level = level if level <= 1 else level / 100
    return float(sps.norm.ppf(0.5 + level / 2))


# ---------------------------------------------------------------------------
# Global and local Moran's I
# ---------------------------------------------------------------------------


@dataclass
class SpatialAutocorrelationResult:
    """Global Moran's I with permutation and (optionally) replicate inference."""

    variable: str
    I: float
    expected_I: float
    p_norm: float
    p_sim: Optional[float]
    z_sim: Optional[float]
    permutations: int
    se_replicate: Optional[float] = None
    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    confidence_level: float = 0.90
    replicate_I: Optional[List[float]] = None
    n: int = 0
    notes: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(self.__dict__)


def morans_i(
    data: pd.DataFrame,
    var: str,
    weights,
    *,
    permutations: int = 999,
    replicates: Optional[pd.DataFrame] = None,
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
    seed: Optional[int] = 12345,
) -> SpatialAutocorrelationResult:
    """Global Moran's I of ``var`` (esda.Moran, row-standardized weights).

    Permutation inference (``permutations``) tests spatial randomness of the
    published estimates. With ``replicates`` (long frame: GEOID, variable,
    rep_1..rep_80) the statistic is recomputed on each replicate map and
    ``se_replicate`` = sqrt(4/80 sum (I_r - I)^2) gives a design-based interval
    I +/- z SE for the expected Moran's I of ACS estimates for these areas.
    Sampling noise attenuates I toward -1/(n-1); the interval is not for the
    I of the (unobserved) true values. Use EBLUPs as a sensitivity analysis.
    """
    from esda.moran import Moran

    d = _aligned(data, var, weights, geoid_col)
    y = d[var].to_numpy(float)
    notes: List[str] = []
    if weights.islands:
        notes.append(f"{len(weights.islands)} island(s) (no neighbors) contribute zero spatial lag")
    if d.attrs.get("extra_rows_dropped"):
        notes.append(f"{len(d.attrs['extra_rows_dropped'])} frame row(s) not in the weights were ignored")
    if seed is not None:
        np.random.seed(seed)  # esda draws permutations from numpy's global RNG
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mi = Moran(y, weights, transformation="r", permutations=int(permutations))
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    res = SpatialAutocorrelationResult(
        variable=var, I=float(mi.I), expected_I=float(mi.EI), p_norm=float(mi.p_norm),
        p_sim=float(mi.p_sim) if permutations else None, z_sim=float(mi.z_sim) if permutations else None,
        permutations=int(permutations), confidence_level=level, n=int(len(y)), notes=notes,
        provenance={"library": "esda.Moran", "weights": getattr(weights, "census_report", None) or weights_report(weights),
                    "seed": seed},
    )
    if replicates is not None and len(replicates):
        R = _replicate_matrix(replicates, var, list(d.index), geoid_col)
        weights.transform = "r"
        Ws = weights.sparse.tocsr()
        I0 = float(_moran_vector(y[:, None], Ws)[0])
        Ir = _moran_vector(R, Ws)
        se = float(math.sqrt(REPLICATE_FACTOR * np.sum((Ir - I0) ** 2)))
        z = _z(level)
        res.se_replicate, res.ci_lower, res.ci_upper = se, res.I - z * se, res.I + z * se
        res.replicate_I = [float(v) for v in Ir]
        notes.append("se_replicate: Moran's I recomputed on each of the 80 ACS replicate maps, "
                     "Var = 4/80 sum (I_r - I)^2; interval targets the expected I of ACS estimates for these "
                     "areas (sampling noise attenuates I relative to the true values). Simulation "
                     "(census.simulation study 'morans_i_replicate_se') shows the SE is conservative "
                     "(about 10-50% too large under ACS-like CVs; intervals over-cover)")
    return res


QUADRANTS = {1: "HH", 2: "LH", 3: "LL", 4: "HL"}


def local_morans(
    data: pd.DataFrame,
    var: str,
    weights,
    *,
    permutations: int = 999,
    correction: str = "bh",
    alpha: float = 0.10,
    replicates: Optional[pd.DataFrame] = None,
    geoid_col: str = "GEOID",
    seed: Optional[int] = 12345,
) -> pd.DataFrame:
    """Local Moran's I (LISA) with multiple-comparison-corrected permutation p-values.

    Returns one row per area: GEOID, Ii, quadrant (HH/LH/LL/HL), p_sim
    (conditional permutation, esda), p_adjusted (census.multitest
    ``correction``: bh default, by, holm, bonferroni), significant
    (p_adjusted <= alpha), cluster (quadrant if significant else 'ns') and,
    with replicates, ``replicate_quadrant_share``: the share of the 80
    replicate maps on which the area keeps its quadrant (a sampling-stability
    check; low values mean the label may be noise). ``df.attrs`` holds notes.
    """
    from esda.moran import Moran_Local
    from .multitest import adjust_pvalues

    d = _aligned(data, var, weights, geoid_col)
    y = d[var].to_numpy(float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lm = Moran_Local(y, weights, transformation="r", permutations=int(permutations), seed=seed,
                         keep_simulations=False)
    p = np.asarray(lm.p_sim, float)
    padj = np.asarray(adjust_pvalues(p, correction), float)
    quad = pd.Series(lm.q).map(QUADRANTS).to_numpy()
    sig = padj <= alpha
    out = pd.DataFrame({
        geoid_col: list(d.index), "Ii": np.asarray(lm.Is, float), "quadrant": quad,
        "p_sim": p, "p_adjusted": padj, "significant": sig,
        "cluster": np.where(sig, quad, "ns"),
    })
    notes = [f"permutation p-values ({int(permutations)} conditional permutations, minimum attainable "
             f"{1 / (permutations + 1):.4g}) adjusted with '{correction}' across {len(y)} local tests",
             "islands (no neighbors) have Ii = 0 and are never significant"] if permutations else []
    if replicates is not None and len(replicates):
        R = _replicate_matrix(replicates, var, list(d.index), geoid_col)
        weights.transform = "r"
        Ws = weights.sparse.tocsr()
        Z = R - R.mean(axis=0, keepdims=True)
        L = Ws @ Z
        q = np.where(Z > 0, np.where(L > 0, 1, 4), np.where(L > 0, 2, 3))
        out["replicate_quadrant_share"] = (q == np.asarray(lm.q)[:, None]).mean(axis=1)
        notes.append("replicate_quadrant_share: share of the 80 ACS replicate maps with the same quadrant")
    out.attrs["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Spatial regression
# ---------------------------------------------------------------------------


def _design(formula: str, d: pd.DataFrame):
    import patsy

    try:
        yd, Xd = patsy.dmatrices(formula, d, NA_action="raise", return_type="dataframe")
    except patsy.PatsyError as exc:
        raise ValueError(f"formula {formula!r} could not be built (missing values?): {exc}") from exc
    if "Intercept" not in Xd.columns:
        raise ValueError("spatial_regression needs an intercept (spreg always adds a constant)")
    X = Xd.drop(columns="Intercept")
    return yd.iloc[:, [0]].to_numpy(float), X.to_numpy(float), list(X.columns), yd.columns[0]


def _spreg_fit(y, X, w, model: str, method: str, names: List[str], yname: str, ml_method: str, kwargs):
    import spreg

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if model == "lag" and method == "ml":
            r = spreg.ML_Lag(y, X, w, method=ml_method, spat_diag=False, name_x=names, name_y=yname, **kwargs)
            spatial = float(r.rho)
        elif model == "lag":
            r = spreg.GM_Lag(y, X, w=w, spat_diag=False, name_x=names, name_y=yname, **kwargs)
            spatial = float(r.rho)
        elif method == "ml":
            r = spreg.ML_Error(y, X, w, method=ml_method, name_x=names, name_y=yname, **kwargs)
            spatial = float(r.lam)
        else:
            r = spreg.GM_Error_Het(y, X, w, name_x=names, name_y=yname, **kwargs)
            spatial = float(np.asarray(r.betas).ravel()[-1])
    betas = np.asarray(r.betas, float).ravel()
    vm = np.asarray(r.vm, float)
    se = np.sqrt(np.diag(vm))
    k = len(names) + 2  # constant + covariates + rho/lambda (vm includes it for all four estimators)
    if len(betas) != k:
        betas = betas[:k]
    if len(se) < k:  # e.g. estimators without inference on the spatial parameter
        se = np.concatenate([se, np.full(k - len(se), np.nan)])
    return r, betas, se[:k], spatial


def lm_diagnostics(y: np.ndarray, X: np.ndarray, w) -> Dict[str, Any]:
    """OLS Lagrange-multiplier tests (Anselin et al. 1996) and a model suggestion."""
    import spreg

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ols = spreg.OLS(y, X)
        lms = spreg.LMtests(ols, w, tests=["lme", "lml", "rlme", "rlml", "sarma"])
    out = {name: {"statistic": float(getattr(lms, attr)[0]), "p_value": float(getattr(lms, attr)[1])}
           for name, attr in [("lm_error", "lme"), ("lm_lag", "lml"), ("robust_lm_error", "rlme"),
                              ("robust_lm_lag", "rlml"), ("lm_sarma", "sarma")]}
    a = 0.05
    le, ll = out["lm_error"]["p_value"] < a, out["lm_lag"]["p_value"] < a
    if not le and not ll:
        sug = "ols (neither LM test significant at 0.05)"
    elif le and not ll:
        sug = "error"
    elif ll and not le:
        sug = "lag"
    else:
        rle, rll = out["robust_lm_error"]["p_value"] < a, out["robust_lm_lag"]["p_value"] < a
        sug = "error" if rle and not rll else "lag" if rll and not rle else \
            ("error" if out["robust_lm_error"]["statistic"] > out["robust_lm_lag"]["statistic"] else "lag")
    out["suggested_model"] = sug
    out["decision_rule"] = "Anselin (2005) decision rule at 0.05: simple LM tests, then robust LM tests"
    return out


def spatial_regression(
    formula: str,
    data: pd.DataFrame,
    weights,
    *,
    model: str = "lag",
    method: str = "ml",
    replicates: Optional[pd.DataFrame] = None,
    target: str = "population",
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
    ml_method: str = "full",
    max_failed_replicates: int = 0,
    **spreg_kwargs: Any,
) -> ModelResult:
    """Spatial lag or spatial error regression (spreg) as a ModelResult.

    ``model``: ``lag`` (y = rho W y + X b + e) or ``error`` (u = lambda W u + e).
    ``method``: ``ml`` (ML_Lag / ML_Error, ``ml_method`` full/ord/LU) or ``gm``
    (GM_Lag: spatial 2SLS with W X, W^2 X instruments; GM_Error_Het:
    Kelejian-Prucha GMM robust to heteroskedasticity). Coefficients include
    the spatial parameter as term ``rho`` or ``lambda``. ``diagnostics`` carries
    the OLS LM tests (``lm_tests``) with a suggested model.

    With ``replicates`` every formula variable that has replicates is replaced
    by its r-th replicate and the model refit 80 times; ``se_sampling`` =
    sqrt(4/80 sum (b_r - b)^2) as in census.models.fit_replicate, and
    ``target`` (population / these_areas / conservative) picks the SE for
    intervals. ML refits cost O(n^3) each with ml_method='full'; use
    ml_method='LU' or method='gm' for large n.
    """
    from .analysis import formula_variables
    from .models import INTERVAL_TARGETS, _table

    model, method = model.lower(), method.lower()
    if model not in SPATIAL_MODELS:
        raise ValueError(f"model must be one of {SPATIAL_MODELS}")
    if method not in SPATIAL_METHODS:
        raise ValueError(f"method must be one of {SPATIAL_METHODS}")
    if target not in INTERVAL_TARGETS:
        raise ValueError(f"target must be one of {INTERVAL_TARGETS}")
    if target == "these_areas" and (replicates is None or not len(replicates)):
        raise ValueError("target='these_areas' needs replicates (se_sampling)")
    outcome = formula.split("~")[0].strip()
    d = _aligned(data, outcome if outcome in data.columns else None, weights, geoid_col)
    weights.transform = "r"
    y, X, names, yname = _design(formula, d)
    if len(y) != weights.n:
        raise ValueError("rows were dropped by the formula; weights must match the modelled areas")
    r, betas, se, spatial = _spreg_fit(y, X, weights, model, method, names, yname, ml_method, spreg_kwargs)
    sp_name = "rho" if model == "lag" else "lambda"
    terms = ["Intercept"] + names + [sp_name]
    params = pd.Series(betas, index=terms)
    se_model = pd.Series(se, index=terms)
    se_sampling = pd.Series(np.nan, index=terms)
    notes: List[str] = []
    sources = [SOURCE_MODEL]
    est_method = f"spreg {'ML' if method == 'ml' else 'GMM'} spatial {model}"
    diagnostics: Dict[str, Any] = {"lm_tests": lm_diagnostics(y, X, weights), "interval_target": target}
    for attr in ("logll", "aic", "schwarz", "pr2", "pr2_e", "sig2"):
        if hasattr(r, attr):
            try:
                diagnostics[attr] = float(np.asarray(getattr(r, attr)).ravel()[0])
            except (TypeError, ValueError):
                pass
    if model == "error" and method == "gm":
        notes.append("GM_Error_Het: Kelejian-Prucha (2010) GMM, heteroskedasticity-robust; spatial parameter "
                     "is 'lambda'")
    if weights.islands:
        notes.append(f"{len(weights.islands)} island(s) in the weights (zero spatial lag)")
    if replicates is not None and len(replicates):
        used = [v for v in formula_variables(formula, d) if v in set(replicates["variable"])]
        if not used:
            raise ValueError("no formula variable has replicates in the replicate frame")
        mats = {v: _replicate_matrix(replicates, v, list(d.index), geoid_col) for v in used}
        B, failed = [], 0
        for k in range(REPLICATE_COUNT):
            dd = d.copy()
            for v, m in mats.items():
                dd[v] = m[:, k]
            try:
                yr, Xr, _, _ = _design(formula, dd)
                _, b, _, _ = _spreg_fit(yr, Xr, weights, model, method, names, yname, ml_method, spreg_kwargs)
                if not np.isfinite(b).all():
                    raise ValueError("non-finite")
                B.append(b)
            except Exception:  # noqa: BLE001 - counted and reported
                failed += 1
        if failed > max_failed_replicates:
            raise RuntimeError(f"{failed} of 80 replicate refits failed (max_failed_replicates={max_failed_replicates})")
        B = np.vstack(B)
        se_sampling = pd.Series(np.sqrt(REPLICATE_FACTOR * ((B - betas) ** 2).sum(axis=0)), index=terms)
        sources.append(SOURCE_SAMPLING_REPLICATE)
        est_method += ", replicate refits"
        diagnostics["replicate_failures"] = failed
        diagnostics["replicate_variables"] = used
        if failed:
            notes.append(f"{failed} replicate refit(s) failed and were omitted; se_sampling may be understated")
        notes.append("se_sampling from 80 replicate refits; se_total is conservative (the model residual already "
                     "absorbs outcome sampling error), as for census.models.fit_replicate")
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    table = _table(params, se_model, se_sampling, level, target)
    notes.append(f"intervals and p-values use the '{target}' target")
    return ModelResult(
        model_type=f"spatial_{model}", formula=formula, n_obs=int(len(y)), coefficients=table,
        method=est_method, uncertainty_sources=sources, confidence_level=level, notes=notes,
        diagnostics=diagnostics,
        provenance={"library": "spreg", "spatial_parameter": sp_name, "ml_method": ml_method if method == "ml" else None,
                    "weights": getattr(weights, "census_report", None) or weights_report(weights)},
    )
