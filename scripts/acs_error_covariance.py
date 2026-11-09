# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACS sampling-error covariance between areas, from the VRE replicates (PLAN_ROUND_16 C1-C2).

Measures how the sampling errors of ACS 5-year estimates covary between
areas, and how reliably 80 replicates can measure that, for Michigan (26) and
New York (36) tracts and counties. Writes

    docs/audits/acs_sampling_covariance.md    methods, tables, conclusion
    docs/audits/acs_sampling_covariance.csv   one row per (release, state,
                                              geography, variable, pair class)

Usage:
    python scripts/acs_error_covariance.py                    # 2024 and 2019 (~10 min)
    python scripts/acs_error_covariance.py --years 2024       # one release
    python scripts/acs_error_covariance.py --no-holdout       # skip the dense model comparison

Data: Variance Replicate Estimate files (census.replicate.fetch_vre; cached
under $CENSUS_VRE_DIR, default .cache/vre) and cartographic boundary files
(census.boundaries; $CENSUS_GEO_DIR, default .cache/geo). Both are public
www2.census.gov downloads; no API key is used. Large files are never written
to the repository; the report lists their URLs and SHA-256.

Variables (types):
    count       B01003_001 total population (tracts; county totals are
                controlled to population estimates and have no sampling error),
                B25003_001 occupied housing units, B17001_002 persons below poverty
    proportion  B17001_002 / B17001_001 poverty rate, B25003_002 / B25003_001 owner share
                (evaluated on each replicate; zero replicate denominator -> 0, VRE doc. Example 7)
    median      household income, linearly interpolated from the B19001 replicate
                distributions (census.medians); no published median table has VREs

The study is info (PLAN_ROUND_16 section 5): it gates nothing.
"""

import argparse
import math
import os
import platform
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import census  # noqa: E402
from census import replicate_covariance as rc  # noqa: E402
from census.boundaries import boundary_vintage, equal_area_crs, get_boundaries  # noqa: E402
from census.medians import _quantile_matrix, distribution_brackets  # noqa: E402
from census.replicate import VREDownloadError, fetch_vre  # noqa: E402
from census.spatial import spatial_weights  # noqa: E402
from census.variance import REPLICATE_COLUMNS, REPLICATE_COUNT  # noqa: E402

OUT_MD = os.path.join(ROOT, "docs", "audits", "acs_sampling_covariance.md")
OUT_CSV = os.path.join(ROOT, "docs", "audits", "acs_sampling_covariance.csv")

STATES = {"26": "Michigan", "36": "New York"}
SEED = 20261108
N_BOOT = 200
MAX_ORDER = 3
WITHIN_COUNTY_CAP = 150_000  # far within-county tract pairs kept (random sample beyond this)
FAR_SAMPLE = 100_000  # random tract pairs drawn for the far, across-county class
CROSS_STATE_SAMPLE = 50_000  # MI x NY tract pairs (independent samples: empirical null)
TRACT_DIST_BINS = [0, 2, 5, 10, 20, 50, 100, np.inf]
COUNTY_DIST_BINS = [0, 50, 100, 200, 400, np.inf]

# name -> (type, kind, spec); kind 'cell', 'ratio' (num, den) or 'median' (table)
VARIABLES = {
    "population": ("count", "cell", "B01003_001"),
    "households": ("count", "cell", "B25003_001"),
    "poverty_count": ("count", "cell", "B17001_002"),
    "poverty_rate": ("proportion", "ratio", ("B17001_002", "B17001_001")),
    "owner_share": ("proportion", "ratio", ("B25003_002", "B25003_001")),
    "median_hh_income": ("median", "median", "B19001"),
}
TABLES = ("B01003", "B25003", "B17001", "B19001")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _load_tables(year, level, state, files):
    frames = {}
    for t in TABLES:
        try:
            f = fetch_vre(t, year, level, state, check_moe=False)
        except VREDownloadError as exc:  # e.g. census.gov firewall; reported, never retried in a loop
            log(f"{year} {level} {state} {t}: not available ({exc})")
            frames.setdefault("_missing", []).append(t)
            continue
        files.append({"kind": "VRE", "year": year, "level": level, "state": state, "table": t,
                      "url": f.attrs["source_url"], "sha256": f.attrs["sha256"]})
        frames[t] = f
    return frames


def _cell(frames, var, geoids):
    t = var.split("_")[0]
    f = frames[t]
    f = f[f["variable"] == var].set_index("GEOID").reindex(geoids)
    return f["estimate"].to_numpy(float), f[REPLICATE_COLUMNS].to_numpy(float), f["controlled"].fillna(False).to_numpy(bool)


def variable_values(frames, name, geoids, year):
    """(estimate (n,), replicates (n, 80), valid mask, note) for one study variable."""
    vtype, kind, spec = VARIABLES[name]
    if kind == "cell":
        est, reps, ctrl = _cell(frames, spec, geoids)
        valid = np.isfinite(est) & np.isfinite(reps).all(axis=1) & ~ctrl
        note = f"{int(ctrl.sum())} controlled" if ctrl.any() else ""
    elif kind == "ratio":
        num, num_r, _ = _cell(frames, spec[0], geoids)
        den, den_r, _ = _cell(frames, spec[1], geoids)
        with np.errstate(divide="ignore", invalid="ignore"):
            est = np.where(den > 0, num / den, np.nan)
            reps = np.where(den_r > 0, num_r / den_r, 0.0)
        valid = np.isfinite(est) & np.isfinite(reps).all(axis=1)
        note = ""
    else:
        br = distribution_brackets(spec, year)
        lo = np.array([b[1] for b in br])
        hi = np.array([b[2] for b in br])
        cells = [_cell(frames, b[0], geoids) for b in br]
        counts = np.column_stack([c[0] for c in cells])  # (n, k)
        rep_counts = np.stack([c[1] for c in cells], axis=2)  # (n, 80, k)
        n = counts.shape[0]
        full = _quantile_matrix(lo, hi, counts, 0.5)
        rq = _quantile_matrix(lo, hi, rep_counts.reshape(n * REPLICATE_COUNT, -1), 0.5)
        est = full["value"]
        reps = rq["value"].reshape(n, REPLICATE_COUNT)
        coded = full["top"] | full["bottom"]
        valid = full["valid"] & ~coded & rq["valid"].reshape(n, -1).all(axis=1) & np.isfinite(counts).all(axis=1)
        note = f"{int((full['valid'] & coded).sum())} top/bottom-coded medians excluded"
    reps = np.where(valid[:, None], reps, np.nan)
    est = np.where(valid, est, np.nan)
    with np.errstate(invalid="ignore"):
        zero = valid & (np.nansum((reps - est[:, None]) ** 2, axis=1) == 0)
    valid &= ~zero
    return est, reps, valid, {"type": vtype, "n_zero_variance": int(zero.sum()), "note": note}


def load_geography(year, level, state, files):
    by = boundary_vintage(year, level)
    gdf = get_boundaries(level, by, state if level == "tract" else None)
    if level == "county":
        gdf = gdf[gdf["GEOID"].str[:2] == state].reset_index(drop=True)
    files.append({"kind": "boundary", "year": by, "level": level, "state": state, "table": "",
                  "url": gdf.attrs["source_url"], "sha256": gdf.attrs["sha256"]})
    gdf = gdf[["GEOID", "geometry"]].reset_index(drop=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        queen = spatial_weights(gdf, "queen", transform="b")
        rook = spatial_weights(gdf, "rook", transform="b")
    assert list(queen.id_order) == list(gdf["GEOID"])
    orders = rc.contiguity_orders(queen, MAX_ORDER)
    rook1 = rc.contiguity_orders(rook, 1)
    pts = gdf.to_crs(equal_area_crs(gdf)).geometry.centroid
    xy = np.column_stack([pts.x.to_numpy(), pts.y.to_numpy()]) / 1000.0  # km
    return {"geoids": list(gdf["GEOID"]), "orders": orders, "rook1": rook1, "xy": xy,
            "county": gdf["GEOID"].str[:5].to_numpy(), "boundary_year": by,
            "weights": {"queen": queen.census_report, "rook": rook.census_report}}


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------


def build_pairs(geo, level, rng):
    """Pair index arrays with order (0 = beyond MAX_ORDER), same-county flag and distance."""
    n = len(geo["geoids"])
    o = sparse_triu(geo["orders"])
    i, j, order = o
    parts = [(i, j)]
    if level == "tract":
        county = geo["county"]
        wi, wj = [], []
        for c in np.unique(county):
            idx = np.flatnonzero(county == c)
            a, b = np.triu_indices(idx.size, 1)
            wi.append(idx[a])
            wj.append(idx[b])
        wi, wj = np.concatenate(wi), np.concatenate(wj)
        far = rc.pair_orders(geo["orders"], wi, wj) == 0
        wi, wj = wi[far], wj[far]
        if wi.size > WITHIN_COUNTY_CAP:
            keep = rng.choice(wi.size, WITHIN_COUNTY_CAP, replace=False)
            wi, wj = wi[keep], wj[keep]
        parts.append((wi, wj))
        ri = rng.integers(0, n, FAR_SAMPLE)
        rj = rng.integers(0, n, FAR_SAMPLE)
        a, b = np.minimum(ri, rj), np.maximum(ri, rj)
        ok = (a != b) & (county[a] != county[b])
        a, b = a[ok], b[ok]
        ok = rc.pair_orders(geo["orders"], a, b) == 0
        parts.append((a[ok], b[ok]))
    else:
        a, b = np.triu_indices(n, 1)
        ok = rc.pair_orders(geo["orders"], a, b) == 0
        parts.append((a[ok], b[ok]))
    pi = np.concatenate([p[0] for p in parts])
    pj = np.concatenate([p[1] for p in parts])
    key = pd.Index(pi.astype(np.int64) * n + pj).duplicated()
    pi, pj = pi[~key], pj[~key]
    order = rc.pair_orders(geo["orders"], pi, pj)
    same = geo["county"][pi] == geo["county"][pj]
    dist = np.sqrt(np.sum((geo["xy"][pi] - geo["xy"][pj]) ** 2, axis=1))
    rook = rc.pair_orders(geo["rook1"], pi, pj) == 1
    return {"i": pi, "j": pj, "order": order, "same": same, "dist": dist, "rook": rook}


def sparse_triu(m):
    from scipy import sparse
    t = sparse.triu(m, k=1).tocoo()
    return t.row.astype(np.int64), t.col.astype(np.int64), t.data.astype(int)


def label_sets(pairs, level):
    o = pairs["order"]
    order_lab = np.where(o > 0, np.char.add("order ", o.astype(str)), "beyond order 3").astype(object)
    sets = {"order": order_lab}
    if level == "tract":
        cnt = np.where(pairs["same"], "within county", "across county")
        sets["order_county"] = (order_lab + " / " + cnt).astype(object)
    rook_lab = np.where(o == 1, np.where(pairs["rook"], "order 1 rook (shared edge)", "order 1 queen-only (corner)"),
                        "")
    sets["rook"] = rook_lab.astype(object)
    bins = TRACT_DIST_BINS if level == "tract" else COUNTY_DIST_BINS
    cut = pd.cut(pairs["dist"], bins, right=False)
    sets["distance"] = np.asarray([f"{iv.left:g}-{iv.right:g} km" for iv in cut], dtype=object)
    return sets


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def holdout_models(geo, idx, level):
    """Dense class matrices for the valid areas ``idx``."""
    sub = geo["orders"][idx][:, idx].toarray().astype(np.int16)
    models = {"order (1-3)": np.where(sub > 0, sub - 1, -1)}
    if level == "tract":
        c = geo["county"][idx]
        same = c[:, None] == c[None, :]
        models["order x county"] = np.where(sub > 0, (sub - 1) * 2 + same, -1)
        models["order x county (+ far within county)"] = np.where(sub > 0, (sub - 1) * 2 + same,
                                                                   np.where(same, 6, -1))
    for m in models.values():
        np.fill_diagonal(m, -1)
    return models


def analyse(year, state, level, frames, geo, rng, do_holdout, out_rows, extras):
    pairs = build_pairs(geo, level, rng)
    sets = label_sets(pairs, level)
    gid = geo["geoids"]
    group = pd.factorize(geo["county"])[0]
    for name, (vtype, _, _) in VARIABLES.items():
        needed = {VARIABLES[name][2].split("_")[0]} if VARIABLES[name][1] != "ratio" else {
            c.split("_")[0] for c in VARIABLES[name][2]}
        if needed & set(frames.get("_missing", [])):
            extras["skipped"].append(f"{year} {STATES[state]} {level} {name}: VRE file could not be downloaded")
            continue
        est, reps, valid, info = variable_values(frames, name, gid, year)
        if name == "population" and level == "county":
            extras["skipped"].append(f"{year} {STATES[state]} county population: {info['note']} (no sampling error)")
            continue
        n_valid = int(valid.sum())
        d = np.zeros_like(reps)
        d[valid] = rc.replicate_deviations(est[valid], reps[valid])
        keep = valid[pairs["i"]] & valid[pairs["j"]]
        pi, pj = pairs["i"][keep], pairs["j"][keep]
        base = {"year": year, "state": STATES[state], "geography": level, "variable": name, "type": vtype}
        for sname, labs in sets.items():
            labs = labs[keep]
            sel = labs != ""
            tab = rc.summarize_pair_correlations(d, pi[sel], pj[sel], labs[sel], groups=group, n_boot=N_BOOT,
                                                 seed=SEED)
            for row in tab.to_dict(orient="records"):
                out_rows.append({**base, "classification": sname, **row})
        # noise floor and split-half on the order classification
        labs = sets["order"][keep]
        far = labs == "beyond order 3"
        perm = rc.permutation_null(d, pi[far], pj[far], n_perm=10, seed=SEED)
        halves = pd.concat([rc.split_half_agreement(d, pi, pj, labs, split=s, seed=SEED)
                            for s in ("random", "odd_even", "first_last")])
        extras["perm"].append({**base, **perm})
        for row in halves.to_dict(orient="records"):
            extras["split"].append({**base, **row})
        extras["n"].append({**base, "n_areas": len(gid), "n_valid": n_valid, **info})
        spec = rc.replicate_spectrum(d[valid])
        if level == "tract":
            extras["directions"][(year, state, name)] = spec["null_directions"]
        extras["spectrum"].append({**base, "n_null": spec["n_null"], "effective_replicates": spec["effective_replicates"],
                                   "lowest_eigen": [float(x) for x in spec["eigen_ratio"][:4]],
                                   "mp_lower_edge": spec["mp_lower_edge"], "note": spec["note"]})
        if vtype == "count" and level == "tract":
            _, agg = rc.aggregation_variance_ratio(d[valid], geo["county"][valid], n_boot=N_BOOT, seed=SEED)
            extras["agg"].append({**base, **agg})
        if do_holdout:
            idx = np.flatnonzero(valid)
            t0 = time.time()
            models = holdout_models(geo, idx, level)
            # Design near-null directions: detected on this set when it is large enough (tracts),
            # else taken from the same release's tracts (a property of the replicate design).
            dirs = (extras["directions"].get((year, state, name)) if level == "county" else None)
            remove = "auto" if dirs is None else np.vstack([dirs, np.ones((1, REPLICATE_COUNT))])
            ho = rc.holdout_model_comparison(d[idx], models, split="random", seed=SEED, remove_directions=remove)
            rank = rc.rank_report(d[idx])
            lw_full = rc.ledoit_wolf_intensity(d[idx])
            extras["holdout"].append({**base, **ho, "rank": rank, "lw_full": lw_full["intensity"],
                                      "seconds": time.time() - t0})
        extras["dev"][(year, state, level, name)] = (d, valid)
    log(f"{year} {STATES[state]} {level}: {len(gid)} areas, {pairs['i'].size} pairs")


def cross_state(year, level, extras):
    """MI x NY pairs: independent samples, an empirical null for the pairwise correlation."""
    rng = np.random.default_rng(SEED + 1)
    rows = []
    null = rc.null_correlation(REPLICATE_COUNT)
    for name in VARIABLES:
        a = extras["dev"].get((year, "26", level, name))
        b = extras["dev"].get((year, "36", level, name))
        if a is None or b is None:
            continue
        ia, ib = np.flatnonzero(a[1]), np.flatnonzero(b[1])
        if level == "tract":
            pi = rng.choice(ia, CROSS_STATE_SAMPLE)
            pj = rng.choice(ib, CROSS_STATE_SAMPLE)
        else:
            pi, pj = (g.ravel() for g in np.meshgrid(ia, ib, indexing="ij"))
        d = np.vstack([a[0], b[0]])
        r = rc.pair_correlations(d, pi, pj + a[0].shape[0])
        tab = rc.summarize_pair_correlations(d, pi, pj + a[0].shape[0], np.full(pi.size, "MI x NY"),
                                             n_boot=N_BOOT, seed=SEED)
        rows.append({"year": year, "geography": level, "variable": name, "type": VARIABLES[name][0],
                     "n_pairs": int(r.size), "mean": float(np.mean(r)), "se": float(tab["se_boot"].iloc[0]),
                     "sd": float(np.std(r)), "q975_abs": float(np.quantile(np.abs(r), 0.95)),
                     "share_beyond_critical": float(np.mean(np.abs(r) > null["critical_abs_r"]))})
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _f(x, nd=3):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "-"
    return f"{x:.{nd}f}"


def _ci(row):
    return f"{_f(row['mean'])} [{_f(row['ci_low'])}, {_f(row['ci_high'])}]"


def _md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join([" :--- "] * len(header)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _git_commit():
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:  # pragma: no cover
        return "unknown"


def _versions():
    import geopandas
    import libpysal
    import scipy
    return {"census-acs-toolkit": census.__version__, "python": platform.python_version(),
            "numpy": np.__version__, "scipy": scipy.__version__, "pandas": pd.__version__,
            "geopandas": geopandas.__version__, "libpysal": libpysal.__version__}


INTERPRETATION = """\
## Findings

Numbers below refer to the tables in this report (2020-2024 release unless
stated). r is the replicate correlation of two areas' sampling errors.

{findings}

## C2 conclusion

{conclusion}
"""


def write_report(df, extras, files, years, elapsed, findings, conclusion):
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    df.to_csv(OUT_CSV, index=False, float_format="%.5g")
    null = rc.null_correlation(REPLICATE_COUNT)
    null40 = rc.null_correlation(REPLICATE_COUNT // 2)
    L = []
    L.append("# ACS sampling-error covariance between areas (VRE replicates)\n")
    L.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by "
             f"`scripts/acs_error_covariance.py` (commit {_git_commit()}, {elapsed / 60:.1f} min). "
             "PLAN_ROUND_16 workstream C (C1-C2). Status: **info** (gates nothing). "
             f"Per-class numbers: `docs/audits/{os.path.basename(OUT_CSV)}`.\n")
    L.append("## Question\n")
    L.append("Do the sampling errors of ACS 5-year estimates covary between areas (neighbouring tracts or "
             "counties, tracts in the same county), and can 80 replicates measure that covariance well enough "
             "to build it into a measurement-error correction (workstream A1) or a LISA null model "
             "(workstream B)?\n")
    L.append("## Methods\n")
    L.append(
        "* **Covariance.** For estimates X (area i) and Y (area j) of one release, "
        "Cov = 4/80 sum_r (X_r - X)(Y_r - Y) over the 80 successive-difference replicates of the Census "
        "Variance Replicate Estimate (VRE) tables, the cross-product form of the VRE variance formula "
        "(census.replicate.VRE_DOCUMENTATION, formula 1). The 4/80 factor cancels in the correlation r, "
        "which is the cosine of the two replicate-deviation vectors. Implementation: "
        "`census/replicate_covariance.py`.\n"
        "* **Variables.** Counts: total population B01003_001 (tracts only; county populations are "
        "controlled to the population estimates and have no sampling error), occupied housing units "
        "B25003_001, persons below poverty B17001_002. Proportions: poverty rate B17001_002/B17001_001 and "
        "owner share B25003_002/B25003_001, recomputed on every replicate (zero replicate denominator -> 0, "
        "VRE documentation Example 7). Median: household income linearly interpolated from the B19001 "
        "replicate distributions (census.medians; no published median table has VRE files - checked against "
        "the 2019 and 2024 VRE table lists); areas whose median is top- or bottom-coded are excluded. Areas "
        "with a zero estimate denominator, controlled estimates or zero replicate variance are excluded "
        "(their correlation is undefined).\n"
        "* **Pairs.** Contiguity from queen weights (libpysal via census.spatial.spatial_weights) on the "
        "Census cartographic boundary files of the release's vintage; orders 1-3 are graph distances in the "
        "queen contiguity graph (order 1 split into rook = shared edge vs queen-only = corner). Tracts: all "
        f"pairs of order 1-3; tract pairs in the same county beyond order 3 (random sample of at most "
        f"{WITHIN_COUNTY_CAP:,}); and a random sample of {FAR_SAMPLE:,} draws of tract pairs in different "
        "counties beyond order 3. Counties: all pairs in the state. Distance = centroid distance in an "
        "equal-area projection (EPSG:5070).\n"
        "* **Structured summaries, not pairwise matrices.** The replicate covariance of n areas has rank "
        "<= 80, and one pairwise r from 80 replicates has null SD 1/sqrt(80). So only class summaries are "
        "reported: mean and median r per contiguity order, within vs across county, distance band. Their "
        f"uncertainty: a bootstrap over the 80 replicate columns ({N_BOOT} draws; replicate noise; "
        "approximate because SDR deviations are not exactly iid across replicates) and a delete-one-county "
        "jackknife (drops every pair touching the county; area heterogeneity plus replicate noise). "
        "Intervals are mean +/- 1.96 x the larger of the two SEs.\n"
        "* **Noise floor.** Analytic: if one deviation vector is spherically symmetric in R^80 and "
        f"independent of the other, (1 + r)/2 ~ Beta(39.5, 39.5): SD {null['sd']:.3f}, two-sided 5% critical "
        f"|r| {null['critical_abs_r']:.3f} (40 replicates: SD {null40['sd']:.3f}, critical "
        f"{null40['critical_abs_r']:.3f}). Empirical: (a) replicate-index derangements applied to the "
        "second area of each far pair; (b) Michigan x New York area pairs, whose samples are independent, "
        "so their correlations show the real null spread of SDR replicate correlations.\n"
        "* **Reliability.** Split-half agreement: per-pair r computed on two disjoint halves of the "
        "replicates (random, odd/even, 1-40 vs 41-80); the correlation across pairs of the two halves' r is "
        "the reliability of a 40-replicate pairwise r, stepped up to 80 replicates by Spearman-Brown. "
        "Aggregation ratio (counts, tracts): Var(sum of a county's tracts) / sum of the tracts' variances "
        "= 1 + sum of within-county covariances / sum of variances; < 1 means negatively correlated errors "
        "(the signature of weighting controls), and it pools every within-county pair in one number.\n"
        "* **Replicate design spectrum.** The eigenvalues of the replicate-space Gram matrix of standardized "
        "deviations (80 x 80, over thousands of tracts) are compared with the Marchenko-Pastur lower edge "
        "for independent replicates (`replicate_spectrum`). Directions far below it are near-null for every "
        "area: a property of the replicate-factor design, not of geography. They lower the effective number "
        "of replicates, hence widen the null spread of r.\n"
        "* **Structured model and shrinkage, scored out of sample.** Replicates are split in half (random, "
        "seeded); on one half fit (i) one correlation per contiguity order 1-3 (0 beyond), (ii) for tracts "
        "one per order x within/across county (0 beyond order 3), (iii) as (ii) plus one for far pairs "
        "within a county, and (iv) Ledoit-Wolf shrinkage of the replicate correlation toward the identity; "
        "score the Gaussian log-likelihood of the other half's standardized replicate deviations (scaled so "
        "E[x x^T] = Sigma) relative to independence; average both directions. Each half is first projected "
        "orthogonally to the design near-null directions and the constant vector: otherwise the held-out "
        "half's component along those directions is minus the training half's for every area at once, a "
        "cross-half dependence that Ledoit-Wolf exploits (it then 'gains' 30-90 nats per replicate even on "
        "variables with no neighbour correlation; with the projection it loses, as it does on independent "
        "synthetic data). A gain > 0 means the fitted structure predicts held-out replicates better than "
        "independent errors. A fitted structure that is not positive definite is projected to the nearest "
        "correlation matrix with eigenvalues >= 1e-3 and flagged.\n")
    L.append("## Data\n")
    L.append(f"Releases {', '.join(f'{y - 4}-{y}' for y in years)}; states Michigan (26) and New York (36). "
             "Files (cached, not committed):\n")
    seen = set()
    rows = []
    for f in files:
        if f["url"] in seen:
            continue
        seen.add(f["url"])
        rows.append([f["kind"], f["year"], f["level"], f"`{f['url'].rsplit('/', 1)[-1]}`", f"`{f['sha256']}`"])
    L.append(_md_table(["kind", "year", "level", "file", "SHA-256"], rows) + "\n")
    L.append("Versions: " + ", ".join(f"{k} {v}" for k, v in _versions().items()) + "\n")
    L.append("Areas analysed (after exclusions):\n")
    nrows = [[r["year"], r["state"], r["geography"], r["variable"], r["type"], r["n_areas"], r["n_valid"],
              r["n_zero_variance"], r["note"]] for r in extras["n"]]
    L.append(_md_table(["year", "state", "geography", "variable", "type", "areas", "analysed",
                        "zero variance", "note"], nrows) + "\n")
    if extras["skipped"]:
        L.append("Skipped: " + "; ".join(extras["skipped"]) + ".\n")

    def class_table(year, geo, sname, classes):
        sub = df[(df["year"] == year) & (df["geography"] == geo) & (df["classification"] == sname)]
        if not len(sub):
            return f"_Not analysed for {geo}s in this release (see Data: skipped)._"
        hdr = ["state", "variable"] + classes
        rows = []
        for (st, var), g in sub.groupby(["state", "variable"], sort=False):
            cells = []
            for c in classes:
                r = g[g["class"] == c]
                cells.append(_ci(r.iloc[0]) + f" (n={int(r.iloc[0]['n_pairs'])})" if len(r) else "-")
            rows.append([st, var] + cells)
        return _md_table(hdr, rows)

    for year in years:
        L.append(f"## Results {year - 4}-{year}\n")
        L.append("Mean pairwise r [95% interval] (number of pairs) by queen contiguity order.\n")
        for geo in ("tract", "county"):
            L.append(f"### {'Tracts' if geo == 'tract' else 'Counties'}: contiguity order\n")
            L.append(class_table(year, geo, "order", ["order 1", "order 2", "order 3", "beyond order 3"]) + "\n")
        L.append("### Tracts: within vs across county (weighting controls)\n")
        L.append(class_table(year, "tract", "order_county",
                             ["order 1 / within county", "order 1 / across county", "order 2 / within county",
                              "order 2 / across county", "beyond order 3 / within county",
                              "beyond order 3 / across county"]) + "\n")
        L.append("### Rook (shared edge) vs queen-only (corner) first-order neighbours\n")
        for geo in ("tract", "county"):
            L.append(f"{'Tracts' if geo == 'tract' else 'Counties'}:\n")
            L.append(class_table(year, geo, "rook", ["order 1 rook (shared edge)", "order 1 queen-only (corner)"])
                     + "\n")
        L.append("### By centroid distance\n")
        for geo, bins in (("tract", TRACT_DIST_BINS), ("county", COUNTY_DIST_BINS)):
            labs = [f"{a:g}-{b:g} km" for a, b in zip(bins[:-1], bins[1:])]
            L.append(f"{'Tracts' if geo == 'tract' else 'Counties'}:\n")
            L.append(class_table(year, geo, "distance", labs) + "\n")
        agg = [a for a in extras["agg"] if a["year"] == year]
        if agg:
            L.append("### Aggregation ratio: Var(county sum of tracts) / sum of tract variances\n")
            L.append(_md_table(
                ["state", "variable", "counties", "tracts", "pooled ratio (SE)", "median county ratio",
                 "implied mean within-county r (SE)"],
                [[a["state"], a["variable"], a["n_groups"], a["n_areas"],
                  f"{_f(a['pooled_ratio'])} ({_f(a['pooled_ratio_se_boot'])})", _f(a["median_group_ratio"]),
                  f"{_f(a['pooled_implied_mean_corr'], 4)} ({_f(a['pooled_implied_mean_corr_se_boot'], 4)})"]
                 for a in agg]) + "\n")
        L.append("### Noise floor and spread\n")
        L.append(f"Analytic null (80 replicates): SD {null['sd']:.3f}, 95% |r| {null['critical_abs_r']:.3f}.\n")
        cs = [c for c in extras["cross"] if c["year"] == year]
        if cs:
            L.append("Michigan x New York pairs (independent samples):\n")
            L.append(_md_table(["geography", "variable", "pairs", "mean r (SE)", "SD of r", "95% |r|",
                                "share |r| > analytic critical"],
                               [[c["geography"], c["variable"], c["n_pairs"], f"{_f(c['mean'], 4)} ({_f(c['se'], 4)})",
                                 _f(c["sd"]), _f(c["q975_abs"]), _f(c["share_beyond_critical"])] for c in cs]) + "\n")
        pm = [p for p in extras["perm"] if p["year"] == year]
        L.append("Derangement null on far pairs (same state, beyond order 3):\n")
        L.append(_md_table(["state", "geography", "variable", "pairs", "mean", "SD", "95% |r|"],
                           [[p["state"], p["geography"], p["variable"], p["n_pairs"], _f(p["mean"], 4), _f(p["sd"]),
                             _f(p["critical_abs_r"])] for p in pm]) + "\n")
        spec = [x for x in extras["spectrum"] if x["year"] == year]
        if spec:
            L.append("Replicate design spectrum (lowest eigenvalues relative to the mean; Marchenko-Pastur lower "
                     "edge for independent replicates in brackets):\n")
            L.append(_md_table(["state", "geography", "variable", "lowest 4 eigenvalues (MP edge)", "near-null",
                                "effective replicates", "null SD 1/sqrt(eff.)"],
                               [[x["state"], x["geography"], x["variable"],
                                 " ".join(_f(v) for v in x["lowest_eigen"]) + f" ({_f(x['mp_lower_edge'])})",
                                 x["n_null"] if not x["note"] else "not tested (too few areas)",
                                 x["effective_replicates"] if not x["note"] else "-",
                                 _f(1 / math.sqrt(x["effective_replicates"])) if not x["note"] else "-"]
                                for x in spec]) + "\n")
        sub = df[(df["year"] == year) & (df["classification"] == "order")]
        cross_sd = {(c["geography"], c["variable"]): c["sd"] for c in cs}
        L.append("Spread of pairwise r per class: SD, excess SD over the analytic null (sqrt(SD^2 - 1/80)) and "
                 "over the empirical Michigan x New York null (sqrt(SD^2 - SD_cross^2)), and the share of pairs "
                 "with |r| above the analytic 5% critical value (0.05 expected under no correlation; about 0.06 "
                 "in the independent cross-state pairs):\n")

        def _excess(r):
            c = cross_sd.get((r["geography"], r["variable"]))
            return math.sqrt(max(0.0, r["sd"] ** 2 - c ** 2)) if c is not None else math.nan

        L.append(_md_table(["state", "geography", "variable", "class", "SD of r", "excess SD (analytic)",
                            "excess SD (cross-state)", "share |r| > crit"],
                           [[r["state"], r["geography"], r["variable"], r["class"], _f(r["sd"]), _f(r["excess_sd"]),
                             _f(_excess(r)), _f(r["share_beyond_critical"])]
                            for r in sub.to_dict(orient="records")]) + "\n")
        sp = pd.DataFrame([s for s in extras["split"] if s["year"] == year])
        if len(sp):
            L.append("### Split-half reliability of individual pairwise correlations\n")
            L.append("Per class: half-A and half-B class means (random split), and the reliability of a single "
                     "pair's r (correlation across pairs between halves; stepped up to 80 replicates), for each "
                     "split.\n")
            rows = []
            for (st, geo, var, cl), g in sp.groupby(["state", "geography", "variable", "class"], sort=False):
                rnd = g[g["split"] == "random"].iloc[0]
                rel = " / ".join(_f(g[g["split"] == s]["pair_reliability_full"].iloc[0], 2)
                                 for s in ("random", "odd_even", "first_last"))
                rows.append([st, geo, var, cl, rnd["n_pairs"], _f(rnd["mean_half_a"]), _f(rnd["mean_half_b"]), rel])
            L.append(_md_table(["state", "geography", "variable", "class", "pairs", "mean (half A)",
                                "mean (half B)", "reliability (random / odd-even / first-last)"], rows) + "\n")
        ho = [h for h in extras["holdout"] if h["year"] == year]
        if ho:
            L.append("### Held-out replicate log-likelihood gain over independence (nats per replicate)\n")
            names = list(dict.fromkeys(k for h in ho for k in h["gain_nats_per_replicate"]))
            rows = []
            for h in ho:
                g = h["gain_nats_per_replicate"]
                rho = h["fitted_rho"]["order (1-3)"]
                rho1 = np.mean([p[0] for p in rho]) if rho and len(rho[0]) else math.nan
                rows.append([h["state"], h["geography"], h["variable"], h["n_areas"]]
                            + [(_f(g[k], 2) + (" (projected)" if h["projected_to_pd"].get(k) else "")) if k in g else "-"
                               for k in names]
                            + [_f(rho1), _f(np.mean(h["ledoit_wolf_intensity"])), _f(h["lw_full"]),
                               f"{h['rank']['rank']} / {h['rank']['n_areas']}"])
            L.append(_md_table(["state", "geography", "variable", "areas"] + names
                               + ["fitted order-1 r (halves)", "LW intensity (40 reps)", "LW intensity (80 reps)",
                                  "rank / areas"], rows) + "\n")
    if len(years) > 1:
        a, b = years[0], years[-1]
        L.append(f"## Stability across releases ({b - 4}-{b} vs {a - 4}-{a})\n")
        L.append("Mean r per class in each release (different tract vintages: 2010 tracts in 2015-2019, 2020 "
                 "tracts in 2020-2024; so pair sets differ). Difference with a combined SE.\n")
        sub = df[df["classification"].isin(["order", "order_county"])]
        rows = []
        for (st, geo, var, cl), g in sub.groupby(["state", "geography", "variable", "class"], sort=False):
            ra, rb = g[g["year"] == a], g[g["year"] == b]
            if not (len(ra) and len(rb)):
                continue
            ra, rb = ra.iloc[0], rb.iloc[0]
            diff = ra["mean"] - rb["mean"]
            se = math.hypot(ra["se"], rb["se"])
            rows.append([st, geo, var, cl, _ci(rb), _ci(ra), f"{_f(diff)} ({_f(se)})"])
        L.append(_md_table(["state", "geography", "variable", "class", f"{b}", f"{a}", "difference (SE)"], rows)
                 + "\n")
    L.append(INTERPRETATION.format(findings=findings, conclusion=conclusion))
    L.append(LIMITATIONS)
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L).rstrip() + "\n")


LIMITATIONS = """\
## Limitations

* Two states, two releases. Michigan and New York contain dense urban tracts (Detroit, New York City) and
  sparse rural ones; other states' weighting areas and sample sizes differ.
* Counties stand in for ACS weighting areas. Census controls weights to population and housing-unit
  estimates within weighting areas that are counties or groups of small counties, so "within county" is an
  approximation of "within weighting area"; for small Michigan counties grouped into one weighting area the
  negative covariance would appear "across county".
* The replicate bootstrap treats the 80 SDR replicate deviations as exchangeable draws. They are
  constructed from a Hadamard design and are close to, but not exactly, iid; the delete-one-county jackknife
  and the split-half comparisons are reported alongside as checks, and every interval uses the larger SE.
* Tract-pair classes beyond order 3 are random samples (sizes in the CSV), so their means carry sampling
  error from that draw as well (included in both SEs).
* The median is a linear interpolation from B19001 brackets, not Census's published B19013 (computed from
  finer internal distributions; no VRE file exists for it). Its replicate deviations are therefore those of
  the interpolated statistic.
* Correlation is reported, not covariance in units: covariance in units scales with each area's
  variance, which the published MOEs already give; only the correlation structure is new information.
* Distance bins use centroid distance; large rural tracts make "0-2 km" mostly urban pairs.
"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--years", type=int, nargs="+", default=[2024, 2019])
    p.add_argument("--no-holdout", action="store_true", help="skip the dense held-out model comparison")
    p.add_argument("--holdout-years", type=int, nargs="*", default=[2024],
                   help="releases for the dense held-out model comparison (default 2024)")
    p.add_argument("--tract-years", type=int, nargs="*", default=[2024, 2019],
                   help="releases analysed at tract level (counties: every --years release)")
    p.add_argument("--results", help="pickle of computed results: loaded if it exists (re-render the report "
                   "only), else written after the computation")
    args = p.parse_args(argv)
    t0 = time.time()
    if args.results and os.path.exists(args.results):
        import pickle
        with open(args.results, "rb") as fh:
            df, extras, files, elapsed = pickle.load(fh)
        findings, conclusion = interpret(df, extras, args.years)
        write_report(df, extras, files, args.years, elapsed, findings, conclusion)
        log(f"re-rendered {OUT_MD} from {args.results}")
        return 0
    rng = np.random.default_rng(SEED)
    files, rows = [], []
    extras = {"perm": [], "split": [], "n": [], "agg": [], "holdout": [], "dev": {}, "skipped": [], "cross": [],
              "spectrum": [], "directions": {}}
    for year in args.years:
        for level in ("tract", "county"):
            if level == "tract" and year not in args.tract_years:
                continue
            for st in STATES:
                frames = _load_tables(year, level, st, files)
                geo = load_geography(year, level, st, files)
                analyse(year, st, level, frames, geo, rng,
                        not args.no_holdout and year in args.holdout_years, rows, extras)
            extras["cross"] += cross_state(year, level, extras)
            extras["dev"].clear()
        extras["directions"].clear()
    df = pd.DataFrame(rows)
    if args.results:
        import pickle
        with open(args.results, "wb") as fh:
            pickle.dump((df, extras, files, time.time() - t0), fh)
    findings, conclusion = interpret(df, extras, args.years)
    write_report(df, extras, files, args.years, time.time() - t0, findings, conclusion)
    log(f"wrote {OUT_MD} and {OUT_CSV}")
    return 0


def interpret(df, extras, years):
    """Narrative text; see FINDINGS / CONCLUSION (written for the recorded file hashes)."""
    return FINDINGS, CONCLUSION


# Written for the run recorded in docs/audits/acs_sampling_covariance.md (file hashes listed there); the
# tables are regenerated, this text is not, so re-read it against them after a rerun on other files.
FINDINGS = """\
1. **Noise floor.** One pairwise r from 80 replicates is noise-dominated. Analytic null SD 0.112 (95% |r|
   0.219). Real independent pairs (Michigan x New York tracts) have mean r within 0.001 of 0 but SD
   0.115-0.116 and 95% |r| 0.226-0.236; 6% of them exceed the analytic critical value. The reason is the
   replicate design: in every variable and both states the same 3 directions of replicate space are
   near-null (eigenvalues 0.02-0.11 of the mean vs the Marchenko-Pastur edge 0.69-0.77), so a
   correlation has about 77 effective replicates (null SD 1/sqrt(77) = 0.114). Derangements of replicate
   indices destroy this structure and give the textbook 0.112, so they understate the real floor
   slightly. Practical floor: |r| below ~0.23 is indistinguishable from 0 for a single pair.
2. **Uncontrolled characteristics: no detectable neighbour correlation.** Poverty rate, owner share and
   interpolated median household income have mean tract r within +/-0.003 of 0 at every contiguity
   order, within and across counties, and at every distance, with 95% intervals of about +/-0.002-0.003
   (e.g. order 1: Michigan poverty rate -0.000 [-0.003, 0.002], New York owner share -0.001 [-0.002,
   0.001], Michigan median income 0.000 [-0.002, 0.003]). The poverty count is the same with a slight
   negative tilt among neighbours in the same county (Michigan order 1 -0.004 [-0.007, -0.002]).
   Counties: the class means for these variables lie in -0.015..+0.025 with intervals of about
   +/-0.02-0.03, none clearly different from the cross-state null (which itself has small positive means,
   +0.006 to +0.018, from the offset described in 4). The per-pair spread beyond the cross-state null is ~0 and
   the split-half reliability of an individual pair's r is 0.02-0.07, the same as for far pairs: the
   individual pairwise correlations are replicate noise.
3. **Controlled totals: negative correlation among neighbours in the same county.** Total population
   (B01003, whose county total is controlled to the population estimates) and occupied housing units
   (B25003_001, tied to the housing-unit controls) show clearly negative first-order correlations inside
   counties: population order 1 within county Michigan -0.081 [-0.106, -0.056], New York -0.043
   [-0.065, -0.020]; households -0.028 [-0.039, -0.018] and -0.016 [-0.024, -0.008]. They decay fast
   with order (population order 2: -0.018 / -0.010; order 3: -0.007 / -0.004; far within county: -0.001)
   and vanish across county lines at every order (order 1 across county: +0.003 [-0.007, 0.014] and
   +0.002 [-0.006, 0.009]). Rook (shared-edge) pairs are more negative than corner-only pairs (Michigan
   population -0.080 vs -0.029). The aggregation ratio confirms the control mechanism without any
   pairwise noise: Var(sum of a county's tracts) / sum of the tract variances is 0.000 for population in
   Michigan and 0.005 in New York (the tract errors sum to exactly zero within a county), 0.22 / 0.26 for
   households and 0.92 / 0.88 for the poverty count. Here the structure is real at the pair level too:
   split-half reliability of an individual neighbour pair's r is 0.6-0.67 for population and 0.2-0.34 for
   households (excess SD 0.13-0.16 and 0.06-0.08), i.e. strongly heterogeneous, with some neighbour pairs
   far more negative than the mean (a few tracts sharing a county's controlled total).
4. **A design-wide positive offset, not spatial covariance, at the county level.** County households
   (and New York county owner share in 2015-2019, +0.02-0.04) show mean r of about +0.03 at every order
   and distance - and
   equally between Michigan and New York counties (+0.029-0.032), whose samples are independent. The
   replicate estimates of these counties sit systematically above the full-sample estimate (mean
   deviation about 0.18 of its RMS), so the uncentred 4/80 cross-products of any two such counties are
   positive (0.18^2 = 0.03). This is part of what the ACS covariance formula returns, but it is not a
   neighbourhood effect, and with 80 replicates it cannot be separated from a true common component.
5. **Structured models out of sample.** One correlation per contiguity order, fitted on 40 replicates,
   predicts the other 40 better than independence only where (3) found structure: population +33 (MI) and
   +17 (NY) nats per replicate, households +4.4 / +2.2; for every uncontrolled variable and for counties
   the gain is within +/-0.2 of 0 (no improvement). Splitting by county adds a little for households
   (+5.1) and, for population in Michigan, cannot be represented as a positive-definite correlation
   matrix (projected, -165): the within-county constraint is an exact linear restriction (the covariance
   is singular), not a small correlation. Adding a single far-within-county correlation makes every
   controlled-total fit non-positive-definite. Ledoit-Wolf shrinkage toward the identity has intensity
   0.87 (40 replicates) to 0.93 (80) for tracts - it keeps only 7-13% of the sample correlation - and,
   scored on held-out replicates after removing the design directions, it is worse than independence
   for every variable (-25 to -50 nats for tracts). Without that projection it appeared to gain 30-90
   nats, entirely from the cross-half dependence created by the near-null design directions.
6. **Stability across releases.** 2015-2019 tract VRE files could not be downloaded in this session
   (census.gov answered with its firewall 'Request Rejected' page; the script is not retried in a loop),
   so the release comparison covers counties only. County class means agree between 2015-2019 and
   2020-2024 within about 1.5 combined SEs for every variable and order except New York owner share
   beyond order 3 (+0.036 vs +0.012, difference -0.024, SE 0.011) - the same county-wide offset as in
   (4), not a neighbour effect. Rerun with `--tract-years 2024 2019` when the tract files are reachable.
"""

CONCLUSION = """\
**The covariance structure is estimable only as a few structured summaries, and for the variables most
analyses map it is indistinguishable from zero.**

* For characteristics that are not weighting controls (rates, shares, medians and most counts), the mean
  sampling-error correlation between neighbouring tracts is 0 to within about +/-0.003 (95%), at orders
  1-3, within and across counties, in Michigan and New York; between neighbouring counties it is 0 to
  within about +/-0.03. Individual pairwise correlations are replicate noise (reliability ~0.05). There
  is nothing to estimate beyond "approximately independent", and an unrestricted or shrunk replicate
  covariance would only add noise (Ledoit-Wolf loses out of sample).
* For controlled totals (population, households, and to a lesser degree counts tied to them) the
  structure is real and estimable: negative, confined to first- and second-order neighbours inside the
  same county (order 1: -0.08 to -0.04 for population, -0.03 to -0.015 for households), and summarised
  far more precisely by the within-county aggregation ratio (population: tract errors sum to zero within
  a county). It is a linear constraint, not a smooth correlation function: a one-parameter-per-order
  model captures it out of sample, a full within-county correlation model is not positive definite.
* Using it. In a measurement-error correction (A1) or LISA null model (B), sampling errors of rates,
  shares and medians can be treated as independent across areas, with that assumption cited to this
  study. For controlled counts the correct treatment is the replicate covariance itself (4/80 D D^T,
  rank 80 - usable for sums and contrasts, as census.replicate already does), or a structured model with
  one negative correlation per contiguity order within county; a generic positive spatial correlation
  (e.g. SAR errors) has the wrong sign for them.
* **For the LISA parameter grid (-0.2..0.5):** the data place ACS sampling-error neighbour correlation at
  about 0 (|rho| <= 0.003) for rates, shares and medians, and at -0.02 to -0.08 (95% bounds -0.11 to
  -0.01) for controlled counts in tracts; no variable, geography or release showed a positive neighbour
  correlation beyond the design-wide county offset of about +0.03. Grid values of +0.1..0.5 are therefore
  stress tests outside the observed range, not plausible ACS conditions for these variables; the
  realistic end of the grid is -0.1..0.05. Correlated-error scenarios with large positive rho should be
  stated as hypothetical (for example for variables or survey designs not studied here).
* Limits: two states, one release at tract level, 80 replicates (about 77 effective), and counties as a
  proxy for weighting areas. The 95% |r| of 0.23 for a single pair means no individual pair can be
  flagged; only class means are evidence.
"""


if __name__ == "__main__":
    sys.exit(main())
