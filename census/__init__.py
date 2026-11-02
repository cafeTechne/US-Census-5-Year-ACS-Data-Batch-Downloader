"""Headless Census ACS data client and processing library."""

from census.api import (
    DATASETS,
    GEO_LEVELS,
    CensusAPIClient,
    build_geo_params,
    fetch_census_data,
    infer_dataset,
)
from census.cache import QueryCache, default_cache_path, get_default_cache
from census.geo import (
    FIPS_MAPPING,
    STATE_FILES,
    STATE_MAPPING,
    add_geoid_column,
    build_geoid,
    counties_for_state,
    extract_state_abbr_from_filename,
    get_local_places_file,
    get_resource_path,
    load_places_from_local,
    load_zip2fips,
    resolve_zip,
    zctas_for_state,
)
from census.tables import (
    COMPARISON_PROFILE_TABLES,
    DATA_PROFILE_TABLES,
    DATASET_TABLES,
    SUBJECT_TABLES,
    VARIABLE_DEFINITIONS,
    get_variable_descriptions,
    load_table_universes,
    load_tables_from_local,
    load_variable_definitions,
    load_variable_metadata,
)
from census.export import (
    enhanced_melt,
    format_dataframe,
    handle_duplicate_columns,
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)
from census.stats import (
    add_reliability_columns,
    aggregate_columns,
    aggregate_estimates,
    calculate_cv,
    clean_census_sentinels,
    coefficient_of_variation,
    reliability_flag,
    standard_error,
    z_test,
)
from census.inflation import (
    CPI_U_ANNUAL,
    adjust_inflation,
    boundary_shift_advisory,
    deflate_dataframe,
)
from census.panel import (
    build_tidy_panel,
    export_parquet,
    generate_codebook,
    make_slug_map,
)
from census.pipeline import (
    OUTPUT_FORMATS,
    Entity,
    ResearchOptions,
    build_entities,
    prepare_frame,
    resolve_metadata,
    run_batch,
)

__all__ = [
    # api
    "CensusAPIClient",
    "fetch_census_data",
    "infer_dataset",
    "build_geo_params",
    "DATASETS",
    "GEO_LEVELS",
    # cache
    "QueryCache",
    "default_cache_path",
    "get_default_cache",
    # geo
    "STATE_FILES",
    "STATE_MAPPING",
    "FIPS_MAPPING",
    "extract_state_abbr_from_filename",
    "get_local_places_file",
    "get_resource_path",
    "load_places_from_local",
    "add_geoid_column",
    "build_geoid",
    "load_zip2fips",
    "resolve_zip",
    "zctas_for_state",
    "counties_for_state",
    # tables
    "VARIABLE_DEFINITIONS",
    "DATA_PROFILE_TABLES",
    "SUBJECT_TABLES",
    "COMPARISON_PROFILE_TABLES",
    "DATASET_TABLES",
    "load_tables_from_local",
    "load_variable_definitions",
    "load_variable_metadata",
    "load_table_universes",
    "get_variable_descriptions",
    # export
    "format_dataframe",
    "handle_duplicate_columns",
    "enhanced_melt",
    "sanitize_filename",
    "process_data_and_save",
    "save_excel_workbook",
    # stats
    "standard_error",
    "coefficient_of_variation",
    "reliability_flag",
    "calculate_cv",
    "z_test",
    "aggregate_estimates",
    "aggregate_columns",
    "add_reliability_columns",
    "clean_census_sentinels",
    # inflation
    "CPI_U_ANNUAL",
    "adjust_inflation",
    "deflate_dataframe",
    "boundary_shift_advisory",
    # panel
    "build_tidy_panel",
    "generate_codebook",
    "make_slug_map",
    "export_parquet",
    # pipeline
    "OUTPUT_FORMATS",
    "Entity",
    "ResearchOptions",
    "build_entities",
    "prepare_frame",
    "resolve_metadata",
    "run_batch",
]
