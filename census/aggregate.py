# SPDX-License-Identifier: AGPL-3.0-or-later
"""Custom geography aggregation (combine tracts, counties, ... into groups).

Counts are summed within each group. The MOE of each group total is either

* **replicate** (exact): the 80 Variance Replicate Estimates of the members
  are summed within the group and the successive-difference replicate
  variance is applied (``census.variance.replicate_moe``). This is the method
  Census uses for published MOEs and captures covariance between members
  (ACS General Handbook 2020, ch. 8, "Calculating Measures of Error Using
  Variance Replicate Tables"); or
* **approximate**: ``census.derived.sum_estimates`` (root sum of squared MOEs,
  Handbook 2020 ch. 8 formula 1), which ignores covariance.

``method="auto"`` uses replicates for every variable the replicate frame
covers for all members, and the approximation otherwise; each output records
the method actually used.

Only additive measures can be summed. Medians, means, averages, per-capita
values, percentages, rates, ratios, Gini indexes and quantile limits are
refused: aggregate their numerator and denominator counts and derive the
statistic afterwards with :func:`aggregate_then_derive` (averaging tract rates
is wrong), and build medians for a group from its distribution table with
``census.medians``. Aggregates (e.g. aggregate household income) are sums and
are allowed.
"""

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .derived import (
    CONTROLLED_MOE,
    DERIVED_FIELDS,
    MANY_COMPONENTS,
    NOTE_COVARIANCE,
    ZERO_RULE_ALL,
    ZERO_RULE_LARGEST,
    ZERO_RULES,
    _as_list,
    _choose_op,
    _clean_estimate,
    _clean_moe,
    derive_entry,
)
from .panel import infer_unit
from .stats import CENSUS_SENTINELS, pair_estimates_and_moes, parse_variable_code
from .variance import (
    METHOD_REPLICATE,
    METHOD_SUM,
    REPLICATE_COLUMNS,
    Z90,
    replicate_moe,
)

Metadata = Mapping[str, Mapping[str, Any]]

METHODS = ("auto", "replicate", "approximate")

_SENTINELS = list(CENSUS_SENTINELS)


class NonAdditiveError(ValueError):
    """Raised when a non-additive measure (median, percent, ratio, ...) would be summed."""


# ---------------------------------------------------------------------------
# Additivity
# ---------------------------------------------------------------------------

# Checked against the variable label and the table concept.
_LABEL_AND_CONCEPT_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("median", re.compile(r"\bmedian\b", re.I)),
    ("mean", re.compile(r"\b(mean|average)\b", re.I)),
    ("per capita value", re.compile(r"\bper capita\b", re.I)),
    ("Gini index", re.compile(r"\bgini\b", re.I)),
    ("quantile", re.compile(r"\b(quartile|quintile|percentile)\b", re.I)),
    ("interval limit", re.compile(r"\b(upper|lower) limits?\b", re.I)),
    ("share", re.compile(r"\bshares? of\b", re.I)),
]
# Checked against the label only: count tables have concepts such as "RATIO OF
# INCOME TO POVERTY LEVEL" or "GROSS RENT AS A PERCENTAGE OF HOUSEHOLD INCOME"
# whose cells are counts.
_LABEL_ONLY_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("rate", re.compile(r"\brate\b", re.I)),
    ("ratio", re.compile(r"\bratio\b", re.I)),
]
_PREFIX_RE = re.compile(r"^\s*Estimate!!", re.I)


def non_additive_reason(code: str, metadata: Optional[Metadata] = None) -> Optional[str]:
    """Why ``code`` cannot be summed across geographies, or None if it can.

    Uses the code suffix (``PE``/``PM`` are percentages), the codebook unit
    from :func:`census.panel.infer_unit`, and keywords in the variable label
    and table concept. Without metadata only the suffix can be checked.
    """
    code = str(code)
    parsed = parse_variable_code(code)
    if parsed and parsed[1] in ("PE", "PM"):
        return "percentage (PE/PM column)"
    if not metadata:
        return None
    base = parsed[0] if parsed else code
    entry = metadata.get(f"{base}E") or metadata.get(code) or {}
    raw_label = str(entry.get("label", ""))
    concept = str(entry.get("concept", ""))
    if raw_label.lower().startswith("percent"):
        return "percentage"
    label = _PREFIX_RE.sub("", raw_label)
    segments = [s.strip().lower() for s in label.split("!!")]
    if any(s.startswith("percent") for s in segments):
        return "percentage"
    for reason, pattern in _LABEL_AND_CONCEPT_RULES:
        if pattern.search(label) or pattern.search(concept):
            return reason
    for reason, pattern in _LABEL_ONLY_RULES:
        if pattern.search(label):
            return reason
    unit = infer_unit(f"{base}E", metadata) if entry else ""
    if unit == "Years":
        return "median age"
    if unit == "Ratio":
        return "ratio"
    return None


def _refusal(code: str, reason: str) -> NonAdditiveError:
    if reason.startswith("median"):
        advice = (
            "Medians cannot be summed or averaged across geographies. Build the group's "
            "median from its distribution table with census.medians "
            "(median_from_distribution)."
        )
    else:
        advice = (
            "Aggregate the underlying numerator and denominator counts and derive the "
            "statistic afterwards with census.aggregate.aggregate_then_derive; averaging "
            "or summing rates across areas is wrong."
        )
    return NonAdditiveError(f"{code} is a {reason} and is not additive. {advice}")


def check_additive(
    codes: Sequence[str],
    metadata: Optional[Metadata] = None,
    assume_additive: Sequence[str] = (),
) -> None:
    """Raise :class:`NonAdditiveError` for the first non-additive code."""
    trusted = {_base(c) for c in assume_additive}
    for code in codes:
        if _base(code) in trusted:
            continue
        reason = non_additive_reason(code, metadata)
        if reason:
            raise _refusal(code, reason)


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

# GEO_ID prefixes: <sumlev(3)><component(2)>US before 2020 ("05000US48201"),
# <sumlev(3)><component(4)>US from 2020 ("0500000US48201").
_GEO_ID_RE = re.compile(r"^[0-9]{3}[0-9A-Z]{2}(?:[0-9A-Z]{2})?US(?P<geoid>.*)$")


def normalize_geoid(value: Any) -> str:
    """Strip a ``GEO_ID`` summary-level prefix (``1400000US``, ``14000US``) and whitespace."""
    text = str(value).strip()
    match = _GEO_ID_RE.match(text)
    if match:
        return match.group("geoid") or "US"
    return text


def _base(code: str) -> str:
    code = str(code)
    parsed = parse_variable_code(code)
    return parsed[0] if parsed else code


def _geoid_column(df: pd.DataFrame, geoid_col: Optional[str]) -> str:
    if geoid_col:
        if geoid_col not in df.columns:
            raise KeyError(f"geoid column {geoid_col!r} not in frame")
        return geoid_col
    for col in ("GEOID", "GEO_ID"):
        if col in df.columns:
            return col
    raise KeyError("frame has no GEOID or GEO_ID column; pass geoid_col")


def _mapping_series(mapping: Any) -> pd.Series:
    """GEOID -> group id as a Series indexed by normalised GEOID."""
    if isinstance(mapping, pd.Series):
        series = mapping.copy()
    elif isinstance(mapping, Mapping):
        series = pd.Series(dict(mapping))
    elif isinstance(mapping, pd.DataFrame):
        if mapping.shape[1] < 2:
            raise ValueError("mapping frame needs a GEOID column and a group column")
        geo = "GEOID" if "GEOID" in mapping.columns else mapping.columns[0]
        grp = "group_id" if "group_id" in mapping.columns else [c for c in mapping.columns if c != geo][0]
        series = pd.Series(mapping[grp].to_numpy(), index=mapping[geo].to_numpy())
    else:
        raise TypeError("mapping must be a dict, Series, or two-column DataFrame")
    series.index = [normalize_geoid(g) for g in series.index]
    if series.index.duplicated().any():
        dups = sorted(set(series.index[series.index.duplicated()]))[:10]
        raise ValueError(f"mapping assigns some GEOIDs more than once: {dups}")
    if series.isna().any():
        raise ValueError("mapping has GEOIDs without a group id")
    return series


def _resolve_bases(df: pd.DataFrame, estimate_cols: Optional[Sequence[str]]) -> List[str]:
    if estimate_cols is None:
        bases = [key for key, est, _ in pair_estimates_and_moes(df.columns) if est.endswith("E") and not est.endswith("PE")]
        if not bases:
            raise ValueError("no estimate/MOE column pairs (<code>E/<code>M) found in frame")
        return bases
    bases: List[str] = []
    for col in estimate_cols:
        parsed = parse_variable_code(str(col))
        if parsed and parsed[1] in ("PE", "PM"):
            raise _refusal(str(col), "percentage (PE/PM column)")
        base = parsed[0] if parsed else str(col)
        if parsed and parsed[1] not in ("E", "M"):
            raise ValueError(f"{col} is not an estimate column")
        for needed in (f"{base}E", f"{base}M"):
            if needed not in df.columns:
                raise KeyError(f"missing column {needed} for {col}")
        if base not in bases:
            bases.append(base)
    return bases


def _replicate_variable(value: Any) -> str:
    text = str(value).strip()
    parsed = parse_variable_code(text)
    if parsed and parsed[1] == "E":
        return parsed[0]
    return text


class _Aggregated:
    """Group totals plus, where available, group replicate sums."""

    def __init__(self) -> None:
        self.frame: pd.DataFrame = pd.DataFrame()
        self.groups: List[Any] = []
        self.rep_est: Dict[str, np.ndarray] = {}
        self.rep_sums: Dict[str, np.ndarray] = {}


def _replicate_coverage(
    rep: Optional[pd.DataFrame], bases: Sequence[str], members: Sequence[str]
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str]]:
    """Per variable: member-indexed frame of estimate + replicates, or why not."""
    covered: Dict[str, pd.DataFrame] = {}
    reasons: Dict[str, str] = {}
    if rep is None:
        return covered, {b: "no replicate frame supplied" for b in bases}
    needed = {"GEOID", "variable", "estimate"} | set(REPLICATE_COLUMNS)
    missing_cols = needed - set(rep.columns)
    if missing_cols:
        raise ValueError(
            f"replicate frame is missing columns {sorted(missing_cols)[:5]}... "
            "(contract: GEOID, NAME, variable, estimate, moe, rep_1..rep_80)"
        )
    frame = rep.copy()
    frame["GEOID"] = [normalize_geoid(g) for g in frame["GEOID"]]
    frame["variable"] = [_replicate_variable(v) for v in frame["variable"]]
    frame = frame[frame["GEOID"].isin(set(members)) & frame["variable"].isin(set(bases))]
    if frame.duplicated(["GEOID", "variable"]).any():
        raise ValueError("replicate frame has duplicate (GEOID, variable) rows")
    member_index = pd.Index(list(members))
    for base in bases:
        sub = frame[frame["variable"] == base].set_index("GEOID")
        values = sub.reindex(member_index)[["estimate"] + REPLICATE_COLUMNS].apply(pd.to_numeric, errors="coerce")
        absent = values.index[values.isna().all(axis=1)]
        if len(absent):
            reasons[base] = f"replicate frame lacks {len(absent)} member(s), e.g. {list(absent[:3])}"
            continue
        incomplete = values.index[values.isna().any(axis=1) | values.isin(_SENTINELS).any(axis=1)]
        if len(incomplete):
            reasons[base] = f"replicate values missing for member(s) {list(incomplete[:3])}"
            continue
        covered[base] = values
    return covered, reasons


def _aggregate(
    df: pd.DataFrame,
    mapping: Any,
    estimate_cols: Optional[Sequence[str]],
    method: str,
    replicate_frame: Optional[pd.DataFrame],
    metadata: Optional[Metadata],
    geoid_col: Optional[str],
    zero_estimate_rule: str,
    assume_additive: Sequence[str],
) -> _Aggregated:
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    gcol = _geoid_column(df, geoid_col)
    groups_by_geoid = _mapping_series(mapping)
    bases = _resolve_bases(df, estimate_cols)
    check_additive([f"{b}E" for b in bases], metadata, assume_additive)

    frame = df.copy()
    frame["_geoid"] = [normalize_geoid(g) for g in frame[gcol]]
    frame = frame[frame["_geoid"].isin(set(groups_by_geoid.index))]
    if frame["_geoid"].duplicated().any():
        dups = sorted(set(frame.loc[frame["_geoid"].duplicated(), "_geoid"]))[:10]
        raise ValueError(
            f"frame has more than one row for GEOIDs {dups}; filter to a single release "
            "(e.g. one Year) before aggregating"
        )
    present = set(frame["_geoid"])
    missing = [g for g in groups_by_geoid.index if g not in present]
    if missing:
        raise ValueError(
            f"{len(missing)} mapped GEOID(s) are not in the frame, e.g. {missing[:10]}; "
            "a group total would silently omit them"
        )
    frame["_group"] = frame["_geoid"].map(groups_by_geoid)
    members_all = list(frame["_geoid"])

    covered, reasons = _replicate_coverage(replicate_frame, bases, members_all)
    if method == "replicate" and len(covered) < len(bases):
        detail = "; ".join(f"{b}: {r}" for b, r in reasons.items())
        raise ValueError(f"method='replicate' needs replicate data for every member and variable ({detail})")
    use_replicate = {b: method in ("replicate", "auto") and b in covered for b in bases}

    group_order = list(pd.unique(frame["_group"]))
    agg = _Aggregated()
    agg.groups = group_order
    rows: Dict[str, List[Any]] = {"group_id": group_order, "member_count": [], "members": []}
    member_lists = {g: list(v) for g, v in frame.groupby("_group", sort=False)["_geoid"]}
    for g in group_order:
        rows["member_count"].append(len(member_lists[g]))
        rows["members"].append(";".join(member_lists[g]))
    out = pd.DataFrame(rows)
    columns: Dict[str, Any] = {}

    no_metadata_note = (
        "additivity checked from column suffixes only (no metadata supplied)" if not metadata else ""
    )
    for base in bases:
        est_col, moe_col = f"{base}E", f"{base}M"
        if use_replicate[base]:
            values = covered[base]
            published = pd.to_numeric(frame.set_index("_geoid")[est_col], errors="coerce").reindex(values.index)
            check = published.notna() & ~published.isin(_SENTINELS)
            diff = (published[check] - values.loc[check, "estimate"]).abs()
            bad = diff[diff > 0.5 + 1e-9 * values.loc[check, "estimate"].abs()]
            if len(bad):
                raise ValueError(
                    f"{est_col}: replicate frame estimates disagree with the frame for "
                    f"{list(bad.index[:3])}; are they from the same release?"
                )
            labelled = values.assign(_group=frame.set_index("_geoid")["_group"].reindex(values.index))
            sums = labelled.groupby("_group", sort=False)[["estimate"] + REPLICATE_COLUMNS].sum()
            sums = sums.reindex(group_order)
            est = sums["estimate"].to_numpy(float)
            reps = sums[REPLICATE_COLUMNS].to_numpy(float)
            moe = replicate_moe(est, reps)
            agg.rep_est[base] = est
            agg.rep_sums[base] = reps
            se = moe / Z90
            with np.errstate(divide="ignore", invalid="ignore"):
                cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
            columns[est_col] = est
            columns[moe_col] = moe
            columns[f"{base}_SE"] = se
            columns[f"{base}_CV"] = cv
            columns[f"{base}_METHOD"] = [METHOD_REPLICATE] * len(group_order)
            columns[f"{base}_NOTES"] = [no_metadata_note] * len(group_order)
            continue
        est, moe, notes_lists = _approximate_group_sums(
            frame, est_col, moe_col, group_order, zero_estimate_rule
        )
        notes = []
        for extra in notes_lists:
            if replicate_frame is not None and method == "auto":
                extra.append(f"replicate variance not used: {reasons.get(base, 'unavailable')}")
            if no_metadata_note:
                extra.append(no_metadata_note)
            notes.append("; ".join(extra))
        methods = [METHOD_SUM] * len(group_order)
        moe[np.isnan(est)] = np.nan
        se = moe / Z90
        with np.errstate(divide="ignore", invalid="ignore"):
            cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
        columns[est_col] = est
        columns[moe_col] = moe
        columns[f"{base}_SE"] = se
        columns[f"{base}_CV"] = cv
        columns[f"{base}_METHOD"] = methods
        columns[f"{base}_NOTES"] = notes
    if columns:
        out = pd.concat([out, pd.DataFrame(columns, index=out.index)], axis=1)
    agg.frame = out
    return agg


def _approximate_group_sums(
    frame: pd.DataFrame,
    est_col: str,
    moe_col: str,
    group_order: Sequence[Any],
    zero_estimate_rule: str,
) -> Tuple[np.ndarray, np.ndarray, List[List[str]]]:
    """Vectorised ``census.derived.sum_estimates`` for every group at once.

    Same rules and notes as ``sum_estimates`` (members named by GEOID).
    """
    if zero_estimate_rule not in ZERO_RULES:
        raise ValueError(f"zero_estimate_rule must be one of {ZERO_RULES}, got {zero_estimate_rule!r}")
    n = len(frame)
    member_notes: List[List[str]] = [[] for _ in range(n)]
    geoids = list(frame["_geoid"])
    raw_moe = pd.to_numeric(frame[moe_col], errors="coerce").to_numpy(float)
    est = _clean_estimate(pd.to_numeric(frame[est_col], errors="coerce").to_numpy(float), geoids, member_notes)
    moe = _clean_moe(raw_moe, geoids, member_notes)
    groups = frame["_group"].to_numpy()
    sq = moe**2
    zero = est == 0
    work = pd.DataFrame({
        "g": groups,
        "est": est,
        "est_nan": np.isnan(est),
        "sq_nonzero": np.where(zero, 0.0, sq),
        "sq_zero": np.where(zero, sq, 0.0),
        "sq_nan": np.isnan(sq),
        "zero": zero,
        "controlled": raw_moe == CONTROLLED_MOE,
        "one": 1,
    })
    by = work.groupby("g", sort=False)
    totals = by.agg(
        est=("est", "sum"), est_nan=("est_nan", "any"), sq_nonzero=("sq_nonzero", "sum"),
        sq_zero_sum=("sq_zero", "sum"), sq_zero_max=("sq_zero", "max"), sq_nan=("sq_nan", "any"),
        n_zero=("zero", "sum"), controlled=("controlled", "any"), count=("one", "sum"),
    ).reindex(list(group_order))
    group_est = np.where(totals["est_nan"], np.nan, totals["est"].to_numpy(float))
    if zero_estimate_rule == ZERO_RULE_LARGEST:
        var = totals["sq_nonzero"].to_numpy(float) + totals["sq_zero_max"].to_numpy(float)
    else:
        var = totals["sq_nonzero"].to_numpy(float) + totals["sq_zero_sum"].to_numpy(float)
    group_moe = np.where(totals["sq_nan"], np.nan, np.sqrt(var))

    position = {g: i for i, g in enumerate(group_order)}
    notes: List[List[str]] = [[] for _ in group_order]
    for row, g in enumerate(groups):
        notes[position[g]].extend(member_notes[row])
    for i, (count, n_zero, controlled) in enumerate(
        zip(totals["count"], totals["n_zero"], totals["controlled"])
    ):
        if zero_estimate_rule == ZERO_RULE_LARGEST and n_zero > 1:
            notes[i].append(
                f"zero_estimate_rule='largest': only the largest MOE among {int(n_zero)} "
                "zero estimates was included (pre-2018 Census guidance)"
            )
        if controlled:
            notes[i].append(
                "sum includes a controlled estimate; Census warns the approximate MOE "
                "can differ substantially from the true MOE"
            )
        if count > MANY_COMPONENTS:
            notes[i].append(
                f"sum of {int(count)} components: the approximation grows less accurate as "
                f"components are added; {NOTE_COVARIANCE}"
            )
    return group_est, group_moe, [list(dict.fromkeys(n)) for n in notes]


def _to_long(wide: pd.DataFrame, bases: Sequence[str]) -> pd.DataFrame:
    parts = []
    for base in bases:
        part = wide[["group_id", "member_count", "members"]].copy()
        part["variable"] = base
        part["estimate"] = wide[f"{base}E"].to_numpy()
        part["moe"] = wide[f"{base}M"].to_numpy()
        part["se"] = wide[f"{base}_SE"].to_numpy()
        part["cv_percent"] = wide[f"{base}_CV"].to_numpy()
        part["method"] = wide[f"{base}_METHOD"].to_numpy()
        part["notes"] = wide[f"{base}_NOTES"].to_numpy()
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def aggregate_geographies(
    df: pd.DataFrame,
    mapping: Any,
    estimate_cols: Optional[Sequence[str]] = None,
    method: str = "auto",
    replicate_frame: Optional[pd.DataFrame] = None,
    metadata: Optional[Metadata] = None,
    geoid_col: Optional[str] = None,
    zero_estimate_rule: str = ZERO_RULE_ALL,
    assume_additive: Sequence[str] = (),
    output: str = "wide",
) -> pd.DataFrame:
    """Sum estimates within groups of geographies and compute their MOEs.

    Args:
        df: Wide frame, one row per geography, with a ``GEOID`` (or
            ``GEO_ID``) column and ``<code>E``/``<code>M`` columns.
        mapping: GEOID -> group id, as a dict, Series, or two-column frame
            (``GEOID``, ``group_id``; otherwise the first two columns). Every
            mapped GEOID must be in ``df``; unmapped rows are ignored.
        estimate_cols: Estimate codes or bases to aggregate. Default: every
            E/M pair in ``df``.
        method: ``"auto"`` (replicate where the frame covers all members,
            else approximate), ``"replicate"`` (exact; error if coverage is
            incomplete) or ``"approximate"``.
        replicate_frame: Long replicate frame (columns ``GEOID, NAME,
            variable, estimate, moe, rep_1..rep_80``, see
            ``census.replicate``) for the same release as ``df``.
        metadata: Variable metadata (code -> {label, concept}); enables the
            non-additive check on labels. Without it only ``PE``/``PM``
            columns can be detected and a note says so.
        geoid_col: GEOID column name if not ``GEOID``/``GEO_ID``.
        zero_estimate_rule: passed to ``census.derived.sum_estimates``.
        assume_additive: codes the caller vouches are counts, overriding a
            false-positive refusal.
        output: ``"wide"`` (one row per group; per variable ``<base>E``,
            ``<base>M``, ``<base>_SE``, ``<base>_CV``, ``<base>_METHOD``,
            ``<base>_NOTES``) or ``"long"`` (one row per group and variable
            with ``estimate, moe, se, cv_percent, method, notes``).

    Returns:
        Frame with ``group_id``, ``member_count`` and ``members``
        (semicolon-joined GEOIDs) plus the per-variable fields.

    Raises:
        NonAdditiveError: a requested column is a median, mean, percent,
            rate, ratio, or other non-additive measure.
    """
    if output not in ("wide", "long"):
        raise ValueError("output must be 'wide' or 'long'")
    agg = _aggregate(df, mapping, estimate_cols, method, replicate_frame, metadata,
                     geoid_col, zero_estimate_rule, assume_additive)
    if output == "long":
        return _to_long(agg.frame, _resolve_bases(df, estimate_cols))
    return agg.frame


# ---------------------------------------------------------------------------
# Aggregate, then derive
# ---------------------------------------------------------------------------

_REPLICATE_OPS = ("proportion", "ratio", "difference", "product", "sum", "scale")


def _replicate_entry(
    entry: Mapping[str, Any],
    op: str,
    operand: Any,
) -> Optional[Tuple[np.ndarray, np.ndarray, str, List[str]]]:
    """Exact replicate evaluation of one spec entry, or None if not possible."""
    if op not in _REPLICATE_OPS:
        return None
    try:
        num = operand(entry["num"])
        den = operand(entry["den"]) if op not in ("sum", "scale") else None
    except KeyError:
        return None
    if num is None or (op not in ("sum", "scale") and den is None):
        return None
    (x, xr) = num
    method = METHOD_REPLICATE
    notes: List[str] = [
        f"{op} evaluated on summed replicates (exact; includes covariance between inputs)"
    ]
    with np.errstate(divide="ignore", invalid="ignore"):
        if op == "sum":
            est, reps = x, xr
        elif op == "scale":
            k = float(entry["k"])
            est, reps = k * x, k * xr
        else:
            (y, yr) = den
            if op == "difference":
                est, reps = x - y, xr - yr
            elif op == "product":
                est, reps = x * y, xr * yr
            else:
                est = np.where(y != 0, x / y, np.nan)
                reps = np.where(y[:, None] != 0, xr / yr, np.nan)
                if entry.get("as_percent"):
                    est, reps = est * 100.0, reps * 100.0
    return est, reps, method, notes


def aggregate_then_derive(
    df: pd.DataFrame,
    mapping: Any,
    spec: Sequence[Mapping[str, Any]],
    estimate_cols: Optional[Sequence[str]] = None,
    method: str = "auto",
    replicate_frame: Optional[pd.DataFrame] = None,
    metadata: Optional[Metadata] = None,
    geoid_col: Optional[str] = None,
    zero_estimate_rule: str = ZERO_RULE_ALL,
    assume_additive: Sequence[str] = (),
) -> pd.DataFrame:
    """Aggregate counts to groups first, then derive rates on the group totals.

    This is the correct order: a group's poverty rate is (sum of people in
    poverty) / (sum of people for whom poverty status is determined), not the
    average of member rates. Medians and other non-additive inputs are refused.

    ``spec`` uses the format of :func:`census.derived.derive_columns`
    (``name``, ``op``, ``num``, ``den``, optional ``as_percent``, ``k``).
    Every code referenced by ``spec`` is aggregated (plus ``estimate_cols``).

    When every input of an entry was aggregated with replicates, the entry
    is evaluated exactly on the replicate sums (``method="replicate"``),
    which captures covariance between numerator and denominator; otherwise
    the Census approximation formulas are applied to the group totals and
    MOEs (``census.derived``). Supported exact ops: proportion, ratio,
    difference, product, sum, scale.

    Returns:
        The wide output of :func:`aggregate_geographies` plus
        ``<name>_estimate``, ``<name>_moe``, ``<name>_se``,
        ``<name>_cv_percent``, ``<name>_method``, ``<name>_notes`` per entry.
    """
    names = {str(e.get("name")) for e in spec}
    referenced: List[str] = []
    for entry in spec:
        for key in ("num", "den"):
            if entry.get(key) is None:
                continue
            for ref in _as_list(entry[key]):
                if ref in names:
                    continue
                base = _base(ref)
                if base not in referenced:
                    referenced.append(base)
    cols = list(dict.fromkeys([_base(c) for c in (estimate_cols or [])] + referenced))
    if not cols:
        raise ValueError("spec references no estimate codes")

    agg = _aggregate(df, mapping, cols, method, replicate_frame, metadata,
                     geoid_col, zero_estimate_rule, assume_additive)
    out = agg.frame.copy()
    exact: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}

    def operand(refs: Any) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        total_est: Optional[np.ndarray] = None
        total_reps: Optional[np.ndarray] = None
        for ref in _as_list(refs):
            if ref in exact:
                e, r = exact[ref]
            else:
                base = _base(ref)
                if base not in agg.rep_sums:
                    return None
                e, r = agg.rep_est[base], agg.rep_sums[base]
            total_est = e if total_est is None else total_est + e
            total_reps = r if total_reps is None else total_reps + r
        if total_est is None or total_reps is None:
            return None
        return total_est, total_reps

    for entry in spec:
        name = str(entry["name"])
        op = _choose_op(entry, metadata)
        replicated = _replicate_entry(entry, op, operand)
        if replicated is not None:
            est, reps, used, notes = replicated
            exact[name] = (est, reps)
            moe = replicate_moe(est, reps)
            moe = np.where(np.isfinite(est) & np.isfinite(moe), moe, np.nan)
            se = moe / Z90
            with np.errstate(divide="ignore", invalid="ignore"):
                cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
            zero_den = np.isnan(est)
            out[f"{name}_estimate"] = est
            out[f"{name}_moe"] = moe
            out[f"{name}_se"] = se
            out[f"{name}_cv_percent"] = cv
            out[f"{name}_method"] = used
            out[f"{name}_notes"] = [
                "; ".join(notes + (["denominator is zero: result undefined"] if z else []))
                for z in zero_den
            ]
            continue
        result = derive_entry(out, entry, metadata=metadata, zero_estimate_rule=zero_estimate_rule)
        for field in DERIVED_FIELDS:
            out[f"{name}_{field}"] = result[field].to_numpy()
    return out
