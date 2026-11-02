# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publication tables and results bundles (PLAN_ROUND_8.md, I3).

Regression, descriptive, comparison and correlation tables, each rendered as
LaTeX (booktabs), Markdown and CSV. The CSV holds the unrounded values
(estimates, SEs, intervals, raw and corrected p-values); LaTeX and Markdown
hold the formatted display with significance markers assigned from the
p-values *after* multiple-comparison correction, notes on which sources of
uncertainty the SEs include, and the data citation.

``results_bundle`` writes tables, figures, captions, a methods section and
provenance into one folder. (``census.tables`` is unrelated: it is the ACS
table catalog.)
"""

import hashlib
import json
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
from scipy import stats

from .multitest import adjust_pvalues
from .results import CorrelationResult, ModelResult
from .variance import Z90

STARS: Tuple[Tuple[float, str], ...] = ((0.01, "***"), (0.05, "**"), (0.10, "*"))
_INTERCEPTS = ("intercept", "const", "(intercept)")


def data_citation(releases: Union[str, Sequence[Any]] = (), tables: Sequence[str] = ()) -> str:
    """Citation string for ACS 5-year data, e.g. releases ``[2023]``."""
    rel = [releases] if isinstance(releases, (str, int)) else list(releases)
    text = "U.S. Census Bureau, American Community Survey 5-Year Estimates"
    if rel:
        text += ", " + ", ".join(f"{int(r) - 4}-{r}" if str(r).isdigit() else str(r) for r in rel)
    if tables:
        text += "; tables " + ", ".join(tables)
    return text + ". Retrieved via the Census Data API (https://api.census.gov)."


def stars(p_adjusted: float, thresholds: Sequence[Tuple[float, str]] = STARS) -> str:
    if p_adjusted is None or not np.isfinite(p_adjusted):
        return ""
    for t, mark in thresholds:
        if p_adjusted < t:
            return mark
    return ""


def _fmt(x: Any, digits: int) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "" if x is None else str(x)
    if not np.isfinite(v):
        return ""
    return f"{v:,.{digits}f}"


def _fmt_p(p: Any) -> str:
    try:
        v = float(p)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(v):
        return ""
    return "<0.001" if v < 0.001 else f"{v:.3f}"


_LATEX_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
              "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\^{}"}


def latex_escape(text: Any) -> str:
    return "".join(_LATEX_ESC.get(c, c) for c in str(text))


@dataclass
class ReportTable:
    """``display`` (strings) is rendered; ``data`` (numbers) goes to CSV."""

    title: str
    display: pd.DataFrame
    data: pd.DataFrame
    notes: List[str] = field(default_factory=list)
    citation: str = ""
    label: str = ""

    def to_latex(self) -> str:
        cols = list(self.display.columns)
        lines = [r"\begin{table}[htbp]", r"\centering", rf"\caption{{{latex_escape(self.title)}}}"]
        if self.label:
            lines.append(rf"\label{{{self.label}}}")
        lines += [r"\begin{tabular}{l" + "r" * (len(cols) - 1) + "}", r"\toprule",
                  " & ".join(latex_escape(c) for c in cols) + r" \\", r"\midrule"]
        for _, row in self.display.iterrows():
            lines.append(" & ".join(latex_escape(v) for v in row.tolist()) + r" \\")
        lines += [r"\bottomrule", r"\end{tabular}"]
        foot = self._footer()
        if foot:
            lines.append(r"\par\smallskip\footnotesize")
            lines += [latex_escape(f) + r"\par" for f in foot]
        lines.append(r"\end{table}")
        return "\n".join(lines) + "\n"

    def to_markdown(self) -> str:
        cols = list(self.display.columns)
        cell = lambda v: str(v).replace("|", r"\|")  # noqa: E731
        lines = [f"**{self.title}**", "", "| " + " | ".join(cell(c) for c in cols) + " |",
                 "| :--- |" + " ---: |" * (len(cols) - 1)]
        for _, row in self.display.iterrows():
            lines.append("| " + " | ".join(cell(v) for v in row.tolist()) + " |")
        foot = self._footer()
        if foot:
            lines += [""] + [f"*{f}*" if i == 0 else f for i, f in enumerate(foot)]
        return "\n".join(lines) + "\n"

    def to_csv(self) -> str:
        return self.data.to_csv(index=False, lineterminator="\n")

    def _footer(self) -> List[str]:
        out = [f"Note: {n}" if i == 0 else n for i, n in enumerate(self.notes)]
        if self.citation:
            out.append(f"Source: {self.citation}")
        return out

    def write(self, folder: Union[str, Path], name: str) -> Dict[str, str]:
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        paths = {}
        for ext, text in (("tex", self.to_latex()), ("md", self.to_markdown()), ("csv", self.to_csv())):
            p = out / f"{name}.{ext}"
            p.write_text(text, encoding="utf-8", newline="\n")
            paths[ext] = str(p)
        return paths


def _star_note(correction: Optional[str], families: str) -> str:
    marks = ", ".join(f"{m} p < {t:g}" for t, m in STARS)
    if correction:
        return f"Significance markers use {correction}-adjusted p-values ({families}): {marks}."
    return f"Significance markers use unadjusted p-values: {marks}."


def regression_table(
    results: Union[ModelResult, Mapping[str, ModelResult]], *, uncertainty: str = "se",
    correction: Optional[str] = "holm", include_intercept_in_family: bool = False,
    digits: int = 3, title: str = "Regression results", citation: str = "", label: str = "",
) -> ReportTable:
    """One column per model; each term shows the estimate with markers and,
    beneath it, ``(SE)`` (``uncertainty="se"``) or ``[lower, upper]``
    (``"ci"``). p-values are adjusted within each model across its terms
    (excluding the intercept unless ``include_intercept_in_family``)."""
    if isinstance(results, ModelResult):
        results = {"Model": results}
    if uncertainty not in ("se", "ci"):
        raise ValueError("uncertainty must be 'se' or 'ci'")
    data_rows, terms = [], []
    for mname, res in results.items():
        c = res.coefficients.reset_index(drop=True)
        p = pd.to_numeric(c["p_value"], errors="coerce").to_numpy(float)
        is_int = c["term"].astype(str).str.lower().isin(_INTERCEPTS).to_numpy()
        fam = np.ones(len(c), bool) if include_intercept_in_family else ~is_int
        p_adj = np.full(len(c), np.nan)
        if correction:
            p_adj[fam] = np.asarray(adjust_pvalues(p[fam], correction), dtype=float)
        else:
            p_adj = p.copy()
        for i, row in c.iterrows():
            t = str(row["term"])
            if t not in terms:
                terms.append(t)
            data_rows.append({"model": mname, "term": t, "estimate": row["estimate"],
                              "se_model": row.get("se_model"), "se_sampling": row.get("se_sampling"),
                              "se_total": row.get("se_total"), "ci_lower": row["ci_lower"],
                              "ci_upper": row["ci_upper"], "p_value": p[i], "p_adjusted": p_adj[i],
                              "marker": stars(p_adj[i]), "confidence_level": res.confidence_level,
                              "method": res.method, "n_obs": res.n_obs})
    data = pd.DataFrame(data_rows)
    models = list(results)
    disp = []
    for t in terms:
        top, bottom = {"Term": t}, {"Term": ""}
        for m in models:
            r = data[(data["model"] == m) & (data["term"] == t)]
            if r.empty:
                top[m] = bottom[m] = ""
                continue
            r = r.iloc[0]
            top[m] = _fmt(r["estimate"], digits) + r["marker"]
            bottom[m] = (f"({_fmt(r['se_total'], digits)})" if uncertainty == "se"
                         else f"[{_fmt(r['ci_lower'], digits)}, {_fmt(r['ci_upper'], digits)}]")
        disp += [top, bottom]
    disp.append({"Term": "N", **{m: f"{results[m].n_obs:,}" for m in models}})
    notes = []
    for m, res in results.items():
        se_desc = "standard errors" if uncertainty == "se" else \
            f"{res.confidence_level * 100:g}% confidence intervals"
        notes.append(f"{m}: {res.method}; {se_desc} include {', '.join(res.uncertainty_sources)}"
                     + (f" ({'; '.join(res.notes)})" if res.notes else "") + ".")
    if uncertainty == "se":
        notes.append("Standard errors in parentheses (se_total; components se_model and "
                     "se_sampling in the CSV).")
    else:
        notes.append("Confidence intervals in brackets.")
    notes.append(_star_note(correction, "across each model's coefficients"
                            + ("" if include_intercept_in_family else ", intercept excluded")))
    return ReportTable(title, pd.DataFrame(disp, columns=["Term"] + models).fillna(""), data,
                       notes, citation, label)


def descriptive_table(
    frame: pd.DataFrame, variables: Sequence[str], *, labels: Optional[Mapping[str, str]] = None,
    cv_threshold: float = 30.0, digits: int = 2, title: str = "Descriptive statistics",
    citation: str = "", label: str = "",
) -> ReportTable:
    """Per variable: n, mean, SD, min, median, max, median sampling SE and CV
    (from ``v_se``/``v_cv`` when present) and the share with CV above the threshold."""
    rows = []
    for v in variables:
        x = pd.to_numeric(frame[v], errors="coerce").dropna()
        se = pd.to_numeric(frame[f"{v}_se"], errors="coerce") if f"{v}_se" in frame else None
        if f"{v}_cv" in frame:
            cv = pd.to_numeric(frame[f"{v}_cv"], errors="coerce")
        elif se is not None:
            cv = se / pd.to_numeric(frame[v], errors="coerce").abs() * 100
        else:
            cv = None
        rows.append({"variable": v, "label": (labels or {}).get(v, v), "n": int(x.size),
                     "mean": x.mean(), "sd": x.std(ddof=1), "min": x.min(), "median": x.median(),
                     "max": x.max(), "median_se": None if se is None else se.median(),
                     "median_cv_percent": None if cv is None else cv.median(),
                     f"share_cv_above_{cv_threshold:g}": None if cv is None
                     else float((cv.dropna() > cv_threshold).mean()) if cv.notna().any() else None})
    data = pd.DataFrame(rows)
    share = f"share_cv_above_{cv_threshold:g}"
    display = pd.DataFrame({
        "Variable": data["label"], "N": data["n"].map(lambda n: f"{n:,}"),
        "Mean": data["mean"].map(lambda v: _fmt(v, digits)), "SD": data["sd"].map(lambda v: _fmt(v, digits)),
        "Min": data["min"].map(lambda v: _fmt(v, digits)), "Median": data["median"].map(lambda v: _fmt(v, digits)),
        "Max": data["max"].map(lambda v: _fmt(v, digits)),
        "Median SE": data["median_se"].map(lambda v: _fmt(v, digits)),
        "Median CV (%)": data["median_cv_percent"].map(lambda v: _fmt(v, 1)),
        f"CV > {cv_threshold:g}% (%)": data[share].map(lambda v: _fmt(None if v is None else v * 100, 1)),
    })
    notes = ["SD is the between-unit standard deviation; Median SE is the median sampling standard "
             "error of the unit estimates (published 90% MOE / 1.645 or derived); CV = SE / |estimate| x 100."]
    return ReportTable(title, display, data, notes, citation, label)


def comparison_table(
    comparisons: pd.DataFrame, *, label_col: Optional[str] = None, digits: int = 2,
    title: str = "Comparisons of estimates", citation: str = "", label: str = "",
) -> ReportTable:
    """Formats ``census.multitest.batch_z_test`` output: difference with
    markers (from its corrected p-values), interval, Z, raw and adjusted p."""
    df = comparisons.reset_index(drop=True)
    label_col = label_col or next((c for c in ("NAME", "GEOID") if c in df.columns), None)
    level = float(df["confidence_level"].iloc[0]) if "confidence_level" in df and len(df) else 0.90
    zc = Z90 if math.isclose(level, 0.90) else float(stats.norm.ppf(1 - (1 - level) / 2))
    diff, se = df["difference"].astype(float), df["se_difference"].astype(float)
    data = pd.DataFrame({"label": df[label_col].astype(str) if label_col else df.index.astype(str),
                         "difference": diff, "se_difference": se, "ci_lower": diff - zc * se,
                         "ci_upper": diff + zc * se, "z": df["z"].astype(float),
                         "p_value": df["p_value"].astype(float), "p_adjusted": df["p_adjusted"].astype(float),
                         "significant_adjusted": df["significant_adjusted"]})
    data["marker"] = data["p_adjusted"].map(stars)
    display = pd.DataFrame({
        "Comparison": data["label"],
        "Difference": [_fmt(d, digits) + m for d, m in zip(data["difference"], data["marker"])],
        f"{level * 100:g}% CI": [f"[{_fmt(a, digits)}, {_fmt(b, digits)}]"
                                 for a, b in zip(data["ci_lower"], data["ci_upper"])],
        "Z": data["z"].map(lambda v: _fmt(v, 2)), "p": data["p_value"].map(_fmt_p),
        "Adjusted p": data["p_adjusted"].map(_fmt_p),
    })
    correction = str(df["correction"].iloc[0]) if "correction" in df and len(df) else None
    family = int(df["family_size"].iloc[0]) if "family_size" in df and len(df) else len(df)
    notes = [f"Two-sample Z tests, SE = MOE / 1.645 (Census statistical testing guidance); "
             f"overlapping 5-year periods adjusted where noted.",
             _star_note(correction, f"across {family} tests")]
    return ReportTable(title, display, data, notes, citation, label)


def correlation_table(
    result: CorrelationResult, *, labels: Optional[Mapping[str, str]] = None, digits: int = 2,
    title: str = "Correlations", citation: str = "", label: str = "",
) -> ReportTable:
    """Lower-triangular correlation table with markers from adjusted p-values;
    long-form CSV with intervals, replicate SEs and disattenuated values."""
    vs = list(result.matrix.index)
    pm = result.p_adjusted if result.p_adjusted is not None else result.p_values
    rows = []
    for i, a in enumerate(vs):
        for b in vs[:i]:
            g = lambda m: None if m is None else m.loc[a, b]  # noqa: E731
            rows.append({"var1": a, "var2": b, "r": result.matrix.loc[a, b], "n": result.n.loc[a, b],
                         "ci_lower": result.ci_lower.loc[a, b], "ci_upper": result.ci_upper.loc[a, b],
                         "p_value": result.p_values.loc[a, b], "p_adjusted": g(result.p_adjusted),
                         "se_replicate": g(result.se_replicate), "disattenuated": g(result.disattenuated),
                         "marker": stars(pm.loc[a, b])})
    data = pd.DataFrame(rows)
    names = [(labels or {}).get(v, v) for v in vs]
    disp = []
    for i, a in enumerate(vs):
        row = {"Variable": f"{i + 1}. {names[i]}"}
        for j, b in enumerate(vs):
            row[str(j + 1)] = ("1" if i == j else "" if j > i else
                               _fmt(result.matrix.loc[a, b], digits) + stars(pm.loc[a, b]))
        disp.append(row)
    notes = [f"{result.method.title()} correlations; Fisher-z {result.confidence_level * 100:g}% "
             "intervals, replicate SEs and disattenuated values in the CSV.",
             _star_note(result.provenance.get("correction") if result.p_adjusted is not None else None,
                        f"across {len(rows)} pairs")]
    return ReportTable(title, pd.DataFrame(disp), data, notes, citation, label)


FigureSpec = Union[Any, Tuple[Callable[..., Any], Mapping[str, Any]]]


def results_bundle(
    folder: Union[str, Path], *, tables: Optional[Mapping[str, ReportTable]] = None,
    figures: Optional[Mapping[str, FigureSpec]] = None, methods: Optional[str] = None,
    results: Optional[Mapping[str, Any]] = None, provenance: Optional[Mapping[str, Any]] = None,
    citation: str = "",
) -> Dict[str, Any]:
    """Write one analysis's outputs into ``folder``:

    ``tables/<name>.{tex,md,csv}``, ``figures/<name>.*`` (each figure given
    as a ``census.figures.FigureOutput``, whose files are copied, or as
    ``(figure_function, kwargs)``, which is rendered there), ``captions.md``,
    ``methods.md`` (when given), ``results/<name>.json`` (``to_dict()`` of
    result objects), and ``bundle.json`` listing every file with its SHA-256,
    the citation and ``provenance``. Returns the bundle manifest.
    """
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Dict[str, str]] = {"tables": {}, "figures": {}, "results": {}}
    for name, table in (tables or {}).items():
        if citation and not table.citation:
            table.citation = citation
        written["tables"][name] = table.write(root / "tables", name)
    captions = []
    fig_dir = root / "figures"
    for name, spec in (figures or {}).items():
        if isinstance(spec, tuple):
            func, kwargs = spec
            out = func(**dict(kwargs, out_dir=fig_dir, name=name))
        else:
            out = spec
            fig_dir.mkdir(parents=True, exist_ok=True)
            for p in out.paths.values():
                src = Path(p)
                dst = fig_dir / f"{name}{src.name[len(out.name):]}"
                if src.resolve() != dst.resolve():
                    shutil.copyfile(src, dst)
        written["figures"][name] = {k: str(fig_dir / f"{name}{Path(v).name[len(out.name):]}")
                                    for k, v in out.paths.items()}
        captions.append(f"**Figure: {name}.** {out.caption}")
    if captions:
        (root / "captions.md").write_text("\n\n".join(captions) + "\n", encoding="utf-8", newline="\n")
    if methods:
        (root / "methods.md").write_text(methods.rstrip() + "\n", encoding="utf-8", newline="\n")
    for name, res in (results or {}).items():
        d = res.to_dict() if hasattr(res, "to_dict") else res
        p = root / "results" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2, sort_keys=True, default=str) + "\n",
                     encoding="utf-8", newline="\n")
        written["results"][name] = str(p)
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "bundle.json":
            files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = {"citation": citation, "provenance": dict(provenance or {}),
                "tables": sorted(written["tables"]), "figures": sorted(written["figures"]),
                "results": sorted(written["results"]), "has_methods": bool(methods), "files": files}
    (root / "bundle.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
                                      encoding="utf-8", newline="\n")
    manifest["paths"] = written
    return manifest
