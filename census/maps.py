# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static maps that show ACS sampling error (PLAN_ROUND_9.md, workstream B).

* ``choropleth``: estimates classed by quantiles (default, k = 5), natural
  breaks (Fisher-Jenks), equal interval, or class-separability breaks; the
  legend reports class separability and estimates with CV above a threshold
  (default 30%) are hatched.
* ``uncertainty_map``: companion CV map (<= 12%, 12-30%, > 30%).
* ``significance_map``: ``census.multitest.batch_z_test`` results as
  significant increase / decrease / no significant change after the
  multiple-comparison correction, never raw differences.

National maps place Alaska, Hawaii and Puerto Rico in insets, each in its
own equal-area projection (``census.boundaries.equal_area_crs``); CONUS uses
EPSG:5070. Palettes are colorblind-safe (ColorBrewer sequential, Okabe-Ito
for categories) and categories that differ only by hue also differ by hatch,
so maps survive grayscale printing.

Output contract is that of ``census.figures``: vector (PDF/SVG/EPS) and PNG,
``<name>.csv`` with the mapped values (no geometry), a caption draft naming
the ACS release, boundary vintage, classification and separability, and a
provenance JSON with the SHA-256 of every file; bytes are reproducible.

Class separability (``class_separability``)
-------------------------------------------
For classes ``1..k`` and each pair of adjacent classes ``(c, c+1)``, every
pair of areas ``(i, j)`` with ``i`` in class ``c`` and ``j`` in class ``c+1``
is *separable* when their intervals do not overlap: ``U_i < L_j`` with
``U = est + z*SE``, ``L = est - z*SE`` and ``z = 1.645`` at the default 90%
level. Separability is the share of all such adjacent-class pairs, pooled
over the ``k - 1`` adjacent class pairs, that are separable:

    S = sum_c #{(i, j) in C_c x C_{c+1}: U_i < L_j} / sum_c |C_c| |C_{c+1}|

S = 1 means every area is statistically distinguishable (at the interval
level) from every area in the neighbouring classes; S near 0 means the class
boundaries are largely within sampling error. Per-boundary values are also
reported. Areas without a finite SE are excluded from the pairs. This follows
the class-separability idea of Sun and Wong (2010, "Incorporating data
quality information in mapping American Community Survey data", Cartography
and Geographic Information Science 37(4): 285-299) and Koo, Chun and
Griffith (2017, "Optimal map classification incorporating uncertainty
information", Annals of the American Association of Geographers 107(3):
575-590); the pairwise share is this toolkit's summary statistic.

Separability breaks (``scheme="separability"``) choose the k - 1 breaks that
minimise the number of areas whose 90% interval straddles a break (an
interval straddles break b when L_i < b < U_i), subject to every class
holding at least ``min_class_size`` areas (default n / (2k)); solved exactly
by dynamic programming over midpoints between sorted estimates. It is a
straddle-minimising variant of the Sun-Wong / Koo-Chun-Griffith criteria,
not a reimplementation of either paper's algorithm.
"""

import math
import textwrap
import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from . import boundaries as B
from .figures import (DEFAULT_FORMATS, JOURNAL_WIDTHS, OKABE_ITO, STYLE, FigureOutput, _caption,
                      _finish, _level, _pct, _provenance, _zcrit)
from .variance import Z90

SCHEMES = ("quantiles", "natural_breaks", "equal_interval", "separability")
SCHEME_LABELS = {"quantiles": "quantiles", "natural_breaks": "natural breaks (Fisher-Jenks)",
                 "equal_interval": "equal intervals", "separability": "class-separability breaks"}
CV_CLASSES = (12.0, 30.0)
NO_DATA_COLOR = "#D9D9D9"
HATCH_LOW = "////"
HATCH_COLOR = "#7F7F7F"  # mid gray: visible on light and dark fills
EDGE = "#FFFFFF"
SIG_COLORS = {"increase": OKABE_ITO[6], "decrease": OKABE_ITO[5],
              "no significant change": "#EDEDED", "not tested": "#FFFFFF"}
SIG_HATCH = {"increase": None, "decrease": "....", "no significant change": None, "not tested": "xxx"}
CV_COLORS = ("#FEE8C8", "#FDBB84", "#E34A33")  # ColorBrewer OrRd-3
# Inset placement as figure fractions (left, bottom, width, height).
INSET_BOXES = {"alaska": (0.01, 0.02, 0.24, 0.24), "hawaii": (0.25, 0.02, 0.13, 0.13),
               "puerto_rico": (0.38, 0.02, 0.09, 0.09)}


# --------------------------------------------------------------------------- statistics

def _intervals(est: np.ndarray, se: np.ndarray, z: float) -> Tuple[np.ndarray, np.ndarray]:
    return est - z * se, est + z * se


def class_separability(est: Sequence[float], se: Sequence[float], classes: Sequence[Any], k: int,
                       confidence_level: float = 0.90) -> Dict[str, Any]:
    """Share of adjacent-class area pairs whose intervals do not overlap
    (see the module docstring). ``classes`` are 0-based class indices (NaN
    or negative for unclassified areas). Returns ``separability`` (pooled),
    ``by_boundary`` (one entry per adjacent class pair), ``pairs`` and
    ``separable_pairs``."""
    z = _zcrit(_level(confidence_level))
    e = np.asarray(est, float)
    s = np.asarray(se, float)
    c = pd.to_numeric(pd.Series(classes), errors="coerce").to_numpy(float)
    ok = np.isfinite(e) & np.isfinite(s) & np.isfinite(c) & (c >= 0)
    lo, hi = _intervals(e, s, z)
    by, tot_pairs, tot_sep = [], 0, 0
    for b in range(k - 1):
        a_mask, b_mask = ok & (c == b), ok & (c == b + 1)
        ua = np.sort(hi[a_mask])
        lb = lo[b_mask]
        pairs = int(ua.size * lb.size)
        sep = int(np.searchsorted(ua, lb, side="left").sum()) if pairs else 0
        by.append({"lower_class": b + 1, "upper_class": b + 2, "pairs": pairs, "separable_pairs": sep,
                   "separability": sep / pairs if pairs else math.nan})
        tot_pairs += pairs
        tot_sep += sep
    return {"separability": tot_sep / tot_pairs if tot_pairs else math.nan, "pairs": tot_pairs,
            "separable_pairs": tot_sep, "by_boundary": by, "confidence_level": _level(confidence_level),
            "n_excluded": int((~ok & np.isfinite(e)).sum())}


def separability_breaks(est: Sequence[float], se: Sequence[float], k: int = 5,
                        confidence_level: float = 0.90, min_class_size: Optional[int] = None) -> np.ndarray:
    """Upper class bounds (mapclassify convention, last = max) minimising
    the number of interval-straddled breaks; see the module docstring."""
    e = np.asarray(est, float)
    s = np.asarray(se, float)
    ok = np.isfinite(e)
    e, s = e[ok], np.where(np.isfinite(s[ok]), s[ok], 0.0)
    n = e.size
    if n == 0:
        raise ValueError("no finite estimates to classify")
    k = int(min(k, np.unique(e).size))
    if k < 2:
        return np.array([e.max()])
    m = max(1, int(min_class_size if min_class_size is not None else n // (2 * k)))
    if m * k > n:
        m = max(1, n // k)
    order = np.argsort(e, kind="mergesort")
    xs = e[order]
    z = _zcrit(_level(confidence_level))
    lo, hi = np.sort(e - z * s), np.sort(e + z * s)
    # Break position p (1..n-1) splits xs[:p] | xs[p:] at the midpoint.
    p = np.arange(1, n)
    mid = (xs[p - 1] + xs[p]) / 2.0
    cost = (np.searchsorted(lo, mid, side="left") - np.searchsorted(hi, mid, side="right")).astype(float)
    cost[xs[p - 1] == xs[p]] = np.inf  # cannot split tied values
    cost = np.concatenate([[np.inf], cost])  # index by p; p=0 invalid
    INF = np.inf
    best = np.full((k, n), INF)
    arg = np.zeros((k, n), dtype=int)
    # t = 1st break at position p needs p >= m.
    best[1, m:n] = cost[m:n]
    for t in range(2, k):
        prev = best[t - 1]
        # best over p' <= p - m
        cm = np.minimum.accumulate(prev)
        # running argmin: index of the first position attaining the prefix minimum
        is_new = np.concatenate([[True], prev[1:] < cm[:-1]])
        ca = np.maximum.accumulate(np.where(is_new, np.arange(n), 0))
        pos = np.arange(m + 1, n)
        q = pos - m
        best[t, pos] = cm[q] + cost[pos]
        arg[t, pos] = ca[q]
    last = best[k - 1].copy()
    last[n - m + 1:] = INF
    if not np.isfinite(last).any():
        raise ValueError("cannot form classes of the minimum size with tied values; lower k")
    pos = int(np.argmin(last))
    breaks = [pos]
    for t in range(k - 1, 1, -1):
        pos = int(arg[t, pos])
        breaks.append(pos)
    breaks = sorted(breaks)
    return np.array([xs[b - 1] for b in breaks] + [xs[-1]], dtype=float)


def class_breaks(est: Sequence[float], scheme: str = "quantiles", k: int = 5,
                 se: Optional[Sequence[float]] = None, confidence_level: float = 0.90) -> np.ndarray:
    """Upper class bounds for ``scheme`` (``mapclassify`` for quantiles,
    Fisher-Jenks natural breaks and equal interval)."""
    import mapclassify

    if scheme not in SCHEMES:
        raise ValueError(f"scheme must be one of {SCHEMES}")
    y = np.asarray(est, float)
    y = y[np.isfinite(y)]
    if y.size == 0:
        raise ValueError("no finite estimates to classify")
    if scheme == "separability":
        if se is None:
            raise ValueError("separability breaks need standard errors")
        e = np.asarray(est, float)
        keep = np.isfinite(e)
        return separability_breaks(e[keep], np.asarray(se, float)[keep], k, confidence_level)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # mapclassify warns when fewer unique values than k
        if scheme == "quantiles":
            bins = mapclassify.Quantiles(y, k=k).bins
        elif scheme == "natural_breaks":
            bins = mapclassify.FisherJenks(y, k=min(k, np.unique(y).size)).bins
        else:
            bins = mapclassify.EqualInterval(y, k=k).bins
    return np.asarray(bins, float)


def assign_classes(est: Sequence[float], bins: Sequence[float]) -> np.ndarray:
    """0-based class of each estimate (x <= bin), -1 for missing."""
    e = np.asarray(est, float)
    cls = np.searchsorted(np.asarray(bins, float), e, side="left")
    cls = np.minimum(cls, len(bins) - 1)
    return np.where(np.isfinite(e), cls, -1)


def sequential_colors(k: int, cmap: str = "YlGnBu") -> List[str]:
    """k colors from a colorblind-safe sequential colormap (ColorBrewer
    YlGnBu by default; viridis/cividis also suitable)."""
    cm = matplotlib.colormaps[cmap]
    return [matplotlib.colors.to_hex(cm(x)) for x in np.linspace(0.12, 0.95, k)]


# --------------------------------------------------------------------------- drawing

def _se(gdf, se_col: Optional[str], moe_col: Optional[str]) -> np.ndarray:
    if se_col:
        return pd.to_numeric(gdf[se_col], errors="coerce").to_numpy(float)
    if moe_col:
        m = pd.to_numeric(gdf[moe_col], errors="coerce").to_numpy(float)
        m = np.where(m < 0, np.nan, m)  # Census sentinel MOEs
        return m / Z90
    raise ValueError("give se_col or moe_col")


def _cv(est: np.ndarray, se: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(est != 0, se / np.abs(est) * 100.0, np.nan)


def _layout(gdf, insets: Union[bool, str]) -> Dict[str, Any]:
    regions = B.region_of(gdf)
    present = set(regions)
    use_insets = (insets is True or insets == "auto") and "conus" in present and \
        bool(present & {"alaska", "hawaii", "puerto_rico"})
    return {"regions": regions, "insets": use_insets, "present": present}


def _new_map(width, height) -> Figure:
    w = JOURNAL_WIDTHS[width] if isinstance(width, str) else float(width)
    with matplotlib.rc_context(STYLE):
        return Figure(figsize=(w, height if height is not None else w * 0.68))


def _draw(fig: Figure, gdf, layers, insets: Union[bool, str]) -> Tuple[Any, List[str], List[str]]:
    """Draw ``layers(ax, sub)`` per region. Returns (main_ax, crs_list, notes)."""
    lay = _layout(gdf, insets)
    regions = lay["regions"]
    notes: List[str] = []
    crs_used: List[str] = []
    if gdf.crs is None:
        raise ValueError("GeoDataFrame has no CRS")
    with matplotlib.rc_context(STYLE):
        main = fig.add_subplot(1, 1, 1)
        if lay["insets"]:
            dropped = int((regions == "other").sum())
            if dropped:
                notes.append(f"{dropped} island-area features (GU, VI, AS, MP) not drawn")
            sub = gdf[regions == "conus"]
            crs = B.crs_for_region("conus")
            layers(main, sub.to_crs(crs))
            crs_used.append(f"conus: {crs.to_string()}")
            for reg in ("alaska", "hawaii", "puerto_rico"):
                part = gdf[regions == reg]
                if part.empty:
                    continue
                ax = fig.add_axes(INSET_BOXES[reg])
                crs = B.crs_for_region(reg)
                layers(ax, part.to_crs(crs))
                crs_used.append(f"{reg}: {crs.to_string()}")
                ax.set_axis_off()
                ax.set_aspect("equal")
            notes.append("Alaska, Hawaii and Puerto Rico shown in insets, not to scale with CONUS")
        else:
            crs = B.equal_area_crs(gdf)
            layers(main, gdf.to_crs(crs))
            crs_used.append(crs.to_string() if crs.to_epsg() is None else f"EPSG:{crs.to_epsg()}")
        main.set_axis_off()
        main.set_aspect("equal")
        _scale_bar(main, right=lay["insets"])  # insets occupy the lower left
    return main, crs_used, notes


def _scale_bar(ax, right: bool = False) -> None:
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    w = x1 - x0
    if not np.isfinite(w) or w <= 0:
        return
    target = w / 5.0 / 1000.0  # km
    mag = 10 ** math.floor(math.log10(target))
    km = max(v * mag for v in (1, 2, 5) if v * mag <= target)
    xa, ya = (x1 - 0.03 * w - km * 1000) if right else x0 + 0.03 * w, y0 + 0.04 * (y1 - y0)
    ax.plot([xa, xa + km * 1000], [ya, ya], color="black", lw=1.2, solid_capstyle="butt",
            gid="scale_bar")
    ax.text(xa + km * 500, ya + 0.012 * (y1 - y0), f"{km:g} km", ha="center", va="bottom", fontsize=6)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)


def _plot_polys(ax, sub, colors, *, hatch=None, gid=None, edgecolor=EDGE, linewidth=0.2):
    if sub.empty:
        return
    before = len(ax.collections)
    if hatch:
        sub.plot(ax=ax, facecolor="none", edgecolor=HATCH_COLOR, linewidth=0.0, hatch=hatch)
    else:
        sub.plot(ax=ax, color=list(colors), edgecolor=edgecolor, linewidth=linewidth)
    for col in ax.collections[before:]:
        if gid:
            col.set_gid(gid)


def _boundary_info(gdf, boundary_source: Optional[str]) -> Dict[str, Any]:
    a = gdf.attrs or {}
    info = {k: a.get(k) for k in ("level", "year", "detail", "source_url", "sha256") if a.get(k) is not None}
    desc = boundary_source or a.get("description")
    if not desc and a.get("year"):
        desc = B.boundary_description(a.get("level", "geography"), a["year"], a.get("detail", "cb"))
    info["description"] = desc or "boundary vintage not recorded"
    return info


def _ids(gdf) -> pd.DataFrame:
    cols = [c for c in ("GEOID", "NAME") if c in gdf.columns]
    return gdf[cols].reset_index(drop=True).astype(str) if cols else pd.DataFrame(index=range(len(gdf)))


LEGEND_TITLE_WIDTH = 26


def _legend(ax, handles, title: str) -> None:
    """Legend outside the map, to the right, left-aligned title. Title lines are
    wrapped at 26 characters so a long variable label is not clipped at the
    figure's right edge (journal widths are fixed)."""
    title = "\n".join(textwrap.fill(line, LEGEND_TITLE_WIDTH) if line else line for line in title.split("\n"))
    leg = ax.legend(handles=handles, loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False,
                    fontsize=6, title_fontsize=6, title=title, alignment="left")
    leg.set_gid("legend")


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.3g}"


_COMMON_DOC = """
    Common keyword arguments: ``insets`` (``"auto"``: AK/HI/PR insets when
    a national map includes them; ``False``: one equal-area projection),
    ``boundary_source`` (caption text; defaults to the boundary vintage in
    ``gdf.attrs`` from ``census.boundaries``), ``out_dir``, ``name``,
    ``width`` (``"one"``, ``"two"`` or inches), ``height``, ``formats``,
    ``dpi``, ``data_source`` (ACS release and table), ``result_id``,
    ``manifest_hash``, ``provenance``.
"""


def _fit_legends(fig, margin: float = 0.005, rounds: int = 6) -> None:
    """Shrink axes whose legend (placed right of the axes) runs past the figure's
    right edge. tight_layout ignores that overflow for equal-aspect maps, so a
    wide area (e.g. a state wider than tall) pushed the legend out of the
    fixed journal-width figure and clipped it."""
    renderer = fig._get_renderer()
    limit = fig.bbox.width * (1 - margin)
    for ax in fig.axes:
        leg = ax.get_legend()
        if leg is None:
            continue
        for _ in range(rounds):
            ax.apply_aspect()
            over = leg.get_window_extent(renderer).x1 - limit
            if over <= 0.5:  # display units (pixels at the figure dpi)
                break
            active = ax.get_position(original=False)
            orig = ax.get_position(original=True)
            width = max(active.width - over / fig.bbox.width, 0.05)
            ax.set_position([active.x0, orig.y0, width, orig.height])


def _render(fig, name, out_dir, values, caption, prov, formats, dpi, notes):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*tight_layout.*")
        warnings.filterwarnings("ignore", message=".*not compatible with tight_layout.*")
        return _finish(fig, name, out_dir, values, caption, prov, formats, dpi, notes=notes,
                       after_layout=_fit_legends)


# --------------------------------------------------------------------------- maps

def choropleth(
    gdf, column: str, *, se_col: Optional[str] = None, moe_col: Optional[str] = None,
    scheme: str = "quantiles", k: int = 5, confidence_level: float = 0.90,
    cv_threshold: float = 30.0, cmap: str = "YlGnBu", legend_title: Optional[str] = None,
    insets: Union[bool, str] = "auto", boundary_source: Optional[str] = None,
    title: Optional[str] = None, out_dir: Union[str, Path] = ".", name: str = "choropleth",
    width: Union[str, float] = "two", height: Optional[float] = None,
    formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300, data_source: str = "",
    result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Choropleth of ``column`` classed by ``scheme`` (``quantiles``,
    ``natural_breaks``, ``equal_interval`` or ``separability``) into ``k``
    classes. The legend states the method and class separability (module
    docstring); areas with CV > ``cv_threshold`` percent are hatched and
    flagged in the values file."""
    level = _level(confidence_level)
    zc = _zcrit(level)
    g = gdf.reset_index(drop=True)
    est = pd.to_numeric(g[column], errors="coerce").to_numpy(float)
    has_se = bool(se_col or moe_col)
    # without SE/MOE the map shows estimates only: no hatching, separability n/a
    se = _se(g, se_col, moe_col) if has_se else np.full(len(est), np.nan)
    cv = _cv(est, se)
    bins = class_breaks(est, scheme, k, se=se if has_se else None, confidence_level=level)
    kk = len(bins)
    cls = assign_classes(est, bins)
    sep = (class_separability(est, se, np.where(cls >= 0, cls, np.nan), kk, level) if has_se
           else {"separability": float("nan")})
    colors = sequential_colors(kk, cmap)
    low = np.isfinite(cv) & (cv > cv_threshold)
    finite = est[np.isfinite(est)]
    lowers = np.concatenate([[finite.min()], bins[:-1]])
    values = pd.concat([_ids(g), pd.DataFrame({
        "estimate": est, "se": se, "moe90": se * Z90, "ci_lower": est - zc * se, "ci_upper": est + zc * se,
        "cv_percent": cv, "low_reliability": low,
        "class": pd.array(np.where(cls >= 0, cls + 1, -1), dtype="Int64"),
        "class_lower": [lowers[c] if c >= 0 else np.nan for c in cls],
        "class_upper": [bins[c] if c >= 0 else np.nan for c in cls],
        "region": B.region_of(g).to_numpy()})], axis=1)
    values.loc[values["class"] == -1, "class"] = pd.NA
    fill = [colors[c] if c >= 0 else NO_DATA_COLOR for c in cls]

    def layers(ax, sub):
        idx = sub.index.to_numpy()
        _plot_polys(ax, sub, [fill[i] for i in idx], gid="classes")
        _plot_polys(ax, sub[low[idx]], None, hatch=HATCH_LOW, gid="low_reliability")

    fig = _new_map(width, height)
    main, crs_used, notes = _draw(fig, g, layers, insets)
    method = SCHEME_LABELS[scheme]
    sep_txt = "n/a" if not np.isfinite(sep["separability"]) else f"{sep['separability'] * 100:.0f}%"
    handles = [Patch(facecolor=colors[i], edgecolor="#999999", lw=0.3,
                     label=f"{_fmt(lowers[i])} – {_fmt(bins[i])}") for i in range(kk)]
    if (cls < 0).any():
        handles.append(Patch(facecolor=NO_DATA_COLOR, edgecolor="#999999", lw=0.3, label="No data"))
    if low.any():
        handles.append(Patch(facecolor="white", edgecolor=HATCH_COLOR, hatch=HATCH_LOW, lw=0.3,
                             label=f"CV > {cv_threshold:g}% (low reliability)"))
    with matplotlib.rc_context(STYLE):
        rel_txt = (f"class separability {sep_txt} ({_pct(level)} intervals)" if has_se
                   else "reliability not shown (no SE/MOE given)")
        _legend(main, handles, f"{legend_title or column}\n{method}, k = {kk}\n{rel_txt}")
    binfo = _boundary_info(g, boundary_source)
    caption = _caption(
        title or f"{legend_title or column} by {binfo.get('level', 'area')}", data_source,
        f"Boundaries: {binfo['description']}",
        (f"{method} with {kk} classes; class separability {sep_txt} (share of adjacent-class area "
         f"pairs whose {_pct(level)} confidence intervals do not overlap, Sun and Wong 2010); "
         f"hatching marks estimates with coefficient of variation above {cv_threshold:g}% "
         f"({int(low.sum())} of {len(g)} areas)" if has_se else
         f"{method} with {kk} classes; reliability not shown (no standard errors or MOEs given, so "
         f"class separability and low-reliability areas are not assessed)")
        + f"; equal-area projection {', '.join(crs_used)}")
    prov = _provenance("choropleth", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, column=column, se_col=se_col, moe_col=moe_col, scheme=scheme,
                            k=kk, bins=list(bins), separability=sep, cv_threshold=cv_threshold,
                            n_low_reliability=int(low.sum()), cmap=cmap, crs=crs_used,
                            boundaries=binfo))
    return _render(fig, name, out_dir, values, caption, prov, formats, dpi, notes)


def uncertainty_map(
    gdf, column: str, *, se_col: Optional[str] = None, moe_col: Optional[str] = None,
    thresholds: Sequence[float] = CV_CLASSES, insets: Union[bool, str] = "auto",
    boundary_source: Optional[str] = None, title: Optional[str] = None, legend_title: Optional[str] = None,
    out_dir: Union[str, Path] = ".", name: str = "uncertainty", width: Union[str, float] = "two",
    height: Optional[float] = None, formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300,
    data_source: str = "", result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Companion map of the coefficient of variation (CV = SE / |estimate| x
    100) in reliability classes: <= 12%, 12-30%, > 30% by default."""
    g = gdf.reset_index(drop=True)
    est = pd.to_numeric(g[column], errors="coerce").to_numpy(float)
    se = _se(g, se_col, moe_col)
    cv = _cv(est, se)
    t = list(thresholds)
    labels = [f"CV ≤ {t[0]:g}% (higher reliability)"] + \
        [f"{a:g}% < CV ≤ {b:g}%" for a, b in zip(t[:-1], t[1:])] + [f"CV > {t[-1]:g}% (low reliability)"]
    ncls = len(t) + 1
    palette = list(CV_COLORS) if ncls == 3 else sequential_colors(ncls, "OrRd")
    cls = np.where(np.isfinite(cv), np.searchsorted(np.asarray(t, float), cv, side="left"), -1)
    fill = [palette[c] if c >= 0 else NO_DATA_COLOR for c in cls]
    values = pd.concat([_ids(g), pd.DataFrame({
        "estimate": est, "se": se, "cv_percent": cv,
        "reliability_class": [labels[c] if c >= 0 else "no data" for c in cls],
        "region": B.region_of(g).to_numpy()})], axis=1)

    def layers(ax, sub):
        idx = sub.index.to_numpy()
        _plot_polys(ax, sub, [fill[i] for i in idx], gid="cv_classes")

    fig = _new_map(width, height)
    main, crs_used, notes = _draw(fig, g, layers, insets)
    handles = [Patch(facecolor=palette[i], edgecolor="#999999", lw=0.3, label=labels[i]) for i in range(ncls)]
    if (cls < 0).any():
        handles.append(Patch(facecolor=NO_DATA_COLOR, edgecolor="#999999", lw=0.3, label="No data / CV undefined"))
    with matplotlib.rc_context(STYLE):
        _legend(main, handles,
                    f"Reliability of {legend_title or column}")
    binfo = _boundary_info(g, boundary_source)
    caption = _caption(title or f"Coefficient of variation of {legend_title or column}", data_source,
                       f"Boundaries: {binfo['description']}",
                       "CV = SE / |estimate| × 100" + ("" if se_col else ", SE from the published 90% MOE / 1.645")
                       + "; classes at " + ", ".join(f"{x:g}%" for x in t))
    prov = _provenance("uncertainty_map", data_source, None, result_id, manifest_hash,
                       dict(provenance or {}, column=column, se_col=se_col, moe_col=moe_col,
                            thresholds=t, crs=crs_used, boundaries=binfo))
    return _render(fig, name, out_dir, values, caption, prov, formats, dpi, notes)


def significance_categories(comparisons: pd.DataFrame) -> pd.Series:
    """``increase`` / ``decrease`` / ``no significant change`` / ``not
    tested`` from ``batch_z_test`` output, using ``significant_adjusted``
    (after the multiple-comparison correction) and the sign of
    ``difference`` (X1 - X2)."""
    sig = comparisons["significant_adjusted"]
    diff = pd.to_numeric(comparisons["difference"], errors="coerce")
    out = pd.Series("not tested", index=comparisons.index, dtype=object)
    tested = sig.notna() & diff.notna()
    s = sig.where(tested, False).astype(bool)
    out[tested & ~s] = "no significant change"
    out[tested & s & (diff > 0)] = "increase"
    out[tested & s & (diff < 0)] = "decrease"
    return out


def significance_map(
    gdf, comparisons: pd.DataFrame, *, geoid_col: str = "GEOID",
    labels: Optional[Mapping[str, str]] = None, insets: Union[bool, str] = "auto",
    boundary_source: Optional[str] = None, title: Optional[str] = None,
    out_dir: Union[str, Path] = ".", name: str = "significance", width: Union[str, float] = "two",
    height: Optional[float] = None, formats: Sequence[str] = DEFAULT_FORMATS, dpi: int = 300,
    data_source: str = "", result_id: Optional[str] = None, manifest_hash: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> FigureOutput:
    """Map of ``census.multitest.batch_z_test`` results joined on GEOID:
    significant increase (X1 > X2), significant decrease, or no significant
    change after the correction recorded in ``comparisons``; untested or
    missing areas are marked. ``labels`` renames categories (e.g. ``{"increase":
    "Higher than US"}``). Decreases are also dotted, so the map reads in
    grayscale."""
    if geoid_col not in comparisons.columns:
        raise ValueError(f"comparisons need a {geoid_col!r} column (batch_z_test carries GEOID when present)")
    g = gdf.reset_index(drop=True)
    comp = comparisons.copy()
    comp["_cat"] = significance_categories(comp)
    comp["_gid"] = comp[geoid_col].astype(str)
    if comp["_gid"].duplicated().any():
        raise ValueError("comparisons have duplicate GEOIDs")
    m = g[["GEOID"]].astype(str).merge(comp, left_on="GEOID", right_on="_gid", how="left",
                                       suffixes=("", "_cmp"))
    cat = m["_cat"].fillna("not tested").to_numpy()
    names = dict({"increase": "Significant increase", "decrease": "Significant decrease",
                  "no significant change": "No significant change", "not tested": "Not tested / no data"},
                 **(labels or {}))
    level = _level(float(comp["confidence_level"].iloc[0])) if "confidence_level" in comp and len(comp) else 0.90
    correction = str(comp["correction"].iloc[0]) if "correction" in comp and len(comp) else "none"
    family = int(comp["family_size"].iloc[0]) if "family_size" in comp and len(comp) else len(comp)
    keep = [c for c in ("difference", "se_difference", "z", "p_value", "p_adjusted",
                        "significant_adjusted") if c in m.columns]
    values = pd.concat([_ids(g), m[keep].reset_index(drop=True),
                        pd.DataFrame({"category": cat, "region": B.region_of(g).to_numpy()})], axis=1)

    def layers(ax, sub):
        idx = sub.index.to_numpy()
        _plot_polys(ax, sub, [SIG_COLORS[cat[i]] for i in idx], gid="categories", edgecolor="#BBBBBB")
        for key, h in SIG_HATCH.items():
            if h:
                _plot_polys(ax, sub[cat[idx] == key], None, hatch=h, gid=f"hatch_{key}")

    fig = _new_map(width, height)
    main, crs_used, notes = _draw(fig, g, layers, insets)
    counts = {k: int((cat == k).sum()) for k in SIG_COLORS}
    handles = [Patch(facecolor=SIG_COLORS[k], edgecolor="#555555", hatch=SIG_HATCH[k], lw=0.3,
                     label=f"{names[k]} ({counts[k]})") for k in SIG_COLORS if counts[k] or k != "not tested"]
    with matplotlib.rc_context(STYLE):
        _legend(main, handles,
                    f"{_pct(level)} level, {correction} correction")
    binfo = _boundary_info(g, boundary_source)
    caption = _caption(title or "Statistically significant differences", data_source,
                       f"Boundaries: {binfo['description']}",
                       f"two-sample Z tests (Census statistical testing guidance) of X1 - X2; "
                       f"significance at {1 - level:g} after {correction} correction across {family} "
                       f"tests; raw differences are not mapped")
    prov = _provenance("significance_map", data_source, level, result_id, manifest_hash,
                       dict(provenance or {}, correction=correction, family_size=family, counts=counts,
                            crs=crs_used, boundaries=binfo))
    return _render(fig, name, out_dir, values, caption, prov, formats, dpi, notes)


for _f in (choropleth, uncertainty_map, significance_map):
    _f.__doc__ = (_f.__doc__ or "") + _COMMON_DOC
