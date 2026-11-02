# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planning tools (PLAN_ROUND_8.md workstream E).

* :func:`minimum_detectable_difference`: the smallest true difference a
  two-sided z-test detects with a given power. Standard two-sample normal
  formula (e.g. Cohen 1988, ch. 12; Lohr, *Sampling*, 2nd ed., sec. 2.7):

      MDD = (z_{1-alpha/2} + z_{power}) * SE_diff,
      SE_diff = sqrt(SE1^2 + SE2^2) * sqrt(1 - C)

  where sqrt(1 - C) is the Census overlapping-period factor
  (:func:`census.stats.overlap_factor`; C = overlapping years / 5).
* :func:`aggregation_forecast`: CV of a sum of k similar areas.
* :func:`reliability_profile`: CV distribution across areas per variable.
"""

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import numpy as np
import pandas as pd
from scipy import stats as sps

from census.stats import (
    ACS_MOE_Z, CENSUS_SENTINELS, CV_HIGH_MAX, CV_MODERATE_MAX, acs5_overlap_years,
    moe_to_se, overlap_factor, pair_estimates_and_moes,
)

ArrayLike = Union[float, Sequence[float], np.ndarray, pd.Series]


def _level(x: float) -> float:
    return x / 100.0 if x > 1 else x


def _moe_to_se(moe: ArrayLike, moe_confidence: float) -> np.ndarray:
    z = float(sps.norm.ppf(1 - (1 - _level(moe_confidence)) / 2))
    if math.isclose(_level(moe_confidence), 0.90):
        z = ACS_MOE_Z  # Census convention: 1.645
    return moe_to_se(moe, z)


def minimum_detectable_difference(
    moe1: ArrayLike,
    moe2: Optional[ArrayLike] = None,
    confidence: float = 0.90,
    power: float = 0.80,
    overlap_years: Optional[int] = None,
    year1: Optional[int] = None,
    year2: Optional[int] = None,
    moe_confidence: float = 0.90,
) -> Dict[str, Any]:
    """Smallest true difference detectable by a two-sided z-test.

    MDD = (z_{1-alpha/2} + z_{power}) * sqrt(SE1^2 + SE2^2) * sqrt(1 - C).
    ``moe2`` defaults to ``moe1`` (two areas/periods with typical MOE).
    Overlap: pass ``overlap_years`` or ACS 5-year end years ``year1``/``year2``.
    Arrays are accepted and broadcast. Census sentinels give NaN.
    """
    conf, pw = _level(confidence), _level(power)
    if not (0 < conf < 1 and 0 < pw < 1):
        raise ValueError("confidence and power must be in (0, 1)")
    notes: List[str] = []
    if overlap_years is None:
        overlap_years = acs5_overlap_years(year1, year2) if (year1 is not None and year2 is not None) else 0
    factor = overlap_factor(int(overlap_years))
    if 0 < overlap_years < 5:
        notes.append(f"periods share {overlap_years} of 5 sample years: SE scaled by sqrt(1 - {overlap_years}/5)")
    se1 = _moe_to_se(moe1, moe_confidence)
    se2 = se1 if moe2 is None else _moe_to_se(moe2, moe_confidence)
    se_diff = np.sqrt(se1 ** 2 + se2 ** 2) * factor
    z_a = float(sps.norm.ppf(1 - (1 - conf) / 2))
    z_b = float(sps.norm.ppf(pw))
    mdd = (z_a + z_b) * se_diff
    scalar = np.ndim(mdd) == 0
    if np.any(np.isnan(mdd)):
        notes.append("some MOEs are missing or Census sentinels: MDD undefined there")
    return {
        "mdd": float(mdd) if scalar else mdd,
        "se_difference": float(se_diff) if scalar else se_diff,
        "z_alpha": z_a, "z_power": z_b, "confidence": conf, "power": pw,
        "overlap_years": int(overlap_years), "overlap_factor": factor,
        "method": "two-sample normal: (z_{1-alpha/2} + z_power) * SE_diff",
        "notes": notes,
    }


def aggregation_forecast(
    estimate: ArrayLike,
    moe: ArrayLike,
    group_sizes: Iterable[int] = (1, 2, 3, 5, 10, 20),
    correlation: float = 0.0,
    target_cv: Optional[float] = None,
    moe_confidence: float = 0.90,
) -> pd.DataFrame:
    """Forecast the CV (%) of a sum of k component areas (counts/totals).

    Uses the mean component estimate e and mean sampling variance v:
    CV_k = sqrt(k v + k(k-1) rho v) / (k e) * 100. ``correlation`` (rho) is the
    average correlation of component sampling errors: 0 reproduces the Census
    approximate (root-sum-of-squares) formula; a value estimated from 80
    replicate estimates (census.variance) captures covariance the approximation
    ignores. Not valid for ratios, percentages or medians. Returns columns
    ``k``, ``estimate``, ``se``, ``cv``, ``meets_target``.
    """
    est = np.asarray(estimate, dtype=float).ravel()
    se = _moe_to_se(moe, moe_confidence).ravel()
    ok = np.isfinite(est) & np.isfinite(se) & ~np.isin(est, list(CENSUS_SENTINELS))
    if not ok.any():
        raise ValueError("no usable estimate/MOE pairs")
    e, v = float(np.mean(est[ok])), float(np.mean(se[ok] ** 2))
    rows = []
    for k in group_sizes:
        k = int(k)
        if k < 1:
            raise ValueError("group sizes must be >= 1")
        total_se = math.sqrt(max(k * v + k * (k - 1) * correlation * v, 0.0))
        total = k * e
        cv = total_se / abs(total) * 100 if total != 0 else math.nan
        rows.append({"k": k, "estimate": total, "se": total_se, "cv": cv,
                     "meets_target": (cv <= target_cv) if target_cv is not None else None})
    out = pd.DataFrame(rows)
    out.attrs["method"] = ("approximate (independent components)" if correlation == 0
                           else f"approximate with average error correlation {correlation:g}")
    out.attrs["n_components_used"] = int(ok.sum())
    return out


def _profile_rows(name: str, est: pd.Series, se: pd.Series) -> Dict[str, Any]:
    est = pd.to_numeric(est, errors="coerce")
    se = pd.to_numeric(se, errors="coerce")
    sentinels = list(CENSUS_SENTINELS)
    est, se = est.mask(est.isin(sentinels)), se.mask(se.isin(sentinels))
    cv = (se.abs() / est.abs().where(est != 0)) * 100.0
    n = len(cv)
    d = cv.dropna()
    q = d.quantile([0.10, 0.25, 0.50, 0.75, 0.90]) if len(d) else pd.Series([math.nan] * 5)
    return {
        "variable": name, "n_areas": n, "n_cv_defined": int(len(d)),
        "cv_p10": float(q.iloc[0]), "cv_p25": float(q.iloc[1]), "cv_median": float(q.iloc[2]),
        "cv_p75": float(q.iloc[3]), "cv_p90": float(q.iloc[4]),
        "share_high": float((d <= CV_HIGH_MAX).sum() / n) if n else math.nan,
        "share_moderate": float(((d > CV_HIGH_MAX) & (d <= CV_MODERATE_MAX)).sum() / n) if n else math.nan,
        "share_low": float((d > CV_MODERATE_MAX).sum() / n) if n else math.nan,
        "share_undefined": float((n - len(d)) / n) if n else math.nan,
    }


def reliability_profile(frame: pd.DataFrame, variables: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Distribution of CVs (%) across areas for each variable.

    Accepts an analysis frame (``v`` with ``v_se``) or a raw ACS frame with
    ``...E``/``...M`` (or ``PE``/``PM``) column pairs. Reliability classes follow
    census.stats (High CV <= 15, Moderate <= 30, Low > 30); shares are of all
    areas, with ``share_undefined`` for missing/zero estimates or sentinels;
    a controlled estimate (MOE ``-555555555``) has SE 0.
    """
    rows = []
    if variables is None:
        variables = [c[:-3] for c in frame.columns if c.endswith("_se") and c[:-3] in frame.columns]
        pairs = [] if variables else pair_estimates_and_moes(frame.columns)
    else:
        pairs = []
    for v in variables:
        if f"{v}_se" in frame.columns:
            rows.append(_profile_rows(v, frame[v], frame[f"{v}_se"]))
        else:
            found = [p for p in pair_estimates_and_moes(frame.columns) if v in (p[0], p[1])]
            if not found:
                raise KeyError(f"no SE or MOE column found for {v!r}")
            pairs.append(found[0])
    for key, e, m in pairs:
        se = pd.Series(moe_to_se(frame[m].to_numpy(), ACS_MOE_Z), index=frame.index)
        rows.append(_profile_rows(key, frame[e], se))
    if not rows:
        raise ValueError("no estimate/SE (v, v_se) or estimate/MOE (E/M) pairs found")
    return pd.DataFrame(rows)
