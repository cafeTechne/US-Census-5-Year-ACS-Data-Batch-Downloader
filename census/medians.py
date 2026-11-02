# SPDX-License-Identifier: AGPL-3.0-or-later
"""Medians (and other quantiles) from ACS distribution tables, with MOEs.

Census publishes medians only for its own geographies. For a user-defined area
(a group of tracts, a custom region) the only route is to add up a published
distribution table (B19001 household income, B25063 gross rent, B25075 home
value, ...) and interpolate. This module does that the way Census documents it.

Interpolation (point estimate)
------------------------------
* **Linear interpolation** within the bracket that contains the 50th
  percentile. The ACS Subject Definitions state that ACS medians are
  "calculated using linear interpolation" from a "standard distribution"
  (2023 ACS Subject Definitions, "Income in the Past 12 Months: Median Income"
  and "Derived Measures: Median / Interpolation / Standard Distributions";
  https://www2.census.gov/programs-surveys/acs/tech_docs/subject_definitions/2023_ACSSubjectDefinitions.pdf).
  ``method="auto"`` therefore means linear.
* **Pareto interpolation** is available as ``method="pareto"`` for closed
  brackets with a positive lower bound. Census used it for wide income
  intervals in older CPS/decennial income products, not for ACS; it is offered
  for comparison only and recorded in the result's components.
* The interpolation convention follows the Census PUMS Accuracy of the Data
  median procedure: a bracket runs from A1 (its smallest value) to A2 (the
  smallest value of the next bracket), so "$10,000 to $14,999" spans
  10,000-15,000 (PUMS Accuracy of the Data 2019-2023, "Medians", step 6;
  https://www2.census.gov/programs-surveys/acs/tech_docs/pums/accuracy/2019_2023AccuracyPUMS.pdf).

Open-ended brackets
-------------------
* Median in the open top bracket ("$200,000 or more"): the estimate is the
  bracket's lower bound, marked ``top_coded`` with a "+" note, and the MOE is
  undefined. This mirrors Census's own convention ("250,000+", MOE "***";
  ACS Multiyear Accuracy of the Data 2019-2023, "Exceptions" item 3).
* Bottom bracket ("Less than $10,000"): its lower bound is taken as 0. For
  income this is an assumption (the bracket also holds zero and negative
  incomes). A median in the bottom bracket is interpolated on that assumption,
  marked ``bottom_coded`` (Census would publish "<upper>-"), and its MOE is
  undefined, again mirroring Census's "***".
* Zero total: estimate NaN.

Note that published medians are computed from finer internal distributions
(household income uses $2,500 intervals up to $250,000; gross rent uses the
same 24 brackets B25063 publishes), so a median rebuilt from B19001 differs
slightly from B19013, while one rebuilt from B25063 reproduces B25064 closely.

MOE
---
* **Replicate (exact, preferred):** compute the median on each of the 80
  replicate distributions (Variance Replicate Estimate tables) and apply the
  successive-difference formula in :mod:`census.variance`.
* **Approximation:** the Census "standard error of a 50 percent proportion"
  procedure from the PUMS Accuracy of the Data (2019-2023), "Medians":
  form the 68% interval 50 +/- SE(50%) on the cumulative percent distribution,
  interpolate both limits, SE(median) = (upper - lower) / 2, MOE = 1.645 SE.
  SE(50%) comes from one of:

  - ``design_factor``: the PUMS 5-year generalized formula
    SE(p) = DF * sqrt(19 / B * p * (100 - p)). The constant 19 and the formula
    were checked against all three worked examples in that document (e.g.
    B = 2,549,716 and DF = 1.5 give SE(50%) = 0.20). Census publishes design
    factors only for PUMS, and 19 = (1 - f) / f for a 5% PUMS sample; applied
    to table data it overstates sampling error. Use it only when no bracket
    MOEs are available.
  - ``count_moes`` (default when available): the same generalized-variance
    form Var(Y) = g * Y * (1 - Y / B), with the factor g (which plays the role
    of DF^2 * 19) fitted by least squares to the published bracket MOEs of the
    distribution itself: g = sum(SE_i^2) / sum(Y_i * (1 - Y_i / B)).
    Then SE(50%) = sqrt(g / B * 50 * 50). This is a documented adaptation, not
    a Census-published formula; its agreement with published median MOEs is
    checked in tests/test_medians.py.

Pure numpy/pandas; no network access. Replicate distributions are accepted as
arrays so this module does not depend on :mod:`census.replicate`.
"""

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from census.stats import CENSUS_SENTINELS
from census.variance import (
    METHOD_MEDIAN_INTERPOLATED,
    REPLICATE_COLUMNS,
    REPLICATE_COUNT,
    Z90,
    DerivedEstimate,
    replicate_se,
)

INTERPOLATION_LINEAR = "linear"
INTERPOLATION_PARETO = "pareto"
INTERPOLATION_METHODS = ("auto", INTERPOLATION_LINEAR, INTERPOLATION_PARETO)

MOE_METHOD_REPLICATE = "replicate"
MOE_METHOD_GVF_FITTED = "census_50pct_proportion_fitted_gvf"
MOE_METHOD_DESIGN_FACTOR = "census_50pct_proportion_pums_design_factor"

# PUMS 5-year generalized variance constant: SE(p) = DF * sqrt(19/B * p(100-p)).
# PUMS Accuracy of the Data (2019-2023), "Totals and Percentages".
PUMS_5YR_FACTOR = 19.0
# Design factor for household income used in that document's median example.
PUMS_EXAMPLE_DESIGN_FACTOR_HH_INCOME = 1.5

NOTE_TOP_CODED = (
    "Median falls in the open-ended top bracket; estimate is the bracket's lower "
    "bound and should be read as '{bound:,.0f}+' (Census convention). MOE undefined."
)
NOTE_BOTTOM_CODED = (
    "Median falls in the lowest bracket, whose lower bound is assumed to be 0; "
    "Census would publish '{bound:,.0f}-'. MOE undefined."
)

Bracket = Tuple[str, float, float, str]

_SENTINEL_VALUES = np.array(sorted(CENSUS_SENTINELS), dtype=float)


# ---------------------------------------------------------------------------
# Bracket parsing
# ---------------------------------------------------------------------------

_NUM = r"\$?(-?[\d,]+(?:\.\d+)?)"
_UNIT = r"(?:\s*(?:percent|years?|rooms?|persons?|minutes|hours))?"
_RE_LESS = re.compile(rf"^(?:less than|under)\s+{_NUM}{_UNIT}$", re.I)
_RE_RANGE = re.compile(rf"^{_NUM}{_UNIT}\s+to\s+{_NUM}{_UNIT}$", re.I)
_RE_MORE = re.compile(rf"^{_NUM}{_UNIT}\s+(?:or more|and over|or over|and more|or older)$", re.I)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _step(text: str) -> float:
    """Smallest unit of a label number: 1 for '14,999', 0.1 for '14.9'."""
    if "." in text:
        return 10.0 ** -len(text.split(".", 1)[1])
    return 1.0


def parse_bracket_label(label: str) -> Optional[Tuple[float, float]]:
    """Parse one bracket label into (lower, upper); None if not a bracket.

    ``upper`` is exclusive: the smallest value of the next bracket (A2 in the
    Census PUMS median procedure), so "$10,000 to $14,999" -> (10000, 15000)
    and "10.0 to 14.9 percent" -> (10.0, 15.0). "Less than X" -> (0, X);
    "X or more" -> (X, inf).
    """
    text = label.split("!!")[-1].strip().rstrip(":").strip()
    m = _RE_LESS.match(text)
    if m:
        return 0.0, _num(m.group(1))
    m = _RE_RANGE.match(text)
    if m:
        hi = m.group(2)
        return _num(m.group(1)), round(_num(hi) + _step(hi), 10)
    m = _RE_MORE.match(text)
    if m:
        return _num(m.group(1)), math.inf
    return None


def brackets_from_variables(
    variables: Dict[str, Dict[str, str]], parent: Optional[str] = None
) -> List[Bracket]:
    """Brackets from a {code: {"label": ...}} mapping (one table, one release).

    Only estimate variables (``...E``) whose last label segment parses as a
    bracket are kept; totals, subtotals ("With cash rent:") and non-bracket
    rows ("No cash rent") are excluded. If brackets sit under more than one
    parent row (e.g. a table split by age of householder), ``parent`` selects
    one by a case-insensitive substring of the parent label; otherwise a
    ValueError lists the choices.
    """
    groups: Dict[str, List[Bracket]] = {}
    for code in sorted(variables):
        if not code.endswith("E") or code.endswith(("PE", "EA")):
            continue
        label = str(variables[code].get("label", ""))
        bounds = parse_bracket_label(label)
        if bounds is None:
            continue
        segments = [s.strip() for s in label.split("!!")]
        if segments and segments[0].lower() == "estimate":
            segments = segments[1:]
        parent_label = "!!".join(segments[:-1])
        groups.setdefault(parent_label, []).append(
            (code[:-1], bounds[0], bounds[1], segments[-1])
        )

    if parent is not None:
        groups = {k: v for k, v in groups.items() if parent.lower() in k.lower()}
    if not groups:
        return []
    if len(groups) > 1:
        raise ValueError(
            "Brackets appear under several parent rows; pass parent= one of: "
            + "; ".join(sorted(groups))
        )
    brackets = sorted(next(iter(groups.values())), key=lambda b: b[1])
    _check_contiguous(brackets)
    return brackets


def _check_contiguous(brackets: Sequence[Bracket]) -> None:
    for prev, cur in zip(brackets, brackets[1:]):
        if not math.isclose(prev[2], cur[1], rel_tol=0, abs_tol=1e-9):
            raise ValueError(
                f"Brackets are not contiguous: '{prev[3]}' ends at {prev[2]} but "
                f"'{cur[3]}' starts at {cur[1]}"
            )


def distribution_brackets(
    table: str,
    year: int,
    dataset: str = "acs5",
    parent: Optional[str] = None,
    client: Any = None,
) -> List[Bracket]:
    """Bracket bounds for a distribution table, from that release's labels.

    Returns a list of ``(code, lower, upper, label)`` sorted by ``lower``,
    where ``code`` has no suffix (``B19001_002``), ``upper`` is exclusive (the
    next bracket's lower bound) or ``inf`` for an open top bracket, and a
    "Less than"/"Under" bottom bracket has lower bound 0. Labels come from the
    per-vintage metadata archive (or the API via ``client``), never from a
    hard-coded list, because brackets changed across releases.
    """
    from census.metadata import table_variables  # local import keeps module light

    variables, source = table_variables(table, dataset, int(year), client)
    if not variables:
        raise ValueError(f"No metadata for table {table} in {dataset} {year} (source: {source})")
    brackets = brackets_from_variables(variables, parent=parent)
    if not brackets:
        raise ValueError(f"Table {table} ({dataset} {year}) has no parseable bracket labels")
    return brackets


# ---------------------------------------------------------------------------
# Vectorized core
# ---------------------------------------------------------------------------


def _clean(values: Any) -> np.ndarray:
    arr = np.array(values, dtype=float)
    arr[np.isin(arr, _SENTINEL_VALUES)] = np.nan
    return arr


def _as_bounds(lower: Sequence[float], upper: Sequence[float]) -> Tuple[np.ndarray, np.ndarray]:
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    if lo.ndim != 1 or lo.shape != hi.shape or lo.size == 0:
        raise ValueError("lower_bounds and upper_bounds must be equal-length 1-D sequences")
    if np.any(hi <= lo):
        raise ValueError("each upper bound must exceed its lower bound")
    if np.any(np.isinf(hi[:-1])):
        raise ValueError("only the last bracket may be open-ended")
    if np.any(np.diff(lo) <= 0):
        raise ValueError("brackets must be sorted by lower bound")
    return lo, hi


def _quantile_matrix(
    lower: np.ndarray,
    upper: np.ndarray,
    counts: np.ndarray,
    q: Any,
    interpolation: str = INTERPOLATION_LINEAR,
) -> Dict[str, np.ndarray]:
    """Interpolated q-quantile for each row of ``counts`` (rows x brackets).

    Returns arrays: value, index (bracket), top (in open top bracket),
    bottom (in first bracket), valid (finite total > 0, no missing counts).
    """
    counts = np.atleast_2d(counts)
    m, n = counts.shape
    if n != lower.size:
        raise ValueError(f"expected {lower.size} bracket counts, got {n}")
    q = np.broadcast_to(np.asarray(q, dtype=float), (m,))
    valid = np.all(np.isfinite(counts), axis=1)
    filled = np.where(np.isfinite(counts), counts, 0.0)
    filled = np.clip(filled, 0.0, None)
    total = filled.sum(axis=1)
    valid &= total > 0
    valid &= (q >= 0) & (q <= 1)

    target = q * total
    cum = np.cumsum(filled, axis=1)
    # first bracket whose cumulative count reaches the target (and is non-empty)
    reached = (cum >= target[:, None]) & (filled > 0)
    idx = np.where(reached.any(axis=1), reached.argmax(axis=1), n - 1)
    rows = np.arange(m)
    f = filled[rows, idx]
    before = cum[rows, idx] - f
    lo = lower[idx]
    hi = upper[idx]
    top = np.isinf(hi)
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.clip((target - before) / f, 0.0, 1.0)
        width = np.where(top, 0.0, hi - lo)
        value = lo + frac * width
        if interpolation == INTERPOLATION_PARETO:
            # Two-point Pareto: S(x) = S(lo) * (lo / x) ** theta within the bracket,
            # theta = ln(S(lo) / S(hi)) / ln(hi / lo); solve S(x) = 1 - q.
            s_lo = (total - before) / total
            s_hi = (total - before - f) / total
            s_t = 1.0 - q
            ok = (~top) & (lo > 0) & (s_hi > 0) & (s_t > 0)
            theta = np.log(s_lo / s_hi) / np.log(hi / lo)
            pareto = lo * (s_lo / s_t) ** (1.0 / theta)
            value = np.where(ok, pareto, value)
    value = np.where(top, lo, value)
    # Exactly q of the distribution lies at or below this bracket's upper bound
    # and the next bracket(s) are empty: the quantile is the midpoint of the empty
    # gap, as Census publishes it (e.g. 2019 B25064 for Sterling County, TX:
    # 50.00% at $800 with $800-$899 empty -> $850). Found by validation check F5.
    # Counts are (sums of) whole numbers; a fixed absolute tolerance avoids
    # treating a cumulative count several units past the target as "exact"
    # for large totals (np.isclose's default rtol would).
    exact = (~top) & np.isclose(cum[rows, idx], target, rtol=0.0, atol=1e-6) & (idx < n - 1)
    if np.any(exact):
        later = filled[:, 1:] > 0  # later[r, j - 1] -> bracket j non-empty
        for r in np.flatnonzero(exact):
            following = np.flatnonzero(later[r, idx[r]:])
            if following.size:
                j = idx[r] + 1 + following[0]
                value[r] = (hi[r] + lower[j]) / 2.0
    value = np.where(valid, value, np.nan)
    return {
        "value": value,
        "index": idx,
        "top": top & valid,
        "bottom": (idx == 0) & valid,
        "valid": valid,
        "total": np.where(valid, total, np.nan),
    }


def _resolve_method(method: str) -> str:
    if method not in INTERPOLATION_METHODS:
        raise ValueError(f"method must be one of {INTERPOLATION_METHODS}, got {method!r}")
    return INTERPOLATION_LINEAR if method == "auto" else method


def _coding_notes(lower: np.ndarray, upper: np.ndarray, top: bool, bottom: bool) -> List[str]:
    if top:
        return [NOTE_TOP_CODED.format(bound=lower[-1])]
    if bottom:
        return [NOTE_BOTTOM_CODED.format(bound=upper[0])]
    return []


# ---------------------------------------------------------------------------
# Public scalar API
# ---------------------------------------------------------------------------


def median_from_distribution(
    lower_bounds: Sequence[float],
    upper_bounds: Sequence[float],
    counts: Sequence[float],
    method: str = "auto",
) -> DerivedEstimate:
    """Interpolated median of a grouped distribution (MOE left undefined).

    ``upper_bounds`` are exclusive (the next bracket's lower bound); the last
    may be ``inf``. ``method``: "auto" (= "linear", the ACS method) or
    "pareto". See the module docstring for open-ended bracket conventions.
    """
    interpolation = _resolve_method(method)
    lo, hi = _as_bounds(lower_bounds, upper_bounds)
    c = _clean(counts).reshape(1, -1)
    r = _quantile_matrix(lo, hi, c, 0.5, interpolation)
    notes: List[str] = []
    if not r["valid"][0]:
        if np.isfinite(c).all():
            notes.append("Distribution total is zero; median undefined.")
        else:
            notes.append("Distribution has missing or sentinel counts; median undefined.")
    top, bottom = bool(r["top"][0]), bool(r["bottom"][0])
    notes += _coding_notes(lo, hi, top, bottom)
    idx = int(r["index"][0])
    return DerivedEstimate(
        estimate=float(r["value"][0]),
        moe=math.nan,
        method=METHOD_MEDIAN_INTERPOLATED,
        notes=notes,
        components={
            "interpolation": interpolation,
            "bracket_index": idx if r["valid"][0] else None,
            "bracket": [float(lo[idx]), float(hi[idx])] if r["valid"][0] else None,
            "total": float(r["total"][0]),
            "top_coded": top,
            "bottom_coded": bottom,
            "moe_method": None,
        },
    )


def _with_moe(base: DerivedEstimate, se: float, moe_method: str, notes: List[str]) -> DerivedEstimate:
    comps = dict(base.components)
    comps["moe_method"] = moe_method
    if not math.isfinite(se):
        return DerivedEstimate(
            estimate=base.estimate, moe=math.nan, method=base.method,
            notes=base.notes + notes, components=comps,
        )
    return DerivedEstimate(
        estimate=base.estimate, moe=se * Z90, se=se, method=base.method,
        notes=base.notes + notes, components=comps,
    )


def _replicate_matrix(replicate_counts: Any, n: int) -> np.ndarray:
    reps = _clean(replicate_counts)
    if reps.ndim != 2:
        raise ValueError("replicate_counts must be 2-D (80 x brackets)")
    if reps.shape == (n, REPLICATE_COUNT) and n != REPLICATE_COUNT:
        reps = reps.T
    if reps.shape != (REPLICATE_COUNT, n):
        raise ValueError(f"replicate_counts must have shape ({REPLICATE_COUNT}, {n}), got {reps.shape}")
    return reps


def median_moe_replicates(
    lower_bounds: Sequence[float],
    upper_bounds: Sequence[float],
    counts: Sequence[float],
    replicate_counts: Any,
    method: str = "auto",
) -> DerivedEstimate:
    """Median with its exact replicate MOE.

    ``replicate_counts`` has shape (80, n_brackets): the bracket counts in each
    Variance Replicate Estimate column (Var_Rep1..80). The median is evaluated
    on each replicate distribution and Var = 4/80 * sum (m_r - m)^2.
    A replicate median in the open top bracket takes its lower bound (as the
    full-sample estimate would). The MOE is undefined when the full-sample
    median is top- or bottom-coded.
    """
    interpolation = _resolve_method(method)
    lo, hi = _as_bounds(lower_bounds, upper_bounds)
    base = median_from_distribution(lo, hi, counts, method=interpolation)
    if not math.isfinite(base.estimate) or base.components["top_coded"] or base.components["bottom_coded"]:
        return _with_moe(base, math.nan, MOE_METHOD_REPLICATE, [])
    reps = _replicate_matrix(replicate_counts, lo.size)
    rep = _quantile_matrix(lo, hi, reps, 0.5, interpolation)
    notes = ["MOE from 80 successive-difference replicate medians (exact under the ACS design)."]
    if not rep["valid"].all():
        return _with_moe(base, math.nan, MOE_METHOD_REPLICATE,
                         ["A replicate distribution is empty or missing; replicate MOE undefined."])
    if rep["top"].any():
        notes.append(f"{int(rep['top'].sum())} replicate median(s) fell in the open top bracket and took its lower bound.")
    se = float(replicate_se(base.estimate, rep["value"]))
    return _with_moe(base, se, MOE_METHOD_REPLICATE, notes)


def fitted_gvf_factor(counts: Sequence[float], count_moes: Sequence[float]) -> float:
    """Generalized-variance factor g fitted to published bracket MOEs.

    Model Var(Y_i) = g * Y_i * (1 - Y_i / B) (the form of the Census PUMS
    generalized variance formula, with g in place of DF^2 * 19); least squares
    through the origin gives g = sum(SE_i^2) / sum(Y_i (1 - Y_i / B)).
    Brackets with zero or missing counts or MOEs are ignored.
    """
    y = _clean(counts)
    se = np.abs(_clean(count_moes)) / Z90
    if y.shape != se.shape:
        raise ValueError("counts and count_moes must have the same shape")
    total = np.nansum(np.clip(y, 0, None))
    use = np.isfinite(y) & np.isfinite(se) & (y > 0)
    if total <= 0 or not use.any():
        return math.nan
    x = y[use] * (1.0 - y[use] / total)
    denom = x.sum()
    return float((se[use] ** 2).sum() / denom) if denom > 0 else math.nan


def se_50_percent(
    total: float,
    count_moes: Optional[Sequence[float]] = None,
    counts: Optional[Sequence[float]] = None,
    design_factor: Optional[float] = None,
) -> Tuple[float, str]:
    """Standard error, in percentage points, of a 50 percent proportion.

    With ``design_factor``: DF * sqrt(19 / B * 50 * 50) (PUMS 5-year formula).
    Otherwise from the fitted factor g: sqrt(g / B * 50 * 50).
    """
    if not (total > 0):
        return math.nan, ""
    if design_factor is not None:
        return float(design_factor) * math.sqrt(PUMS_5YR_FACTOR / total * 2500.0), MOE_METHOD_DESIGN_FACTOR
    if count_moes is None or counts is None:
        raise ValueError("pass count_moes (published bracket MOEs) or design_factor")
    g = fitted_gvf_factor(counts, count_moes)
    return (math.sqrt(g / total * 2500.0) if math.isfinite(g) else math.nan), MOE_METHOD_GVF_FITTED


def median_moe_approximate(
    lower_bounds: Sequence[float],
    upper_bounds: Sequence[float],
    counts: Sequence[float],
    count_moes: Optional[Sequence[float]] = None,
    design_factor: Optional[float] = None,
    method: str = "auto",
) -> DerivedEstimate:
    """Median with the Census "standard error of a 50 percent proportion" MOE.

    Pass ``count_moes`` (published 90% MOEs of the bracket counts; preferred)
    or ``design_factor`` (PUMS formula; see module docstring for caveats). If
    both are given the design factor is used, since it was asked for
    explicitly. The interval limits are always linearly interpolated, as in
    the Census procedure.
    """
    interpolation = _resolve_method(method)
    lo, hi = _as_bounds(lower_bounds, upper_bounds)
    base = median_from_distribution(lo, hi, counts, method=interpolation)
    if not math.isfinite(base.estimate) or base.components["top_coded"] or base.components["bottom_coded"]:
        moe_method = MOE_METHOD_DESIGN_FACTOR if design_factor is not None else MOE_METHOD_GVF_FITTED
        return _with_moe(base, math.nan, moe_method, [])
    se50, moe_method = se_50_percent(base.components["total"], count_moes, counts, design_factor)
    return _with_moe(base, *_interval_se(lo, hi, _clean(counts), se50, moe_method))


def median_confidence_bounds(
    lower_bounds: Sequence[float],
    upper_bounds: Sequence[float],
    counts: Sequence[float],
    se_50: float,
) -> Tuple[float, float]:
    """Steps 4-7 of the Census median procedure: interpolate p = 50 -/+ SE(50%).

    ``se_50`` is in percentage points. Returns (lower bound, upper bound) of the
    68% interval, linearly interpolated as A1 + (p - C1) / (C2 - C1) * (A2 - A1).
    A limit in the open top bracket is returned as NaN.
    """
    lo, hi = _as_bounds(lower_bounds, upper_bounds)
    c = _clean(counts)
    p = np.array([0.5 - se_50 / 100.0, 0.5 + se_50 / 100.0])
    r = _quantile_matrix(lo, hi, np.vstack([c, c]), p, INTERPOLATION_LINEAR)
    vals = np.where(r["top"], np.nan, r["value"])
    return float(vals[0]), float(vals[1])


def _interval_se(lo, hi, counts, se50: float, moe_method: str) -> Tuple[float, str, List[str]]:
    if not math.isfinite(se50):
        return math.nan, moe_method, ["Could not estimate SE(50%); MOE undefined."]
    lower, upper = median_confidence_bounds(lo, hi, counts, se50)
    if not (math.isfinite(lower) and math.isfinite(upper)):
        return math.nan, moe_method, [
            "A limit of the 68% interval falls in the open top bracket or is undefined; MOE undefined."
        ]
    se = (upper - lower) / 2.0
    label = "PUMS design factor" if moe_method == MOE_METHOD_DESIGN_FACTOR else "GVF fitted to bracket MOEs"
    return se, moe_method, [
        f"Approximate MOE: Census 50-percent-proportion method (SE(50%) = {se50:.4f} pp, {label})."
    ]


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------


def _column(df: pd.DataFrame, code: str, suffix: str) -> Optional[str]:
    for name in (f"{code}{suffix}", code if suffix == "E" else None):
        if name and name in df.columns:
            return name
    return None


def _bracket_matrix(df: pd.DataFrame, brackets: Sequence[Bracket], suffix: str) -> Optional[np.ndarray]:
    cols = [_column(df, b[0], suffix) for b in brackets]
    if any(c is None for c in cols):
        if suffix == "E":
            missing = [b[0] + "E" for b, c in zip(brackets, cols) if c is None]
            raise KeyError(f"distribution columns missing from frame: {missing}")
        return None
    return _clean(df[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float))


def _resolve_brackets(table: str, year: Optional[int], brackets, dataset: str) -> List[Bracket]:
    if brackets is not None:
        return list(brackets)
    if year is None:
        raise ValueError("pass year (to read bracket labels) or brackets")
    return distribution_brackets(table, year, dataset=dataset)


def _replicates_for(
    replicate_frame: pd.DataFrame, brackets: Sequence[Bracket], geoids: Optional[Iterable[str]]
) -> np.ndarray:
    """Sum replicate counts over geographies -> (80, n_brackets).

    ``replicate_frame`` follows the census.replicate long contract: columns
    GEOID, variable (no suffix), rep_1..rep_80.
    """
    frame = replicate_frame
    if geoids is not None:
        wanted = {str(g) for g in geoids}
        frame = frame[frame["GEOID"].astype(str).isin(wanted)]
        absent = sorted(wanted - set(frame["GEOID"].astype(str)))
        if absent:
            # Summing replicates over fewer (or more) geographies than the
            # estimate would give a meaningless "exact" MOE.
            raise KeyError(f"replicate frame lacks GEOIDs: {absent[:10]}")
    frame = frame.assign(variable=frame["variable"].astype(str).str.rstrip("E"))
    summed = frame.groupby("variable")[REPLICATE_COLUMNS].sum(min_count=1)
    codes = [b[0] for b in brackets]
    missing = [c for c in codes if c not in summed.index]
    if missing:
        raise KeyError(f"replicate frame lacks variables: {missing}")
    return summed.loc[codes].to_numpy(dtype=float).T


def median_for_group(
    df_wide: pd.DataFrame,
    table: str,
    year: Optional[int] = None,
    geoids: Optional[Iterable[str]] = None,
    geoid_column: str = "GEOID",
    brackets: Optional[Sequence[Bracket]] = None,
    replicate_frame: Optional[pd.DataFrame] = None,
    design_factor: Optional[float] = None,
    method: str = "auto",
    dataset: str = "acs5",
) -> DerivedEstimate:
    """Median of a distribution table for a group of geographies combined.

    Bracket counts are additive, so they are summed across the selected rows
    (``geoids`` in ``geoid_column``; all rows when ``geoids`` is None) and the
    median is interpolated from the summed distribution. Medians themselves
    are never averaged. MOE, in order of preference:

    1. ``replicate_frame`` given (census.replicate long contract): exact
       replicate MOE from the summed replicate distributions.
    2. ``design_factor`` given: PUMS design-factor approximation.
    3. bracket MOE columns (``<code>M``) present: bracket MOEs are combined by
       root-sum-of-squares (Census sum approximation) and used to fit the
       generalized-variance factor for the 50-percent-proportion method.
    4. otherwise the MOE is left undefined.
    """
    brackets = _resolve_brackets(table, year, brackets, dataset)
    rows = df_wide
    if geoids is not None:
        wanted = [str(g) for g in geoids]
        rows = df_wide[df_wide[geoid_column].astype(str).isin(wanted)]
        missing = sorted(set(wanted) - set(rows[geoid_column].astype(str)))
        if missing:
            raise KeyError(f"GEOIDs not in frame: {missing}")
    if rows.empty:
        raise ValueError("no rows selected")
    lo = np.array([b[1] for b in brackets])
    hi = np.array([b[2] for b in brackets])
    est = _bracket_matrix(rows, brackets, "E")
    counts = est.sum(axis=0) if np.isfinite(est).all() else np.full(lo.size, np.nan)
    group_note = [f"Distribution summed over {len(rows)} geographies before interpolation."]

    if replicate_frame is not None:
        # Always restrict replicates to exactly the rows summed for the estimate.
        selected = rows[geoid_column].astype(str).tolist()
        reps = _replicates_for(replicate_frame, brackets, selected)
        result = median_moe_replicates(lo, hi, counts, reps, method=method)
    elif design_factor is not None:
        result = median_moe_approximate(lo, hi, counts, design_factor=design_factor, method=method)
    else:
        moes = _bracket_matrix(rows, brackets, "M")
        if moes is not None:
            combined = np.sqrt(np.sum(moes ** 2, axis=0))
            result = median_moe_approximate(lo, hi, counts, count_moes=combined, method=method)
        else:
            result = median_from_distribution(lo, hi, counts, method=method)
            result.notes.append("No bracket MOEs or replicates supplied; MOE undefined.")
    result.notes = group_note + result.notes
    result.components.update({"table": table.upper(), "year": year, "n_geographies": int(len(rows))})
    return result


def median_for_rows(
    df_wide: pd.DataFrame,
    table: str,
    year: Optional[int] = None,
    brackets: Optional[Sequence[Bracket]] = None,
    approximate_moe: bool = True,
    design_factor: Optional[float] = None,
    method: str = "auto",
    dataset: str = "acs5",
) -> pd.DataFrame:
    """Vectorized median for every row (geography) of a wide frame.

    Returns a frame aligned to ``df_wide.index`` with columns ``estimate``,
    ``moe``, ``se``, ``cv_percent``, ``method``, ``moe_method``,
    ``top_coded``, ``bottom_coded``, ``notes``. With ``approximate_moe`` the
    MOE uses the 50-percent-proportion method (fitted GVF from ``<code>M``
    columns, or ``design_factor``); top- and bottom-coded medians get NaN MOE.
    Use :func:`median_moe_replicates` per row for exact replicate MOEs.
    """
    interpolation = _resolve_method(method)
    brackets = _resolve_brackets(table, year, brackets, dataset)
    lo = np.array([b[1] for b in brackets])
    hi = np.array([b[2] for b in brackets])
    lo, hi = _as_bounds(lo, hi)
    counts = _bracket_matrix(df_wide, brackets, "E")
    r = _quantile_matrix(lo, hi, counts, 0.5, interpolation)
    m = counts.shape[0]
    se = np.full(m, np.nan)
    moe_method = np.full(m, None, dtype=object)

    moes = _bracket_matrix(df_wide, brackets, "M") if approximate_moe and design_factor is None else None
    if approximate_moe and (design_factor is not None or moes is not None):
        total = r["total"]
        if design_factor is not None:
            se50 = float(design_factor) * np.sqrt(PUMS_5YR_FACTOR / total * 2500.0)
            moe_method[:] = MOE_METHOD_DESIGN_FACTOR
        else:
            g = np.array([fitted_gvf_factor(counts[i], moes[i]) for i in range(m)])
            se50 = np.sqrt(g / total * 2500.0)
            moe_method[:] = MOE_METHOD_GVF_FITTED
        lo_q = _quantile_matrix(lo, hi, counts, 0.5 - se50 / 100.0, INTERPOLATION_LINEAR)
        hi_q = _quantile_matrix(lo, hi, counts, 0.5 + se50 / 100.0, INTERPOLATION_LINEAR)
        ok = r["valid"] & ~r["top"] & ~r["bottom"] & ~hi_q["top"] & np.isfinite(se50)
        se = np.where(ok, (hi_q["value"] - lo_q["value"]) / 2.0, np.nan)

    notes = []
    for i in range(m):
        n: List[str] = []
        if not r["valid"][i]:
            n.append("Median undefined (zero total or missing counts).")
        n += _coding_notes(lo, hi, bool(r["top"][i]), bool(r["bottom"][i]))
        notes.append(" ".join(n))
    est = r["value"]
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
    return pd.DataFrame(
        {
            "estimate": est,
            "moe": se * Z90,
            "se": se,
            "cv_percent": cv,
            "method": METHOD_MEDIAN_INTERPOLATED,
            "moe_method": moe_method,
            "interpolation": interpolation,
            "top_coded": r["top"],
            "bottom_coded": r["bottom"],
            "notes": notes,
        },
        index=df_wide.index,
    )
