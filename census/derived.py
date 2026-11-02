# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derived ACS estimates with approximate 90% margins of error.

Implements the Census Bureau's approximation formulas for user-derived
estimates. The specification is:

* ACS General Handbook (2020), "Understanding and Using American Community
  Survey Data: What All Data Users Need to Know", chapter 8, "Calculating
  Measures of Error for Derived Estimates", formulas (1)-(9).
  https://www.census.gov/content/dam/Census/library/publications/2020/acs/acs_general_handbook_2020_ch08.pdf
* "Instructions for Applying Statistical Testing to American Community Survey
  Data" (2025 edition) and "Worked Examples for Approximating Margins of Error"
  (2025 edition), linked from
  https://www.census.gov/programs-surveys/acs/technical-documentation/code-lists.html

Formulas (MOE may be used in place of SE throughout; they differ by 1.645):

=================  ==================  ==============================================
function           estimate            MOE
=================  ==================  ==============================================
sum_estimates      X1 + ... + Xn       sqrt(sum MOE_i^2)                    (HB 1)
difference         X - Y               sqrt(MOE_X^2 + MOE_Y^2)              (HB 1)
proportion         P = X / Y           (1/Y) sqrt(MOE_X^2 - P^2 MOE_Y^2)    (HB 6)
ratio              R = X / Y           (1/Y) sqrt(MOE_X^2 + R^2 MOE_Y^2)    (HB 7)
percent_change     100 (C - P) / P     100 * MOE(C / P), ratio formula      (HB 8)
product            X * Y               sqrt(X^2 MOE_Y^2 + Y^2 MOE_X^2)      (HB 9)
scale              k * X               abs(k) * MOE_X   (k a constant without error)
=================  ==================  ==============================================

Proportion special cases (Instructions for Statistical Testing, 2025, p. 4):
if the radicand is negative, the ratio formula is used instead (method
``proportion_ratio_fallback``); if P equals 1, SE(P) = SE(X) / Y.

All formulas ignore the covariance between components, so they can over- or
under-state the true MOE. The exact alternative is replicate variance from the
Variance Replicate Estimate tables (``census.replicate``).

Zero estimates in sums
----------------------
Earlier Census guidance told users approximating the MOE of a sum to include
only the largest MOE among component estimates that are zero ("When
approximating a sum, use only the largest zero estimate MOE, once": S. Fuller,
"Using ACS Estimates and Margins of Error", Census Bureau training slides,
April 6, 2016, slide 26). The current documents no longer contain that rule:
the 2018 and 2020 General Handbooks (ch. 8, formula 1) and the 2025 Worked
Examples (formula 2) square and sum the MOE of *every* component. This module
follows the current text by default (``zero_estimate_rule="all"``) and keeps
the old behavior behind ``zero_estimate_rule="largest"``.

Inputs and outputs
------------------
Every function accepts scalars or aligned arrays / pandas Series. Scalar
inputs return :class:`census.variance.DerivedEstimate`; array inputs return a
DataFrame with columns ``estimate, moe, se, cv_percent, method, notes``
(``notes`` is a "; "-joined string, empty when there is nothing to report).

Census sentinel codes are handled per element: a sentinel estimate makes the
result undefined (NaN); a controlled-estimate MOE (``-555555555``, shown as
``*****``) is treated as 0 because the estimate has no sampling error; any
other sentinel or missing MOE makes the result MOE NaN, with a note naming the
component.
"""

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from .stats import CENSUS_SENTINELS, acs5_overlap_years, parse_variable_code
from .variance import (
    METHOD_DIFFERENCE,
    METHOD_PERCENT_CHANGE,
    METHOD_PRODUCT,
    METHOD_PROPORTION,
    METHOD_PROPORTION_AS_RATIO,
    METHOD_RATIO,
    METHOD_SUM,
    Z90,
    DerivedEstimate,
)

METHOD_SCALE = "scale"

CONTROLLED_MOE = -555555555
SENTINEL_VALUES = np.array(sorted(CENSUS_SENTINELS), dtype=float)

ZERO_RULE_ALL = "all"
ZERO_RULE_LARGEST = "largest"
ZERO_RULES = (ZERO_RULE_ALL, ZERO_RULE_LARGEST)

# The handbook warns the sum approximation degrades as components are added.
MANY_COMPONENTS = 4

RESULT_COLUMNS = ["estimate", "moe", "se", "cv_percent", "method", "notes"]

Number = Union[int, float]
Input = Union[Number, Sequence[Number], np.ndarray, pd.Series]

NOTE_COVARIANCE = (
    "approximation ignores covariance between the components "
    "(ACS Handbook 2020 ch. 8); use replicate tables (census.replicate) for an exact MOE"
)


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> np.ndarray:
    """Coerce a scalar, sequence or Series to a float array (non-numeric -> NaN)."""
    if isinstance(value, (pd.Series, pd.Index)):
        return pd.to_numeric(pd.Series(value), errors="coerce").to_numpy(dtype=float)
    if isinstance(value, pd.DataFrame):
        return value.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    arr = np.asarray(value, dtype=object)
    if arr.ndim == 0:
        v = arr.item()
        if v is None:
            return np.array(math.nan)
        try:
            return np.array(float(v))
        except (TypeError, ValueError):
            return np.array(math.nan)
    flat = pd.to_numeric(pd.Series(arr.ravel()), errors="coerce").to_numpy(dtype=float)
    return flat.reshape(arr.shape)


def _common_index(values: Iterable[Any]) -> Optional[pd.Index]:
    index: Optional[pd.Index] = None
    for value in values:
        if isinstance(value, (pd.Series, pd.DataFrame)):
            if index is None:
                index = value.index
            elif not index.equals(value.index):
                raise ValueError(
                    "pandas inputs have different indexes; align them first "
                    "(e.g. with Series.reindex) so components refer to the same rows"
                )
    return index


def _prepare(*values: Any) -> Tuple[List[np.ndarray], bool, Optional[pd.Index]]:
    """Broadcast inputs to aligned 1-D float arrays.

    Returns (arrays, is_scalar, index).
    """
    index = _common_index(values)
    arrays = [_to_float(v) for v in values]
    scalar = all(a.ndim == 0 for a in arrays)
    try:
        arrays = [np.atleast_1d(a) for a in np.broadcast_arrays(*arrays)]
    except ValueError as exc:
        raise ValueError(f"inputs cannot be aligned: {exc}") from None
    if arrays and arrays[0].ndim > 1:
        raise ValueError("inputs must be scalars or one-dimensional")
    return [a.astype(float, copy=True) for a in arrays], scalar, index


def _sentinel_text(value: float) -> str:
    return CENSUS_SENTINELS.get(int(value), "Census sentinel")


def _nm(name: Union[str, Sequence[str]], i: int) -> str:
    """Component name for element i (one name for all, or one per element)."""
    return name if isinstance(name, str) else str(name[i])


def _clean_estimate(est: np.ndarray, name: Union[str, Sequence[str]], notes: List[List[str]]) -> np.ndarray:
    est = est.copy()
    sentinel = np.isin(est, SENTINEL_VALUES)
    for i in np.flatnonzero(sentinel):
        notes[i].append(
            f"{_nm(name, i)} estimate is Census sentinel {int(est[i])} "
            f"({_sentinel_text(est[i])}); result undefined"
        )
    est[sentinel] = np.nan
    for i in np.flatnonzero(np.isnan(est) & ~sentinel):
        notes[i].append(f"{_nm(name, i)} estimate is missing; result undefined")
    return est


def _clean_moe(moe: np.ndarray, name: Union[str, Sequence[str]], notes: List[List[str]]) -> np.ndarray:
    moe = moe.copy()
    controlled = moe == CONTROLLED_MOE
    for i in np.flatnonzero(controlled):
        notes[i].append(
            f"{_nm(name, i)} MOE is -555555555 (*****): estimate is controlled and has no "
            "sampling error, MOE treated as 0"
        )
    sentinel = np.isin(moe, SENTINEL_VALUES) & ~controlled
    for i in np.flatnonzero(sentinel):
        notes[i].append(
            f"{_nm(name, i)} MOE undefined: Census sentinel {int(moe[i])} "
            f"({_sentinel_text(moe[i])}); result MOE undefined"
        )
    negative = (moe < 0) & ~controlled & ~sentinel
    for i in np.flatnonzero(negative):
        notes[i].append(f"{_nm(name, i)} MOE {moe[i]:g} is negative and invalid; result MOE undefined")
    missing = np.isnan(moe) & ~sentinel
    for i in np.flatnonzero(missing):
        notes[i].append(f"{_nm(name, i)} MOE is missing; result MOE undefined")
    moe[controlled] = 0.0
    moe[sentinel | negative] = np.nan
    return moe


def _clean_pair(
    est: np.ndarray, moe: np.ndarray, name: str, notes: List[List[str]]
) -> Tuple[np.ndarray, np.ndarray]:
    return _clean_estimate(est, name, notes), _clean_moe(moe, name, notes)


def _finish(
    est: np.ndarray,
    moe: np.ndarray,
    method: Union[str, np.ndarray],
    notes: List[List[str]],
    scalar: bool,
    index: Optional[pd.Index],
    components: Optional[Dict[str, Any]] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    est = np.asarray(est, dtype=float)
    moe = np.asarray(moe, dtype=float).copy()
    moe[np.isnan(est)] = np.nan  # an undefined estimate has no meaningful MOE
    se = moe / Z90
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where((est != 0) & np.isfinite(est), se / np.abs(est) * 100.0, np.nan)
    methods = np.broadcast_to(np.asarray(method, dtype=object), est.shape)
    # Deduplicate notes while keeping their order.
    notes = [list(dict.fromkeys(n)) for n in notes]
    if scalar:
        return DerivedEstimate(
            estimate=float(est[0]),
            moe=float(moe[0]),
            method=str(methods[0]),
            se=float(se[0]),
            cv_percent=float(cv[0]),
            notes=notes[0],
            components=dict(components or {}),
        )
    out = pd.DataFrame(
        {
            "estimate": est,
            "moe": moe,
            "se": se,
            "cv_percent": cv,
            "method": [str(m) for m in methods],
            "notes": ["; ".join(n) for n in notes],
        },
        index=index if index is not None and len(index) == len(est) else None,
    )
    return out


def _table_of(code: str) -> Optional[str]:
    code = str(code)
    parsed = parse_variable_code(code)
    base = parsed[0] if parsed else code
    if "_" not in base:
        return None
    return base.rsplit("_", 1)[0]


def _same_table_note(codes: Optional[Sequence[str]]) -> Optional[str]:
    if not codes or len(codes) < 2:
        return None
    tables = {_table_of(c) for c in codes}
    if len(tables) == 1 and None not in tables:
        return f"both inputs come from table {tables.pop()}; {NOTE_COVARIANCE}"
    return None


def _apply_note(notes: List[List[str]], note: Optional[str]) -> None:
    if note:
        for n in notes:
            n.append(note)


def _components(scalar: bool, **kwargs: Any) -> Dict[str, Any]:
    if not scalar:
        return {}
    out: Dict[str, Any] = {}
    for key, value in kwargs.items():
        arr = _to_float(value)
        out[key] = float(arr) if arr.ndim == 0 else arr.tolist()
    return out


# ---------------------------------------------------------------------------
# Kernels (vectorised, inputs already cleaned)
# ---------------------------------------------------------------------------


def _ratio_moe(r: np.ndarray, den: np.ndarray, moe_num: np.ndarray, moe_den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.sqrt(moe_num**2 + r**2 * moe_den**2) / np.abs(den)


def _zero_denominator(den: np.ndarray, notes: List[List[str]], what: str) -> np.ndarray:
    zero = den == 0
    for i in np.flatnonzero(zero):
        notes[i].append(f"denominator is zero: {what} undefined")
    return zero


# ---------------------------------------------------------------------------
# Public formulas
# ---------------------------------------------------------------------------


def _component_matrix(
    values: Any, label: str
) -> Tuple[np.ndarray, bool, Optional[pd.Index], Optional[List[str]]]:
    """Return (matrix rows=results, cols=components, scalar, index, names)."""
    if isinstance(values, pd.DataFrame):
        return (
            _to_float(values),
            False,
            values.index,
            [str(c) for c in values.columns],
        )
    if isinstance(values, pd.Series):
        return _to_float(values)[None, :], True, None, [str(i) for i in values.index]
    if isinstance(values, np.ndarray):
        arr = _to_float(values)
        if arr.ndim == 1:
            return arr[None, :], True, None, None
        if arr.ndim == 2:
            return arr, False, None, None
        raise ValueError(f"{label} must be one- or two-dimensional")
    items = list(values)
    if items and any(np.ndim(item) > 0 for item in items):
        # A list of aligned component arrays: one sum per row.
        index = _common_index(items)
        cols = [np.atleast_1d(_to_float(item)) for item in items]
        try:
            stacked = np.column_stack(np.broadcast_arrays(*cols))
        except ValueError as exc:
            raise ValueError(f"{label} components cannot be aligned: {exc}") from None
        names = [str(item.name) if isinstance(item, pd.Series) and item.name is not None else None for item in items]
        return stacked, False, index, names if all(names) else None  # type: ignore[return-value]
    return _to_float(items)[None, :], True, None, None


def sum_estimates(
    estimates: Any,
    moes: Any,
    zero_estimate_rule: str = ZERO_RULE_ALL,
    names: Optional[Sequence[str]] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """Sum estimates; MOE = sqrt(sum of squared component MOEs).

    ACS General Handbook (2020) ch. 8, formula (1). Covariance between the
    components is ignored, so the approximation drifts from the true MOE as
    more components are combined; a note is added for more than
    ``MANY_COMPONENTS`` components. Controlled components (MOE ``*****``)
    contribute 0 and are noted, since the Worked Examples (2025) warn sums
    involving controlled estimates can be "tremendously different".

    Args:
        estimates: Components of one sum (a 1-D list, array or Series), or
            many sums at once: a DataFrame / 2-D array (one sum per row, one
            component per column) or a list of aligned arrays / Series (one
            per component).
        moes: Component MOEs in the same layout (matched by position).
        zero_estimate_rule: ``"all"`` (default, current guidance: every
            component MOE is included) or ``"largest"`` (pre-2018 guidance:
            among components whose estimate is zero, include only the largest
            MOE, once). See the module docstring for citations.
        names: Component names used in notes (defaults to DataFrame columns,
            Series names or ``component i``).

    Returns:
        DerivedEstimate for a single sum, else a DataFrame (one row per sum).
    """
    if zero_estimate_rule not in ZERO_RULES:
        raise ValueError(f"zero_estimate_rule must be one of {ZERO_RULES}, got {zero_estimate_rule!r}")
    est_m, scalar, index, est_names = _component_matrix(estimates, "estimates")
    moe_m, moe_scalar, moe_index, _ = _component_matrix(moes, "moes")
    if est_m.shape != moe_m.shape:
        raise ValueError(f"estimates {est_m.shape} and moes {moe_m.shape} have different shapes")
    if index is not None and moe_index is not None and len(index) == len(moe_index):
        if not index.equals(moe_index):
            raise ValueError("estimates and moes have different indexes")
    if index is None:
        index = moe_index
    n_rows, n_comp = est_m.shape
    if n_comp == 0:
        raise ValueError("at least one component is required")
    comp_names = list(names) if names is not None else est_names
    if comp_names is None or len(comp_names) != n_comp:
        comp_names = [f"component {i + 1}" for i in range(n_comp)]

    notes: List[List[str]] = [[] for _ in range(n_rows)]
    clean_est = np.empty_like(est_m)
    clean_moe = np.empty_like(moe_m)
    for j in range(n_comp):
        clean_est[:, j] = _clean_estimate(est_m[:, j], comp_names[j], notes)
        clean_moe[:, j] = _clean_moe(moe_m[:, j], comp_names[j], notes)

    total = clean_est.sum(axis=1)  # NaN if any component is undefined
    sq = clean_moe**2
    if zero_estimate_rule == ZERO_RULE_LARGEST:
        zero = clean_est == 0
        n_zero = zero.sum(axis=1)
        for i in np.flatnonzero(n_zero > 1):
            zero_cols = np.flatnonzero(zero[i])
            zero_sq = sq[i, zero_cols]
            keep = np.nan if np.isnan(zero_sq).any() else zero_sq.max()
            sq[i, zero_cols] = 0.0
            sq[i, zero_cols[0]] = keep
            notes[i].append(
                f"zero_estimate_rule='largest': only the largest MOE among {int(n_zero[i])} "
                "zero estimates was included (pre-2018 Census guidance)"
            )
    moe = np.sqrt(sq.sum(axis=1))

    controlled = (moe_m == CONTROLLED_MOE).any(axis=1)
    for i in np.flatnonzero(controlled):
        notes[i].append(
            "sum includes a controlled estimate; Census warns the approximate MOE "
            "can differ substantially from the true MOE"
        )
    if n_comp > MANY_COMPONENTS:
        _apply_note(
            notes,
            f"sum of {n_comp} components: the approximation grows less accurate as "
            f"components are added; {NOTE_COVARIANCE}",
        )
    components = (
        {"estimates": clean_est[0].tolist(), "moes": clean_moe[0].tolist(), "names": comp_names}
        if scalar
        else None
    )
    return _finish(total, moe, METHOD_SUM, notes, scalar and moe_scalar, index, components)


def difference(
    x: Input,
    moe_x: Input,
    y: Input,
    moe_y: Input,
    codes: Optional[Tuple[str, str]] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """X - Y with MOE sqrt(MOE_X^2 + MOE_Y^2) (Handbook 2020 ch. 8, formula 1).

    ``codes`` (variable codes of X and Y) enables a covariance note when both
    come from the same table.
    """
    (x, mx, y, my), scalar, index = _prepare(x, moe_x, y, moe_y)
    notes: List[List[str]] = [[] for _ in range(len(x))]
    x, mx = _clean_pair(x, mx, "X", notes)
    y, my = _clean_pair(y, my, "Y", notes)
    est = x - y
    moe = np.sqrt(mx**2 + my**2)
    _apply_note(notes, _same_table_note(codes))
    return _finish(est, moe, METHOD_DIFFERENCE, notes, scalar, index,
                   _components(scalar, x=x[0], moe_x=mx[0], y=y[0], moe_y=my[0]))


def proportion(
    num: Input,
    moe_num: Input,
    den: Input,
    moe_den: Input,
    as_percent: bool = False,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """P = X / Y where X is a subset of Y (Handbook 2020 ch. 8, formula 6).

    MOE(P) = (1/Y) sqrt(MOE_X^2 - P^2 MOE_Y^2). Special cases from the
    Instructions for Applying Statistical Testing (2025), p. 4:

    * radicand negative: the ratio formula (plus sign) is used and the method
      is ``proportion_ratio_fallback``;
    * P = 1: MOE(P) = MOE_X / Y.

    ``as_percent=True`` multiplies estimate and MOE by 100. A zero
    denominator gives NaN with a note; P outside [0, 1] is noted because it
    means the numerator is not a subset of the denominator.
    """
    (x, mx, y, my), scalar, index = _prepare(num, moe_num, den, moe_den)
    notes: List[List[str]] = [[] for _ in range(len(x))]
    x, mx = _clean_pair(x, mx, "numerator", notes)
    y, my = _clean_pair(y, my, "denominator", notes)
    zero = _zero_denominator(y, notes, "proportion")
    with np.errstate(divide="ignore", invalid="ignore"):
        p = np.where(zero, np.nan, x / y)
        radicand = mx**2 - p**2 * my**2
        moe = np.sqrt(np.where(radicand >= 0, radicand, np.nan)) / np.abs(y)
    method = np.full(len(x), METHOD_PROPORTION, dtype=object)

    one = (p == 1) & ~np.isnan(mx)
    moe = np.where(one, mx / np.where(zero, np.nan, np.abs(y)), moe)
    for i in np.flatnonzero(one):
        notes[i].append("P = 1: MOE = MOE(numerator) / denominator (Census special case)")

    fallback = (radicand < 0) & ~one
    if fallback.any():
        moe = np.where(fallback, _ratio_moe(p, y, mx, my), moe)
        method[fallback] = METHOD_PROPORTION_AS_RATIO
        for i in np.flatnonzero(fallback):
            notes[i].append(
                "proportion radicand MOE_X^2 - P^2*MOE_Y^2 is negative; ratio formula used "
                "(Census guidance)"
            )
    outside = (p > 1) | (p < 0)
    for i in np.flatnonzero(outside):
        notes[i].append(
            f"proportion {p[i]:.6g} is outside [0, 1]: the numerator is not a subset of the "
            "denominator; use ratio()"
        )
    if as_percent:
        p, moe = p * 100.0, moe * 100.0
    return _finish(p, moe, method, notes, scalar, index,
                   _components(scalar, num=x[0], moe_num=mx[0], den=y[0], moe_den=my[0],
                               as_percent=float(as_percent)))


def ratio(
    num: Input,
    moe_num: Input,
    den: Input,
    moe_den: Input,
    as_percent: bool = False,
    codes: Optional[Tuple[str, str]] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """R = X / Y, X not a subset of Y (Handbook 2020 ch. 8, formula 7).

    MOE(R) = (1/Y) sqrt(MOE_X^2 + R^2 MOE_Y^2). Means, per-capita values and
    persons-per-household are ratios. A zero denominator gives NaN with a note.
    ``codes`` enables a covariance note when both inputs share a table.
    """
    (x, mx, y, my), scalar, index = _prepare(num, moe_num, den, moe_den)
    notes: List[List[str]] = [[] for _ in range(len(x))]
    x, mx = _clean_pair(x, mx, "numerator", notes)
    y, my = _clean_pair(y, my, "denominator", notes)
    zero = _zero_denominator(y, notes, "ratio")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(zero, np.nan, x / y)
    moe = _ratio_moe(r, y, mx, my)
    _apply_note(notes, _same_table_note(codes))
    if as_percent:
        r, moe = r * 100.0, moe * 100.0
    return _finish(r, moe, METHOD_RATIO, notes, scalar, index,
                   _components(scalar, num=x[0], moe_num=mx[0], den=y[0], moe_den=my[0],
                               as_percent=float(as_percent)))


def product(
    x: Input,
    moe_x: Input,
    y: Input,
    moe_y: Input,
    codes: Optional[Tuple[str, str]] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """X * Y with MOE sqrt(X^2 MOE_Y^2 + Y^2 MOE_X^2) (Handbook 2020 ch. 8, formula 9).

    When one factor is a published percentage, pass it as a proportion
    (82.4% -> 0.824, MOE 0.1 -> 0.001), as in the handbook example.
    """
    (x, mx, y, my), scalar, index = _prepare(x, moe_x, y, moe_y)
    notes: List[List[str]] = [[] for _ in range(len(x))]
    x, mx = _clean_pair(x, mx, "X", notes)
    y, my = _clean_pair(y, my, "Y", notes)
    est = x * y
    moe = np.sqrt(x**2 * my**2 + y**2 * mx**2)
    _apply_note(notes, _same_table_note(codes))
    return _finish(est, moe, METHOD_PRODUCT, notes, scalar, index,
                   _components(scalar, x=x[0], moe_x=mx[0], y=y[0], moe_y=my[0]))


def percent_change(
    current: Input,
    moe_current: Input,
    prior: Input,
    moe_prior: Input,
    year_current: Optional[int] = None,
    year_prior: Optional[int] = None,
) -> Union[DerivedEstimate, pd.DataFrame]:
    """100 (C - P) / P with MOE = 100 * MOE(C / P) by the ratio formula.

    Handbook 2020 ch. 8, formula (8). The formula ignores the correlation
    between overlapping periods; when ``year_current`` and ``year_prior``
    (ACS 5-year period end years) overlap, a note says so. A zero prior
    estimate gives NaN with a note.
    """
    (c, mc, p, mp), scalar, index = _prepare(current, moe_current, prior, moe_prior)
    notes: List[List[str]] = [[] for _ in range(len(c))]
    c, mc = _clean_pair(c, mc, "current", notes)
    p, mp = _clean_pair(p, mp, "prior", notes)
    zero = _zero_denominator(p, notes, "percent change")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(zero, np.nan, c / p)
    moe = 100.0 * _ratio_moe(r, p, mc, mp)
    est = 100.0 * (r - 1.0)
    if year_current is not None and year_prior is not None:
        overlap = acs5_overlap_years(int(year_current), int(year_prior))
        if overlap > 0:
            _apply_note(
                notes,
                f"5-year periods ending {year_prior} and {year_current} share {overlap} of 5 "
                "sample years; this MOE ignores that correlation. Census advises comparing "
                "non-overlapping periods where possible",
            )
    return _finish(est, moe, METHOD_PERCENT_CHANGE, notes, scalar, index,
                   _components(scalar, current=c[0], moe_current=mc[0], prior=p[0], moe_prior=mp[0]))


def scale(x: Input, moe_x: Input, k: Input) -> Union[DerivedEstimate, pd.DataFrame]:
    """k * X with MOE abs(k) * MOE_X, for a constant k without sampling error.

    Use for unit conversions (proportion -> percent, dollars -> thousands) or
    price-index adjustment; the Census Bureau treats index ratios as constants.
    """
    (x, mx, kk), scalar, index = _prepare(x, moe_x, k)
    notes: List[List[str]] = [[] for _ in range(len(x))]
    x, mx = _clean_pair(x, mx, "X", notes)
    for i in np.flatnonzero(~np.isfinite(kk)):
        notes[i].append("scale factor k is missing or infinite; result undefined")
    kk = np.where(np.isfinite(kk), kk, np.nan)
    return _finish(kk * x, np.abs(kk) * mx, METHOD_SCALE, notes, scalar, index,
                   _components(scalar, x=x[0], moe_x=mx[0], k=kk[0]))


# ---------------------------------------------------------------------------
# Label hierarchy
# ---------------------------------------------------------------------------

_ESTIMATE_PREFIX_RE = re.compile(r"^\s*(Estimate|Percent|Margin of Error|Percent Margin of Error)!!", re.I)


def _label_path(code: str, metadata: Mapping[str, Mapping[str, Any]]) -> Optional[List[str]]:
    code = str(code)
    parsed = parse_variable_code(code)
    candidates = [code]
    if parsed:
        candidates += [f"{parsed[0]}E", f"{parsed[0]}PE"]
    else:
        candidates += [f"{code}E", f"{code}PE"]
    for cand in candidates:
        entry = metadata.get(cand)
        if entry and entry.get("label"):
            label = _ESTIMATE_PREFIX_RE.sub("", str(entry["label"]))
            return [p.strip().rstrip(":").strip().lower() for p in label.split("!!") if p.strip()]
    return None


def subset_relationship(
    num_code: str, den_code: str, metadata: Mapping[str, Mapping[str, Any]]
) -> bool:
    """True when the numerator's label path lies under the denominator's.

    Both codes must come from the same table (same table ID, and the same
    ``_Cnn`` column for subject tables), and the denominator's label path
    (``Total:!!Female:``) must be a prefix of the numerator's
    (``Total:!!Female:!!Never married``). Used to choose :func:`proportion`
    over :func:`ratio`; callers can always override the choice.

    Raises:
        KeyError: if either code has no label in ``metadata``.
    """
    num_path = _label_path(num_code, metadata)
    den_path = _label_path(den_code, metadata)
    missing = [c for c, p in ((num_code, num_path), (den_code, den_path)) if p is None]
    if missing:
        raise KeyError(f"no label in metadata for {', '.join(missing)}")
    if _table_of(num_code) is None or _table_of(num_code) != _table_of(den_code):
        return False
    assert num_path is not None and den_path is not None
    return len(den_path) <= len(num_path) and num_path[: len(den_path)] == den_path


# ---------------------------------------------------------------------------
# DataFrame helper
# ---------------------------------------------------------------------------

DERIVED_FIELDS = ("estimate", "moe", "se", "cv_percent", "method", "notes")
SPEC_OPS = ("proportion", "ratio", "auto", "difference", "product", "sum", "percent_change", "scale")


def resolve_columns(df: pd.DataFrame, ref: str) -> Tuple[str, str]:
    """Find the (estimate, MOE) columns for a reference.

    ``ref`` may be an estimate code (``B17001_002E``, ``DP03_0062PE``), a
    variable base (``B17001_002``), or the name of a column set produced by
    :func:`derive_columns` (``<name>_estimate`` / ``<name>_moe``).
    """
    ref = str(ref)
    if f"{ref}_estimate" in df.columns and f"{ref}_moe" in df.columns:
        return f"{ref}_estimate", f"{ref}_moe"
    parsed = parse_variable_code(ref)
    if parsed and parsed[1] in ("E", "PE"):
        base, suffix = parsed
        moe_col = f"{base}{'M' if suffix == 'E' else 'PM'}"
        if ref in df.columns and moe_col in df.columns:
            return ref, moe_col
        missing = [c for c in (ref, moe_col) if c not in df.columns]
        raise KeyError(f"missing columns for {ref}: {missing}")
    if f"{ref}E" in df.columns and f"{ref}M" in df.columns:
        return f"{ref}E", f"{ref}M"
    raise KeyError(f"no estimate/MOE columns found for {ref!r}")


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _spec_codes(entry: Mapping[str, Any]) -> List[str]:
    codes: List[str] = []
    for key in ("num", "den"):
        if entry.get(key) is not None:
            codes += _as_list(entry[key])
    return codes


def _choose_op(entry: Mapping[str, Any], metadata: Optional[Mapping[str, Mapping[str, Any]]]) -> str:
    op = str(entry.get("op", "auto")).lower()
    if op not in SPEC_OPS:
        raise ValueError(f"unknown op {op!r} in spec entry {entry.get('name')!r}; expected one of {SPEC_OPS}")
    if op != "auto":
        return op
    if metadata is None:
        raise ValueError(
            f"spec entry {entry.get('name')!r} uses op='auto', which needs metadata to decide "
            "between proportion and ratio; pass metadata or set op explicitly"
        )
    num, den = entry.get("num"), entry.get("den")
    if isinstance(num, (list, tuple)) or isinstance(den, (list, tuple)):
        raise ValueError("op='auto' needs single codes for num and den")
    return "proportion" if subset_relationship(str(num), str(den), metadata) else "ratio"


def derive_entry(
    df: pd.DataFrame,
    entry: Mapping[str, Any],
    metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    zero_estimate_rule: str = ZERO_RULE_ALL,
) -> pd.DataFrame:
    """Evaluate one :func:`derive_columns` spec entry; returns the result frame."""
    name = entry.get("name")
    if not name:
        raise ValueError(f"spec entry needs a 'name': {dict(entry)!r}")
    op = _choose_op(entry, metadata)
    as_percent = bool(entry.get("as_percent", False))

    def pair(ref: str) -> Tuple[pd.Series, pd.Series]:
        e, m = resolve_columns(df, ref)
        return df[e], df[m]

    if op == "sum":
        refs = _as_list(entry["num"])
        cols = [resolve_columns(df, r) for r in refs]
        est = df[[c[0] for c in cols]]
        moe = df[[c[1] for c in cols]]
        return sum_estimates(est, moe, zero_estimate_rule=zero_estimate_rule, names=refs)  # type: ignore[return-value]
    if op == "scale":
        x, mx = pair(_as_list(entry["num"])[0])
        if "k" not in entry:
            raise ValueError(f"spec entry {name!r}: op='scale' needs 'k'")
        return scale(x, mx, entry["k"])  # type: ignore[return-value]

    def operand(key: str) -> Tuple[Any, Any, str]:
        refs = _as_list(entry[key])
        if len(refs) == 1:
            e, m = pair(refs[0])
            return e, m, refs[0]
        cols = [resolve_columns(df, r) for r in refs]
        s = sum_estimates(df[[c[0] for c in cols]], df[[c[1] for c in cols]],
                          zero_estimate_rule=zero_estimate_rule, names=refs)
        return s["estimate"], s["moe"], refs[0]  # type: ignore[index]

    if entry.get("num") is None or entry.get("den") is None:
        raise ValueError(f"spec entry {name!r}: op={op!r} needs 'num' and 'den'")
    x, mx, cx = operand("num")
    y, my, cy = operand("den")
    codes = (cx, cy)
    if op == "proportion":
        result = proportion(x, mx, y, my, as_percent=as_percent)
    elif op == "ratio":
        result = ratio(x, mx, y, my, as_percent=as_percent, codes=codes)
    elif op == "difference":
        result = difference(x, mx, y, my, codes=codes)
    elif op == "product":
        result = product(x, mx, y, my, codes=codes)
    else:  # percent_change
        result = percent_change(x, mx, y, my, entry.get("year_current"), entry.get("year_prior"))
    return result  # type: ignore[return-value]


def derive_columns(
    df: pd.DataFrame,
    spec: Sequence[Mapping[str, Any]],
    metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    zero_estimate_rule: str = ZERO_RULE_ALL,
) -> pd.DataFrame:
    """Add derived columns to a wide frame.

    Each spec entry is a mapping with:

    * ``name``: output prefix; adds ``<name>_estimate``, ``<name>_moe``,
      ``<name>_se``, ``<name>_cv_percent``, ``<name>_method``, ``<name>_notes``.
    * ``op``: ``proportion``, ``ratio``, ``auto`` (default: proportion when
      :func:`subset_relationship` holds, else ratio; needs ``metadata``),
      ``difference``, ``product``, ``sum``, ``percent_change`` or ``scale``.
    * ``num`` / ``den``: estimate codes (``B17001_002E``), bases
      (``B17001_002``) or names of earlier entries. A list sums its members
      first (Census "multiple approximations", e.g. (A+B+C)/(D+E)). ``sum``
      uses ``num`` only; ``scale`` uses ``num`` and a constant ``k``.
    * optional ``as_percent`` (proportion, ratio), ``year_current`` /
      ``year_prior`` (percent_change).

    MOE columns are located by the ``E``/``M`` (``PE``/``PM``) suffix
    convention of :mod:`census.stats`. Entries are evaluated in order, so
    later entries may use earlier ones.
    """
    out = df.copy()
    for entry in spec:
        result = derive_entry(out, entry, metadata=metadata, zero_estimate_rule=zero_estimate_rule)
        name = entry["name"]
        for field in DERIVED_FIELDS:
            values = result[field]
            out[f"{name}_{field}"] = values.to_numpy() if hasattr(values, "to_numpy") else values
    return out
