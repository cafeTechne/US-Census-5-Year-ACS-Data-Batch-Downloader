# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-ready analysis frames with standard errors and replicates (PLAN_ROUND_8 A1-A3).

Convention (see :mod:`census.results`): one row per unit (``GEOID``, plus
``Year`` for panels); for each analysis variable ``v`` the columns ``v``
(estimate), ``v_se``, ``v_cv`` (percent) and ``v_method``. Replicates live in
a companion long frame: ``GEOID`` (+ ``Year``), ``variable``, ``estimate`` and
``rep_1 .. rep_80``.

Standard errors of published estimates are MOE / 1.645 (ACS MOEs are 90%
margins). A controlled estimate (MOE sentinel ``-555555555``) has SE 0; any
other sentinel or a negative MOE gives an undefined SE. Derived variables are
evaluated with :func:`census.derived.derive_columns` (approximate MOE,
ACS General Handbook 2020 ch. 8) or, after :func:`attach_replicates`, with the
exact successive-difference replicate variance

    Var = 4/80 * sum_r (X_r - X)^2

(ACS Variance Replicate Estimate documentation,
https://www.census.gov/programs-surveys/acs/data/variance-tables.html), with
each derivation re-evaluated on every replicate. Undefined replicate ratios
(zero replicate denominator) are set to 0 as in VRE documentation Example 7.
"""

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from .aggregate import normalize_geoid
from .derived import derive_columns, resolve_columns
from .stats import CENSUS_SENTINELS, moe_to_se, pair_estimates_and_moes, parse_variable_code
from .variance import (
    METHOD_PUBLISHED,
    METHOD_REPLICATE,
    REPLICATE_COLUMNS,
    REPLICATE_COUNT,
    Z90,
    replicate_se,
)

CONTROLLED_MOE = -555555555
UNIT_COLUMNS = ("GEOID", "Year")
ID_COLUMNS = ("GEOID", "Year", "NAME")
Source = Union[str, pd.DataFrame, Sequence[Union[str, pd.DataFrame]]]
Variables = Optional[Union[Sequence[str], Mapping[str, Any]]]


# ---------------------------------------------------------------------------
# Loading and column resolution
# ---------------------------------------------------------------------------


def _read(source: Union[str, pd.DataFrame]) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    path = str(source)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".parquet":
        return pd.read_parquet(path)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype={"GEOID": str, "GEO_ID": str})
    return pd.read_csv(path, dtype={"GEOID": str, "GEO_ID": str}, low_memory=False)


def load_source(source: Source) -> pd.DataFrame:
    """Read one or more wide/panel files or frames and stack them."""
    items = [source] if isinstance(source, (str, pd.DataFrame)) else list(source)
    if not items:
        raise ValueError("no source given")
    df = pd.concat([_read(s) for s in items], ignore_index=True, sort=False)
    if "GEOID" not in df.columns:
        if "GEO_ID" in df.columns:
            df["GEOID"] = df["GEO_ID"]
        else:
            raise ValueError("source needs a GEOID (or GEO_ID) column")
    df["GEOID"] = df["GEOID"].map(normalize_geoid)
    if "Year" in df.columns:
        df["Year"] = pd.to_numeric(df["Year"]).astype("int64")
    return df


def _resolve(df: pd.DataFrame, ref: Any) -> Tuple[str, Optional[str]]:
    """(estimate column, MOE column or None) for a variable reference.

    Accepts a code (``B17001_002E``/``..PE``), a base (``B17001_002``), a
    slugged panel stem (``<stem>`` with ``<stem>_est``/``<stem>_moe`` or
    ``<stem>_pct``/``<stem>_pct_moe``), or an explicit ``(est, moe)`` pair
    (``moe`` may be None for a variable without sampling error).
    """
    if isinstance(ref, (tuple, list)):
        est, moe = ref
        return str(est), (str(moe) if moe is not None else None)
    ref = str(ref)
    try:
        return resolve_columns(df, ref)
    except KeyError:
        pass
    for e, m in ((f"{ref}_est", f"{ref}_moe"), (f"{ref}_pct", f"{ref}_pct_moe")):
        if e in df.columns and m in df.columns:
            return e, m
    if ref in df.columns:
        return ref, None
    raise KeyError(f"no estimate/MOE columns found for {ref!r}")


def _default_name(ref: Any) -> str:
    if isinstance(ref, (tuple, list)):
        return str(ref[0])
    parsed = parse_variable_code(str(ref))
    if parsed and parsed[1] == "E":
        return parsed[0]
    if parsed and parsed[1] == "PE":
        return f"{parsed[0]}P"
    return str(ref)


def _default_variables(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, est, moe in pair_estimates_and_moes(df.columns):
        out[key] = (est, moe)
    for col in df.columns:
        col = str(col)
        if col.endswith("_pct_moe") or not col.endswith(("_est", "_pct")):
            continue
        stem = col.rsplit("_", 1)[0]
        moe = f"{stem}_moe" if col.endswith("_est") else f"{stem}_pct_moe"
        if moe in df.columns:
            out[stem if col.endswith("_est") else f"{stem}_pct"] = (col, moe)
    return out


METHOD_NOT_SAMPLE_BASED = "not_sample_based"


def is_decennial_count(column: str) -> bool:
    """True for a 2020 Census count column: raw code ``<base>N`` or panel slug ``*_count``."""
    parsed = parse_variable_code(str(column))
    if parsed:
        return parsed[1] == "N"
    return str(column).endswith("_count")


def _clean(est: pd.Series, moe: Optional[pd.Series]) -> Tuple[np.ndarray, np.ndarray]:
    sentinels = np.array(sorted(CENSUS_SENTINELS), dtype=float)
    e = pd.to_numeric(est, errors="coerce").to_numpy(float)
    e = np.where(np.isin(e, sentinels), np.nan, e)
    if moe is None:
        return e, np.zeros_like(e)
    se = moe_to_se(moe, Z90)
    se = np.where(np.isfinite(e), se, np.nan)
    return e, se


def _cv(est: np.ndarray, se: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(est != 0, se / np.abs(est) * 100.0, np.nan)


def _put(out: pd.DataFrame, name: str, est: Any, se: Any, method: Any) -> None:
    est = np.asarray(est, dtype=float)
    se = np.asarray(se, dtype=float)
    out[name] = est
    out[f"{name}_se"] = se
    out[f"{name}_cv"] = _cv(est, se)
    out[f"{name}_method"] = method


# ---------------------------------------------------------------------------
# A1. analysis_frame
# ---------------------------------------------------------------------------


def analysis_frame(
    source: Source,
    variables: Variables = None,
    derive: Optional[Sequence[Mapping[str, Any]]] = None,
    metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    keep_columns: Sequence[str] = (),
    concept_ids: Optional[Mapping[str, str]] = None,
    release: Optional[str] = None,
) -> pd.DataFrame:
    """Build a model-ready frame (one row per GEOID, or per GEOID and Year).

    ``source``: wide extraction or tidy panel file(s) (CSV/Parquet/Excel) or
    DataFrame(s). ``variables``: list of references or ``{name: reference}``
    (see :func:`_resolve`); default: every estimate/MOE pair found. ``derive``:
    a :func:`census.derived.derive_columns` spec whose ``num``/``den`` may be
    codes or analysis-variable names; each entry ``name`` becomes a variable.
    ``keep_columns`` are copied through unchanged (e.g. error-free covariates).

    Provenance: ``release`` column (``acs5_<Year>`` when Year is present, or
    the ``release`` argument), and ``frame.attrs["variables"]`` recording each
    variable's source columns, method and concept ID.

    2020 Census counts (``<base>N`` codes, ``*_count`` slugs) are not
    sample-based: their ``_se`` and ``_cv`` are NaN (undefined, never 0) and
    their method is ``not_sample_based``; release is ``dec_<Year>``, or
    ``acs5_<Year>+dec_2020`` with a note in ``frame.attrs["notes"]`` when ACS
    and decennial variables are mixed.
    """
    df = load_source(source)
    if variables is None:
        var_map: Dict[str, Any] = _default_variables(df)
    elif isinstance(variables, Mapping):
        var_map = dict(variables)
    else:
        var_map = {_default_name(r): r for r in variables}
    if not var_map and not derive:
        raise ValueError("no analysis variables found or given")

    if df.duplicated([c for c in UNIT_COLUMNS if c in df.columns]).any():
        raise ValueError("source has duplicate GEOID (and Year) rows; one row per unit is required")
    out = df[[c for c in ID_COLUMNS if c in df.columns]].copy()
    info: Dict[str, Dict[str, Any]] = {}
    work = df.copy()
    n_decennial = 0
    for name, ref in var_map.items():
        est_col, moe_col = _resolve(df, ref)
        e, se = _clean(df[est_col], df[moe_col] if moe_col else None)
        method = METHOD_PUBLISHED if moe_col else "no_sampling_error"
        if moe_col is None and is_decennial_count(est_col):
            se = np.full_like(e, np.nan)  # census count: sampling SE undefined, never 0
            method = METHOD_NOT_SAMPLE_BASED
            n_decennial += 1
        _put(out, name, e, se, method)
        info[name] = {"estimate_column": est_col, "moe_column": moe_col,
                      "method": method,
                      "concept_id": (concept_ids or {}).get(name)}
        # expose cleaned values under the analysis name for derive specs
        work[f"{name}_estimate"] = e
        work[f"{name}_moe"] = se * Z90

    for entry in derive or []:
        name = str(entry.get("name"))
        if name in out.columns:
            raise ValueError(f"derived variable {name!r} collides with an existing column")
    if derive:
        derived = derive_columns(work, derive, metadata=metadata)
        for entry in derive:
            name = str(entry["name"])
            _put(out, name, derived[f"{name}_estimate"], derived[f"{name}_se"], derived[f"{name}_method"].to_numpy())
            out[f"{name}_notes"] = derived[f"{name}_notes"].to_numpy()
            info[name] = {"derive": dict(entry), "method": "derived (census.derived, approximate MOE)",
                          "concept_id": (concept_ids or {}).get(name)}
    for col in keep_columns:
        out[col] = df[col].to_numpy()
    notes: List[str] = []
    if n_decennial and n_decennial < len(var_map):
        from .decennial import MIXED_NOTE

        notes.append(MIXED_NOTE)
    if "Year" in out.columns or release:
        if "Year" not in out.columns:
            label: Any = release
        elif n_decennial == len(var_map) and n_decennial:
            label = "dec_" + out["Year"].astype(str)
        else:
            label = "acs5_" + out["Year"].astype(str) + ("+dec_2020" if n_decennial else "")
        out.insert(len([c for c in ID_COLUMNS if c in out.columns]), "release", label)
    out = out.reset_index(drop=True)
    out.attrs["variables"] = info
    out.attrs["notes"] = notes
    out.attrs["derive"] = [dict(e) for e in (derive or [])]
    return out


def analysis_variables(frame: pd.DataFrame) -> List[str]:
    """Analysis variable names in a frame (columns ``v`` with ``v_se``)."""
    return [c for c in frame.columns if f"{c}_se" in frame.columns and not str(c).endswith("_se")]


# ---------------------------------------------------------------------------
# A2. attach_replicates
# ---------------------------------------------------------------------------


def _replicate_long(replicates: Any) -> pd.DataFrame:
    if isinstance(replicates, Mapping):
        parts = []
        for year, f in replicates.items():
            f = f.copy()
            f["Year"] = int(year)
            parts.append(f)
        reps = pd.concat(parts, ignore_index=True)
    elif isinstance(replicates, (list, tuple)):
        reps = pd.concat(list(replicates), ignore_index=True)
    else:
        reps = replicates.copy()
    missing = [c for c in ["GEOID", "variable", "estimate"] + REPLICATE_COLUMNS if c not in reps.columns]
    if missing:
        raise ValueError(f"replicate frame lacks columns {missing[:5]} (expected census.replicate format)")
    reps["GEOID"] = reps["GEOID"].astype(str).map(normalize_geoid)
    reps["variable"] = reps["variable"].astype(str).map(lambda v: _default_name(v))
    return reps


def _units(frame: pd.DataFrame, reps: pd.DataFrame) -> List[str]:
    keys = ["GEOID"]
    if "Year" in frame.columns:
        if "Year" in reps.columns:
            keys.append("Year")
        elif frame["Year"].nunique() > 1:
            raise ValueError("panel frame spans several years; pass replicates as {year: frame} or with a Year column")
    return keys


def _rep_cube(frame: pd.DataFrame, reps: pd.DataFrame, keys: List[str], code: str) -> np.ndarray:
    """(81, n_rows) values (estimate first) aligned to frame rows; NaN where absent."""
    sub = reps[reps["variable"] == code]
    if sub.duplicated(keys).any():
        raise ValueError(f"replicate frame has duplicate rows for {code}")
    idx = pd.MultiIndex.from_frame(frame[keys]) if len(keys) > 1 else pd.Index(frame["GEOID"])
    sub = sub.set_index(keys)
    # copy: under pandas 3 copy-on-write, to_numpy can return a read-only view
    vals = sub[["estimate"] + REPLICATE_COLUMNS].reindex(idx).to_numpy(float, copy=True)
    return vals.T


def _ratio_reps(num: np.ndarray, den: np.ndarray, zero_fill: bool = True) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den != 0, num / den, np.nan)
    if zero_fill:
        # VRE documentation, Example 7 (proportions/ratios). Not for percent
        # change, where a 0 ratio would read as a -100% change.
        full_ok = np.isfinite(out[0])
        bad = ~np.isfinite(out[1:]) & full_ok[None, :]
        out[1:][bad] = 0.0
    return out


def _eval_spec_replicates(entry: Mapping[str, Any], get) -> np.ndarray:
    op = str(entry.get("op", "auto")).lower()

    def total(key: str) -> np.ndarray:
        refs = entry[key] if isinstance(entry[key], (list, tuple)) else [entry[key]]
        return sum(get(str(r)) for r in refs)

    if op == "sum":
        return total("num")
    if op == "scale":
        return total("num") * float(entry["k"])
    x, y = total("num"), total("den")
    if op in ("proportion", "ratio", "auto"):
        return _ratio_reps(x, y) * (100.0 if entry.get("as_percent") else 1.0)
    if op == "difference":
        return x - y
    if op == "product":
        return x * y
    if op == "percent_change":
        return 100.0 * (_ratio_reps(x, y, zero_fill=False) - 1.0)
    raise ValueError(f"unsupported op {op!r} for replicate evaluation")


def attach_replicates(
    frame: pd.DataFrame,
    replicates: Any,
    variables: Variables = None,
    derive: Optional[Sequence[Mapping[str, Any]]] = None,
    replace_se: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Attach the 80 ACS replicate values to an analysis frame.

    ``replicates``: a :mod:`census.replicate` frame (``read_vre_csv`` /
    ``fetch_vre``), a list of them, or ``{Year: frame}`` for panels.
    ``variables``: analysis names to attach, or ``{name: replicate variable}``
    (default: every analysis variable whose name is a replicate variable).
    ``derive``: derive spec to evaluate on each replicate (default: the spec
    recorded by :func:`analysis_frame`).

    Returns ``(frame, replicate_long)``. With ``replace_se`` the frame's
    ``v_se``/``v_cv`` become exact replicate values and ``v_method`` becomes
    ``replicate`` for every row where replicates exist.
    """
    reps = _replicate_long(replicates)
    keys = _units(frame, reps)
    available = set(reps["variable"])
    names = analysis_variables(frame)
    if variables is None:
        var_map = {v: v for v in names if v in available}
    elif isinstance(variables, Mapping):
        var_map = {str(k): _default_name(v) for k, v in variables.items()}
    else:
        var_map = {str(v): _default_name(v) for v in variables}
    spec = list(derive if derive is not None else frame.attrs.get("derive", []))

    cache: Dict[str, np.ndarray] = {}

    def get(ref: str) -> np.ndarray:
        if ref in cache:
            return cache[ref]
        code = var_map.get(ref, _default_name(ref))
        if code not in available:
            raise KeyError(f"no replicates for {ref!r} (replicate variable {code!r})")
        cache[ref] = _rep_cube(frame, reps, keys, code)
        return cache[ref]

    results: Dict[str, np.ndarray] = {name: get(name) for name in var_map}
    for entry in spec:
        name = str(entry["name"])
        cache[name] = _eval_spec_replicates(entry, get)
        results[name] = cache[name]

    out = frame.copy()
    long_parts = []
    notes: List[str] = []
    for name, cube in results.items():
        has = np.isfinite(cube[1:]).all(axis=0) & np.isfinite(cube[0])
        if name in out.columns:
            diff = np.abs(cube[0] - out[name].to_numpy(float))
            scale = np.maximum(np.abs(out[name].to_numpy(float)), 1.0)
            if np.nanmax(np.where(has, diff / scale, 0.0), initial=0.0) > 1e-6:
                notes.append(f"{name}: replicate-table estimate differs from the frame estimate for some rows")
        if replace_se and name in out.columns:
            se = replicate_se(cube[0], cube[1:].T)
            est = out[name].to_numpy(float)
            out[f"{name}_se"] = np.where(has, se, out[f"{name}_se"].to_numpy(float))
            out[f"{name}_cv"] = _cv(est, out[f"{name}_se"].to_numpy(float))
            out[f"{name}_method"] = np.where(has, METHOD_REPLICATE, out[f"{name}_method"].astype(object))
        part = frame[keys].copy()
        part["variable"] = name
        part["estimate"] = cube[0]
        part = pd.concat([part.reset_index(drop=True),
                          pd.DataFrame(cube[1:].T, columns=REPLICATE_COLUMNS)], axis=1)
        long_parts.append(part[has])
    long = pd.concat(long_parts, ignore_index=True) if long_parts else pd.DataFrame(
        columns=keys + ["variable", "estimate"] + REPLICATE_COLUMNS)
    long.attrs["notes"] = notes
    return out, long


# ---------------------------------------------------------------------------
# A3. coverage_report
# ---------------------------------------------------------------------------


def coverage_report(
    frame: pd.DataFrame,
    replicates: Optional[pd.DataFrame] = None,
    cv_threshold: float = 30.0,
) -> pd.DataFrame:
    """Per-variable readiness: replicate coverage, undefined SEs, CV distribution.

    ``cv_threshold`` (percent) marks Census's "low reliability" (CV > 30%).
    ``replicates`` is the long frame returned by :func:`attach_replicates`.
    """
    rows = []
    keys = [c for c in UNIT_COLUMNS if c in frame.columns]
    for v in analysis_variables(frame):
        est = frame[v].to_numpy(float)
        se = frame[f"{v}_se"].to_numpy(float)
        cv = frame[f"{v}_cv"].to_numpy(float) if f"{v}_cv" in frame else _cv(est, se)
        defined = np.isfinite(est)
        n = len(frame)
        share_rep = 0.0
        if replicates is not None and len(replicates):
            sub = replicates[replicates["variable"] == v]
            if len(sub):
                k = [c for c in keys if c in sub.columns]
                share_rep = float(frame[k].merge(sub[k].drop_duplicates(), on=k, how="left", indicator=True)
                                  ["_merge"].eq("both").mean())
        fin = cv[np.isfinite(cv)]
        methods = frame[f"{v}_method"].astype(str).value_counts().to_dict() if f"{v}_method" in frame else {}
        rows.append({
            "variable": v,
            "n": n,
            "share_estimate_missing": float((~defined).mean()) if n else np.nan,
            "share_se_undefined": float((defined & ~np.isfinite(se)).mean()) if n else np.nan,
            "replicates_available": share_rep > 0,
            "share_with_replicates": share_rep,
            "cv_median": float(np.median(fin)) if fin.size else np.nan,
            "cv_p90": float(np.percentile(fin, 90)) if fin.size else np.nan,
            "cv_max": float(fin.max()) if fin.size else np.nan,
            "share_cv_over_threshold": float((cv > cv_threshold).sum() / n) if n else np.nan,
            "methods": methods,
        })
    return pd.DataFrame(rows)


def formula_variables(formula: str, frame: pd.DataFrame) -> List[str]:
    """Frame columns named in a patsy formula (outcome first)."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", formula)
    seen = list(dict.fromkeys(t for t in tokens if t in frame.columns))
    return seen
