# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statistical methodology for ACS sampling error.

Implements the Census Bureau's published guidance for working with ACS margins
of error (see "Understanding and Using American Community Survey Data", ch. 7-8):

* Standard errors from 90% MOEs: ``SE = MOE / 1.645``
* Coefficients of variation: ``CV = SE / estimate * 100``
* Two-tailed Z-tests for differences between estimates, including the
  overlapping-period adjustment for comparing two 5-year ACS estimates
* Root-sum-of-squares MOE propagation for sums of estimates
* Replacement of jam/sentinel values (-666666666, etc.) with NaN
"""

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# Z critical values for two-tailed tests at common confidence levels.
Z_CRITICAL: Dict[float, float] = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}

# ACS published MOEs are at the 90% confidence level.
ACS_MOE_Z = Z_CRITICAL[0.90]

CV_HIGH_MAX = 15.0
CV_MODERATE_MAX = 30.0

RELIABILITY_HIGH = "High"
RELIABILITY_MODERATE = "Moderate"
RELIABILITY_LOW = "Low"

# Census API "jam values" — numeric codes standing in for footnoted cells.
# https://www.census.gov/data/developers/data-sets/acs-1year/notes-on-acs-estimate-and-annotation-values.html
CENSUS_SENTINELS: Dict[int, str] = {
    -999999999: "Insufficient sample cases to display estimate or MOE (N)",
    -888888888: "Not applicable (X)",
    -666666666: "Estimate could not be computed: insufficient sample observations (-)",
    -555555555: "MOE not appropriate: estimate is controlled (*****)",
    -333333333: "MOE not appropriate: median falls in open-ended interval (***)",
    -222222222: "MOE could not be computed: insufficient sample observations (**)",
}

CONTROLLED_MOE = -555555555


def moe_to_se(moe: Any, z: Optional[float] = None) -> np.ndarray:
    """Convert MOEs to standard errors with consistent sentinel handling.

    ``-555555555`` (controlled estimate: no sampling error per Census) -> 0;
    every other Census sentinel, other negative values, and NaN -> NaN;
    otherwise ``|moe| / z`` (default z = 1.645, the ACS 90% convention).
    """
    z = ACS_MOE_Z if z is None else float(z)
    shape = np.shape(moe)
    flat = pd.to_numeric(pd.Series(np.ravel(np.asarray(moe, dtype=object))), errors="coerce")
    m = flat.to_numpy(float).reshape(shape)
    sentinels = np.array(sorted(CENSUS_SENTINELS), dtype=float)
    with np.errstate(invalid="ignore"):
        se = np.where(m == CONTROLLED_MOE, 0.0,
                      np.where(np.isin(m, sentinels) | (m < 0), np.nan, m / z))
    return se


# Values in the EA/MA annotation columns that mark top/bottom coding.
ANNOTATION_CODES: Dict[str, str] = {
    "+": "Top-coded: true value lies in the upper open-ended interval",
    "-": "Bottom-coded: true value lies in the lower open-ended interval",
}

# Variable code anatomy: <table>[_C<col> | _<period>]_<line><suffix>
#   B19013_001E, B01001A_003M, DP03_0062PE, S1701_C01_001E, B19013_001EA,
#   CP03_2017_001E, CP03_2022to2017_001SS (comparison-profile significance)
# Optional "__<first>_<last>" marks a concept segment (a reused code split by year).
BASE_PATTERN = r"[A-Z]+\d+[A-Z]*(?:_C\d+|_\d{4}(?:to\d{4})?)?_\d+(?:__\d{4}_\d{4})?"
VARIABLE_CODE_RE = re.compile(
    rf"^(?P<base>{BASE_PATTERN})(?P<suffix>PEA|PMA|PE|PM|EA|MA|SS|E|M)$"
)

MEASUREMENT_TYPES: Dict[str, str] = {
    "E": "Estimate",
    "M": "Margin of Error",
    "PE": "Percentage",
    "PM": "Margin of Error (Percentage)",
    "EA": "Annotation",
    "MA": "Annotation",
    "PEA": "Annotation",
    "PMA": "Annotation",
    "SS": "Significance",
}

# Estimate suffix -> paired MOE suffix
ESTIMATE_MOE_PAIRS: Dict[str, str] = {"E": "M", "PE": "PM"}

# Derived-column suffixes appended to a variable base by add_reliability_columns
DERIVED_SUFFIXES: Tuple[str, ...] = ("_SE", "_CV", "_RELIABILITY")


def parse_variable_code(code: str) -> Optional[Tuple[str, str]]:
    """Split a Census variable code into (base, suffix), or None if not a variable."""
    match = VARIABLE_CODE_RE.match(code)
    if not match:
        return None
    return match.group("base"), match.group("suffix")


def is_annotation_column(code: str) -> bool:
    parsed = parse_variable_code(code)
    return bool(parsed and MEASUREMENT_TYPES[parsed[1]] == "Annotation")


def is_value_column(code: str) -> bool:
    """True for estimate / MOE / percent columns (not annotations or geography)."""
    parsed = parse_variable_code(code)
    return bool(
        parsed and MEASUREMENT_TYPES[parsed[1]] not in ("Annotation", "Significance")
    )


# ---------------------------------------------------------------------------
# Scalar statistics
# ---------------------------------------------------------------------------


def _z_for_confidence(confidence_level: float) -> float:
    if confidence_level > 1:
        confidence_level = confidence_level / 100.0
    for level, z in Z_CRITICAL.items():
        if math.isclose(level, confidence_level, abs_tol=1e-9):
            return z
    return normal_ppf(1 - (1 - confidence_level) / 2)


def _is_sentinel(value: object) -> bool:
    """True when ``value`` is one of the Census API jam values (raw input)."""
    if value is None or _isnan(value):
        return False
    v = float(value)  # type: ignore[arg-type]
    return v.is_integer() and int(v) in CENSUS_SENTINELS


def standard_error(moe: float, moe_confidence: float = 0.90) -> float:
    """Convert a margin of error to a standard error (default: ACS 90% MOE).

    Raw Census sentinels are decoded first, as in :func:`moe_to_se` and the
    cleaned pipeline: ``-555555555`` (controlled estimate) -> 0; any other
    sentinel -> NaN (uncertainty undefined). An ordinary negative MOE keeps the
    long-standing ``|moe|`` compatibility behavior.
    """
    if moe is None or _isnan(moe):
        return math.nan
    if _is_sentinel(moe):
        return 0.0 if int(float(moe)) == CONTROLLED_MOE else math.nan
    return abs(float(moe)) / _z_for_confidence(moe_confidence)


def coefficient_of_variation(estimate: float, moe: float) -> float:
    """CV (%) = SE / estimate * 100. Undefined (NaN) when estimate is 0, missing
    or a Census sentinel."""
    if estimate is None or _isnan(estimate) or _is_sentinel(estimate) or float(estimate) == 0:
        return math.nan
    se = standard_error(moe)
    if _isnan(se):
        return math.nan
    return se / abs(float(estimate)) * 100.0


def reliability_flag(cv: float) -> str:
    """Census reliability category for a CV (%). Empty string when CV is undefined."""
    if cv is None or _isnan(cv):
        return ""
    if cv <= CV_HIGH_MAX:
        return RELIABILITY_HIGH
    if cv <= CV_MODERATE_MAX:
        return RELIABILITY_MODERATE
    return RELIABILITY_LOW


def calculate_cv(estimate: float, moe: float) -> Dict[str, object]:
    """Return SE, CV and reliability flag for a single estimate/MOE pair."""
    se = standard_error(moe)
    cv = coefficient_of_variation(estimate, moe)
    # Interval half-width from the decoded SE: 0 for a controlled estimate,
    # undefined for a missing/suppressed MOE or a sentinel estimate.
    half = math.nan if _isnan(se) else (0.0 if se == 0 else abs(float(moe)))
    if estimate is None or _isnan(estimate) or _is_sentinel(estimate):
        half = math.nan
    return {
        "estimate": estimate,
        "moe": moe,
        "standard_error": se,
        "cv_percent": cv,
        "reliability": reliability_flag(cv),
        "lower_90": float(estimate) - half if not _isnan(half) else math.nan,
        "upper_90": float(estimate) + half if not _isnan(half) else math.nan,
    }


def normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (bisection; accurate to ~1e-10)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def two_tailed_p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def overlap_factor(overlap_years: int, period_years: int = 5) -> float:
    """sqrt(1 - C) where C is the share of overlapping sample years.

    Census guidance for comparing overlapping 5-year periods (e.g. 2015-2019 vs
    2017-2021 share 3 years -> C = 3/5) for the same geography.

    Two estimates from the same period (overlap >= period) are comparisons of
    different geographies or subgroups, which Census treats as independent:
    the factor is 1, not sqrt(0), which would make every difference
    "significant" with zero standard error.
    """
    if overlap_years <= 0 or overlap_years >= period_years:
        return 1.0
    c = min(overlap_years, period_years) / period_years
    return math.sqrt(1.0 - c)


def acs5_overlap_years(year1: int, year2: int) -> int:
    """Number of sample years shared by two ACS 5-year periods (by end year)."""
    return max(0, 5 - abs(int(year1) - int(year2)))


def acs_overlap_years(
    year1: int, year2: int, product1: str = "5-year", product2: Optional[str] = None
) -> int:
    """Sample years shared by two ACS releases of the same product (by end year).

    5-year: ``max(0, 5 - |y1 - y2|)``. 1-year: different releases never share
    sample (0); the same release returns 1 (= the period, i.e. "same period",
    which :func:`overlap_factor` treats as independent subgroups). Mixing 1-year
    and 5-year estimates raises ``ValueError``: Census advises against that
    comparison (https://www.census.gov/programs-surveys/acs/guidance/comparing-acs-data.html).
    """
    from census.api import PRODUCT_1YR, require_same_product  # local: keep stats light

    product = require_same_product(product1, product2 if product2 is not None else product1)
    if product == PRODUCT_1YR:
        return 1 if int(year1) == int(year2) else 0
    return acs5_overlap_years(year1, year2)


def z_test(
    est1: float,
    moe1: float,
    est2: float,
    moe2: float,
    confidence_level: float = 0.90,
    overlap_years: int = 0,
    product1: Optional[str] = None,
    product2: Optional[str] = None,
) -> Dict[str, object]:
    """Two-tailed Z-test for the difference between two ACS estimates.

    Z = |X1 - X2| / sqrt(SE1^2 + SE2^2), with the denominator scaled by
    sqrt(1 - C) when the two 5-year periods overlap by ``overlap_years``.
    ``product1``/``product2`` ('5-year' / '1-year' or dataset keys), when
    given, must match: a 1-year vs 5-year comparison raises ``ValueError``.
    1-year releases never overlap; pass ``overlap_years=0`` (see
    :func:`acs_overlap_years`).
    """
    period = 5
    if product1 is not None or product2 is not None:
        from census.api import PRODUCT_PERIOD_YEARS, require_same_product

        period = PRODUCT_PERIOD_YEARS[require_same_product(product1, product2)]
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    critical = _z_for_confidence(level)
    label = f"{level * 100:g}%"
    notes: List[str] = []

    def _clean_moe(moe: float, which: str) -> float:
        # Census sentinels: -555555555 marks a controlled estimate (no sampling
        # error); every other sentinel or NaN makes the MOE undefined.
        if moe is None or _isnan(moe):
            notes.append(f"{which} MOE missing")
            return math.nan
        if int(float(moe)) == -555555555:
            notes.append(f"{which} estimate is controlled (MOE 0)")
            return 0.0
        if int(float(moe)) in CENSUS_SENTINELS:
            notes.append(f"{which} MOE is a Census sentinel ({int(float(moe))}): undefined")
            return math.nan
        return float(moe)

    m1, m2 = _clean_moe(moe1, "first"), _clean_moe(moe2, "second")
    diff = float(est1) - float(est2)
    if any(_isnan(v) for v in (est1, est2)) or any(
        int(float(v)) in CENSUS_SENTINELS for v in (est1, est2) if not _isnan(v)
    ):
        m1 = math.nan
        notes.append("an estimate is missing or a Census sentinel")
    if _isnan(m1) or _isnan(m2):
        return {
            "difference": diff,
            "se_difference": math.nan,
            "z_statistic": math.nan,
            "p_value": math.nan,
            "confidence_level": level,
            "critical_value": critical,
            "significant": None,
            "significant_at": {f"{int(lv * 100)}%": None for lv in Z_CRITICAL},
            "overlap_years": overlap_years,
            "notes": notes,
            "verdict": "Not tested: " + "; ".join(notes) + ".",
        }
    se1, se2 = standard_error(m1), standard_error(m2)
    se_diff = math.sqrt(se1**2 + se2**2) * overlap_factor(overlap_years, period)
    if overlap_years >= period:
        notes.append("same period: treated as independent samples (no overlap adjustment)")
    if se_diff == 0:
        z = math.inf if diff != 0 else 0.0
    else:
        z = abs(diff) / se_diff
    p_value = two_tailed_p_value(z) if math.isfinite(z) else 0.0
    significant = z >= critical

    return {
        "difference": diff,
        "se_difference": se_diff,
        "z_statistic": z,
        "p_value": p_value,
        "confidence_level": level,
        "critical_value": critical,
        "significant": bool(significant),
        "significant_at": {
            f"{int(lv * 100)}%": bool(z >= zc) for lv, zc in Z_CRITICAL.items()
        },
        "overlap_years": overlap_years,
        "notes": notes,
        "verdict": (
            f"Statistically significant difference at the {label} confidence level."
            if significant
            else f"No statistically significant difference at the {label} confidence level."
        ),
    }


def aggregate_estimates(
    estimates: Sequence[float], moes: Sequence[float], zero_estimate_rule: str = "all"
) -> Tuple[float, float]:
    """Sum estimates and propagate MOEs in quadrature: sqrt(sum(MOE_i^2)).

    zero_estimate_rule="all" (default) includes every component's MOE, as in the
    ACS General Handbook 2018/2020 ch. 8 formula 1 and the 2025 worked examples.
    "largest" reproduces older Census training guidance (2016) that included only
    the largest MOE among zero estimates. See census.derived.sum_estimates for
    the method-tracking version.
    """
    if len(estimates) != len(moes):
        raise ValueError("estimates and moes must be the same length")
    if zero_estimate_rule not in ("all", "largest"):
        raise ValueError("zero_estimate_rule must be 'all' or 'largest'")
    total = float(sum(estimates))
    if zero_estimate_rule == "all":
        return total, math.sqrt(sum(float(m) ** 2 for m in moes))
    zero_moes = [abs(m) for e, m in zip(estimates, moes) if e == 0]
    nonzero_moes = [abs(m) for e, m in zip(estimates, moes) if e != 0]
    if zero_moes:
        nonzero_moes.append(max(zero_moes))
    combined_moe = math.sqrt(sum(m**2 for m in nonzero_moes))
    return total, combined_moe


def _isnan(value: object) -> bool:
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# DataFrame operations
# ---------------------------------------------------------------------------


def controlled_column(moe_col: str) -> Optional[str]:
    """``<base>_CONTROLLED`` (``<base>P_CONTROLLED`` for a PM column) for an MOE column."""
    parsed = parse_variable_code(str(moe_col))
    if not parsed or parsed[1] not in ("M", "PM"):
        return None
    base, suffix = parsed
    return f"{base}{'P' if suffix == 'PM' else ''}_CONTROLLED"


def clean_census_sentinels(
    df: pd.DataFrame,
    columns: Optional[Iterable[str]] = None,
    add_notes: bool = False,
    preserve_controlled: bool = True,
) -> pd.DataFrame:
    """Convert value columns to numeric and replace Census sentinel codes with NaN.

    Controlled estimates are the documented exception (Round 9, G1). The
    Census Bureau publishes ``-555555555`` in an MOE column when the estimate
    is controlled to an independent population estimate and has no sampling
    error. By default that MOE becomes ``0`` and a boolean
    ``<base>_CONTROLLED`` column (``<base>P_CONTROLLED`` for percent MOEs) is
    added, True on controlled rows, whenever the column contains at least one
    controlled cell. Validation check F1 accepts exactly this representation.
    ``preserve_controlled=False`` restores the Round 8 behavior (NaN, no flag).

    Args:
        df: Frame with raw Census variable-code columns (strings or numbers).
        columns: Columns to clean. Defaults to all estimate/MOE/percent columns.
        add_notes: If True, add a ``<column>_NOTE`` column describing each
            replaced sentinel (and top/bottom coding read from EA annotations).
        preserve_controlled: Keep controlled MOEs as 0 with a flag column.
    """
    out = df.copy()
    targets = list(columns) if columns is not None else [
        c for c in out.columns if is_value_column(str(c))
    ]
    sentinel_values = list(CENSUS_SENTINELS.keys())
    flags: Dict[str, pd.Series] = {}

    for col in targets:
        numeric = pd.to_numeric(out[col], errors="coerce")
        mask = numeric.isin(sentinel_values)
        if add_notes:
            notes = numeric.where(mask).map(CENSUS_SENTINELS).fillna("")
            parsed = parse_variable_code(str(col))
            if parsed:
                ann_col = f"{parsed[0]}{parsed[1]}A"
                if ann_col in out.columns:
                    coded = out[ann_col].astype(str).map(ANNOTATION_CODES).fillna("")
                    notes = notes.where(notes != "", coded)
            if (notes != "").any():
                out[f"{col}_NOTE"] = notes
        flag_col = controlled_column(str(col)) if preserve_controlled else None
        controlled = numeric == CONTROLLED_MOE
        if flag_col and controlled.any():
            out[col] = numeric.mask(mask & ~controlled).where(~controlled, 0.0)
            flags[flag_col] = controlled.astype(bool)
        else:
            out[col] = numeric.mask(mask)

    if flags:
        keep = out.drop(columns=[c for c in flags if c in out.columns])
        out = pd.concat([keep, pd.DataFrame(flags, index=out.index)], axis=1)
    return out


def pair_estimates_and_moes(columns: Iterable[str]) -> List[Tuple[str, str, str]]:
    """Find (base_key, estimate_col, moe_col) triples present in ``columns``.

    base_key is ``<base>`` for E/M pairs and ``<base>P`` for PE/PM pairs.
    """
    cols = set(columns)
    pairs: List[Tuple[str, str, str]] = []
    for col in sorted(cols):
        parsed = parse_variable_code(col)
        if not parsed:
            continue
        base, suffix = parsed
        moe_suffix = ESTIMATE_MOE_PAIRS.get(suffix)
        if moe_suffix and f"{base}{moe_suffix}" in cols:
            key = base if suffix == "E" else f"{base}P"
            pairs.append((key, col, f"{base}{moe_suffix}"))
    return pairs


def add_reliability_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Append ``<base>_SE``, ``<base>_CV`` and ``<base>_RELIABILITY`` per E/M pair."""
    out = df.copy()
    new_cols: Dict[str, pd.Series] = {}
    sentinels = list(CENSUS_SENTINELS.keys())
    for key, est_col, moe_col in pair_estimates_and_moes(out.columns):
        est = pd.to_numeric(out[est_col], errors="coerce")
        moe = pd.to_numeric(out[moe_col], errors="coerce")
        est = est.mask(est.isin(sentinels))
        moe = moe.mask(moe.isin(sentinels))
        se = moe.abs() / ACS_MOE_Z
        cv = (se / est.abs().where(est != 0)) * 100.0
        new_cols[f"{key}_SE"] = se
        new_cols[f"{key}_CV"] = cv
        new_cols[f"{key}_RELIABILITY"] = cv.map(reliability_flag)
    if new_cols:
        out = pd.concat([out, pd.DataFrame(new_cols, index=out.index)], axis=1)
    return out


def aggregate_columns(
    df: pd.DataFrame, bases: Sequence[str], name: str
) -> pd.DataFrame:
    """Sum several estimate columns and propagate their MOEs in quadrature.

    ``bases`` are variable bases such as ``["B01001_007", "B01001_008"]``; the
    result gains ``<name>E`` and ``<name>M`` columns.
    """
    out = df.copy()
    est_cols = [f"{b}E" for b in bases]
    moe_cols = [f"{b}M" for b in bases]
    missing = [c for c in est_cols + moe_cols if c not in out.columns]
    if missing:
        raise KeyError(f"Missing columns for aggregation: {missing}")

    def _row(row: pd.Series) -> pd.Series:
        ests = [float(row[c]) for c in est_cols]
        moes = [float(row[c]) for c in moe_cols]
        if any(math.isnan(v) for v in ests + moes):
            return pd.Series({f"{name}E": math.nan, f"{name}M": math.nan})
        total, moe = aggregate_estimates(ests, moes)
        return pd.Series({f"{name}E": total, f"{name}M": moe})

    numeric = out[est_cols + moe_cols].apply(pd.to_numeric, errors="coerce")
    result = numeric.apply(_row, axis=1)
    out[f"{name}E"] = result[f"{name}E"]
    out[f"{name}M"] = result[f"{name}M"]
    return out
