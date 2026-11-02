# SPDX-License-Identifier: AGPL-3.0-or-later
"""Result types shared by the Round 8 inference modules (PLAN_ROUND_8.md).

Models, small-area estimation, correlations, figures, tables, manifests, and
the MCP/CLI layer all exchange these objects, so they stay small and plain:
pandas frames for tabular parts, dicts for everything else, and ``to_dict()``
for JSON-safe output.

Analysis-frame convention (census.analysis): one row per unit (GEOID, and
Year for panels); for each analysis variable ``v``: ``v`` (estimate),
``v_se``, ``v_cv`` (percent), ``v_method``. Replicates live in a companion long
frame with columns ``GEOID`` (+ ``Year``), ``variable`` (= ``v``), and
``census.variance.REPLICATE_COLUMNS``.

Uncertainty sources recorded on results: ``"model"`` (usual model-based SE),
``"sampling_replicate"`` (ACS sampling error from 80 replicates),
``"sampling_model"`` (ACS sampling error via a measurement-error model).
"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

SOURCE_MODEL = "model"
SOURCE_SAMPLING_REPLICATE = "sampling_replicate"
SOURCE_SAMPLING_MODEL = "sampling_model"

COEFFICIENT_COLUMNS = [
    "term", "estimate", "se_model", "se_sampling", "se_total",
    "ci_lower", "ci_upper", "p_value",
]
SMALL_AREA_COLUMNS = [
    "GEOID", "direct", "direct_se", "synthetic", "eblup", "mse", "rmse",
    "ci_lower", "ci_upper", "gamma",
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return [_jsonable(r) for r in value.to_dict(orient="records")]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


@dataclass
class ModelResult:
    """A fitted model. ``coefficients`` has COEFFICIENT_COLUMNS (plus extras)."""

    model_type: str
    formula: str
    n_obs: int
    coefficients: pd.DataFrame
    method: str
    uncertainty_sources: List[str]
    confidence_level: float = 0.90
    notes: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.model_type} '{self.formula}', n={self.n_obs}, method={self.method}, "
            f"SEs include: {', '.join(self.uncertainty_sources)}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable({
            "model_type": self.model_type, "formula": self.formula, "n_obs": self.n_obs,
            "coefficients": self.coefficients, "method": self.method,
            "uncertainty_sources": self.uncertainty_sources,
            "confidence_level": self.confidence_level, "notes": self.notes,
            "diagnostics": self.diagnostics, "provenance": self.provenance,
            "summary": self.summary(),
        })


@dataclass
class SmallAreaResult:
    """Fay-Herriot results. ``estimates`` has SMALL_AREA_COLUMNS."""

    estimates: pd.DataFrame
    sigma2_u: float
    coefficients: pd.DataFrame
    method: str
    variance_method: str
    mse_method: str
    confidence_level: float = 0.90
    notes: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass
class CorrelationResult:
    """Square frames indexed and columned by variable name."""

    matrix: pd.DataFrame
    n: pd.DataFrame
    p_values: pd.DataFrame
    ci_lower: pd.DataFrame
    ci_upper: pd.DataFrame
    method: str
    se_replicate: Optional[pd.DataFrame] = None
    p_adjusted: Optional[pd.DataFrame] = None
    disattenuated: Optional[pd.DataFrame] = None
    confidence_level: float = 0.90
    notes: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable({k: (v.reset_index() if isinstance(v, pd.DataFrame) else v)
                          for k, v in self.__dict__.items()})
