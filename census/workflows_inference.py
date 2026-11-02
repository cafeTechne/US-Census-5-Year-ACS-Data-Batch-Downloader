# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 8 inference workflows shared by the MCP server and the CLI.

Plain arguments in (paths, lists, numbers), JSON-safe dicts out. Every result
carries ``success``, a one-sentence ``summary``, and a ``next_step`` naming the
tool to call next, so smaller local models can follow the workflow
(PLAN_ROUND_8 workstream J). Failures carry ``error_code`` (census.errors).
"""

import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from census import errors
from census.workflows import ROOT, _jsonable, _load_frame, _records, _replicates_for, _write_frame

_MODEL_METHODS = ("replicate", "regression_calibration", "simex", "fay_herriot")
_FIGURE_KINDS = ("estimates", "forest", "trend")


def _ok(summary: str, next_step: str, **payload: Any) -> Dict[str, Any]:
    return _jsonable({"success": True, "summary": summary, "next_step": next_step, **payload})


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
    return pd.read_parquet(path) if path.lower().endswith(".parquet") else pd.read_csv(path, dtype={"GEOID": str})


def build_analysis_frame(
    input_path: str,
    variables: Optional[List[str]] = None,
    derive: Optional[List[Dict[str, Any]]] = None,
    use_replicates: bool = False,
    table_code: Optional[str] = None,
    year: Optional[int] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Make a model-ready table from a downloaded file: each variable gets its
    estimate, standard error (_se), CV (_cv), and method, plus optional Census
    replicate values for exact sampling error.

    Args:
        input_path: CSV/Parquet from batch_download.
        variables: Estimate columns to keep, e.g. ["B19013_001E"]. Default: all.
        derive: Optional derived variables, e.g. [{"name": "poverty_rate",
            "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}].
        use_replicates: Download Census replicate estimates (needs table_code, year).
        table_code, year: Detailed table and release for replicates.
        output_path: Where to write the frame (default downloads/analysis_frame.parquet).

    Example: build_analysis_frame("out/B17001_county_panel_2023.csv", derive=[...])
    """
    from census.analysis import analysis_frame, attach_replicates, coverage_report

    try:
        df = _load_frame(input_path)
        if year is not None and "Year" in df.columns:
            df = df[df["Year"] == year]
        frame = analysis_frame(df, variables=variables, derive=derive)
        replicates = None
        if use_replicates:
            if not (table_code and year):
                return _fail(errors.INVALID_INPUT, "use_replicates needs table_code and year.")
            rep = _replicates_for(table_code, year, df, list(df["GEOID"].astype(str)))
            frame, replicates = attach_replicates(frame, rep, derive=derive)
        report = coverage_report(frame, replicates)
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    path = _write_frame(frame, output_path or os.path.join(ROOT, "downloads", "analysis_frame.parquet"),
                        "analysis_frame.parquet")
    rep_path = None
    if replicates is not None:
        rep_path = os.path.splitext(path)[0] + "_replicates.parquet"
        replicates.to_parquet(rep_path, index=False)
    noisy = int((report.get("share_low_reliability", pd.Series(dtype=float)) > 0.25).sum()) if len(report) else 0
    return _ok(
        f"Analysis frame with {len(frame)} rows written; {noisy} variable(s) have over 25% low-reliability rows.",
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
) -> Dict[str, Any]:
    """Fit a regression whose standard errors account for ACS sampling error.

    Args:
        input_path: Analysis frame from build_analysis_frame.
        formula: e.g. "poverty_rate ~ median_rent + unemployment_rate".
        method: "replicate" (refit on Census replicates; best), "regression_calibration"
            or "simex" (predictors with known SEs, no replicates), "fay_herriot"
            (outcome with known SE in se_col).
        model: "ols", "wls", "binomial", "poisson", or "negbin".
        replicates_path: From build_analysis_frame (needed for exact sampling error).
        error_covariates: Predictors measured with error (regression_calibration/simex).
        se_col: Outcome SE column (fay_herriot).
        confidence_level: 0.90 matches ACS margins of error.
        figure_dir: Also write a coefficient plot here.

    Results report se_model, se_sampling, se_total and which sources are included.
    Example: fit_model("downloads/analysis_frame.parquet", "rate ~ rent", replicates_path="...")
    """
    from census import models, sae

    if method not in _MODEL_METHODS:
        return _fail(errors.INVALID_INPUT, f"method must be one of {_MODEL_METHODS}.")
    try:
        frame = _load_frame(input_path)
        if method == "replicate":
            result = models.fit_replicate(formula, frame, _read_replicates(replicates_path), model=model,
                                          confidence_level=confidence_level)
        elif method in ("regression_calibration", "simex"):
            if not error_covariates:
                return _fail(errors.INVALID_INPUT, f"{method} needs error_covariates.")
            fn = models.regression_calibration if method == "regression_calibration" else models.simex
            result = fn(formula, frame, error_covariates, model=model, confidence_level=confidence_level)
        else:
            if not se_col:
                return _fail(errors.INVALID_INPUT, "fay_herriot needs se_col (the outcome's SE column).")
            result = sae.fh_regression(formula, frame, se_col, confidence_level=confidence_level)
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    out = result.to_dict()
    if figure_dir:
        from census.figures import coefficient_plot

        fig = coefficient_plot(result, out_dir=_out_dir(figure_dir, "figures"), name="coefficients")
        out["figure_paths"] = [str(p) for p in fig.paths.values()] if isinstance(fig.paths, dict) else [str(p) for p in fig.paths]
    if method == "replicate" and not replicates_path:
        out["notes"] = list(out.get("notes") or []) + [
            "No replicates supplied: standard errors include model error only, not ACS sampling error."
        ]
    return _ok(result.summary(), "make_table or make_figure; report se_total and the uncertainty sources",
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
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
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
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
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
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
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
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    files = list(written.values()) if isinstance(written, dict) else written
    return _ok(f"Wrote {kind} table '{name}'.", "include the .tex or .md table in your write-up",
               table_paths=[str(p) for p in files], markdown=table.to_markdown())


def save_json(result: Dict[str, Any], path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_jsonable(result), f, indent=2)
    return path
