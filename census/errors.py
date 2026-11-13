# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured error codes shared by the library, MCP server, and CLI.

Every failure result has ``success: False``, an ``error_code`` from this list,
and a human-readable ``error``. Agents should branch on ``error_code``.
"""

from typing import Any, Dict

INVALID_INPUT = "INVALID_INPUT"
INVALID_GEO_LEVEL = "INVALID_GEO_LEVEL"
INVALID_OUTPUT_FORMAT = "INVALID_OUTPUT_FORMAT"
INVALID_DATASET = "INVALID_DATASET"
INVALID_VARIABLES = "INVALID_VARIABLES"
VINTAGE_UNAVAILABLE = "VINTAGE_UNAVAILABLE"
PRICE_INDEX_UNAVAILABLE = "PRICE_INDEX_UNAVAILABLE"
CONCORDANCE_REVIEW_REQUIRED = "CONCORDANCE_REVIEW_REQUIRED"
UNVERIFIED_CONCORDANCE = "UNVERIFIED_CONCORDANCE"
METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
API_HTTP_ERROR = "API_HTTP_ERROR"
NO_DATA = "NO_DATA"
BUNDLED_DATA_INTEGRITY = "BUNDLED_DATA_INTEGRITY"
PROPOSITION_MISMATCH = "PROPOSITION_MISMATCH"
FORMALIZATION_REVIEW_REQUIRED = "FORMALIZATION_REVIEW_REQUIRED"
VERIFICATION_SCHEMA = "VERIFICATION_SCHEMA"
VERIFICATION_LIMIT = "VERIFICATION_LIMIT"
EVIDENCE_INVALID = "EVIDENCE_INVALID"

ERROR_CODES: Dict[str, str] = {
    PROPOSITION_MISMATCH: "Evidence/request identity differs from the operator-pinned proposition.",
    FORMALIZATION_REVIEW_REQUIRED: "A matching supervised local formalization review is required.",
    VERIFICATION_SCHEMA: "The restricted exact-mathematics record is malformed or unsupported syntax.",
    VERIFICATION_LIMIT: "A bounded exact-mathematics resource limit was exceeded.",
    EVIDENCE_INVALID: "Mathematical evidence could not be replayed or imported safely.",
    INVALID_INPUT: "An argument is missing or malformed.",
    INVALID_GEO_LEVEL: "geo_level is not one of us, state, county, tract, place, zcta, msa, or a required code is missing.",
    INVALID_OUTPUT_FORMAT: "output_format is not supported.",
    INVALID_DATASET: "dataset is not one of acs5, profile, subject, cprofile.",
    INVALID_VARIABLES: "One or more requested variables do not exist in any requested vintage of the table.",
    VINTAGE_UNAVAILABLE: "A requested year is not published for the dataset.",
    PRICE_INDEX_UNAVAILABLE: "The price index, or a needed year of it, is not available locally.",
    CONCORDANCE_REVIEW_REQUIRED: "Selected variables have unresolved cross-vintage issues; review them (check_variable_concordance) or narrow the selection.",
    UNVERIFIED_CONCORDANCE: "concordance_mode='code' across several years needs allow_unverified_concordance=True.",
    METADATA_UNAVAILABLE: "Variable metadata for a vintage could not be loaded.",
    API_HTTP_ERROR: "The Census API request failed.",
    NO_DATA: "The Census API returned no rows.",
    BUNDLED_DATA_INTEGRITY: "A bundled price table (price index or Regional Price Parity CSV) does not match the "
                            "checksum shipped with it; restore or regenerate it before adjusting dollars.",
}


def error_result(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """Standard failure payload."""
    return {"success": False, "error_code": code, "error": message, **extra}
