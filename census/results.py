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

# Replicate completeness policy (PLAN_ROUND_11 Q2/D7): "strict" (confirmatory,
# used by census.manifest.run_manifest) refuses a sampling SE built from
# incomplete replicates; "flag" (exploratory default) keeps the estimate but
# records diagnostics["replicates_incomplete"] and an understatement note.
REPLICATE_POLICIES = ("flag", "strict")
REPLICATES_UNDERSTATED = "sampling SE may be understated"


def check_replicate_policy(policy: str) -> str:
    if policy not in REPLICATE_POLICIES:
        raise ValueError(f"replicate_policy must be one of {REPLICATE_POLICIES}, got {policy!r}")
    return policy


def stochastic_variables(frame: Optional[pd.DataFrame], variables) -> List[str]:
    """Variables with ACS sampling error: a ``<v>_se`` column with a positive finite value."""
    if frame is None:
        return []
    out = []
    for v in variables:
        c = f"{v}_se"
        if c in frame.columns:
            s = pd.to_numeric(frame[c], errors="coerce").to_numpy(float)
            if (np.isfinite(s) & (s > 0)).any():
                out.append(v)
    return out


def replicate_completeness(policy: str, *, failed_refits: int = 0, rows_held_fixed: Optional[Dict[str, int]] = None,
                           variables_without_replicates=(), context: str = "replicate refits",
                           diagnostics: Optional[Dict[str, Any]] = None,
                           notes: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Apply ``replicate_policy`` to incomplete replicates; return the incompleteness record or None.

    ``strict`` raises ValueError when any refit failed, any row was held at its
    full-sample value, or any stochastic variable had no replicates. ``flag``
    stores the record in ``diagnostics["replicates_incomplete"]`` and appends a
    note to ``notes``.
    """
    check_replicate_policy(policy)
    rows = {str(k): int(v) for k, v in (rows_held_fixed or {}).items() if int(v)}
    vars_ = sorted(set(variables_without_replicates))
    failed = int(failed_refits)
    if not (failed or rows or vars_):
        return None
    info = {"failed_refits": failed, "rows_held_fixed": rows, "variables_without_replicates": vars_}
    parts = ([f"{failed} replicate refit(s) failed" if failed else ""]
             + [f"rows without replicates held fixed: {rows}" if rows else ""]
             + [f"stochastic variable(s) without replicates: {vars_}" if vars_ else ""])
    detail = "; ".join(p for p in parts if p)
    if policy == "strict":
        raise ValueError(f"{context}: replicate_policy='strict' requires complete replicates ({detail}); the "
                         "sampling SE would be understated. Supply complete replicates or use "
                         "replicate_policy='flag' for exploratory work")
    info["note"] = f"{REPLICATES_UNDERSTATED} ({detail})"
    if diagnostics is not None:
        diagnostics["replicates_incomplete"] = info
    if notes is not None:
        notes.append(f"REPLICATES INCOMPLETE: {info['note']}")
    return info


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
