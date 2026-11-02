"""End-to-end batch extraction pipeline shared by the GUI, MCP server, and SDK.

    raw API rows -> GEOID -> sentinel cleaning -> CPI-U deflation -> SE/CV
                 -> legacy CSV/Excel outputs, or tidy (GEOID, Year) panel
                 -> codebook.csv + extraction metadata JSON
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from census.api import CensusAPIClient, DATASETS, infer_dataset
from census.cache import get_default_cache
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
    CPI_SOURCE,
    CPI_U_ANNUAL,
    boundary_shift_advisory,
    deflate_dataframe,
    dollar_columns,
)
from census.panel import (
    IDENTIFIER_COLUMNS,
    build_tidy_panel,
    describe_columns,
    export_parquet,
    generate_codebook,
    make_slug_map,
)
from census.stats import add_reliability_columns, clean_census_sentinels
from census.tables import load_table_universes, load_variable_metadata

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
    """Variable metadata for a table: local variables.json, else the API."""
    meta: Dict[str, Dict[str, str]] = {}
    if dataset == "acs5":
        prefix = f"{table_code}_"
        meta = {
            k: v for k, v in load_variable_metadata().items() if k.startswith(prefix)
        }
    if not meta and client is not None:
        raw = client.fetch_variable_metadata(year, table_code, dataset)
        meta = {
            k: {
                "label": str(v.get("label", "")),
                "concept": str(v.get("concept", "")),
                "group": str(v.get("group", "")),
                "predicateType": str(v.get("predicateType", "")),
            }
            for k, v in raw.items()
            if isinstance(v, dict)
        }
    return meta


def prepare_frame(
    raw: List[List[str]],
    year: int,
    options: ResearchOptions,
    metadata: Mapping[str, Mapping[str, str]],
    geo_level: Optional[str] = None,
) -> pd.DataFrame:
    """Apply GEOID, sentinel cleaning, deflation and SE/CV to one API response.

    Columns keep raw Census variable codes.
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
                df, year, options.inflation_base_year, cols, options.cpi_table
            )
    if options.compute_reliability:
        df = add_reliability_columns(df)
    return df


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
) -> Dict[str, Any]:
    """Download ``table_code`` for every entity x year and write outputs."""
    options = options or ResearchOptions()
    emit: LogFn = log or (lambda _msg: None)
    years = sorted(set(int(y) for y in years))

    if output_format not in OUTPUT_FORMATS:
        return {
            "success": False,
            "error": f"Unsupported output_format '{output_format}'. "
            f"Choose from {list(OUTPUT_FORMATS)}.",
        }
    if not years:
        return {"success": False, "error": "No years requested."}
    if not entities:
        return {"success": False, "error": "No geographic entities requested."}
    dataset = dataset or infer_dataset(table_code)
    if dataset not in DATASETS:
        return {"success": False, "error": f"Unsupported dataset '{dataset}'."}
    if options.inflation_base_year:
        cpi = options.cpi_table or CPI_U_ANNUAL
        missing = [y for y in years + [options.inflation_base_year] if y not in cpi]
        if missing:
            return {
                "success": False,
                "error": f"No CPI-U values for year(s) {sorted(set(missing))}; "
                f"available {min(cpi)}-{max(cpi)}. Supply cpi_table to extend.",
            }

    if client is None:
        client = CensusAPIClient(
            cache=get_default_cache() if options.use_cache else None
        )

    os.makedirs(output_dir, exist_ok=True)
    geo_level = entities[0].geo_level
    advisories: List[str] = []
    advisory = boundary_shift_advisory(years[0], years[-1], geo_level)
    if advisory:
        advisories.append(advisory)
        emit(advisory)
    if dataset == "cprofile":
        advisories.append(
            "Comparison Profiles report each period alongside an earlier period with "
            "Census-computed significance flags (*_SS); dollar values are already in "
            "the release year's dollars, and variable codes embed the period, so they "
            "differ between releases."
        )
    elif dataset in ("profile", "subject") and len(years) > 1:
        advisories.append(
            "Data Profile and Subject table line numbers are occasionally renumbered "
            "between releases; confirm variable meanings per year (see the codebook "
            "and Census table shells) before pooling years."
        )

    metadata = resolve_metadata(table_code, dataset, years[-1], client)
    if dataset == "cprofile":
        # Codes embed the comparison period, so each release has its own set.
        for yr in years[:-1]:
            for code, entry in resolve_metadata(table_code, dataset, yr, client).items():
                metadata.setdefault(code, entry)
    universe = load_table_universes().get(table_code, "") if dataset == "acs5" else ""

    is_panel = output_format in PANEL_FORMATS
    total_calls = len(entities) * len(years)
    call_index = 0
    successful_calls = 0
    failed_calls: List[Dict[str, Any]] = []
    seen_columns: Dict[str, None] = {}
    panel_frames: List[pd.DataFrame] = []

    groups: Dict[str, List[Entity]] = {}
    for ent in entities:
        groups.setdefault(ent.group, []).append(ent)

    emit(
        f"Starting download: {len(entities)} request target(s) x {len(years)} year(s) "
        f"({total_calls} calls). Table {table_code} [{dataset}], format: {output_format}."
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

                coded = prepare_frame(data, year, options, metadata, geo_level)
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
                    rename = {
                        k: v
                        for k, v in describe_columns(coded.columns, metadata).items()
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

    span = f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])
    panel_path: Optional[str] = None
    duplicates_dropped = 0
    slug_map: Optional[Dict[str, str]] = None
    columns_out: List[str] = [c for c in seen_columns]

    if is_panel and panel_frames:
        panel, duplicates_dropped = build_tidy_panel(panel_frames)
        columns_out = list(panel.columns)
        slug_map = make_slug_map(columns_out, metadata, options.slug_style)
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

    codebook_path: Optional[str] = None
    if options.include_codebook and successful_calls:
        codebook = generate_codebook(
            columns_out,
            metadata,
            universe=universe,
            slug_map=slug_map,
            inflation_base_year=options.inflation_base_year,
            slug_style=options.slug_style,
        )
        codebook_path = os.path.join(output_dir, f"{table_code}_codebook.csv")
        codebook.to_csv(codebook_path, index=False, encoding="utf-8")
        emit(f"Codebook saved: {codebook_path} ({len(codebook)} variables)")

    endpoint_example = client.endpoint(years[0], dataset)
    metadata_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "table_code": table_code,
        "dataset": dataset,
        "endpoint": endpoint_example.replace(str(years[0]), "{year}", 1),
        "years": years,
        "geo_level": geo_level,
        "entities": [asdict(e) for e in entities],
        "output_format": output_format,
        "options": {
            k: v for k, v in asdict(options).items() if k != "cpi_table"
        },
        "inflation": (
            {"base_year": options.inflation_base_year, "source": CPI_SOURCE}
            if options.inflation_base_year
            else None
        ),
        "moe_confidence_level": 0.90,
        "advisories": advisories,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "outputs": {"panel": panel_path, "codebook": codebook_path},
    }
    metadata_path = os.path.join(output_dir, f"{table_code}_extraction_metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata_doc, f, indent=2, default=str)

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
        "metadata_path": metadata_path,
        "duplicates_dropped": duplicates_dropped,
        "advisories": advisories,
    }
