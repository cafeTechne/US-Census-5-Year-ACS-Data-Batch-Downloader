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


class IncompleteReplicatesError(ValueError):
    """replicate_policy="strict" refused a sampling SE built from incomplete replicates."""


def replicate_completeness(policy: str, *, failed_refits: int = 0, rows_held_fixed: Optional[Dict[str, int]] = None,
                           variables_without_replicates=(), context: str = "replicate refits",
                           diagnostics: Optional[Dict[str, Any]] = None,
                           notes: Optional[List[str]] = None,
                           rows_label: str = "rows without replicates held fixed") -> Optional[Dict[str, Any]]:
    """Apply ``replicate_policy`` to incomplete replicates; return the incompleteness record or None.

    ``strict`` raises IncompleteReplicatesError (a ValueError) when any refit
    failed, any row was held at its full-sample value (or, per
    ``rows_label``, dropped), or any stochastic variable had no replicates.
    ``flag`` stores the record in ``diagnostics["replicates_incomplete"]`` and
    appends one combined "REPLICATES INCOMPLETE" note to ``notes``.
    """
    check_replicate_policy(policy)
    rows = {str(k): int(v) for k, v in (rows_held_fixed or {}).items() if int(v)}
    vars_ = sorted(set(variables_without_replicates))
    failed = int(failed_refits)
    if not (failed or rows or vars_):
        return None
    info = {"failed_refits": failed, "rows_held_fixed": rows, "variables_without_replicates": vars_}
    parts = ([f"{failed} replicate refit(s) failed" if failed else ""]
             + [f"{rows_label}: {rows}" if rows else ""]
             + [f"stochastic variable(s) without replicates: {vars_}" if vars_ else ""])
    detail = "; ".join(p for p in parts if p)
    if policy == "strict":
        raise IncompleteReplicatesError(
            f"{context}: replicate_policy='strict' requires complete replicates ({detail}); the "
            "sampling SE would be understated. Supply complete replicates or use "
            "replicate_policy='flag' for exploratory work")
    info["note"] = f"{REPLICATES_UNDERSTATED} ({detail})"
    if diagnostics is not None:
        diagnostics["replicates_incomplete"] = info
    if notes is not None:
        notes.append(f"REPLICATES INCOMPLETE: {info['note']}")
    return info


def precheck_replicates(policy: str, *, rows_held_fixed: Optional[Dict[str, int]] = None,
                        variables_without_replicates=(), context: str = "replicate refits",
                        rows_label: str = "rows without replicates held fixed") -> None:
    """Under ``strict``, refuse missing replicate rows/variables before any refit is run."""
    if check_replicate_policy(policy) == "strict":
        replicate_completeness(policy, rows_held_fixed=rows_held_fixed,
                               variables_without_replicates=variables_without_replicates, context=context,
                               rows_label=rows_label)


def check_refit_count(failed: int, n_ok: int, max_failed: int, context: str = "replicate refits") -> None:
    """After replicate_completeness: refuse when every refit failed or more than ``max_failed`` did."""
    if n_ok == 0:
        raise RuntimeError(f"{context}: all {int(failed)} replicate refits failed; se_sampling is unavailable "
                           "(check the model specification and the replicate inputs)")
    if failed > max_failed:
        raise RuntimeError(f"{context}: {int(failed)} of {int(failed) + int(n_ok)} replicate refits failed "
                           f"(max_failed_replicates={max_failed})")


COEFFICIENT_COLUMNS = [
    "term", "estimate", "se_model", "se_sampling", "se_total",
    "ci_lower", "ci_upper", "p_value",
]
SMALL_AREA_COLUMNS = [
    "GEOID", "direct", "direct_se", "synthetic", "eblup", "mse", "rmse",
    "ci_lower", "ci_upper", "gamma",
]


# Interval targets (census.models.INTERVAL_TARGETS) -> the SE column their
# intervals and p-values use; results without a target (regression
# calibration, SIMEX, Fay-Herriot regression) use se_total. This is the single
# mapping every consumer (tables, figures, methods text, workflows) reads.
TARGET_SE = {"population": "se_model", "these_areas": "se_sampling", "conservative": "se_total"}
_ESTIMAND = {
    "population": "the population (superpopulation) association",
    "these_areas": "the association among these areas in this period (finite population)",
    "conservative": "the population association with conservative intervals (se_total double-counts outcome "
                    "sampling error)",
}
# PUMS models are fitted to weighted person or housing-unit records, not areas
_PUMS_ESTIMAND = {
    "population": "the weighted population (superpopulation) association among persons or housing units",
    "these_areas": "the weighted association among the surveyed population (persons or housing units) "
                   "in this period (finite population)",
    "conservative": "the weighted population association among persons or housing units with conservative "
                    "intervals (se_total double-counts outcome sampling error)",
}
DID_CAUSAL_CAVEAT = ("causal interpretation requires the DiD identifying assumptions (parallel trends, "
                     "no interference between units, no anticipation)")
_CORRECTED_ESTIMAND = {
    "regression_calibration": "the association corrected for predictor measurement error",
    "simex": "the association corrected for predictor measurement error (SIMEX, exploratory only)",
}


def interval_description(result: Any) -> Dict[str, Any]:
    """What a ModelResult's intervals mean, read from its existing fields.

    Returns ``basis_column`` (the SE behind intervals and p-values),
    ``target``, ``level``, ``distribution`` ("t" when the coefficient table
    carries a finite ``df`` column, else "normal"), ``df`` ({term: df} or
    None), ``estimand`` (always an association, not a causal effect),
    ``undefined_terms`` (terms whose basis SE is undefined: no interval,
    never significant), ``replicates_incomplete`` and a one-line ``text``.
    """
    diag = getattr(result, "diagnostics", None) or {}
    target = diag.get("interval_target")
    basis = TARGET_SE.get(target, "se_total")
    level = float(getattr(result, "confidence_level", 0.90))
    coefs = getattr(result, "coefficients", None)
    df = None
    undefined: List[str] = []
    if isinstance(coefs, pd.DataFrame) and len(coefs):
        terms = coefs["term"].astype(str).tolist()
        if "df" in coefs.columns:
            d = pd.to_numeric(coefs["df"], errors="coerce").to_numpy(float)
            if np.isfinite(d).any():
                df = {t: (float(v) if np.isfinite(v) else None) for t, v in zip(terms, d)}
        if basis in coefs.columns:
            se = pd.to_numeric(coefs[basis], errors="coerce").to_numpy(float)
            ok = np.isfinite(se)
            for c in ("ci_lower", "ci_upper"):
                if c in coefs.columns:
                    ok &= np.isfinite(pd.to_numeric(coefs[c], errors="coerce").to_numpy(float))
            undefined = [t for t, v in zip(terms, ok) if not v]
    sources = list(getattr(result, "uncertainty_sources", None) or [])
    # se_model carries model-based sampling error (known variances) but never replicate refits
    basis_sources = {"se_model": [x for x in sources if x != SOURCE_SAMPLING_REPLICATE],
                     "se_sampling": [x for x in sources if x == SOURCE_SAMPLING_REPLICATE]}.get(basis, sources)
    distribution = "t" if df else "normal"
    estimand = _estimand_text(result, target)
    rep = (diag.get("replicates_incomplete")
           or (getattr(result, "provenance", None) or {}).get("replicates_incomplete"))
    dist_text = "t with per-term df" if df else "normal"
    text = (f"{level * 100:g}% {dist_text} intervals and p-values from {basis}"
            + (f" (includes {', '.join(basis_sources)})" if basis_sources else "")
            + (f", {target} target" if target else "") + f"; estimand: {estimand}"
            + (f"; undefined uncertainty (no interval) for {undefined}" if undefined else "")
            + (f"; REPLICATES INCOMPLETE: {rep['note']}" if rep else ""))
    return {"target": target, "basis_column": basis, "level": level, "distribution": distribution, "df": df,
            "basis_sources": basis_sources,
            "estimand": estimand, "undefined_terms": undefined, "replicates_incomplete": bool(rep), "text": text}


def _estimand_text(result: Any, target: Optional[str]) -> str:
    """Method-aware estimand: PUMS (weighted unit level), DiD (contrast), else area level."""
    method = str(getattr(result, "method", ""))
    kind = str(getattr(result, "model_type", "") or "").lower()
    outcome = _outcome_of(getattr(result, "formula", ""))
    if kind.startswith("did") or method.lower().startswith("did"):
        return (f"the difference-in-differences contrast in {outcome} (treated x post); "
                f"{DID_CAUSAL_CAVEAT}; without them it is a descriptive contrast"
                + (f" ({target} target)" if target else ""))
    if kind.startswith("pums") or method.lower().startswith("pums"):
        what = _PUMS_ESTIMAND.get(target) or _CORRECTED_ESTIMAND.get(method, "the weighted unit-level association")
        return f"{what} of {outcome} with the predictors; an association, not a causal effect"
    what = _ESTIMAND.get(target) or _CORRECTED_ESTIMAND.get(method, "the association")
    return (f"{what} of {outcome} with the predictors among areas (area level, not individuals); "
            "an association, not a causal effect")


def interval_row(info: Dict[str, Any], term: Any, row: Any = None) -> Dict[str, Any]:
    """Per-term interval fields shared by tables and figures, from :func:`interval_description`.

    ``row`` (a coefficient row, mapping-like) supplies ``se_reported`` (the
    basis SE); without it ``se_reported`` is NaN.
    """
    t = str(term)
    basis = info["basis_column"]
    se = math.nan
    if row is not None:
        try:
            v = row.get(basis, math.nan)
            se = math.nan if v is None else v
        except AttributeError:
            pass
    return {"se_basis": basis, "se_reported": se, "distribution": info["distribution"],
            "df": (info["df"] or {}).get(t, math.nan) if info["df"] else math.nan,
            "interval_defined": t not in info["undefined_terms"]}


def _outcome_of(formula: str) -> str:
    left = str(formula).split("~", 1)[0].strip() if "~" in str(formula) else ""
    return left or "the outcome"


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

    def summary(self, info: Optional[Dict[str, Any]] = None) -> str:
        """One line; includes the REPLICATES INCOMPLETE note (from the interval text) at most once."""
        info = info or interval_description(self)
        return (
            f"{self.model_type} '{self.formula}', n={self.n_obs}, method={self.method}, "
            f"SEs include: {', '.join(self.uncertainty_sources)}; {info['text']}"
        )

    def to_dict(self) -> Dict[str, Any]:
        info = interval_description(self)
        return _jsonable({
            "model_type": self.model_type, "formula": self.formula, "n_obs": self.n_obs,
            "coefficients": self.coefficients, "method": self.method,
            "uncertainty_sources": self.uncertainty_sources,
            "confidence_level": self.confidence_level, "notes": self.notes,
            "diagnostics": self.diagnostics, "provenance": self.provenance,
            "interval_se_column": info["basis_column"],
            "interval": info,
            "summary": self.summary(info),
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
