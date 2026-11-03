# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content tests for census.maps (PLAN_ROUND_9.md, workstream B). No image baselines."""

import itertools
import json
from pathlib import Path

import geopandas as gpd
import mapclassify
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from census import maps as M
from census.multitest import batch_z_test

SRC = "ACS 5-year 2019-2023, table B19013"


def _grid(n=12, seed=0):
    rng = np.random.default_rng(seed)
    est = np.round(rng.uniform(20_000, 120_000, n), 0)
    moe = est * rng.uniform(0.02, 0.4, n)  # CV 1.2% .. 24%
    low = [i for i in (1, 4, 7) if i < n]
    moe[low] = est[low] * 0.8  # CV ~49% -> low reliability
    geoms = [box(-100 + (i % 4) * 0.5, 40 + (i // 4) * 0.5, -99.5 + (i % 4) * 0.5, 40.5 + (i // 4) * 0.5)
             for i in range(n)]
    g = gpd.GeoDataFrame({"GEOID": [f"20{i:03d}" for i in range(n)], "est": est, "moe": moe},
                         geometry=geoms, crs="EPSG:4269")
    g.attrs.update(level="county", year=2023, detail="cb",
                   description="Census 2023 cartographic boundary file (1:500,000), county")
    return g


def _collections(fig, gid):
    return [c for ax in fig.axes for c in ax.collections if c.get_gid() == gid]


@pytest.mark.parametrize("scheme,cls", [("quantiles", mapclassify.Quantiles),
                                        ("natural_breaks", mapclassify.FisherJenks),
                                        ("equal_interval", mapclassify.EqualInterval)])
def test_breaks_match_mapclassify(scheme, cls):
    y = _grid(40, seed=3)["est"].to_numpy()
    bins = M.class_breaks(y, scheme, 5)
    ref = cls(y, k=5)
    np.testing.assert_allclose(bins, ref.bins)
    np.testing.assert_array_equal(M.assign_classes(y, bins), ref.yb)


def test_separability_on_constructed_data():
    # Class 0: intervals [0, 2] and [1, 3]; class 1: [2.5, 4.5] and [10, 12].
    z = 1.6448536269514722  # exact 90% critical value used by figures._zcrit
    est = np.array([1.0, 2.0, 3.5, 11.0])
    se = np.full(4, 1.0 / z)
    r = M.class_separability(est, se, [0, 0, 1, 1], k=2)
    # pairs: (0,2) 2<2.5 yes, (0,3) yes, (1,2) 3<2.5 no, (1,3) yes -> 3/4
    assert r["pairs"] == 4 and r["separable_pairs"] == 3
    assert r["separability"] == pytest.approx(0.75)
    # three classes pooled over two boundaries; missing SE excluded
    r3 = M.class_separability([1, 5, 9, 9.5], [0.1, 0.1, 0.1, np.nan], [0, 1, 2, 2], k=3)
    assert r3["pairs"] == 2 and r3["separability"] == 1.0 and r3["n_excluded"] == 1
    assert [b["separability"] for b in r3["by_boundary"]] == [1.0, 1.0]


def test_separability_breaks_optimal_by_brute_force():
    rng = np.random.default_rng(7)
    est = np.sort(rng.normal(50, 15, 14))
    se = rng.uniform(0.5, 6, 14)
    k, m = 3, 2
    bins = M.separability_breaks(est, se, k=k, min_class_size=m)
    lo, hi = est - 1.6448536269514722 * se, est + 1.6448536269514722 * se

    def cost(pos):
        return sum(int(((lo < b) & (b < hi)).sum()) for b in ((est[p - 1] + est[p]) / 2 for p in pos))

    best = min(cost(p) for p in itertools.combinations(range(1, 14), k - 1)
               if p[0] >= m and all(b - a >= m for a, b in zip(p, p[1:])) and 14 - p[-1] >= m)
    got = [int(np.searchsorted(est, b, side="right")) for b in bins[:-1]]
    assert cost(got) == best
    assert bins[-1] == est.max()


def test_separability_breaks_find_gaps():
    est = np.array([1, 1.1, 1.2, 5, 5.1, 5.2, 10, 10.1, 10.2])
    np.testing.assert_allclose(M.separability_breaks(est, np.full(9, 0.1), k=3), [1.2, 5.2, 10.2])


def test_choropleth_hatches_low_reliability_polygons(tmp_path):
    g = _grid()
    out = M.choropleth(g, "est", moe_col="moe", out_dir=tmp_path, formats=("svg",), data_source=SRC)
    low = out.values["low_reliability"].to_numpy()
    assert set(np.flatnonzero(low)) == {1, 4, 7}
    hatched = _collections(out.figure, "low_reliability")
    assert len(hatched) == 1 and hatched[0].get_hatch() == M.HATCH_LOW
    proj = g.to_crs(5070)
    got = sorted(tuple(np.round(p.get_extents().extents, 0)) for p in hatched[0].get_paths())
    want = sorted(tuple(np.round(proj.geometry.iloc[i].bounds, 0)) for i in (1, 4, 7))
    assert got == want
    assert len(_collections(out.figure, "classes")[0].get_paths()) == 12


def test_choropleth_values_caption_provenance(tmp_path):
    g = _grid()
    out = M.choropleth(g, "est", moe_col="moe", out_dir=tmp_path, formats=("svg", "png"),
                       data_source=SRC, result_id="r1")
    v = out.values
    ref = mapclassify.Quantiles(g["est"].to_numpy(), k=5)
    assert (v["class"].to_numpy(int) == ref.yb + 1).all()
    np.testing.assert_allclose(v["cv_percent"], g["moe"] / 1.645 / g["est"] * 100)
    for text in ("2019-2023", "Census 2023 cartographic boundary file", "quantiles",
                 "class separability", "30%"):
        assert text in out.caption
    prov = json.loads(Path(out.paths["provenance"]).read_text())
    sep = prov["inputs"]["separability"]
    assert 0 <= sep["separability"] <= 1 and prov["inputs"]["boundaries"]["year"] == 2023
    assert prov["inputs"]["crs"] == ["EPSG:5070"]
    assert set(prov["files"]) == {"choropleth.svg", "choropleth.png", "choropleth.csv",
                                  "choropleth.caption.txt"}
    legend = out.figure.axes[0].get_legend()
    assert "class separability" in legend.get_title().get_text()
    assert any("CV > 30%" in t.get_text() for t in legend.get_texts())


def test_choropleth_separability_scheme_and_missing(tmp_path):
    g = _grid()
    g.loc[0, "est"] = np.nan
    out = M.choropleth(g, "est", moe_col="moe", scheme="separability", k=3, out_dir=tmp_path,
                       formats=("svg",))
    assert out.values["class"].isna().sum() == 1
    assert "class-separability breaks" in out.caption
    assert any(t.get_text() == "No data" for t in out.figure.axes[0].get_legend().get_texts())


def test_byte_identical_regeneration(tmp_path):
    outs = [M.choropleth(_grid(), "est", moe_col="moe", out_dir=tmp_path / str(i), result_id="r1")
            for i in (1, 2)]
    for fmt in ("svg", "pdf", "eps", "png", "csv", "caption", "provenance"):
        assert Path(outs[0].paths[fmt]).read_bytes() == Path(outs[1].paths[fmt]).read_bytes(), fmt


def test_uncertainty_map(tmp_path):
    g = _grid()
    out = M.uncertainty_map(g, "est", moe_col="moe", out_dir=tmp_path, formats=("svg",))
    cv = out.values["cv_percent"].to_numpy()
    cls = out.values["reliability_class"]
    assert (cls[cv > 30].str.contains("low reliability")).all()
    assert (cls[cv <= 12].str.contains("higher reliability")).all()
    assert "CV = SE" in out.caption


def test_significance_map(tmp_path):
    g = _grid(6)
    df = pd.DataFrame({"GEOID": g["GEOID"], "e1": [100, 100, 100, 100, 100, np.nan],
                       "m1": [1, 1, 1, 50, 1, 1], "e2": [80, 120, 101, 90, 100, 100],
                       "m2": [1, 1, 1, 50, 1, 1]})
    res = batch_z_test(df, "e1", "m1", "e2", "m2")
    out = M.significance_map(g, res, out_dir=tmp_path, formats=("svg",), data_source=SRC)
    assert out.values["category"].tolist() == ["increase", "decrease", "no significant change",
                                               "no significant change", "no significant change",
                                               "not tested"]
    dotted = _collections(out.figure, "hatch_decrease")
    assert len(dotted) == 1 and len(dotted[0].get_paths()) == 1
    assert "holm correction" in out.caption and "raw differences are not mapped" in out.caption
    with pytest.raises(ValueError, match="GEOID"):
        M.significance_map(g, res.drop(columns="GEOID"), out_dir=tmp_path, formats=("svg",))


def test_national_map_has_insets(tmp_path):
    parts = [("20001", -100, 40), ("06001", -120, 37), ("02020", -150, 61), ("15003", -158, 21),
             ("72127", -66.2, 18.2), ("78010", -64.8, 17.7)]
    g = gpd.GeoDataFrame({"GEOID": [p[0] for p in parts], "est": [1.0, 2, 3, 4, 5, 6],
                          "moe": [0.1] * 6},
                         geometry=[box(x, y, x + 1, y + 1) for _, x, y in parts], crs="EPSG:4269")
    out = M.choropleth(g, "est", moe_col="moe", k=3, out_dir=tmp_path, formats=("svg",))
    assert len(out.figure.axes) == 4  # CONUS + AK, HI, PR
    crs = json.loads(Path(out.paths["provenance"]).read_text())["inputs"]["crs"]
    assert crs[0] == "conus: EPSG:5070" and crs[1] == "alaska: EPSG:3338"
    assert any("island-area" in n for n in out.notes)
    single = M.choropleth(g, "est", moe_col="moe", k=3, insets=False, out_dir=tmp_path,
                          name="flat", formats=("svg",))
    assert len(single.figure.axes) == 1
