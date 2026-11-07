# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained interactive HTML choropleths (PLAN_ROUND_10 F3).

``web_choropleth`` writes one HTML file that opens offline in any browser:

* geometry is projected to the same equal-area CRS as the static maps
  (``census.boundaries.equal_area_crs``), simplified (topology-preserving,
  tolerance in meters) and inlined as GeoJSON in a ``<script
  type="application/json">`` block, with coordinates rounded to meters;
* the page script is inline and draws SVG paths itself: no CDN, no web
  fonts, no tile server, and no network request of any kind (a
  Content-Security-Policy meta tag forbids them);
* classes, colors, the low-reliability rule and class separability come from
  :mod:`census.maps` (``class_breaks``, ``assign_classes``,
  ``class_separability``, ``sequential_colors``), so the legend matches the
  static ``choropleth`` for the same arguments;
* hovering (or focusing with the keyboard) an area shows its name, estimate,
  90% MOE, CV and reliability category (``census.stats.reliability_flag``);
  areas with CV above ``cv_threshold`` are hatched, as in the static maps;
* a size guard: when the file would exceed ``max_bytes`` the geometry is
  simplified further (tolerance doubled, up to ``max_attempts`` times) and
  then a ValueError is raised rather than writing an oversized page.

The interactive map is for exploring results; cite the static vector maps
(``census.maps``) in publications.
"""

import html
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from . import boundaries as B
from . import maps as M
from .figures import _level, _pct, _zcrit
from .stats import reliability_flag
from .variance import Z90

DEFAULT_MAX_BYTES = 8_000_000
CENSUS_CITATION = "U.S. Census Bureau, American Community Survey 5-year estimates; Census cartographic boundary files."


def _num(v: float, digits: int = 4) -> Optional[float]:
    return float(f"{v:.{digits}g}") if v is not None and math.isfinite(v) else None


def _rings(geom, nd: int) -> List[List[List[List[float]]]]:
    """Polygon coordinates (GeoJSON MultiPolygon layout), rounded."""
    polys = getattr(geom, "geoms", [geom]) if geom is not None and not geom.is_empty else []
    out = []
    for p in polys:
        if p.geom_type != "Polygon" or p.is_empty:
            continue
        rings = [p.exterior] + list(p.interiors)
        out.append([[[round(x, nd), round(y, nd)] for x, y in r.coords] for r in rings])
    return out


def _features(g, tolerance: float, props: List[Dict[str, Any]], nd: int) -> Dict[str, Any]:
    geoms = g.geometry.simplify(tolerance, preserve_topology=True) if tolerance > 0 else g.geometry
    feats = []
    for geom, p in zip(geoms, props):
        feats.append({"type": "Feature", "properties": p,
                      "geometry": {"type": "MultiPolygon", "coordinates": _rings(geom, nd)}})
    return {"type": "FeatureCollection", "features": feats}


def _json_for_script(obj: Any) -> str:
    # Inside <script>, "</" could close the element; escape it (and & and <, for safety).
    return (json.dumps(obj, separators=(",", ":"), allow_nan=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>__TITLE__</title>
<style>
:root { --bg: #ffffff; --fg: #1a1a1a; --muted: #555555; --edge: #ffffff; --panel: #f4f4f4; }
@media (prefers-color-scheme: dark) { :root { --bg: #161616; --fg: #ececec; --muted: #b0b0b0; --edge: #161616; --panel: #242424; } }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; }
h1 { font-size: 18px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 12px; }
.wrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
#map { flex: 1 1 480px; min-width: 0; }
#map svg { width: 100%; height: auto; max-height: 80vh; display: block; touch-action: none; cursor: grab; }
#map path { stroke: var(--edge); stroke-width: 0.6; vector-effect: non-scaling-stroke; fill-rule: evenodd; }
#map path.low { stroke: #7f7f7f; }
#map path:hover, #map path:focus { stroke: var(--fg); stroke-width: 1.6; outline: none; }
.legend { flex: 0 1 260px; background: var(--panel); padding: 10px 12px; border-radius: 6px; }
.legend h2 { font-size: 14px; margin: 0 0 6px; }
.legend .row { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
.legend .sw { width: 18px; height: 14px; border: 1px solid #999; flex: none; }
.legend .note { color: var(--muted); font-size: 12px; margin-top: 8px; }
#tip { position: fixed; pointer-events: none; background: var(--panel); color: var(--fg); border: 1px solid #999;
       border-radius: 4px; padding: 6px 8px; font-size: 13px; display: none; max-width: 280px; z-index: 2; }
#tip b { display: block; margin-bottom: 2px; }
footer { color: var(--muted); font-size: 12px; margin-top: 12px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>
<div class="wrap">
<div id="map" aria-label="Choropleth map"></div>
<div class="legend" id="legend"></div>
</div>
<div id="tip" role="status"></div>
<footer>__FOOTER__</footer>
<script type="application/json" id="map-data">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("map-data").textContent);
  var F = D.geojson.features, W = 1000;
  var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  F.forEach(function (f) { f.geometry.coordinates.forEach(function (p) { p[0].forEach(function (c) {
    if (c[0] < x0) x0 = c[0]; if (c[0] > x1) x1 = c[0]; if (c[1] < y0) y0 = c[1]; if (c[1] > y1) y1 = c[1]; }); }); });
  var s = W / Math.max(x1 - x0, 1e-9), H = Math.max((y1 - y0) * s, 1);
  function pt(c) { return ((c[0] - x0) * s).toFixed(1) + " " + ((y1 - c[1]) * s).toFixed(1); }
  function esc(t) { return String(t).replace(/[&<>"]/g, function (ch) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]; }); }
  var parts = ['<svg viewBox="0 0 ' + W + " " + H.toFixed(1) + '" role="img"><defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="none"/><line x1="0" y1="0" x2="0" y2="6" stroke="#7f7f7f" stroke-width="1.6"/></pattern></defs><g id="vp">'];
  F.forEach(function (f, i) {
    var d = f.geometry.coordinates.map(function (p) { return p.map(function (r) { return "M" + r.map(pt).join("L") + "Z"; }).join(""); }).join("");
    var c = f.properties.class, fill = c > 0 ? D.colors[c - 1] : D.no_data_color;
    parts.push('<path tabindex="0" data-i="' + i + '" d="' + d + '" fill="' + fill + '"></path>');
    if (f.properties.low) parts.push('<path class="low" data-i="' + i + '" d="' + d + '" fill="url(#hatch)" pointer-events="none"></path>');
  });
  parts.push("</g></svg>");
  var map = document.getElementById("map");
  map.innerHTML = parts.join("");
  var leg = ["<h2>" + esc(D.legend_title) + "</h2>"];
  D.classes.forEach(function (c, i) { leg.push('<div class="row"><span class="sw" style="background:' + D.colors[i] + '"></span>' + esc(c.label) + "</div>"); });
  if (D.has_no_data) leg.push('<div class="row"><span class="sw" style="background:' + D.no_data_color + '"></span>No data</div>');
  if (D.n_low) leg.push('<div class="row"><span class="sw" style="background:repeating-linear-gradient(45deg,#7f7f7f 0 2px,transparent 2px 5px)"></span>' + esc(D.low_label) + "</div>");
  leg.push('<div class="note">' + esc(D.method) + "<br>" + esc(D.reliability) + "</div>");
  document.getElementById("legend").innerHTML = leg.join("");
  var tip = document.getElementById("tip");
  function fmt(v, suf) { return v === null || v === undefined ? "n/a" : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : String(v)) + (suf || ""); }
  function show(e, i) {
    var p = F[i].properties;
    tip.innerHTML = "<b>" + esc(p.name) + "</b>" + esc(D.column) + ": " + fmt(p.est) +
      (D.has_se ? "<br>90% MOE: &plusmn;" + fmt(p.moe) + "<br>CV: " + fmt(p.cv, "%") + "<br>Reliability: " + esc(p.rel || "n/a") : "");
    tip.style.display = "block";
    var r = e.clientX !== undefined ? {x: e.clientX, y: e.clientY} : (function () { var b = e.target.getBoundingClientRect(); return {x: b.right, y: b.top}; })();
    tip.style.left = Math.min(r.x + 12, window.innerWidth - 290) + "px";
    tip.style.top = (r.y + 12) + "px";
  }
  function hide() { tip.style.display = "none"; }
  map.addEventListener("mousemove", function (e) { var i = e.target.getAttribute && e.target.getAttribute("data-i"); if (i !== null && i !== undefined) show(e, +i); else hide(); });
  map.addEventListener("mouseleave", hide);
  map.addEventListener("focusin", function (e) { var i = e.target.getAttribute("data-i"); if (i !== null) show(e, +i); });
  map.addEventListener("focusout", hide);
  // Wheel zoom and drag pan on the viewBox.
  var svg = map.querySelector("svg"), vb = [0, 0, W, H], drag = null;
  function setVB() { svg.setAttribute("viewBox", vb.map(function (v) { return v.toFixed(1); }).join(" ")); }
  function toSvg(e) { var b = svg.getBoundingClientRect(); return [vb[0] + (e.clientX - b.left) / b.width * vb[2], vb[1] + (e.clientY - b.top) / b.height * vb[3]]; }
  svg.addEventListener("wheel", function (e) {
    e.preventDefault(); var f = e.deltaY < 0 ? 0.8 : 1.25, p = toSvg(e);
    var w = Math.min(Math.max(vb[2] * f, W / 40), W), h = w * H / W;
    vb = [p[0] - (p[0] - vb[0]) * w / vb[2], p[1] - (p[1] - vb[1]) * h / vb[3], w, h]; setVB();
  }, {passive: false});
  svg.addEventListener("pointerdown", function (e) { drag = toSvg(e); svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", function (e) { if (!drag) return; var p = toSvg(e); vb[0] -= p[0] - drag[0]; vb[1] -= p[1] - drag[1]; setVB(); });
  svg.addEventListener("pointerup", function () { drag = null; });
  svg.addEventListener("dblclick", function () { vb = [0, 0, W, H]; setVB(); });
})();
</script>
</body>
</html>
"""


class WebMap(dict):
    """Result of :func:`web_choropleth`: ``path``, ``bytes``, ``tolerance_m``,
    ``separability``, ``bins``, ``n_low_reliability``, ``n_features``."""


def web_choropleth(
    gdf,
    column: str,
    *,
    se_col: Optional[str] = None,
    moe_col: Optional[str] = None,
    scheme: str = "quantiles",
    k: int = 5,
    confidence_level: float = 0.90,
    cv_threshold: float = 30.0,
    cmap: str = "YlGnBu",
    title: Optional[str] = None,
    legend_title: Optional[str] = None,
    data_source: str = "",
    boundary_source: Optional[str] = None,
    out_path: Union[str, Path] = "choropleth.html",
    tolerance_m: Optional[float] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_attempts: int = 6,
) -> WebMap:
    """Write a self-contained interactive choropleth of ``column`` (see module docstring).

    ``tolerance_m``: simplification tolerance in meters (default: 1/2000 of the
    larger side of the data's extent, so detail scales with the map). Returns a
    :class:`WebMap` describing the file written.
    """
    level = _level(confidence_level)
    zc = _zcrit(level)
    g = gdf.reset_index(drop=True)
    if g.crs is None:
        raise ValueError("gdf has no CRS; set one (Census files are EPSG:4269)")
    g = g.to_crs(B.equal_area_crs(g))
    est = pd.to_numeric(g[column], errors="coerce").to_numpy(float)
    has_se = bool(se_col or moe_col)
    se = M._se(g, se_col, moe_col) if has_se else np.full(len(est), np.nan)
    cv = M._cv(est, se)
    bins = M.class_breaks(est, scheme, k, se=se if has_se else None, confidence_level=level)
    kk = len(bins)
    cls = M.assign_classes(est, bins)
    sep = (M.class_separability(est, se, np.where(cls >= 0, cls, np.nan), kk, level) if has_se
           else {"separability": float("nan")})
    colors = M.sequential_colors(kk, cmap)
    low = np.isfinite(cv) & (cv > cv_threshold)
    finite = est[np.isfinite(est)]
    lowers = np.concatenate([[finite.min()], bins[:-1]])
    ids = M._ids(g)
    label_col = next((c for c in ("NAMELSAD", "NAME", "GEOID") if c in g.columns), None)
    names = g[label_col].astype(str) if label_col else pd.Series([str(i) for i in range(len(g))])
    props = []
    for i in range(len(g)):
        props.append({
            "geoid": str(ids["GEOID"].iloc[i]) if "GEOID" in ids else str(i),
            "name": str(names.iloc[i]),
            "est": _num(est[i], 6), "moe": _num(se[i] * Z90, 4), "cv": _num(cv[i], 3),
            "rel": reliability_flag(cv[i]) if np.isfinite(cv[i]) else "",
            "class": int(cls[i] + 1) if cls[i] >= 0 else 0, "low": bool(low[i]),
        })
    method = M.SCHEME_LABELS[scheme]
    sep_txt = "n/a" if not np.isfinite(sep["separability"]) else f"{sep['separability'] * 100:.0f}%"
    reliability = (f"Class separability {sep_txt} (share of adjacent-class area pairs whose {_pct(level)} "
                   "confidence intervals do not overlap, Sun and Wong 2010)" if has_se
                   else "Reliability not shown (no SE/MOE given)")
    binfo = M._boundary_info(g, boundary_source)
    payload = {
        "column": column, "legend_title": legend_title or column, "colors": colors,
        "no_data_color": M.NO_DATA_COLOR, "has_no_data": bool((cls < 0).any()), "has_se": has_se,
        "classes": [{"lower": _num(lowers[i], 6), "upper": _num(bins[i], 6),
                     "label": f"{M._fmt(lowers[i])} – {M._fmt(bins[i])}"} for i in range(kk)],
        "method": f"{method}, k = {kk}", "reliability": reliability,
        "n_low": int(low.sum()), "low_label": f"CV > {cv_threshold:g}% (low reliability)",
        "separability": None if not np.isfinite(sep["separability"]) else sep["separability"],
        "boundaries": binfo["description"],
    }
    title = title or f"{legend_title or column} by {binfo.get('level', 'area')}"
    subtitle = f"{method}, {kk} classes. {reliability}."
    footer = " ".join(x for x in (
        html.escape(data_source) if data_source else "",
        html.escape(f"Boundaries: {binfo['description']}."),
        html.escape(CENSUS_CITATION),
        "Interactive view for exploration; cite the static maps.",
    ) if x)
    minx, miny, maxx, maxy = g.total_bounds
    tol = float(tolerance_m) if tolerance_m is not None else max(maxx - minx, maxy - miny) / 2000.0
    out = Path(out_path)
    for attempt in range(max_attempts + 1):
        payload["geojson"] = _features(g, tol, props, 0)
        page = (_PAGE.replace("__TITLE__", html.escape(title))
                .replace("__SUBTITLE__", html.escape(subtitle))
                .replace("__FOOTER__", footer)
                .replace("__DATA__", _json_for_script(payload)))
        raw = page.encode("utf-8")
        if len(raw) <= max_bytes:
            break
        tol = tol * 2.0 if tol > 0 else max(maxx - minx, maxy - miny) / 2000.0
    else:
        raise ValueError(f"interactive map would be {len(raw):,} bytes (> max_bytes={max_bytes:,}) even with "
                         f"{tol / 2:.0f} m simplification; map fewer areas or raise max_bytes")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return WebMap(path=str(out), bytes=len(raw), tolerance_m=tol, separability=payload["separability"],
                  bins=[float(b) for b in bins], n_low_reliability=int(low.sum()), n_features=len(g),
                  crs=str(g.crs.to_string()), caption=f"{title}. {subtitle} Boundaries: {binfo['description']}.")
