# SPDX-License-Identifier: AGPL-3.0-or-later
"""Research output formats: tidy (GEOID, Year) panels, codebooks, and Parquet.

All functions here operate on frames whose value columns still carry raw Census
variable codes (``B19013_001E``), plus optional derived columns produced by
``census.stats`` (``B19013_001_SE`` ...) and ``census.stats.clean_census_sentinels``
(``B19013_001E_NOTE``).
"""

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from census.inflation import is_dollar_variable
from census.stats import BASE_PATTERN, MEASUREMENT_TYPES, parse_variable_code

IDENTIFIER_COLUMNS: Dict[str, str] = {
    "GEOID": "Standard Census GEOID (TIGER/Line join key)",
    "Year": "ACS 5-year period end year",
    "NAME": "Geographic area name",
}

_DERIVED_RE = re.compile(
    rf"^(?P<base>{BASE_PATTERN})(?P<pct>P?)_(?P<kind>SE|CV|RELIABILITY)$"
)
_DOLLAR_IN_LABEL_RE = re.compile(r"\s*\(in \d{4} inflation-adjusted dollars\)", re.I)
_LABEL_PREFIX_RE = re.compile(
    r"^(Estimate|Percent|Margin of Error|Percent Margin of Error)!!", re.I
)

SLUG_SUFFIXES: Dict[str, str] = {
    "E": "est",
    "M": "moe",
    "PE": "pct",
    "PM": "pct_moe",
    "ANN": "ann",
    "SE": "se",
    "CV": "cv",
    "RELIABILITY": "rel",
    "SIG": "sig",
}

Metadata = Mapping[str, Mapping[str, str]]


# ---------------------------------------------------------------------------
# Column classification and labels
# ---------------------------------------------------------------------------


def classify_column(col: str) -> Optional[Tuple[str, str, bool]]:
    """Return (variable_base, kind, is_percent) for a value/derived column.

    kind is one of E, M, PE, PM, ANN, SE, CV, RELIABILITY, NOTE(:<inner kind>).
    """
    if col.endswith("_NOTE"):
        inner = classify_column(col[: -len("_NOTE")])
        if inner:
            return inner[0], f"NOTE:{inner[1]}", inner[2]
        return None
    derived = _DERIVED_RE.match(col)
    if derived:
        return derived.group("base"), derived.group("kind"), bool(derived.group("pct"))
    parsed = parse_variable_code(col)
    if parsed:
        base, suffix = parsed
        if suffix == "SS":
            return base, "SIG", False
        if MEASUREMENT_TYPES[suffix] == "Annotation":
            return base, "ANN", suffix.startswith("P")
        return base, suffix, suffix in ("PE", "PM")
    return None


def _label_of(code: str, metadata: Metadata) -> str:
    entry = metadata.get(code)
    if not entry:
        return ""
    # Labels come from a single metadata vintage; the year in "(in 2021
    # inflation-adjusted dollars)" would be wrong for other periods. The
    # codebook Unit column records the actual dollar basis.
    return _DOLLAR_IN_LABEL_RE.sub(" (inflation-adjusted dollars)", str(entry.get("label", "")))


def base_label(base: str, is_percent: bool, metadata: Metadata) -> str:
    """Label of the underlying estimate (PE for percent pairs, else E)."""
    if is_percent:
        return _label_of(f"{base}PE", metadata) or _label_of(f"{base}E", metadata)
    return (
        _label_of(f"{base}E", metadata)
        or _label_of(f"{base}PE", metadata)
        or _label_of(f"{base}SS", metadata)
    )


def describe_column(col: str, metadata: Metadata) -> str:
    """Human-readable description for a raw, derived, or note column."""
    if col in IDENTIFIER_COLUMNS:
        return IDENTIFIER_COLUMNS[col]
    direct = _label_of(col, metadata)
    if direct:
        return direct
    info = classify_column(col)
    if not info:
        return ""
    base, kind, pct = info
    found = base_label(base, pct, metadata)
    if not found and kind in ("E", "PE"):
        return ""  # no label known: keep the raw code rather than invent one
    label = found or f"{base}{'PE' if pct else 'E'}"
    stripped = _LABEL_PREFIX_RE.sub("", label)
    if kind.startswith("NOTE:"):
        inner = describe_column(col[:-5], metadata) or col[:-5]
        return f"Census footnote for {inner}"
    return {
        "E": label,
        "PE": label,
        "M": f"Margin of Error (90%)!!{stripped}",
        "PM": f"Percent Margin of Error (90%)!!{stripped}",
        "ANN": f"{_annotation_kind(col)} Annotation!!{stripped}",
        "SE": f"Standard Error!!{stripped}",
        "CV": f"Coefficient of Variation (%)!!{stripped}",
        "RELIABILITY": f"Reliability (CV<=15% High, <=30% Moderate, else Low)!!{stripped}",
        "SIG": f"Significance of change between periods (* = significant at 90%)!!{stripped}",
    }.get(kind, label)


def _annotation_kind(col: str) -> str:
    parsed = parse_variable_code(col)
    suffix = parsed[1] if parsed else ""
    return {
        "EA": "Estimate",
        "MA": "Margin of Error",
        "PEA": "Percent Estimate",
        "PMA": "Percent Margin of Error",
    }.get(suffix, "")


def describe_columns(columns: Iterable[str], metadata: Metadata) -> Dict[str, str]:
    """Map columns to readable labels (only those with a known description)."""
    out: Dict[str, str] = {}
    for col in columns:
        desc = describe_column(str(col), metadata)
        if desc:
            out[str(col)] = desc
    return out


def measurement_type(col: str) -> str:
    if col in IDENTIFIER_COLUMNS:
        return "Identifier"
    info = classify_column(col)
    if not info:
        return "Other"
    kind = info[1]
    if kind.startswith("NOTE:"):
        return "Annotation"
    return {
        "E": "Estimate",
        "M": "Margin of Error",
        "PE": "Percentage",
        "PM": "Margin of Error",
        "ANN": "Annotation",
        "SE": "Standard Error",
        "CV": "Coefficient of Variation",
        "RELIABILITY": "Reliability Flag",
        "SIG": "Significance Flag",
    }[kind]


def infer_unit(
    col: str,
    metadata: Metadata,
    inflation_base_year: Optional[int] = None,
    price_index: str = "R-CPI-U-RS",
) -> str:
    if col in IDENTIFIER_COLUMNS:
        return "Year" if col == "Year" else "Text"
    info = classify_column(col)
    if not info:
        return ""
    base, kind, pct = info
    if kind.startswith("NOTE:") or kind in ("ANN", "RELIABILITY", "SIG"):
        return "Text"
    if kind == "CV" or pct:
        return "Percent"
    label = base_label(base, pct, metadata)
    entry = metadata.get(f"{base}E") or {}
    if is_dollar_variable(str(entry.get("label", "")), str(entry.get("concept", ""))):
        if inflation_base_year:
            return f"Dollars (constant {inflation_base_year} dollars, {price_index})"
        return "Dollars (nominal, period end-year dollars)"
    if "percent" in label.lower() or label.lower().rstrip().endswith("rate"):
        return "Percent"
    if "ratio" in label.lower():
        return "Ratio"
    if "median age" in f"{label} {entry.get('concept', '')}".lower():
        return "Years"
    return "Count"


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------


def _slug_part(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def slugify(text: str, max_len: int = 48) -> str:
    """Snake-case a Census label, keeping its most specific parts when too long.

    Labels run general -> specific ("Total!!Population for whom poverty status
    is determined!!AGE!!Under 18 years"); long ones keep the first part plus as
    many trailing parts as fit, so sibling variables stay distinguishable.
    """
    text = _DOLLAR_IN_LABEL_RE.sub("", text)
    text = _LABEL_PREFIX_RE.sub("", text)
    parts = [p for p in (_slug_part(x) for x in text.split("!!")) if p]
    slug = "_".join(parts)
    if len(slug) > max_len and len(parts) > 1:
        tail: List[str] = []
        for part in reversed(parts[1:]):
            if len("_".join([parts[0], part] + tail)) > max_len:
                break
            tail.insert(0, part)
        if tail:
            slug = "_".join([parts[0]] + tail)
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("_", 1)[0] if "_" in cut else cut
    if slug and slug[0].isdigit():
        slug = f"v_{slug}"
    return slug


def make_slug_map(
    columns: Iterable[str],
    metadata: Metadata,
    style: str = "label",
    max_len: int = 48,
) -> Dict[str, str]:
    """Map columns to unique, sanitized snake_case identifiers.

    style='label' derives stems from variable labels (``total_male_under_5_years_est``);
    style='code' uses variable codes (``b01001_003_est``), which stay within
    Stata's 32-character limit.
    """
    columns = [str(c) for c in columns]
    mapping: Dict[str, str] = {}
    # One stem per variable base: E / M / PE / PM / SE / CV share it and are
    # distinguished by suffix (_est, _moe, _pct, _pct_moe, ...).
    stems: Dict[str, str] = {}
    used_stems: Dict[str, str] = {}

    for col in sorted(columns):
        info = classify_column(col)
        if not info or info[0] in stems:
            continue
        base = info[0]
        if style == "code":
            stem = base.lower()
        else:
            stem = slugify(base_label(base, False, metadata), max_len) or base.lower()
            if "__" in base:  # concept segment of a reused code
                stem = f"{stem}_y{base.split('__', 1)[1]}"
        if stem in used_stems and used_stems[stem] != base:
            stem = f"{stem}_{base.rsplit('_', 1)[-1]}"
        if stem in used_stems and used_stems[stem] != base:
            stem = base.lower()
        stems[base] = stem
        used_stems[stem] = base

    taken = set()
    for col in columns:
        if col in IDENTIFIER_COLUMNS:
            slug = col.lower()
        else:
            info = classify_column(col)
            if not info:
                slug = re.sub(r"[^0-9a-zA-Z]+", "_", col).strip("_").lower() or "col"
            else:
                base, kind, pct = info
                stem = stems[base]
                if kind.startswith("NOTE:"):
                    inner = kind.split(":", 1)[1]
                    slug = f"{stem}_{SLUG_SUFFIXES.get(inner, inner.lower())}_note"
                else:
                    suffix = SLUG_SUFFIXES[kind]
                    if pct and kind in ("SE", "CV", "RELIABILITY"):
                        suffix = f"pct_{suffix}"
                    slug = f"{stem}_{suffix}"
        base_slug, n = slug, 2
        while slug in taken:
            slug = f"{base_slug}_{n}"
            n += 1
        taken.add(slug)
        mapping[col] = slug
    return mapping


# ---------------------------------------------------------------------------
# Codebook
# ---------------------------------------------------------------------------

CODEBOOK_COLUMNS = [
    "Variable_Code",
    "Variable_Slug",
    "Measurement_Type",
    "Full_Description",
    "Concept",
    "Universe",
    "Unit",
]
CONCORDANCE_CODEBOOK_COLUMNS = [
    "Concept_ID",
    "Years_Available",
    "Source_Codes_By_Year",
    "Concordance_Status",
]


def generate_codebook(
    columns: Iterable[str],
    metadata: Metadata,
    universe: str = "",
    slug_map: Optional[Mapping[str, str]] = None,
    inflation_base_year: Optional[int] = None,
    slug_style: str = "label",
    price_index: str = "R-CPI-U-RS",
    concordance: Optional[Any] = None,
) -> pd.DataFrame:
    """Build a data dictionary for the given output columns.

    With a ``census.concordance.Concordance`` whose representative bases name
    the columns, adds Concept_ID, Years_Available, Source_Codes_By_Year and
    Concordance_Status.
    """
    rep_to_cid: Dict[str, str] = {}
    if concordance is not None:
        rep_to_cid = {b: cid for cid, b in concordance.representative_bases().items()}
    columns = [str(c) for c in columns]
    slugs = dict(slug_map) if slug_map else make_slug_map(columns, metadata, slug_style)
    rows: List[Dict[str, str]] = []
    for col in columns:
        info = classify_column(col)
        concept = ""
        if info:
            concept = _DOLLAR_IN_LABEL_RE.sub(
                " (INFLATION-ADJUSTED DOLLARS)",
                str(
                    (metadata.get(f"{info[0]}E") or metadata.get(f"{info[0]}PE") or {}).get(
                        "concept", ""
                    )
                ),
            )
        rows.append(
            {
                "Variable_Code": col,
                "Variable_Slug": slugs.get(col, ""),
                "Measurement_Type": measurement_type(col),
                "Full_Description": describe_column(col, metadata),
                "Concept": concept,
                "Universe": universe if info else "",
                "Unit": infer_unit(col, metadata, inflation_base_year, price_index),
            }
        )
        if concordance is not None:
            cid = rep_to_cid.get(info[0], "") if info else ""
            c = concordance.concepts.get(cid) if cid else None
            rows[-1].update(
                {
                    "Concept_ID": cid,
                    "Years_Available": ",".join(str(y) for y in c.years) if c else "",
                    "Source_Codes_By_Year": ";".join(f"{y}:{c.codes[y]}" for y in c.years) if c else "",
                    "Concordance_Status": ";".join(c.statuses) if c else "",
                }
            )
    cols = CODEBOOK_COLUMNS + (CONCORDANCE_CODEBOOK_COLUMNS if concordance is not None else [])
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Tidy panel
# ---------------------------------------------------------------------------

PANEL_ID_COLUMNS = ["GEOID", "Year", "NAME"]


def build_tidy_panel(
    frames: Iterable[pd.DataFrame],
    keep_annotations: bool = False,
) -> Tuple[pd.DataFrame, int]:
    """Stack coded per-year frames into a (GEOID, Year)-unique panel.

    Returns the panel (still using raw codes as column names; see
    ``make_slug_map`` to rename) and the number of duplicate rows dropped.
    Raises ValueError when GEOID or Year is missing.
    """
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=PANEL_ID_COLUMNS), 0
    combined = pd.concat(frames, ignore_index=True, sort=False)
    for col in ("GEOID", "Year"):
        if col not in combined.columns:
            raise ValueError(f"Panel frames must contain a '{col}' column.")

    value_cols: List[str] = []
    for col in combined.columns:
        info = classify_column(str(col))
        if not info:
            continue
        if info[1] == "ANN" and not keep_annotations:
            continue
        value_cols.append(col)

    id_cols = [c for c in PANEL_ID_COLUMNS if c in combined.columns]
    panel = combined[id_cols + value_cols].copy()
    for col in value_cols:
        kind = classify_column(str(col))[1]  # type: ignore[index]
        if kind in ("E", "M", "PE", "PM", "SE", "CV"):
            panel[col] = pd.to_numeric(panel[col], errors="coerce")
        else:
            panel[col] = panel[col].fillna("").astype(str)
    panel["GEOID"] = panel["GEOID"].astype(str)
    panel["Year"] = pd.to_numeric(panel["Year"]).astype("int64")

    before = len(panel)
    panel = panel.drop_duplicates(subset=["GEOID", "Year"], keep="first")
    dropped = before - len(panel)
    panel = panel.sort_values(["GEOID", "Year"]).reset_index(drop=True)
    return panel, dropped


def export_parquet(df: pd.DataFrame, path: str, compression: str = "snappy") -> str:
    """Write a DataFrame to Parquet (pyarrow engine) preserving native dtypes."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].astype("string")
    out.to_parquet(path, engine="pyarrow", compression=compression, index=False)
    return path
