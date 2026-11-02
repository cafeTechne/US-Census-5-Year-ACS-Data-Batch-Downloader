# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multiple-comparison corrections and batch significance testing for ACS estimates.

Comparing many geographies at once inflates the chance of false positives:
at the 90% level, roughly 1 in 10 true-null comparisons is "significant" by
chance. :func:`adjust_pvalues` implements the standard corrections and
:func:`batch_z_test` applies the Census Z test (Instructions for Applying
Statistical Testing to ACS Data, 2025) row by row, then corrects across the
family of comparisons.

Methods
-------
* ``bonferroni``: p * m (family-wise error rate, FWER).
* ``holm``: Holm (1979) step-down; controls FWER and is uniformly at least as
  powerful as Bonferroni. Default.
* ``bh``: Benjamini-Hochberg (1995) step-up; controls the false discovery rate
  (FDR) under independence or positive dependence. Recommended for
  exploratory screening.
* ``by``: Benjamini-Yekutieli (2001); FDR under arbitrary dependence
  (BH times sum_{i=1..m} 1/i).

Census's own published comparisons in some products apply a Bonferroni
correction within a family of comparisons; Comparison Profiles flag each pair
at the 90% level without correction.
"""

import math
from typing import Any, Optional, Union

import numpy as np
import pandas as pd

from .stats import CENSUS_SENTINELS, _z_for_confidence, acs5_overlap_years, overlap_factor
from .variance import Z90

METHODS = ("bonferroni", "holm", "bh", "by")
_ALIASES = {
    "bonferroni": "bonferroni",
    "holm": "holm",
    "holm-bonferroni": "holm",
    "bh": "bh",
    "fdr_bh": "bh",
    "benjamini-hochberg": "bh",
    "by": "by",
    "fdr_by": "by",
    "benjamini-yekutieli": "by",
}

CONTROLLED_MOE = -555555555
_SENTINELS = np.array(sorted(CENSUS_SENTINELS), dtype=float)

NOTE_OVERLAP = (
    "periods overlap by {k} of 5 years: SE of the difference scaled by sqrt(1 - {k}/5) "
    "per Census guidance; overlapping periods share sample, and Census advises comparing "
    "non-overlapping periods where possible"
)


def _method(name: str) -> str:
    key = str(name).strip().lower()
    if key not in _ALIASES:
        raise ValueError(f"unknown correction {name!r}; expected one of {METHODS}")
    return _ALIASES[key]


def adjust_pvalues(pvalues: Any, method: str = "holm") -> Union[np.ndarray, pd.Series]:
    """Adjust p-values for multiple comparisons.

    NaN p-values are excluded from the family (they do not count toward m)
    and are returned as NaN. Adjusted values are capped at 1 and, for the
    step-wise methods, made monotone in the order of the raw p-values.

    Args:
        pvalues: 1-D array-like or Series of p-values in [0, 1].
        method: ``bonferroni``, ``holm`` (default), ``bh`` or ``by``
            (aliases ``fdr_bh``, ``fdr_by`` accepted).

    Returns:
        Adjusted p-values, as a Series with the input's index when a Series
        was given, else a NumPy array.
    """
    m_name = _method(method)
    index = pvalues.index if isinstance(pvalues, pd.Series) else None
    p = pd.to_numeric(pd.Series(np.asarray(pvalues, dtype=object).ravel()), errors="coerce").to_numpy(float)
    if np.asarray(pvalues, dtype=object).ndim > 1:
        raise ValueError("pvalues must be one-dimensional")
    valid = ~np.isnan(p)
    if ((p[valid] < 0) | (p[valid] > 1)).any():
        raise ValueError("p-values must lie in [0, 1]")
    out = np.full(p.shape, np.nan)
    pv = p[valid]
    m = pv.size
    if m:
        order = np.argsort(pv, kind="mergesort")
        sorted_p = pv[order]
        ranks = np.arange(1, m + 1, dtype=float)
        if m_name == "bonferroni":
            adj_sorted = np.minimum(sorted_p * m, 1.0)
        elif m_name == "holm":
            adj_sorted = np.minimum(np.maximum.accumulate((m - ranks + 1) * sorted_p), 1.0)
        else:
            factor = 1.0
            if m_name == "by":
                factor = float(np.sum(1.0 / ranks))
            raw = sorted_p * m * factor / ranks
            adj_sorted = np.minimum(np.minimum.accumulate(raw[::-1])[::-1], 1.0)
        adj = np.empty(m)
        adj[order] = adj_sorted
        out[valid] = adj
    if index is not None:
        return pd.Series(out, index=index, name=getattr(pvalues, "name", None))
    return out


def _numeric(df: pd.DataFrame, col: Any) -> np.ndarray:
    if isinstance(col, str) and col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").to_numpy(float)
    if isinstance(col, (pd.Series, np.ndarray, list, tuple)):
        arr = pd.to_numeric(pd.Series(np.asarray(col, dtype=object)), errors="coerce").to_numpy(float)
        if arr.size != len(df):
            raise ValueError(f"array argument has length {arr.size}, frame has {len(df)} rows")
        return arr
    raise KeyError(f"column {col!r} not in frame")


def _years(df: pd.DataFrame, year: Any) -> Optional[np.ndarray]:
    if year is None:
        return None
    if isinstance(year, str) and year in df.columns:
        return pd.to_numeric(df[year], errors="coerce").to_numpy(float)
    return np.full(len(df), float(year))


def batch_z_test(
    df: pd.DataFrame,
    est1: Any,
    moe1: Any,
    est2: Any,
    moe2: Any,
    year1: Any = None,
    year2: Any = None,
    confidence_level: float = 0.90,
    correction: str = "holm",
) -> pd.DataFrame:
    """Two-sample Z tests for every row, with a multiple-comparison correction.

    For each row: Z = (X1 - X2) / sqrt(SE1^2 + SE2^2), SE = MOE / 1.645
    (Census, Instructions for Applying Statistical Testing, 2025). When
    ``year1`` and ``year2`` (ACS 5-year period end years; ints or column
    names) overlap, the SE of the difference is multiplied by
    ``census.stats.overlap_factor`` = sqrt(1 - C), C the overlapping share of
    the 5 sample years, and the row carries a note.

    The family is every row with a defined test; rows with a missing
    estimate or an undefined MOE (sentinels other than the controlled-
    estimate code, which is treated as MOE 0 per Census) are not tested,
    get NaN p-values and ``<NA>`` significance, and do not count toward the
    family size.

    Significance uses p-values: ``significant_raw`` is p < 1 - confidence
    and ``significant_adjusted`` is adjusted p < 1 - confidence. (Census
    states the 90% rule as |Z| > 1.645; the exact critical value 1.6449 differs
    only for |Z| in [1.64485, 1.645].)

    Args:
        df: Frame with one row per geography (or comparison).
        est1, moe1, est2, moe2: Column names (or aligned arrays).
        year1, year2: Period end years (ints or column names), optional.
        confidence_level: e.g. 0.90 (Census default), 0.95.
        correction: ``holm`` (default; FWER), ``bonferroni``, ``bh`` (FDR,
            suggested for exploratory screening) or ``by``.

    Returns:
        DataFrame indexed like ``df`` (``GEOID``/``NAME`` carried when
        present) with columns ``difference, moe_difference, se_difference,
        z, p_value, p_adjusted, significant_raw, significant_adjusted,
        overlap_years, overlap_factor, family_size, correction,
        confidence_level, notes``. ``difference`` is X1 - X2 and ``z`` is
        signed (positive when X1 > X2). ``moe_difference`` is the 90% MOE
        (1.645 x SE) of the difference, after any overlap adjustment.
    """
    method = _method(correction)
    level = confidence_level / 100.0 if confidence_level > 1 else float(confidence_level)
    if not 0 < level < 1:
        raise ValueError("confidence_level must be in (0, 1)")
    alpha = 1.0 - level
    _z_for_confidence(level)  # validates the level

    x1, m1 = _numeric(df, est1), _numeric(df, moe1)
    x2, m2 = _numeric(df, est2), _numeric(df, moe2)
    n = len(df)
    notes = [[] for _ in range(n)]

    def clean_est(x: np.ndarray, label: str) -> np.ndarray:
        x = x.copy()
        bad = np.isin(x, _SENTINELS)
        for i in np.flatnonzero(bad):
            notes[i].append(f"{label} estimate is Census sentinel {int(x[i])}; not tested")
        x[bad] = np.nan
        for i in np.flatnonzero(np.isnan(x) & ~bad):
            notes[i].append(f"{label} estimate missing; not tested")
        return x

    def clean_moe(m: np.ndarray, label: str) -> np.ndarray:
        m = m.copy()
        controlled = m == CONTROLLED_MOE
        for i in np.flatnonzero(controlled):
            notes[i].append(f"{label} MOE is controlled (*****); SE set to 0 per Census")
        other = np.isin(m, _SENTINELS) & ~controlled
        for i in np.flatnonzero(other):
            notes[i].append(
                f"{label} MOE is Census sentinel {int(m[i])} "
                f"({CENSUS_SENTINELS.get(int(m[i]), '')}); statistical test not appropriate"
            )
        negative = (m < 0) & ~controlled & ~other
        for i in np.flatnonzero(negative):
            notes[i].append(f"{label} MOE is negative and invalid; not tested")
        for i in np.flatnonzero(np.isnan(m) & ~other):
            notes[i].append(f"{label} MOE missing; not tested")
        m[controlled] = 0.0
        m[other | negative] = np.nan
        return m

    x1, x2 = clean_est(x1, "first"), clean_est(x2, "second")
    m1, m2 = clean_moe(m1, "first"), clean_moe(m2, "second")

    y1, y2 = _years(df, year1), _years(df, year2)
    overlap = np.zeros(n, dtype=float)
    factor = np.ones(n, dtype=float)
    if y1 is not None and y2 is not None:
        for i in range(n):
            if np.isnan(y1[i]) or np.isnan(y2[i]):
                overlap[i] = np.nan
                factor[i] = np.nan
                notes[i].append("period year missing; not tested")
                continue
            k = acs5_overlap_years(int(y1[i]), int(y2[i]))
            overlap[i] = k
            factor[i] = overlap_factor(k)
            if k > 0:
                notes[i].append(NOTE_OVERLAP.format(k=k))
            if k >= 5:
                notes[i].append("both estimates are from the same period")

    diff = x1 - x2
    se_diff = np.sqrt((m1 / Z90) ** 2 + (m2 / Z90) ** 2) * factor
    se_diff[np.isnan(diff)] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(se_diff > 0, diff / se_diff, np.where(diff == 0, 0.0, np.sign(diff) * np.inf))
    z = np.where(np.isnan(diff) | np.isnan(se_diff), np.nan, z)
    p = np.array([math.erfc(abs(v) / math.sqrt(2.0)) if not np.isnan(v) else np.nan for v in z])
    for i in np.flatnonzero(np.isfinite(diff) & (se_diff == 0)):
        notes[i].append("both SEs are zero (controlled or fixed values): difference has no sampling error")

    p_adj = np.asarray(adjust_pvalues(p, method), dtype=float)
    tested = ~np.isnan(p)
    family = int(tested.sum())

    sig_raw = pd.array([bool(v < alpha) if t else pd.NA for v, t in zip(p, tested)], dtype="boolean")
    sig_adj = pd.array([bool(v < alpha) if t else pd.NA for v, t in zip(p_adj, tested)], dtype="boolean")

    out = pd.DataFrame(index=df.index)
    for col in ("GEOID", "NAME"):
        if col in df.columns:
            out[col] = df[col].to_numpy()
    out["difference"] = diff
    out["moe_difference"] = se_diff * Z90
    out["se_difference"] = se_diff
    out["z"] = z
    out["p_value"] = p
    out["p_adjusted"] = p_adj
    out["significant_raw"] = sig_raw
    out["significant_adjusted"] = sig_adj
    out["overlap_years"] = overlap
    out["overlap_factor"] = factor
    out["family_size"] = family
    out["correction"] = method
    out["confidence_level"] = level
    out["notes"] = ["; ".join(dict.fromkeys(nn)) for nn in notes]
    return out
