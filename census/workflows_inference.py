# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 8 inference workflows shared by the MCP server and the CLI.

Plain arguments in (paths, lists, numbers), JSON-safe dicts out. Every result
carries ``success``, a one-sentence ``summary``, and a ``next_step`` naming the
tool to call next, so smaller local models can follow the workflow
(PLAN_ROUND_8 workstream J). Failures carry ``error_code`` (census.errors).
"""

import json
import os
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from census import errors
from census.workflows import ROOT, _jsonable, _load_frame, _records, _replicates_for, _write_frame
from census.results import interval_description

try:  # statsmodels formulas raise patsy errors for bad formulas
    from patsy import PatsyError as _PatsyError
except ImportError:  # pragma: no cover
    class _PatsyError(Exception):
        pass

# Errors from user input or numerically degenerate data -> INVALID_INPUT.
_INPUT_ERRORS = (FileNotFoundError, OSError, KeyError, ValueError, TypeError, RuntimeError,
                 ZeroDivisionError, np.linalg.LinAlgError, _PatsyError)

_MODEL_METHODS = ("replicate", "regression_calibration", "simex", "fay_herriot")
_FIGURE_KINDS = ("estimates", "forest", "trend")


def _ok(summary: str, next_step: str, **payload: Any) -> Dict[str, Any]:
    return _jsonable({"success": True, "summary": summary, "next_step": next_step, **payload})


def replicate_flag(result: Any) -> str:
    """Summary suffix for results flagged with incomplete replicates (PLAN_ROUND_11 Q2/D7), else ""."""
    info = ((getattr(result, "diagnostics", None) or {}).get("replicates_incomplete")
            or (getattr(result, "provenance", None) or {}).get("replicates_incomplete"))
    if not info:
        return ""
    return f" (REPLICATES INCOMPLETE: {info['note']})"


def result_summary(result: Any) -> str:
    """``result.summary()`` plus the REPLICATES INCOMPLETE flag, which appears at most once."""
    text = result.summary()
    return text if "REPLICATES INCOMPLETE" in text else text + replicate_flag(result)


def interval_next_step(info: Dict[str, Any]) -> str:
    """next_step guidance from census.results.interval_description (one wording for every model workflow)."""
    dist = "t with each coefficient's df" if info["distribution"] == "t" else "normal"
    step = (f"make_table or make_figure; report each coefficient with {info['basis_column']} (the SE behind its "
            f"{info['level'] * 100:g}% {dist} confidence interval and p-value; includes "
            f"{', '.join(info['basis_sources']) or 'no listed source'}) as {info['estimand']}")
    if info["undefined_terms"]:
        step += f"; uncertainty is undefined for {info['undefined_terms']}: report no interval or significance there"
    if info["replicates_incomplete"]:
        step += "; disclose that replicates were incomplete (sampling SE may be understated)"
    return step


def _fail(code: str, message: str, next_step: str = "") -> Dict[str, Any]:
    out = errors.error_result(code, message)
    out["summary"] = message
    if next_step:
        out["next_step"] = next_step
    return out


def _out_dir(path: Optional[str], default: str) -> str:
    folder = path or os.path.join(ROOT, "downloads", default)
    os.makedirs(folder, exist_ok=True)
    return folder


def _read_replicates(path: Optional[str]) -> Optional[pd.DataFrame]:
    if not path:
        return None
    from census.manifest import read_replicates
    return read_replicates(path)


def _load_frames(input_path: Any) -> pd.DataFrame:
    """One file, or several merged into one row per GEOID (and Year): files of
    different tables add columns, files of different states add rows. A cell
    given different values by two files is an error, never resolved silently."""
    if isinstance(input_path, str):
        return _load_frame(input_path)
    paths = list(input_path or [])
    if not paths:
        raise ValueError("input_path is an empty list")
    frames = [_load_frame(str(p)) for p in paths]
    if len(frames) == 1:
        return frames[0]
    keys = ["GEOID"] + (["Year"] if any("Year" in f.columns for f in frames) else [])
    # rows without a key would be dropped by the merge (groupby drops missing keys): refuse instead
    for path, frame in zip(paths, frames):
        for key in keys:
            if key not in frame.columns:
                raise ValueError(f"{path} has no {key} column" + (" (another input file has one); every file "
                                 "must carry the same keys" if key != "GEOID" else "; every input file needs one"))
            missing = int(frame[key].isna().sum())
            if missing:
                raise ValueError(f"{path} has {missing} row(s) with a missing {key}; fix or drop them first")
    df = pd.concat(frames, ignore_index=True, sort=False)
    df["GEOID"] = df["GEOID"].astype(str)
    grouped = df.groupby(keys, sort=False)
    conflicts = [c for c in df.columns if c not in keys and (grouped[c].nunique(dropna=True) > 1).any()]
    if conflicts:
        raise ValueError(f"input files disagree on {conflicts[:5]} for the same {'/'.join(keys)}")
    return grouped.first().reset_index()


def build_analysis_frame(
    input_path: Union[str, List[str]],
    variables: Optional[List[str]] = None,
    derive: Optional[List[Dict[str, Any]]] = None,
    use_replicates: bool = False,
    table_code: Optional[Union[str, List[str]]] = None,
    year: Optional[int] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Make a model-ready table from a downloaded file: each variable gets its
    estimate, standard error (_se), CV (_cv), and method, plus optional Census
    replicate values for exact sampling error.

    Args:
        input_path: CSV/Parquet from batch_download, or a list of them (other
            tables are joined on GEOID/Year; other states are stacked).
        variables: Estimate columns to keep, e.g. ["B19013_001E"]. Default: all.
        derive: Optional derived variables, e.g. [{"name": "poverty_rate",
            "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}].
        use_replicates: Download Census replicate estimates (needs table_code, year).
        table_code, year: Detailed table(s) (one code or a list) and release for replicates.
        output_path: Where to write the frame (default downloads/analysis_frame.parquet).

    Example: build_analysis_frame("out/B17001_county_panel_2023.csv", derive=[...])
    """
    from census.analysis import analysis_frame, attach_replicates, coverage_report

    try:
        df = _load_frames(input_path)
        if year is not None and "Year" in df.columns:
            df = df[df["Year"] == year]
        frame = analysis_frame(df, variables=variables, derive=derive)
        replicates = None
        if use_replicates:
            if not (table_code and year):
                return _fail(errors.INVALID_INPUT, "use_replicates needs table_code and year.")
            tables = [table_code] if isinstance(table_code, str) else list(table_code)
            geoids = list(df["GEOID"].astype(str))
            rep = pd.concat([_replicates_for(t, year, df, geoids) for t in tables], ignore_index=True)
            frame, replicates = attach_replicates(frame, rep, derive=derive)
        report = coverage_report(frame, replicates)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    path = _write_frame(frame, output_path or os.path.join(ROOT, "downloads", "analysis_frame.parquet"),
                        "analysis_frame.parquet")
    rep_path = None
    if replicates is not None:
        rep_path = os.path.splitext(path)[0] + "_replicates.parquet"
        replicates.to_parquet(rep_path, index=False)
    noisy = int((report.get("share_cv_over_threshold", pd.Series(dtype=float)) > 0.25).sum()) if len(report) else 0
    return _ok(
        f"Analysis frame with {len(frame)} rows written; {noisy} variable(s) have over 25% of rows with CV above the reliability threshold.",
        "fit_model (or correlation_matrix) with input_path=output_path"
        + (" and replicates_path" if rep_path else ""),
        output_path=path, replicates_path=rep_path, coverage=_records(report),
    )


def fit_model(
    input_path: str,
    formula: str,
    method: str = "replicate",
    model: str = "ols",
    replicates_path: Optional[str] = None,
    error_covariates: Optional[List[str]] = None,
    se_col: Optional[str] = None,
    confidence_level: float = 0.90,
    figure_dir: Optional[str] = None,
    target: str = "population",
    weights: Optional[str] = None,
    exposure: Optional[str] = None,
    trials: Optional[str] = None,
) -> Dict[str, Any]:
    """Fit a regression whose standard errors account for ACS sampling error.

    Args:
        input_path: Analysis frame from build_analysis_frame.
        formula: e.g. "poverty_rate ~ median_rent + unemployment_rate".
        method: "replicate" (refit on Census replicates), "regression_calibration"
            (predictors measured with error; corrects attenuation), "simex" (not
            validated for confirmatory use), "fay_herriot" (outcome with known SE in se_col).
        target: "population" (default: the general relationship, model SE),
            "these_areas" (these areas in this period, replicate SE), or
            "conservative" (both added; intervals too wide).
        model: "ols", "wls", "binomial", "poisson", or "negbin".
        replicates_path: From build_analysis_frame (needed for exact sampling error).
        error_covariates: Predictors measured with error (regression_calibration/simex).
        se_col: Outcome SE column (fay_herriot).
        confidence_level: 0.90 matches ACS margins of error.
        figure_dir: Also write a coefficient plot here.
        weights: Weight column (model="wls").
        exposure: Exposure column (model="poisson"/"negbin").
        trials: Trials column (model="binomial"; default effective sample size).

    target applies to method="replicate" only; corrected methods report se_total.
    Results report se_model, se_sampling, se_total and which sources are included.
    Example: fit_model("downloads/analysis_frame.parquet", "rate ~ rent", replicates_path="...")
    """
    from census import models, sae

    if method not in _MODEL_METHODS:
        return _fail(errors.INVALID_INPUT, f"method must be one of {_MODEL_METHODS}.")
    if method != "replicate" and target != "population":
        return _fail(errors.INVALID_INPUT,
                     f"target applies only to method='replicate'; {method} reports se_total.",
                     "call fit_model again without target")
    if method == "fay_herriot" and (weights or exposure or trials or model != "ols"):
        return _fail(errors.INVALID_INPUT,
                     "fay_herriot is a linear model with its own weights; model/weights/exposure/trials do not apply.",
                     "call fit_model again without model, weights, exposure, trials")
    extra = {"weights": weights, "exposure": exposure, "trials": trials}
    try:
        frame = _load_frame(input_path)
        if method == "replicate":
            result = models.fit_replicate(formula, frame, _read_replicates(replicates_path), model=model,
                                          confidence_level=confidence_level, target=target, **extra)
        elif method in ("regression_calibration", "simex"):
            if not error_covariates:
                return _fail(errors.INVALID_INPUT, f"{method} needs error_covariates.")
            fn = models.regression_calibration if method == "regression_calibration" else models.simex
            result = fn(formula, frame, error_covariates, model=model, confidence_level=confidence_level, **extra)
        else:
            if not se_col:
                return _fail(errors.INVALID_INPUT, "fay_herriot needs se_col (the outcome's SE column).")
            result = sae.fh_regression(formula, frame, se_col, confidence_level=confidence_level)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    out = result.to_dict()
    if figure_dir:
        from census.figures import coefficient_plot

        fig = coefficient_plot(result, out_dir=_out_dir(figure_dir, "figures"), name="coefficients")
        out["figure_paths"] = [str(p) for p in fig.paths.values()] if isinstance(fig.paths, dict) else [str(p) for p in fig.paths]
    if method == "replicate" and not replicates_path:
        out["notes"] = list(out.get("notes") or []) + [
            "No replicates supplied: standard errors include model error only, not ACS sampling error."
        ]
    # Report the SE that the displayed intervals and p-values actually use
    # (PLAN_ROUND_11 C7): se_model for target="population", se_sampling for
    # "these_areas", se_total for "conservative" and the corrected methods.
    info = interval_description(result)
    se_basis = info["basis_column"]
    out["interval_se_column"] = se_basis
    next_step = interval_next_step(info)
    if method == "simex":
        next_step += "; SIMEX under-covers and is exploratory only, not for confirmatory claims"
    return _ok(f"{result_summary(result)}; intervals and p-values use {se_basis}", next_step,
               **{k: v for k, v in out.items() if k != "summary"})


def small_area_estimates(
    input_path: str,
    formula: str,
    se_col: str,
    method: str = "REML",
    output_path: Optional[str] = None,
    figure_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Sharpen noisy small-area estimates (tracts, small counties) with the
    Fay-Herriot model, which borrows strength from covariates.

    Args:
        input_path: Analysis frame with the outcome, its SE column, and covariates.
        formula: e.g. "poverty_rate ~ unemployment_rate + median_rent".
        se_col: The outcome's standard error column, e.g. "poverty_rate_se".
        method: "REML" (default), "ML", or "FH".
        output_path: CSV of EBLUPs, MSEs, intervals, and shrinkage (gamma).
        figure_dir: Also write a shrinkage plot here.

    gamma near 0 means the area was pulled almost entirely to the model.
    """
    from census.sae import fay_herriot

    try:
        res = fay_herriot(formula, _load_frame(input_path), se_col, method=method)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    path = _write_frame(res.estimates, output_path, "small_area_estimates.csv")
    payload: Dict[str, Any] = {"output_path": path, "sigma2_u": res.sigma2_u,
                               "coefficients": res.coefficients, "notes": res.notes,
                               "preview": _records(res.estimates, 10)}
    if figure_dir:
        from census.figures import shrinkage_plot

        fig = shrinkage_plot(res, out_dir=_out_dir(figure_dir, "figures"))
        payload["figure_paths"] = [str(p) for p in (fig.paths.values() if isinstance(fig.paths, dict) else fig.paths)]
    return _ok(
        f"Fay-Herriot ({method}) estimates for {len(res.estimates)} areas; sigma2_u={res.sigma2_u:.4g}.",
        "make_table or make_figure; report EBLUPs with their MSE-based intervals",
        **payload,
    )


def correlation_matrix(
    input_path: str,
    variables: List[str],
    method: str = "pearson",
    replicates_path: Optional[str] = None,
    disattenuate: bool = False,
    figure_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Correlations among ACS variables with confidence intervals, corrected
    p-values, design-based SEs from replicates, and optional correction for the
    weakening caused by sampling error (reported next to, never instead of, r).

    Args:
        input_path: Analysis frame.
        variables: Two or more variable names.
        method: "pearson" or "spearman".
        replicates_path: From build_analysis_frame, for replicate SEs.
        disattenuate: Also report correlations corrected for sampling error.
        figure_dir: Also write a heatmap here.
    """
    from census.correlation import correlation_matrix as _corr

    if len(variables or []) < 2:
        return _fail(errors.INVALID_INPUT, "Pass at least two variables.")
    try:
        res = _corr(_load_frame(input_path), variables, method=method,
                    replicates=_read_replicates(replicates_path), disattenuate=disattenuate)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    payload = res.to_dict()
    if figure_dir:
        from census.figures import correlation_heatmap

        fig = correlation_heatmap(res, out_dir=_out_dir(figure_dir, "figures"))
        payload["figure_paths"] = [str(p) for p in (fig.paths.values() if isinstance(fig.paths, dict) else fig.paths)]
    return _ok(f"{method.title()} correlations among {len(variables)} variables.",
               "make_table (correlation) or report r with its interval", **payload)


def minimum_detectable_difference(
    moe1: float,
    moe2: Optional[float] = None,
    confidence: float = 0.90,
    power: float = 0.80,
    year1: Optional[int] = None,
    year2: Optional[int] = None,
) -> Dict[str, Any]:
    """Smallest difference between two estimates these margins of error can detect.

    Args:
        moe1, moe2: Published 90% MOEs (moe2 defaults to moe1).
        confidence: Test confidence level. power: Desired power.
        year1, year2: Releases, to account for overlapping 5-year periods.

    Example: minimum_detectable_difference(2.1, 2.4) -> about 4.8 points (90% confidence, 80% power).
    """
    from census.planning import minimum_detectable_difference as _mdd

    res = _mdd(moe1, moe2, confidence=confidence, power=power, year1=year1, year2=year2)
    value = res.get("mdd") if isinstance(res, dict) else None
    return _ok(f"Minimum detectable difference: {value}.",
               "if too large, aggregate geographies (aggregate_geographies) or use non-overlapping years",
               **res)


def reliability_profile(input_path: str, variables: Optional[List[str]] = None) -> Dict[str, Any]:
    """Distribution of coefficients of variation for each variable in a file:
    how many estimates are high (CV<=12%), moderate, or low (CV>30%) reliability.
    Use before choosing a geography level or model."""
    from census.planning import reliability_profile as _profile

    try:
        res = _profile(_load_frame(input_path), variables)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    return _ok(f"Reliability profile for {len(res)} variable(s).",
               "build_analysis_frame with the reliable variables", profile=_records(res))


def trend_test(years: List[int], estimates: List[float], moes: List[float],
               confidence_level: float = 0.90) -> Dict[str, Any]:
    """Test for a linear trend across three or more NON-overlapping ACS 5-year
    releases (e.g. 2013, 2018, 2023). Overlapping releases are refused with a
    suggested non-overlapping set.

    Example: trend_test([2013, 2018, 2023], [14.1, 13.0, 11.8], [0.4, 0.4, 0.3])
    """
    from census.trends import linear_trend_test

    from census.trends import overlap_design_check

    try:
        design = overlap_design_check(years)
    except ValueError as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    if design.get("overlapping"):
        latest = max(int(y) for y in years)
        example = [latest - 10, latest - 5, latest]
        return _fail(
            errors.INVALID_INPUT,
            f"Releases {sorted(years)} overlap: 5-year releases less than 5 years apart share sample "
            f"years, so a trend test across them overstates precision. Use releases at least 5 years "
            f"apart, e.g. {example}.",
            f"download releases {example} and call trend_test again",
        )
    try:
        res = linear_trend_test(years, estimates, moes=moes, confidence_level=confidence_level)
    except ValueError as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    sig = res.get("significant")
    return _ok(f"Trend slope {res.get('slope')} per year; significant: {sig}.",
               "make_figure(kind='trend') to plot it", **res)


def make_figure(
    kind: str,
    input_path: str,
    output_dir: Optional[str] = None,
    label_col: Optional[str] = None,
    estimate_col: Optional[str] = None,
    moe_col: Optional[str] = None,
    se_col: Optional[str] = None,
    period_col: Optional[str] = None,
    width: str = "one",
    data_source: str = "",
) -> Dict[str, Any]:
    """Journal-ready figure (PDF, SVG, EPS, PNG) with a values CSV, draft caption,
    and provenance file.

    Args:
        kind: "estimates" (estimates with 90% intervals; needs label_col,
            estimate_col, moe_col or se_col), "forest" (input is a
            batch_significance output), or "trend" (needs period_col, estimate_col,
            moe_col or se_col).
        input_path: CSV/Parquet with the values.
        output_dir: Folder for the files (default downloads/figures).
        width: "one" or "two" journal columns.
        data_source: Text for the caption, e.g. "ACS 5-year 2019-2023, table B17001".

    Model, correlation, and small-area figures come from fit_model,
    correlation_matrix, and small_area_estimates via their figure_dir argument.
    """
    from census import figures

    if kind not in _FIGURE_KINDS:
        return _fail(errors.INVALID_INPUT, f"kind must be one of {_FIGURE_KINDS}.")
    folder = _out_dir(output_dir, "figures")
    try:
        df = _load_frame(input_path)
        common = {"out_dir": folder, "width": width, "data_source": data_source}
        if kind == "estimates":
            fig = figures.estimates_with_intervals(df, label_col, estimate_col, se_col=se_col, moe_col=moe_col, **common)
        elif kind == "forest":
            fig = figures.forest_plot(df, label_col=label_col, **common)
        else:
            fig = figures.trend_plot(df, period_col, estimate_col, se_col=se_col, moe_col=moe_col, **common)
    except (FileNotFoundError, OSError, KeyError, ValueError, TypeError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    paths = fig.paths.values() if isinstance(fig.paths, dict) else fig.paths
    return _ok(f"Wrote {kind} figure '{fig.name}'.", "make_table, or include the PDF/SVG and caption in your write-up",
               figure_paths=[str(p) for p in paths], caption=fig.caption)


def make_table(kind: str, input_path: str, output_dir: Optional[str] = None,
               variables: Optional[List[str]] = None, name: str = "table") -> Dict[str, Any]:
    """Publication table as LaTeX (booktabs), Markdown, and CSV.

    Args:
        kind: "descriptive" (variables from an analysis frame) or "comparison"
            (a batch_significance output file).
        input_path: The file. variables: For descriptive tables.
        output_dir: Folder (default downloads/tables).
    """
    from census import report_tables

    folder = _out_dir(output_dir, "tables")
    try:
        df = _load_frame(input_path)
        if kind == "descriptive":
            table = report_tables.descriptive_table(df, variables or [c for c in df.columns if f"{c}_se" in df.columns])
        elif kind == "comparison":
            table = report_tables.comparison_table(df)
        else:
            return _fail(errors.INVALID_INPUT, "kind must be 'descriptive' or 'comparison'.")
        written = table.write(folder, name)
    except _INPUT_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                     "check the formula, column names, and input file, then retry")
    files = list(written.values()) if isinstance(written, dict) else written
    return _ok(f"Wrote {kind} table '{name}'.", "include the .tex or .md table in your write-up",
               table_paths=[str(p) for p in files], markdown=table.to_markdown())


def _extraction_record(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Provenance fields of an extraction metadata JSON."""
    return {"table": meta.get("table_code"), "dataset": meta.get("dataset"), "years": meta.get("years"),
            "geo_level": meta.get("geo_level"), "endpoint": meta.get("endpoint"),
            "states": sorted({e.get("state_code") for e in meta.get("entities") or [] if e.get("state_code")}),
            "variables": meta.get("variables"), "generated_at": meta.get("generated_at"),
            "package_version": meta.get("package_version"), "git_commit": meta.get("git_commit"),
            "failed_calls": len(meta.get("failed_calls") or []), "data_source": meta.get("data_source")}


def _recorded_source(doc: Any) -> Optional[Dict[str, Any]]:
    """The data source a saved JSON (extraction metadata, tool result, manifest run or bundle)
    recorded when its data were read: {"synthetic": bool, ...}, or None when it records none."""
    if not isinstance(doc, dict):
        return None
    for source in (doc.get("data_source"), (doc.get("provenance") or {}).get("data_source")):
        if isinstance(source, dict) and isinstance(source.get("synthetic"), bool):
            return source
    return None


def _input_names(paths: List[str]) -> List[str]:
    """Input paths relative to their common folder ('/'-separated), so same-named files from
    different folders (extract/26/B19013/..., extract/36/B19013/...) stay distinguishable."""
    full = [os.path.abspath(p) for p in paths]
    if not full:
        return []
    try:
        common = os.path.commonpath([os.path.dirname(p) for p in full])
    except ValueError:  # different drives (Windows): keep the parent folder name
        return [f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}" for p in full]
    return [os.path.relpath(p, common).replace(os.sep, "/") for p in full]


def _bundle_source(recorded: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Bundle data source from the inputs' recorded sources (each {path, synthetic, ...})."""
    if not recorded:
        return {"kind": "unknown", "synthetic": None}
    synthetic = [r for r in recorded if r["synthetic"]]
    if not synthetic:
        return {"kind": "census_api", "synthetic": False}
    marker = {k: synthetic[0][k] for k in ("description", "generator") if synthetic[0].get(k)}
    return {"kind": "synthetic" if len(synthetic) == len(recorded) else "mixed", "synthetic": True, **marker}


def make_results_bundle(
    output_dir: str,
    results: Dict[str, str],
    figure_paths: Optional[List[str]] = None,
    inputs: Optional[List[str]] = None,
    title: str = "",
    notes: Optional[List[str]] = None,
    allow_failed_results: bool = False,
) -> Dict[str, Any]:
    """Collect an exploratory (not pre-registered) analysis into one results bundle:
    results JSON, figures, methods.md, and bundle.json with every file's SHA-256,
    the data citation and provenance (data source, input hashes, extractions).

    Args:
        output_dir: Bundle folder.
        results: {name: path} of tool results saved with `python -m census tool ... --save PATH`.
            A saved failure (success false) is refused unless allow_failed_results=True,
            which lists it under "Failed steps" in methods.md, never as a result.
        figure_paths: Figure files to copy (e.g. make_map figure_paths).
        inputs: Files the analysis used (extraction metadata JSON, frames, replicate
            files); their SHA-256 (keyed by the path relative to the inputs' common
            folder) and, for extraction metadata, table, years, states, endpoint,
            call failures and data source are recorded.
        title, notes: Methods heading and extra methods/limitations lines.
    The data source comes from what the inputs and results recorded when their data
    were read (extraction metadata records the cache marker at extraction time): all
    synthetic -> "synthetic", none synthetic -> "census_api", both -> "mixed" (with a
    warning), nothing recorded -> "unknown". It is never taken from the cache present
    at bundle time. Pre-registered analyses use run_manifest instead (status "confirmatory").
    """
    import shutil

    from census.manifest import STATUS_EXPLORATORY
    from census.pipeline import file_sha256
    from census.report_tables import data_citation, results_bundle

    try:
        loaded, failed, recorded = {}, [], []
        for name, path in (results or {}).items():
            with open(path, encoding="utf-8") as f:
                res = json.load(f)
            if isinstance(res, dict) and res.get("success") is False:
                failed.append({"name": name, "path": os.path.basename(path), "error_code": res.get("error_code"),
                               "error": res.get("error") or res.get("summary")})
                if not allow_failed_results:
                    return _fail(errors.INVALID_INPUT,
                                 f"Saved result '{name}' ({path}) is a failure (success false: "
                                 f"{failed[-1]['error']}); a failed step is not a result.",
                                 "fix and re-run that step, or drop it from results (allow_failed_results=True "
                                 "lists it under Failed steps, not as a result)")
                continue
            loaded[name] = res
            source = _recorded_source(res)
            if source:
                recorded.append({"path": f"results/{name}.json", **source})
        input_records, extractions = [], []
        for path, rel in zip(inputs or [], _input_names(list(inputs or []))):
            input_records.append({"path": rel, "sha256": file_sha256(path)})
            if path.lower().endswith(".json"):
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                if path.endswith("_extraction_metadata.json") and isinstance(doc, dict):
                    extractions.append(_extraction_record(doc))
                source = _recorded_source(doc)
                if source:
                    recorded.append({"path": rel, **source})
        os.makedirs(os.path.join(output_dir, "figures"), exist_ok=True)
        figs = list(figure_paths or [])
        base = [os.path.basename(p) for p in figs]
        # same file name from different folders (e.g. maps/26/choropleth.pdf, maps/36/...): prefix the folder
        names = [b if base.count(b) == 1 else f"{os.path.basename(os.path.dirname(os.path.abspath(p)))}_{b}"
                 for p, b in zip(figs, base)]
        if len(set(names)) != len(names):
            raise ValueError("figure_paths contain the same file name twice in the same folder name")
        for path, name in zip(figs, names):
            shutil.copyfile(path, os.path.join(output_dir, "figures", name))
    except (OSError, ValueError, TypeError) as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}")
    source = _bundle_source(recorded)
    warnings: List[str] = []
    if source["kind"] == "mixed":
        synth = [r["path"] for r in recorded if r["synthetic"]]
        real = [r["path"] for r in recorded if not r["synthetic"]]
        warnings.append(f"MIXED DATA SOURCES: SYNTHETIC inputs ({', '.join(synth)}) are combined with Census "
                        f"Data API inputs ({', '.join(real)}). These are not Census results and must not be "
                        "reported as such; re-extract every input from one source.")
    elif source["kind"] == "unknown":
        warnings.append("DATA SOURCE UNKNOWN: no input records where its data came from; pass the extraction "
                        "metadata JSON files as inputs (they record the source observed at extraction time).")
    releases = sorted({int(y) for e in extractions for y in e.get("years") or []})
    tables = sorted({e["table"] for e in extractions if e.get("table")})
    citation = data_citation(releases, tables, synthetic=bool(source["synthetic"]),
                             description=source.get("description"), retrieved=source["kind"] != "unknown")
    if source["kind"] == "mixed":
        citation = "MIXED DATA SOURCES (some inputs synthetic): " + citation
    lines = [f"# {title or 'Results'}", "",
             f"**Analysis status:** {STATUS_EXPLORATORY} (not a pre-registered manifest run).", "",
             f"**Data:** {citation}", ""]
    for w in warnings:
        lines += [f"**WARNING:** {w}", ""]
    for name, res in loaded.items():
        lines.append(f"## {name}")
        lines.append("")
        if res.get("summary"):
            lines.append(str(res["summary"]))
            lines.append("")
        interval = res.get("interval")
        if isinstance(interval, dict) and interval.get("text"):
            lines += [f"Intervals: {interval['text']}", ""]
        for note in res.get("notes") or []:
            lines.append(f"- {note}")
        lines.append("")
    if failed:
        lines += ["## Failed steps (FAILED: not results)", ""]
        lines += [f"- {f['name']}: FAILED ({f['error_code']}): {f['error']}" for f in failed] + [""]
    if notes:
        lines += ["## Notes and limitations", ""] + [f"- {n}" for n in notes] + [""]
    provenance = {"analysis_status": STATUS_EXPLORATORY, "data_source": source, "inputs": input_records,
                  "recorded_data_sources": recorded, "extractions": extractions, "title": title}
    if warnings:
        provenance["warnings"] = warnings
    if failed:
        provenance["failed_results"] = failed
    bundle = results_bundle(output_dir, methods="\n".join(lines), results=loaded, provenance=provenance,
                            citation=citation)
    return _ok(f"Results bundle with {len(loaded)} result(s), {len(figure_paths or [])} figure file(s) and "
               f"{len(input_records)} input hash(es) written; data source {source['kind']}."
               + (f" WARNING: {' '.join(warnings)}" if warnings else ""),
               "cite bundle.json (file hashes and provenance) and methods.md with the results"
               + ("; resolve the data source warning before reporting" if warnings else ""),
               output_dir=output_dir, data_source=source, citation=citation, warnings=warnings,
               results=list(loaded), files=len(bundle.get("files", {})), extractions=extractions)


def save_json(result: Dict[str, Any], path: str) -> str:
    """Write a tool result as the JSON ``--json`` prints (``python -m census tool ... --save PATH``)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(result, indent=2, default=str) + "\n")
    return path


def create_manifest(manifest_path: str) -> Dict[str, Any]:
    """Validate a pre-registration manifest (JSON) and return its SHA-256 hash.
    Commit the manifest and hash BEFORE pulling data; then call run_manifest.
    Several states: geography {"level": "county", "states": ["26", "36"]}; a price
    adjustment: "adjustments": [{"type": "rpp", "variables": ["income"], "rpp_year": 2024}]
    (rpp_year is required; when omitted, the latest RPP year is filled in before
    hashing and the completed manifest is written to <name>.completed.json, never
    over the input; commit and run that file, as the result's notes say).
    Several releases: the adjustment also needs "dollar_year", "price_index"
    and "rpp_years" ({"2019": 2019, "2024": 2024}); omitted ones are filled the same
    way. "replicates": {"source": "census_vre"} makes run_manifest fetch Census
    replicates; the notes list variables that have none.

    The schema is documented in census/manifest.py and examples/manifests/.
    Example: create_manifest("examples/manifests/income_population_tracts.json")
    """
    from census.manifest import create_manifest as _create

    try:
        res = _create(manifest_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    if res.get("errors"):
        return _fail(errors.INVALID_INPUT, "Manifest invalid: " + "; ".join(map(str, res["errors"])),
                     "fix the listed problems and call create_manifest again")
    return _ok(f"Manifest valid; SHA-256 {res['hash']}." + "".join(f" {n}." for n in res.get("notes") or []),
               "commit the manifest, then run_manifest with the same path", **res)


def run_manifest(manifest_path: str, output_dir: Optional[str] = None,
                 replicates_path: Optional[str] = None, replicates_synthetic: Optional[bool] = None) -> Dict[str, Any]:
    """Run a pre-registered analysis exactly as written and write a results bundle
    (tables, figures, methods.md); anything that could not run is a deviation.

    Args:
        manifest_path: the manifest JSON.
        replicates_path: optional CSV/Parquet of replicate estimates (long format:
            GEOID, Year, variable, rep_1..rep_80), needed for "replicates": "required".
            Not with a census_vre manifest.
        replicates_synthetic: mark supplied replicates synthetic.

    Returns analysis_status, data_source, replicates, deviations, hypotheses;
    success is False when analysis_status is "confirmatory_incomplete".

    Example: run_manifest("examples/manifests/income_population_tracts.json")
    """
    from census.manifest import run_manifest as _run

    folder = _out_dir(output_dir, "manifest_run")
    try:
        reps = _read_replicates(replicates_path)
    except (FileNotFoundError, OSError, ValueError, ImportError) as exc:
        return _fail(errors.INVALID_INPUT, f"replicates_path unreadable: {type(exc).__name__}: {exc}",
                     "pass a CSV or Parquet file in the analysis replicate long format")
    try:
        res = _run(manifest_path, folder, replicates=reps, replicates_synthetic=replicates_synthetic)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}" if isinstance(exc, KeyError) else str(exc))
    if res.get("errors"):
        return _fail(errors.INVALID_INPUT, "Manifest invalid: " + "; ".join(map(str, res["errors"])),
                     "fix the manifest with create_manifest")
    hyp = res.get("hypotheses")
    devs = res.get("deviations") or []
    status = res.get("analysis_status")
    out = {
        "manifest_hash": res.get("manifest_hash"), "output_dir": res.get("output_dir"),
        "analysis_status": status, "data_source": res.get("data_source"), "replicates": res.get("replicates"),
        "deviations": devs, "hypotheses": hyp, "bundle": res.get("bundle"),
    }
    if "adjustments" in res:
        out["adjustments"] = res["adjustments"]
    payload = _jsonable(out)
    if not res.get("success"):
        kinds = sorted({str(d.get("kind")) for d in devs})
        payload.update({"success": False, "error_code": errors.INVALID_INPUT if "data_source_mismatch" in kinds
                        else errors.NO_DATA,
                        "error": f"Manifest ran with {len(devs)} deviation(s) that block confirmatory results "
                                 f"({', '.join(kinds)}); analysis_status {status}.",
                        "summary": f"Run incomplete ({status}): {len(devs)} deviation(s); see deviations.",
                        "next_step": "report the run as confirmatory_incomplete with its deviations; do not "
                                     "substitute variables, releases or data sources"
                                     + ("; supply replicates_path" if "replicates_unavailable" in kinds else "")})
        return payload
    return _ok(f"Manifest run complete ({status}); {len(devs)} deviation(s); results in {res.get('output_dir')}.",
               "read methods.md and the tables in the output folder; report deviations if any",
               **out)
