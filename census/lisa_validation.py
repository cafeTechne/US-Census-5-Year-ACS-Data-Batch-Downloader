# SPDX-License-Identifier: AGPL-3.0-or-later
"""LISA under ACS sampling error: five scenarios, four methods (PLAN_ROUND_16 workstream B).

Validation code only. The candidate methods below are prototypes for this
study; none of them changes the behavior of :func:`census.spatial.local_morans`
or any other tool. Results and conclusions: ``docs/audits/lisa_validation.md``.

Design (12 x 12 lattice, rook contiguity, 144 areas)
----------------------------------------------------
True values theta_i ~ N(LV_MU, LV_TAU^2) independently, so the truth has no
spatial clustering, except in the cluster scenarios, where a LV_BLOCK x
LV_BLOCK block in the middle of the map is raised by LV_SHIFT (a hot spot).
Published estimates y = theta + e. Scenarios (PLAN_ROUND_16 B1):

1. ``independent_homogeneous``: e_i ~ N(0, s^2) independent, s = LV_SE for
   every area (median tract CV x LV_MU);
2. ``heterogeneous_independent``: e_i ~ N(0, s_i^2) independent, s_i the
   tract-profile CVs (x LV_MU) sorted along the lattice diagonal: variances
   vary smoothly across space, errors do not covary. This is the scenario of
   the Round 15 finding (study 'local_morans_size', "38% of maps");
3. ``correlated``: e ~ N(0, S R S), S = s I (homogeneous variances, so the
   scenario isolates covariance from heterogeneity), R a correlation
   matrix with mean first-order (rook) neighbour correlation r1;
4. ``cluster_independent``: hot spot, errors as in 1;
5. ``cluster_correlated``: hot spot, errors as in 3.

Error correlation (assumption, not an ACS measurement): R is the SAR(1)
covariance (I - lam W)^-1 (I - lam W')^-1 (W the row-standardized rook
matrix) rescaled to unit diagonal, with lam solved so that the mean rook
neighbour correlation equals r1 in :data:`LV_NEIGHBOUR_CORRS` (-0.2 .. 0.5;
r1 = 0 is scenario 1/4). Negative r1 (a possible effect of weighting
controls that hold area totals) gives a checkerboard structure: second-order
neighbours are positively correlated. The grid spans the range in which
workstream C's replicate-based neighbour correlations can be located.

All r1 values of one repetition share theta and the standard-normal draw z
(e = S L z, L the Cholesky factor of R), so differences between r1 values are
paired (common random numbers).

Replicates: 80 replicate deviations per map, d_r = S L z_r / 2 (z_r a fresh
standard-normal vector per replicate), then rescaled per area so that
4/80 sum_r d_ir^2 = s_i^2 exactly (as census.simulation.make_replicates);
the replicate maps are y + d_r. Their cross-area covariance is the
scenario's error covariance (up to the per-area rescaling).

Methods (B2), each applied to the same maps
-------------------------------------------
* ``lisa`` - census.spatial.local_morans with its defaults (two-sided
  p-values, 9999 permutations, BH, alpha 0.10). Flag: significant. Cluster
  label: HH/LL.
* ``lisa_stable`` - the same call with replicates: an area is flagged when
  significant AND replicate_quadrant_share >= LV_STABLE (stated threshold).
  replicate_quadrant_share is a stability diagnostic, not a calibrated
  probability; this reading measures what filtering by it does, and
  ``stable_only`` (HH/LL with share >= LV_STABLE, no significance) shows
  what stability alone would flag.
* ``lisa_eblup`` - local_morans (defaults) on spatial Fay-Herriot EBLUPs
  (census.sae_spatial, REML, intercept only, the analysis W, D = s_i^2; the
  model assumes independent sampling errors). Reported with the smoothing
  diagnostics: estimated rho, global Moran's I of the EBLUPs vs the true
  values, the hot-spot contrast relative to the truth, and the paired change
  in the false-cluster indicator relative to ``lisa`` on the direct
  estimates.
* ``wong_cl<CL>`` - Wong (2021, IJERPH 18:9848) heuristic hot spot / cold
  spot detection, as far as the paper specifies it
  (:func:`wong_hot_cold_spots`): seeds are areas whose estimate lies beyond
  the analyst's thresholds (here LV_MU +/- LV_WONG_Z x LV_TAU, a stated
  domain threshold on the true-value scale); a first-order neighbour of a
  member joins the seed's spot when its estimate is not significantly
  different from the seed's estimate, compared with the seed (not with the
  member), repeated until no neighbour joins. The paper's worked example
  fixes the operational rule: a neighbour joins when the two-sided p-value
  of the difference is >= CL (Louisiana/Mississippi p = 0.65 joins at
  CL 0.648; Alabama/Mississippi p = 0.13 at CL 0.128). The test statistic is
  the independent-errors z = (y_s - y_j) / sqrt(s_s^2 + s_j^2) (the paper
  says "t-test" without a formula; this is the ACS comparison formula).
  Wong's method targets a different estimand from LISA: latent elevation
  beyond a threshold, not local similarity beyond chance. Under "no
  clustering" it still reports spots wherever extreme values occur, so it
  is not scored with LISA's false-positive metrics but against its own
  target (:func:`wong_metrics`): ``exceed_recall`` (areas whose true value is
  beyond a threshold that are flagged with that label), ``seed_precision``
  (seeds whose true value is beyond the threshold), ``member_near_share``
  (grown, non-seed members whose true value is within LV_WONG_NEAR of their
  seed's), ``member_beyond_share``, ``growth_per_seed`` (grown members per
  seed: as a function of the SEs and of the error correlation, which the
  independence-based test ignores) and, with a hot spot, ``power`` and
  ``jaccard`` for the hot label against the block.

Three questions are kept apart (PLAN_ROUND_16 B4): latent elevation (Wong),
local similarity beyond chance (LISA's significance), stability across
replicates (replicate_quadrant_share). In a simulation the truth is known,
so evidence of a genuine pattern is scored against it.

Metrics (B3), per map, then averaged over maps with the between-map MC SE
--------------------------------------------------------------------------
Null areas: every area in scenarios 1-3; areas outside the block in 4-5.
* ``fpr``: flagged null areas / null areas;
* ``fdr``: mean false discovery proportion, flagged null / flagged (0 when
  nothing is flagged), i.e. the FDR that BH controls;
* ``false_cluster_map_k2``, ``false_cluster_map_k4`` (the false-cluster map
  rate; not Sun et al.'s (2015) false cluster rate, which weights
  predefined clusters): 1 if the map has at least one
  rook-connected component of areas flagged with the same cluster label
  (HH or LL; hot or cold) of size >= k made only of null areas;
* ``power``: block areas flagged with the correct label (HH; hot) / block
  areas;
* ``jaccard``: |F n T| / |F u T|, F the areas flagged with the correct
  label, T the block;
* ``halo_share``: share of the false discoveries that are rook neighbours of
  the block (cluster scenarios; "halo" flags next to a real hot spot).
Sensitivity to W: the ``lisa`` method is also run with queen contiguity,
k-nearest neighbours (k = LV_KNN) and a misspecified W (rook contiguity of a
fixed random permutation of the areas: unrelated to the map) on selected
maps. Decomposition: the same truth analysed without sampling error
(``no_sampling_error``), with independent error, with heterogeneous
variances, with correlated errors at each r1, and with the misspecified W.

Studies (info rows only, run on demand: ``--studies lisa_validation_null
lisa_validation_clusters``): one row per (scenario, error structure, method,
weights, metric) with ``coverage`` = the metric's mean, ``mc_se`` its MC SE.
"""

import math
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import linalg as sla, optimize, stats as sps

from .simulation import (CV_PROFILES, FAST, FULL, STUDIES, SimConfig, _geoids, _lisa_gradient_rank, acs_frame,
                         draw_cv, lattice_weights)
from .variance import REPLICATE_COUNT, REPLICATE_FACTOR

LV_SIDE = 12
LV_MU = 50.0                                          # mean of the true values
LV_TAU = 10.0                                         # SD of the independent true values
LV_SE = CV_PROFILES["tract"]["median"] / 100 * LV_MU  # homogeneous sampling SE (6.7; reliability ~0.69)
LV_BLOCK = 4                                          # hot spot: LV_BLOCK x LV_BLOCK block ...
LV_SHIFT = 2.5 * LV_TAU                               # ... raised by 2.5 true-value SDs
LV_NEIGHBOUR_CORRS = (-0.2, -0.1, 0.0, 0.1, 0.25, 0.5)  # mean rook-neighbour error correlation r1
LV_ALPHA = 0.10                                       # local_morans default alpha
LV_STABLE = 0.8                                       # replicate_quadrant_share stability threshold
LV_WONG_Z = 1.645                                     # Wong thresholds: LV_MU +/- 1.645 LV_TAU (true 5% tails)
LV_WONG_CLS = (0.5, 0.75)                             # Wong confidence levels (the paper's simulated examples)
LV_KNN = 6
LV_CLUSTER_K = (2, 4)
LV_W_SENSITIVITY_CORRS = (0.0, 0.25)                  # r1 values also analysed under queen / kNN / misspecified W
                                                      # (as are the no-error and heterogeneous maps)
LISA_REPS_FULL = {"lisa_validation_null": 400, "lisa_validation_clusters": 400}
LISA_REPS_FAST = {"lisa_validation_null": 3, "lisa_validation_clusters": 3}
LV_WONG_NEAR = 0.5 * LV_TAU                           # a grown member is "near" its seed if |theta_j - theta_s| <= this
METRICS = ("fpr", "fdr", "false_cluster_map_k2", "false_cluster_map_k4", "power", "jaccard", "halo_share")


# ---------------------------------------------------------------------------
# Error covariance and replicates
# ---------------------------------------------------------------------------


def rook_adjacency(side: int) -> np.ndarray:
    """Binary rook adjacency of a side x side lattice (row-major cells)."""
    m = side * side
    A = np.zeros((m, m))
    for r in range(side):
        for c in range(side):
            for rr, cc in ((r + 1, c), (r, c + 1)):
                if rr < side and cc < side:
                    A[r * side + c, rr * side + cc] = A[rr * side + cc, r * side + c] = 1.0
    return A


def sar_correlation(W: np.ndarray, lam: float) -> np.ndarray:
    """Correlation matrix of the SAR(1) process (I - lam W)^-1 eps, eps ~ N(0, I)."""
    # scipy (LAPACK) rather than numpy.linalg.inv: see census.sae_spatial._State
    B = sla.inv(np.eye(W.shape[0]) - lam * W)
    S = B @ B.T
    d = np.sqrt(np.diag(S))
    return S / np.outer(d, d)


def mean_neighbour_correlation(R: np.ndarray, A: np.ndarray, order: int = 1) -> float:
    """Mean correlation over pairs at contiguity order 1 (neighbours) or 2 (neighbours of neighbours)."""
    if order == 1:
        mask = A > 0
    else:
        mask = ((A @ A) > 0) & (A == 0) & ~np.eye(A.shape[0], dtype=bool)
    return float(R[mask].mean())


def error_correlation(W: np.ndarray, A: np.ndarray, r1: float) -> Dict[str, Any]:
    """SAR error correlation with mean rook-neighbour correlation r1 (r1 = 0: identity).

    Returns lam, the correlation matrix R, its Cholesky factor L and the
    achieved first- and second-order mean correlations.
    """
    m = W.shape[0]
    if r1 == 0:
        R = np.eye(m)
        lam = 0.0
    else:
        lam = optimize.brentq(lambda t: mean_neighbour_correlation(sar_correlation(W, t), A) - r1,
                              -0.999 if r1 < 0 else 1e-9, -1e-9 if r1 < 0 else 0.999, xtol=1e-10)
        R = sar_correlation(W, lam)
    return {"lam": float(lam), "R": R, "L": sla.cholesky(R, lower=True), "r1": mean_neighbour_correlation(R, A, 1),
            "r2": mean_neighbour_correlation(R, A, 2)}


def correlated_replicates(se: np.ndarray, L: np.ndarray, Z: np.ndarray) -> np.ndarray:
    """(n, 80) replicate deviations d_r = S L z_r / 2, rescaled so 4/80 sum_r d_r^2 = se^2 per area.

    ``Z``: (n, 80) standard normals. The cross-area correlation of the
    deviations is that of L L' (the scenario's error correlation).
    """
    dev = (L @ Z) * se[:, None] / 2.0
    scale = np.sqrt(REPLICATE_FACTOR * (dev ** 2).sum(axis=1, keepdims=True))
    return np.where(scale > 0, dev / np.where(scale > 0, scale, 1.0), 0.0) * se[:, None]


# ---------------------------------------------------------------------------
# Wong (2021) heuristic hot spot / cold spot detection
# ---------------------------------------------------------------------------


def wong_hot_cold_spots(y: np.ndarray, se: np.ndarray, neighbours: Sequence[Sequence[int]], high: float,
                        low: float, confidence: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wong (2021) heuristic hot spots / cold spots: (labels, seed mask, seed_of).

    labels: 'hot', 'cold', 'both' (claimed by a hot and a cold seed; the
    paper does not say how spots from different seeds meet) or ''.
    seed_of: for each area, the first seed (in area order) whose spot it
    joined (a seed is its own), -1 for areas in no spot.

    Seeds: y > high (hot) or y < low (cold). Growing (paper sec. 4.2): a
    first-order neighbour of a member of a seed's spot joins it when its
    estimate is not significantly different from the SEED's estimate; the
    comparison is repeated outward until every candidate differs. The
    operational rule reproduces the paper's worked example (sec. 5.1): join
    when the two-sided p-value of z = (y_s - y_j) / sqrt(se_s^2 + se_j^2) is
    >= ``confidence`` (so lowering the confidence level grows the spots, as
    the paper describes). Independent errors are assumed by the test.
    """
    y, se = np.asarray(y, float), np.asarray(se, float)
    n = len(y)
    hot, cold = np.zeros(n, bool), np.zeros(n, bool)
    seed_of = np.full(n, -1)
    seeds_hot, seeds_cold = y > high, y < low
    for s in np.flatnonzero(seeds_hot | seeds_cold):
        member, seen, frontier = {int(s)}, {int(s)}, [int(s)]
        while frontier:
            nxt = []
            for i in frontier:
                for j in neighbours[i]:
                    j = int(j)
                    if j in seen:
                        continue
                    seen.add(j)
                    sd = math.sqrt(se[s] ** 2 + se[j] ** 2)
                    p = 2 * sps.norm.sf(abs(y[s] - y[j]) / sd) if sd > 0 else float(y[s] == y[j])
                    if p >= confidence:
                        member.add(j)
                        nxt.append(j)
            frontier = nxt
        mem = np.array(sorted(member))
        (hot if seeds_hot[s] else cold)[mem] = True
        seed_of[mem[seed_of[mem] < 0]] = s
    seeds = seeds_hot | seeds_cold
    seed_of[seeds] = np.flatnonzero(seeds)
    labels = np.where(hot & cold, "both", np.where(hot, "hot", np.where(cold, "cold", "")))
    return labels, seeds, seed_of


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def components(labels: np.ndarray, neighbours: Sequence[Sequence[int]], keep: Sequence[str]) -> List[np.ndarray]:
    """Connected components (via ``neighbours``) of areas sharing a label in ``keep``."""
    labels = np.asarray(labels)
    seen = np.zeros(len(labels), bool)
    out = []
    for s in range(len(labels)):
        if seen[s] or labels[s] not in keep:
            continue
        comp, stack = [], [s]
        seen[s] = True
        while stack:
            i = stack.pop()
            comp.append(i)
            for j in neighbours[i]:
                if not seen[j] and labels[j] == labels[s]:
                    seen[j] = True
                    stack.append(j)
        out.append(np.array(sorted(comp)))
    return out


def map_metrics(flag: np.ndarray, labels: np.ndarray, truth: np.ndarray, correct: str, cluster_labels: Sequence[str],
                neighbours: Sequence[Sequence[int]], halo: Optional[np.ndarray] = None) -> Dict[str, float]:
    """B3 metrics for one map (see the module docstring); power/jaccard are NaN without true clusters."""
    flag, truth = np.asarray(flag, bool), np.asarray(truth, bool)
    null = ~truth
    false = flag & null
    out = {"fpr": float(false.sum() / null.sum()), "fdr": float(false.sum() / max(int(flag.sum()), 1))}
    comps = components(np.where(flag, labels, ""), neighbours, cluster_labels)
    for k in LV_CLUSTER_K:
        out[f"false_cluster_map_k{k}"] = float(any(len(c) >= k and not truth[c].any() for c in comps))
    if truth.any():
        hit = flag & (np.asarray(labels) == correct)
        out["power"] = float((hit & truth).sum() / truth.sum())
        out["jaccard"] = float((hit & truth).sum() / (hit | truth).sum())
        out["halo_share"] = (float((false & halo).sum() / false.sum()) if false.any() and halo is not None
                             else float("nan"))
    else:
        out["power"] = out["jaccard"] = out["halo_share"] = float("nan")
    return out


def wong_metrics(labels: np.ndarray, seeds: np.ndarray, seed_of: np.ndarray, theta: np.ndarray, high: float,
                 low: float, truth: np.ndarray) -> Dict[str, float]:
    """Wong (2021) against its own target, latent elevation beyond the thresholds (see the module docstring).

    LISA's false-positive metrics are not computed: under "no clustering"
    Wong's spots are areas beyond the thresholds, which is its estimand.
    """
    nan = float("nan")
    labels = np.asarray(labels)
    true_hot, true_cold = theta > high, theta < low
    hit = ((labels == "hot") & true_hot) | ((labels == "cold") & true_cold)
    beyond = true_hot | true_cold
    seed_ok = np.where(labels == "hot", true_hot, true_cold)
    grown = (labels != "") & ~seeds
    near = np.abs(theta - theta[np.maximum(seed_of, 0)]) <= LV_WONG_NEAR
    out = {"exceed_recall": float(hit[beyond].mean()) if beyond.any() else nan,
           "seed_precision": float(seed_ok[seeds].mean()) if seeds.any() else nan,
           "member_near_share": float(near[grown].mean()) if grown.any() else nan,
           "member_beyond_share": float(seed_ok[grown].mean()) if grown.any() else nan,
           "growth_per_seed": float(grown.sum() / seeds.sum()) if seeds.any() else nan,
           "flagged_share": float((labels != "").mean())}
    if truth.any():
        hot_flag = labels == "hot"
        out["power"] = float((hot_flag & truth).sum() / truth.sum())
        out["jaccard"] = float((hot_flag & truth).sum() / (hot_flag | truth).sum())
    return out


def _moran(v: np.ndarray, W: np.ndarray) -> float:
    z = v - v.mean()
    return float(len(v) / W.sum() * (z @ W @ z) / (z @ z))


# ---------------------------------------------------------------------------
# Weights for the sensitivity analysis
# ---------------------------------------------------------------------------


def _lattice_coords(side: int) -> np.ndarray:
    r, c = np.divmod(np.arange(side * side), side)
    return np.column_stack([c, r]).astype(float)


def analysis_weights(side: int, ids: Sequence[str], seed: int) -> Dict[str, Any]:
    """libpysal W for 'rook', 'queen', f'knn{LV_KNN}' and 'misspecified' (rook of a fixed random permutation)."""
    import libpysal

    ids = list(ids)
    out = {"rook": lattice_weights(side, ids)}
    nb: Dict[str, List[str]] = {}
    for r in range(side):
        for c in range(side):
            nb[ids[r * side + c]] = [ids[rr * side + cc] for rr in (r - 1, r, r + 1) for cc in (c - 1, c, c + 1)
                                     if (rr, cc) != (r, c) and 0 <= rr < side and 0 <= cc < side]
    out["queen"] = libpysal.weights.W(nb, id_order=ids, silence_warnings=True)
    out[f"knn{LV_KNN}"] = libpysal.weights.KNN(_lattice_coords(side), k=LV_KNN, ids=ids)
    # area i is placed at lattice cell pos[i]; its neighbours are the areas at the rook cells around pos[i]
    pos = np.random.default_rng(seed).permutation(len(ids))
    at = np.argsort(pos)  # at[cell] = area placed there
    idx = {gid: k for k, gid in enumerate(ids)}
    rook = out["rook"]
    nb_m = {ids[i]: [ids[at[idx[q]]] for q in rook.neighbors[ids[pos[i]]]] for i in range(len(ids))}
    out["misspecified"] = libpysal.weights.W(nb_m, id_order=ids, silence_warnings=True)
    for w in out.values():
        w.transform = "r"
    return out


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------


class _Acc:
    """Per-map metric values keyed by (cell, method, weights)."""

    def __init__(self):
        self.vals: Dict[Tuple[str, str, str], Dict[str, List[float]]] = {}

    def add(self, cell: str, method: str, wname: str, metrics: Dict[str, float]) -> None:
        d = self.vals.setdefault((cell, method, wname), {})
        for k, v in metrics.items():
            d.setdefault(k, []).append(float(v))


def _summary_rows(acc: _Acc, cells: Dict[str, Dict[str, Any]], side: int) -> List[Dict[str, Any]]:
    rows = []
    for (cell, method, wname), d in acc.vals.items():
        info = cells[cell]
        for metric, vals in d.items():
            v = np.asarray(vals, float)
            v = v[np.isfinite(v)]
            if not v.size:
                continue
            rows.append({
                "method": f"{method}, {wname} W, {side}x{side} lattice, {info['label']}",
                "target": f"{metric} (coverage column = mean of the per-map {metric})",
                "reps": int(v.size), "nominal": float("nan"), "coverage": float(v.mean()),
                "mc_se": float(v.std(ddof=1) / math.sqrt(v.size)) if v.size > 1 else float("nan"),
                "status": "info", "scenario": info["scenario"], "errors": info["errors"],
                "neighbour_corr": info.get("r1", float("nan")), "sar_lambda": info.get("lam", float("nan")),
                "second_order_corr": info.get("r2", float("nan")), "lv_method": method, "weights": wname,
                "metric": metric, "value": float(v.mean()),
            })
    return rows


def _lisa_flags(out: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    return out["significant"].to_numpy(bool), out["quadrant"].to_numpy(str)


def _run(cfg: SimConfig, reps: int, clusters: bool, offset: int, side: int = LV_SIDE) -> List[Dict[str, Any]]:
    from .sae_spatial import _fit
    from .spatial import local_morans
    from .simulation import replicate_long

    m = side * side
    g = _geoids(m)
    ws = analysis_weights(side, g, cfg.seed + 1600)
    rook = ws["rook"]
    W = rook.full()[0]
    A = rook_adjacency(side)
    nbrs = [np.flatnonzero(A[i]) for i in range(m)]
    r_, c_ = np.divmod(np.arange(m), side)
    lo = (side - LV_BLOCK) // 2
    block = (r_ >= lo) & (r_ < lo + LV_BLOCK) & (c_ >= lo) & (c_ < lo + LV_BLOCK)
    truth = block if clusters else np.zeros(m, bool)
    halo = (A[:, block].sum(axis=1) > 0) & ~block
    structures = {r1: error_correlation(W, A, r1) for r1 in LV_NEIGHBOUR_CORRS}
    grad = _lisa_gradient_rank(side)
    hi, lo_t = LV_MU + LV_WONG_Z * LV_TAU, LV_MU - LV_WONG_Z * LV_TAU
    X1 = np.ones((m, 1))
    scen_ind, scen_cor = (("cluster_independent", "cluster_correlated") if clusters
                          else ("independent_homogeneous", "correlated"))
    cells: Dict[str, Dict[str, Any]] = {
        "truth": {"scenario": "no_sampling_error", "errors": "none",
                  "label": ("hot spot" if clusters else "no clustering") + ", no sampling error (true values)"}}
    for r1, st in structures.items():
        key = f"r1={r1:g}"
        cells[key] = {"scenario": scen_ind if r1 == 0 else scen_cor,
                      "errors": "independent" if r1 == 0 else "correlated",
                      "r1": st["r1"], "r2": st["r2"], "lam": st["lam"],
                      "label": (("hot spot, " if clusters else "no clustering, ")
                                + (f"independent homogeneous errors (SE {LV_SE:g})" if r1 == 0 else
                                   f"correlated errors, SAR lambda {st['lam']:.3f}, "
                                   f"mean neighbour corr {st['r1']:.2f}"))}
    if not clusters:
        cells["heterogeneous"] = {"scenario": "heterogeneous_independent", "errors": "independent",
                                  "label": "no clustering, heterogeneous independent errors (tract CVs sorted along "
                                           "the diagonal)"}
    acc = _Acc()
    rng = np.random.default_rng(cfg.seed + offset)
    for _ in range(reps):
        theta = LV_MU + LV_TAU * rng.standard_normal(m) + (LV_SHIFT * block if clusters else 0.0)
        z = rng.standard_normal(m)
        Zr = rng.standard_normal((m, REPLICATE_COUNT))
        se_het = np.sort(draw_cv(m, "tract", rng))[grad] / 100 * LV_MU
        base_seed = int(rng.integers(2 ** 31 - 1000))
        maps: List[Tuple[str, np.ndarray, Optional[np.ndarray], Optional[np.ndarray]]] = [("truth", theta, None, None)]
        se_h = np.full(m, LV_SE)
        for r1, st in structures.items():
            maps.append((f"r1={r1:g}", theta + se_h * (st["L"] @ z), se_h, st["L"]))
        if not clusters:
            maps.append(("heterogeneous", theta + se_het * z, se_het, np.eye(m)))
        contrast_true = theta[block].mean() - theta[~block].mean() if clusters else float("nan")
        for k, (cell, y, se, L) in enumerate(maps):
            seed = base_seed + k
            frame = acs_frame(g, {"y": y}, {"y": se if se is not None else np.zeros(m)})
            reps_frame = None
            if se is not None:
                dev = correlated_replicates(se, L, Zr)
                reps_frame = replicate_long(g, ["y"], y[:, None], (y[:, None] + dev)[:, None, :])
            out = local_morans(frame, "y", rook, seed=seed, replicates=reps_frame)
            sig, quad = _lisa_flags(out)
            mm = map_metrics(sig, quad, truth, "HH", ("HH", "LL"), nbrs, halo)
            mm["moran_I"] = _moran(y, W)
            acc.add(cell, "lisa", "rook", mm)
            r1 = float(cell.split("=")[1]) if cell.startswith("r1=") else None
            if cell in ("truth", "heterogeneous") or r1 in LV_W_SENSITIVITY_CORRS:
                for wname, w in ws.items():
                    if wname == "rook":
                        continue
                    o = local_morans(frame, "y", w, seed=seed)
                    s2, q2 = _lisa_flags(o)
                    acc.add(cell, "lisa", wname, map_metrics(s2, q2, truth, "HH", ("HH", "LL"), nbrs, halo))
            if se is None:
                continue
            # replicate_quadrant_share as a filter and alone (diagnostic readings)
            share = out["replicate_quadrant_share"].to_numpy(float)
            stable = share >= LV_STABLE
            st_m = map_metrics(sig & stable, quad, truth, "HH", ("HH", "LL"), nbrs, halo)
            st_m["stable_share_of_significant"] = float(stable[sig].mean()) if sig.any() else float("nan")
            st_m["share_mean_sig_null"] = float(share[sig & ~truth].mean()) if (sig & ~truth).any() else float("nan")
            st_m["share_mean_sig_true"] = float(share[sig & truth].mean()) if (sig & truth).any() else float("nan")
            acc.add(cell, "lisa_stable", "rook", st_m)
            hhll = np.isin(quad, ("HH", "LL"))
            acc.add(cell, "stable_only", "rook",
                    map_metrics(stable & hhll, quad, truth, "HH", ("HH", "LL"), nbrs, halo))
            # spatial Fay-Herriot EBLUPs, then LISA
            fit = _fit(y, X1, se ** 2, W, "REML")
            eb = fit["eblup"]
            oe = local_morans(acs_frame(g, {"y": eb}, {"y": se}), "y", rook, seed=seed)
            se_sig, se_quad = _lisa_flags(oe)
            em = map_metrics(se_sig, se_quad, truth, "HH", ("HH", "LL"), nbrs, halo)
            em["rho_hat"] = fit["rho"]
            em["moran_I"] = _moran(eb, W)
            em["moran_I_truth"] = _moran(theta, W)
            for kk in LV_CLUSTER_K:
                em[f"induced_false_cluster_map_k{kk}"] = (em[f"false_cluster_map_k{kk}"]
                                                          - mm[f"false_cluster_map_k{kk}"])
            if clusters:
                em["contrast_ratio"] = float((eb[block].mean() - eb[~block].mean()) / contrast_true)
                mm_c = float((y[block].mean() - y[~block].mean()) / contrast_true)
                acc.add(cell, "lisa", "rook", {"contrast_ratio": mm_c})
            acc.add(cell, "lisa_eblup", "rook", em)
            # Wong (2021), scored against its own target (latent elevation beyond the thresholds)
            for cl in LV_WONG_CLS:
                lab, seeds, seed_of = wong_hot_cold_spots(y, se, nbrs, hi, lo_t, cl)
                acc.add(cell, f"wong_cl{cl:g}", "rook", wong_metrics(lab, seeds, seed_of, theta, hi, lo_t, truth))
    return _summary_rows(acc, cells, side)


def study_lisa_validation_null(cfg: SimConfig, reps: int, side: int = LV_SIDE) -> List[Dict[str, Any]]:
    """Scenarios 1-3 (no true clustering): independent homogeneous, heterogeneous independent, correlated errors.

    ``side`` < LV_SIDE only for quick tests (the error-correlation grid is solved for that lattice).
    """
    return _run(cfg, reps, clusters=False, offset=1600, side=side)


def study_lisa_validation_clusters(cfg: SimConfig, reps: int, side: int = LV_SIDE) -> List[Dict[str, Any]]:
    """Scenarios 4-5 (a 4x4 hot spot): independent and correlated errors (``side`` as in the null study)."""
    return _run(cfg, reps, clusters=True, offset=1601, side=side)


LISA_STUDIES: Dict[str, Callable[[SimConfig, int], List[Dict[str, Any]]]] = {
    "lisa_validation_null": study_lisa_validation_null,
    "lisa_validation_clusters": study_lisa_validation_clusters,
}
STUDIES.update(LISA_STUDIES)


def lisa_config(fast: bool = True, **overrides: Any) -> SimConfig:
    """Config that runs only the Round 16 LISA validation studies."""
    base = FAST if fast else FULL
    reps = {k: 0 for k in base.reps}
    reps.update(LISA_REPS_FAST if fast else LISA_REPS_FULL)
    return replace(base, reps=reps, **overrides)
