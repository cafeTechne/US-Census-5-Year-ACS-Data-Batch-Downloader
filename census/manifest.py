# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-registered analysis manifests (PLAN_ROUND_8 workstream D).

A manifest is a JSON document that fixes an analysis before any data are
pulled. Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "title": "Income and poverty in NY tracts",
      "authors": ["human:jdoe", "agent:claude"],           # identity convention of overrides
      "created": "2026-11-02T12:00:00Z",                    # ISO 8601
      "releases": [2019],                                   # ACS 5-year end years
      "geography": {"level": "tract", "state": "36", "codes": ["061"]},
      "variables": [                                        # YEAR:CODE or concept IDs
        {"name": "income", "ref": "2019:B19013_001"},
        {"name": "pop", "ref": "c_0123456789ab", "table": "B01003"}
      ],
      "aggregation": {"mapping": {"<GEOID>": "<group>"}},    # optional
      "derivations": [{"name": "rate", "op": "proportion", "num": "a", "den": "b"}],
      "exclusions": [{"rule": "cv_screen", "threshold": 30, "variables": ["income"],
                      "action": "flag" | "exclude"},
                     {"rule": "geoid_exclude", "geoids": ["..."]}],
      "missing": "complete_case",
      "confidence_level": 0.90,
      "correction": {"method": "holm", "family": "all" | "by_family"},
      "analyses": [{"id": "m1", "method": "fit_replicate", "formula": "income ~ pop",
                    "args": {"model": "ols"}}],
      "hypotheses": [{"id": "H1", "statement": "...", "analysis": "m1",
                      "statistic": "coefficient", "term": "pop",
                      "direction": "two-sided" | "greater" | "less", "family": "primary"}],
      "data_source": {"synthetic": true,                    # optional; default: real Census API data
                      "description": "...", "generator": "..."}
    }

``data_source`` (optional) declares where the input data come from. When
``synthetic`` is true (or :func:`run_manifest` is given a client with a true
``synthetic`` attribute) the citation and methods text say the data are
synthetic and never claim retrieval from the Census Data API.

Run status (``analysis_status`` in results, ``results/run.json``,
``bundle.json`` provenance and ``methods.md``):

- ``"confirmatory"`` (:data:`STATUS_CONFIRMATORY`): the pre-registered
  analysis ran as registered, with no blocking deviation.
- ``"confirmatory_incomplete"`` (:data:`STATUS_CONFIRMATORY_INCOMPLETE`): at
  least one blocking deviation (:data:`BLOCKING_DEVIATIONS`, e.g. a hypothesis
  not tested, an analysis failed, replicates incomplete, an extraction
  failure); the pre-registered analysis did not run as registered and its
  results are not confirmatory.
- ``"exploratory"``: anything run outside a manifest (:func:`label_exploratory`).

Supported analysis methods are :data:`ANALYSIS_METHODS`; hypothesis
statistics are :data:`STATISTICS`. :func:`create_manifest` validates (every
variable resolves in the offline metadata archive through the concordance,
every method is supported) and returns the SHA-256 of the canonical JSON
(:func:`manifest_hash`). :func:`run_manifest` executes it exactly and writes
the hash into every output; deviations are recorded, never substituted
silently. Anything run outside a manifest should be tagged with
:func:`label_exploratory`.
"""

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import numpy as np
import pandas as pd

from .results import IncompleteReplicatesError, ModelResult, interval_description

SCHEMA_VERSION = 1
AUTHOR_RE = re.compile(r"^(human|agent):\S+$")
CONCEPT_RE = re.compile(r"^c_[0-9a-f]{12}$")
YEAR_CODE_RE = re.compile(r"^(\d{4}):([A-Z]+\d+[A-Z]*_\d+[A-Z]*)$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DIRECTIONS = ("two-sided", "greater", "less")
STATUS_CONFIRMATORY = "confirmatory"
STATUS_CONFIRMATORY_INCOMPLETE = "confirmatory_incomplete"
INCOMPLETE_STATUS_TEXT = "pre-registered analysis did not run as registered; results are not confirmatory"
# deviations meaning the pre-registered analysis did not run as registered
BLOCKING_DEVIATIONS = ("blocked_variable", "extraction_failed", "variable_missing_from_extraction",
                       "aggregation_failed", "analysis_frame_failed", "analysis_not_run",
                       "replicates_unavailable", "analysis_failed", "replicates_incomplete",
                       "hypothesis_not_tested")
DATA_SOURCE_KEYS = ("synthetic", "description", "generator")
STATUS_EXPLORATORY = "exploratory"

# method -> (required args, statistics it supports)
ANALYSIS_METHODS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "fit_replicate": (("formula",), ("coefficient",)),
    "regression_calibration": (("formula", "error_covariates"), ("coefficient",)),
    "simex": (("formula", "error_covariates"), ("coefficient",)),
    "fh_regression": (("formula",), ("coefficient",)),
    "fay_herriot": (("formula",), ("coefficient",)),
    "correlation_matrix": (("variables",), ("correlation",)),
    "linear_trend_test": (("variable", "geoid"), ("trend_slope",)),
    "batch_z_test": (("variable_1", "variable_2"), ("z_difference",)),
}
STATISTICS = ("coefficient", "correlation", "trend_slope", "z_difference")
EXCLUSION_RULES = ("cv_screen", "geoid_exclude")
FORMULA_FUNCTIONS = {"C", "I", "np", "log", "exp", "sqrt", "center", "standardize", "scale", "Q"}

REFERENCES = {
    "acs": "U.S. Census Bureau (2020). Understanding and Using American Community Survey Data: "
           "What All Data Users Need to Know (ACS General Handbook), ch. 7-8.",
    "vre": "U.S. Census Bureau. Variance Replicate Estimate Tables Documentation. "
           "https://www.census.gov/programs-surveys/acs/data/variance-tables.html",
    "design": "U.S. Census Bureau (2014). American Community Survey Design and Methodology, ch. 12-13.",
    "testing": "U.S. Census Bureau (2025). Instructions for Applying Statistical Testing to ACS Data.",
    "carroll": "Carroll, R.J., Ruppert, D., Stefanski, L.A. & Crainiceanu, C.M. (2006). Measurement "
               "Error in Nonlinear Models: A Modern Perspective, 2nd ed. Chapman & Hall/CRC.",
    "simex": "Cook, J.R. & Stefanski, L.A. (1994). JASA 89, 1314-1328; Stefanski, L.A. & Cook, J.R. "
             "(1995). JASA 90, 1247-1256; Devanarayan, V. & Stefanski, L.A. (2002). Stat. Prob. "
             "Letters 59, 219-225.",
    "fisher": "Fisher, R.A. (1921). On the probable error of a coefficient of correlation. Metron 1, 3-32.",
    "holm": "Holm, S. (1979). Scand. J. Statist. 6, 65-70.",
    "bh": "Benjamini, Y. & Hochberg, Y. (1995). JRSS-B 57, 289-300.",
    "by": "Benjamini, Y. & Yekutieli, D. (2001). Ann. Statist. 29, 1165-1188.",
    "bonferroni": "Dunn, O.J. (1961). JASA 56, 52-64.",
    "statsmodels": "Seabold, S. & Perktold, J. (2010). statsmodels: Econometric and statistical "
                   "modeling with Python. Proc. 9th Python in Science Conf.",
}


# ---------------------------------------------------------------------------
# Hashing, loading
# ---------------------------------------------------------------------------


def canonical_json(manifest: Mapping[str, Any]) -> str:
    """Canonical serialization: sorted keys, no whitespace, UTF-8 text."""
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """SHA-256 (hex) of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def load_manifest(path_or_dict: Union[str, Path, Mapping[str, Any]]) -> Dict[str, Any]:
    if isinstance(path_or_dict, Mapping):
        return copy.deepcopy(dict(path_or_dict))
    with open(path_or_dict, encoding="utf-8") as f:
        return json.load(f)


def _table_of(var: Mapping[str, Any]) -> Optional[str]:
    if var.get("table"):
        return str(var["table"]).upper()
    m = YEAR_CODE_RE.match(str(var.get("ref", "")))
    return m.group(2).split("_")[0] if m else None


def _dataset_for(table: str) -> str:
    from .api import infer_dataset
    return infer_dataset(table)


def tables_and_refs(manifest: Mapping[str, Any]) -> Dict[str, List[str]]:
    """table code -> variable refs (in manifest order)."""
    out: Dict[str, List[str]] = {}
    for v in manifest.get("variables", []):
        t = _table_of(v)
        if t:
            out.setdefault(t, []).append(str(v["ref"]))
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_SPEC_KEYS = ("formula", "error_covariates", "variables", "variable", "geoid", "variable_1", "variable_2")


def _normalize_analysis(a: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the analysis with spec keys (formula, variables, ...) lifted from
    ``args`` to the top level, so either placement works the same way.
    Duplicates (a key both top-level and in args) are a validation error."""
    out = dict(a)
    args = dict(a.get("args") or {})
    for k in _SPEC_KEYS:
        if k in args and k not in out:
            out[k] = args.pop(k)
        elif k in args:
            args.pop(k)
    out["args"] = args
    return out


def _duplicate_spec_keys(a: Mapping[str, Any]) -> List[str]:
    args = a.get("args") or {}
    return [k for k in _SPEC_KEYS if k in a and k in args]


def _formula_names(formula: str) -> List[str]:
    """Identifiers in a patsy formula that are not function calls."""
    names = []
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_.]*", str(formula)):
        tail = str(formula)[m.end():].lstrip()
        if tail.startswith("(") or m.group(0) in FORMULA_FUNCTIONS or "." in m.group(0):
            continue
        names.append(m.group(0))
    return names


def _check_variables(manifest: Mapping[str, Any], errors: List[str], resolve: bool, client: Any) -> None:
    from .metadata import archived_vintages, vintage_variables
    releases = [int(r) for r in manifest.get("releases", []) if str(r).isdigit()]
    variables = manifest.get("variables") or []
    if not variables:
        errors.append("variables: at least one variable is required")
        return
    by_table: Dict[str, List[str]] = {}
    for i, v in enumerate(variables):
        if not isinstance(v, Mapping) or "name" not in v or "ref" not in v:
            errors.append(f"variables[{i}]: needs 'name' and 'ref'")
            continue
        ref = str(v["ref"])
        m = YEAR_CODE_RE.match(ref)
        if not m and not CONCEPT_RE.match(ref):
            errors.append(f"variables[{i}] {ref!r}: ref must be YEAR:CODE or a concept ID (c_ + 12 hex)")
            continue
        table = _table_of(v)
        if table is None:
            errors.append(f"variables[{i}] {ref!r}: a concept ID needs 'table'")
            continue
        dataset = _dataset_for(table)
        if m:
            year, code = int(m.group(1)), m.group(2)
            if year not in releases:
                errors.append(f"variables[{i}] {ref!r}: year {year} is not among releases {releases}")
            if resolve:
                if year not in archived_vintages(dataset):
                    errors.append(f"variables[{i}] {ref!r}: {dataset} {year} not in the offline metadata archive")
                    continue
                known, _ = vintage_variables(dataset, year)
                if not any(k == code or k.startswith(code) and k[len(code):] in ("E", "PE") for k in known):
                    errors.append(f"variables[{i}] {ref!r}: {code} does not exist in {dataset} {year}")
                    continue
        by_table.setdefault(table, []).append(ref)
    if not resolve:
        return
    from .concordance import build_concordance
    for table, refs in by_table.items():
        dataset = _dataset_for(table)
        missing = [y for y in releases if y not in archived_vintages(dataset)]
        if missing:
            errors.append(f"{table}: releases {missing} not in the offline metadata archive for {dataset}")
            continue
        try:
            conc = build_concordance(table, dataset, releases, client=client)
            _, unknown = conc.resolve_selection(refs)
        except Exception as exc:  # concordance build failures are validation errors
            errors.append(f"{table}: concordance could not be built ({exc})")
            continue
        for ref in unknown:
            errors.append(f"variable {ref!r} does not resolve in the {table} concordance for {releases}")


def validate_manifest(manifest: Mapping[str, Any], resolve: bool = True, client: Any = None) -> List[str]:
    """Return a list of errors (empty when the manifest is valid).

    ``resolve``: check every variable against the offline metadata archive
    and the concordance (no network when the archive covers the releases).
    """
    errors: List[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(manifest.get("title", "")).strip():
        errors.append("title is required")
    authors = manifest.get("authors") or []
    if not authors:
        errors.append("authors: at least one author ('human:<id>' or 'agent:<id>') is required")
    for a in authors:
        if not AUTHOR_RE.match(str(a)):
            errors.append(f"author {a!r} must look like 'human:<id>' or 'agent:<id>'")
    try:
        datetime.fromisoformat(str(manifest.get("created", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("created must be an ISO 8601 timestamp")
    releases = manifest.get("releases") or []
    if not releases or not all(isinstance(r, int) for r in releases):
        errors.append("releases must be a non-empty list of ACS 5-year end years (integers)")
    geo = manifest.get("geography") or {}
    if not geo.get("level"):
        errors.append("geography.level is required")
    else:
        try:
            from .pipeline import build_entities
            build_entities(geo["level"], geo.get("state"), geo.get("codes"))
        except Exception as exc:
            errors.append(f"geography: {exc}")
    level = manifest.get("confidence_level", 0.90)
    if not isinstance(level, (int, float)) or not 0 < float(level) < 1:
        errors.append("confidence_level must be in (0, 1), e.g. 0.90")
    corr = manifest.get("correction") or {}
    try:
        from .multitest import _method
        _method(corr.get("method", "holm"))
    except ValueError as exc:
        errors.append(f"correction: {exc}")
    if corr.get("family", "all") not in ("all", "by_family"):
        errors.append("correction.family must be 'all' or 'by_family'")
    ds = manifest.get("data_source")
    if ds is not None:
        if not isinstance(ds, Mapping):
            errors.append("data_source must be an object {synthetic, description, generator}")
        else:
            if not isinstance(ds.get("synthetic"), bool):
                errors.append("data_source.synthetic is required and must be true or false")
            for k in ("description", "generator"):
                if k in ds and not isinstance(ds[k], str):
                    errors.append(f"data_source.{k} must be a string")
            for k in sorted(set(ds) - set(DATA_SOURCE_KEYS)):
                errors.append(f"data_source: unknown key {k!r}; allowed {DATA_SOURCE_KEYS}")
    if manifest.get("missing", "complete_case") != "complete_case":
        errors.append("missing: only 'complete_case' is supported")

    _check_variables(manifest, errors, resolve, client)
    names = [str(v.get("name")) for v in manifest.get("variables", []) if isinstance(v, Mapping)]
    for d in manifest.get("derivations") or []:
        names.append(str(d.get("name")))
        for part in ("num", "den"):
            refs = d.get(part)
            for r in (refs if isinstance(refs, list) else [refs] if refs is not None else []):
                if str(r) not in names:
                    errors.append(f"derivation {d.get('name')!r}: {part} {r!r} is not an earlier variable name")
    for n in names:
        if not NAME_RE.match(n):
            errors.append(f"name {n!r} must be a Python identifier (used in formulas)")
    if len(set(names)) != len(names):
        errors.append("variable/derivation names must be unique")
    agg = manifest.get("aggregation")
    if agg is not None and not isinstance(agg.get("mapping"), Mapping):
        errors.append("aggregation.mapping must be an object GEOID -> group id")

    for i, ex in enumerate(manifest.get("exclusions") or []):
        if ex.get("rule") not in EXCLUSION_RULES:
            errors.append(f"exclusions[{i}]: rule must be one of {EXCLUSION_RULES}")
        if ex.get("rule") == "cv_screen" and ex.get("action", "flag") not in ("flag", "exclude"):
            errors.append(f"exclusions[{i}]: action must be 'flag' or 'exclude'")
        for v in ex.get("variables") or []:
            if v not in names:
                errors.append(f"exclusions[{i}]: unknown variable {v!r}")

    analyses = manifest.get("analyses") or []
    if not analyses:
        errors.append("analyses: at least one analysis is required")
    ids = {}
    for i, a in enumerate(analyses):
        for k in _duplicate_spec_keys(a):
            errors.append(f"analyses[{i}]: {k!r} given both at the top level and in args; give it once")
        a = _normalize_analysis(a)
        method = a.get("method")
        if method not in ANALYSIS_METHODS:
            errors.append(f"analyses[{i}]: unsupported method {method!r}; supported {sorted(ANALYSIS_METHODS)}")
            continue
        if a.get("id") in ids or not a.get("id"):
            errors.append(f"analyses[{i}]: id must be present and unique")
        ids[a.get("id")] = method
        for req in ANALYSIS_METHODS[method][0]:
            if req not in a:
                errors.append(f"analyses[{i}] ({method}): {req!r} is required")
        used = _formula_names(a["formula"]) if a.get("formula") else []
        for key in ("variables", "error_covariates"):
            used += list(a.get(key) or [])
        for key in ("variable", "variable_1", "variable_2"):
            if a.get(key):
                used.append(a[key])
        for u in used:
            if u not in names:
                errors.append(f"analyses[{i}] ({a.get('id')}): {u!r} is not a manifest variable")
    hyps = manifest.get("hypotheses") or []
    if not hyps:
        errors.append("hypotheses: at least one pre-registered hypothesis is required")
    hids = set()
    for i, h in enumerate(hyps):
        if not h.get("id") or h["id"] in hids:
            errors.append(f"hypotheses[{i}]: id must be present and unique")
        hids.add(h.get("id"))
        if h.get("direction", "two-sided") not in DIRECTIONS:
            errors.append(f"hypotheses[{i}]: direction must be one of {DIRECTIONS}")
        method = ids.get(h.get("analysis"))
        if method is None:
            errors.append(f"hypotheses[{i}]: analysis {h.get('analysis')!r} is not defined")
            continue
        if h.get("statistic") not in ANALYSIS_METHODS[method][1]:
            errors.append(f"hypotheses[{i}]: statistic {h.get('statistic')!r} not available from {method} "
                          f"(supported: {ANALYSIS_METHODS[method][1]})")
        need = {"coefficient": ("term",), "correlation": ("variables",), "trend_slope": (),
                "z_difference": ("geoid",)}.get(h.get("statistic"), ())
        for k in need:
            if k not in h:
                errors.append(f"hypotheses[{i}]: {k!r} is required for statistic {h.get('statistic')!r}")
    return errors


def create_manifest(
    path_or_dict: Union[str, Path, Mapping[str, Any]], path: Optional[Union[str, Path]] = None,
    client: Any = None, resolve: bool = True,
) -> Dict[str, Any]:
    """Validate a manifest and record its SHA-256 before any data are pulled.

    When valid and ``path`` is given (or the input was a dict and ``path``
    is given) the manifest is written there as pretty JSON. Commit the file
    to the repository to fix the pre-registration (PLAN_ROUND_8 decision 3).
    Returns ``{"hash", "path", "errors", "valid"}``.
    """
    manifest = load_manifest(path_or_dict)
    errors = validate_manifest(manifest, resolve=resolve, client=client)
    out_path = str(path) if path else (str(path_or_dict) if not isinstance(path_or_dict, Mapping) else None)
    if not errors and path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
    return {"hash": manifest_hash(manifest), "path": out_path, "errors": errors, "valid": not errors}


# ---------------------------------------------------------------------------
# Exploratory labelling
# ---------------------------------------------------------------------------


def label_exploratory(result: Any, reason: str = "not in a pre-registered manifest",
                      manifest_hash_value: Optional[str] = None) -> Any:
    """Tag a result (result object with ``provenance`` or a dict) as exploratory.

    Use for any analysis run outside a manifest, including on the same data
    as a manifest run (pass its hash in ``manifest_hash_value``).
    """
    tag = {"analysis_status": STATUS_EXPLORATORY, "exploratory_reason": reason,
           "related_manifest_hash": manifest_hash_value}
    if hasattr(result, "provenance") and isinstance(result.provenance, dict):
        result.provenance.update(tag)
        if hasattr(result, "notes") and isinstance(result.notes, list):
            result.notes.append(f"EXPLORATORY: {reason}")
        return result
    if isinstance(result, dict):
        result.update(tag)
        return result
    raise TypeError("label_exploratory needs a result object with .provenance or a dict")


def analysis_status(result: Any) -> str:
    prov = getattr(result, "provenance", result if isinstance(result, dict) else {})
    return prov.get("analysis_status", STATUS_EXPLORATORY) if isinstance(prov, dict) else STATUS_EXPLORATORY


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _codebook_columns(codebook: pd.DataFrame, concept_id: str) -> Optional[Tuple[str, str, str]]:
    """(estimate slug, moe slug, estimate code) for a concept in a run_batch codebook."""
    rows = codebook[codebook["Concept_ID"].astype(str) == concept_id]
    est = rows[rows["Variable_Slug"].str.endswith(("_est", "_pct")) & ~rows["Variable_Slug"].str.endswith("_moe")]
    if est.empty:
        return None
    e = est.iloc[0]
    slug = str(e["Variable_Slug"])
    moe = f"{slug}_moe" if slug.endswith("_pct") else slug[: -len("_est")] + "_moe"
    return slug, moe, str(e["Variable_Code"])


def _wide_from_panel(panel: pd.DataFrame, columns: Mapping[str, Tuple[str, str, str]]) -> pd.DataFrame:
    """Panel (slugged) -> wide frame keyed by ACS codes (``<base>E``/``<base>M``)."""
    from .stats import parse_variable_code
    out = pd.DataFrame({"GEOID": panel["geoid"].astype(str), "Year": panel["year"].astype("int64")})
    if "name" in panel.columns:
        out["NAME"] = panel["name"]
    for _, (est, moe, code) in columns.items():
        base, suffix = parse_variable_code(code) or (code.rstrip("E"), "E")
        out[f"{base}{suffix}"] = pd.to_numeric(panel[est], errors="coerce").to_numpy()
        out[f"{base}{'PM' if suffix == 'PE' else 'M'}"] = pd.to_numeric(panel[moe], errors="coerce").to_numpy()
    return out


CONFIRMATORY_REPLICATE_POLICY = "strict"
# Manifest methods that use replicates; each runs with replicate_policy="strict".
# (Spatial, multilevel, DiD and PUMS analyses are not manifest methods; if added
# they must join this set so confirmatory runs refuse incomplete replicates.)
REPLICATE_METHODS = ("fit_replicate", "correlation_matrix")


def _one_sided(p: float, estimate: float, direction: str) -> float:
    if direction == "two-sided" or p is None or not np.isfinite(p):
        return p
    agrees = (estimate > 0) if direction == "greater" else (estimate < 0)
    return p / 2 if agrees else 1 - p / 2


def _run_analysis(a: Mapping[str, Any], frame: pd.DataFrame, level: float, replicates: Any,
                  correction: str) -> Any:
    from . import correlation, models, multitest, sae, trends
    a = _normalize_analysis(a)
    method, args = a["method"], dict(a.get("args") or {})
    for k in ("error_covariates", "variables", "variable", "geoid", "variable_1", "variable_2"):
        if k in a:
            args.setdefault(k, a[k])
    if method in REPLICATE_METHODS:
        # Confirmatory runs refuse incomplete replicates (PLAN_ROUND_11 Q2/D7);
        # the failure is recorded as a "replicates_incomplete" deviation.
        args["replicate_policy"] = CONFIRMATORY_REPLICATE_POLICY
    if method == "fit_replicate":
        return models.fit_replicate(a["formula"], frame, replicates=replicates, confidence_level=level, **args)
    if method in ("regression_calibration", "simex"):
        return getattr(models, method)(a["formula"], frame, confidence_level=level, **args)
    if method in ("fay_herriot", "fh_regression"):
        y = a["formula"].split("~")[0].strip()
        args.setdefault("se_col", f"{y}_se")
        return getattr(sae, method)(a["formula"], frame, confidence_level=level, **args)
    if method == "correlation_matrix":
        return correlation.correlation_matrix(frame, args.pop("variables"), confidence_level=level,
                                              replicates=replicates, correction=correction, **args)
    if method == "linear_trend_test":
        v, g = args.pop("variable"), str(args.pop("geoid"))
        sub = frame[frame["GEOID"] == g].sort_values("Year")
        return trends.linear_trend_test(sub["Year"].tolist(), sub[v].tolist(), ses=sub[f"{v}_se"].tolist(),
                                        confidence_level=level, **args)
    if method == "batch_z_test":
        v1, v2 = args.pop("variable_1"), args.pop("variable_2")
        df = frame[["GEOID"]].copy()
        for v, tag in ((v1, "1"), (v2, "2")):
            df[f"e{tag}"] = frame[v].to_numpy(float)
            df[f"m{tag}"] = frame[f"{v}_se"].to_numpy(float) * 1.645
        out = multitest.batch_z_test(df, "e1", "m1", "e2", "m2", confidence_level=level,
                                     correction=correction, **args)
        return out
    raise ValueError(f"unsupported method {method}")


def _statistic(h: Mapping[str, Any], res: Any) -> Dict[str, Any]:
    stat = h["statistic"]
    if stat == "coefficient":
        c = res.coefficients.set_index("term")
        if h["term"] not in c.index:
            raise KeyError(f"term {h['term']!r} not in model (terms: {list(c.index)})")
        row = c.loc[h["term"]]
        from .models import interval_se_column
        se_col = interval_se_column(res)  # the SE behind p_value and the interval
        return {"estimate": float(row["estimate"]), "se": float(row[se_col]), "se_basis": se_col,
                "p_two_sided": float(row["p_value"]),
                "ci_lower": float(row["ci_lower"]), "ci_upper": float(row["ci_upper"])}
    if stat == "correlation":
        a, b = h["variables"]
        return {"estimate": float(res.matrix.loc[a, b]), "se": None, "p_two_sided": float(res.p_values.loc[a, b]),
                "ci_lower": float(res.ci_lower.loc[a, b]), "ci_upper": float(res.ci_upper.loc[a, b])}
    if stat == "trend_slope":
        return {"estimate": res["slope"], "se": res["se_slope"], "p_two_sided": res["p_value"],
                "ci_lower": res["ci_lower"], "ci_upper": res["ci_upper"]}
    if stat == "z_difference":
        row = res[res["GEOID"] == str(h["geoid"])]
        if row.empty:
            raise KeyError(f"GEOID {h['geoid']} not in the test frame")
        r = row.iloc[0]
        diff = float(r.get("difference", r["e1"] - r["e2"]))
        return {"estimate": diff, "se": None, "p_two_sided": float(r["p_value"]), "ci_lower": None, "ci_upper": None}
    raise ValueError(stat)


def _tag(result: Any, h: str, analysis_id: str, status: str = STATUS_CONFIRMATORY) -> Any:
    tag = {"analysis_status": status, "manifest_hash": h, "manifest_analysis_id": analysis_id}
    if hasattr(result, "provenance") and isinstance(result.provenance, dict):
        result.provenance.update(tag)
        return result
    if isinstance(result, pd.DataFrame):
        return {**tag, "rows": result}
    if isinstance(result, dict):
        return {**result, **tag}
    return result


def run_manifest(
    manifest: Union[str, Path, Mapping[str, Any]], output_dir: Union[str, Path], client: Any = None,
    replicates: Any = None, use_cache: bool = True, replicates_synthetic: Optional[bool] = None,
) -> Dict[str, Any]:
    """Execute a manifest exactly: plan -> extraction -> analysis frame ->
    exclusions -> analyses -> hypothesis tests with the pre-registered
    correction -> results bundle (tables, figures, methods.md).

    ``replicates``: optional pre-fetched replicate long frame (from
    :func:`census.analysis.attach_replicates`) used by ``fit_replicate`` and
    ``correlation_matrix``; without it those report model-based SEs and a
    deviation is recorded when the analysis sets ``"replicates": "required"``.
    Replicate analyses run with ``replicate_policy="strict"`` (confirmatory):
    failed refits, rows held fixed, or stochastic variables without replicates
    stop the analysis and record a ``replicates_incomplete`` deviation.
    Supplied replicates are recorded as caller-supplied (not fetched from
    Census VRE files by this function) with the SHA-256 of their canonical CSV
    (:func:`replicates_sha256`); ``replicates_synthetic`` (default: the data
    source's synthetic flag, or ``replicates.attrs["synthetic"]``) marks them
    synthetic in the methods text.
    ``analysis_status`` is ``"confirmatory"`` only without blocking deviations,
    else ``"confirmatory_incomplete"`` (see the module docstring).
    Never raises on data problems; returns ``success``, ``deviations``,
    ``hypotheses`` and the bundle manifest. The manifest hash is written into
    every output (results JSON, tables, figure sidecars, methods.md, bundle.json).
    """
    from .analysis import analysis_frame
    from .multitest import adjust_pvalues
    from .pipeline import ResearchOptions, build_entities, plan_extraction, run_batch
    from .report_tables import data_citation

    m = load_manifest(manifest)
    errors = validate_manifest(m, resolve=True, client=client)
    h = manifest_hash(m)
    out = Path(output_dir)
    if errors:
        return {"success": False, "manifest_hash": h, "errors": errors}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                       encoding="utf-8", newline="\n")
    deviations: List[Dict[str, Any]] = []
    level = float(m.get("confidence_level", 0.90))
    corr = m.get("correction") or {}
    corr_method = corr.get("method", "holm")
    releases = [int(r) for r in m["releases"]]
    geo = m["geography"]
    entities = build_entities(geo["level"], geo.get("state"), geo.get("codes"), years=releases)
    opts = ResearchOptions(use_cache=use_cache, include_codebook=True,
                           concordance_mode=m.get("concordance_mode", "fail"))

    # 1-2. plan and extraction, one table at a time
    columns: Dict[str, Tuple[str, str, str]] = {}
    panels: List[pd.DataFrame] = []
    extraction: Dict[str, Any] = {}
    name_of = {str(v["ref"]): str(v["name"]) for v in m["variables"]}
    for table, refs in tables_and_refs(m).items():
        plan = plan_extraction(table, entities, releases, "panel", opts, variables=refs, client=client)
        plan_pub = {k: v for k, v in plan.items() if not k.startswith("_")}
        extraction[table] = {"plan": plan_pub}
        if not plan.get("success"):
            deviations.append({"kind": "blocked_variable", "table": table, "variables": refs,
                               "detail": plan.get("message") or plan.get("error_code")})
            continue
        res = run_batch(table, entities, releases, str(out / "extraction" / table), "panel", opts,
                        client=client, variables=refs)
        extraction[table]["run"] = {k: res.get(k) for k in ("success", "failed_calls", "successful_calls",
                                                             "metadata_path", "panel_path", "advisories")}
        if not res.get("success"):
            deviations.append({"kind": "extraction_failed", "table": table, "variables": refs,
                               "detail": res.get("message") or res.get("error_code")})
            continue
        if res.get("failed_calls"):
            deviations.append({"kind": "failed_calls", "table": table, "detail": res["failed_calls"]})
        if not res.get("panel_path") or not res.get("codebook_path"):
            deviations.append({"kind": "extraction_failed", "table": table, "variables": refs,
                               "detail": "no data returned (every call failed)"})
            continue
        codebook = pd.read_csv(res["codebook_path"], dtype=str)
        cid_of = {s["input"]: s["concept_id"] for s in plan.get("selection", [])}
        tcols = {}
        for ref in refs:
            cols = _codebook_columns(codebook, cid_of.get(ref, ref))
            if cols is None:
                deviations.append({"kind": "variable_missing_from_extraction", "table": table, "variable": ref})
            else:
                tcols[name_of[ref]] = cols
        panel = pd.read_csv(res["panel_path"], dtype={"geoid": str}) if str(res["panel_path"]).endswith(".csv") \
            else pd.read_parquet(res["panel_path"])
        panels.append(_wide_from_panel(panel, tcols))
        columns.update(tcols)

    # 3. analysis frame (optional aggregation first)
    frame = None
    if panels:
        wide = panels[0]
        for p in panels[1:]:
            wide = wide.merge(p.drop(columns=[c for c in ("NAME",) if c in p.columns]), on=["GEOID", "Year"],
                              how="outer")
        refs_by_name = {}
        for name, (_, _, code) in columns.items():
            refs_by_name[name] = code
        if m.get("aggregation"):
            from .aggregate import aggregate_geographies
            try:
                parts = []
                for year, sub in wide.groupby("Year"):
                    agg = aggregate_geographies(sub.drop(columns=["Year"]), m["aggregation"]["mapping"],
                                                estimate_cols=list(refs_by_name.values()))
                    agg["Year"] = year
                    parts.append(agg)
                wide = pd.concat(parts, ignore_index=True)
            except Exception as exc:
                deviations.append({"kind": "aggregation_failed", "detail": str(exc)})
                wide = None
        if wide is not None:
            try:
                frame = analysis_frame(wide, variables=refs_by_name, derive=m.get("derivations") or None)
            except Exception as exc:
                deviations.append({"kind": "analysis_frame_failed", "detail": str(exc)})

    # 4. exclusions
    exclusion_log: List[Dict[str, Any]] = []
    if frame is not None:
        from .models import reliability_screen
        for ex in m.get("exclusions") or []:
            if ex["rule"] == "geoid_exclude":
                drop = frame["GEOID"].isin([str(g) for g in ex.get("geoids", [])])
                frame = frame[~drop].reset_index(drop=True)
                exclusion_log.append({"rule": "geoid_exclude", "rows_excluded": int(drop.sum())})
            else:
                thr = float(ex.get("threshold", 30.0))
                vars_ = [v for v in ex.get("variables") or [] if f"{v}_cv" in frame.columns]
                flagged = reliability_screen(frame, vars_, threshold=thr)
                entry = {"rule": "cv_screen", "threshold": thr, "variables": vars_,
                         "action": ex.get("action", "flag"), "rows_flagged": int(flagged["GEOID"].nunique())
                         if len(flagged) else 0}
                if entry["action"] == "exclude" and len(flagged):
                    keys = [k for k in ("GEOID", "Year") if k in flagged.columns]
                    bad = frame[keys].merge(flagged[keys].drop_duplicates(), how="left", indicator=True)
                    keep = (bad["_merge"] == "left_only").to_numpy()
                    entry["rows_excluded"] = int((~keep).sum())
                    frame = frame[keep].reset_index(drop=True)
                exclusion_log.append(entry)

    # 5. analyses
    available = set(columns) | {str(d["name"]) for d in m.get("derivations") or []}
    results: Dict[str, Any] = {}
    for a in m["analyses"]:
        a = _normalize_analysis(a)
        aid = a["id"]
        needed = set(_formula_names(a.get("formula", "")))
        for k in ("variables", "error_covariates"):
            needed |= set(a.get(k) or [])
        needed |= {a[k] for k in ("variable", "variable_1", "variable_2") if a.get(k)}
        if frame is None or not needed <= available:
            deviations.append({"kind": "analysis_not_run", "analysis": aid,
                               "detail": f"unavailable variables: {sorted(needed - available)}"
                               if frame is not None else "no analysis frame"})
            continue
        if a.get("replicates") == "required" and replicates is None and a["method"] in REPLICATE_METHODS:
            deviations.append({"kind": "replicates_unavailable", "analysis": aid,
                               "detail": "manifest requires replicate SEs; none supplied, analysis not run"})
            continue
        try:
            results[aid] = _run_analysis(a, frame, level, replicates, corr_method)
        except Exception as exc:
            kind = "replicates_incomplete" if isinstance(exc, IncompleteReplicatesError) else "analysis_failed"
            deviations.append({"kind": kind, "analysis": aid, "detail": f"{type(exc).__name__}: {exc}"})

    # 6. hypotheses with the pre-registered correction
    rows = []
    for hyp in m["hypotheses"]:
        row = {"id": hyp["id"], "analysis": hyp["analysis"], "statistic": hyp["statistic"],
               "direction": hyp.get("direction", "two-sided"), "family": hyp.get("family", "primary"),
               "statement": hyp.get("statement", "")}
        res = results.get(hyp["analysis"])
        if res is None:
            row.update(status="not_tested", estimate=None, p_value=None)
            deviations.append({"kind": "hypothesis_not_tested", "hypothesis": hyp["id"],
                               "detail": f"analysis {hyp['analysis']} did not run"})
        else:
            try:
                s = _statistic(hyp, res)
                row.update(s, status="tested",
                           p_value=_one_sided(s["p_two_sided"], s["estimate"], row["direction"]))
            except Exception as exc:
                row.update(status="not_tested", estimate=None, p_value=None)
                deviations.append({"kind": "hypothesis_not_tested", "hypothesis": hyp["id"], "detail": str(exc)})
        rows.append(row)
    hyp_df = pd.DataFrame(rows)
    hyp_df["p_adjusted"] = np.nan
    groups = [hyp_df.index] if corr.get("family", "all") == "all" else \
        [g.index for _, g in hyp_df.groupby("family")]
    for idx in groups:
        p = pd.to_numeric(hyp_df.loc[idx, "p_value"], errors="coerce")
        hyp_df.loc[idx, "p_adjusted"] = np.asarray(adjust_pvalues(p.to_numpy(float), corr_method))
    hyp_df["significant"] = hyp_df["p_adjusted"] < (1 - level)
    hyp_df["manifest_hash"] = h

    # 7. bundle
    complete = not any(d["kind"] in BLOCKING_DEVIATIONS for d in deviations)
    status = STATUS_CONFIRMATORY if complete else STATUS_CONFIRMATORY_INCOMPLETE
    source = effective_data_source(m, client)
    rep_info = None
    if replicates is not None:
        if replicates_synthetic is None:
            attrs = getattr(replicates, "attrs", {}) or {}
            replicates_synthetic = bool(attrs.get("synthetic", source["synthetic"]))
        rep_info = {"source": "caller_supplied", "synthetic": bool(replicates_synthetic),
                    "rows": int(len(replicates)), "sha256": replicates_sha256(replicates)}
    tagged = {aid: _tag(r, h, aid, status) for aid, r in results.items()}
    run_info = {"manifest_hash": h, "analysis_status": status, "deviations": deviations,
                "exclusions": exclusion_log, "extraction": extraction, "n_units": None if frame is None
                else int(len(frame)), "title": m.get("title"), "data_source": source, "replicates": rep_info}
    if not complete:
        run_info["status_note"] = INCOMPLETE_STATUS_TEXT
    tables_used = sorted(tables_and_refs(m))
    citation = data_citation(releases, tables_used, synthetic=source["synthetic"],
                             description=source.get("description"))
    bundle = _write_bundle(out / "bundle", m, h, tagged, hyp_df, run_info, citation)
    return {"success": complete, "analysis_status": status,
            "manifest_hash": h, "deviations": deviations, "hypotheses": hyp_df, "results": tagged,
            "frame": frame, "bundle": bundle, "output_dir": str(out), "errors": []}


def effective_data_source(manifest: Mapping[str, Any], client: Any = None) -> Dict[str, Any]:
    """The manifest's ``data_source`` (default: not synthetic), forced synthetic
    when ``client`` carries a true ``synthetic`` attribute."""
    ds = dict(manifest.get("data_source") or {})
    ds["synthetic"] = bool(ds.get("synthetic")) or getattr(client, "synthetic", False) is True
    return ds


def replicates_sha256(replicates: pd.DataFrame) -> str:
    """SHA-256 of the canonical CSV of a replicate frame: columns sorted by
    name, rows sorted by all columns, no index, UTF-8, LF line endings."""
    df = replicates.copy()
    df.columns = [str(c) for c in df.columns]
    df = df.reindex(columns=sorted(df.columns))
    df = df.sort_values(list(df.columns), kind="mergesort").reset_index(drop=True)
    return hashlib.sha256(df.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()


def _write_bundle(folder: Path, m, h, results, hyp_df, run_info, citation) -> Dict[str, Any]:
    from . import figures as figs
    from .report_tables import ReportTable, correlation_table, regression_table
    from .results import CorrelationResult, ModelResult, SmallAreaResult
    note = f"Pre-registered analysis; manifest SHA-256 {h}."
    fig_source = f"{citation} {note}"
    tables, figures_ = {}, {}
    model_results = {aid: r for aid, r in results.items() if isinstance(r, ModelResult)}
    if model_results:
        t = regression_table(model_results, correction=(m.get("correction") or {}).get("method", "holm"))
        t.notes.append(note)
        t.data = t.data.assign(manifest_hash=h)
        tables["regression"] = t
        figures_["coefficients"] = (figs.coefficient_plot, {"results": model_results, "manifest_hash": h,
                                                            "data_source": fig_source})
    for aid, r in results.items():
        if isinstance(r, CorrelationResult):
            t = correlation_table(r)
            t.notes.append(note)
            t.data = t.data.assign(manifest_hash=h)
            tables[f"correlation_{aid}"] = t
            figures_[f"correlation_{aid}"] = (figs.correlation_heatmap, {"result": r, "manifest_hash": h,
                                                                         "data_source": fig_source})
        if isinstance(r, SmallAreaResult):
            figures_[f"shrinkage_{aid}"] = (figs.shrinkage_plot, {"result": r, "manifest_hash": h,
                                                                  "data_source": fig_source})
    cols = ["id", "statistic", "direction", "estimate", "p_value", "p_adjusted", "significant", "status"]
    show = hyp_df.reindex(columns=cols)
    tables["hypotheses"] = ReportTable(
        title="Pre-registered hypotheses", display=show.astype(object).where(show.notna(), "").astype(str),
        data=hyp_df, notes=[note, f"p-values adjusted by {(m.get('correction') or {}).get('method', 'holm')} "
                                  f"(family: {(m.get('correction') or {}).get('family', 'all')})."])
    payload = dict(results)
    payload["hypotheses"] = {"manifest_hash": h, "rows": hyp_df}
    payload["run"] = run_info
    from .report_tables import results_bundle
    return results_bundle(folder, tables=tables, figures=figures_, methods=methods_section(m, run_info),
                          results=payload, provenance={"manifest_hash": h,
                                                       "analysis_status": run_info["analysis_status"],
                                                       "deviations": len(run_info["deviations"]),
                                                       "data_source": run_info.get("data_source"),
                                                       "replicates": run_info.get("replicates")},
                          citation=citation)


# ---------------------------------------------------------------------------
# Methods text
# ---------------------------------------------------------------------------

_METHOD_TEXT = {
    "fit_replicate": ("Coefficients were estimated with {model} regression (statsmodels) on the full-sample "
                      "estimates; sampling standard errors came from refitting on each of {rep_source}, Var = 4/80 sum (b_r - b)^2, combined with the model standard error as "
                      "se_total = sqrt(se_model^2 + se_sampling^2). Reported standard errors, intervals and p-values use "
                      "the {target_se} ({target} target: {estimand}), with normal-theory intervals. Without "
                      "replicates only model standard errors are reported.", ["vre", "design", "statsmodels"]),
    "regression_calibration": ("Covariates measured with known ACS sampling error ({cov}) were corrected by "
                               "regression calibration with heteroscedastic known error variances; standard "
                               "errors (se_total, used for normal-theory intervals and p-values) by row bootstrap "
                               "re-estimating the calibration. Coefficients are {estimand}.", ["carroll"]),
    "simex": ("Covariates measured with known ACS sampling error ({cov}) were corrected by "
              "simulation-extrapolation (SIMEX, quadratic extrapolation) with the measurement-error jackknife "
              "variance (se_total, used for normal-theory intervals and p-values). Coefficients are "
              "{estimand}.", ["simex", "carroll"]),
    "fh_regression": ("The outcome's known sampling variance was modelled with an area-level Fay-Herriot model "
                      "(GLS coefficients, REML variance component).", ["fh"]),
    "fay_herriot": ("Small-area estimates are Fay-Herriot EBLUPs (REML) with Prasad-Rao/Datta-Lahiri MSE.",
                    ["fh"]),
    "correlation_matrix": ("Correlations with Fisher-z intervals{rep}.", ["fisher", "vre"]),
    "linear_trend_test": ("Linear trends across non-overlapping 5-year periods were tested by GLS with known "
                          "sampling variances.", ["testing"]),
    "batch_z_test": ("Differences were tested with the Census two-sample z test, SE = MOE/1.645.", ["testing"]),
}


def methods_section(manifest: Mapping[str, Any], results: Optional[Mapping[str, Any]] = None) -> str:
    """Markdown methods text matching what the manifest specifies (and, with
    ``results`` = the run info of :func:`run_manifest`, what actually ran)."""
    from .report_tables import data_citation
    from .sae import REFERENCES as FH_REFS
    m = manifest
    h = manifest_hash(m)
    run = results or {}
    source = run.get("data_source") or effective_data_source(m)
    rep_info = run.get("replicates")
    if rep_info:
        kind = "synthetic caller-supplied" if rep_info.get("synthetic") else "caller-supplied"
        rep_source = (f"the {kind} replicate estimates (not fetched from Census variance replicate files by "
                      f"the toolkit; canonical CSV SHA-256 `{rep_info.get('sha256')}`)")
        rep_short = f" and replicate-based standard errors from {kind} replicate estimates"
    elif results is not None:
        rep_source = "replicate estimates when supplied (none were supplied to this run)"
        rep_short = " and replicate-based standard errors"
    else:
        rep_source = "the 80 ACS replicate estimates"
        rep_short = " and replicate-based standard errors"
    level = float(m.get("confidence_level", 0.90))
    corr = m.get("correction") or {}
    cites: List[str] = ["acs"]
    lines = [f"## Methods", "", f"*Pre-registered analysis \"{m.get('title', '')}\"; manifest SHA-256 `{h}`, "
             f"created {m.get('created')} by {', '.join(m.get('authors', []))}.*", ""]
    if results is not None:
        status = results.get("analysis_status", STATUS_CONFIRMATORY)
        lines += [f"**Analysis status: {status}**"
                  + (f": {INCOMPLETE_STATUS_TEXT}." if status == STATUS_CONFIRMATORY_INCOMPLETE else "."), ""]
    lines += ["### Data", ""]
    tables = sorted(tables_and_refs(m))
    geo = m.get("geography", {})
    cite = data_citation(m.get("releases", []), tables, synthetic=source["synthetic"],
                         description=source.get("description"))
    lines.append(f"Data are {cite} Geography: {geo.get('level')}"
                 + (f" (state {geo.get('state')})" if geo.get("state") else "")
                 + (f", codes {', '.join(map(str, geo['codes']))}" if geo.get("codes") else "") + ".")
    if source["synthetic"] and source.get("generator"):
        lines.append(f"Synthetic data generator: {source['generator']}.")
    lines.append("Variables (YEAR:CODE references resolve to that vintage's concept; concept IDs follow the "
                 "toolkit's cross-vintage concordance, with blocking concordance issues stopping extraction "
                 "rather than being substituted):")
    lines += [f"- `{v['name']}`: {v['ref']}" + (f" (table {v['table']})" if v.get("table") else "")
              for v in m.get("variables", [])]
    if len(m.get("releases", [])) > 1:
        lines.append("Multiple releases were linked through the concordance; overlapping 5-year periods "
                     "share sample years and are not independent.")
    if m.get("aggregation"):
        lines.append(f"Geographies were aggregated into {len(set(m['aggregation']['mapping'].values()))} groups; "
                     "aggregate MOEs use replicate variance where available, else the ACS approximation "
                     "sqrt(sum MOE^2).")
        cites.append("vre")
    lines += ["", "### Measures and uncertainty", "",
              "Standard errors of published estimates are MOE/1.645 (ACS MOEs are 90% margins)."]
    for d in m.get("derivations") or []:
        lines.append(f"- `{d['name']}` = {d.get('op', 'auto')} of {d.get('num')}"
                     + (f" over {d['den']}" if d.get("den") is not None else "")
                     + "; MOE by the ACS approximation formulas (Handbook ch. 8).")
    for ex in m.get("exclusions") or []:
        if ex["rule"] == "cv_screen":
            lines.append(f"Observations with CV > {ex.get('threshold', 30)}% on {', '.join(ex.get('variables', []))} "
                         f"were {'excluded' if ex.get('action') == 'exclude' else 'kept and flagged'}.")
        else:
            lines.append(f"{len(ex.get('geoids', []))} pre-specified GEOIDs were excluded.")
    lines.append("Rows with missing or undefined estimates were dropped analysis by analysis (complete cases).")
    lines += ["", "### Analyses", ""]
    for a in m.get("analyses", []):
        a = _normalize_analysis(a)
        text, refs = _METHOD_TEXT[a["method"]]
        args = a.get("args") or {}
        tgt = args.get("target", "population")
        # the same interpretation contract as the fitted result (census.results.interval_description)
        probe = ModelResult(model_type="", formula=str(a.get("formula") or ""), n_obs=0,
                            coefficients=pd.DataFrame(columns=["term"]), method=a["method"], uncertainty_sources=[],
                            confidence_level=level,
                            diagnostics={"interval_target": tgt} if a["method"] == "fit_replicate" else {})
        info = interval_description(probe)
        text = text.format(model=args.get("model", "ols").upper(), target=tgt, estimand=info["estimand"],
                           target_se={"se_model": "model SE (se_model)", "se_sampling": "replicate sampling SE "
                                      "(se_sampling)", "se_total": "combined SE (se_total)"}[info["basis_column"]],
                           cov=", ".join(a.get("error_covariates") or args.get("error_covariates") or []),
                           rep=rep_short if (a.get("replicates") or rep_info) else "", rep_source=rep_source)
        target = a.get("formula") or a.get("variables") or a.get("variable")
        lines.append(f"- **{a['id']}** (`{target}`): {text}")
        cites += refs
    lines += ["", "### Hypotheses and multiple comparisons", ""]
    for hyp in m.get("hypotheses", []):
        lines.append(f"- **{hyp['id']}** ({hyp.get('direction', 'two-sided')}, {hyp['statistic']}"
                     + (f" of `{hyp['term']}`" if hyp.get("term") else "") + f"): {hyp.get('statement', '')}")
    cm = str(corr.get("method", "holm")).lower()
    lines.append(f"Intervals are {level:.0%}. p-values were adjusted with the {cm} procedure across "
                 f"{'all hypotheses' if corr.get('family', 'all') == 'all' else 'each hypothesis family'}; "
                 f"one-sided p-values are half the two-sided value in the hypothesized direction.")
    cites.append({"benjamini-hochberg": "bh", "fdr_bh": "bh", "fdr_by": "by"}.get(cm, cm))
    if results is not None:
        devs = results.get("deviations", [])
        lines += ["", "### Deviations from the manifest", ""]
        lines += [f"- {d['kind']}: " + ", ".join(f"{k}={v}" for k, v in d.items() if k != "kind")
                  for d in devs] or ["None."]
        for e in results.get("exclusions", []):
            lines.append(f"- Exclusion {e['rule']}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k != "rule"))
    lines += ["", "### References", ""]
    seen = []
    for c in cites:
        if c in seen:
            continue
        seen.append(c)
        if c == "fh":
            lines += [f"- {r}" for r in FH_REFS]
        elif c in REFERENCES:
            lines.append(f"- {REFERENCES[c]}")
    return "\n".join(lines) + "\n"
