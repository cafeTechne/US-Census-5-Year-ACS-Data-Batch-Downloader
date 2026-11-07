# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publication figures from Round 8 results (PLAN_ROUND_8.md, I1).

matplotlib only, rendered off-screen (``matplotlib.figure.Figure`` with the
Agg canvas; pyplot is never imported, so no global backend or figure state).
Every figure function writes, under ``out_dir``:

* ``<name>.pdf/.svg/.eps`` (vector) and ``<name>.png`` (``dpi``);
* ``<name>.csv``: the plotted values;
* ``<name>.caption.txt``: a caption draft (data source, interval level, method);
* ``<name>.json``: provenance sidecar (result ID, manifest hash, inputs, and
  the SHA-256 of every file written).

Style (decision 6): sans-serif fonts at >= 7 pt, the Okabe-Ito colorblind-safe
palette, distinct markers/fills so series survive grayscale printing, widths
for one- (3.5 in) and two-column (7.0 in) journal layouts, embedded TrueType
fonts. Bytes are reproducible: fixed SVG hash salt, no creation dates in
PDF/SVG/EPS metadata, no software tag in PNG metadata.
"""

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from scipy import stats

from .results import CorrelationResult, ModelResult, SmallAreaResult, interval_description
from .variance import Z90

OKABE_ITO = ["#000000", "#E69F00", "#56B4E9", "#009E73", "#F0E442",
             "#0072B2", "#D55E00", "#CC79A7"]
# Series order: black, blue, vermillion, sky blue, orange, green, purple.
SERIES_COLORS = ["#000000", "#0072B2", "#D55E00", "#56B4E9", "#E69F00", "#009E73", "#CC79A7"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]
LINESTYLES = ["-", "--", ":", "-."]
JOURNAL_WIDTHS = {"one": 3.5, "two": 7.0}
DEFAULT_FORMATS = ("pdf", "svg", "eps", "png")
SVG_HASHSALT = "census-acs-figures"
CV_THRESHOLDS = (12.0, 30.0)

STYLE: Dict[str, Any] = {
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.labelsize": 8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "legend.fontsize": 7,
    "axes.prop_cycle": matplotlib.cycler(color=OKABE_ITO),
    "axes.spines.top": False,
    "axes.spines.right": False,
    "lines.linewidth": 1.0,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "path",
    "svg.hashsalt": SVG_HASHSALT,
    "path.simplify": False,
}
_METADATA = {
    "pdf": {"CreationDate": None, "ModDate": None, "Producer": None, "Creator": None},
    "svg": {"Date": None, "Creator": None},
    "eps": {"Creator": None},
    "png": {"Software": None},
}


@dataclass
class FigureOutput:
    """What a figure function produced. ``figure`` is kept for inspection."""

    name: str
    paths: Dict[str, str]
    values: pd.DataFrame
    caption: str
    provenance: Dict[str, Any]
    figure: Any = None
    notes: List[str] = field(default_factory=list)


def _level(confidence_level: float) -> float:
    level = confidence_level / 100.0 if confidence_level > 1 else float(confidence_level)
    if not 0 < level < 1:
        raise ValueError("confidence_level must be in (0, 1)")
    return level


def _zcrit(level: float) -> float:
    return Z90 if math.isclose(level, 0.90) else float(stats.norm.ppf(1 - (1 - level) / 2))


def _pct(level: float) -> str:
    return f"{level * 100:g}%"


def _size(width: Union[str, float], height: Optional[float], default_ratio: float):
    w = JOURNAL_WIDTHS[width] if isinstance(width, str) else float(width)
    return (w, height if height is not None else w * default_ratio)


def _new_figure(width, height, ratio):
    with matplotlib.rc_context(STYLE):
        fig = Figure(figsize=_size(width, height, ratio))
        ax = fig.add_subplot(1, 1, 1)
    return fig, ax


def _err(est, lower, upper) -> np.ndarray:
    """Asymmetric errorbar extents as a (2, n) float array."""
    e, lo, hi = (np.asarray(a, dtype=float) for a in (est, lower, upper))
    return np.vstack([e - lo, hi - e])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _se_from(frame: pd.DataFrame, se_col: Optional[str], moe_col: Optional[str]) -> np.ndarray:
    if se_col:
        return pd.to_numeric(frame[se_col], errors="coerce").to_numpy(float)
    if moe_col:
        return pd.to_numeric(frame[moe_col], errors="coerce").to_numpy(float) / Z90
    raise ValueError("give se_col or moe_col")


def _finish(fig: Figure, name: str, out_dir: Union[str, Path], values: pd.DataFrame,
            caption: str, provenance: Dict[str, Any], formats: Sequence[str], dpi: int,
            notes: Optional[List[str]] = None) -> FigureOutput:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    with matplotlib.rc_context(STYLE):
        fig.tight_layout()
        for fmt in formats:
            fmt = fmt.lower()
            if fmt not in _METADATA:
                raise ValueError(f"unsupported format {fmt!r}; use {sorted(_METADATA)}")
            p = out / f"{name}.{fmt}"
            kwargs = {"format": fmt, "metadata": _METADATA[fmt]}
            if fmt == "png":
                kwargs["dpi"] = dpi
            if fmt == "eps":
                # PS backend stamps the time unless SOURCE_DATE_EPOCH is set.
                prior = os.environ.get("SOURCE_DATE_EPOCH")
                os.environ["SOURCE_DATE_EPOCH"] = "0"
                try:
                    fig.savefig(p, **kwargs)
                finally:
                    if prior is None:
                        os.environ.pop("SOURCE_DATE_EPOCH", None)
                    else:
                        os.environ["SOURCE_DATE_EPOCH"] = prior
            else:
                fig.savefig(p, **kwargs)
            paths[fmt] = str(p)
    csv = out / f"{name}.csv"
    values.to_csv(csv, index=False, lineterminator="\n")
    paths["csv"] = str(csv)
    cap = out / f"{name}.caption.txt"
    cap.write_text(caption + "\n", encoding="utf-8", newline="\n")
    paths["caption"] = str(cap)
    prov = dict(provenance)
    prov["figure"] = name
    prov["notes"] = list(notes or [])
    prov["files"] = {Path(v).name: _sha256(Path(v)) for k, v in sorted(paths.items())}
    prov["matplotlib_version"] = matplotlib.__version__
    side = out / f"{name}.json"
    side.write_text(json.dumps(_clean(prov), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    paths["provenance"] = str(side)
    return FigureOutput(name=name, paths=paths, values=values, caption=caption,
                        provenance=prov, figure=fig, notes=list(notes or []))


def _clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _provenance(kind: str, data_source: str, level: Optional[float], result_id, manifest_hash,
                extra: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    prov: Dict[str, Any] = {"kind": kind, "data_source": data_source,
                            "confidence_level": level, "result_id": result_id,
                            "manifest_hash": manifest_hash}
    if extra:
        prov["inputs"] = dict(extra)
    return prov


def _caption(title: str, data_source: str, interval: str, method: str) -> str:
    parts = [title.rstrip(".") + "."]
    if data_source:
        parts.append(f"Source: {data_source.rstrip('.')}.")
    if interval:
        parts.append(interval.rstrip(".") + ".")
    if method:
        parts.append(f"Method: {method.rstrip('.')}.")
    return " ".join(parts)


_COMMON_DOC = """
    Common keyword arguments: ``out_dir``, ``name`` (file stem), ``width``
    (``"one"``, ``"two"`` or inches), ``height`` (inches), ``formats``,
    ``dpi``, ``data_source`` (releases and tables, for the caption),
    ``result_id``, ``manifest_hash``, ``provenance`` (extra inputs to record).
"""


def estimates_with_intervals(
    frame: pd.DataFrame, label_col: str, estimate_col: str, *, se_col: Optional[str] = None,
    moe_col: Optional[str] = None, confidence_level: float = 0.90,
    cv_threshold: float = CV_THRESHOLDS[1], xlabel: Optional[str] = None,
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "estimates",
    width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Sorted dot-and-whisker plot; estimates with CV > ``cv_threshold`` are
    drawn with open markers and flagged in the CSV."""
    level = _level(confidence_level)
    zc = _zcrit(level)
    est = pd.to_numeric(frame[estimate_col], errors="coerce").to_numpy(float)
    se = _se_from(frame, se_col, moe_col)
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
    values = pd.DataFrame({"label": frame[label_col].astype(str).to_numpy(), "estimate": est,
                           "se": se, "ci_lower": est - zc * se, "ci_upper": est + zc * se,
                           "cv_percent": cv, "low_reliability": cv > cv_threshold})
    values = values.sort_values("estimate", kind="mergesort").reset_index(drop=True)
    values["position"] = np.arange(len(values), dtype=float)
    fig, ax = _new_figure(width, height, max(0.6, 0.12 * len(values) + 0.3))
    with matplotlib.rc_context(STYLE):
        for low, fill, lab in ((False, OKABE_ITO[0], "Reliable"),
                               (True, "white", f"CV > {cv_threshold:g}%")):
            sub = values[values["low_reliability"] == low]
            if sub.empty:
                continue
            ax.errorbar(sub["estimate"].to_numpy(float), sub["position"].to_numpy(float),
                        xerr=_err(sub["estimate"], sub["ci_lower"], sub["ci_upper"]),
                        fmt="o", color=OKABE_ITO[0], mfc=fill, ms=4, capsize=0, elinewidth=0.8,
                        label=lab)
        ax.set_yticks(values["position"], values["label"])
        ax.set_xlabel(xlabel or estimate_col)
        if values["low_reliability"].any():
            ax.legend(frameon=False, loc="best")
    caption = _caption(title or f"Estimates of {estimate_col} with {_pct(level)} confidence intervals",
                       data_source, f"Whiskers show {_pct(level)} confidence intervals (estimate ± "
                       f"{zc:.3f} × SE{'' if se_col else ', SE from the published 90% MOE / 1.645'})",
                       f"open markers denote coefficients of variation above {cv_threshold:g}% "
                       "(low reliability)")
    prov = _provenance("estimates_with_intervals", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, estimate_col=estimate_col, se_col=se_col, moe_col=moe_col))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi)


def coefficient_plot(
    results: Union[ModelResult, Mapping[str, ModelResult]], *, terms: Optional[Sequence[str]] = None,
    drop_intercept: bool = True, title: Optional[str] = None, out_dir: Union[str, Path] = ".",
    name: str = "coefficients", width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Coefficients with intervals, one series per model (e.g. ``{"Naive":
    ols_result, "Corrected": replicate_result}``) side by side so attenuation
    is visible. Intervals are each result's ``ci_lower``/``ci_upper``."""
    if isinstance(results, ModelResult):
        results = {results.method: results}
    rows = []
    infos = {label: interval_description(res) for label, res in results.items()}
    for label, res in results.items():
        c = res.coefficients
        info = infos[label]
        for _, row in c.iterrows():
            t = str(row["term"])
            if drop_intercept and t.lower() in ("intercept", "const", "(intercept)"):
                continue
            if terms is not None and t not in terms:
                continue
            rows.append({"model": label, "term": t, "estimate": row["estimate"],
                         "ci_lower": row["ci_lower"], "ci_upper": row["ci_upper"],
                         "se_total": row.get("se_total", math.nan),
                         "se_basis": info["basis_column"], "se_reported": row.get(info["basis_column"], math.nan),
                         "distribution": info["distribution"],
                         "df": (info["df"] or {}).get(t, math.nan) if info["df"] else math.nan,
                         "interval_defined": t not in info["undefined_terms"],
                         "confidence_level": res.confidence_level, "method": res.method,
                         "uncertainty_sources": ";".join(res.uncertainty_sources)})
    values = pd.DataFrame(rows, columns=["model", "term", "estimate", "ci_lower", "ci_upper",
                                         "se_total", "se_basis", "se_reported", "distribution", "df",
                                         "interval_defined", "confidence_level", "method",
                                         "uncertainty_sources"])
    order = list(terms) if terms is not None else list(dict.fromkeys(values["term"]))
    models = list(results.keys())
    k = len(models)
    offs = (np.arange(k) - (k - 1) / 2) * (0.6 / max(k, 1))
    values["position"] = [order.index(t) + offs[models.index(m)]
                          for t, m in zip(values["term"], values["model"])]
    fig, ax = _new_figure(width, height, max(0.6, 0.18 * len(order) + 0.4))
    with matplotlib.rc_context(STYLE):
        ax.axvline(0, color="0.6", lw=0.6, ls=":")
        for i, m in enumerate(models):
            sub = values[values["model"] == m]
            # undefined uncertainty: draw the point without whiskers (never a zero-width interval)
            err = np.nan_to_num(_err(sub["estimate"], sub["ci_lower"], sub["ci_upper"]), nan=0.0)
            ax.errorbar(sub["estimate"].to_numpy(float), sub["position"].to_numpy(float),
                        xerr=err,
                        fmt=MARKERS[i % len(MARKERS)], color=SERIES_COLORS[i % len(SERIES_COLORS)],
                        mfc="white" if i % 2 else None, ms=4, capsize=0, elinewidth=0.8,
                        ls="none", label=str(m))
        ax.set_yticks(range(len(order)), order)
        ax.invert_yaxis()
        ax.set_xlabel("Coefficient")
        if k > 1:
            ax.legend(frameon=False, loc="best")
    methods = "; ".join(f"{m}: {r.method}, {infos[m]['text']}" for m, r in results.items())
    shown = ", ".join(f"{m}: {_pct(i['level'])} {'t (per-term df)' if i['distribution'] == 't' else 'normal'} "
                      f"from {i['basis_column']}" for m, i in infos.items())
    undefined = [f"{m}: {', '.join(g['term'])}" for m, g in values[~values["interval_defined"].astype(bool)].groupby(
        "model", sort=False)]
    interval = (f"Bars show confidence intervals ({shown})"
                + (f"; no bar is drawn where uncertainty is undefined ({'; '.join(undefined)})" if undefined else ""))
    caption = _caption(title or "Model coefficients with confidence intervals", data_source, interval, methods)
    prov = _provenance("coefficient_plot", data_source, None, result_id, manifest_hash,
                       dict(provenance or {}, models={m: r.summary() for m, r in results.items()},
                            model_provenance={m: r.provenance for m, r in results.items()}))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi)


def correlation_heatmap(
    result: CorrelationResult, *, show: str = "matrix", labels: Optional[Mapping[str, str]] = None,
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "correlations",
    width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Heatmap of ``result.matrix`` (or ``"disattenuated"``), cells annotated
    with r and ``*`` where the corrected p-value is below 1 - level."""
    mat = result.disattenuated if show == "disattenuated" else result.matrix
    if mat is None:
        raise ValueError(f"result has no {show!r} matrix")
    variables = list(mat.index)
    alpha = 1 - result.confidence_level
    pmat = result.p_adjusted if result.p_adjusted is not None else result.p_values
    rows = []
    for i, a in enumerate(variables):
        for j, b in enumerate(variables):
            if j <= i:
                continue
            rows.append({"var1": a, "var2": b, "r": result.matrix.loc[a, b],
                         "ci_lower": result.ci_lower.loc[a, b], "ci_upper": result.ci_upper.loc[a, b],
                         "n": result.n.loc[a, b], "p_value": result.p_values.loc[a, b],
                         "p_adjusted": None if result.p_adjusted is None else result.p_adjusted.loc[a, b],
                         "se_replicate": None if result.se_replicate is None else result.se_replicate.loc[a, b],
                         "disattenuated": None if result.disattenuated is None else result.disattenuated.loc[a, b],
                         "significant": bool(pmat.loc[a, b] < alpha)})
    values = pd.DataFrame(rows)
    names = [(labels or {}).get(v, v) for v in variables]
    fig, ax = _new_figure(width, height, 0.85)
    with matplotlib.rc_context(STYLE):
        im = ax.imshow(mat.to_numpy(float), cmap="PuOr_r", vmin=-1, vmax=1)
        ax.set_xticks(range(len(variables)), names, rotation=45, ha="right")
        ax.set_yticks(range(len(variables)), names)
        for i, a in enumerate(variables):
            for j, b in enumerate(variables):
                v = mat.loc[a, b]
                star = "*" if i != j and pmat.loc[a, b] < alpha else ""
                txt = "" if not np.isfinite(v) else f"{v:.2f}{star}"
                ax.text(j, i, txt, ha="center", va="center", fontsize=7,
                        color="white" if np.isfinite(v) and abs(v) > 0.6 else "black")
        cb = fig.colorbar(im, ax=ax, shrink=0.8)
        cb.set_label("Disattenuated r" if show == "disattenuated" else f"{result.method.title()} r")
    corr = "corrected" if result.p_adjusted is not None else "uncorrected"
    method = (f"{result.method.title()} correlation; Fisher-z {_pct(result.confidence_level)} "
              f"intervals in the values file; * marks p < {alpha:g} ({corr})")
    if result.se_replicate is not None:
        method += "; design-based SEs from 80 ACS replicates"
    if show == "disattenuated":
        method += "; cells show Spearman-disattenuated correlations, observed r in the values file"
    caption = _caption(title or "Correlations among ACS estimates", data_source, "", method)
    prov = _provenance("correlation_heatmap", data_source, result.confidence_level, result_id,
                       manifest_hash, dict(provenance or {}, show=show,
                                           correlation=result.provenance))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi, notes=result.notes)


def forest_plot(
    comparisons: pd.DataFrame, *, label_col: Optional[str] = None, xlabel: str = "Difference",
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "forest",
    width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Forest plot of ``census.multitest.batch_z_test`` output: difference ±
    z × se_difference; filled markers are significant after correction."""
    df = comparisons.reset_index(drop=True)
    label_col = label_col or next((c for c in ("NAME", "GEOID") if c in df.columns), None)
    labels = df[label_col].astype(str) if label_col else pd.Series(df.index.astype(str))
    level = _level(float(df["confidence_level"].iloc[0])) if "confidence_level" in df else 0.90
    zc = _zcrit(level)
    diff = df["difference"].to_numpy(float)
    se = df["se_difference"].to_numpy(float)
    sig = df["significant_adjusted"].fillna(False).astype(bool).to_numpy()
    values = pd.DataFrame({"label": labels.to_numpy(), "difference": diff, "se_difference": se,
                           "ci_lower": diff - zc * se, "ci_upper": diff + zc * se,
                           "p_value": df["p_value"].to_numpy(float),
                           "p_adjusted": df["p_adjusted"].to_numpy(float),
                           "significant_adjusted": sig})
    values["position"] = np.arange(len(values), dtype=float)
    fig, ax = _new_figure(width, height, max(0.6, 0.12 * len(values) + 0.3))
    with matplotlib.rc_context(STYLE):
        ax.axvline(0, color="0.6", lw=0.6, ls=":")
        for s, mk, fill, lab in ((True, "s", OKABE_ITO[5], "Significant after correction"),
                                 (False, "o", "white", "Not significant")):
            sub = values[values["significant_adjusted"] == s]
            if sub.empty:
                continue
            ax.errorbar(sub["difference"].to_numpy(float), sub["position"].to_numpy(float),
                        xerr=_err(sub["difference"], sub["ci_lower"], sub["ci_upper"]),
                        fmt=mk, color=OKABE_ITO[5] if s else OKABE_ITO[0], mfc=fill, ms=4,
                        capsize=0, elinewidth=0.8, label=lab)
        ax.set_yticks(values["position"], values["label"])
        ax.invert_yaxis()
        ax.set_xlabel(xlabel)
        ax.legend(frameon=False, loc="best")
    correction = str(df["correction"].iloc[0]) if "correction" in df and len(df) else "none"
    family = int(df["family_size"].iloc[0]) if "family_size" in df and len(df) else len(df)
    caption = _caption(title or "Differences between estimates", data_source,
                       f"Bars show {_pct(level)} confidence intervals of the difference",
                       f"two-sample Z tests (Census statistical testing guidance); squares mark "
                       f"differences significant at {1 - level:g} after {correction} correction "
                       f"across {family} tests")
    prov = _provenance("forest_plot", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, correction=correction, family_size=family))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi)


def shrinkage_plot(
    result: SmallAreaResult, *, label_col: str = "GEOID", xlabel: str = "Estimate",
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "shrinkage",
    width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Direct estimates vs Fay-Herriot EBLUPs with intervals; a thin segment
    joins each pair to show how far the area moved."""
    level = _level(result.confidence_level)
    zc = _zcrit(level)
    e = result.estimates.reset_index(drop=True)
    direct = e["direct"].to_numpy(float)
    dse = e["direct_se"].to_numpy(float)
    values = pd.DataFrame({"label": e[label_col].astype(str).to_numpy(), "direct": direct,
                           "direct_ci_lower": direct - zc * dse, "direct_ci_upper": direct + zc * dse,
                           "eblup": e["eblup"].to_numpy(float),
                           "eblup_ci_lower": e["ci_lower"].to_numpy(float),
                           "eblup_ci_upper": e["ci_upper"].to_numpy(float),
                           "gamma": e["gamma"].to_numpy(float)})
    values["shift"] = values["eblup"] - values["direct"]
    values = values.sort_values("direct", kind="mergesort").reset_index(drop=True)
    pos = np.arange(len(values), dtype=float)
    values["position"] = pos
    fig, ax = _new_figure(width, height, max(0.6, 0.14 * len(values) + 0.3))
    with matplotlib.rc_context(STYLE):
        for i in range(len(values)):
            ax.plot([values["direct"][i], values["eblup"][i]], [pos[i] - 0.15, pos[i] + 0.15],
                    color="0.7", lw=0.5, zorder=1)
        ax.errorbar(values["direct"].to_numpy(float), pos - 0.15,
                    xerr=_err(values["direct"], values["direct_ci_lower"], values["direct_ci_upper"]),
                    fmt="o", mfc="white", color=OKABE_ITO[0], ms=4, elinewidth=0.8, label="Direct")
        ax.errorbar(values["eblup"].to_numpy(float), pos + 0.15,
                    xerr=_err(values["eblup"], values["eblup_ci_lower"], values["eblup_ci_upper"]),
                    fmt="s", color=OKABE_ITO[6], ms=4, elinewidth=0.8, label="EBLUP")
        ax.set_yticks(pos, values["label"])
        ax.set_xlabel(xlabel)
        ax.legend(frameon=False, loc="best")
    caption = _caption(title or "Direct survey estimates and Fay-Herriot EBLUPs", data_source,
                       f"Bars show {_pct(level)} intervals (direct: estimate ± {zc:.3f} × SE; "
                       f"EBLUP: from {result.mse_method} MSE)",
                       f"Fay-Herriot model ({result.method}; variance component by "
                       f"{result.variance_method}, sigma2_u = {result.sigma2_u:.4g})")
    prov = _provenance("shrinkage_plot", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, small_area=result.provenance))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi, notes=result.notes)


def trend_plot(
    frame: pd.DataFrame, period_col: str, estimate_col: str, *, se_col: Optional[str] = None,
    moe_col: Optional[str] = None, group_col: Optional[str] = None, confidence_level: float = 0.90,
    trend_test: Optional[Mapping[str, Any]] = None, ylabel: Optional[str] = None,
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "trend",
    width: Union[str, float] = "one", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Estimates by period (5-year end year) with intervals, one line per
    group. ``trend_test`` (e.g. ``{"slope": .., "p_value": ..}``) is quoted in
    the caption. Overlapping 5-year periods are flagged in the caption."""
    level = _level(confidence_level)
    zc = _zcrit(level)
    est = pd.to_numeric(frame[estimate_col], errors="coerce").to_numpy(float)
    se = _se_from(frame, se_col, moe_col)
    values = pd.DataFrame({"group": frame[group_col].astype(str).to_numpy() if group_col else "all",
                           "period": pd.to_numeric(frame[period_col]).to_numpy(), "estimate": est,
                           "se": se, "ci_lower": est - zc * se, "ci_upper": est + zc * se})
    values = values.sort_values(["group", "period"], kind="mergesort").reset_index(drop=True)
    fig, ax = _new_figure(width, height, 0.75)
    overlap = False
    with matplotlib.rc_context(STYLE):
        for i, (g, sub) in enumerate(values.groupby("group", sort=True)):
            if (np.diff(sub["period"].to_numpy(float)) < 5).any():
                overlap = True
            ax.errorbar(sub["period"].to_numpy(float), sub["estimate"].to_numpy(float),
                        yerr=_err(sub["estimate"], sub["ci_lower"], sub["ci_upper"]),
                        fmt=MARKERS[i % len(MARKERS)], ls=LINESTYLES[i % len(LINESTYLES)],
                        color=OKABE_ITO[i % 8], ms=4, capsize=2, elinewidth=0.8,
                        label=None if g == "all" else g)
        ax.set_xlabel("ACS 5-year period (end year)")
        ax.set_ylabel(ylabel or estimate_col)
        if group_col:
            ax.legend(frameon=False, loc="best")
    method = "ACS 5-year estimates by period"
    if trend_test:
        method += "; trend test: " + ", ".join(
            f"{k} = {v:.4g}" if isinstance(v, float) else f"{k} = {v}" for k, v in trend_test.items())
    if overlap:
        method += ("; WARNING: some plotted 5-year periods overlap, so adjacent estimates share "
                   "sample years and are not independent")
    caption = _caption(title or f"Trend in {estimate_col}", data_source,
                       f"Bars show {_pct(level)} confidence intervals", method)
    prov = _provenance("trend_plot", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, trend_test=dict(trend_test or {}), overlapping_periods=overlap))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi)


def reliability_profile_plot(
    frame: pd.DataFrame, level_col: str, cv_col: str, *,
    thresholds: Sequence[float] = CV_THRESHOLDS, title: Optional[str] = None,
    out_dir: Union[str, Path] = ".", name: str = "reliability", width: Union[str, float] = "one",
    height: Optional[float] = None, formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300,
    data_source: str = "", result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Box plots of the CV (percent) by geography level, with dashed
    reliability thresholds (default 12% and 30%)."""
    df = frame[[level_col, cv_col]].copy()
    df[cv_col] = pd.to_numeric(df[cv_col], errors="coerce")
    levels = list(dict.fromkeys(df[level_col].astype(str)))
    groups = [df.loc[df[level_col].astype(str) == lv, cv_col].dropna().to_numpy(float) for lv in levels]
    rows = []
    for lv, g in zip(levels, groups):
        row = {"level": lv, "n": int(g.size), "median_cv": float(np.median(g)) if g.size else math.nan,
               "q1_cv": float(np.percentile(g, 25)) if g.size else math.nan,
               "q3_cv": float(np.percentile(g, 75)) if g.size else math.nan}
        for t in thresholds:
            row[f"share_cv_above_{t:g}"] = float(np.mean(g > t)) if g.size else math.nan
        rows.append(row)
    values = pd.DataFrame(rows)
    fig, ax = _new_figure(width, height, 0.75)
    with matplotlib.rc_context(STYLE):
        ax.boxplot(groups, showfliers=True, medianprops={"color": OKABE_ITO[6]},
                   flierprops={"marker": "o", "ms": 2, "mfc": "none"})
        ax.set_xticks(range(1, len(levels) + 1), levels)
        for i, t in enumerate(thresholds):
            ax.axhline(t, color="0.4", lw=0.7, ls=LINESTYLES[(i + 1) % len(LINESTYLES)],
                       label=f"CV = {t:g}%")
        ax.set_xlabel("Geography level")
        ax.set_ylabel("Coefficient of variation (%)")
        ax.legend(frameon=False, loc="best")
    caption = _caption(title or "Reliability of estimates by geography level", data_source,
                       "", "CV = SE / estimate × 100, SE from the published 90% MOE / 1.645; "
                       "reference lines at " + ", ".join(f"{t:g}%" for t in thresholds))
    prov = _provenance("reliability_profile_plot", data_source, None, result_id, manifest_hash,
                       dict(provenance or {}, thresholds=list(thresholds)))
    return _finish(fig, name, out_dir, values, caption, prov, formats, dpi)


for _f in (estimates_with_intervals, coefficient_plot, correlation_heatmap, forest_plot,
           shrinkage_plot, trend_plot, reliability_profile_plot):
    _f.__doc__ = (_f.__doc__ or "") + _COMMON_DOC
