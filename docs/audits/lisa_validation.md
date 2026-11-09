# LISA under ACS sampling error (PLAN_ROUND_16 workstream B)

Simulation study of local Moran's I (LISA) and three alternatives when the mapped values are survey estimates. It covers sampling errors that are independent, heterogeneous or spatially correlated.

- **Run:** 2026-11-08, Python 3.14.4, numpy 2.5.4, pandas 3.1.1, esda 2.10.0.
- **Command:**

  ```
  python scripts/inference_validation.py --studies lisa_validation_null lisa_validation_clusters --jobs 2 --out <path>
  ```

- **Configuration:** full, 400 maps per study, seed 20261102 (+1600 for the null study, +1601 for the cluster study).
- **Runtime:** single process on a shared, heavily loaded 16-core machine. `lisa_validation_null` took 25.7 min and `lisa_validation_clusters` 22.6 min.
- **Raw rows:** `--out` also writes a CSV next to the report, one row per scenario, error structure, method, weights and metric. `coverage` is the metric's mean and `mc_se` its Monte Carlo SE. CSVs under `docs/audits` are not committed (`.gitignore`); the tables below are taken from that file.
- **Code:** `census/lisa_validation.py`. All candidate methods are prototypes in validation code. **No tool default changed:** `local_morans` still uses two-sided p-values, 9999 permutations and BH at alpha 0.10.

Every row has status `info`. The studies run only when named with `--studies`, because each takes more than the ~5 min budget for default-report studies.

## 1. Questions kept apart

1. **Local similarity beyond chance:** LISA's significance, the question LISA answers.
2. **Stability across replicate maps:** `replicate_quadrant_share`. It is a diagnostic, not a calibrated probability.
3. **Latent elevation:** whether an area's true value is beyond a threshold. This is Wong's (2021) question, and Sun et al.'s (2015).

Because the truth is known in a simulation, **evidence of a genuine pattern** is scored against it. Significance (1) and stability (2) are reported separately from that evidence. Two error situations are also kept distinct:

- **heterogeneous variances:** errors independent, SEs varying across space;
- **correlated errors:** errors covary between neighbours.

The Round 15 "38%" result (`local_morans_size`) is about the first situation only.

## 2. Design

### Map

- **Lattice:** 12 x 12, 144 areas, rook contiguity.
- **True values:** theta_i ~ N(50, 10²), independent.
- **Cluster scenarios:** the central 4 x 4 block is raised by 25, i.e. 2.5 true-value SDs. This is stronger than Round 15's 1.5 SD, where power was 1%.
- **Estimates:** y = theta + e.

### Scenarios (B1)

| # | Scenario (`scenario` column) | Truth | Sampling errors |
| :--- | :--- | :--- | :--- |
| 1 | `independent_homogeneous` | no clustering | independent, SE 6.7 for every area (median tract CV 13.4% x 50; reliability about 0.69) |
| 2 | `heterogeneous_independent` | no clustering | independent; SE = tract-profile CVs x 50, sorted along the lattice diagonal (the Round 15 "patterned" case) |
| 3 | `correlated` | no clustering | SE 6.7 for every area; correlation matrix R with mean rook-neighbour correlation r1 |
| 4 | `cluster_independent` | hot spot | as 1 |
| 5 | `cluster_correlated` | hot spot | as 3 |

Each scenario is also analysed with no sampling error (`no_sampling_error`, the true values). This is the reference for the decomposition.

### Error correlation (stated assumption, not an ACS measurement)

- R is the SAR(1) covariance (I − λW)⁻¹(I − λW')⁻¹, with W the row-standardized rook matrix, rescaled to unit diagonal.
- λ is solved so that the mean rook-neighbour correlation equals r1.
- The SE stays homogeneous in scenarios 3 and 5, so they isolate covariance from heterogeneity.
- The grid spans -0.2 to 0.5, so workstream C's replicate-based neighbour correlations can be located in it.
- Negative r1 represents a possible effect of weighting controls. It gives a checkerboard structure in which second-order neighbours are positively correlated.

| r1 (target = achieved) | λ | mean 2nd-order correlation |
| ---: | ---: | ---: |
| -0.20 | -0.350 | 0.043 |
| -0.10 | -0.181 | 0.011 |
| 0 | 0 | 0 |
| 0.10 | 0.181 | 0.011 |
| 0.25 | 0.427 | 0.067 |
| 0.50 | 0.724 | 0.256 |

### Paired draws and replicates

All r1 values and scenario 2 in one repetition share the true values and the standard-normal draw z (e = S L z, L the Cholesky factor of R). Differences between error structures are therefore paired (common random numbers).

Replicates follow the ACS VRE scaling, with 80 maps per analysed map:

- deviations d_r = S L z_r / 2, with z_r fresh for each replicate;
- each area's deviations are rescaled so that 4/80 Σ_r d_ir² = s_i² exactly, as in `census.simulation.make_replicates`;
- the replicate maps are y + d_r.

Their cross-area correlation is the scenario's error correlation.

## 3. Methods (B2), all on the same maps

1. **`lisa`:** `census.spatial.local_morans` with its defaults. An area is flagged when `significant`; the cluster label is HH or LL.
2. **`lisa_stable`:** the same call with the replicates. An area is flagged when significant **and** `replicate_quadrant_share` ≥ 0.8 (a stated stability threshold).
   - It is reported as a diagnostic filter, not as a test.
   - `stable_only` flags HH or LL areas with share ≥ 0.8 and ignores significance. It shows what stability measures on its own.
3. **`lisa_eblup`:** `local_morans` (defaults) on spatial Fay-Herriot EBLUPs.
   - Fit: `census.sae_spatial`, REML, intercept-only model, rook W, D = s_i².
   - The model assumes independent sampling errors.
   - Smoothing is measured directly: estimated ρ, Moran's I of the EBLUPs against the truth, hot-spot contrast relative to the truth, and the paired change in the false-cluster map indicator against `lisa` on the direct estimates.
4. **`wong_cl0.5`, `wong_cl0.75`:** the Wong (2021) heuristic hot spot / cold spot procedure (section 6), at the confidence levels of the paper's simulated examples.
   - Thresholds: 50 ± 1.645 x 10. These are an analyst's domain thresholds, the true 5% tails.
   - It is scored against its own target, not against LISA's false-positive definitions.

## 4. Metrics (B3)

Each metric is computed per map, then averaged over the 400 maps. The MC SE is the between-map SD / √400, because the areas of one map share a fit and a permutation run. Null areas are every area in scenarios 1-3 and the areas outside the block in 4-5.

| Metric | Definition |
| :--- | :--- |
| `fpr` | flagged null areas / null areas (per-area false-positive rate) |
| `fdr` | false discovery proportion, flagged null / flagged (0 when nothing is flagged), averaged: the FDR that BH targets. **Under the complete null (scenarios 1-3) it equals the family-wise rate, the share of maps with any flag**: the quantity behind Round 15's "38%" |
| `false_cluster_map_k2`, `_k4` | **false-cluster map rate:** share of maps with at least one rook-connected component of areas flagged with the same cluster label (HH/LL; hot/cold) of size ≥ k made only of null areas. k = 2 and 4 are stated. Not Sun et al.'s (2015) false cluster rate, which weights predefined clusters |
| `power` | block areas flagged with the correct label (HH; hot) / 16 |
| `jaccard` | \|F ∩ T\| / \|F ∪ T\|, with F the areas flagged with the correct label and T the block (cluster-location accuracy) |
| `halo_share` | share of false discoveries that are rook neighbours of the block |
| W sensitivity | `lisa` also run with queen contiguity, k-nearest neighbours (k = 6) and a **misspecified W** (rook contiguity of a fixed random permutation of the areas, unrelated to the map). Run on the no-error, r1 = 0, r1 = 0.25 and heterogeneous maps |

Wong-specific metrics:

| Metric | Definition |
| :--- | :--- |
| `exceed_recall` | areas whose true value is beyond a threshold that are flagged with that label |
| `seed_precision` | seeds whose true value is beyond their threshold |
| `member_near_share` | grown (non-seed) members whose true value is within 5 (0.5 SD) of their seed's true value |
| `member_beyond_share` | grown members whose true value is itself beyond the threshold |
| `growth_per_seed` | grown members per seed |

## 5. Results

Values are the mean over 400 maps, with the MC SE in parentheses. All LISA rows use rook W unless stated otherwise.

### 5.1 No true clustering (scenarios 1-3)

| Map | `lisa` maps with any flag (= FDR) | `lisa` fpr | `lisa` false-cluster map k2 | `lisa_eblup` maps with any flag | `lisa_eblup` false-cluster map k2 / k4 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| no sampling error | 0.085 (0.014) | 0.001 | 0.000 | | |
| independent homogeneous (r1 = 0) | 0.060 (0.012) | 0.001 | 0.000 | 0.070 (0.013) | 0.003 (0.003) / 0.000 |
| heterogeneous, independent | **0.378 (0.024)** | 0.004 | 0.007 (0.004) | 0.133 (0.017) | 0.000 / 0.000 |
| correlated r1 = -0.20 | 0.085 (0.014) | 0.001 | 0.000 | 0.110 (0.016) | 0.000 / 0.000 |
| correlated r1 = -0.10 | 0.062 (0.012) | 0.001 | 0.000 | 0.075 (0.013) | 0.000 / 0.000 |
| correlated r1 = +0.10 | 0.072 (0.013) | 0.001 | 0.000 | 0.072 (0.013) | 0.003 (0.003) / 0.000 |
| correlated r1 = +0.25 | 0.087 (0.014) | 0.001 | 0.003 (0.003) | 0.107 (0.016) | 0.013 (0.006) / 0.007 (0.004) |
| correlated r1 = +0.50 | **0.188 (0.020)** | 0.002 | 0.013 (0.006) | **0.340 (0.024)** | **0.098 (0.015) / 0.035 (0.009)** |

Reading:

- **Round 15's 38% is reproduced** (0.378) and characterized:
  - it is the share of maps with **at least one significant area**, not with a contiguous cluster;
  - contiguous false clusters of size ≥ 2 occur in 0.7% of maps under rook W;
  - only 0.4% of areas are flagged.
- **Correlated errors** inflate the rate only for clearly positive neighbour correlation: 0.188 at r1 = 0.5, against 0.060 at r1 = 0.
  - At -0.2 to +0.25 the rate stays at 0.06-0.09, within MC error of the no-error reference (0.085). Size at that reference is within nominal: BH at 0.10, conservative under dependence.
- **Heterogeneous variances** inflate the rate more than any correlation in the grid. These are two different failure mechanisms (section 5.4).

### 5.2 Hot spot (scenarios 4-5)

| Map | `lisa` power | `lisa` Jaccard | `lisa` FDR | `lisa` false-cluster map k2 | `lisa_eblup` power | `lisa_eblup` Jaccard | `lisa_eblup` FDR | `lisa_eblup` false-cluster map k2 / k4 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no sampling error | 0.298 (0.007) | 0.298 (0.007) | 0.051 (0.005) | 0.003 (0.003) | | | | |
| r1 = 0 | 0.185 (0.007) | 0.185 (0.007) | 0.053 (0.007) | 0.000 | 0.302 (0.008) | 0.302 (0.008) | 0.067 (0.006) | 0.030 (0.009) / 0.005 (0.004) |
| r1 = -0.20 | 0.184 (0.007) | 0.184 (0.007) | 0.066 (0.007) | 0.000 | 0.283 (0.008) | 0.283 (0.008) | 0.065 (0.006) | 0.005 (0.004) / 0.003 (0.003) |
| r1 = -0.10 | 0.181 (0.007) | 0.181 (0.007) | 0.055 (0.007) | 0.000 | 0.287 (0.008) | 0.287 (0.008) | 0.056 (0.006) | 0.013 (0.006) / 0.003 (0.003) |
| r1 = +0.10 | 0.184 (0.007) | 0.184 (0.007) | 0.049 (0.006) | 0.003 (0.003) | 0.313 (0.008) | 0.312 (0.008) | 0.074 (0.007) | 0.037 (0.010) / 0.010 (0.005) |
| r1 = +0.25 | 0.189 (0.007) | 0.189 (0.007) | 0.070 (0.008) | 0.018 (0.007) | 0.329 (0.008) | 0.328 (0.008) | 0.099 (0.007) | 0.090 (0.014) / 0.025 (0.008) |
| r1 = +0.50 | 0.213 (0.007) | 0.213 (0.007) | 0.117 (0.009) | 0.037 (0.010) | 0.376 (0.009) | 0.374 (0.009) | 0.184 (0.009) | **0.260 (0.022) / 0.133 (0.017)** |

Reading:

- With rook W, LISA finds under a third of a 2.5-SD hot spot even without sampling error (0.298). LISA's local statistic is diluted at the block's edge and corners.
- Independent sampling error at reliability 0.69 cuts power to 0.185.
- Positive error correlation raises the apparent power slightly (0.213 at 0.5) and the FDR more (0.117).

### 5.3 Smoothing by the spatial Fay-Herriot model (B2.3)

| Map | ρ̂ (null) | Moran's I: truth / direct / EBLUP (null) | Δ false-cluster map k2 (null) | ρ̂ (hot spot) | hot-spot contrast / true contrast: direct / EBLUP | Δ false-cluster map k2 / k4 (hot spot) |
| :--- | ---: | :--- | ---: | ---: | :--- | :--- |
| r1 = 0 | -0.006 (0.008) | -0.008 / -0.009 / -0.012 | +0.003 (0.003) | 0.453 (0.006) | 0.994 / 0.858 | +0.030 (0.009) / +0.005 (0.004) |
| heterogeneous | -0.010 (0.008) | -0.008 / -0.010 / -0.016 | -0.007 (0.004) | | | |
| r1 = -0.20 | -0.161 (0.008) | -0.008 / -0.070 / -0.126 | 0.000 | 0.370 (0.006) | 0.996 / 0.848 | +0.005 (0.004) / +0.003 (0.003) |
| r1 = +0.10 | 0.072 (0.008) | -0.008 / 0.021 / 0.045 | +0.003 (0.003) | 0.492 (0.005) | 0.994 / 0.862 | +0.035 (0.009) / +0.007 (0.004) |
| r1 = +0.25 | 0.186 (0.008) | -0.008 / 0.066 / 0.130 | +0.010 (0.005) | 0.546 (0.005) | 0.993 / 0.866 | +0.072 (0.013) / +0.022 (0.007) |
| r1 = +0.50 | 0.354 (0.008) | -0.008 / 0.134 / **0.270** | **+0.085 (0.014)** | 0.623 (0.005) | 0.991 / 0.870 | **+0.223 (0.021) / +0.128 (0.017)** |

Δ is the paired difference between `lisa_eblup` and `lisa` on the same map. The hot-spot Moran's I of the truth is 0.270; the direct estimates give 0.208 at r1 = 0, and the EBLUPs 0.343-0.493.

### 5.4 Sensitivity to W, and the decomposition

| Map | W | maps with any flag (null) | false-cluster map k2 (null) | power (hot spot) | FDR (hot spot) | halo share (hot spot) | false-cluster map k2 (hot spot) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| no sampling error | rook | 0.085 (0.014) | 0.000 | 0.298 (0.007) | 0.051 (0.005) | 0.032 (0.017) | 0.003 (0.003) |
| no sampling error | queen | 0.077 (0.013) | 0.022 (0.007) | 0.547 (0.007) | 0.116 (0.006) | 0.390 (0.026) | 0.095 (0.015) |
| no sampling error | kNN 6 | 0.077 (0.013) | 0.018 (0.007) | 0.467 (0.006) | 0.112 (0.006) | 0.474 (0.027) | 0.065 (0.012) |
| no sampling error | misspecified | 0.080 (0.014) | 0.000 | 0.000 | 0.049 (0.011) | | 0.000 |
| r1 = 0 | rook | 0.060 (0.012) | 0.000 | 0.185 (0.007) | 0.053 (0.007) | 0.056 (0.024) | 0.000 |
| r1 = 0 | queen | 0.095 (0.015) | 0.015 (0.006) | 0.441 (0.007) | 0.116 (0.006) | 0.386 (0.027) | 0.090 (0.014) |
| r1 = 0 | kNN 6 | 0.068 (0.013) | 0.010 (0.005) | 0.362 (0.007) | 0.117 (0.007) | 0.429 (0.029) | 0.058 (0.012) |
| r1 = 0 | misspecified | 0.080 (0.014) | 0.003 (0.003) | 0.000 | 0.062 (0.012) | | 0.000 |
| heterogeneous | rook | 0.378 (0.024) | 0.007 (0.004) | | | | |
| heterogeneous | queen | 0.372 (0.024) | 0.125 (0.017) | | | | |
| heterogeneous | kNN 6 | 0.375 (0.024) | 0.098 (0.015) | | | | |
| heterogeneous | misspecified | **0.075 (0.013)** | 0.000 | | | | |
| r1 = +0.25 | rook | 0.087 (0.014) | 0.003 (0.003) | 0.189 (0.007) | 0.070 (0.008) | 0.053 (0.022) | 0.018 (0.007) |
| r1 = +0.25 | queen | 0.205 (0.020) | 0.062 (0.012) | 0.456 (0.008) | 0.185 (0.008) | 0.316 (0.021) | 0.215 (0.021) |
| r1 = +0.25 | kNN 6 | 0.172 (0.019) | 0.060 (0.012) | 0.379 (0.007) | 0.166 (0.008) | 0.363 (0.024) | 0.158 (0.018) |
| r1 = +0.25 | misspecified | 0.072 (0.013) | 0.003 (0.003) | 0.000 | 0.052 (0.011) | | 0.000 |

The halo share for the misspecified W is about 0.09 with large SEs (few discoveries) and is omitted.

Decomposition, comparing the same truth across rows:

- **Sampling uncertainty (independent, homogeneous):** with rook W the null rate does not rise (0.085 → 0.060). Power falls from 0.298 to 0.185 (-0.113), and Jaccard likewise. This error is a loss of power, not false positives.
- **Heterogeneous variances:** the null rate rises 0.060 → 0.378 under rook, queen and kNN alike. It falls back to 0.075 under the misspecified W. The excess comes from the SE gradient being **aligned with the neighbourhood structure**: neighbours share similar SEs, so high-variance regions contain extreme values next to each other. It is not an artefact of a particular contiguity rule.
- **Correlated errors:** the null rate is 0.087 at r1 = 0.25 (rook) and 0.188 at r1 = 0.5. The permutation null assumes exchangeable values; positively covarying neighbour errors make neighbours alike without any true pattern. The misspecified W removes the excess (0.072). Negative r1 down to -0.2 did not inflate false flags.
- **W choice (not misspecification of the truth):**
  - queen and kNN-6 double power over rook (0.44-0.55 against 0.19-0.30);
  - they also double the FDR (0.11-0.19) and put 32-47% of false discoveries in the ring around the true hot spot;
  - under correlated errors they amplify false clusters (false-cluster map k2 0.215 queen and 0.158 kNN against 0.018 rook at r1 = 0.25, hot spot).
  - The tool's `spatial_autocorrelation` default W is queen.
- **Misspecified W** removes both power (0.000) and the dependence-driven false positives. It keeps the nominal null rate, as permutation inference should when W is unrelated to the values.

### 5.5 Stability is not evidence (B4)

`replicate_quadrant_share` (threshold 0.8):

| Map | share of significant areas that are stable | mean share, significant null areas | mean share, significant true-cluster areas | `stable_only` fpr | `stable_only` false-cluster map k4 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| null, r1 = 0 | 0.806 (0.077) | 0.908 (0.025) | | 0.324 (0.002) | 0.998 (0.003) |
| null, heterogeneous | 0.691 (0.035) | 0.851 (0.012) | | 0.321 (0.002) | 0.998 (0.003) |
| null, r1 = +0.50 | 0.858 (0.034) | 0.926 (0.014) | | 0.394 (0.003) | 1.000 |
| hot spot, r1 = 0 | 0.949 (0.008) | 0.924 (0.013) | 0.980 (0.003) | 0.355 (0.003) | 1.000 |
| hot spot, r1 = +0.50 | 0.966 (0.005) | 0.948 (0.007) | 0.986 (0.002) | 0.417 (0.003) | 1.000 |

The null-scenario stable shares are averaged over the maps that have significant areas.

- **Filtering by stability changes almost nothing.** For example, the null maps with any flag in the heterogeneous case go from 0.378 to 0.285, and hot-spot power from 0.185 to 0.182.
- **Falsely significant areas are almost as stable as true ones** (0.91-0.95 against 0.98).
- **Stability alone is not evidence of a pattern:**
  - it marks a third or more of null areas as stable HH/LL;
  - it gives a stable null "cluster" of size ≥ 4 on essentially every map;
  - positive error correlation makes null labels **more** stable (0.926 at r1 = 0.5).
- The reason: a replicate map perturbs the published map by one more draw of the same error. A spurious pattern already in the estimates is not removed by that perturbation, and correlated perturbations preserve the quadrant structure.

The three quantities are therefore separate:

- **stability:** high almost everywhere;
- **significance:** LISA's BH flags, which control false discoveries reasonably except under heterogeneity aligned with W, or strong positive correlation;
- **genuine-pattern evidence:** only measurable here because the truth is known.

### 5.6 Wong (2021) against its own target (latent elevation)

| Map | CL | exceed recall | seed precision | member near seed | member beyond threshold | growth per seed | areas flagged |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| null, r1 = 0 | 0.5 | 0.750 (0.006) | 0.415 (0.005) | 0.446 (0.015) | 0.130 (0.010) | 0.185 (0.006) | 0.207 |
| null, r1 = 0 | 0.75 | 0.722 (0.007) | 0.415 (0.005) | 0.454 (0.029) | 0.208 (0.024) | 0.037 (0.002) | 0.182 |
| null, heterogeneous | 0.5 | 0.811 (0.006) | 0.316 (0.004) | 0.329 (0.007) | 0.050 (0.003) | **1.194 (0.049)** | 0.501 |
| null, heterogeneous | 0.75 | 0.729 (0.006) | 0.316 (0.004) | 0.330 (0.016) | 0.090 (0.010) | 0.102 (0.004) | 0.254 |
| null, r1 = -0.20 | 0.5 | 0.753 (0.006) | 0.414 (0.005) | 0.438 (0.015) | 0.154 (0.011) | 0.167 (0.006) | 0.205 |
| null, r1 = +0.25 | 0.5 | 0.742 (0.006) | 0.415 (0.005) | 0.446 (0.013) | 0.098 (0.008) | 0.217 (0.006) | 0.213 |
| null, r1 = +0.50 | 0.5 | 0.733 (0.006) | 0.420 (0.005) | 0.505 (0.012) | 0.060 (0.006) | 0.263 (0.007) | 0.219 |
| hot spot, r1 = 0 | 0.5 | 0.833 (0.004) | 0.599 (0.004) | 0.425 (0.014) | 0.180 (0.011) | 0.146 (0.005) | 0.272 |
| hot spot, r1 = 0 | 0.75 | 0.807 (0.004) | 0.599 (0.004) | 0.435 (0.028) | 0.227 (0.023) | 0.030 (0.002) | 0.245 |
| hot spot, r1 = +0.50 | 0.5 | 0.838 (0.004) | 0.604 (0.004) | 0.484 (0.011) | 0.143 (0.009) | 0.203 (0.005) | 0.285 |

Growth per seed over the whole r1 grid, at CL 0.5:

| Map | -0.2 | -0.1 | 0 | 0.1 | 0.25 | 0.5 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| null | 0.167 | 0.173 | 0.185 | 0.196 | 0.217 | 0.263 |
| hot spot | 0.135 | 0.136 | 0.146 | 0.157 | 0.176 | 0.203 |

The hot-spot power of the hot label against the block is 0.82 at CL 0.5 and 0.78 at CL 0.75 (Jaccard 0.45-0.46). That is much higher than LISA's, because the block's true values are mostly beyond the threshold.

- **Seeds:** at ACS-like reliability (0.69) most seeds are not truly beyond the threshold. Precision is 0.42 with no true clusters and 0.32 under heterogeneous SEs. Thresholding point estimates selects noise, and the paper's procedure has no step that accounts for a seed's own SE.
- **Growth:**
  - it rises with the SEs, as the paper says (sec. 5.2): 1.19 grown members per seed under the SE gradient against 0.19 with homogeneous SEs;
  - it rises with positive error correlation, monotonically from 0.167 (r1 = -0.2) to 0.263 (r1 = 0.5) at CL 0.5;
  - this confirms the expectation that the independence-based difference test under-rejects when seed and neighbour errors covary positively, and over-rejects when they covary negatively.
- **Grown members:** fewer than half are truly within 0.5 SD of their seed (0.33-0.52), and few are themselves beyond the threshold.

## 6. Wong (2021): what the paper specifies and what was implemented

**Source:** Wong, D.W.S. (2021). Issues in the Current Practices of Spatial Cluster Detection and Exploring Alternative Methods. *IJERPH* 18(18):9848, doi:10.3390/ijerph18189848, CC BY.

**Read:** the full text, through PubMed Central PMC8466737 (the BioC export and the JATS XML for Eq. 1). The MDPI page returned HTTP 403.

Verified from the text:

- **Thresholds and seeds** (sec. 4.2): the analyst sets low and high thresholds from domain knowledge (the "global criteria"). Areas whose **estimates** are beyond them are seeds.
- **Growing:**
  - a first-order neighbour joins when its estimate is "not statistically different" from the **seed's** estimate, by "a t-test given a confidence level";
  - second- and higher-order neighbours of members are compared with the seed again;
  - growth stops when every candidate differs;
  - a seed with no joining neighbour is a single-unit spot;
  - multiple seeds give multiple spots.
- **Join rule, from the worked example** (sec. 5.1 and Table 2):
  - Louisiana (p = 0.65 against Mississippi) joins at CL ≈ 0.648;
  - Rhode Island and New York (p = 0.52 and 0.50 against Connecticut) join just below CL 0.50;
  - Alabama (p = 0.13 against Mississippi) joins at CL 0.128;
  - at CL 0.95 no neighbour joins.

  The operational rule is therefore **join when p ≥ CL**: lowering CL grows the spots. This is the paper's convention and is not "fail to reject at α = 1 − CL", which would act in the opposite direction.
- **Eq. (1):** prob(x) = C(n, x) CL^x (1 − CL)^(n−x), "assumes the independence of events". The paper's value 6.9674e-18 for n = 42, x = 1, CL = 0.648 was reproduced.
  - **Not implemented as an inference.** It is the probability of x joins if each join occurred with probability CL, not a null distribution for "no clustering".
- **Not specified, and our choices:**
  - test formula, degrees of freedom and sidedness: two-sided z = (y_s − y_j)/√(s_s² + s_j²), the ACS comparison formula, assuming independent errors;
  - how spots from different seeds meet: an area claimed by a hot and a cold seed is labelled `both` (not observed with these thresholds);
  - growth uses rook neighbours.
- **Not implemented:**
  - the class separability classification of sec. 4.1, which is a choropleth-break method;
  - the paper's web application.

The paper reports no accuracy metrics for its simulations.

## 7. Conclusions

1. **The Round 15 caveat overstated the finding.** "Spurious clusters in 38% of maps" under spatially patterned SEs is, precisely:
   - 38% of maps with at least one significant area (0.4-0.5% of areas);
   - under **heterogeneous but independent** errors;
   - with contiguous false clusters in under 1% of maps under rook W.

   It said nothing about correlated errors. The caveat text is corrected (section 9).
2. **The two error situations fail differently:**
   - Heterogeneous variances aligned with the neighbourhood structure raise the null rate strongly (0.38), under any contiguity-based W.
   - Positively correlated errors raise it only at stronger correlation (0.19 at r1 = 0.5; 0.06-0.09 for r1 between -0.2 and 0.25).
   - Where workstream C's measured neighbour correlations fall in -0.2 .. 0.5 determines which regime real ACS maps are in. That reading belongs to C's results; nothing here assumes a value.
3. **Sampling error costs power more than size.** Under independent homogeneous errors the null rate stays nominal, and power drops by a third.
4. **`replicate_quadrant_share` should not be offered as a check against false positives.** Falsely significant areas are as stable as true ones, and stability rises with positive error correlation.
5. **Spatial Fay-Herriot smoothing induces and alters patterns:**
   - Without true clustering and with independent errors it is harmless (ρ̂ ≈ 0).
   - With correlated errors, ρ̂ absorbs the error correlation (ρ̂ 0.35 at r1 = 0.5 with truth ρ = 0). Moran's I doubles (0.134 → 0.270 against a true -0.008), maps with any flag rise from 0.19 to 0.34, and false clusters of ≥ 2 areas rise by 8.5 points.
   - With a true hot spot it raises power (0.185 → 0.302, matching the no-error reference) but attenuates the hot-spot contrast by about 14%. It adds false clusters around it: +3 points at r1 = 0, +22 points at r1 = 0.5.
   - "Rerun on Fay-Herriot estimates" is therefore not a neutral sensitivity check.
6. **W choice is a power/precision trade-off,** not a correctness question. Queen and kNN double power, and double FDR and halo false discoveries. Under correlated errors they amplify false clusters most.
7. **Wong (2021) answers a different question** (latent elevation). As specified it:
   - selects seeds by point estimate, so precision is low at ACS reliability (0.32-0.60);
   - grows spots on absence of evidence, so growth rises with SEs and with positive error correlation.

   It is not a replacement for LISA and is not recommended as a tool method in this form.

No method is preferred by false positives alone. LISA on EBLUPs has the best power but the worst induced false clusters under correlated errors. Rook LISA has the fewest false clusters but low power. The stability filter changes neither.

## 8. Proposals (not implemented)

- **P-B1. No change to the `local_morans` defaults.** The evidence does not support a different default p-value, permutation count or correction.
- **P-B2. Caveat and guidance:**
  - stop recommending `replicate_quadrant_share` or Fay-Herriot reruns as false-positive checks;
  - describe `replicate_quadrant_share` as sampling stability only;
  - describe EBLUP LISA as higher power with smoothing-induced clusters, larger when errors are positively correlated.

  The caveat text was corrected only where it mischaracterized the finding. A fuller rewrite of the guidance is an owner decision.
- **P-B3. Correlated-error null (later round, conditional on C):** if workstream C finds neighbour error correlations that are estimable and positive, prototype a conditional-permutation alternative. It would simulate the null distribution of each Ii from the replicate-based (structured, regularized) error covariance instead of exchangeable permutations, and be validated with this harness (scenarios 3 and 5). Not before C's evidence.
- **P-B4. Latent elevation (question 1):** if wanted, the principled route is posterior exceedance probabilities, P(theta_i > threshold), from a model with known sampling variances, thresholded as in Sun et al. (2015, Procedure 1).
  - A spatial Fay-Herriot Gaussian approximation (EBLUP, MSE) would be **our adaptation**, not a paper method. It needs its own validation, using this harness's `exceed_recall` and `seed_precision`.
  - Wong's point-estimate seeds are the failure to avoid.
- **P-B5. Weights guidance:** state in the LISA notes that queen and kNN roughly double power and FDR relative to rook on a lattice, and put most extra false discoveries next to real clusters. The `spatial_autocorrelation` default (queen) stays.

## 9. Changes made in this workstream

- `census/lisa_validation.py`: the scenarios, error-correlation calibration, replicate generation, the Wong procedure, metrics, and the studies `lisa_validation_null` and `lisa_validation_clusters`, registered in `census.simulation.STUDIES` as the on-demand `lisa` group.
- `scripts/inference_validation.py`: routes the `lisa` group only when named with `--studies`.
- `census/spatial.py` (`LOCAL_MORAN_CAVEAT` and docstrings) and `census/workflows_geo.py` (docstring) are corrected:
  - "spatially patterned sampling error produced spurious clusters in 38% of maps" became "heterogeneous but independent errors gave at least one significant area in 38% of maps (0.5% of areas); correlated errors were not tested by that study";
  - `replicate_quadrant_share` is described as a stability check, not a probability that a cluster is real;
  - the unvalidated recommendation to "check replicate_quadrant_share or rerun on Fay-Herriot estimates" was removed. Sections 5.3 and 5.5 show neither is a false-positive check.
- `docs/audits/CHECK_MAP.md`: the check-map rows and the study description.
- `tests/test_lisa_validation.py`: building blocks (error-correlation calibration, replicate scaling and correlation, Wong's join rule against the paper's worked example, metrics, misspecified W), registration, and seeded tiny runs.
