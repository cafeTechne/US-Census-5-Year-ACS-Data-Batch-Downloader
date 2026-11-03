# PLAN_ROUND_9.md: Geography, Maps, and Spatial Inference

**Planning Round:** 9
**Date:** 2026-11-02
**Status:** Decisions made (section 12); in implementation
**Branch:** `phase9/geography-and-maps` (from `defaultBranch` after the Round 8 merge)

---

## 1. Summary

Rounds 5–8 made ACS values, derived statistics, and models trustworthy. What they lack is **space**: boundaries to map, neighbors to model, and accurate ways to move estimates between changing geographies. Round 9 adds a geospatial layer built on the open-source scientific Python stack, fixes the remaining accuracy gap in the 2010/2020 crosswalk, and closes Round 8's carryovers.

| Priority | Workstream | Section |
| :--- | :--- | :--- |
| P0 | A. Boundaries: official Census cartographic files, matched to each ACS release | 3 |
| P0 | B. Maps that show uncertainty (journal-ready static maps) | 4 |
| P0 | C. Spatial statistics and models with sampling error | 5 |
| P1 | D. Population-weighted crosswalks from decennial blocks | 6 |
| P1 | E. Block groups and custom areas from user polygons | 7 |
| P1 | F. ACS errata and data-quality flags | 8 |
| P1 | G. Round 8 carryovers | 9 |
| P2 | H. Packaging and release: pip install, versioned releases, DOI | 10 |

---

## 2. Approach to maps and GIS

**Why not ArcGIS as the engine.** ArcGIS is the institutional standard for producing and sharing maps, but it is proprietary, licensed per seat, and slow for batch geoprocessing because operations run through a GUI-oriented geoprocessing framework one feature class at a time. It also cannot be driven by agents or local models without a license and Windows desktop.

**What we use instead.** The open-source stack that ArcGIS itself reads and writes:

| Library | Role | Standing |
| :--- | :--- | :--- |
| GEOS via Shapely 2 | Geometry operations, vectorized in C | The same engine used by PostGIS and QGIS |
| GeoPandas | Tables with geometry, spatial joins, dissolves | De facto Python standard |
| pyogrio (GDAL) | Fast reading and writing of GIS formats | GDAL underlies nearly every GIS, including ArcGIS's format support |
| pyproj (PROJ) | Coordinate systems and projections | The reference projection library |
| PySAL (`libpysal`, `esda`, `spreg`, `mapclassify`) | Spatial weights, autocorrelation, spatial regression, map classification | Peer-reviewed, widely cited (Rey and Anselin) |
| matplotlib | Static maps for journals | Already used for figures |

Operations that are slow in desktop GIS (a spatial join of every US tract, dissolving 85,000 tracts into custom regions, building contiguity for a national tract file) take seconds to tens of seconds here because they are vectorized and run without a GUI.

**Interoperability with ArcGIS.** Outputs are written as **GeoPackage** (an open OGC standard that ArcGIS Pro and QGIS open natively), **GeoParquet** (fast, columnar), and shapefile only on request (field names truncated to 10 characters, so ACS codes collide). A researcher can do the analysis here and finish cartography in ArcGIS Pro or QGIS if a journal or agency requires it.

---

## 3. Workstream A (P0): boundaries

- **Source:** Census **cartographic boundary files** (`cb_<year>_<state>_<level>_500k`), generalized for mapping and small, from `www2.census.gov/geo/tiger/GENZ<year>/`. Full TIGER/Line files when exact boundaries are needed (areal calculations, crosswalks).
- **Vintage matching:** each ACS release is joined to the boundary vintage Census used for it (ACS 5-year uses the geography of its final year), so 2019 releases get 2010-based tracts and 2020+ get 2020 tracts. A mismatch is an error, not a silent join.
- **Join key:** GEOID, which every output already carries. The join reports unmatched GEOIDs on either side.
- **Cache and integrity:** downloaded files go to `.cache/geo/<year>/` with SHA-256 sidecars, as with replicate files.
- **Projections:** stored in the source CRS (NAD83). Area and distance calculations use an equal-area projection (EPSG:5070 for the conterminous US; Alaska, Hawaii, and Puerto Rico have their own), chosen automatically.
- **Tools:** `get_boundaries(level, year, state=None)`, `join_boundaries(frame, ...)`, `write_geo(frame, path, format=...)`.

## 4. Workstream B (P0): maps that show uncertainty

A choropleth of ACS estimates is misleading when neighboring classes are not statistically distinguishable or many areas are unreliable. The literature offers concrete remedies (Sun and Wong 2010, *Cartography and Geographic Information Science*; Koo, Chun and Griffith 2017, on class separability with MOEs); we implement them:

- **Classification:** quantile, natural breaks, equal interval, and **class-separability breaks** that maximize the share of areas whose 90% intervals do not overlap adjacent classes (via `mapclassify` plus an MOE-aware criterion). The legend reports how separable the classes are.
- **Reliability overlays:** hatching or reduced opacity for low-reliability estimates (CV > 30%), with the threshold in the legend.
- **Significance maps:** for comparisons (two periods, or each area vs a reference), show significant increases, decreases, and no significant change after multiple-comparison correction (Round 7), not raw differences.
- **Companion uncertainty map:** a CV or MOE map beside every estimate map, on request.
- **Cartographic basics:** scale bar, north arrow only when meaningful, inset handling for Alaska, Hawaii, and Puerto Rico, colorblind-safe sequential and diverging palettes, grayscale-safe hatching.
- **Same output contract as Round 8 figures:** PDF/SVG/EPS/PNG, values CSV, caption draft with data sources (ACS release and boundary vintage), provenance JSON, byte-identical regeneration.
- **Interactive maps** (HTML) are optional and P2 (decision 3).

## 5. Workstream C (P0): spatial statistics and models with sampling error

- **Spatial weights:** queen and rook contiguity, k-nearest neighbors, distance bands (`libpysal`), built from boundaries, cached, with reports of islands (areas with no neighbors).
- **Spatial autocorrelation:** global Moran's I and local indicators (LISA) via `esda`, with permutation inference and the Round 7 multiple-comparison corrections for local tests.
- **Sampling error in spatial statistics:** noisy small-area estimates can create or hide clusters. Two remedies:
  - compute Moran's I on each of the 80 replicate maps (design-based SE for I), and
  - test on Fay–Herriot EBLUPs (Round 8) as a sensitivity analysis.
- **Spatial regression:** spatial lag and spatial error models via `spreg` (ML and GMM), with diagnostics (Lagrange multiplier tests) that suggest which model fits; replicate refits for sampling SEs where feasible.
- **Spatial Fay–Herriot:** small-area estimates that borrow strength from neighbors as well as covariates (SAR random effects; Pratesi and Salvati 2008), validated against published reference results and by simulation, as in Round 8.
- **Validation:** the Round 8 simulation harness gains spatially correlated populations; coverage targets as before (88–92% for nominal 90%).

## 6. Workstream D (P1): population-weighted crosswalks from decennial blocks

Round 7 apportions tracts across the 2010/2020 boundary change by land area, which assumes people are spread evenly over land. Block-level population is published in the decennial census, so we can do better without NHGIS:

- Use the Census **2010-to-2020 block relationship file** and **2010 and 2020 decennial block populations** (P1 table, `dec/pl` endpoint) to build population (and housing-unit) weights for every tract-to-tract piece.
- Choose weights by variable: population counts use population weights; household and housing variables use housing-unit weights.
- Validate: weights from blocks against land-area weights (report where they differ most), and apportioned 2019 tract totals against county totals (which did not change).
- Same mechanism yields **ZCTA 2010→2020** and **place** crosswalks.

## 7. Workstream E (P1): block groups and custom areas

- **Block groups (summary level 150):** API extraction, boundaries, and replicate files (Census publishes block-group VRE files per state). Many tables are not published at block-group level; the planner reports which.
- **Custom areas from polygons:** a user supplies polygons (neighborhoods, service areas, school catchments) as GeoPackage, GeoJSON, or shapefile; tracts or block groups are allocated by area overlap or by block-population weights (workstream D), then aggregated with MOEs (Round 7). Partial-overlap allocation is reported and its assumption stated.

## 8. Workstream F (P1): ACS errata and data-quality flags

The Census Bureau publishes errata and user notes for ACS releases (tables withdrawn, corrected, or with known problems). Researchers rarely check them.

- Maintain a dataset of errata and user notes by release and table, generated from the Census errata and user-notes pages with source URLs and retrieval dates (`scripts/fetch_acs_errata.py`).
- `plan_extraction` reports any errata or user notes affecting the requested tables and releases; confirmatory manifests record them.
- The Round 7 validation battery gains a check that every cited note resolves.

## 9. Workstream G (P1): Round 8 carryovers

1. **Controlled-estimate MOEs** (`-555555555`) preserved as 0 through the pipeline (with an explicit `controlled` flag column), and the Round 7 F1 check updated to accept the documented representation.
2. **Slim `batch_download` for the core profile** (a few required parameters, sensible defaults) and native `summary`/`next_step` in Round 7 tools.
3. **Realistic tract CV profiles** for simulations from real tract MOEs (fixtures), replacing the assumed profile.
4. **Local-model re-evaluation** with an 8K context and the new geography tools, comparing at least two models.
5. **Map tools in the `core` profile** if the local-model evaluation shows small models can use them.

## 10. Workstream H (P2): packaging and release

- `pyproject.toml` so the toolkit installs with `pip install` (optional extra `dev`), with a console entry point (`census-acs`) and an MCP entry point.
- Tagged, versioned releases with a changelog; a Zenodo DOI per release, wired into `CITATION.cff`, so papers can cite the exact version used.
- A built documentation site from the existing Markdown (optional).

---

## 11. Out of scope (Round 10 and later)

- **PUMS microdata** (custom tabulations with person and household replicate weights, PUMAs).
- **ACS 1-year estimates** (larger areas, more timely, different variance properties).
- **Cost-of-living adjustment across places** (BEA Regional Price Parities) for real-income comparisons between areas.
- **Multilevel models** (tracts within counties) and difference-in-differences designs on non-overlapping periods.
- **Interactive web maps** beyond the optional HTML export.

---

## 12. Decisions

| # | Question | Decision |
| :--- | :--- | :--- |
| 1 | Geospatial libraries | **Required dependencies** (GeoPandas, pyogrio, pyproj, Shapely, PySAL: `libpysal`, `esda`, `spreg`, `mapclassify`) |
| 2 | Boundary detail | **Decided:** cartographic boundary files (1:500k) for maps; TIGER/Line for areal calculations and crosswalks |
| 3 | Map priority | **Decided:** static journal maps are the priority; optional self-contained HTML export (no external tile servers) is P2 |
| 4 | Default map classification | **Decided: follow research practice.** Quantile classes (5 by default) are the most common choice in social-science journals because classes are comparable across maps; natural breaks (Jenks) and equal interval are available. Because ACS estimates carry error, every map's legend reports class separability (the share of adjacent-class pairs whose 90% intervals do not overlap), and class-separability breaks (Sun and Wong 2010) are available for maps where distinguishability matters most |
| 5 | Crosswalk weights | **Decided:** block-population weights by default; land-area fallback with a note |
| 6 | Shapefile output | **Decided:** available on request; GeoPackage is the default |

---

## 13. Acceptance criteria

1. Every map states its ACS release, boundary vintage, classification method, and class separability; low-reliability areas are marked.
2. Boundary joins fail loudly on vintage mismatch and report unmatched GEOIDs.
3. A national tract map (about 85,000 tracts) with contiguity weights builds in under a minute on a laptop.
4. Moran's I and spatial regression results reproduce PySAL reference examples; replicate-based SEs for Moran's I are validated by simulation.
5. Spatial Fay–Herriot reproduces a published reference result and meets coverage targets in simulation.
6. Block-population crosswalk weights sum to one per source tract, and apportioned 2019 tract totals match unchanged county totals.
7. Errata affecting a requested table appear in the plan and in manifest outputs.
8. Outputs open in ArcGIS Pro and QGIS (GeoPackage), verified by reading them back with GDAL.
9. CI stays green on Linux and Windows, Python 3.10–3.12.
