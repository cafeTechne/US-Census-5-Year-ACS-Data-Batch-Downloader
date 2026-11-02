# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end batch extraction pipeline shared by the GUI, MCP server, CLI and SDK.

    plan:     per-vintage metadata -> concordance (+ reviewed overrides)
              -> variable selection -> continuity check -> block or proceed
    download: raw API rows -> GEOID -> sentinel cleaning -> R-CPI-U-RS deflation
              -> SE/CV -> concept-keyed columns
    write:    legacy CSV/Excel, or tidy (GEOID, Year) panel / Parquet
              + concordance.csv, codebook.csv, extraction metadata JSON

See docs/agent_protocol.md for how agents should act on plan results.
"""

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

import pandas as pd

from census import errors
from census.api import CensusAPIClient, DATASETS, infer_dataset
from census.cache import get_default_cache
from census.concordance import (
    ISSUE_METADATA_UNAVAILABLE,
    NORMALIZATION_VERSION,
    Concordance,
    apply_overrides,
    build_concordance,
    crosswalk_path,
)
from census.continuity import continuity_check
from census.export import (
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)
from census.geo import (
    STATE_MAPPING,
    add_geoid_column,
    zctas_for_state,
)
from census.inflation import (
    DEFAULT_PRICE_INDEX,
    INDEX_DISPLAY,
    PRICE_INDEXES,
    PriceIndexUnavailable,
    boundary_shift_advisory,
    deflate_dataframe,
    dollar_columns,
    get_cpi,
    home_value_advisory,
    price_index_info,
)
from census.metadata import load_manifest, table_universe, table_variables
from census.panel import (
    IDENTIFIER_COLUMNS,
    build_tidy_panel,
    classify_column,
    describe_columns,
    export_parquet,
    generate_codebook,
    make_slug_map,
)
from census.stats import add_reliability_columns, clean_census_sentinels, parse_variable_code
from census.tables import load_variable_metadata

PACKAGE_VERSION = "0.6.0"

CONCORDANCE_MODES = ("fail", "strict", "code")
MAX_ISSUES_IN_RESULT = 100

LEGACY_FORMATS = ("both", "wide_only", "melted_only", "excel")
PANEL_FORMATS = ("panel", "parquet")
OUTPUT_FORMATS = LEGACY_FORMATS + PANEL_FORMATS

GEO_LEVEL_NAMES = ("us", "state", "county", "tract", "place", "zcta", "msa")

# ZCTAs per API request when querying explicit ZCTA lists
ZCTA_CHUNK_SIZE = 50

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str, int], None]


@dataclass
class ResearchOptions:
    """Research-grade processing switches for a batch extraction."""

    clean_sentinels: bool = True
    add_sentinel_notes: bool = False
    compute_reliability: bool = False
    inflation_base_year: Optional[int] = None
    include_codebook: bool = False
    use_cache: bool = True
    slug_style: str = "label"
    cpi_table: Optional[Dict[int, float]] = None
    # R-CPI-U-RS matches the Census Bureau's own ACS adjustments; "cpi-u" also available.
    price_index: str = DEFAULT_PRICE_INDEX
    # "fail": stop when unresolved cross-vintage issues touch selected variables.
    # "strict": keep unresolved items as separate columns and report them.
    # "code": key columns by raw code (single-year, or allow_unverified_concordance).
    concordance_mode: str = "fail"
    continuity_check: bool = True
    allow_unverified_concordance: bool = False


@dataclass
class Entity:
    """One API request target (may cover many rows, e.g. all tracts in a county)."""

    name: str
    geo_level: str
    state_code: Optional[str] = None
    place_code: Optional[str] = None
    county_code: Optional[str] = None
    tract_code: Optional[str] = None
    zcta_code: Optional[str] = None
    msa_code: Optional[str] = None
    group: str = ""  # output folder label; chunks of one request share a group

    def __post_init__(self) -> None:
        if not self.group:
            self.group = self.name

    def request_kwargs(self) -> Dict[str, Any]:
        return {
            "geo_level": self.geo_level,
            "state_code": self.state_code,
            "place_code": self.place_code,
            "county_code": self.county_code,
            "tract_code": self.tract_code,
            "zcta_code": self.zcta_code,
            "msa_code": self.msa_code,
        }


def _join(codes: Optional[Sequence[str]]) -> Optional[str]:
    if not codes:
        return None
    return ",".join(str(c).strip() for c in codes if str(c).strip()) or None


def build_entities(
    geo_level: str,
    state_code: Optional[str] = None,
    codes: Optional[Sequence[str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> List[Entity]:
    """Translate a geography selection into API request entities.

    codes meaning by level:
        place  -> place FIPS codes (required; one request each)
        county -> county FIPS codes (optional; default all counties in state)
        tract  -> county FIPS codes whose tracts to fetch (default: all counties)
        zcta   -> 5-digit ZCTAs (default: ZIPs in state via zip2fips.json, or
                  all ZCTAs nationally when no state is given)
        msa    -> CBSA codes (default: all metro/micro areas)
        state, us -> ignored
    """
    names = names or {}
    state_name = STATE_MAPPING.get(state_code or "", f"State_{state_code}")
    if geo_level not in GEO_LEVEL_NAMES:
        raise ValueError(
            f"Unsupported geo_level: {geo_level}. Choose from {list(GEO_LEVEL_NAMES)}."
        )
    if geo_level in ("place", "county", "tract", "state") and not state_code:
        raise ValueError(f"state_code is required for geo_level '{geo_level}'.")

    if geo_level == "place":
        if not codes:
            raise ValueError("place codes must be provided when geo_level is 'place'.")
        return [
            Entity(names.get(c, f"Place_{c}"), "place", state_code, place_code=c)
            for c in codes
        ]
    if geo_level == "state":
        return [Entity(state_name, "state", state_code)]
    if geo_level == "us":
        return [Entity("United_States", "us")]
    if geo_level == "county":
        joined = _join(codes)
        label = f"{state_name}_All_Counties" if not joined else f"{state_name}_Counties"
        return [Entity(label, "county", state_code, county_code=joined or "*")]
    if geo_level == "tract":
        if not codes:
            return [Entity(f"{state_name}_All_Tracts", "tract", state_code, county_code="*")]
        return [
            Entity(
                f"{names.get(c, 'County_' + c)}_Tracts",
                "tract",
                state_code,
                county_code=c,
            )
            for c in codes
        ]
    if geo_level == "msa":
        joined = _join(codes)
        label = "All_CBSAs" if not joined else "Selected_CBSAs"
        return [Entity(label, "msa", msa_code=joined or "*")]

    # zcta
    zctas = list(codes) if codes else (zctas_for_state(state_code) if state_code else [])
    if not zctas:
        return [Entity("All_ZCTAs", "zcta", zcta_code="*")]
    group = f"{state_name}_ZCTAs" if state_code else "Selected_ZCTAs"
    chunks = [zctas[i : i + ZCTA_CHUNK_SIZE] for i in range(0, len(zctas), ZCTA_CHUNK_SIZE)]
    return [
        Entity(
            group if len(chunks) == 1 else f"{group}_part{i + 1}",
            "zcta",
            state_code,
            zcta_code=",".join(chunk),
            group=group,
        )
        for i, chunk in enumerate(chunks)
    ]


def resolve_metadata(
    table_code: str,
    dataset: str,
    year: int,
    client: Optional[CensusAPIClient] = None,
) -> Dict[str, Dict[str, str]]:
    """Variable metadata for a table in one vintage (archive, then API).

    Falls back to the latest archived detailed-table metadata only when the
    vintage itself is unavailable; callers needing vintage-exact labels should
    use census.metadata.table_variables and check its source.
    """
    variables, _ = table_variables(table_code, dataset, year, client)
    if variables:
        return variables
    if dataset == "acs5":
        prefix = f"{table_code}_"
        return {k: v for k, v in load_variable_metadata().items() if k.startswith(prefix)}
    return {}


def prepare_frame(
    raw: List[List[str]],
    year: int,
    options: ResearchOptions,
    metadata: Mapping[str, Mapping[str, str]],
    geo_level: Optional[str] = None,
) -> pd.DataFrame:
    """Apply GEOID, sentinel cleaning, deflation and SE/CV to one API response.

    Columns keep the year's raw Census variable codes; ``metadata`` must be the
    same vintage's metadata (it decides which columns are dollar values).
    """
    df = pd.DataFrame(raw[1:], columns=raw[0])
    # get=NAME,group(X) returns NAME twice; keep the first of any duplicate.
    df = df.loc[:, ~df.columns.duplicated()]
    df = add_geoid_column(df, geo_level)
    if options.clean_sentinels:
        df = clean_census_sentinels(df, add_notes=options.add_sentinel_notes)
    if options.inflation_base_year:
        cols = dollar_columns(df.columns, metadata)
        if cols:
            # Sentinels must never be scaled into plausible-looking values.
            df = clean_census_sentinels(df, columns=cols)
            df = deflate_dataframe(
                df, year, options.inflation_base_year, cols, options.cpi_table, options.price_index
            )
    if options.compute_reliability:
        df = add_reliability_columns(df)
    return df


def _rename_column(col: str, mapping: Mapping[str, str]) -> Optional[str]:
    """New name for a column under a base->representative mapping (None = drop)."""
    if col.endswith("_NOTE"):
        inner = _rename_column(col[: -len("_NOTE")], mapping)
        return f"{inner}_NOTE" if inner else None
    parsed = parse_variable_code(col)
    if parsed:
        base, suffix = parsed
        return f"{mapping[base]}{suffix}" if base in mapping else None
    info = classify_column(col)
    if info and info[1] in ("SE", "CV", "RELIABILITY"):
        base, kind, pct = info
        return f"{mapping[base]}{'P' if pct else ''}_{kind}" if base in mapping else None
    return col  # identifiers and geography columns


def apply_concordance(df: pd.DataFrame, mapping: Mapping[str, str]) -> pd.DataFrame:
    """Rename one year's columns to concept-representative codes.

    Variables outside ``mapping`` (unselected, excluded, or unknown to the
    vintage metadata) are dropped.
    """
    renames: Dict[str, str] = {}
    keep: List[str] = []
    for col in df.columns:
        new = _rename_column(str(col), mapping)
        if new is None:
            continue
        keep.append(col)
        renames[col] = new
    out = df[keep].rename(columns=renames)
    if out.columns.duplicated().any():
        dupes = sorted(set(out.columns[out.columns.duplicated()]))
        raise AssertionError(f"Concordance mapped several columns to {dupes[:5]}")
    return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _is_blocking(issue: Any, multi_year: bool) -> bool:
    if issue.kind == ISSUE_METADATA_UNAVAILABLE and not multi_year:
        return False
    return issue.blocking


def _issue_dicts(issues: Sequence[Any], multi_year: bool) -> List[Dict[str, Any]]:
    out = []
    for issue in issues:
        d = issue.to_dict()
        d["blocking"] = _is_blocking(issue, multi_year)
        out.append(d)
    return out


def _price_index_error(options: ResearchOptions, years: Sequence[int]) -> Optional[Dict[str, Any]]:
    if not options.inflation_base_year:
        return None
    if options.price_index not in PRICE_INDEXES:
        return errors.error_result(
            errors.INVALID_INPUT,
            f"price_index must be one of {PRICE_INDEXES}.",
        )
    try:
        for year in list(years) + [options.inflation_base_year]:
            get_cpi(year, options.cpi_table, options.price_index)
    except PriceIndexUnavailable as exc:
        return errors.error_result(errors.PRICE_INDEX_UNAVAILABLE, str(exc.args[0]))
    return None


def plan_extraction(
    table_code: str,
    entities: Sequence[Entity],
    years: Sequence[int],
    output_format: str = "panel",
    options: Optional[ResearchOptions] = None,
    dataset: Optional[str] = None,
    variables: Optional[Sequence[str]] = None,
    client: Optional[CensusAPIClient] = None,
) -> Dict[str, Any]:
    """Dry run: validate, build the concordance, check continuity, count calls.

    Makes at most one national-level API call per year (for the continuity
    check; cached), and no data downloads. The returned dict is JSON-safe
    except for the private ``_concordance`` key, which run_batch reuses.
    """
    options = options or ResearchOptions()
    years = sorted(set(int(y) for y in years))
    table_code = table_code.strip().upper()

    if output_format not in OUTPUT_FORMATS:
        return errors.error_result(
            errors.INVALID_OUTPUT_FORMAT,
            f"Unsupported output_format '{output_format}'. Choose from {list(OUTPUT_FORMATS)}.",
        )
    if not years:
        return errors.error_result(errors.INVALID_INPUT, "No years requested.")
    if not entities:
        return errors.error_result(errors.INVALID_INPUT, "No geographic entities requested.")
    dataset = dataset or infer_dataset(table_code)
    if dataset not in DATASETS:
        return errors.error_result(errors.INVALID_DATASET, f"Unsupported dataset '{dataset}'.")
    if options.concordance_mode not in CONCORDANCE_MODES:
        return errors.error_result(
            errors.INVALID_INPUT, f"concordance_mode must be one of {CONCORDANCE_MODES}."
        )
    price_error = _price_index_error(options, years)
    if price_error:
        return price_error

    if client is None:
        client = CensusAPIClient(cache=get_default_cache() if options.use_cache else None)

    multi_year = len(years) > 1
    geo_level = entities[0].geo_level

    conc = build_concordance(table_code, dataset, years, client)
    override_report = apply_overrides(conc, client)
    if not conc.concepts:
        return errors.error_result(
            errors.METADATA_UNAVAILABLE,
            f"No variable metadata found for {table_code} ({dataset}) in {years}.",
            issues=_issue_dicts(conc.issues, multi_year),
        )

    selected, unknown, resolution = conc.explain_selection(variables)
    if unknown:
        return errors.error_result(
            errors.INVALID_VARIABLES,
            f"Not found in {table_code} for {years[0]}-{years[-1]}: {unknown}",
            unknown_variables=unknown,
        )

    continuity_report: Dict[str, Any] = {}
    if multi_year and options.continuity_check and options.concordance_mode != "code":
        flags, continuity_report = continuity_check(conc, selected, client)
        conc.issues.extend(flags)

    relevant = conc.issues_for(selected)
    blocking = [i for i in relevant if _is_blocking(i, multi_year)]

    advisories: List[str] = []
    for adv in (
        boundary_shift_advisory(years[0], years[-1], geo_level),
        home_value_advisory(table_code, years) if options.inflation_base_year else None,
    ):
        if adv:
            advisories.append(adv)
    if dataset == "cprofile":
        advisories.append(
            "Comparison Profiles carry several periods per release with Census-computed "
            "significance flags (*_SS); concepts are keyed by role (current, comparison, "
            "significance) because codes embed the period."
        )
    if variables is None and multi_year:
        advisories.append(
            "No variables= selection: every line of the table is checked, so unrelated "
            "drift can block the run. Name the variables your analysis uses."
        )

    would_block = False
    block_reason = ""
    if options.concordance_mode == "code" and multi_year and not options.allow_unverified_concordance:
        would_block, block_reason = True, errors.UNVERIFIED_CONCORDANCE
    elif options.concordance_mode == "fail" and blocking:
        would_block, block_reason = True, errors.CONCORDANCE_REVIEW_REQUIRED

    cache = client.cache
    calls = 0
    cached = 0
    for year in years:
        for ent in entities:
            calls += 1
            if cache is not None:
                try:
                    endpoint, params = client.build_request(
                        year=year, table_code=table_code, dataset=dataset, **ent.request_kwargs()
                    )
                except ValueError as exc:
                    return errors.error_result(errors.INVALID_GEO_LEVEL, str(exc))
                if cache.get(endpoint, params) is not None:
                    cached += 1

    issue_dicts = _issue_dicts(relevant, multi_year)
    issue_dicts.sort(key=lambda d: (not d["blocking"], d["kind"]))
    return {
        "success": True,
        "table_code": table_code,
        "dataset": dataset,
        "years": years,
        "geo_level": geo_level,
        "output_format": output_format,
        "concordance_mode": options.concordance_mode,
        "api_calls": calls,
        "cached_calls": cached,
        "selected_concepts": len(selected),
        "selection": resolution,
        "concordance_summary": conc.summary(),
        "issues": issue_dicts[:MAX_ISSUES_IN_RESULT],
        "issue_count": len(issue_dicts),
        "blocking_issue_count": sum(1 for d in issue_dicts if d["blocking"]),
        "would_block": would_block,
        "block_reason": block_reason,
        "advisories": advisories,
        "overrides": override_report,
        "continuity": {
            "checked_concepts": len(continuity_report),
            "passed": sum(1 for r in continuity_report.values() if r["passed"]),
        },
        "_concordance": conc,
        "_selected": selected,
        "_continuity": continuity_report,
    }


def public_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop private keys so a plan can be serialized."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _git_commit() -> Optional[str]:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _vintage_provenance(conc: Concordance) -> Dict[str, Any]:
    files = load_manifest().get("files", {})
    out: Dict[str, Any] = {}
    for year, source in conc.sources.items():
        entry = files.get(f"{conc.dataset}/{year}/variables.json.gz", {})
        out[str(year)] = {"source": source, "sha256": entry.get("sha256"), "url": entry.get("url")}
    return out


def _write_json(path: str, payload: Any) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_batch(
    table_code: str,
    entities: Sequence[Entity],
    years: Sequence[int],
    output_dir: str,
    output_format: str = "both",
    options: Optional[ResearchOptions] = None,
    dataset: Optional[str] = None,
    client: Optional[CensusAPIClient] = None,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    variables: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Plan, then download ``table_code`` for every entity x year and write outputs.

    Never raises on API failures; check ``success``, ``error_code`` and
    ``failed_calls``. When the plan blocks, the concordance and issue files are
    still written to ``output_dir`` for review.
    """
    options = options or ResearchOptions()
    emit: LogFn = log or (lambda _msg: None)
    table_code = table_code.strip().upper()
    if client is None:
        client = CensusAPIClient(cache=get_default_cache() if options.use_cache else None)

    plan = plan_extraction(
        table_code, entities, years, output_format, options, dataset, variables, client
    )
    if not plan.get("success"):
        return plan
    years = plan["years"]
    dataset = plan["dataset"]
    conc: Concordance = plan["_concordance"]
    selected: Set[str] = plan["_selected"]
    multi_year = len(years) > 1
    geo_level = plan["geo_level"]
    span = f"{years[0]}-{years[-1]}" if multi_year else str(years[0])

    os.makedirs(output_dir, exist_ok=True)
    for adv in plan["advisories"]:
        emit(adv)

    use_concordance = options.concordance_mode != "code"
    reps = conc.representative_bases()
    if use_concordance and len(set(reps.values())) != len(reps):
        raise AssertionError("Two concepts share a panel column; concordance is inconsistent.")
    slugs_by_concept: Dict[str, str] = {}

    concordance_path = os.path.join(output_dir, f"{table_code}_concordance.csv")
    issues_path = os.path.join(output_dir, f"{table_code}_concordance_issues.json")
    all_issues = _issue_dicts(conc.issues_for(selected), multi_year)
    _write_json(
        issues_path,
        {
            "table_code": table_code,
            "dataset": dataset,
            "years": years,
            "summary": conc.summary(),
            "continuity": plan["_continuity"],
            "issues": all_issues,
        },
    )

    if plan["would_block"]:
        conc.write_csv(concordance_path, selected=selected)
        message = (
            f"{plan['blocking_issue_count']} blocking concordance issue(s) affect the selected "
            f"variables of {table_code} {span}. Review {issues_path}, resolve them with reviewed "
            "overrides (see docs/agent_protocol.md), narrow variables=, or use "
            "concordance_mode='strict' for exploratory output."
            if plan["block_reason"] == errors.CONCORDANCE_REVIEW_REQUIRED
            else "concordance_mode='code' across several years pools codes without checking "
            "their meaning; pass allow_unverified_concordance=True to accept that."
        )
        return errors.error_result(
            plan["block_reason"],
            message,
            plan=public_plan(plan),
            concordance_path=concordance_path,
            issues_path=issues_path,
        )

    is_panel = output_format in PANEL_FORMATS
    total_calls = len(entities) * len(years)
    call_index = 0
    successful_calls = 0
    failed_calls: List[Dict[str, Any]] = []
    seen_columns: Dict[str, None] = {}
    panel_frames: List[pd.DataFrame] = []

    rep_metadata = conc.representative_metadata()
    groups: Dict[str, List[Entity]] = {}
    for ent in entities:
        groups.setdefault(ent.group, []).append(ent)

    emit(
        f"Starting download: {len(entities)} request target(s) x {len(years)} year(s) "
        f"({total_calls} calls, {plan['cached_calls']} cached). Table {table_code} "
        f"[{dataset}], format: {output_format}, concordance: {options.concordance_mode}."
    )

    saved_entities: List[Dict[str, Any]] = []
    for group_idx, (group_name, members) in enumerate(groups.items(), 1):
        folder = os.path.join(output_dir, f"{sanitize_filename(group_name)}_{table_code}")
        if not is_panel:
            os.makedirs(folder, exist_ok=True)
        acc: Dict[str, List[pd.DataFrame]] = {"wide": [], "melted": []}
        years_saved: List[int] = []
        rows_saved = 0
        emit(f"[{group_idx}/{len(groups)}] Processing {group_name} ({geo_level})...")

        for year in years:
            year_ok = False
            year_meta = conc.year_metadata.get(year) or resolve_metadata(table_code, dataset, year, client)
            code_map = conc.code_map(year)
            mapping = {
                base: rep for base, rep in conc.rename_map(year).items() if code_map[base] in selected
            }
            for ent in members:
                call_index += 1
                if progress:
                    progress(call_index, total_calls, ent.name, year)
                data = client.fetch(
                    year=year, table_code=table_code, dataset=dataset, **ent.request_kwargs()
                )
                if not data or len(data) < 2:
                    reason = client.last_error or "no data returned"
                    failed_calls.append({"entity": ent.name, "year": year, "error": reason})
                    emit(f"  {year} {ent.name}: skipped ({reason}).")
                    continue

                coded = prepare_frame(data, year, options, year_meta, geo_level)
                if use_concordance:
                    coded = apply_concordance(coded, mapping)
                for col in coded.columns:
                    seen_columns.setdefault(str(col), None)
                successful_calls += 1
                rows_saved += len(coded)
                year_ok = True

                if is_panel:
                    coded = coded.copy()
                    coded.insert(1, "Year", year)
                    panel_frames.append(coded)
                else:
                    labels_from = rep_metadata if use_concordance else year_meta
                    rename = {
                        k: v
                        for k, v in describe_columns(coded.columns, labels_from).items()
                        if k not in IDENTIFIER_COLUMNS
                    }
                    process_data_and_save(
                        formatted_df=coded.rename(columns=rename),
                        folder_name=folder,
                        entity_name=ent.name,
                        table_code=table_code,
                        year=year,
                        output_format=output_format,
                        excel_accumulator=acc,
                    )
                emit(f"  {year} {ent.name}: {len(coded)} row(s), {len(coded.columns)} columns.")
            if year_ok:
                years_saved.append(year)

        excel_path = None
        if output_format == "excel":
            excel_path = save_excel_workbook(folder, group_name, table_code, acc)
            if excel_path:
                emit(f"  Excel workbook saved: {os.path.basename(excel_path)}")
        saved_entities.append(
            {
                "entity": group_name,
                "folder": None if is_panel else folder,
                "years_saved": years_saved,
                "rows_saved": rows_saved,
                "excel_path": excel_path,
            }
        )

    if use_concordance:
        label_metadata: Dict[str, Dict[str, str]] = rep_metadata
    else:
        label_metadata = {}
        for year in years:  # latest vintage wins
            label_metadata.update(conc.year_metadata.get(year, {}))

    panel_path: Optional[str] = None
    duplicates_dropped = 0
    slug_map: Optional[Dict[str, str]] = None
    columns_out: List[str] = list(seen_columns)

    if is_panel and panel_frames:
        panel, duplicates_dropped = build_tidy_panel(panel_frames)
        columns_out = list(panel.columns)
        slug_map = make_slug_map(columns_out, label_metadata, options.slug_style)
        panel = panel.rename(columns=slug_map)
        if duplicates_dropped:
            emit(f"Dropped {duplicates_dropped} duplicate (GEOID, Year) row(s).")
        ext = "parquet" if output_format == "parquet" else "csv"
        panel_path = os.path.join(output_dir, f"{table_code}_{geo_level}_panel_{span}.{ext}")
        if ext == "parquet":
            export_parquet(panel, panel_path)
        else:
            panel.to_csv(panel_path, index=False, encoding="utf-8")
        emit(f"Tidy panel saved: {panel_path} ({len(panel)} rows)")
    elif not is_panel and "Year" not in seen_columns:
        columns_out = ["Year"] + columns_out

    if slug_map and use_concordance:
        base_to_cid = {b: cid for cid, b in reps.items()}
        for col, slug in slug_map.items():
            parsed = parse_variable_code(col)
            if parsed and parsed[1] in ("E", "PE", "SS") and parsed[0] in base_to_cid:
                slugs_by_concept.setdefault(base_to_cid[parsed[0]], slug)
    if use_concordance:
        conc.write_csv(concordance_path, slugs_by_concept, selected)
    else:
        concordance_path = None  # type: ignore[assignment]

    universe = table_universe(table_code, dataset, years[-1], client)
    codebook_path: Optional[str] = None
    if options.include_codebook and successful_calls:
        codebook = generate_codebook(
            columns_out,
            label_metadata,
            universe=universe,
            slug_map=slug_map,
            inflation_base_year=options.inflation_base_year,
            slug_style=options.slug_style,
            price_index=INDEX_DISPLAY.get(options.price_index, options.price_index),
            concordance=conc if use_concordance else None,
        )
        codebook_path = os.path.join(output_dir, f"{table_code}_codebook.csv")
        codebook.to_csv(codebook_path, index=False, encoding="utf-8")
        emit(f"Codebook saved: {codebook_path} ({len(codebook)} variables)")

    metadata_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_version": PACKAGE_VERSION,
        "git_commit": _git_commit(),
        "table_code": table_code,
        "dataset": dataset,
        "endpoint": client.endpoint(years[0], dataset).replace(str(years[0]), "{year}", 1),
        "years": years,
        "geo_level": geo_level,
        "entities": [asdict(e) for e in entities],
        "output_format": output_format,
        "variables_requested": list(variables) if variables else None,
        "options": {k: v for k, v in asdict(options).items() if k != "cpi_table"},
        "inflation": (
            {
                "base_year": options.inflation_base_year,
                "index": INDEX_DISPLAY.get(options.price_index, options.price_index),
                "source": price_index_info(options.price_index) if options.cpi_table is None else "cpi_table supplied by caller",
            }
            if options.inflation_base_year
            else None
        ),
        "concordance": {
            "mode": options.concordance_mode,
            "normalization_version": NORMALIZATION_VERSION,
            "summary": conc.summary(),
            "selected_concepts": len(selected),
            "overrides_file": crosswalk_path(dataset, table_code) if conc.overrides_applied else None,
            "overrides_applied": conc.overrides_applied,
            "metadata_vintages": _vintage_provenance(conc),
        },
        "moe_confidence_level": 0.90,
        "advisories": plan["advisories"],
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "outputs": {
            "panel": panel_path,
            "codebook": codebook_path,
            "concordance": concordance_path,
            "issues": issues_path,
        },
    }
    metadata_path = _write_json(
        os.path.join(output_dir, f"{table_code}_extraction_metadata.json"), metadata_doc
    )

    return {
        "success": True,
        "base_directory": output_dir,
        "table_code": table_code,
        "dataset": dataset,
        "total_calls_attempted": total_calls,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "format": output_format,
        "entities": saved_entities,
        "panel_path": panel_path,
        "codebook_path": codebook_path,
        "concordance_path": concordance_path,
        "issues_path": issues_path,
        "metadata_path": metadata_path,
        "duplicates_dropped": duplicates_dropped,
        "advisories": plan["advisories"],
        "concordance_issues": all_issues[:MAX_ISSUES_IN_RESULT],
        "concordance_issue_count": len(all_issues),
    }
