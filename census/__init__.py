# SPDX-License-Identifier: AGPL-3.0-or-later
"""Headless Census ACS data client and processing library."""

__version__ = "0.13.0"

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
    default_metadata_file,
    load_table_catalog,
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
from census.data_integrity import BundledDataIntegrityError
from census.inflation import (
    CPI_U_ANNUAL,
    DEFAULT_PRICE_INDEX,
    PRICE_INDEXES,
    PriceIndexUnavailable,
    adjust_inflation,
    boundary_shift_advisory,
    deflate_dataframe,
    home_value_advisory,
    load_price_index,
    price_index_info,
)
from census.metadata import (
    archived_vintages,
    available_vintages,
    describe_variable,
    latest_vintage,
    table_variables,
    vintage_groups,
    vintage_variables,
)
from census.concordance import (
    Concordance,
    apply_overrides,
    build_concordance,
    build_evidence,
    canonical_label,
    save_evidence,
    validate_all_crosswalks,
    validate_crosswalk_row,
)
from census.continuity import continuity_check
from census import errors
from census.panel import (
    build_tidy_panel,
    export_parquet,
    generate_codebook,
    make_slug_map,
)
from census.pipeline import (
    CONCORDANCE_MODES,
    OUTPUT_FORMATS,
    Entity,
    ResearchOptions,
    apply_concordance,
    build_entities,
    plan_extraction,
    prepare_frame,
    public_plan,
    resolve_metadata,
    run_batch,
)

from census.pipeline import (
    REPLAY_SCHEMA_VERSION,
    diff_frames,
    expand_states,
    file_sha256,
    is_nationwide,
    replay_extraction,
)
from census.inflation import C_CPI_U, provisional_advisory, provisional_years
from census.concordance import RULE_COLUMNS, build_rule_evidence, load_rules, rules_path, validate_rule_row
from census.review import draft_rule_override, record_rule_review
from census.variance import (
    DerivedEstimate,
    REPLICATE_COLUMNS,
    REPLICATE_COUNT,
    Z90,
    derived_from_replicates,
    moe_to_se,
    replicate_moe,
    replicate_se,
    replicate_variance,
    se_to_moe,
)
from census.derived import (
    METHOD_SCALE,
    derive_columns,
    difference,
    percent_change,
    product,
    proportion,
    ratio,
    scale,
    subset_relationship,
    sum_estimates,
)
from census.multitest import adjust_pvalues, batch_z_test
from census.aggregate import NonAdditiveError, aggregate_geographies, aggregate_then_derive
from census.replicate import (
    VREDownloadError,
    fetch_vre,
    read_vre_csv,
    replicate_apply,
    replicate_difference,
    replicate_proportion,
    replicate_ratio,
    replicate_sum,
    verify_published_moe,
    vre_availability,
    vre_available,
    vre_tables,
)
from census.medians import (
    distribution_brackets,
    median_for_group,
    median_for_rows,
    median_from_distribution,
    median_moe_approximate,
    median_moe_replicates,
)
from census.geo_crosswalk import apportion, harmonize_panel, tract_crosswalk, tract_vintage

from census.results import CorrelationResult, ModelResult, SmallAreaResult
from census.analysis import analysis_frame, attach_replicates, coverage_report
from census.models import fit_replicate, regression_calibration, reliability_screen, simex
from census.sae import fay_herriot, fh_regression
from census.planning import aggregation_forecast, minimum_detectable_difference, reliability_profile
from census.trends import linear_trend_test, overlap_design_check
from census.correlation import correlation_matrix
from census.report_tables import results_bundle
from census.manifest import create_manifest, label_exploratory, manifest_hash, methods_section, run_manifest, validate_manifest
from census.models import INTERVAL_TARGETS
from census.boundaries import equal_area_crs, get_boundaries, join_boundaries, write_geo
from census.maps import choropleth, class_separability, significance_map, uncertainty_map
from census.spatial import local_morans, morans_i, spatial_regression, spatial_weights
from census.sae_spatial import spatial_fay_herriot
from census.block_weights import tract_weights
from census.custom_areas import aggregate_custom_areas, allocate_units, read_areas
from census.errata import errata_for, errata_summary
from census.pums import crosstab, fetch_pums, pums_regression, read_pums_csv, tabulate
from census.multilevel import fit_multilevel
from census.did import difference_in_differences
from census.rpp import adjust_for_place, adjust_for_place_and_time
from census.web_maps import web_choropleth
from census.api import PRODUCTS, dataset_for_product, dataset_product

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
    "load_table_catalog",
    "default_metadata_file",
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
    "home_value_advisory",
    "DEFAULT_PRICE_INDEX",
    "PRICE_INDEXES",
    "PriceIndexUnavailable",
    "BundledDataIntegrityError",
    "load_price_index",
    "price_index_info",
    # metadata
    "archived_vintages",
    "available_vintages",
    "describe_variable",
    "latest_vintage",
    "table_variables",
    "vintage_groups",
    "vintage_variables",
    # concordance
    "Concordance",
    "apply_overrides",
    "build_concordance",
    "build_evidence",
    "canonical_label",
    "save_evidence",
    "validate_all_crosswalks",
    "validate_crosswalk_row",
    "continuity_check",
    "errors",
    # panel
    "build_tidy_panel",
    "generate_codebook",
    "make_slug_map",
    "export_parquet",
    # pipeline
    "OUTPUT_FORMATS",
    "CONCORDANCE_MODES",
    "apply_concordance",
    "plan_extraction",
    "public_plan",
    "Entity",
    "ResearchOptions",
    "build_entities",
    "prepare_frame",
    "resolve_metadata",
    "run_batch",
    # pipeline: nationwide and replay
    "REPLAY_SCHEMA_VERSION",
    "diff_frames",
    "expand_states",
    "file_sha256",
    "is_nationwide",
    "replay_extraction",
    # price indexes
    "C_CPI_U",
    "provisional_advisory",
    "provisional_years",
    # concordance rules
    "RULE_COLUMNS",
    "build_rule_evidence",
    "load_rules",
    "rules_path",
    "validate_rule_row",
    "draft_rule_override",
    "record_rule_review",
    # variance and derived estimates
    "DerivedEstimate",
    "REPLICATE_COLUMNS",
    "REPLICATE_COUNT",
    "Z90",
    "derived_from_replicates",
    "moe_to_se",
    "replicate_moe",
    "replicate_se",
    "replicate_variance",
    "se_to_moe",
    "METHOD_SCALE",
    "derive_columns",
    "difference",
    "percent_change",
    "product",
    "proportion",
    "ratio",
    "scale",
    "subset_relationship",
    "sum_estimates",
    # multiple comparisons
    "adjust_pvalues",
    "batch_z_test",
    # aggregation
    "NonAdditiveError",
    "aggregate_geographies",
    "aggregate_then_derive",
    # replicate tables
    "VREDownloadError",
    "fetch_vre",
    "read_vre_csv",
    "replicate_apply",
    "replicate_difference",
    "replicate_proportion",
    "replicate_ratio",
    "replicate_sum",
    "verify_published_moe",
    "vre_availability",
    "vre_available",
    "vre_tables",
    # medians
    "distribution_brackets",
    "median_for_group",
    "median_for_rows",
    "median_from_distribution",
    "median_moe_approximate",
    "median_moe_replicates",
    # tract crosswalk
    "apportion",
    "harmonize_panel",
    "tract_crosswalk",
    "tract_vintage",
    # Round 8: inference
    "CorrelationResult",
    "ModelResult",
    "SmallAreaResult",
    "analysis_frame",
    "attach_replicates",
    "coverage_report",
    "fit_replicate",
    "regression_calibration",
    "reliability_screen",
    "simex",
    "fay_herriot",
    "fh_regression",
    "aggregation_forecast",
    "minimum_detectable_difference",
    "reliability_profile",
    "linear_trend_test",
    "overlap_design_check",
    "correlation_matrix",
    "results_bundle",
    "create_manifest",
    "label_exploratory",
    "manifest_hash",
    "methods_section",
    "run_manifest",
    "validate_manifest",
    "INTERVAL_TARGETS",
    # Round 9: geography, maps, spatial
    "equal_area_crs",
    "get_boundaries",
    "join_boundaries",
    "write_geo",
    "choropleth",
    "class_separability",
    "significance_map",
    "uncertainty_map",
    "local_morans",
    "morans_i",
    "spatial_regression",
    "spatial_weights",
    "spatial_fay_herriot",
    "tract_weights",
    "aggregate_custom_areas",
    "allocate_units",
    "read_areas",
    "errata_for",
    "errata_summary",
    # Round 10: microdata, 1-year, designs, places
    "crosstab",
    "fetch_pums",
    "pums_regression",
    "read_pums_csv",
    "tabulate",
    "fit_multilevel",
    "difference_in_differences",
    "adjust_for_place",
    "adjust_for_place_and_time",
    "web_choropleth",
    "PRODUCTS",
    "dataset_for_product",
    "dataset_product",
]
