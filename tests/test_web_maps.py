# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained interactive HTML choropleths (Round 10 F3). Offline: DC tract fixture."""

import json
import os
import re
from html.parser import HTMLParser

import numpy as np
import pandas as pd
import pytest

from census import maps, web_maps

FIXTURE_GEO = os.path.join(os.path.dirname(__file__), "fixtures", "geo")


@pytest.fixture(scope="module")
def gdf():
    from census import boundaries

    old = os.environ.get("CENSUS_GEO_DIR")
    os.environ["CENSUS_GEO_DIR"] = FIXTURE_GEO
    try:
        b = boundaries.get_boundaries("tract", 2023, state="11")
    finally:
        if old is None:
            os.environ.pop("CENSUS_GEO_DIR", None)
        else:
            os.environ["CENSUS_GEO_DIR"] = old
    rng = np.random.default_rng(3)
    b = b.copy()
    b["rate"] = rng.uniform(5, 30, len(b)).round(1)
    b["rate_moe"] = (b["rate"] * rng.uniform(0.1, 0.7, len(b))).round(2)
    b.loc[b.index[0], "rate"] = np.nan
    return b


class _Checker(HTMLParser):
    VOID = {"meta", "br", "link", "img", "input", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.scripts, self._in_script = [], [], [], None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if tag == "script":
            self._in_script = dict(attrs)
            for k, v in attrs:
                if k == "src":
                    self.errors.append(f"external script {v}")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()
        if tag == "script":
            self._in_script = None

    def handle_data(self, data):
        if self._in_script is not None:
            self.scripts.append((self._in_script, data))


def _write(gdf, tmp_path, **kw):
    return web_maps.web_choropleth(gdf, "rate", moe_col="rate_moe", out_path=tmp_path / "m.html",
                                   data_source="ACS 2019-2023 5-year (synthetic test values)", **kw)


def test_self_contained_valid_html_with_embedded_data(gdf, tmp_path):
    res = _write(gdf, tmp_path)
    text = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    p = _Checker()
    p.feed(text)
    p.close()
    assert not p.errors and not p.stack
    # No URLs to any host (the page makes no requests; the citation has no links).
    assert not re.findall(r"https?://", text, flags=re.I)
    assert not re.search(r"""(src|href)\s*=\s*["'](?!#)""", text)
    assert "default-src 'none'" in text
    data = [d for a, d in p.scripts if a.get("type") == "application/json"]
    assert len(data) == 1
    payload = json.loads(data[0])
    feats = payload["geojson"]["features"]
    assert len(feats) == len(gdf) == res["n_features"]
    assert all(f["geometry"]["coordinates"] for f in feats)
    first = next(f["properties"] for f in feats if f["properties"]["est"] is not None)
    assert set(first) >= {"geoid", "name", "est", "moe", "cv", "rel", "class", "low"}
    assert first["rel"] in ("High", "Moderate", "Low")
    assert sum(f["properties"]["class"] == 0 for f in feats) == 1  # the NaN tract is "no data"
    assert "U.S. Census Bureau" in text and res["bytes"] == len(text.encode("utf-8"))


def test_legend_matches_static_classification(gdf, tmp_path):
    res = _write(gdf, tmp_path, scheme="quantiles", k=5)
    payload = _payload(tmp_path)
    est = gdf["rate"].to_numpy(float)
    se = gdf["rate_moe"].to_numpy(float) / 1.645
    bins = maps.class_breaks(est, "quantiles", 5, se=se)
    cls = maps.assign_classes(est, bins)
    sep = maps.class_separability(est, se, np.where(cls >= 0, cls, np.nan), len(bins))
    assert res["bins"] == pytest.approx(list(bins))
    assert res["separability"] == pytest.approx(sep["separability"])
    assert payload["colors"] == maps.sequential_colors(len(bins))
    assert f"{sep['separability'] * 100:.0f}%" in payload["reliability"]
    cv = se / np.abs(est) * 100
    assert res["n_low_reliability"] == int(np.nansum(cv > 30)) == payload["n_low"]
    by_geoid = {f["properties"]["geoid"]: f["properties"] for f in payload["geojson"]["features"]}
    for gid, c, low in zip(gdf["GEOID"].astype(str), cls, cv > 30):
        assert by_geoid[gid]["class"] == (c + 1 if c >= 0 else 0)
        assert by_geoid[gid]["low"] == bool(low)


def _payload(tmp_path):
    text = (tmp_path / "m.html").read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="map-data">(.*?)</script>', text, re.S).group(1)
    return json.loads(raw)


def test_size_guard_simplifies_then_refuses(gdf, tmp_path):
    full = _write(gdf, tmp_path, tolerance_m=0.0)
    coarse = _write(gdf, tmp_path, tolerance_m=50.0)
    limit = (full["bytes"] + coarse["bytes"]) // 2
    small = _write(gdf, tmp_path, max_bytes=limit, tolerance_m=1.0)
    assert small["bytes"] <= limit and small["tolerance_m"] > 1.0
    with pytest.raises(ValueError, match="max_bytes"):
        _write(gdf, tmp_path / "x", max_bytes=20_000, max_attempts=1)
    assert not (tmp_path / "x" / "m.html").exists()


def test_script_breakout_in_names_is_escaped(gdf, tmp_path):
    g = gdf.copy()
    g["NAME"] = "</script><script>alert(1)</script>"
    web_maps.web_choropleth(g, "rate", out_path=tmp_path / "m.html")
    text = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert text.count("</script>") == 2  # only the two real script elements
    assert _payload(tmp_path)["has_se"] is False
