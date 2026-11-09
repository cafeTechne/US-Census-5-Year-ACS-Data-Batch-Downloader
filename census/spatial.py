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
  Default: two-sided p-values (2 x esda's folded p_sim) from 9999
  permutations. Simulation ('local_morans_size'): per-area and family-wise
  rates at nominal under randomness; little power on ~150 areas; spatially
  heterogeneous (independent) sampling error gives spurious significant
  areas (Round 16 docs/audits/lisa_validation.md: correlated errors).
* :func:`spatial_regression` - spatial lag / spatial error models by ML or
  GMM (spreg), returned as :class:`census.results.ModelResult` with the OLS
  Lagrange-multiplier diagnostics that suggest which model fits, and replicate
  refits (80) for ACS sampling SEs. Default estimator: GMM for the lag model,
  ML for the error model. Simulation ('spatial_regression_coverage'): under
  ACS outcome error ML lag intervals for rho fail (GMM lag holds), lambda is
  attenuated by both estimators, beta intervals hold; LM tests keep their
  0.05 size. Option ``correct_outcome_error=True`` (error model, Round 16
  A1) corrects lambda for independent outcome sampling error with the
  spatial Fay-Herriot likelihood (census.sae_spatial; known SE^2).

References: Moran (1950) Biometrika 37:17-23; Anselin (1995) Geographical
Analysis 27:93-115; Anselin, Bera, Florax & Yoon (1996) Regional Science and
Urban Economics 26:77-104; Kelejian & Prucha (1998, 2010); Rey & Anselin (2007)
PySAL; ACS Variance Replicate Estimate Tables documentation.
"""

import builtins
import hashlib
import math
import sys
import os
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd
from scipy import sparse, stats as sps

from .results import (SOURCE_MODEL, SOURCE_SAMPLING_MODEL, SOURCE_SAMPLING_REPLICATE, ModelResult, _jsonable, check_refit_count,
                      check_replicate_policy, precheck_replicates, replicate_completeness, stochastic_variables)
from .variance import REPLICATE_COLUMNS, REPLICATE_COUNT, REPLICATE_FACTOR

WEIGHT_KINDS = ("queen", "rook", "knn", "distance")
SPATIAL_MODELS = ("lag", "error")
SPATIAL_METHODS = ("ml", "gm")
# Default estimator per model (PLAN_ROUND_15 B, owner decision after the
# 'spatial_regression_coverage' study): GMM for the lag model (ML lag rho
# intervals fail under ACS outcome error, GMM's hold); ML for the error model
# (lambda is attenuated by both estimators and GMM is not better: coverage
# 0.558 vs 0.581 at lambda 0.3, 0.162 vs 0.192 at 0.6; beta alike).
DEFAULT_METHODS = {"lag": "gm", "error": "ml"}
LISA_PERMUTATIONS = 9999
LISA_SIDED = ("two", "folded")

# Measured caveats (PLAN_ROUND_15 workstream B; census.simulation studies
# 'spatial_regression_coverage' and 'local_morans_size', full configuration,
# 12 x 12 rook lattice, tract-profile ACS CVs, nominal 90% / 0.10). Reported,
# not tuned.
LAG_ML_CAVEAT = (
    "Caveat (simulation 'spatial_regression_coverage'): with ACS sampling error in the outcome, ML lag "
    "intervals for rho covered 0.48 (true rho 0.3) and 0.003 (rho 0.6) at nominal 0.90; rho is biased toward "
    "0 (-0.16, -0.41) because the outcome's sampling error enters W y. GMM (method='gm', spatial 2SLS, the "
    "default for the lag model) covered 0.90 and 0.89 for rho. beta intervals covered 0.90 (rho 0.3) "
    "and 0.84-0.85 (rho 0.6) for both estimators")
LAG_GM_NOTE = (
    "GMM lag (spatial 2SLS, the default for the lag model): simulation 'spatial_regression_coverage' found rho "
    "intervals near nominal (0.89-0.90 at rho 0, 0.3, 0.6) under ACS outcome error, where ML lag fails "
    "(0.48 and 0.003 at rho 0.3 and 0.6); beta intervals covered 0.89-0.90, but 0.84 at rho 0.6")
ERROR_CAVEAT = (
    "Caveat (simulation 'spatial_regression_coverage'): with ACS sampling error in the outcome, lambda describes "
    "the spatial correlation of the observed residual, sampling noise included, and understates the true lambda "
    "(bias -0.16 at 0.3, -0.26 at 0.6; 90% intervals covered 0.56-0.58 and 0.16-0.19, ML and GMM alike); "
    "beta intervals covered 0.88-0.91. To correct lambda for independent outcome sampling error, refit with "
    "correct_outcome_error=True and se_col=<the outcome's SE column> (REML lambda intervals covered 0.86-0.93; "
    "docs/audits/spatial_regression_validation.md A1)")
# Error model corrected for outcome sampling error (PLAN_ROUND_16 A1, owner-approved option
# correct_outcome_error=True). Numbers: docs/audits/spatial_regression_validation.md (A1, studies
# 'spatial_error_attenuation' and 'spatial_error_attenuation_correlated', 12x12 lattices, nominal 0.90) and
# docs/audits/acs_sampling_covariance.md (workstream C, VRE replicates).
OUTCOME_ERROR_CORRECTION_NOTE = (
    "lambda corrected for outcome sampling error: spatial error model with the outcome's known sampling "
    "variances, V = sigma^2 [(I - lambda W)'(I - lambda W)]^-1 + diag(SE^2) (the spatial Fay-Herriot "
    "likelihood of census.sae_spatial; beta by GLS). Assumption: sampling errors are independent across areas "
    "and their variances are known (= SE^2 from se_col). Evidence: docs/audits/acs_sampling_covariance.md found "
    "neighbour sampling-error correlation of about 0 (|r| <= 0.003 among tracts) for rates, shares, medians and "
    "most counts; docs/audits/spatial_regression_validation.md A1 (simulation 'spatial_error_attenuation', "
    "independent errors, CV x 0.5-2, rook/queen/kNN): REML lambda intervals covered 0.86-0.93 at lambda 0.3 "
    "and 0.6 (uncorrected ML: 0.003-0.85) and 0.83-0.89 at lambda 0; beta intervals 0.85-0.93")
LAMBDA_MEANING_NOTE = (
    "lambda is the spatial autocorrelation of the disturbances (the diffusion of shocks through unmodelled, "
    "spatially clustered factors; Lacombe & LeSage 2015), not a spillover of y or x between areas; beta is the "
    "direct = total effect only if there is no substantive spillover")
CORRELATED_ERROR_CAVEAT = (
    "Caveat (simulation 'spatial_error_attenuation_correlated'): positively correlated sampling errors between "
    "neighbours bias the correction upward, because the correlation is read as spatial dependence of the "
    "disturbances. With SAR(0.6)-correlated errors (mean neighbour error correlation 0.29-0.38) and true lambda "
    "0 the corrected lambda averaged +0.19 to +0.22 and its 90% intervals covered 0.59-0.66 (0.64-0.74 at "
    "lambda 0.3); with SAR(0.3) errors (0.11-0.17) 0.80-0.83 at lambda 0. ACS VRE replicates showed no positive "
    "neighbour correlation (docs/audits/acs_sampling_covariance.md)")
CONTROLLED_TOTAL_CAVEAT = (
    "WARNING: the outcome looks like a controlled total (population, household or housing-unit count). ACS "
    "weighting controls make the sampling errors of such totals negatively correlated among neighbouring tracts "
    "in the same county (first-order r -0.08 to -0.04 for population, -0.03 to -0.015 for households; tract "
    "population errors sum to about 0 within a county; docs/audits/acs_sampling_covariance.md), so the "
    "independence assumption fails. Negative error correlation would likely pull the corrected lambda down; the "
    "size of that bias was not simulated. Prefer a rate or share outcome, or report the corrected lambda as a "
    "sensitivity analysis")
# Outcome columns treated as controlled totals: ACS cells of total population, households and housing units
# (C: B01003_001 and B25003_001 measured; the others are the same totals in other tables).
CONTROLLED_TOTAL_CELLS = ("B01003_001", "B01001_001", "B11001_001", "B25001_001", "B25002_001", "B25003_001")
_CONTROLLED_TOTAL_NAMES = ("population", "total_population", "pop", "total_pop", "households",
                           "total_households", "housing_units", "total_housing_units", "occupied_housing_units",
                           "occupied_units")
CORRECTION_METHODS = ("reml", "ml")
CORRECTION_LAMBDA_BOUNDARY = 0.98  # |lambda| at or above: boundary fit (the A1 studies' definition)
SIGMA2_ZERO_REL = 1e-8             # sigma^2 at or below this share of Var(y): boundary (as the A1 studies)
LAG_EFFECTS_NOTE = (
    "Lag model: the coefficient beta of a covariate is NOT its marginal effect. A change in x in one area "
    "changes y there and, through the spatial multiplier (I - rho W)^-1, in every other area. Report the "
    "effects table (result.effects): direct = average effect of an area's own x on its own y (feedback "
    "included), indirect = average spillover summed over all other areas, total = direct + indirect "
    "(LeSage & Pace 2009); delta-method SEs from the fit's (beta, rho) covariance, plus replicate refits "
    "when replicates are given")
SPATIAL_PARAMETER_TEST_NOTE = (
    "The p-value of {sp} tests {sp} = 0 within the fitted model; it is not a specification test (it does not "
    "choose between the lag, error and Durbin models; Juhl 2021, Political Analysis 29(2), sec. 3.2). "
    "lm_tests compare each spatial model with OLS only")
LAG_EFFECTS_CAVEAT = (
    "Caveat (simulation 'spatial_dgp_recovery', 12x12 rook lattice, nominal 0.90): with ACS outcome error the "
    "default GMM lag's delta-method effect intervals covered 0.87-0.92 at rho 0.3 but 0.80-0.87 at rho 0.6, "
    "where the indirect and total effects are heavy-tailed (rho near 1 in some samples; mean bias +0.8, "
    "simulated percentile intervals covered 0.97-0.98). ML lag effects fail under outcome error (indirect "
    "0.40 and 0.002), as its rho does. If the data follow a spatial Durbin process (W X effects), the lag "
    "model's effects are biased, and GMM's W X instruments are invalid (rho near 1)")
# Effects need the eigenvalues of the dense W (O(n^3), once per fit); above
# this many areas they are not computed (a note says so).
EFFECTS_MAX_N = 5000
NOISY_COVARIATE_CAVEAT = (
    "Covariates with sampling error attenuate beta (simulation: bias about -20%, 90% interval coverage "
    "0.19-0.40); spatial_regression has no measurement-error correction")
LOCAL_MORAN_CAVEAT = (
    "Caveat (simulation 'local_morans_size', 144-area lattice): the default reading (two-sided p_value = "
    "min(1, 2 p_sim), 9999 permutations, BH at alpha 0.10) kept the per-area rate of p_value <= 0.10 at "
    "0.099 and the family-wise rate (= FDR) at 0.088 under spatial randomness. It has little power: 1.1% of "
    "areas in a 4x4 hot spot raised by 1.5 SD were flagged, so no significant cluster is not evidence of no "
    "clustering. Spatially heterogeneous but independent sampling errors (CVs varying smoothly across the map) "
    "gave at least one significant area in 38% of maps (0.5% of areas); that study did not test spatially "
    "correlated sampling errors (see docs/audits/lisa_validation.md). replicate_quadrant_share is a stability "
    "check, not a probability that a cluster is real. p_sim is "
    "esda's folded (one-sided, observed-direction) pseudo p-value; sided='folded' with permutations=999 is "
    "the pre-Round 15 default (per-area rate of p_sim <= 0.10: 0.196)")
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


def _release_year(data: pd.DataFrame) -> Optional[int]:
    """The single release (``Year``) of an estimate frame, None when it has no
    Year column; refuses frames that mix releases."""
    if "Year" not in data.columns:
        return None
    years = pd.to_numeric(data["Year"], errors="coerce").dropna().unique()
    if len(years) > 1:
        raise ValueError(f"estimate frame mixes releases (Year {sorted(int(y) for y in years)}); "
                         "subset it to one release")
    return int(years[0]) if len(years) else None


def _replicate_matrix(replicates: pd.DataFrame, var: str, ids: Sequence[str], geoid_col: str,
                      year: Optional[int] = None) -> np.ndarray:
    """(n, 80) replicate matrix for ``var`` in ``ids`` order.

    Requires exactly one replicate row per area (PLAN_ROUND_11 C3/D3): with a
    ``Year`` column the release ``year`` (the estimate frame's) is selected
    first; replicates spanning several releases without a known year, or
    duplicate area rows (even identical ones), are refused rather than resolved
    by row order.
    """
    sub = replicates[replicates["variable"] == var]
    if not len(sub):
        raise ValueError(f"no replicates for variable {var!r}")
    if "Year" in sub.columns:
        yrs = pd.to_numeric(sub["Year"], errors="coerce")
        if year is not None:
            sub = sub[yrs == int(year)]
            if not len(sub):
                raise ValueError(f"no replicates for variable {var!r} in release {int(year)} "
                                 f"(replicate releases: {sorted(int(y) for y in yrs.dropna().unique())})")
        elif yrs.nunique(dropna=False) > 1:
            raise ValueError(f"replicates for {var!r} span several releases "
                             f"({sorted(int(y) for y in yrs.dropna().unique())}); give the estimate frame a "
                             "Year column or subset the replicates to one release")
    sub = sub.assign(_gid=sub[geoid_col].astype(str))
    dup = sub["_gid"].duplicated(keep=False)
    if dup.any():
        raise ValueError(f"{int(sub.loc[dup, '_gid'].nunique())} area(s) have more than one replicate row for "
                         f"{var!r}: {sorted(sub.loc[dup, '_gid'].unique())[:20]}; keep one row per area and release")
    sub = sub.set_index("_gid")
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
        variable=var, I=_scalar(mi.I), expected_I=_scalar(mi.EI), p_norm=_scalar(mi.p_norm),
        p_sim=_scalar(mi.p_sim) if permutations else None, z_sim=_scalar(mi.z_sim) if permutations else None,
        permutations=int(permutations), confidence_level=level, n=int(len(y)), notes=notes,
        provenance={"library": "esda.Moran", "weights": getattr(weights, "census_report", None) or weights_report(weights),
                    "seed": seed},
    )
    if replicates is not None and len(replicates):
        R = _replicate_matrix(replicates, var, list(d.index), geoid_col, _release_year(data))
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
    permutations: int = LISA_PERMUTATIONS,
    correction: str = "bh",
    alpha: float = 0.10,
    sided: str = "two",
    replicates: Optional[pd.DataFrame] = None,
    geoid_col: str = "GEOID",
    seed: Optional[int] = 12345,
) -> pd.DataFrame:
    """Local Moran's I (LISA) with multiple-comparison-corrected permutation p-values.

    Returns one row per area: GEOID, Ii, quadrant (HH/LH/LL/HL), p_sim
    (conditional permutation, esda: folded, i.e. one-sided in the observed
    direction), p_value (``sided='two'``, the default: min(1, 2 p_sim);
    ``sided='folded'``: p_sim), p_adjusted (p_value adjusted with
    census.multitest ``correction``: bh default, by, holm, bonferroni), significant
    (p_adjusted <= alpha), cluster (quadrant if significant else 'ns') and,
    with replicates, ``replicate_quadrant_share``: the share of the 80
    replicate maps on which the area keeps its quadrant (a sampling-stability
    check; low values mean the label may be noise). ``df.attrs`` holds notes.

    Defaults (PLAN_ROUND_15 B, owner decision after the 'local_morans_size'
    study; :data:`LOCAL_MORAN_CAVEAT` is added to the notes): two-sided
    p-values and 9999 permutations, so the smallest p_value (0.0002) can pass
    BH's first threshold alpha/n up to n = 500. Under spatial randomness the
    per-area rate of p_value <= 0.10 and the family-wise rate are at nominal
    (the folded 999-permutation reading used before: per-area 0.196, and a
    family-wise rate of 0.056 only because of its coarse p-value grid).
    Power is low on ~150 areas, and spatially heterogeneous (independent)
    sampling error produces spurious significant areas. About 0.6 s for
    3,000 areas.
    """
    from esda.moran import Moran_Local
    from .multitest import adjust_pvalues

    if sided not in LISA_SIDED:
        raise ValueError(f"sided must be one of {LISA_SIDED}")
    d = _aligned(data, var, weights, geoid_col)
    y = d[var].to_numpy(float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lm = Moran_Local(y, weights, transformation="r", permutations=int(permutations), seed=seed,
                         keep_simulations=False)
    p = np.asarray(lm.p_sim, float)
    pv = np.minimum(1.0, 2 * p) if sided == "two" else p
    padj = np.asarray(adjust_pvalues(pv, correction), float)
    quad = pd.Series(lm.q).map(QUADRANTS).to_numpy()
    sig = padj <= alpha
    out = pd.DataFrame({
        geoid_col: list(d.index), "Ii": np.asarray(lm.Is, float), "quadrant": quad,
        "p_sim": p, "p_value": pv, "p_adjusted": padj, "significant": sig,
        "cluster": np.where(sig, quad, "ns"),
    })
    notes = [f"{'two-sided p_value = min(1, 2 p_sim)' if sided == 'two' else 'folded (one-sided) p_value = p_sim'}"
             f" from {int(permutations)} conditional permutations (minimum attainable p_sim "
             f"{1 / (permutations + 1):.4g}), adjusted with '{correction}' across {len(y)} local tests, "
             f"significant at p_adjusted <= {alpha:g}",
             "islands (no neighbors) have Ii = 0 and are never significant"] if permutations else []
    if replicates is not None and len(replicates):
        R = _replicate_matrix(replicates, var, list(d.index), geoid_col, _release_year(data))
        weights.transform = "r"
        Ws = weights.sparse.tocsr()
        Z = R - R.mean(axis=0, keepdims=True)
        L = Ws @ Z
        q = np.where(Z > 0, np.where(L > 0, 1, 4), np.where(L > 0, 2, 3))
        out["replicate_quadrant_share"] = (q == np.asarray(lm.q)[:, None]).mean(axis=1)
        notes.append("replicate_quadrant_share: share of the 80 ACS replicate maps with the same quadrant")
    notes.append(LOCAL_MORAN_CAVEAT)
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


def _scalar(x) -> float:
    """float() of a scalar or 1-element array (numpy 2 rejects float() on ndim>0 arrays)."""
    return float(np.asarray(x, dtype=float).reshape(-1)[0])


def _spreg_print(*args: Any, **kwargs: Any) -> None:
    """``print`` for the spreg.output module: drops what would reach stdout, forwards other files."""
    target = kwargs.get("file")
    if target is not None and target is not sys.stdout and target is not sys.__stdout__:
        builtins.print(*args, **kwargs)


def _silence_spreg_output() -> None:
    """spreg 1.9 prints the estimator's class name (``print(_reg.__class__.__name__)`` in
    ``spreg.output.out_part_top``) on every ML/GMM fit, and stdout carries the MCP stdio protocol.
    Shadow ``print`` in that module only, once: no process-wide stdout redirection, so concurrent
    fits and other threads' output are unaffected (contextlib.redirect_stdout swaps sys.stdout for
    every thread)."""
    import importlib

    # the module, not the spreg.output function that spreg/__init__ re-exports under the same name
    spreg_output = importlib.import_module("spreg.output")
    if spreg_output.__dict__.get("print") is not _spreg_print:
        spreg_output.print = _spreg_print  # type: ignore[attr-defined]


def _spreg_fit(y, X, w, model: str, method: str, names: List[str], yname: str, ml_method: str, kwargs):
    import spreg

    _silence_spreg_output()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if model == "lag" and method == "ml":
            r = spreg.ML_Lag(y, X, w, method=ml_method, spat_diag=False, name_x=names, name_y=yname, **kwargs)
            spatial = _scalar(r.rho)
        elif model == "lag":
            r = spreg.GM_Lag(y, X, w=w, spat_diag=False, name_x=names, name_y=yname, **kwargs)
            spatial = _scalar(r.rho)
        elif method == "ml":
            r = spreg.ML_Error(y, X, w, method=ml_method, name_x=names, name_y=yname, **kwargs)
            spatial = _scalar(r.lam)
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


def weights_eigenvalues(w) -> Optional[np.ndarray]:
    """Eigenvalues of the (row-standardized) dense W, or None above EFFECTS_MAX_N areas.

    Computed once per fit and reused by every replicate refit: the effect
    multipliers at any rho are sums over them (Ord 1975).
    """
    if w.n > EFFECTS_MAX_N:
        return None
    return np.linalg.eigvals(np.asarray(w.full()[0], float))


def lag_multipliers(eig: np.ndarray, rho: float) -> Dict[str, float]:
    """Average impact multipliers of the spatial lag model at ``rho`` and their rho-derivatives.

    S(W) = (I - rho W)^-1. direct = tr(S)/n = mean 1/(1 - rho w_i) over the
    eigenvalues w_i of W; total = 1' S 1 / n = 1/(1 - rho) for a
    row-standardized W without islands (islands: :func:`_lag_effect_values`);
    indirect = total - direct (LeSage & Pace 2009, ch. 2.7). NaN outside
    |rho| < 1.
    """
    nan = {k: float("nan") for k in ("direct", "total", "indirect", "d_direct", "d_total", "d_indirect")}
    if not (np.isfinite(rho) and abs(rho) < 1):
        return nan
    e = np.asarray(eig)
    den = 1.0 - rho * e
    direct = float(np.real(np.mean(1.0 / den)))
    d_direct = float(np.real(np.mean(e / den ** 2)))
    total, d_total = 1.0 / (1.0 - rho), 1.0 / (1.0 - rho) ** 2
    return {"direct": direct, "total": total, "indirect": total - direct,
            "d_direct": d_direct, "d_total": d_total, "d_indirect": d_total - d_direct}


def _lag_effect_values(params: np.ndarray, eig: np.ndarray, n_x: int, share: np.ndarray) -> np.ndarray:
    """Effects [x1 direct, x1 indirect, x1 total, x2 ...] from [const, x1..xk, rho].

    ``share``: the mean row sum of W (1 for row-standardized W without
    islands); with islands, total = beta (1 - s + s / (1 - rho)) where s is
    the share of areas with neighbors, since an island's row of S is e_i.
    """
    rho = float(params[-1])
    m = lag_multipliers(eig, rho)
    s = float(share)
    tot = (1 - s) + s * m["total"] if np.isfinite(m["total"]) else float("nan")
    out = []
    for j in range(n_x):
        b = float(params[1 + j])
        out += [b * m["direct"], b * (tot - m["direct"]), b * tot]
    return np.asarray(out, float)


def lag_effects(params: np.ndarray, vm: Optional[np.ndarray], names: List[str], eig: np.ndarray,
                share: float = 1.0) -> Dict[str, Any]:
    """Direct, indirect and total effects of a spatial lag fit with delta-method SEs.

    ``params``: [const, x1..xk, rho] (spreg order); ``vm``: their covariance
    (spreg ``vm``; for GM_Lag and ML_Lag rho is the last row). For covariate
    k the effects are beta_k times the multipliers of :func:`lag_multipliers`;
    the gradient is (multiplier) for beta_k and beta_k x d(multiplier)/d rho
    for rho, so Var = g' V[(k, rho)] g. Returns {"terms", "estimate", "se"}.
    """
    params = np.asarray(params, float).ravel()
    n_x = len(names)
    est = _lag_effect_values(params, eig, n_x, np.asarray(share))
    se = np.full(len(est), np.nan)
    rho = float(params[-1])
    m = lag_multipliers(eig, rho)
    s = float(share)
    if vm is not None and np.isfinite(m["direct"]):
        V = np.asarray(vm, float)
        mult = {"direct": (m["direct"], m["d_direct"]),
                "total": ((1 - s) + s * m["total"], s * m["d_total"])}
        mult["indirect"] = (mult["total"][0] - m["direct"], mult["total"][1] - m["d_direct"])
        r = V.shape[0] - 1
        for j in range(n_x):
            b, k = float(params[1 + j]), 1 + j
            sub = V[np.ix_([k, r], [k, r])]
            for i, eff in enumerate(("direct", "indirect", "total")):
                mu, dmu = mult[eff]
                g = np.array([mu, b * dmu])
                se[3 * j + i] = math.sqrt(max(float(g @ sub @ g), 0.0))
    terms = [f"{v} ({eff})" for v in names for eff in ("direct", "indirect", "total")]
    return {"terms": terms, "estimate": est, "se": se}


def lm_diagnostics(y: np.ndarray, X: np.ndarray, w) -> Dict[str, Any]:
    """OLS Lagrange-multiplier tests (Anselin et al. 1996) and a model suggestion."""
    import spreg

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ols = spreg.OLS(y, X)
        lms = spreg.LMtests(ols, w, tests=["lme", "lml", "rlme", "rlml", "sarma"])
    out = {name: {"statistic": _scalar(getattr(lms, attr)[0]), "p_value": _scalar(getattr(lms, attr)[1])}
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


def controlled_total_reason(outcome: str) -> Optional[str]:
    """Why ``outcome`` looks like an ACS controlled total (population / household / housing-unit count), or None.

    Matches the ACS cells in :data:`CONTROLLED_TOTAL_CELLS` (with or without the
    ``E`` suffix) and plain names such as ``population`` or ``households``.
    """
    name = str(outcome).strip()
    code = name.upper()[:-1] if name.upper().endswith("E") else name.upper()
    if code in CONTROLLED_TOTAL_CELLS:
        return f"ACS cell {code} is a total controlled to the population / housing-unit estimates"
    if name.lower() in _CONTROLLED_TOTAL_NAMES:
        return f"the name {name!r} suggests a population / household / housing-unit total"
    return None


def _error_model_known_variances(y: np.ndarray, X: np.ndarray, W: np.ndarray, D: np.ndarray,
                                 method: str) -> Dict[str, Any]:
    """Spatial error model with known independent outcome sampling variances ``D`` (census.sae_spatial).

    y = X beta + u + e, u = lambda W u + eps, eps ~ N(0, sigma^2 I), e ~ N(0, diag(D)):
    V = sigma^2 [(I - lambda W)'(I - lambda W)]^-1 + diag(D), the spatial
    Fay-Herriot likelihood with rho = lambda (Petrucci & Salvati 2006;
    Molina & Marhuenda 2015 eq. (6)). (sigma^2, lambda) by REML or ML
    (census.sae_spatial.estimate_variance_rho), beta by GLS with covariance
    (X'V^-1X)^-1, SEs of (sigma^2, lambda) from the inverse expected
    information 0.5 tr(M V_k M V_l), M = P (REML) or V^-1 (ML) - the
    estimator of the A1 studies (census.simulation_spreg.me_sem_fit with a
    diagonal Sigma). ``X`` includes the constant. Raises RuntimeError when the
    optimizer does not converge (no fallback to the uncorrected estimate).
    """
    from scipy import linalg as sla
    from .sae_spatial import _fisher, _fit, _loglik

    fit = _fit(y, X, D, W, method.upper())
    if not fit["converged"]:
        raise RuntimeError(
            f"correct_outcome_error: the {method.upper()} optimizer for (sigma^2, lambda) did not converge "
            f"(stopped at lambda={fit['rho']:.4f}, sigma^2={fit['A']:.4g}); no corrected estimate is reported and "
            "the uncorrected fit is not substituted. Check the SE column (units must match the outcome), the "
            "weights, and whether the sampling variances exceed the outcome's variance")
    s = fit["state"]
    A, lam = float(fit["A"]), float(fit["rho"])
    Fi = np.full((2, 2), np.nan)
    # at the sigma^2 = 0 boundary (relative to the outcome's variance) lambda is not identified: no SE
    sigma2_zero = A <= SIGMA2_ZERO_REL * max(float(np.var(y, ddof=1)), 1e-300)
    if not sigma2_zero:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", sla.LinAlgWarning)
                Fi = sla.inv(_fisher(s, method.upper()))
        except (np.linalg.LinAlgError, sla.LinAlgError, ValueError):
            pass
    ll, _ = _loglik(np.array([A, lam]), y, X, D, W, "ML")
    return {"A": A, "lambda": lam, "beta": np.asarray(fit["beta"], float), "vcov_beta": np.asarray(fit["Q"], float),
            "se_A": float(math.sqrt(Fi[0, 0])) if np.isfinite(Fi[0, 0]) and Fi[0, 0] > 0 else float("nan"),
            "se_lambda": float(math.sqrt(Fi[1, 1])) if np.isfinite(Fi[1, 1]) and Fi[1, 1] > 0 else float("nan"),
            "loglik": float(ll), "converged": True, "sigma2_zero": bool(sigma2_zero)}


def _corrected_error_regression(formula: str, d: pd.DataFrame, weights, *, outcome: str, se_col: Optional[str],
                                method: Optional[str], target: str, level: float, controlled_total: Optional[bool],
                                ml_method: str, spreg_kwargs: Dict[str, Any]) -> ModelResult:
    """spatial_regression(model='error', correct_outcome_error=True): see :func:`spatial_regression`."""
    from .models import _table

    if se_col is None:
        raise ValueError("correct_outcome_error=True needs se_col: the column of the outcome's sampling SEs")
    if outcome not in d.columns:
        raise ValueError(f"correct_outcome_error=True needs a plain outcome column on the left of the formula "
                         f"(got {outcome!r}); se_col must be that column's SE")
    if se_col not in d.columns:
        raise KeyError(f"se_col {se_col!r} not in frame")
    corr_method = "reml" if method is None else method.lower()
    if corr_method not in CORRECTION_METHODS:
        raise ValueError(f"with correct_outcome_error=True, method must be one of {CORRECTION_METHODS} "
                         f"(variance-component estimator; REML is the validated default), not {method!r}")
    se = pd.to_numeric(d[se_col], errors="coerce").to_numpy(float)
    if not np.isfinite(se).all() or (se < 0).any():
        bad = list(np.asarray(d.index)[~(np.isfinite(se) & (se >= 0))][:20])
        raise ValueError(f"se_col {se_col!r} must be finite and >= 0 in every area: {bad}")
    y2, X, names, yname = _design(formula, d)
    y = y2[:, 0]
    n = len(y)
    if n <= X.shape[1] + 3:
        raise ValueError(f"need more areas ({n}) than parameters ({X.shape[1] + 3})")
    W = np.asarray(weights.full()[0], float)  # row-standardized, in w.id_order (= d's row order)
    D = se ** 2
    Xc = np.column_stack([np.ones(n), X])
    fit = _error_model_known_variances(y, Xc, W, D, corr_method)
    terms = ["Intercept"] + names + ["lambda"]
    params = pd.Series(list(fit["beta"]) + [fit["lambda"]], index=terms)
    se_model = pd.Series(list(np.sqrt(np.diag(fit["vcov_beta"]))) + [fit["se_lambda"]], index=terms)
    table = _table(params, se_model, pd.Series(np.nan, index=terms), level, target)

    reason = controlled_total_reason(outcome) if controlled_total is None else (
        "flagged by the caller (controlled_total=True)" if controlled_total else None)
    boundary = []
    if abs(fit["lambda"]) >= CORRECTION_LAMBDA_BOUNDARY:
        boundary.append(f"|lambda| >= {CORRECTION_LAMBDA_BOUNDARY:g} (near the unit bound; the SAR process is "
                        "near-singular)")
    var_y = float(np.var(y, ddof=1))
    if fit["sigma2_zero"]:
        boundary.append("sigma^2 estimated at 0: the sampling variances absorb all residual variation, lambda is "
                        "not identified and has no interval")
    try:
        _, _, _, lam_naive = _spreg_fit(y2, X, weights, "error", "ml", names, yname, ml_method, spreg_kwargs)
    except Exception:  # noqa: BLE001 - comparison only; reported as unavailable
        lam_naive = None
    k = Xc.shape[1] + 2
    diagnostics: Dict[str, Any] = {
        "lm_tests": lm_diagnostics(y2, X, weights), "interval_target": target,
        "converged": True, "boundary": bool(boundary), "controlled_total_warning": reason is not None,
        "logll": fit["loglik"], "aic": -2 * fit["loglik"] + 2 * k, "schwarz": -2 * fit["loglik"] + k * math.log(n),
        "outcome_error_correction": {
            "se_col": se_col, "variance_method": corr_method.upper(), "sigma2": fit["A"],
            "sigma2_se": fit["se_A"], "lambda": fit["lambda"], "lambda_se": fit["se_lambda"],
            "lambda_uncorrected": lam_naive,
            "lambda_uncorrected_method": "spreg ML_Error (the default error model, no correction)",
            "mean_sampling_variance_share": float(np.mean(D) / var_y) if var_y > 0 else None,
            "assumption": "sampling errors independent across areas; variances known (SE^2)",
            "boundary_reasons": boundary, "controlled_total_reason": reason,
            "evidence": ["docs/audits/spatial_regression_validation.md (A1)",
                         "docs/audits/acs_sampling_covariance.md (C)"],
        },
    }
    notes: List[str] = []
    if reason is not None:
        notes.append(f"{CONTROLLED_TOTAL_CAVEAT} ({reason}; pass controlled_total=False if it is not)")
    notes += [OUTCOME_ERROR_CORRECTION_NOTE, LAMBDA_MEANING_NOTE, CORRELATED_ERROR_CAVEAT]
    if lam_naive is not None:
        notes.append(f"uncorrected lambda (spreg ML_Error) = {lam_naive:.4f}; corrected = {fit['lambda']:.4f}")
    notes += [f"boundary fit: {b}" for b in boundary]
    if not np.isfinite(fit["se_lambda"]):
        notes.append("lambda has no SE (sigma^2 at 0, or singular information at the fitted values): no interval "
                     "is reported")
    if weights.islands:
        notes.append(f"{len(weights.islands)} island(s) in the weights (zero spatial lag)")
    if d.attrs.get("extra_rows_dropped"):
        notes.append(f"{len(d.attrs['extra_rows_dropped'])} frame row(s) not in the weights were ignored")
    notes.append(f"intervals and p-values use the '{target}' target (normal, from se_model: GLS for beta, the "
                 f"{corr_method.upper()} information for lambda)")
    notes.append(SPATIAL_PARAMETER_TEST_NOTE.format(sp="lambda"))
    notes.append(NOISY_COVARIATE_CAVEAT)
    return ModelResult(
        model_type="spatial_error", formula=formula, n_obs=int(n), coefficients=table,
        method=f"spatial error model with known outcome sampling variances (spatial Fay-Herriot likelihood, "
               f"{corr_method.upper()})",
        uncertainty_sources=[SOURCE_MODEL, SOURCE_SAMPLING_MODEL], confidence_level=level, notes=notes,
        diagnostics=diagnostics,
        provenance={"library": "census.sae_spatial", "spatial_parameter": "lambda", "ml_method": None,
                    "outcome_error_correction": True, "se_col": se_col,
                    "weights": getattr(weights, "census_report", None) or weights_report(weights)},
    )


def spatial_regression(
    formula: str,
    data: pd.DataFrame,
    weights,
    *,
    model: str = "lag",
    method: Optional[str] = None,
    replicates: Optional[pd.DataFrame] = None,
    target: str = "population",
    confidence_level: float = 0.90,
    geoid_col: str = "GEOID",
    ml_method: str = "full",
    max_failed_replicates: int = 0,
    replicate_policy: str = "flag",
    correct_outcome_error: bool = False,
    se_col: Optional[str] = None,
    controlled_total: Optional[bool] = None,
    **spreg_kwargs: Any,
) -> ModelResult:
    """Spatial lag or spatial error regression (spreg) as a ModelResult.

    ``model``: ``lag`` (y = rho W y + X b + e) or ``error`` (u = lambda W u + e).
    ``method``: ``ml`` (ML_Lag / ML_Error, ``ml_method`` full/ord/LU) or ``gm``
    (GM_Lag: spatial 2SLS with W X, W^2 X instruments; GM_Error_Het:
    Kelejian-Prucha GMM robust to heteroskedasticity). Default
    (:data:`DEFAULT_METHODS`): ``gm`` for the lag model, ``ml`` for the error
    model. Coefficients include
    the spatial parameter as term ``rho`` or ``lambda``. ``diagnostics`` carries
    the OLS LM tests (``lm_tests``) with a suggested model.

    With ``replicates`` every formula variable that has replicates is replaced
    by its r-th replicate and the model refit 80 times; ``se_sampling`` =
    sqrt(4/80 sum (b_r - b)^2) as in census.models.fit_replicate, and
    ``target`` (population / these_areas / conservative) picks the SE for
    intervals. ML refits cost O(n^3) each with ml_method='full'; use
    ml_method='LU' or method='gm' for large n.

    Measured behavior (census.simulation 'spatial_regression_coverage', added
    to the notes): with ACS sampling error in the outcome the ML lag
    estimator's rho is biased toward 0 and its intervals under-cover badly
    (0.48 at rho 0.3, 0.003 at 0.6); GMM lag, the default since Round 15,
    covers rho near nominal (0.89-0.90). In the error model lambda is
    attenuated by either estimator (ML, the default, is not worse), while
    beta intervals stay near nominal. ``replicate_policy``: "flag"
    (default) records failed refits or stochastic variables without
    replicates in diagnostics["replicates_incomplete"]; "strict" raises
    ValueError (census.results.replicate_completeness, PLAN_ROUND_11 Q2/D7).

    ``correct_outcome_error=True`` (model='error' only; PLAN_ROUND_16 A1,
    owner-approved option, off by default) needs ``se_col``, the outcome's
    sampling SEs, and fits the spatial error model with those known
    variances: V = sigma^2 [(I - lambda W)'(I - lambda W)]^-1 + diag(SE^2),
    the spatial Fay-Herriot likelihood of census.sae_spatial. ``method`` is
    then the variance-component estimator, ``reml`` (default, validated) or
    ``ml``. lambda (corrected) has a Wald interval from the information
    matrix and beta GLS Wald intervals, the interval methods validated by the
    'spatial_error_attenuation' study (REML lambda coverage 0.86-0.93 at
    lambda 0.3-0.6 vs 0.003-0.85 uncorrected). It assumes sampling errors
    independent across areas (docs/audits/acs_sampling_covariance.md: about 0
    for rates, shares, medians and most counts); positively correlated errors
    bias it upward (notes). Controlled totals (population, households,
    housing units: :data:`CONTROLLED_TOTAL_CELLS`, or ``controlled_total=True``)
    have negatively correlated errors within counties and get a warning note
    and ``diagnostics['controlled_total_warning']``. Non-convergence raises
    RuntimeError. Replicates are not used with this option.
    """
    from .analysis import formula_variables
    from .models import INTERVAL_TARGETS, _table

    check_replicate_policy(replicate_policy)

    model = model.lower()
    if model not in SPATIAL_MODELS:
        raise ValueError(f"model must be one of {SPATIAL_MODELS}")
    if correct_outcome_error:
        if model != "error":
            raise ValueError("correct_outcome_error=True applies to model='error' only (the lag model's default "
                             "GMM estimator already holds its rho intervals under outcome error)")
        if replicates is not None and len(replicates):
            raise ValueError("correct_outcome_error=True does not use replicates: the outcome's sampling variance "
                             "enters the model through se_col; fit without replicates")
        if target != "population":
            raise ValueError("correct_outcome_error=True supports target='population' only (intervals from the "
                             "model, which includes the known sampling variances)")
        outcome = formula.split("~")[0].strip()
        d = _aligned(data, outcome if outcome in data.columns else None, weights, geoid_col)
        weights.transform = "r"
        return _corrected_error_regression(
            formula, d, weights, outcome=outcome, se_col=se_col, method=method, target=target,
            level=confidence_level if confidence_level <= 1 else confidence_level / 100,
            controlled_total=controlled_total, ml_method=ml_method, spreg_kwargs=spreg_kwargs)
    if se_col is not None or controlled_total is not None:
        raise ValueError("se_col and controlled_total apply only with correct_outcome_error=True")
    method = DEFAULT_METHODS[model] if method is None else method.lower()
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
    eig = weights_eigenvalues(weights) if model == "lag" else None
    share = 1.0 - len(weights.islands) / weights.n
    eff = None
    if eig is not None:
        k = len(terms)
        vm_k = np.asarray(r.vm, float)[:k, :k]
        eff = lag_effects(betas, vm_k, names, eig, share)
        # for simulated (LeSage & Pace 2009) effect intervals: draws from N(estimates, vcov)
        diagnostics["coefficient_vcov"] = {"terms": terms, "matrix": vm_k.tolist()}
    B = None
    if replicates is not None and len(replicates):
        all_used = formula_variables(formula, d)
        used = [v for v in all_used if v in set(replicates["variable"])]
        if not used:
            raise ValueError("no formula variable has replicates in the replicate frame")
        yr = _release_year(data)
        mats = {v: _replicate_matrix(replicates, v, list(d.index), geoid_col, yr) for v in used}
        # rows without replicates are refused by _replicate_matrix, so only
        # failed refits and variables without replicates can be incomplete
        stoch = stochastic_variables(data, [v for v in all_used if v not in used])
        precheck_replicates(replicate_policy, variables_without_replicates=stoch, context="spatial_regression")
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
        replicate_completeness(replicate_policy, failed_refits=failed, variables_without_replicates=stoch,
                               context="spatial_regression", diagnostics=diagnostics, notes=notes)
        check_refit_count(failed, len(B), max_failed_replicates, "spatial_regression")
        B = np.vstack(B)
        se_sampling = pd.Series(np.sqrt(REPLICATE_FACTOR * ((B - betas) ** 2).sum(axis=0)), index=terms)
        sources.append(SOURCE_SAMPLING_REPLICATE)
        est_method += ", replicate refits"
        diagnostics["replicate_failures"] = failed
        diagnostics["replicate_variables"] = used
        notes.append("se_sampling from 80 replicate refits; se_total is conservative (the model residual already "
                     "absorbs outcome sampling error), as for census.models.fit_replicate")
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    table = _table(params, se_model, se_sampling, level, target)
    effects = None
    if model == "lag":
        table["interpretation"] = ["intercept"] + [
            "coefficient on X in y = rho W y + X beta + e: NOT the marginal effect (see effects)"] * len(names) + [
            "spatial lag parameter rho (spillover strength); its p-value tests rho = 0, not the model "
            "specification"]
        if eff is None:
            notes.insert(0, f"{LAG_EFFECTS_NOTE}. Effects were NOT computed: {weights.n} areas exceed "
                            f"EFFECTS_MAX_N={EFFECTS_MAX_N} (dense eigenvalues of W)")
        else:
            eff_sampling = pd.Series(np.nan, index=eff["terms"])
            if B is not None:
                E = np.vstack([_lag_effect_values(b, eig, len(names), np.asarray(share)) for b in B])
                eff_sampling[:] = np.sqrt(REPLICATE_FACTOR * ((E - eff["estimate"]) ** 2).sum(axis=0))
            effects = _table(pd.Series(eff["estimate"], index=eff["terms"]), pd.Series(eff["se"], index=eff["terms"]),
                             eff_sampling, level, target)
            effects.insert(1, "variable", [v for v in names for _ in range(3)])
            effects.insert(2, "effect", ["direct", "indirect", "total"] * len(names))
            notes[0:0] = [LAG_EFFECTS_NOTE, LAG_EFFECTS_CAVEAT]
            diagnostics["effects_multipliers"] = {k: v for k, v in lag_multipliers(eig, spatial).items()
                                                  if not k.startswith("d_")}
    notes.append(f"intervals and p-values use the '{target}' target")
    notes.append(SPATIAL_PARAMETER_TEST_NOTE.format(sp=sp_name))
    notes.append(ERROR_CAVEAT if model == "error" else LAG_ML_CAVEAT if method == "ml" else LAG_GM_NOTE)
    notes.append(NOISY_COVARIATE_CAVEAT)
    return ModelResult(
        model_type=f"spatial_{model}", formula=formula, n_obs=int(len(y)), coefficients=table,
        method=est_method, uncertainty_sources=sources, confidence_level=level, notes=notes,
        diagnostics=diagnostics,
        provenance={"library": "spreg", "spatial_parameter": sp_name, "ml_method": ml_method if method == "ml" else None,
                    "weights": getattr(weights, "census_report", None) or weights_report(weights),
                    **({"coefficient_meaning": "beta is the coefficient on X in y = rho W y + X beta + e, not a "
                                               "marginal effect; see effects"} if model == "lag" else {})},
        effects=effects,
    )
