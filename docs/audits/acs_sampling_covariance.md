# ACS sampling-error covariance between areas (VRE replicates)

Generated 2026-11-09 15:28 UTC by `scripts/acs_error_covariance.py` (commit v0.15.0-1-g981e832-dirty, 9.2 min). PLAN_ROUND_16 workstream C (C1-C2). Status: **info** (gates nothing). Per-class numbers: `docs/audits/acs_sampling_covariance.csv` (written by the script; git-ignored like the other audit CSVs).

## Question

Do the sampling errors of ACS 5-year estimates covary between areas (neighbouring tracts or counties, tracts in the same county), and can 80 replicates measure that covariance well enough to build it into a measurement-error correction (workstream A1) or a LISA null model (workstream B)?

## Methods

* **Covariance.** For estimates X (area i) and Y (area j) of one release, Cov = 4/80 sum_r (X_r - X)(Y_r - Y) over the 80 successive-difference replicates of the Census Variance Replicate Estimate (VRE) tables, the cross-product form of the VRE variance formula (census.replicate.VRE_DOCUMENTATION, formula 1). The 4/80 factor cancels in the correlation r, which is the cosine of the two replicate-deviation vectors. Implementation: `census/replicate_covariance.py`.
* **Variables.** Counts: total population B01003_001 (tracts only; county populations are controlled to the population estimates and have no sampling error), occupied housing units B25003_001, persons below poverty B17001_002. Proportions: poverty rate B17001_002/B17001_001 and owner share B25003_002/B25003_001, recomputed on every replicate. Median: household income linearly interpolated from the B19001 replicate distributions (census.medians; no published median table has VRE files - checked against the 2019 and 2024 VRE table lists); areas whose median is top- or bottom-coded are excluded. Areas with a zero estimate denominator, a zero denominator in any replicate (the replicate ratio is undefined; the SE convention of VRE documentation Example 7, ratio 0, would fabricate a deviation of minus the estimate), controlled estimates or zero replicate variance are excluded (their correlation is undefined).
* **Pairs.** Contiguity from queen weights (libpysal via census.spatial.spatial_weights) on the Census cartographic boundary files of the release's vintage; orders 1-3 are graph distances in the queen contiguity graph (order 1 split into rook = shared edge vs queen-only = corner). Tracts: all pairs of order 1-3; tract pairs in the same county beyond order 3 (random sample of at most 150,000); and a random sample of 100,000 draws of tract pairs in different counties beyond order 3. Counties: all pairs in the state. Distance = centroid distance in an equal-area projection (EPSG:5070).
* **Structured summaries, not pairwise matrices.** The replicate covariance of n areas has rank <= 80, and one pairwise r from 80 replicates has null SD 1/sqrt(80). So only class summaries are reported: mean and median r per contiguity order, within vs across county, distance band. Their uncertainty: a bootstrap over the 80 replicate columns (200 draws; replicate noise; approximate because SDR deviations are not exactly iid across replicates) and a delete-one-county jackknife (drops every pair touching the county; area heterogeneity plus replicate noise). Intervals are mean +/- 1.96 x the larger of the two SEs.
* **Noise floor.** Analytic: if one deviation vector is spherically symmetric in R^80 and independent of the other, (1 + r)/2 ~ Beta(39.5, 39.5): SD 0.112, two-sided 5% critical |r| 0.219 (40 replicates: SD 0.158, critical 0.308). Empirical: (a) replicate-index derangements applied to the second area of each far pair; (b) Michigan x New York area pairs, whose samples are independent, so their correlations show the real null spread of SDR replicate correlations.
* **Reliability.** Split-half agreement: per-pair r computed on two disjoint halves of the replicates (random, odd/even, 1-40 vs 41-80); the correlation across pairs of the two halves' r is the reliability of a 40-replicate pairwise r, stepped up to 80 replicates by Spearman-Brown. Aggregation ratio (counts, tracts): Var(sum of a county's tracts) / sum of the tracts' variances = 1 + sum of within-county covariances / sum of variances; < 1 means negatively correlated errors (the signature of weighting controls), and it pools every within-county pair in one number.
* **Replicate design spectrum.** The eigenvalues of the replicate-space Gram matrix of standardized deviations (80 x 80, over thousands of tracts) are compared with the Marchenko-Pastur lower edge for independent replicates (`replicate_spectrum`). Directions far below it are near-null for every area: a property of the replicate-factor design, not of geography. They lower the effective number of replicates, hence widen the null spread of r.
* **Structured model and shrinkage, scored out of sample.** Replicates are split in half (random, seeded); on one half fit (i) one correlation per contiguity order 1-3 (0 beyond), (ii) for tracts one per order x within/across county (0 beyond order 3), (iii) as (ii) plus one for far pairs within a county, and (iv) Ledoit-Wolf shrinkage of the replicate correlation toward the identity; score the Gaussian log-likelihood of the other half's standardized replicate deviations (scaled so E[x x^T] = Sigma) relative to independence; average both directions. Each half is first projected orthogonally to the design near-null directions and the constant vector: otherwise the held-out half's component along those directions is minus the training half's for every area at once, a cross-half dependence that Ledoit-Wolf exploits (it then 'gains' 30-90 nats per replicate even on variables with no neighbour correlation; with the projection it loses, as it does on independent synthetic data). A gain > 0 means the fitted structure predicts held-out replicates better than independent errors. A fitted structure that is not positive definite is projected to the nearest correlation matrix with eigenvalues >= 1e-3 and flagged.

## Data

Releases 2020-2024, 2015-2019; states Michigan (26) and New York (36). Files (cached, not committed):

| kind | year | level | file | SHA-256 |
| :--- | :--- | :--- | :--- | :--- |
| VRE | 2024 | tract | `B01003_26.csv.zip` | `235fda51b0cc2f2cef29f0525b30cdef5fe2addd829a00125eb86102d4b0c4de` |
| VRE | 2024 | tract | `B25003_26.csv.zip` | `beb257fde07b8885af75aa5233c90fa96a092931b9952a2b3a53711a44c503bc` |
| VRE | 2024 | tract | `B17001_26.csv.zip` | `06aac06540352fbddb516fb3dd31eb652eb5ae969db3450b9fcbfda8c5cfa58a` |
| VRE | 2024 | tract | `B19001_26.csv.zip` | `a6df2bd01e65a71191d76bfb9428ea79fd0f958e1525cae11b39a1a84682e846` |
| boundary | 2024 | tract | `cb_2024_26_tract_500k.zip` | `9bd8aaf99a903bffff16efc59bad8f0ebea65dcf086ef315cea89663f4cc3b6e` |
| VRE | 2024 | tract | `B01003_36.csv.zip` | `87b39e2b153bb8ea64b0283624687e61429afa89f1cca3437cd3b7a46752a970` |
| VRE | 2024 | tract | `B25003_36.csv.zip` | `05462d99d4039ca4264e4dcfa133b5b4f2e5589d9577051536f54a017e7d3584` |
| VRE | 2024 | tract | `B17001_36.csv.zip` | `6b1e85f8c8be2c25edf462fd8a40cd50f85f30748cf4b0d9f2df809de230da83` |
| VRE | 2024 | tract | `B19001_36.csv.zip` | `6a68190ea29b73389399e397ac5aeef97faa60db5dbfa964fcfb014fcce3094a` |
| boundary | 2024 | tract | `cb_2024_36_tract_500k.zip` | `ab7f72cb4a66079fd5b3a99302af0a48bdf87d53aec3b49e5ee4d18a7280cd02` |
| VRE | 2024 | county | `B01003.csv.zip` | `65e008a179c90712205a77b7a9bb0cdc8af535ae759e5b13f7faba970928f62f` |
| VRE | 2024 | county | `B25003.csv.zip` | `9aceebd33bd3a840d95341b3c09f2536a15e56c3fa2c4d08d09058aee13b0c35` |
| VRE | 2024 | county | `B17001.csv.zip` | `3d24fa18e9f005b3c9455d3bda4a4ab843eb8f443d423b56b00496b58ad0c605` |
| VRE | 2024 | county | `B19001.csv.zip` | `95cb48eabcaef5c44fc68377f8b596f31cbb079d846bbea9c6bf7e83ba896d0f` |
| boundary | 2024 | county | `cb_2024_us_county_500k.zip` | `da4051717caec55c75e3748c3608c2a3dbde8d1ff401bbaf4f952e3c3fb63ef1` |
| VRE | 2019 | tract | `B01001_26.csv.zip` (substitute for `B01003_26.csv.zip`) | `4ef59dbb65e02ce8de6779cd40d382f3e143a17079b1ffa56dd886f60d72970a` |
| VRE | 2019 | tract | `B25003_26.csv.zip` | `0b6a3f85d252700385d92a435396f24750d659b89f114e3d6006e525231317d4` |
| VRE | 2019 | tract | `B17001_26.csv.zip` | `8f6e7fb8d21f80e605d03ce4fab11fc3a372184cb1e550d64b84cf779569d729` |
| VRE | 2019 | tract | `B19001_26.csv.zip` | `0cd0858b255228c403627e3bd08094950a56e3d15d414446fd77a719ecf99168` |
| boundary | 2019 | tract | `cb_2019_26_tract_500k.zip` | `a0a60fdb67c9d99c99a4c9a58c38e7f05b7b38e5e43802a02016a5514a3f3e2d` |
| VRE | 2019 | tract | `B01003_36.csv.zip` | `ffb652e753dc225a7be6e0936e30fdd6ce1048d31f1009b037abe5dc0315569a` |
| VRE | 2019 | tract | `B25003_36.csv.zip` | `3abde86fb9e93ab998d31c2521a17cf8d53e71233c4e763536ef4fe7d37be5a5` |
| VRE | 2019 | tract | `B17001_36.csv.zip` | `8dbe92bf59958da80c5e12cea3c76d762b6e0d94e63dd72cf1537906efc30724` |
| VRE | 2019 | tract | `B19001_36.csv.zip` | `461ebbd9fab3f9f051c443a63842d3217f0f7ca1ddf21746cda8058f6493ca41` |
| boundary | 2019 | tract | `cb_2019_36_tract_500k.zip` | `e4ed39cd98bc03365d96b6588a8ff5e2e2c71134d9afa1ff9fd093286c43b553` |
| VRE | 2019 | county | `B01003.csv.zip` | `61cb0a5357aafcbba1d498872165d0402c28f460910592e7e2d9f24bfe9aeaf7` |
| VRE | 2019 | county | `B25003.csv.zip` | `b0cbdab927b427839e5d6a9bda4afa5fc36dd8bd243f38d7101a39effe6170c6` |
| VRE | 2019 | county | `B17001.csv.zip` | `05bc088f51d2c31fbb0aff6fe328c08bab7a94c6de9b531ffb382918bb87de15` |
| VRE | 2019 | county | `B19001.csv.zip` | `56374faae955dd44c9eec5f45c7b2d145781b6271028d714eae6ebafc2f260d3` |
| boundary | 2019 | county | `cb_2019_us_county_500k.zip` | `1c4b703cd34c3f5e5e6368f91f1bd80de9281ef81ad91e005844848df9976c4b` |

**Substituted files** (census.replicate.fetch_vre, `VRE_TOTAL_FALLBACKS`):

| year | level | state | requested | reason | used instead |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 2019 | tract | Michigan | https://www2.census.gov/programs-surveys/acs/replicate_estimates/2019/data/5-year/140/B01003_26.csv.zip | census.gov firewall 'Request Rejected' page (support ID 13979921158196382640) | B01003_001 = B01001_001 of https://www2.census.gov/programs-surveys/acs/replicate_estimates/2019/data/5-year/140/B01001_26.csv.zip |

Basis: B01003 line 1 (Total population) equals B01001 line 1 (Total:) cell for cell: verified 2026-11-09 on the 2015-2019 New York tract VRE files (4,918 tracts; ESTIMATE, MOE and Var_Rep1-80 identical, 0 differing cells); re-checked by tests/test_replicate_live.py (CENSUS_LIVE_TESTS=1). The substitute file's SHA-256 is the one listed above.

Versions: census-acs-toolkit 0.15.1, python 3.14.4, numpy 2.5.4, scipy 1.18.1, pandas 3.1.1, geopandas 1.2.0, libpysal 4.15.1

Areas analysed (after exclusions):

| year | state | geography | variable | type | areas | analysed | zero variance | note |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2024 | Michigan | tract | population | count | 2971 | 2914 | 57 |  |
| 2024 | Michigan | tract | households | count | 2971 | 2897 | 74 |  |
| 2024 | Michigan | tract | poverty_count | count | 2971 | 2893 | 78 |  |
| 2024 | Michigan | tract | poverty_rate | proportion | 2971 | 2887 | 13 |  |
| 2024 | Michigan | tract | owner_share | proportion | 2971 | 2865 | 31 | 1 area(s) with a zero replicate denominator excluded |
| 2024 | Michigan | tract | median_hh_income | median | 2971 | 2870 | 6 | 20 top/bottom-coded medians excluded |
| 2024 | New York | tract | population | count | 5394 | 5289 | 91 |  |
| 2024 | New York | tract | households | count | 5394 | 5251 | 129 |  |
| 2024 | New York | tract | poverty_count | count | 5394 | 5242 | 138 |  |
| 2024 | New York | tract | poverty_rate | proportion | 5394 | 5238 | 22 | 2 area(s) with a zero replicate denominator excluded |
| 2024 | New York | tract | owner_share | proportion | 5394 | 5145 | 105 | 1 area(s) with a zero replicate denominator excluded |
| 2024 | New York | tract | median_hh_income | median | 5394 | 5053 | 11 | 186 top/bottom-coded medians excluded |
| 2024 | Michigan | county | households | count | 83 | 83 | 0 |  |
| 2024 | Michigan | county | poverty_count | count | 83 | 83 | 0 |  |
| 2024 | Michigan | county | poverty_rate | proportion | 83 | 83 | 0 |  |
| 2024 | Michigan | county | owner_share | proportion | 83 | 83 | 0 |  |
| 2024 | Michigan | county | median_hh_income | median | 83 | 83 | 0 | 0 top/bottom-coded medians excluded |
| 2024 | New York | county | households | count | 62 | 62 | 0 |  |
| 2024 | New York | county | poverty_count | count | 62 | 62 | 0 |  |
| 2024 | New York | county | poverty_rate | proportion | 62 | 62 | 0 |  |
| 2024 | New York | county | owner_share | proportion | 62 | 62 | 0 |  |
| 2024 | New York | county | median_hh_income | median | 62 | 62 | 0 | 0 top/bottom-coded medians excluded |
| 2019 | Michigan | tract | population | count | 2772 | 2754 | 18 |  |
| 2019 | Michigan | tract | households | count | 2772 | 2738 | 34 |  |
| 2019 | Michigan | tract | poverty_count | count | 2772 | 2736 | 36 |  |
| 2019 | Michigan | tract | poverty_rate | proportion | 2772 | 2734 | 8 |  |
| 2019 | Michigan | tract | owner_share | proportion | 2772 | 2722 | 16 |  |
| 2019 | Michigan | tract | median_hh_income | median | 2772 | 2730 | 2 | 6 top/bottom-coded medians excluded |
| 2019 | New York | tract | population | count | 4906 | 4854 | 52 |  |
| 2019 | New York | tract | households | count | 4906 | 4828 | 78 |  |
| 2019 | New York | tract | poverty_count | count | 4906 | 4811 | 95 |  |
| 2019 | New York | tract | poverty_rate | proportion | 4906 | 4806 | 28 | 2 area(s) with a zero replicate denominator excluded |
| 2019 | New York | tract | owner_share | proportion | 4906 | 4747 | 80 | 1 area(s) with a zero replicate denominator excluded |
| 2019 | New York | tract | median_hh_income | median | 4906 | 4745 | 16 | 66 top/bottom-coded medians excluded |
| 2019 | Michigan | county | households | count | 83 | 83 | 0 |  |
| 2019 | Michigan | county | poverty_count | count | 83 | 83 | 0 |  |
| 2019 | Michigan | county | poverty_rate | proportion | 83 | 83 | 0 |  |
| 2019 | Michigan | county | owner_share | proportion | 83 | 83 | 0 |  |
| 2019 | Michigan | county | median_hh_income | median | 83 | 83 | 0 | 0 top/bottom-coded medians excluded |
| 2019 | New York | county | households | count | 62 | 62 | 0 |  |
| 2019 | New York | county | poverty_count | count | 62 | 62 | 0 |  |
| 2019 | New York | county | poverty_rate | proportion | 62 | 62 | 0 |  |
| 2019 | New York | county | owner_share | proportion | 62 | 62 | 0 |  |
| 2019 | New York | county | median_hh_income | median | 62 | 62 | 0 | 0 top/bottom-coded medians excluded |

Skipped: 2024 Michigan county population: 83 controlled (no sampling error); 2024 New York county population: 62 controlled (no sampling error); 2019 Michigan county population: 83 controlled (no sampling error); 2019 New York county population: 62 controlled (no sampling error).

## Results 2020-2024

Mean pairwise r [95% interval] (number of pairs) by queen contiguity order.

### Tracts: contiguity order

| state | variable | order 1 | order 2 | order 3 | beyond order 3 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.071 [-0.090, -0.052] (n=9099) | -0.014 [-0.017, -0.011] (n=21283) | -0.004 [-0.005, -0.003] (n=36300) | -0.001 [-0.001, 0.000] (n=225050) |
| Michigan | households | -0.025 [-0.033, -0.017] (n=9033) | -0.008 [-0.011, -0.004] (n=21067) | -0.003 [-0.005, -0.002] (n=35923) | -0.001 [-0.001, 0.000] (n=223197) |
| Michigan | poverty_count | -0.004 [-0.007, -0.002] (n=9024) | -0.003 [-0.005, -0.002] (n=21028) | -0.000 [-0.002, 0.001] (n=35876) | 0.000 [-0.001, 0.002] (n=222204) |
| Michigan | poverty_rate | -0.000 [-0.003, 0.002] (n=8988) | -0.002 [-0.004, -0.001] (n=20944) | 0.000 [-0.002, 0.002] (n=35733) | 0.000 [-0.001, 0.001] (n=221162) |
| Michigan | owner_share | -0.002 [-0.005, 0.000] (n=8875) | 0.000 [-0.001, 0.002] (n=20622) | -0.001 [-0.002, 0.001] (n=35161) | 0.000 [-0.000, 0.001] (n=217458) |
| Michigan | median_hh_income | 0.000 [-0.002, 0.003] (n=8892) | 0.000 [-0.002, 0.003] (n=20659) | 0.001 [-0.000, 0.002] (n=35172) | 0.001 [0.000, 0.002] (n=217954) |
| New York | population | -0.040 [-0.060, -0.020] (n=15821) | -0.009 [-0.013, -0.004] (n=37762) | -0.003 [-0.004, -0.002] (n=65290) | -0.001 [-0.002, -0.001] (n=230255) |
| New York | households | -0.015 [-0.022, -0.008] (n=15668) | -0.003 [-0.006, -0.001] (n=37281) | -0.002 [-0.003, -0.001] (n=64393) | -0.001 [-0.001, -0.000] (n=227670) |
| New York | poverty_count | -0.002 [-0.003, 0.000] (n=15646) | -0.001 [-0.002, 0.001] (n=37221) | 0.000 [-0.001, 0.001] (n=64288) | -0.000 [-0.001, 0.001] (n=226711) |
| New York | poverty_rate | -0.000 [-0.002, 0.002] (n=15627) | -0.000 [-0.001, 0.001] (n=37151) | 0.000 [-0.001, 0.001] (n=64145) | 0.000 [-0.001, 0.001] (n=226417) |
| New York | owner_share | -0.001 [-0.002, 0.001] (n=15121) | 0.000 [-0.001, 0.001] (n=35819) | 0.001 [-0.000, 0.002] (n=61842) | 0.000 [-0.000, 0.001] (n=217364) |
| New York | median_hh_income | 0.000 [-0.002, 0.002] (n=14672) | 0.001 [-0.000, 0.002] (n=34551) | -0.000 [-0.001, 0.001] (n=59637) | 0.001 [0.000, 0.001] (n=210794) |

### Counties: contiguity order

| state | variable | order 1 | order 2 | order 3 | beyond order 3 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | households | 0.026 [0.001, 0.051] (n=208) | 0.031 [0.009, 0.052] (n=339) | 0.027 [0.005, 0.049] (n=404) | 0.033 [0.015, 0.051] (n=2452) |
| Michigan | poverty_count | -0.005 [-0.027, 0.017] (n=208) | 0.009 [-0.007, 0.026] (n=339) | 0.005 [-0.014, 0.024] (n=404) | 0.002 [-0.007, 0.010] (n=2452) |
| Michigan | poverty_rate | -0.005 [-0.027, 0.017] (n=208) | 0.009 [-0.007, 0.026] (n=339) | 0.005 [-0.014, 0.024] (n=404) | 0.002 [-0.007, 0.010] (n=2452) |
| Michigan | owner_share | -0.003 [-0.025, 0.018] (n=208) | 0.018 [-0.001, 0.036] (n=339) | 0.022 [0.006, 0.039] (n=404) | 0.001 [-0.006, 0.009] (n=2452) |
| Michigan | median_hh_income | 0.003 [-0.016, 0.022] (n=208) | 0.002 [-0.014, 0.018] (n=339) | 0.002 [-0.015, 0.020] (n=404) | -0.000 [-0.007, 0.007] (n=2452) |
| New York | households | 0.021 [-0.005, 0.048] (n=142) | 0.024 [-0.001, 0.049] (n=231) | 0.025 [0.001, 0.050] (n=252) | 0.031 [0.015, 0.047] (n=1266) |
| New York | poverty_count | 0.003 [-0.025, 0.031] (n=142) | 0.006 [-0.012, 0.024] (n=231) | 0.019 [-0.006, 0.045] (n=252) | 0.008 [-0.003, 0.019] (n=1266) |
| New York | poverty_rate | 0.003 [-0.025, 0.031] (n=142) | 0.006 [-0.012, 0.024] (n=231) | 0.019 [-0.006, 0.045] (n=252) | 0.008 [-0.003, 0.019] (n=1266) |
| New York | owner_share | -0.002 [-0.034, 0.030] (n=142) | 0.010 [-0.015, 0.035] (n=231) | -0.002 [-0.021, 0.017] (n=252) | 0.012 [0.002, 0.022] (n=1266) |
| New York | median_hh_income | 0.006 [-0.019, 0.031] (n=142) | -0.008 [-0.031, 0.015] (n=231) | 0.004 [-0.015, 0.024] (n=252) | 0.012 [0.002, 0.021] (n=1266) |

### Tracts: within vs across county (weighting controls)

| state | variable | order 1 / within county | order 1 / across county | order 2 / within county | order 2 / across county | beyond order 3 / within county | beyond order 3 / across county |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.081 [-0.106, -0.056] (n=8000) | 0.003 [-0.007, 0.014] (n=1099) | -0.018 [-0.024, -0.012] (n=15774) | -0.001 [-0.005, 0.002] (n=5509) | -0.001 [-0.001, -0.000] (n=137288) | 0.000 [-0.001, 0.001] (n=87762) |
| Michigan | households | -0.028 [-0.039, -0.018] (n=7936) | -0.003 [-0.012, 0.006] (n=1097) | -0.009 [-0.015, -0.004] (n=15569) | -0.002 [-0.006, 0.002] (n=5498) | -0.001 [-0.003, -0.000] (n=136491) | 0.001 [-0.001, 0.002] (n=86706) |
| Michigan | poverty_count | -0.005 [-0.007, -0.002] (n=7926) | 0.001 [-0.008, 0.011] (n=1098) | -0.005 [-0.006, -0.003] (n=15537) | 0.000 [-0.003, 0.003] (n=5491) | 0.000 [-0.001, 0.002] (n=135706) | -0.000 [-0.002, 0.001] (n=86498) |
| Michigan | poverty_rate | -0.001 [-0.003, 0.002] (n=7892) | 0.002 [-0.007, 0.012] (n=1096) | -0.003 [-0.005, -0.002] (n=15457) | 0.001 [-0.003, 0.004] (n=5487) | 0.001 [-0.001, 0.002] (n=135029) | -0.000 [-0.002, 0.001] (n=86133) |
| Michigan | owner_share | -0.002 [-0.005, -0.000] (n=7781) | 0.000 [-0.010, 0.011] (n=1094) | 0.001 [-0.001, 0.003] (n=15164) | -0.001 [-0.006, 0.003] (n=5458) | 0.000 [-0.000, 0.001] (n=132634) | 0.000 [-0.001, 0.001] (n=84824) |
| Michigan | median_hh_income | 0.000 [-0.002, 0.003] (n=7804) | -0.001 [-0.011, 0.009] (n=1088) | -0.000 [-0.002, 0.002] (n=15220) | 0.002 [-0.002, 0.006] (n=5439) | 0.001 [0.001, 0.002] (n=132753) | 0.000 [-0.001, 0.001] (n=85201) |
| New York | population | -0.043 [-0.065, -0.020] (n=14769) | 0.002 [-0.006, 0.009] (n=1052) | -0.010 [-0.016, -0.005] (n=31951) | 0.001 [-0.003, 0.005] (n=5811) | -0.001 [-0.002, -0.001] (n=140773) | -0.001 [-0.002, 0.000] (n=89482) |
| New York | households | -0.016 [-0.024, -0.008] (n=14624) | -0.000 [-0.011, 0.011] (n=1044) | -0.004 [-0.008, -0.001] (n=31575) | 0.002 [-0.002, 0.006] (n=5706) | -0.001 [-0.002, -0.001] (n=139403) | 0.000 [-0.001, 0.001] (n=88267) |
| New York | poverty_count | -0.002 [-0.004, -0.000] (n=14606) | 0.004 [-0.005, 0.013] (n=1040) | -0.000 [-0.002, 0.001] (n=31514) | -0.002 [-0.006, 0.002] (n=5707) | -0.000 [-0.002, 0.002] (n=138746) | 0.000 [-0.001, 0.001] (n=87965) |
| New York | poverty_rate | -0.000 [-0.002, 0.002] (n=14588) | 0.003 [-0.007, 0.013] (n=1039) | 0.000 [-0.001, 0.001] (n=31453) | -0.002 [-0.006, 0.003] (n=5698) | -0.000 [-0.002, 0.002] (n=138557) | 0.000 [-0.000, 0.001] (n=87860) |
| New York | owner_share | -0.001 [-0.003, 0.001] (n=14105) | 0.004 [-0.007, 0.015] (n=1016) | 0.000 [-0.001, 0.001] (n=30292) | -0.000 [-0.005, 0.004] (n=5527) | 0.000 [-0.000, 0.001] (n=132456) | 0.001 [-0.000, 0.001] (n=84908) |
| New York | median_hh_income | 0.000 [-0.002, 0.003] (n=13668) | 0.000 [-0.009, 0.010] (n=1004) | 0.001 [0.000, 0.003] (n=29082) | -0.001 [-0.006, 0.003] (n=5469) | 0.001 [0.000, 0.001] (n=129020) | 0.001 [-0.001, 0.002] (n=81774) |

### Rook (shared edge) vs queen-only (corner) first-order neighbours

Tracts:

| state | variable | order 1 rook (shared edge) | order 1 queen-only (corner) |
| :--- | :--- | :--- | :--- |
| Michigan | population | -0.080 [-0.099, -0.060] (n=7433) | -0.029 [-0.039, -0.020] (n=1666) |
| Michigan | households | -0.028 [-0.035, -0.021] (n=7373) | -0.013 [-0.024, -0.003] (n=1660) |
| Michigan | poverty_count | -0.005 [-0.008, -0.002] (n=7363) | 0.000 [-0.004, 0.005] (n=1661) |
| Michigan | poverty_rate | -0.001 [-0.003, 0.002] (n=7333) | 0.003 [-0.003, 0.008] (n=1655) |
| Michigan | owner_share | -0.003 [-0.005, 0.000] (n=7244) | 0.000 [-0.010, 0.011] (n=1631) |
| Michigan | median_hh_income | 0.001 [-0.002, 0.003] (n=7257) | -0.002 [-0.008, 0.005] (n=1635) |
| New York | population | -0.043 [-0.065, -0.022] (n=13919) | -0.014 [-0.024, -0.003] (n=1902) |
| New York | households | -0.016 [-0.023, -0.008] (n=13778) | -0.010 [-0.017, -0.002] (n=1890) |
| New York | poverty_count | -0.002 [-0.004, -0.000] (n=13753) | 0.003 [-0.003, 0.008] (n=1893) |
| New York | poverty_rate | -0.000 [-0.002, 0.002] (n=13736) | 0.002 [-0.003, 0.007] (n=1891) |
| New York | owner_share | -0.001 [-0.003, 0.001] (n=13308) | 0.001 [-0.004, 0.006] (n=1813) |
| New York | median_hh_income | -0.000 [-0.003, 0.002] (n=12911) | 0.004 [-0.003, 0.011] (n=1761) |

Counties:

| state | variable | order 1 rook (shared edge) | order 1 queen-only (corner) |
| :--- | :--- | :--- | :--- |
| Michigan | households | 0.030 [0.003, 0.057] (n=180) | 0.001 [-0.068, 0.071] (n=28) |
| Michigan | poverty_count | -0.004 [-0.026, 0.019] (n=180) | -0.016 [-0.082, 0.051] (n=28) |
| Michigan | poverty_rate | -0.004 [-0.026, 0.019] (n=180) | -0.015 [-0.082, 0.053] (n=28) |
| Michigan | owner_share | -0.007 [-0.030, 0.016] (n=180) | 0.019 [-0.052, 0.089] (n=28) |
| Michigan | median_hh_income | 0.002 [-0.018, 0.022] (n=180) | 0.008 [-0.041, 0.057] (n=28) |
| New York | households | 0.023 [-0.004, 0.049] (n=140) | -0.075 [-0.226, 0.076] (n=2) |
| New York | poverty_count | 0.002 [-0.027, 0.031] (n=140) | 0.102 [-0.033, 0.237] (n=2) |
| New York | poverty_rate | 0.002 [-0.027, 0.031] (n=140) | 0.097 [-0.038, 0.233] (n=2) |
| New York | owner_share | -0.002 [-0.034, 0.030] (n=140) | -0.014 [-0.345, 0.316] (n=2) |
| New York | median_hh_income | 0.008 [-0.018, 0.033] (n=140) | -0.077 [-0.411, 0.258] (n=2) |

### By centroid distance

Tracts:

| state | variable | 0-2 km | 2-5 km | 5-10 km | 10-20 km | 20-50 km | 50-100 km | 100-inf km |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.045 [-0.070, -0.020] (n=3779) | -0.024 [-0.052, 0.003] (n=19037) | -0.009 [-0.019, 0.002] (n=34754) | -0.003 [-0.006, 0.000] (n=66527) | -0.001 [-0.003, 0.001] (n=89209) | -0.001 [-0.004, 0.001] (n=19601) | 0.001 [-0.001, 0.002] (n=58825) |
| Michigan | households | -0.016 [-0.033, 0.000] (n=3735) | -0.009 [-0.020, 0.002] (n=18826) | -0.005 [-0.011, 0.001] (n=34360) | -0.003 [-0.005, -0.000] (n=66049) | -0.002 [-0.004, 0.000] (n=88762) | 0.000 [-0.002, 0.002] (n=19364) | 0.002 [0.000, 0.003] (n=58124) |
| Michigan | poverty_count | -0.005 [-0.008, -0.002] (n=3741) | -0.003 [-0.004, -0.001] (n=18819) | -0.000 [-0.003, 0.002] (n=34298) | -0.000 [-0.002, 0.001] (n=65783) | 0.000 [-0.001, 0.001] (n=88201) | 0.001 [-0.001, 0.003] (n=19331) | -0.000 [-0.002, 0.001] (n=57959) |
| Michigan | poverty_rate | 0.000 [-0.003, 0.004] (n=3719) | -0.001 [-0.003, 0.001] (n=18714) | 0.000 [-0.002, 0.003] (n=34131) | 0.000 [-0.002, 0.002] (n=65534) | 0.000 [-0.000, 0.001] (n=87741) | 0.001 [-0.001, 0.003] (n=19254) | -0.001 [-0.002, 0.001] (n=57734) |
| Michigan | owner_share | -0.004 [-0.010, 0.002] (n=3652) | -0.001 [-0.003, 0.001] (n=18367) | 0.001 [-0.001, 0.003] (n=33447) | 0.000 [-0.001, 0.002] (n=64393) | -0.000 [-0.001, 0.001] (n=86317) | 0.001 [-0.001, 0.003] (n=18934) | -0.000 [-0.001, 0.001] (n=57006) |
| Michigan | median_hh_income | 0.002 [-0.002, 0.006] (n=3697) | 0.001 [-0.003, 0.005] (n=18471) | 0.002 [-0.001, 0.004] (n=33510) | 0.001 [0.000, 0.002] (n=64441) | 0.001 [-0.000, 0.002] (n=86237) | 0.001 [-0.002, 0.004] (n=19017) | 0.000 [-0.002, 0.002] (n=57304) |
| New York | population | -0.006 [-0.013, 0.001] (n=45076) | -0.008 [-0.015, -0.001] (n=59683) | -0.005 [-0.010, 0.000] (n=74890) | -0.002 [-0.004, -0.000] (n=58714) | -0.002 [-0.004, -0.000] (n=40629) | 0.000 [-0.002, 0.002] (n=14459) | -0.001 [-0.002, -0.000] (n=55677) |
| New York | households | -0.002 [-0.005, 0.001] (n=44716) | -0.003 [-0.006, 0.000] (n=58849) | -0.003 [-0.005, -0.001] (n=74152) | -0.002 [-0.003, -0.001] (n=58039) | -0.002 [-0.004, 0.000] (n=40143) | 0.001 [-0.002, 0.004] (n=14253) | -0.000 [-0.001, 0.001] (n=54860) |
| New York | poverty_count | -0.000 [-0.001, 0.001] (n=44676) | -0.001 [-0.002, 0.001] (n=58855) | 0.000 [-0.001, 0.002] (n=73813) | -0.000 [-0.003, 0.002] (n=57668) | -0.001 [-0.002, 0.001] (n=39943) | 0.001 [-0.002, 0.004] (n=14232) | 0.000 [-0.002, 0.002] (n=54679) |
| New York | poverty_rate | 0.000 [-0.001, 0.001] (n=44632) | -0.001 [-0.002, 0.001] (n=58704) | 0.001 [-0.001, 0.002] (n=73705) | -0.000 [-0.002, 0.001] (n=57560) | -0.000 [-0.002, 0.001] (n=39911) | 0.002 [-0.001, 0.004] (n=14225) | 0.001 [-0.001, 0.002] (n=54603) |
| New York | owner_share | 0.000 [-0.001, 0.001] (n=42025) | 0.001 [-0.000, 0.002] (n=56058) | 0.001 [-0.001, 0.002] (n=70722) | -0.000 [-0.001, 0.001] (n=55345) | 0.000 [-0.001, 0.002] (n=38968) | -0.000 [-0.003, 0.003] (n=13816) | 0.000 [-0.001, 0.002] (n=53212) |
| New York | median_hh_income | 0.001 [-0.001, 0.003] (n=41976) | 0.001 [-0.000, 0.002] (n=54594) | 0.001 [-0.000, 0.001] (n=68456) | 0.000 [-0.001, 0.002] (n=53399) | 0.000 [-0.001, 0.002] (n=36338) | 0.001 [-0.001, 0.004] (n=12913) | 0.000 [-0.001, 0.002] (n=51978) |

Counties:

| state | variable | 0-50 km | 50-100 km | 100-200 km | 200-400 km | 400-inf km |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | households | 0.031 [0.003, 0.059] (n=139) | 0.026 [0.004, 0.049] (n=374) | 0.027 [0.010, 0.045] (n=1023) | 0.034 [0.014, 0.054] (n=1309) | 0.038 [0.005, 0.071] (n=558) |
| Michigan | poverty_count | -0.007 [-0.033, 0.020] (n=139) | 0.008 [-0.007, 0.024] (n=374) | 0.003 [-0.008, 0.013] (n=1023) | 0.002 [-0.009, 0.012] (n=1309) | 0.001 [-0.018, 0.021] (n=558) |
| Michigan | poverty_rate | -0.007 [-0.033, 0.020] (n=139) | 0.008 [-0.007, 0.024] (n=374) | 0.003 [-0.008, 0.013] (n=1023) | 0.002 [-0.009, 0.013] (n=1309) | 0.001 [-0.018, 0.021] (n=558) |
| Michigan | owner_share | -0.007 [-0.035, 0.022] (n=139) | 0.018 [0.001, 0.035] (n=374) | 0.010 [0.000, 0.021] (n=1023) | 0.001 [-0.009, 0.011] (n=1309) | -0.001 [-0.014, 0.011] (n=558) |
| Michigan | median_hh_income | 0.004 [-0.021, 0.028] (n=139) | 0.004 [-0.011, 0.019] (n=374) | -0.002 [-0.012, 0.008] (n=1023) | 0.002 [-0.007, 0.010] (n=1309) | -0.000 [-0.014, 0.014] (n=558) |
| New York | households | -0.001 [-0.037, 0.036] (n=91) | 0.038 [0.011, 0.065] (n=235) | 0.027 [0.008, 0.045] (n=537) | 0.032 [0.017, 0.046] (n=867) | 0.021 [-0.035, 0.077] (n=161) |
| New York | poverty_count | 0.003 [-0.028, 0.035] (n=91) | 0.009 [-0.010, 0.028] (n=235) | 0.011 [-0.006, 0.028] (n=537) | 0.009 [-0.003, 0.020] (n=867) | 0.006 [-0.021, 0.033] (n=161) |
| New York | poverty_rate | 0.004 [-0.028, 0.035] (n=91) | 0.009 [-0.011, 0.029] (n=235) | 0.011 [-0.006, 0.028] (n=537) | 0.009 [-0.003, 0.021] (n=867) | 0.007 [-0.021, 0.034] (n=161) |
| New York | owner_share | 0.017 [-0.030, 0.064] (n=91) | 0.012 [-0.011, 0.035] (n=235) | 0.002 [-0.013, 0.017] (n=537) | 0.012 [0.000, 0.023] (n=867) | 0.007 [-0.017, 0.031] (n=161) |
| New York | median_hh_income | 0.005 [-0.030, 0.040] (n=91) | 0.011 [-0.011, 0.034] (n=235) | 0.004 [-0.013, 0.022] (n=537) | 0.007 [-0.005, 0.018] (n=867) | 0.023 [-0.000, 0.047] (n=161) |

### Aggregation ratio: Var(county sum of tracts) / sum of tract variances

| state | variable | counties | tracts | pooled ratio (SE) | median county ratio | implied mean within-county r (SE) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | 83 | 2914 | 0.000 (0.000) | 0.000 | -0.0043 (0.0000) |
| Michigan | households | 83 | 2897 | 0.218 (0.008) | 0.474 | -0.0039 (0.0000) |
| Michigan | poverty_count | 82 | 2892 | 0.915 (0.051) | 0.896 | -0.0004 (0.0003) |
| New York | population | 62 | 5289 | 0.005 (0.001) | 0.000 | -0.0030 (0.0000) |
| New York | households | 62 | 5251 | 0.259 (0.018) | 0.331 | -0.0026 (0.0001) |
| New York | poverty_count | 62 | 5242 | 0.879 (0.044) | 0.936 | -0.0005 (0.0002) |

### Noise floor and spread

Analytic null (80 replicates): SD 0.112, 95% |r| 0.219.

Michigan x New York pairs (independent samples):

| geography | variable | pairs | mean r (SE) | SD of r | 95% |r| | share |r| > analytic critical |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| tract | population | 50000 | -0.0009 (0.0004) | 0.115 | 0.229 | 0.060 |
| tract | households | 50000 | 0.0006 (0.0004) | 0.116 | 0.230 | 0.060 |
| tract | poverty_count | 50000 | 0.0008 (0.0005) | 0.116 | 0.236 | 0.064 |
| tract | poverty_rate | 50000 | -0.0003 (0.0005) | 0.115 | 0.231 | 0.062 |
| tract | owner_share | 50000 | 0.0009 (0.0005) | 0.116 | 0.231 | 0.062 |
| tract | median_hh_income | 50000 | 0.0011 (0.0004) | 0.115 | 0.226 | 0.058 |
| county | households | 5146 | 0.0320 (0.0032) | 0.121 | 0.243 | 0.077 |
| county | poverty_count | 5146 | 0.0017 (0.0017) | 0.116 | 0.223 | 0.055 |
| county | poverty_rate | 5146 | 0.0018 (0.0017) | 0.116 | 0.223 | 0.055 |
| county | owner_share | 5146 | 0.0066 (0.0019) | 0.116 | 0.226 | 0.058 |
| county | median_hh_income | 5146 | 0.0010 (0.0014) | 0.115 | 0.226 | 0.058 |

Derangement null on far pairs (same state, beyond order 3):

| state | geography | variable | pairs | mean | SD | 95% |r| |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | 225050 | 0.0000 | 0.112 | 0.218 |
| Michigan | tract | households | 223197 | 0.0003 | 0.112 | 0.219 |
| Michigan | tract | poverty_count | 222204 | -0.0001 | 0.112 | 0.219 |
| Michigan | tract | poverty_rate | 221162 | -0.0000 | 0.112 | 0.218 |
| Michigan | tract | owner_share | 217458 | 0.0003 | 0.112 | 0.219 |
| Michigan | tract | median_hh_income | 217954 | 0.0006 | 0.113 | 0.220 |
| New York | tract | population | 230255 | 0.0000 | 0.112 | 0.219 |
| New York | tract | households | 227670 | 0.0003 | 0.112 | 0.219 |
| New York | tract | poverty_count | 226711 | 0.0001 | 0.112 | 0.219 |
| New York | tract | poverty_rate | 226417 | 0.0001 | 0.112 | 0.219 |
| New York | tract | owner_share | 217364 | 0.0005 | 0.112 | 0.219 |
| New York | tract | median_hh_income | 210794 | 0.0008 | 0.112 | 0.220 |
| Michigan | county | households | 2452 | 0.0311 | 0.123 | 0.248 |
| Michigan | county | poverty_count | 2452 | 0.0010 | 0.112 | 0.219 |
| Michigan | county | poverty_rate | 2452 | 0.0009 | 0.112 | 0.219 |
| Michigan | county | owner_share | 2452 | 0.0023 | 0.112 | 0.220 |
| Michigan | county | median_hh_income | 2452 | -0.0002 | 0.112 | 0.219 |
| New York | county | households | 1266 | 0.0281 | 0.115 | 0.231 |
| New York | county | poverty_count | 1266 | 0.0114 | 0.112 | 0.220 |
| New York | county | poverty_rate | 1266 | 0.0116 | 0.112 | 0.221 |
| New York | county | owner_share | 1266 | 0.0096 | 0.112 | 0.220 |
| New York | county | median_hh_income | 1266 | 0.0091 | 0.112 | 0.219 |

Replicate design spectrum (lowest eigenvalues relative to the mean; Marchenko-Pastur lower edge for independent replicates in brackets):

| state | geography | variable | lowest 4 eigenvalues (MP edge) | near-null | effective replicates | null SD 1/sqrt(eff.) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | 0.040 0.046 0.073 0.391 (0.696) | 3 | 77 | 0.114 |
| Michigan | tract | households | 0.028 0.030 0.054 0.399 (0.695) | 3 | 77 | 0.114 |
| Michigan | tract | poverty_count | 0.028 0.030 0.064 0.403 (0.695) | 3 | 77 | 0.114 |
| Michigan | tract | poverty_rate | 0.028 0.031 0.064 0.402 (0.695) | 3 | 77 | 0.114 |
| Michigan | tract | owner_share | 0.029 0.032 0.063 0.391 (0.694) | 3 | 77 | 0.114 |
| Michigan | tract | median_hh_income | 0.068 0.073 0.108 0.421 (0.694) | 3 | 77 | 0.114 |
| New York | tract | population | 0.038 0.043 0.073 0.417 (0.769) | 3 | 77 | 0.114 |
| New York | tract | households | 0.024 0.026 0.050 0.404 (0.768) | 3 | 77 | 0.114 |
| New York | tract | poverty_count | 0.028 0.032 0.061 0.408 (0.768) | 3 | 77 | 0.114 |
| New York | tract | poverty_rate | 0.030 0.034 0.062 0.410 (0.768) | 3 | 77 | 0.114 |
| New York | tract | owner_share | 0.028 0.031 0.062 0.407 (0.766) | 3 | 77 | 0.114 |
| New York | tract | median_hh_income | 0.081 0.085 0.114 0.422 (0.764) | 3 | 77 | 0.114 |
| Michigan | county | households | 0.000 0.001 0.003 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | poverty_count | 0.001 0.002 0.002 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | poverty_rate | 0.001 0.002 0.003 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | owner_share | 0.000 0.001 0.001 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | median_hh_income | 0.000 0.001 0.003 0.004 (-) | not tested (too few areas) | - | - |
| New York | county | households | 0.008 0.016 0.025 0.030 (-) | not tested (too few areas) | - | - |
| New York | county | poverty_count | 0.019 0.034 0.042 0.054 (-) | not tested (too few areas) | - | - |
| New York | county | poverty_rate | 0.019 0.033 0.043 0.055 (-) | not tested (too few areas) | - | - |
| New York | county | owner_share | 0.012 0.019 0.031 0.038 (-) | not tested (too few areas) | - | - |
| New York | county | median_hh_income | 0.018 0.030 0.032 0.059 (-) | not tested (too few areas) | - | - |

Spread of pairwise r per class: SD, excess SD over the analytic null (sqrt(SD^2 - 1/80)) and over the empirical Michigan x New York null (sqrt(SD^2 - SD_cross^2)), and the share of pairs with |r| above the analytic 5% critical value (0.05 expected under no correlation; about 0.06 in the independent cross-state pairs):

| state | geography | variable | class | SD of r | excess SD (analytic) | excess SD (cross-state) | share |r| > crit |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | order 1 | 0.192 | 0.156 | 0.153 | 0.168 |
| Michigan | tract | population | order 2 | 0.126 | 0.058 | 0.050 | 0.076 |
| Michigan | tract | population | order 3 | 0.118 | 0.039 | 0.026 | 0.063 |
| Michigan | tract | population | beyond order 3 | 0.115 | 0.027 | 0.000 | 0.060 |
| Michigan | tract | households | order 1 | 0.136 | 0.078 | 0.072 | 0.105 |
| Michigan | tract | households | order 2 | 0.120 | 0.042 | 0.030 | 0.069 |
| Michigan | tract | households | order 3 | 0.116 | 0.032 | 0.011 | 0.062 |
| Michigan | tract | households | beyond order 3 | 0.115 | 0.028 | 0.000 | 0.061 |
| Michigan | tract | poverty_count | order 1 | 0.115 | 0.027 | 0.000 | 0.063 |
| Michigan | tract | poverty_count | order 2 | 0.116 | 0.032 | 0.011 | 0.066 |
| Michigan | tract | poverty_count | order 3 | 0.116 | 0.031 | 0.008 | 0.064 |
| Michigan | tract | poverty_count | beyond order 3 | 0.115 | 0.027 | 0.000 | 0.064 |
| Michigan | tract | poverty_rate | order 1 | 0.115 | 0.027 | 0.006 | 0.065 |
| Michigan | tract | poverty_rate | order 2 | 0.116 | 0.032 | 0.017 | 0.065 |
| Michigan | tract | poverty_rate | order 3 | 0.116 | 0.032 | 0.017 | 0.063 |
| Michigan | tract | poverty_rate | beyond order 3 | 0.115 | 0.027 | 0.006 | 0.062 |
| Michigan | tract | owner_share | order 1 | 0.115 | 0.028 | 0.000 | 0.061 |
| Michigan | tract | owner_share | order 2 | 0.117 | 0.033 | 0.012 | 0.062 |
| Michigan | tract | owner_share | order 3 | 0.115 | 0.029 | 0.000 | 0.060 |
| Michigan | tract | owner_share | beyond order 3 | 0.115 | 0.027 | 0.000 | 0.060 |
| Michigan | tract | median_hh_income | order 1 | 0.115 | 0.028 | 0.000 | 0.059 |
| Michigan | tract | median_hh_income | order 2 | 0.115 | 0.027 | 0.000 | 0.057 |
| Michigan | tract | median_hh_income | order 3 | 0.116 | 0.029 | 0.005 | 0.058 |
| Michigan | tract | median_hh_income | beyond order 3 | 0.116 | 0.030 | 0.010 | 0.059 |
| New York | tract | population | order 2 | 0.122 | 0.048 | 0.038 | 0.071 |
| New York | tract | population | order 1 | 0.175 | 0.134 | 0.131 | 0.118 |
| New York | tract | population | order 3 | 0.117 | 0.035 | 0.020 | 0.062 |
| New York | tract | population | beyond order 3 | 0.115 | 0.029 | 0.000 | 0.061 |
| New York | tract | households | order 2 | 0.117 | 0.036 | 0.019 | 0.065 |
| New York | tract | households | order 1 | 0.128 | 0.062 | 0.055 | 0.083 |
| New York | tract | households | order 3 | 0.116 | 0.031 | 0.008 | 0.064 |
| New York | tract | households | beyond order 3 | 0.115 | 0.028 | 0.000 | 0.061 |
| New York | tract | poverty_count | order 2 | 0.116 | 0.029 | 0.000 | 0.066 |
| New York | tract | poverty_count | order 1 | 0.115 | 0.028 | 0.000 | 0.064 |
| New York | tract | poverty_count | order 3 | 0.115 | 0.028 | 0.000 | 0.066 |
| New York | tract | poverty_count | beyond order 3 | 0.115 | 0.029 | 0.000 | 0.065 |
| New York | tract | poverty_rate | order 2 | 0.115 | 0.028 | 0.007 | 0.063 |
| New York | tract | poverty_rate | order 1 | 0.115 | 0.027 | 0.002 | 0.065 |
| New York | tract | poverty_rate | order 3 | 0.115 | 0.028 | 0.008 | 0.063 |
| New York | tract | poverty_rate | beyond order 3 | 0.116 | 0.030 | 0.013 | 0.064 |
| New York | tract | owner_share | order 2 | 0.115 | 0.028 | 0.000 | 0.060 |
| New York | tract | owner_share | order 1 | 0.115 | 0.028 | 0.000 | 0.060 |
| New York | tract | owner_share | order 3 | 0.115 | 0.029 | 0.000 | 0.061 |
| New York | tract | owner_share | beyond order 3 | 0.115 | 0.029 | 0.000 | 0.061 |
| New York | tract | median_hh_income | order 2 | 0.115 | 0.028 | 0.000 | 0.057 |
| New York | tract | median_hh_income | order 1 | 0.115 | 0.027 | 0.000 | 0.054 |
| New York | tract | median_hh_income | order 3 | 0.116 | 0.032 | 0.013 | 0.060 |
| New York | tract | median_hh_income | beyond order 3 | 0.116 | 0.029 | 0.005 | 0.059 |
| Michigan | county | households | order 1 | 0.117 | 0.034 | 0.000 | 0.082 |
| Michigan | county | households | order 3 | 0.120 | 0.043 | 0.000 | 0.079 |
| Michigan | county | households | order 2 | 0.121 | 0.045 | 0.000 | 0.065 |
| Michigan | county | households | beyond order 3 | 0.126 | 0.059 | 0.037 | 0.093 |
| Michigan | county | poverty_count | order 1 | 0.114 | 0.023 | 0.000 | 0.058 |
| Michigan | county | poverty_count | order 3 | 0.116 | 0.029 | 0.000 | 0.059 |
| Michigan | county | poverty_count | order 2 | 0.113 | 0.018 | 0.000 | 0.044 |
| Michigan | county | poverty_count | beyond order 3 | 0.116 | 0.030 | 0.000 | 0.059 |
| Michigan | county | poverty_rate | order 1 | 0.114 | 0.022 | 0.000 | 0.058 |
| Michigan | county | poverty_rate | order 3 | 0.116 | 0.030 | 0.000 | 0.057 |
| Michigan | county | poverty_rate | order 2 | 0.113 | 0.018 | 0.000 | 0.041 |
| Michigan | county | poverty_rate | beyond order 3 | 0.116 | 0.030 | 0.004 | 0.058 |
| Michigan | county | owner_share | order 1 | 0.116 | 0.031 | 0.006 | 0.053 |
| Michigan | county | owner_share | order 3 | 0.117 | 0.034 | 0.016 | 0.064 |
| Michigan | county | owner_share | order 2 | 0.115 | 0.027 | 0.000 | 0.044 |
| Michigan | county | owner_share | beyond order 3 | 0.115 | 0.026 | 0.000 | 0.056 |
| Michigan | county | median_hh_income | order 1 | 0.112 | 0.010 | 0.000 | 0.062 |
| Michigan | county | median_hh_income | order 3 | 0.114 | 0.024 | 0.000 | 0.057 |
| Michigan | county | median_hh_income | order 2 | 0.115 | 0.028 | 0.011 | 0.062 |
| Michigan | county | median_hh_income | beyond order 3 | 0.111 | 0.000 | 0.000 | 0.048 |
| New York | county | households | order 3 | 0.119 | 0.041 | 0.000 | 0.063 |
| New York | county | households | order 1 | 0.112 | 0.000 | 0.000 | 0.049 |
| New York | county | households | order 2 | 0.126 | 0.058 | 0.035 | 0.091 |
| New York | county | households | beyond order 3 | 0.118 | 0.036 | 0.000 | 0.070 |
| New York | county | poverty_count | order 3 | 0.123 | 0.050 | 0.040 | 0.079 |
| New York | county | poverty_count | order 1 | 0.118 | 0.038 | 0.024 | 0.056 |
| New York | county | poverty_count | order 2 | 0.115 | 0.027 | 0.000 | 0.061 |
| New York | county | poverty_count | beyond order 3 | 0.114 | 0.023 | 0.000 | 0.064 |
| New York | county | poverty_rate | order 3 | 0.122 | 0.050 | 0.040 | 0.079 |
| New York | county | poverty_rate | order 1 | 0.119 | 0.041 | 0.028 | 0.049 |
| New York | county | poverty_rate | order 2 | 0.115 | 0.027 | 0.000 | 0.065 |
| New York | county | poverty_rate | beyond order 3 | 0.114 | 0.023 | 0.000 | 0.062 |
| New York | county | owner_share | order 3 | 0.109 | 0.000 | 0.000 | 0.048 |
| New York | county | owner_share | order 1 | 0.115 | 0.028 | 0.000 | 0.063 |
| New York | county | owner_share | order 2 | 0.115 | 0.029 | 0.000 | 0.056 |
| New York | county | owner_share | beyond order 3 | 0.115 | 0.025 | 0.000 | 0.063 |
| New York | county | median_hh_income | order 3 | 0.111 | 0.000 | 0.000 | 0.048 |
| New York | county | median_hh_income | order 1 | 0.117 | 0.034 | 0.023 | 0.063 |
| New York | county | median_hh_income | order 2 | 0.119 | 0.041 | 0.031 | 0.074 |
| New York | county | median_hh_income | beyond order 3 | 0.114 | 0.020 | 0.000 | 0.051 |

### Split-half reliability of individual pairwise correlations

Per class: half-A and half-B class means (random split), and the reliability of a single pair's r (correlation across pairs between halves; stepped up to 80 replicates), for each split.

| state | geography | variable | class | pairs | mean (half A) | mean (half B) | reliability (random / odd-even / first-last) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | order 1 | 9099 | -0.070 | -0.070 | 0.67 / 0.67 / 0.66 |
| Michigan | tract | population | order 2 | 21283 | -0.014 | -0.014 | 0.22 / 0.21 / 0.18 |
| Michigan | tract | population | order 3 | 36300 | -0.003 | -0.005 | 0.11 / 0.11 / 0.09 |
| Michigan | tract | population | beyond order 3 | 225050 | -0.000 | -0.001 | 0.06 / 0.05 / 0.02 |
| Michigan | tract | households | order 1 | 9033 | -0.024 | -0.026 | 0.34 / 0.32 / 0.30 |
| Michigan | tract | households | order 2 | 21067 | -0.007 | -0.007 | 0.14 / 0.11 / 0.08 |
| Michigan | tract | households | order 3 | 35923 | -0.003 | -0.004 | 0.08 / 0.07 / 0.03 |
| Michigan | tract | households | beyond order 3 | 223197 | -0.001 | -0.001 | 0.07 / 0.05 / 0.01 |
| Michigan | tract | poverty_count | order 1 | 9024 | -0.003 | -0.004 | 0.04 / 0.07 / -0.01 |
| Michigan | tract | poverty_count | order 2 | 21028 | -0.003 | -0.003 | 0.07 / 0.08 / 0.02 |
| Michigan | tract | poverty_count | order 3 | 35876 | -0.001 | 0.000 | 0.07 / 0.08 / 0.04 |
| Michigan | tract | poverty_count | beyond order 3 | 222204 | 0.001 | -0.000 | 0.06 / 0.05 / -0.00 |
| Michigan | tract | poverty_rate | order 1 | 8988 | 0.000 | -0.000 | 0.05 / 0.07 / -0.01 |
| Michigan | tract | poverty_rate | order 2 | 20944 | -0.002 | -0.002 | 0.07 / 0.08 / 0.03 |
| Michigan | tract | poverty_rate | order 3 | 35733 | -0.000 | 0.000 | 0.07 / 0.08 / 0.04 |
| Michigan | tract | poverty_rate | beyond order 3 | 221162 | 0.001 | -0.000 | 0.06 / 0.05 / -0.00 |
| Michigan | tract | owner_share | order 1 | 8875 | -0.003 | -0.001 | 0.06 / 0.06 / -0.02 |
| Michigan | tract | owner_share | order 2 | 20622 | 0.000 | 0.001 | 0.07 / 0.07 / 0.05 |
| Michigan | tract | owner_share | order 3 | 35161 | -0.001 | -0.000 | 0.06 / 0.05 / 0.01 |
| Michigan | tract | owner_share | beyond order 3 | 217458 | 0.000 | -0.000 | 0.06 / 0.05 / -0.00 |
| Michigan | tract | median_hh_income | order 1 | 8892 | -0.001 | 0.001 | 0.06 / 0.06 / 0.03 |
| Michigan | tract | median_hh_income | order 2 | 20659 | 0.000 | 0.001 | 0.06 / 0.06 / 0.03 |
| Michigan | tract | median_hh_income | order 3 | 35172 | 0.001 | 0.001 | 0.06 / 0.06 / 0.04 |
| Michigan | tract | median_hh_income | beyond order 3 | 217954 | 0.001 | 0.001 | 0.07 / 0.07 / 0.05 |
| New York | tract | population | order 2 | 37762 | -0.009 | -0.008 | 0.16 / 0.16 / 0.12 |
| New York | tract | population | order 1 | 15821 | -0.039 | -0.041 | 0.61 / 0.60 / 0.58 |
| New York | tract | population | order 3 | 65290 | -0.003 | -0.003 | 0.10 / 0.09 / 0.05 |
| New York | tract | population | beyond order 3 | 230255 | -0.001 | -0.001 | 0.07 / 0.06 / 0.01 |
| New York | tract | households | order 2 | 37281 | -0.004 | -0.003 | 0.09 / 0.09 / 0.05 |
| New York | tract | households | order 1 | 15668 | -0.014 | -0.016 | 0.25 / 0.21 / 0.20 |
| New York | tract | households | order 3 | 64393 | -0.003 | -0.002 | 0.09 / 0.06 / 0.00 |
| New York | tract | households | beyond order 3 | 227670 | -0.001 | -0.001 | 0.06 / 0.05 / -0.00 |
| New York | tract | poverty_count | order 2 | 37221 | -0.000 | -0.001 | 0.06 / 0.05 / 0.02 |
| New York | tract | poverty_count | order 1 | 15646 | -0.001 | -0.002 | 0.05 / 0.05 / -0.02 |
| New York | tract | poverty_count | order 3 | 64288 | 0.001 | -0.000 | 0.06 / 0.05 / -0.00 |
| New York | tract | poverty_count | beyond order 3 | 226711 | -0.000 | 0.000 | 0.06 / 0.06 / -0.00 |
| New York | tract | poverty_rate | order 2 | 37151 | 0.001 | -0.001 | 0.06 / 0.04 / 0.01 |
| New York | tract | poverty_rate | order 1 | 15627 | 0.001 | -0.001 | 0.04 / 0.03 / -0.02 |
| New York | tract | poverty_rate | order 3 | 64145 | 0.000 | -0.000 | 0.05 / 0.05 / 0.00 |
| New York | tract | poverty_rate | beyond order 3 | 226417 | 0.000 | 0.000 | 0.07 / 0.06 / 0.00 |
| New York | tract | owner_share | order 2 | 35819 | -0.001 | 0.001 | 0.05 / 0.04 / -0.00 |
| New York | tract | owner_share | order 1 | 15121 | -0.001 | -0.000 | 0.06 / 0.06 / -0.02 |
| New York | tract | owner_share | order 3 | 61842 | 0.001 | 0.001 | 0.06 / 0.06 / -0.01 |
| New York | tract | owner_share | beyond order 3 | 217364 | 0.000 | 0.001 | 0.06 / 0.05 / 0.01 |
| New York | tract | median_hh_income | order 2 | 34551 | 0.000 | 0.002 | 0.07 / 0.06 / 0.04 |
| New York | tract | median_hh_income | order 1 | 14672 | -0.000 | 0.000 | 0.02 / 0.04 / 0.03 |
| New York | tract | median_hh_income | order 3 | 59637 | -0.000 | 0.000 | 0.07 / 0.08 / 0.04 |
| New York | tract | median_hh_income | beyond order 3 | 210794 | 0.000 | 0.001 | 0.07 / 0.07 / 0.04 |
| Michigan | county | households | order 1 | 208 | 0.003 | 0.052 | 0.09 / -0.04 / 0.14 |
| Michigan | county | households | order 3 | 404 | 0.019 | 0.035 | 0.10 / 0.17 / 0.15 |
| Michigan | county | households | order 2 | 339 | 0.028 | 0.035 | 0.18 / 0.24 / 0.18 |
| Michigan | county | households | beyond order 3 | 2452 | 0.029 | 0.036 | 0.29 / 0.26 / 0.25 |
| Michigan | county | poverty_count | order 1 | 208 | -0.011 | 0.001 | -0.03 / 0.11 / -0.09 |
| Michigan | county | poverty_count | order 3 | 404 | -0.005 | 0.014 | 0.11 / 0.06 / 0.03 |
| Michigan | county | poverty_count | order 2 | 339 | 0.007 | 0.012 | 0.14 / 0.01 / -0.10 |
| Michigan | county | poverty_count | beyond order 3 | 2452 | 0.002 | 0.001 | 0.04 / 0.02 / 0.04 |
| Michigan | county | poverty_rate | order 1 | 208 | -0.012 | 0.001 | -0.03 / 0.11 / -0.10 |
| Michigan | county | poverty_rate | order 3 | 404 | -0.005 | 0.014 | 0.11 / 0.06 / 0.04 |
| Michigan | county | poverty_rate | order 2 | 339 | 0.008 | 0.012 | 0.14 / 0.00 / -0.11 |
| Michigan | county | poverty_rate | beyond order 3 | 2452 | 0.002 | 0.000 | 0.04 / 0.02 / 0.04 |
| Michigan | county | owner_share | order 1 | 208 | -0.001 | -0.011 | -0.03 / 0.12 / 0.03 |
| Michigan | county | owner_share | order 3 | 404 | 0.014 | 0.031 | 0.13 / 0.05 / 0.17 |
| Michigan | county | owner_share | order 2 | 339 | 0.025 | 0.009 | -0.01 / 0.07 / 0.01 |
| Michigan | county | owner_share | beyond order 3 | 2452 | -0.000 | 0.004 | 0.04 / 0.01 / 0.01 |
| Michigan | county | median_hh_income | order 1 | 208 | 0.013 | -0.008 | 0.14 / 0.05 / -0.06 |
| Michigan | county | median_hh_income | order 3 | 404 | 0.002 | 0.003 | 0.17 / -0.03 / -0.02 |
| Michigan | county | median_hh_income | order 2 | 339 | -0.004 | 0.009 | 0.08 / 0.13 / 0.14 |
| Michigan | county | median_hh_income | beyond order 3 | 2452 | 0.001 | -0.001 | 0.01 / -0.06 / -0.06 |
| New York | county | households | order 3 | 252 | 0.025 | 0.026 | 0.23 / 0.26 / 0.25 |
| New York | county | households | order 1 | 142 | 0.029 | 0.013 | 0.11 / 0.24 / 0.00 |
| New York | county | households | order 2 | 231 | 0.041 | 0.006 | 0.18 / 0.34 / 0.27 |
| New York | county | households | beyond order 3 | 1266 | 0.036 | 0.026 | 0.17 / 0.10 / 0.10 |
| New York | county | poverty_count | order 3 | 252 | 0.019 | 0.017 | 0.05 / 0.12 / 0.15 |
| New York | county | poverty_count | order 1 | 142 | 0.011 | -0.004 | 0.36 / -0.08 / 0.26 |
| New York | county | poverty_count | order 2 | 231 | 0.011 | -0.000 | 0.08 / 0.13 / -0.02 |
| New York | county | poverty_count | beyond order 3 | 1266 | 0.002 | 0.014 | 0.07 / 0.05 / -0.04 |
| New York | county | poverty_rate | order 3 | 252 | 0.019 | 0.017 | 0.05 / 0.12 / 0.14 |
| New York | county | poverty_rate | order 1 | 142 | 0.012 | -0.005 | 0.37 / -0.06 / 0.27 |
| New York | county | poverty_rate | order 2 | 231 | 0.012 | -0.000 | 0.06 / 0.13 / -0.03 |
| New York | county | poverty_rate | beyond order 3 | 1266 | 0.003 | 0.014 | 0.07 / 0.05 / -0.04 |
| New York | county | owner_share | order 3 | 252 | -0.002 | 0.000 | -0.10 / 0.01 / -0.09 |
| New York | county | owner_share | order 1 | 142 | 0.003 | -0.006 | 0.15 / 0.10 / 0.13 |
| New York | county | owner_share | order 2 | 231 | -0.005 | 0.024 | 0.05 / 0.15 / 0.06 |
| New York | county | owner_share | beyond order 3 | 1266 | 0.019 | 0.006 | 0.02 / 0.09 / 0.08 |
| New York | county | median_hh_income | order 3 | 252 | 0.001 | 0.007 | 0.05 / -0.05 / -0.10 |
| New York | county | median_hh_income | order 1 | 142 | 0.021 | -0.007 | 0.04 / 0.20 / 0.17 |
| New York | county | median_hh_income | order 2 | 231 | -0.016 | 0.001 | 0.10 / 0.02 / -0.01 |
| New York | county | median_hh_income | beyond order 3 | 1266 | 0.009 | 0.015 | 0.03 / 0.05 / -0.06 |

### Held-out replicate log-likelihood gain over independence (nats per replicate)

| state | geography | variable | areas | order (1-3) | order x county | order x county (+ far within county) | ledoit_wolf | fitted order-1 r (halves) | LW intensity (40 reps) | LW intensity (80 reps) | rank / areas |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | 2914 | 32.87 | -164.87 (projected) | -551.70 (projected) | -25.00 | -0.070 | 0.870 | 0.924 | 80 / 2914 |
| Michigan | tract | households | 2897 | 4.41 | 5.07 | -165.60 (projected) | -27.34 | -0.026 | 0.871 | 0.931 | 80 / 2897 |
| Michigan | tract | poverty_count | 2893 | 0.18 | 0.16 | 0.15 | -28.70 | -0.004 | 0.870 | 0.930 | 80 / 2893 |
| Michigan | tract | poverty_rate | 2887 | 0.05 | -0.04 | -0.05 | -28.20 | 0.000 | 0.870 | 0.929 | 80 / 2887 |
| Michigan | tract | owner_share | 2865 | -0.00 | -0.00 | -0.03 | -28.50 | -0.002 | 0.870 | 0.931 | 80 / 2865 |
| Michigan | tract | median_hh_income | 2870 | -0.02 | -0.10 | -0.14 | -36.92 | -0.001 | 0.872 | 0.930 | 80 / 2870 |
| New York | tract | population | 5289 | 16.85 | 17.15 | -244.21 (projected) | -36.87 | -0.040 | 0.869 | 0.928 | 80 / 5289 |
| New York | tract | households | 5251 | 2.20 | 2.27 | -373.56 (projected) | -37.33 | -0.015 | 0.870 | 0.929 | 80 / 5251 |
| New York | tract | poverty_count | 5242 | -0.04 | -0.10 | -0.10 | -37.16 | -0.002 | 0.870 | 0.927 | 80 / 5242 |
| New York | tract | poverty_rate | 5238 | -0.09 | -0.13 | -0.13 | -38.19 | 0.000 | 0.870 | 0.927 | 80 / 5238 |
| New York | tract | owner_share | 5145 | -0.02 | -0.06 | -0.06 | -40.09 | -0.001 | 0.869 | 0.928 | 80 / 5145 |
| New York | tract | median_hh_income | 5053 | -0.04 | -0.08 | -0.09 | -49.58 | -0.001 | 0.872 | 0.927 | 80 / 5053 |
| Michigan | county | households | 83 | -0.10 | - | - | -0.17 | -0.000 | 0.910 | 0.764 | 80 / 83 |
| Michigan | county | poverty_count | 83 | -0.03 | - | - | -0.35 | -0.008 | 0.896 | 0.952 | 80 / 83 |
| Michigan | county | poverty_rate | 83 | -0.03 | - | - | -0.36 | -0.008 | 0.894 | 0.950 | 80 / 83 |
| Michigan | county | owner_share | 83 | -0.04 | - | - | -0.54 | -0.011 | 0.881 | 0.950 | 80 / 83 |
| Michigan | county | median_hh_income | 83 | -0.06 | - | - | -0.22 | 0.003 | 0.931 | 1.000 | 80 / 83 |
| New York | county | households | 62 | -0.08 | - | - | -0.12 | -0.007 | 0.893 | 0.852 | 62 / 62 |
| New York | county | poverty_count | 62 | -0.05 | - | - | -0.07 | -0.005 | 0.911 | 0.953 | 62 / 62 |
| New York | county | poverty_rate | 62 | -0.05 | - | - | -0.08 | -0.005 | 0.910 | 0.953 | 62 / 62 |
| New York | county | owner_share | 62 | -0.08 | - | - | -0.52 | -0.014 | 0.883 | 0.971 | 62 / 62 |
| New York | county | median_hh_income | 62 | -0.06 | - | - | -0.18 | -0.001 | 0.901 | 0.973 | 62 / 62 |

## Results 2015-2019

Mean pairwise r [95% interval] (number of pairs) by queen contiguity order.

### Tracts: contiguity order

| state | variable | order 1 | order 2 | order 3 | beyond order 3 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.066 [-0.088, -0.044] (n=8661) | -0.014 [-0.017, -0.011] (n=20170) | -0.004 [-0.005, -0.003] (n=34283) | -0.000 [-0.001, -0.000] (n=235969) |
| Michigan | households | 0.025 [0.016, 0.033] (n=8614) | 0.006 [0.000, 0.013] (n=20000) | 0.001 [-0.002, 0.005] (n=33974) | -0.000 [-0.001, 0.001] (n=234537) |
| Michigan | poverty_count | -0.004 [-0.007, -0.001] (n=8601) | -0.002 [-0.004, 0.000] (n=19957) | 0.000 [-0.002, 0.002] (n=33911) | -0.000 [-0.000, 0.000] (n=234178) |
| Michigan | poverty_rate | -0.000 [-0.003, 0.003] (n=8581) | -0.001 [-0.003, 0.001] (n=19916) | 0.000 [-0.002, 0.002] (n=33847) | 0.000 [-0.001, 0.001] (n=233863) |
| Michigan | owner_share | -0.001 [-0.004, 0.001] (n=8503) | -0.001 [-0.004, 0.002] (n=19730) | 0.001 [-0.000, 0.002] (n=33514) | 0.000 [-0.001, 0.001] (n=231151) |
| Michigan | median_hh_income | 0.002 [-0.001, 0.004] (n=8563) | -0.000 [-0.003, 0.003] (n=19869) | 0.001 [-0.001, 0.002] (n=33766) | 0.001 [-0.000, 0.002] (n=232936) |
| New York | population | -0.039 [-0.060, -0.018] (n=14629) | -0.008 [-0.013, -0.003] (n=35303) | -0.002 [-0.004, -0.001] (n=61223) | -0.001 [-0.002, -0.000] (n=236096) |
| New York | households | 0.012 [0.003, 0.020] (n=14502) | 0.005 [0.002, 0.008] (n=34948) | 0.002 [0.001, 0.004] (n=60587) | -0.000 [-0.001, 0.001] (n=234210) |
| New York | poverty_count | -0.003 [-0.005, -0.001] (n=14431) | -0.000 [-0.002, 0.001] (n=34710) | -0.001 [-0.002, 0.000] (n=60162) | 0.000 [-0.000, 0.001] (n=232610) |
| New York | poverty_rate | -0.001 [-0.002, 0.001] (n=14378) | -0.000 [-0.001, 0.001] (n=34613) | -0.001 [-0.002, 0.001] (n=60035) | 0.000 [-0.000, 0.001] (n=232123) |
| New York | owner_share | 0.000 [-0.002, 0.002] (n=14033) | 0.000 [-0.001, 0.002] (n=33723) | 0.000 [-0.001, 0.002] (n=58430) | 0.000 [-0.000, 0.001] (n=225550) |
| New York | median_hh_income | 0.002 [-0.001, 0.004] (n=14048) | 0.000 [-0.001, 0.002] (n=33653) | 0.001 [-0.001, 0.002] (n=58491) | 0.000 [-0.000, 0.001] (n=227384) |

### Counties: contiguity order

| state | variable | order 1 | order 2 | order 3 | beyond order 3 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | households | 0.023 [-0.004, 0.050] (n=208) | 0.027 [0.008, 0.047] (n=339) | 0.016 [-0.002, 0.035] (n=404) | 0.023 [0.009, 0.038] (n=2452) |
| Michigan | poverty_count | -0.007 [-0.031, 0.016] (n=208) | 0.011 [-0.006, 0.028] (n=339) | 0.008 [-0.005, 0.022] (n=404) | 0.003 [-0.005, 0.010] (n=2452) |
| Michigan | poverty_rate | -0.007 [-0.030, 0.016] (n=208) | 0.012 [-0.005, 0.028] (n=339) | 0.008 [-0.005, 0.022] (n=404) | 0.003 [-0.005, 0.010] (n=2452) |
| Michigan | owner_share | -0.001 [-0.023, 0.021] (n=208) | 0.016 [-0.003, 0.036] (n=339) | 0.005 [-0.010, 0.021] (n=404) | 0.006 [-0.002, 0.014] (n=2452) |
| Michigan | median_hh_income | 0.005 [-0.016, 0.026] (n=208) | 0.008 [-0.008, 0.024] (n=339) | 0.003 [-0.012, 0.018] (n=404) | 0.001 [-0.008, 0.009] (n=2452) |
| New York | households | 0.015 [-0.017, 0.047] (n=142) | 0.005 [-0.023, 0.032] (n=231) | 0.028 [0.008, 0.049] (n=252) | 0.031 [0.013, 0.050] (n=1266) |
| New York | poverty_count | 0.014 [-0.014, 0.043] (n=142) | 0.001 [-0.019, 0.021] (n=231) | -0.001 [-0.021, 0.019] (n=252) | 0.007 [-0.001, 0.016] (n=1266) |
| New York | poverty_rate | 0.016 [-0.013, 0.044] (n=142) | 0.001 [-0.019, 0.020] (n=231) | -0.002 [-0.022, 0.019] (n=252) | 0.007 [-0.001, 0.016] (n=1266) |
| New York | owner_share | 0.028 [-0.007, 0.063] (n=142) | 0.025 [0.002, 0.047] (n=231) | 0.018 [-0.008, 0.043] (n=252) | 0.036 [0.018, 0.055] (n=1266) |
| New York | median_hh_income | 0.024 [-0.003, 0.052] (n=142) | 0.006 [-0.019, 0.030] (n=231) | 0.004 [-0.019, 0.028] (n=252) | 0.011 [-0.001, 0.022] (n=1266) |

### Tracts: within vs across county (weighting controls)

| state | variable | order 1 / within county | order 1 / across county | order 2 / within county | order 2 / across county | beyond order 3 / within county | beyond order 3 / across county |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.075 [-0.103, -0.046] (n=7608) | -0.005 [-0.014, 0.004] (n=1053) | -0.019 [-0.026, -0.013] (n=14988) | 0.001 [-0.003, 0.005] (n=5182) | -0.001 [-0.001, 0.000] (n=146947) | -0.000 [-0.001, 0.001] (n=89022) |
| Michigan | households | 0.028 [0.020, 0.035] (n=7563) | 0.001 [-0.012, 0.014] (n=1051) | 0.008 [0.001, 0.016] (n=14828) | 0.001 [-0.003, 0.005] (n=5172) | -0.001 [-0.003, 0.002] (n=146625) | 0.000 [-0.001, 0.001] (n=87912) |
| Michigan | poverty_count | -0.005 [-0.008, -0.002] (n=7549) | 0.001 [-0.007, 0.010] (n=1052) | -0.001 [-0.003, 0.001] (n=14793) | -0.003 [-0.008, 0.001] (n=5164) | 0.000 [-0.000, 0.001] (n=146388) | -0.001 [-0.002, 0.000] (n=87790) |
| Michigan | poverty_rate | -0.000 [-0.003, 0.002] (n=7531) | 0.001 [-0.007, 0.009] (n=1050) | 0.000 [-0.002, 0.003] (n=14758) | -0.003 [-0.007, 0.002] (n=5158) | 0.001 [0.000, 0.001] (n=146203) | -0.001 [-0.002, 0.000] (n=87660) |
| Michigan | owner_share | -0.001 [-0.004, 0.002] (n=7458) | -0.005 [-0.015, 0.005] (n=1045) | -0.000 [-0.005, 0.004] (n=14587) | -0.002 [-0.006, 0.003] (n=5143) | 0.000 [-0.000, 0.001] (n=144184) | -0.000 [-0.001, 0.001] (n=86967) |
| Michigan | median_hh_income | 0.001 [-0.001, 0.004] (n=7512) | 0.005 [-0.006, 0.016] (n=1051) | -0.000 [-0.004, 0.004] (n=14702) | -0.000 [-0.004, 0.004] (n=5167) | 0.001 [-0.001, 0.003] (n=145491) | 0.001 [-0.001, 0.002] (n=87445) |
| New York | population | -0.042 [-0.065, -0.018] (n=13624) | 0.000 [-0.008, 0.009] (n=1005) | -0.010 [-0.016, -0.004] (n=29760) | -0.000 [-0.004, 0.004] (n=5543) | -0.002 [-0.002, -0.001] (n=145264) | 0.000 [-0.001, 0.001] (n=90832) |
| New York | households | 0.013 [0.003, 0.022] (n=13506) | -0.004 [-0.013, 0.006] (n=996) | 0.007 [0.004, 0.009] (n=29494) | -0.003 [-0.010, 0.003] (n=5454) | -0.001 [-0.002, 0.000] (n=144341) | 0.001 [-0.001, 0.003] (n=89869) |
| New York | poverty_count | -0.003 [-0.005, -0.001] (n=13441) | 0.004 [-0.006, 0.013] (n=990) | -0.000 [-0.002, 0.001] (n=29280) | 0.000 [-0.004, 0.004] (n=5430) | -0.000 [-0.001, 0.001] (n=143413) | 0.000 [-0.001, 0.001] (n=89197) |
| New York | poverty_rate | -0.001 [-0.003, 0.001] (n=13388) | 0.003 [-0.008, 0.013] (n=990) | -0.000 [-0.001, 0.001] (n=29185) | 0.000 [-0.003, 0.004] (n=5428) | 0.000 [-0.001, 0.001] (n=143103) | 0.000 [-0.001, 0.001] (n=89020) |
| New York | owner_share | 0.000 [-0.002, 0.002] (n=13062) | 0.001 [-0.009, 0.011] (n=971) | 0.000 [-0.001, 0.002] (n=28439) | 0.002 [-0.003, 0.006] (n=5284) | 0.001 [-0.000, 0.001] (n=138644) | 0.000 [-0.001, 0.002] (n=86906) |
| New York | median_hh_income | 0.001 [-0.001, 0.003] (n=13070) | 0.009 [-0.003, 0.021] (n=978) | 0.000 [-0.001, 0.002] (n=28295) | 0.000 [-0.004, 0.005] (n=5358) | 0.000 [-0.001, 0.001] (n=140550) | 0.000 [-0.001, 0.001] (n=86834) |

### Rook (shared edge) vs queen-only (corner) first-order neighbours

Tracts:

| state | variable | order 1 rook (shared edge) | order 1 queen-only (corner) |
| :--- | :--- | :--- | :--- |
| Michigan | population | -0.075 [-0.099, -0.051] (n=7076) | -0.028 [-0.035, -0.020] (n=1585) |
| Michigan | households | 0.025 [0.017, 0.034] (n=7033) | 0.021 [0.009, 0.033] (n=1581) |
| Michigan | poverty_count | -0.005 [-0.008, -0.001] (n=7018) | -0.002 [-0.008, 0.004] (n=1583) |
| Michigan | poverty_rate | -0.000 [-0.003, 0.003] (n=7001) | 0.001 [-0.006, 0.007] (n=1580) |
| Michigan | owner_share | -0.001 [-0.004, 0.002] (n=6938) | -0.005 [-0.010, 0.001] (n=1565) |
| Michigan | median_hh_income | 0.001 [-0.002, 0.004] (n=6986) | 0.004 [-0.003, 0.010] (n=1577) |
| New York | population | -0.043 [-0.065, -0.021] (n=12901) | -0.009 [-0.018, 0.001] (n=1728) |
| New York | households | 0.013 [0.003, 0.022] (n=12783) | 0.004 [-0.004, 0.012] (n=1719) |
| New York | poverty_count | -0.003 [-0.005, -0.000] (n=12720) | -0.005 [-0.010, 0.001] (n=1711) |
| New York | poverty_rate | -0.000 [-0.002, 0.002] (n=12672) | -0.004 [-0.010, 0.002] (n=1706) |
| New York | owner_share | -0.001 [-0.003, 0.002] (n=12368) | 0.008 [0.000, 0.015] (n=1665) |
| New York | median_hh_income | 0.002 [-0.001, 0.004] (n=12379) | 0.001 [-0.008, 0.009] (n=1669) |

Counties:

| state | variable | order 1 rook (shared edge) | order 1 queen-only (corner) |
| :--- | :--- | :--- | :--- |
| Michigan | households | 0.019 [-0.012, 0.050] (n=180) | 0.049 [0.003, 0.095] (n=28) |
| Michigan | poverty_count | -0.005 [-0.031, 0.020] (n=180) | -0.020 [-0.074, 0.033] (n=28) |
| Michigan | poverty_rate | -0.005 [-0.030, 0.021] (n=180) | -0.020 [-0.073, 0.032] (n=28) |
| Michigan | owner_share | 0.007 [-0.016, 0.029] (n=180) | -0.051 [-0.098, -0.004] (n=28) |
| Michigan | median_hh_income | 0.002 [-0.021, 0.025] (n=180) | 0.025 [-0.025, 0.075] (n=28) |
| New York | households | 0.015 [-0.017, 0.048] (n=140) | -0.013 [-0.213, 0.187] (n=2) |
| New York | poverty_count | 0.015 [-0.013, 0.044] (n=140) | -0.049 [-0.640, 0.541] (n=2) |
| New York | poverty_rate | 0.017 [-0.012, 0.045] (n=140) | -0.053 [-0.688, 0.582] (n=2) |
| New York | owner_share | 0.027 [-0.008, 0.063] (n=140) | 0.071 [-0.123, 0.264] (n=2) |
| New York | median_hh_income | 0.023 [-0.004, 0.050] (n=140) | 0.135 [-0.144, 0.414] (n=2) |

### By centroid distance

Tracts:

| state | variable | 0-2 km | 2-5 km | 5-10 km | 10-20 km | 20-50 km | 50-100 km | 100-inf km |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | -0.039 [-0.078, 0.001] (n=3830) | -0.021 [-0.050, 0.008] (n=19110) | -0.008 [-0.019, 0.003] (n=35824) | -0.002 [-0.005, 0.001] (n=69777) | -0.001 [-0.004, 0.002] (n=91586) | -0.000 [-0.003, 0.002] (n=20557) | -0.000 [-0.001, 0.001] (n=58399) |
| Michigan | households | 0.032 [0.006, 0.058] (n=3786) | 0.014 [0.005, 0.023] (n=18947) | 0.003 [0.001, 0.006] (n=35548) | -0.002 [-0.005, 0.002] (n=69531) | -0.001 [-0.003, 0.000] (n=91378) | -0.001 [-0.004, 0.002] (n=20313) | 0.001 [-0.001, 0.002] (n=57622) |
| Michigan | poverty_count | -0.003 [-0.010, 0.004] (n=3789) | -0.002 [-0.005, 0.001] (n=18945) | -0.001 [-0.002, -0.000] (n=35516) | 0.000 [-0.001, 0.001] (n=69418) | 0.000 [-0.000, 0.001] (n=91157) | -0.000 [-0.003, 0.002] (n=20216) | -0.000 [-0.002, 0.001] (n=57606) |
| Michigan | poverty_rate | 0.001 [-0.006, 0.008] (n=3778) | -0.001 [-0.002, 0.001] (n=18895) | -0.001 [-0.002, 0.000] (n=35416) | 0.001 [-0.000, 0.001] (n=69298) | 0.000 [-0.000, 0.001] (n=91079) | -0.001 [-0.003, 0.002] (n=20196) | -0.000 [-0.002, 0.001] (n=57545) |
| Michigan | owner_share | -0.001 [-0.006, 0.004] (n=3726) | -0.000 [-0.005, 0.005] (n=18628) | 0.001 [-0.001, 0.003] (n=34906) | 0.000 [-0.001, 0.001] (n=68456) | 0.000 [-0.000, 0.001] (n=89982) | 0.001 [-0.002, 0.004] (n=20075) | -0.001 [-0.002, 0.000] (n=57125) |
| Michigan | median_hh_income | 0.001 [-0.003, 0.004] (n=3753) | 0.000 [-0.004, 0.004] (n=18798) | 0.001 [-0.001, 0.002] (n=35254) | 0.002 [-0.000, 0.003] (n=69047) | 0.001 [-0.000, 0.002] (n=90750) | 0.001 [-0.002, 0.004] (n=20151) | 0.001 [-0.000, 0.002] (n=57381) |
| New York | population | -0.005 [-0.013, 0.003] (n=41793) | -0.008 [-0.015, -0.001] (n=60659) | -0.004 [-0.008, 0.000] (n=77159) | -0.002 [-0.005, -0.000] (n=58451) | -0.002 [-0.004, -0.000] (n=39904) | -0.001 [-0.003, 0.001] (n=14162) | -0.000 [-0.002, 0.002] (n=55123) |
| New York | households | 0.006 [-0.001, 0.013] (n=41584) | 0.005 [0.000, 0.009] (n=60046) | 0.000 [-0.002, 0.003] (n=76635) | -0.002 [-0.003, -0.000] (n=57971) | -0.001 [-0.002, 0.001] (n=39492) | 0.000 [-0.004, 0.004] (n=13996) | 0.001 [-0.001, 0.003] (n=54523) |
| New York | poverty_count | -0.001 [-0.002, 0.000] (n=41371) | -0.001 [-0.002, 0.000] (n=59777) | -0.000 [-0.001, 0.001] (n=76082) | -0.001 [-0.002, 0.000] (n=57471) | -0.000 [-0.001, 0.001] (n=39165) | 0.000 [-0.004, 0.005] (n=13940) | 0.001 [-0.000, 0.002] (n=54107) |
| New York | poverty_rate | -0.001 [-0.002, 0.000] (n=41258) | 0.000 [-0.001, 0.001] (n=59610) | 0.000 [-0.001, 0.001] (n=75907) | -0.001 [-0.002, 0.001] (n=57343) | 0.000 [-0.001, 0.001] (n=39104) | 0.000 [-0.005, 0.005] (n=13925) | 0.001 [-0.000, 0.002] (n=54002) |
| New York | owner_share | 0.001 [-0.001, 0.002] (n=39501) | 0.001 [-0.001, 0.002] (n=57517) | 0.000 [-0.001, 0.001] (n=73645) | 0.000 [-0.001, 0.002] (n=55901) | 0.001 [-0.001, 0.002] (n=38467) | -0.001 [-0.003, 0.002] (n=13691) | 0.000 [-0.001, 0.002] (n=53014) |
| New York | median_hh_income | 0.001 [-0.000, 0.003] (n=40628) | 0.001 [-0.000, 0.001] (n=58252) | 0.000 [-0.001, 0.001] (n=74411) | -0.000 [-0.001, 0.001] (n=55926) | 0.001 [-0.001, 0.002] (n=37726) | -0.001 [-0.004, 0.002] (n=13563) | 0.000 [-0.001, 0.002] (n=53070) |

Counties:

| state | variable | 0-50 km | 50-100 km | 100-200 km | 200-400 km | 400-inf km |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | households | 0.018 [-0.015, 0.051] (n=139) | 0.026 [0.008, 0.045] (n=375) | 0.024 [0.007, 0.040] (n=1022) | 0.015 [0.000, 0.029] (n=1309) | 0.040 [0.013, 0.066] (n=558) |
| Michigan | poverty_count | -0.000 [-0.030, 0.029] (n=139) | 0.002 [-0.016, 0.019] (n=375) | 0.010 [0.000, 0.020] (n=1022) | 0.001 [-0.009, 0.012] (n=1309) | -0.001 [-0.018, 0.016] (n=558) |
| Michigan | poverty_rate | 0.000 [-0.029, 0.030] (n=139) | 0.002 [-0.015, 0.020] (n=375) | 0.010 [0.000, 0.020] (n=1022) | 0.001 [-0.009, 0.012] (n=1309) | -0.001 [-0.018, 0.015] (n=558) |
| Michigan | owner_share | 0.008 [-0.018, 0.034] (n=139) | 0.011 [-0.007, 0.028] (n=375) | 0.010 [0.000, 0.020] (n=1022) | 0.006 [-0.004, 0.016] (n=1309) | -0.001 [-0.017, 0.015] (n=558) |
| Michigan | median_hh_income | -0.003 [-0.029, 0.024] (n=139) | 0.008 [-0.007, 0.023] (n=375) | 0.008 [-0.004, 0.019] (n=1022) | 0.001 [-0.010, 0.011] (n=1309) | -0.009 [-0.026, 0.009] (n=558) |
| New York | households | 0.034 [-0.022, 0.089] (n=91) | 0.026 [-0.004, 0.056] (n=235) | 0.017 [-0.001, 0.036] (n=537) | 0.027 [0.007, 0.046] (n=867) | 0.052 [0.010, 0.093] (n=161) |
| New York | poverty_count | 0.002 [-0.033, 0.037] (n=91) | 0.007 [-0.009, 0.023] (n=235) | 0.005 [-0.009, 0.019] (n=537) | 0.005 [-0.005, 0.015] (n=867) | 0.015 [-0.009, 0.040] (n=161) |
| New York | poverty_rate | 0.003 [-0.033, 0.038] (n=91) | 0.007 [-0.009, 0.023] (n=235) | 0.005 [-0.008, 0.019] (n=537) | 0.005 [-0.005, 0.015] (n=867) | 0.014 [-0.011, 0.040] (n=161) |
| New York | owner_share | 0.046 [-0.006, 0.099] (n=91) | 0.033 [0.003, 0.063] (n=235) | 0.023 [0.003, 0.043] (n=537) | 0.031 [0.013, 0.050] (n=867) | 0.054 [0.013, 0.095] (n=161) |
| New York | median_hh_income | 0.020 [-0.011, 0.051] (n=91) | 0.026 [0.006, 0.046] (n=235) | 0.012 [-0.007, 0.030] (n=537) | 0.007 [-0.006, 0.020] (n=867) | -0.004 [-0.030, 0.023] (n=161) |

### Aggregation ratio: Var(county sum of tracts) / sum of tract variances

| state | variable | counties | tracts | pooled ratio (SE) | median county ratio | implied mean within-county r (SE) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | population | 83 | 2754 | 0.000 (0.000) | 0.000 | -0.0046 (0.0000) |
| Michigan | households | 82 | 2737 | 0.902 (0.053) | 0.798 | -0.0006 (0.0003) |
| Michigan | poverty_count | 83 | 2736 | 0.886 (0.043) | 0.939 | -0.0006 (0.0002) |
| New York | population | 62 | 4854 | 0.000 (0.000) | 0.000 | -0.0032 (0.0000) |
| New York | households | 62 | 4828 | 0.997 (0.056) | 0.806 | -0.0000 (0.0002) |
| New York | poverty_count | 62 | 4811 | 0.944 (0.041) | 0.893 | -0.0002 (0.0002) |

### Noise floor and spread

Analytic null (80 replicates): SD 0.112, 95% |r| 0.219.

Michigan x New York pairs (independent samples):

| geography | variable | pairs | mean r (SE) | SD of r | 95% |r| | share |r| > analytic critical |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| tract | population | 50000 | -0.0006 (0.0006) | 0.115 | 0.224 | 0.056 |
| tract | households | 50000 | 0.0000 (0.0004) | 0.116 | 0.228 | 0.060 |
| tract | poverty_count | 50000 | 0.0000 (0.0004) | 0.116 | 0.234 | 0.064 |
| tract | poverty_rate | 50000 | -0.0001 (0.0005) | 0.116 | 0.232 | 0.063 |
| tract | owner_share | 50000 | 0.0001 (0.0005) | 0.115 | 0.224 | 0.056 |
| tract | median_hh_income | 50000 | 0.0004 (0.0004) | 0.115 | 0.223 | 0.056 |
| county | households | 5146 | 0.0285 (0.0028) | 0.120 | 0.240 | 0.078 |
| county | poverty_count | 5146 | 0.0055 (0.0020) | 0.116 | 0.228 | 0.061 |
| county | poverty_rate | 5146 | 0.0055 (0.0020) | 0.116 | 0.229 | 0.063 |
| county | owner_share | 5146 | 0.0184 (0.0027) | 0.115 | 0.228 | 0.060 |
| county | median_hh_income | 5146 | 0.0074 (0.0022) | 0.117 | 0.231 | 0.064 |

Derangement null on far pairs (same state, beyond order 3):

| state | geography | variable | pairs | mean | SD | 95% |r| |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | 235969 | 0.0000 | 0.112 | 0.219 |
| Michigan | tract | households | 234537 | 0.0004 | 0.113 | 0.222 |
| Michigan | tract | poverty_count | 234178 | 0.0002 | 0.112 | 0.219 |
| Michigan | tract | poverty_rate | 233863 | 0.0003 | 0.112 | 0.219 |
| Michigan | tract | owner_share | 231151 | 0.0004 | 0.112 | 0.218 |
| Michigan | tract | median_hh_income | 232936 | 0.0007 | 0.112 | 0.219 |
| New York | tract | population | 236096 | -0.0001 | 0.112 | 0.219 |
| New York | tract | households | 234210 | 0.0004 | 0.113 | 0.221 |
| New York | tract | poverty_count | 232610 | 0.0001 | 0.112 | 0.219 |
| New York | tract | poverty_rate | 232123 | 0.0001 | 0.112 | 0.219 |
| New York | tract | owner_share | 225550 | 0.0009 | 0.112 | 0.219 |
| New York | tract | median_hh_income | 227384 | 0.0008 | 0.112 | 0.219 |
| Michigan | county | households | 2452 | 0.0265 | 0.118 | 0.236 |
| Michigan | county | poverty_count | 2452 | 0.0023 | 0.112 | 0.218 |
| Michigan | county | poverty_rate | 2452 | 0.0024 | 0.112 | 0.218 |
| Michigan | county | owner_share | 2452 | 0.0074 | 0.112 | 0.221 |
| Michigan | county | median_hh_income | 2452 | 0.0016 | 0.112 | 0.219 |
| New York | county | households | 1266 | 0.0328 | 0.120 | 0.242 |
| New York | county | poverty_count | 1266 | 0.0103 | 0.111 | 0.216 |
| New York | county | poverty_rate | 1266 | 0.0103 | 0.111 | 0.216 |
| New York | county | owner_share | 1266 | 0.0384 | 0.117 | 0.244 |
| New York | county | median_hh_income | 1266 | 0.0150 | 0.114 | 0.221 |

Replicate design spectrum (lowest eigenvalues relative to the mean; Marchenko-Pastur lower edge for independent replicates in brackets):

| state | geography | variable | lowest 4 eigenvalues (MP edge) | near-null | effective replicates | null SD 1/sqrt(eff.) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | 0.042 0.050 0.077 0.432 (0.688) | 3 | 77 | 0.114 |
| Michigan | tract | households | 0.052 0.054 0.078 0.400 (0.687) | 3 | 77 | 0.114 |
| Michigan | tract | poverty_count | 0.025 0.025 0.052 0.410 (0.687) | 3 | 77 | 0.114 |
| Michigan | tract | poverty_rate | 0.026 0.026 0.054 0.409 (0.687) | 3 | 77 | 0.114 |
| Michigan | tract | owner_share | 0.027 0.031 0.064 0.402 (0.687) | 3 | 77 | 0.114 |
| Michigan | tract | median_hh_income | 0.063 0.066 0.089 0.404 (0.687) | 3 | 77 | 0.114 |
| New York | tract | population | 0.047 0.054 0.086 0.421 (0.760) | 3 | 77 | 0.114 |
| New York | tract | households | 0.043 0.045 0.072 0.414 (0.759) | 3 | 77 | 0.114 |
| New York | tract | poverty_count | 0.024 0.025 0.052 0.421 (0.759) | 3 | 77 | 0.114 |
| New York | tract | poverty_rate | 0.025 0.027 0.053 0.418 (0.759) | 3 | 77 | 0.114 |
| New York | tract | owner_share | 0.028 0.029 0.062 0.410 (0.757) | 3 | 77 | 0.114 |
| New York | tract | median_hh_income | 0.062 0.069 0.089 0.409 (0.757) | 3 | 77 | 0.114 |
| Michigan | county | households | 0.001 0.001 0.001 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | poverty_count | 0.000 0.001 0.002 0.003 (-) | not tested (too few areas) | - | - |
| Michigan | county | poverty_rate | 0.001 0.001 0.002 0.004 (-) | not tested (too few areas) | - | - |
| Michigan | county | owner_share | 0.001 0.001 0.002 0.005 (-) | not tested (too few areas) | - | - |
| Michigan | county | median_hh_income | 0.001 0.002 0.002 0.003 (-) | not tested (too few areas) | - | - |
| New York | county | households | 0.019 0.025 0.038 0.046 (-) | not tested (too few areas) | - | - |
| New York | county | poverty_count | 0.019 0.027 0.029 0.035 (-) | not tested (too few areas) | - | - |
| New York | county | poverty_rate | 0.019 0.026 0.033 0.036 (-) | not tested (too few areas) | - | - |
| New York | county | owner_share | 0.011 0.020 0.028 0.038 (-) | not tested (too few areas) | - | - |
| New York | county | median_hh_income | 0.022 0.027 0.037 0.052 (-) | not tested (too few areas) | - | - |

Spread of pairwise r per class: SD, excess SD over the analytic null (sqrt(SD^2 - 1/80)) and over the empirical Michigan x New York null (sqrt(SD^2 - SD_cross^2)), and the share of pairs with |r| above the analytic 5% critical value (0.05 expected under no correlation; about 0.06 in the independent cross-state pairs):

| state | geography | variable | class | SD of r | excess SD (analytic) | excess SD (cross-state) | share |r| > crit |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | order 1 | 0.185 | 0.148 | 0.145 | 0.163 |
| Michigan | tract | population | order 2 | 0.126 | 0.059 | 0.053 | 0.076 |
| Michigan | tract | population | order 3 | 0.119 | 0.040 | 0.031 | 0.063 |
| Michigan | tract | population | beyond order 3 | 0.115 | 0.028 | 0.012 | 0.058 |
| Michigan | tract | households | order 1 | 0.140 | 0.085 | 0.079 | 0.115 |
| Michigan | tract | households | order 2 | 0.124 | 0.054 | 0.043 | 0.076 |
| Michigan | tract | households | order 3 | 0.119 | 0.041 | 0.025 | 0.066 |
| Michigan | tract | households | beyond order 3 | 0.117 | 0.034 | 0.010 | 0.061 |
| Michigan | tract | poverty_count | order 1 | 0.114 | 0.024 | 0.000 | 0.061 |
| Michigan | tract | poverty_count | order 2 | 0.116 | 0.030 | 0.000 | 0.064 |
| Michigan | tract | poverty_count | order 3 | 0.115 | 0.029 | 0.000 | 0.064 |
| Michigan | tract | poverty_count | beyond order 3 | 0.116 | 0.031 | 0.008 | 0.064 |
| Michigan | tract | poverty_rate | order 1 | 0.114 | 0.023 | 0.000 | 0.060 |
| Michigan | tract | poverty_rate | order 2 | 0.116 | 0.030 | 0.000 | 0.062 |
| Michigan | tract | poverty_rate | order 3 | 0.116 | 0.030 | 0.000 | 0.062 |
| Michigan | tract | poverty_rate | beyond order 3 | 0.116 | 0.030 | 0.006 | 0.063 |
| Michigan | tract | owner_share | order 1 | 0.115 | 0.027 | 0.010 | 0.058 |
| Michigan | tract | owner_share | order 2 | 0.115 | 0.025 | 0.000 | 0.058 |
| Michigan | tract | owner_share | order 3 | 0.115 | 0.028 | 0.013 | 0.059 |
| Michigan | tract | owner_share | beyond order 3 | 0.115 | 0.029 | 0.014 | 0.059 |
| Michigan | tract | median_hh_income | order 1 | 0.113 | 0.019 | 0.000 | 0.055 |
| Michigan | tract | median_hh_income | order 2 | 0.116 | 0.032 | 0.019 | 0.060 |
| Michigan | tract | median_hh_income | order 3 | 0.116 | 0.029 | 0.015 | 0.058 |
| Michigan | tract | median_hh_income | beyond order 3 | 0.115 | 0.027 | 0.009 | 0.057 |
| New York | tract | population | order 1 | 0.173 | 0.133 | 0.130 | 0.122 |
| New York | tract | population | order 3 | 0.118 | 0.038 | 0.028 | 0.060 |
| New York | tract | population | order 2 | 0.124 | 0.053 | 0.046 | 0.069 |
| New York | tract | population | beyond order 3 | 0.115 | 0.029 | 0.013 | 0.058 |
| New York | tract | households | order 1 | 0.131 | 0.067 | 0.059 | 0.090 |
| New York | tract | households | order 3 | 0.118 | 0.038 | 0.020 | 0.065 |
| New York | tract | households | order 2 | 0.120 | 0.044 | 0.029 | 0.069 |
| New York | tract | households | beyond order 3 | 0.117 | 0.033 | 0.006 | 0.061 |
| New York | tract | poverty_count | order 1 | 0.115 | 0.028 | 0.000 | 0.063 |
| New York | tract | poverty_count | order 3 | 0.115 | 0.027 | 0.000 | 0.062 |
| New York | tract | poverty_count | order 2 | 0.116 | 0.029 | 0.000 | 0.064 |
| New York | tract | poverty_count | beyond order 3 | 0.115 | 0.029 | 0.000 | 0.064 |
| New York | tract | poverty_rate | order 1 | 0.115 | 0.027 | 0.000 | 0.066 |
| New York | tract | poverty_rate | order 3 | 0.115 | 0.027 | 0.000 | 0.062 |
| New York | tract | poverty_rate | order 2 | 0.115 | 0.029 | 0.000 | 0.062 |
| New York | tract | poverty_rate | beyond order 3 | 0.115 | 0.029 | 0.000 | 0.063 |
| New York | tract | owner_share | order 1 | 0.115 | 0.025 | 0.003 | 0.059 |
| New York | tract | owner_share | order 3 | 0.116 | 0.029 | 0.014 | 0.059 |
| New York | tract | owner_share | order 2 | 0.115 | 0.028 | 0.012 | 0.059 |
| New York | tract | owner_share | beyond order 3 | 0.115 | 0.029 | 0.014 | 0.059 |
| New York | tract | median_hh_income | order 1 | 0.115 | 0.029 | 0.014 | 0.059 |
| New York | tract | median_hh_income | order 3 | 0.115 | 0.028 | 0.011 | 0.057 |
| New York | tract | median_hh_income | order 2 | 0.115 | 0.027 | 0.008 | 0.057 |
| New York | tract | median_hh_income | beyond order 3 | 0.115 | 0.027 | 0.009 | 0.058 |
| Michigan | county | households | order 1 | 0.124 | 0.054 | 0.032 | 0.096 |
| Michigan | county | households | order 3 | 0.115 | 0.027 | 0.000 | 0.064 |
| Michigan | county | households | order 2 | 0.117 | 0.033 | 0.000 | 0.062 |
| Michigan | county | households | beyond order 3 | 0.123 | 0.052 | 0.028 | 0.076 |
| Michigan | county | poverty_count | order 1 | 0.115 | 0.027 | 0.000 | 0.043 |
| Michigan | county | poverty_count | order 3 | 0.107 | 0.000 | 0.000 | 0.045 |
| Michigan | county | poverty_count | order 2 | 0.113 | 0.014 | 0.000 | 0.053 |
| Michigan | county | poverty_count | beyond order 3 | 0.116 | 0.032 | 0.010 | 0.058 |
| Michigan | county | poverty_rate | order 1 | 0.114 | 0.024 | 0.000 | 0.043 |
| Michigan | county | poverty_rate | order 3 | 0.108 | 0.000 | 0.000 | 0.052 |
| Michigan | county | poverty_rate | order 2 | 0.112 | 0.009 | 0.000 | 0.056 |
| Michigan | county | poverty_rate | beyond order 3 | 0.116 | 0.031 | 0.006 | 0.059 |
| Michigan | county | owner_share | order 1 | 0.113 | 0.013 | 0.000 | 0.043 |
| Michigan | county | owner_share | order 3 | 0.109 | 0.000 | 0.000 | 0.040 |
| Michigan | county | owner_share | order 2 | 0.109 | 0.000 | 0.000 | 0.041 |
| Michigan | county | owner_share | beyond order 3 | 0.115 | 0.028 | 0.009 | 0.057 |
| Michigan | county | median_hh_income | order 1 | 0.115 | 0.026 | 0.000 | 0.053 |
| Michigan | county | median_hh_income | order 3 | 0.114 | 0.020 | 0.000 | 0.050 |
| Michigan | county | median_hh_income | order 2 | 0.109 | 0.000 | 0.000 | 0.032 |
| Michigan | county | median_hh_income | beyond order 3 | 0.117 | 0.035 | 0.011 | 0.062 |
| New York | county | households | order 3 | 0.111 | 0.000 | 0.000 | 0.071 |
| New York | county | households | order 1 | 0.122 | 0.048 | 0.020 | 0.063 |
| New York | county | households | order 2 | 0.128 | 0.062 | 0.044 | 0.091 |
| New York | county | households | beyond order 3 | 0.124 | 0.054 | 0.031 | 0.084 |
| New York | county | poverty_count | order 3 | 0.112 | 0.000 | 0.000 | 0.060 |
| New York | county | poverty_count | order 1 | 0.120 | 0.043 | 0.030 | 0.049 |
| New York | county | poverty_count | order 2 | 0.112 | 0.000 | 0.000 | 0.039 |
| New York | county | poverty_count | beyond order 3 | 0.113 | 0.018 | 0.000 | 0.050 |
| New York | county | poverty_rate | order 3 | 0.111 | 0.000 | 0.000 | 0.060 |
| New York | county | poverty_rate | order 1 | 0.120 | 0.043 | 0.030 | 0.056 |
| New York | county | poverty_rate | order 2 | 0.112 | 0.009 | 0.000 | 0.043 |
| New York | county | poverty_rate | beyond order 3 | 0.113 | 0.018 | 0.000 | 0.053 |
| New York | county | owner_share | order 3 | 0.128 | 0.062 | 0.056 | 0.087 |
| New York | county | owner_share | order 1 | 0.120 | 0.044 | 0.035 | 0.056 |
| New York | county | owner_share | order 2 | 0.116 | 0.030 | 0.013 | 0.061 |
| New York | county | owner_share | beyond order 3 | 0.119 | 0.041 | 0.031 | 0.078 |
| New York | county | median_hh_income | order 3 | 0.109 | 0.000 | 0.000 | 0.032 |
| New York | county | median_hh_income | order 1 | 0.116 | 0.029 | 0.000 | 0.056 |
| New York | county | median_hh_income | order 2 | 0.111 | 0.000 | 0.000 | 0.056 |
| New York | county | median_hh_income | beyond order 3 | 0.117 | 0.035 | 0.010 | 0.051 |

### Split-half reliability of individual pairwise correlations

Per class: half-A and half-B class means (random split), and the reliability of a single pair's r (correlation across pairs between halves; stepped up to 80 replicates), for each split.

| state | geography | variable | class | pairs | mean (half A) | mean (half B) | reliability (random / odd-even / first-last) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | order 1 | 8661 | -0.067 | -0.064 | 0.64 / 0.65 / 0.64 |
| Michigan | tract | population | order 2 | 20170 | -0.013 | -0.015 | 0.21 / 0.22 / 0.22 |
| Michigan | tract | population | order 3 | 34283 | -0.005 | -0.004 | 0.12 / 0.11 / 0.10 |
| Michigan | tract | population | beyond order 3 | 235969 | -0.001 | -0.000 | 0.06 / 0.06 / 0.03 |
| Michigan | tract | households | order 1 | 8614 | 0.022 | 0.027 | 0.37 / 0.37 / 0.35 |
| Michigan | tract | households | order 2 | 20000 | 0.005 | 0.007 | 0.19 / 0.19 / 0.20 |
| Michigan | tract | households | order 3 | 33974 | 0.001 | 0.002 | 0.11 / 0.12 / 0.13 |
| Michigan | tract | households | beyond order 3 | 234537 | -0.000 | -0.000 | 0.08 / 0.09 / 0.07 |
| Michigan | tract | poverty_count | order 1 | 8601 | -0.007 | -0.001 | 0.03 / 0.05 / -0.04 |
| Michigan | tract | poverty_count | order 2 | 19957 | -0.002 | -0.001 | 0.08 / 0.06 / 0.01 |
| Michigan | tract | poverty_count | order 3 | 33911 | -0.001 | 0.000 | 0.07 / 0.05 / -0.01 |
| Michigan | tract | poverty_count | beyond order 3 | 234178 | -0.000 | 0.000 | 0.07 / 0.06 / 0.02 |
| Michigan | tract | poverty_rate | order 1 | 8581 | -0.003 | 0.003 | 0.02 / 0.04 / -0.05 |
| Michigan | tract | poverty_rate | order 2 | 19916 | -0.000 | -0.000 | 0.07 / 0.07 / 0.01 |
| Michigan | tract | poverty_rate | order 3 | 33847 | -0.001 | 0.001 | 0.07 / 0.05 / 0.00 |
| Michigan | tract | poverty_rate | beyond order 3 | 233863 | 0.000 | 0.000 | 0.07 / 0.06 / 0.01 |
| Michigan | tract | owner_share | order 1 | 8503 | -0.000 | -0.002 | 0.04 / 0.05 / 0.02 |
| Michigan | tract | owner_share | order 2 | 19730 | -0.001 | -0.001 | 0.06 / 0.05 / -0.02 |
| Michigan | tract | owner_share | order 3 | 33514 | 0.001 | 0.001 | 0.06 / 0.06 / -0.00 |
| Michigan | tract | owner_share | beyond order 3 | 231151 | 0.000 | 0.000 | 0.06 / 0.06 / 0.01 |
| Michigan | tract | median_hh_income | order 1 | 8563 | 0.000 | 0.003 | 0.02 / 0.03 / 0.02 |
| Michigan | tract | median_hh_income | order 2 | 19869 | -0.000 | 0.000 | 0.08 / 0.08 / 0.04 |
| Michigan | tract | median_hh_income | order 3 | 33766 | 0.001 | 0.000 | 0.06 / 0.06 / 0.03 |
| Michigan | tract | median_hh_income | beyond order 3 | 232936 | 0.001 | 0.001 | 0.05 / 0.06 / 0.03 |
| New York | tract | population | order 1 | 14629 | -0.038 | -0.039 | 0.60 / 0.60 / 0.59 |
| New York | tract | population | order 3 | 61223 | -0.002 | -0.002 | 0.09 / 0.10 / 0.08 |
| New York | tract | population | order 2 | 35303 | -0.008 | -0.008 | 0.19 / 0.19 / 0.15 |
| New York | tract | population | beyond order 3 | 236096 | -0.001 | -0.001 | 0.07 / 0.06 / 0.02 |
| New York | tract | households | order 1 | 14502 | 0.011 | 0.012 | 0.28 / 0.27 / 0.28 |
| New York | tract | households | order 3 | 60587 | 0.002 | 0.003 | 0.11 / 0.12 / 0.10 |
| New York | tract | households | order 2 | 34948 | 0.005 | 0.005 | 0.13 / 0.12 / 0.12 |
| New York | tract | households | beyond order 3 | 234210 | -0.000 | -0.000 | 0.09 / 0.08 / 0.07 |
| New York | tract | poverty_count | order 1 | 14431 | -0.003 | -0.003 | 0.06 / 0.08 / 0.01 |
| New York | tract | poverty_count | order 3 | 60162 | 0.000 | -0.002 | 0.06 / 0.05 / -0.01 |
| New York | tract | poverty_count | order 2 | 34710 | 0.000 | -0.001 | 0.06 / 0.05 / 0.01 |
| New York | tract | poverty_count | beyond order 3 | 232610 | 0.000 | -0.000 | 0.07 / 0.06 / 0.01 |
| New York | tract | poverty_rate | order 1 | 14378 | -0.001 | -0.000 | 0.06 / 0.07 / 0.01 |
| New York | tract | poverty_rate | order 3 | 60035 | 0.000 | -0.001 | 0.06 / 0.05 / -0.01 |
| New York | tract | poverty_rate | order 2 | 34613 | 0.000 | -0.000 | 0.06 / 0.05 / -0.00 |
| New York | tract | poverty_rate | beyond order 3 | 232123 | 0.000 | -0.000 | 0.07 / 0.06 / 0.01 |
| New York | tract | owner_share | order 1 | 14033 | 0.001 | -0.000 | 0.05 / 0.05 / -0.02 |
| New York | tract | owner_share | order 3 | 58430 | 0.000 | 0.001 | 0.06 / 0.06 / 0.02 |
| New York | tract | owner_share | order 2 | 33723 | 0.000 | 0.000 | 0.04 / 0.07 / 0.01 |
| New York | tract | owner_share | beyond order 3 | 225550 | 0.001 | 0.000 | 0.06 / 0.05 / 0.02 |
| New York | tract | median_hh_income | order 1 | 14048 | 0.002 | 0.001 | 0.05 / 0.07 / 0.05 |
| New York | tract | median_hh_income | order 3 | 58491 | 0.000 | 0.001 | 0.06 / 0.05 / 0.03 |
| New York | tract | median_hh_income | order 2 | 33653 | 0.001 | 0.000 | 0.06 / 0.07 / 0.01 |
| New York | tract | median_hh_income | beyond order 3 | 227384 | -0.000 | 0.000 | 0.06 / 0.05 / 0.02 |
| Michigan | county | households | order 1 | 208 | 0.014 | 0.033 | 0.17 / 0.19 / 0.30 |
| Michigan | county | households | order 3 | 404 | 0.025 | 0.008 | 0.15 / 0.16 / 0.06 |
| Michigan | county | households | order 2 | 339 | 0.031 | 0.025 | 0.19 / 0.05 / 0.10 |
| Michigan | county | households | beyond order 3 | 2452 | 0.023 | 0.023 | 0.20 / 0.15 / 0.19 |
| Michigan | county | poverty_count | order 1 | 208 | -0.002 | -0.010 | -0.06 / 0.11 / 0.12 |
| Michigan | county | poverty_count | order 3 | 404 | 0.008 | 0.007 | -0.37 / -0.04 / -0.05 |
| Michigan | county | poverty_count | order 2 | 339 | 0.009 | 0.013 | 0.02 / -0.10 / -0.06 |
| Michigan | county | poverty_count | beyond order 3 | 2452 | -0.001 | 0.005 | 0.07 / 0.09 / 0.04 |
| Michigan | county | poverty_rate | order 1 | 208 | -0.002 | -0.008 | -0.06 / 0.11 / 0.10 |
| Michigan | county | poverty_rate | order 3 | 404 | 0.007 | 0.008 | -0.37 / -0.02 / -0.05 |
| Michigan | county | poverty_rate | order 2 | 339 | 0.008 | 0.014 | 0.02 / -0.09 / -0.07 |
| Michigan | county | poverty_rate | beyond order 3 | 2452 | -0.001 | 0.005 | 0.06 / 0.08 / 0.03 |
| Michigan | county | owner_share | order 1 | 208 | -0.009 | 0.007 | -0.09 / 0.01 / -0.18 |
| Michigan | county | owner_share | order 3 | 404 | 0.004 | 0.007 | 0.01 / -0.14 / 0.01 |
| Michigan | county | owner_share | order 2 | 339 | 0.019 | 0.014 | -0.04 / -0.07 / -0.05 |
| Michigan | county | owner_share | beyond order 3 | 2452 | 0.002 | 0.012 | 0.06 / 0.09 / 0.03 |
| Michigan | county | median_hh_income | order 1 | 208 | -0.006 | 0.014 | -0.03 / -0.07 / 0.15 |
| Michigan | county | median_hh_income | order 3 | 404 | 0.019 | -0.012 | -0.07 / 0.07 / -0.07 |
| Michigan | county | median_hh_income | order 2 | 339 | 0.004 | 0.013 | -0.14 / 0.02 / 0.10 |
| Michigan | county | median_hh_income | beyond order 3 | 2452 | -0.003 | 0.004 | 0.12 / 0.07 / 0.09 |
| New York | county | households | order 3 | 252 | 0.016 | 0.039 | 0.04 / 0.03 / 0.05 |
| New York | county | households | order 1 | 142 | 0.012 | 0.015 | 0.04 / 0.15 / 0.12 |
| New York | county | households | order 2 | 231 | -0.009 | 0.019 | 0.13 / 0.30 / 0.23 |
| New York | county | households | beyond order 3 | 1266 | 0.025 | 0.037 | 0.19 / 0.21 / 0.28 |
| New York | county | poverty_count | order 3 | 252 | -0.007 | 0.005 | -0.05 / -0.05 / 0.12 |
| New York | county | poverty_count | order 1 | 142 | 0.025 | 0.005 | 0.06 / 0.18 / 0.02 |
| New York | county | poverty_count | order 2 | 231 | 0.004 | -0.001 | -0.11 / 0.06 / -0.24 |
| New York | county | poverty_count | beyond order 3 | 1266 | 0.007 | 0.007 | 0.08 / 0.06 / -0.01 |
| New York | county | poverty_rate | order 3 | 252 | -0.008 | 0.004 | -0.05 / -0.05 / 0.10 |
| New York | county | poverty_rate | order 1 | 142 | 0.025 | 0.008 | 0.07 / 0.20 / 0.02 |
| New York | county | poverty_rate | order 2 | 231 | 0.004 | -0.002 | -0.09 / 0.07 / -0.24 |
| New York | county | poverty_rate | beyond order 3 | 1266 | 0.007 | 0.007 | 0.08 / 0.06 / -0.01 |
| New York | county | owner_share | order 3 | 252 | 0.025 | 0.010 | 0.17 / 0.30 / 0.22 |
| New York | county | owner_share | order 1 | 142 | 0.041 | 0.012 | 0.13 / 0.09 / 0.10 |
| New York | county | owner_share | order 2 | 231 | 0.019 | 0.031 | 0.20 / 0.04 / -0.07 |
| New York | county | owner_share | beyond order 3 | 1266 | 0.043 | 0.029 | 0.14 / 0.13 / 0.19 |
| New York | county | median_hh_income | order 3 | 252 | -0.001 | 0.010 | -0.06 / -0.07 / -0.15 |
| New York | county | median_hh_income | order 1 | 142 | 0.018 | 0.031 | 0.01 / 0.17 / 0.10 |
| New York | county | median_hh_income | order 2 | 231 | 0.009 | 0.002 | 0.00 / -0.02 / 0.00 |
| New York | county | median_hh_income | beyond order 3 | 1266 | 0.010 | 0.010 | 0.10 / 0.04 / 0.11 |

## Stability across releases (2015-2019 vs 2020-2024)

Mean r per class in each release (different tract vintages: 2010 tracts in 2015-2019, 2020 tracts in 2020-2024; so pair sets differ). Difference with a combined SE.

| state | geography | variable | class | 2019 | 2024 | difference (SE) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Michigan | tract | population | order 1 | -0.066 [-0.088, -0.044] | -0.071 [-0.090, -0.052] | -0.004 (0.015) |
| Michigan | tract | population | order 2 | -0.014 [-0.017, -0.011] | -0.014 [-0.017, -0.011] | 0.000 (0.002) |
| Michigan | tract | population | order 3 | -0.004 [-0.005, -0.003] | -0.004 [-0.005, -0.003] | 0.000 (0.001) |
| Michigan | tract | population | beyond order 3 | -0.000 [-0.001, -0.000] | -0.001 [-0.001, 0.000] | -0.000 (0.000) |
| Michigan | tract | population | order 1 / within county | -0.075 [-0.103, -0.046] | -0.081 [-0.106, -0.056] | -0.006 (0.019) |
| Michigan | tract | population | order 2 / within county | -0.019 [-0.026, -0.013] | -0.018 [-0.024, -0.012] | 0.001 (0.005) |
| Michigan | tract | population | order 2 / across county | 0.001 [-0.003, 0.005] | -0.001 [-0.005, 0.002] | -0.002 (0.003) |
| Michigan | tract | population | order 1 / across county | -0.005 [-0.014, 0.004] | 0.003 [-0.007, 0.014] | 0.008 (0.007) |
| Michigan | tract | population | order 3 / across county | -0.000 [-0.003, 0.002] | 0.001 [-0.001, 0.003] | 0.001 (0.001) |
| Michigan | tract | population | order 3 / within county | -0.007 [-0.009, -0.004] | -0.007 [-0.009, -0.006] | -0.001 (0.001) |
| Michigan | tract | population | beyond order 3 / within county | -0.001 [-0.001, 0.000] | -0.001 [-0.001, -0.000] | -0.000 (0.000) |
| Michigan | tract | population | beyond order 3 / across county | -0.000 [-0.001, 0.001] | 0.000 [-0.001, 0.001] | 0.000 (0.001) |
| Michigan | tract | households | order 1 | 0.025 [0.016, 0.033] | -0.025 [-0.033, -0.017] | -0.050 (0.006) |
| Michigan | tract | households | order 2 | 0.006 [0.000, 0.013] | -0.008 [-0.011, -0.004] | -0.014 (0.004) |
| Michigan | tract | households | order 3 | 0.001 [-0.002, 0.005] | -0.003 [-0.005, -0.002] | -0.005 (0.002) |
| Michigan | tract | households | beyond order 3 | -0.000 [-0.001, 0.001] | -0.001 [-0.001, 0.000] | -0.000 (0.001) |
| Michigan | tract | households | order 1 / within county | 0.028 [0.020, 0.035] | -0.028 [-0.039, -0.018] | -0.056 (0.006) |
| Michigan | tract | households | order 2 / within county | 0.008 [0.001, 0.016] | -0.009 [-0.015, -0.004] | -0.018 (0.005) |
| Michigan | tract | households | order 2 / across county | 0.001 [-0.003, 0.005] | -0.002 [-0.006, 0.002] | -0.003 (0.003) |
| Michigan | tract | households | order 1 / across county | 0.001 [-0.012, 0.014] | -0.003 [-0.012, 0.006] | -0.004 (0.008) |
| Michigan | tract | households | order 3 / across county | 0.001 [-0.002, 0.004] | 0.000 [-0.002, 0.003] | -0.000 (0.002) |
| Michigan | tract | households | order 3 / within county | 0.002 [-0.003, 0.007] | -0.006 [-0.009, -0.003] | -0.008 (0.003) |
| Michigan | tract | households | beyond order 3 / within county | -0.001 [-0.003, 0.002] | -0.001 [-0.003, -0.000] | -0.001 (0.001) |
| Michigan | tract | households | beyond order 3 / across county | 0.000 [-0.001, 0.001] | 0.001 [-0.001, 0.002] | 0.001 (0.001) |
| Michigan | tract | poverty_count | order 1 | -0.004 [-0.007, -0.001] | -0.004 [-0.007, -0.002] | 0.000 (0.002) |
| Michigan | tract | poverty_count | order 2 | -0.002 [-0.004, 0.000] | -0.003 [-0.005, -0.002] | -0.002 (0.001) |
| Michigan | tract | poverty_count | order 3 | 0.000 [-0.002, 0.002] | -0.000 [-0.002, 0.001] | -0.000 (0.001) |
| Michigan | tract | poverty_count | beyond order 3 | -0.000 [-0.000, 0.000] | 0.000 [-0.001, 0.002] | 0.000 (0.001) |
| Michigan | tract | poverty_count | order 1 / within county | -0.005 [-0.008, -0.002] | -0.005 [-0.007, -0.002] | 0.000 (0.002) |
| Michigan | tract | poverty_count | order 2 / within county | -0.001 [-0.003, 0.001] | -0.005 [-0.006, -0.003] | -0.003 (0.001) |
| Michigan | tract | poverty_count | order 2 / across county | -0.003 [-0.008, 0.001] | 0.000 [-0.003, 0.003] | 0.003 (0.003) |
| Michigan | tract | poverty_count | order 1 / across county | 0.001 [-0.007, 0.010] | 0.001 [-0.008, 0.011] | 0.000 (0.007) |
| Michigan | tract | poverty_count | order 3 / across county | 0.001 [-0.002, 0.003] | 0.001 [-0.002, 0.003] | -0.000 (0.002) |
| Michigan | tract | poverty_count | order 3 / within county | -0.000 [-0.002, 0.001] | -0.001 [-0.003, 0.001] | -0.001 (0.001) |
| Michigan | tract | poverty_count | beyond order 3 / within county | 0.000 [-0.000, 0.001] | 0.000 [-0.001, 0.002] | 0.000 (0.001) |
| Michigan | tract | poverty_count | beyond order 3 / across county | -0.001 [-0.002, 0.000] | -0.000 [-0.002, 0.001] | 0.000 (0.001) |
| Michigan | tract | poverty_rate | order 1 | -0.000 [-0.003, 0.003] | -0.000 [-0.003, 0.002] | -0.000 (0.002) |
| Michigan | tract | poverty_rate | order 2 | -0.001 [-0.003, 0.001] | -0.002 [-0.004, -0.001] | -0.002 (0.001) |
| Michigan | tract | poverty_rate | order 3 | 0.000 [-0.002, 0.002] | 0.000 [-0.002, 0.002] | -0.000 (0.001) |
| Michigan | tract | poverty_rate | beyond order 3 | 0.000 [-0.001, 0.001] | 0.000 [-0.001, 0.001] | 0.000 (0.001) |
| Michigan | tract | poverty_rate | order 1 / within county | -0.000 [-0.003, 0.002] | -0.001 [-0.003, 0.002] | -0.000 (0.002) |
| Michigan | tract | poverty_rate | order 2 / within county | 0.000 [-0.002, 0.003] | -0.003 [-0.005, -0.002] | -0.003 (0.002) |
| Michigan | tract | poverty_rate | order 2 / across county | -0.003 [-0.007, 0.002] | 0.001 [-0.003, 0.004] | 0.003 (0.003) |
| Michigan | tract | poverty_rate | order 1 / across county | 0.001 [-0.007, 0.009] | 0.002 [-0.007, 0.012] | 0.001 (0.006) |
| Michigan | tract | poverty_rate | order 3 / across county | 0.000 [-0.002, 0.003] | 0.000 [-0.002, 0.003] | 0.000 (0.002) |
| Michigan | tract | poverty_rate | order 3 / within county | 0.000 [-0.002, 0.002] | -0.000 [-0.003, 0.002] | -0.000 (0.002) |
| Michigan | tract | poverty_rate | beyond order 3 / within county | 0.001 [0.000, 0.001] | 0.001 [-0.001, 0.002] | 0.000 (0.001) |
| Michigan | tract | poverty_rate | beyond order 3 / across county | -0.001 [-0.002, 0.000] | -0.000 [-0.002, 0.001] | 0.000 (0.001) |
| Michigan | tract | owner_share | order 1 | -0.001 [-0.004, 0.001] | -0.002 [-0.005, 0.000] | -0.001 (0.002) |
| Michigan | tract | owner_share | order 2 | -0.001 [-0.004, 0.002] | 0.000 [-0.001, 0.002] | 0.001 (0.002) |
| Michigan | tract | owner_share | order 3 | 0.001 [-0.000, 0.002] | -0.001 [-0.002, 0.001] | -0.002 (0.001) |
| Michigan | tract | owner_share | beyond order 3 | 0.000 [-0.001, 0.001] | 0.000 [-0.000, 0.001] | 0.000 (0.000) |
| Michigan | tract | owner_share | order 1 / within county | -0.001 [-0.004, 0.002] | -0.002 [-0.005, -0.000] | -0.002 (0.002) |
| Michigan | tract | owner_share | order 2 / within county | -0.000 [-0.005, 0.004] | 0.001 [-0.001, 0.003] | 0.001 (0.002) |
| Michigan | tract | owner_share | order 2 / across county | -0.002 [-0.006, 0.003] | -0.001 [-0.006, 0.003] | 0.001 (0.003) |
| Michigan | tract | owner_share | order 1 / across county | -0.005 [-0.015, 0.005] | 0.000 [-0.010, 0.011] | 0.006 (0.007) |
| Michigan | tract | owner_share | order 3 / across county | 0.000 [-0.003, 0.004] | -0.000 [-0.003, 0.003] | -0.000 (0.002) |
| Michigan | tract | owner_share | order 3 / within county | 0.002 [0.000, 0.003] | -0.001 [-0.003, 0.001] | -0.003 (0.001) |
| Michigan | tract | owner_share | beyond order 3 / within county | 0.000 [-0.000, 0.001] | 0.000 [-0.000, 0.001] | -0.000 (0.001) |
| Michigan | tract | owner_share | beyond order 3 / across county | -0.000 [-0.001, 0.001] | 0.000 [-0.001, 0.001] | 0.001 (0.001) |
| Michigan | tract | median_hh_income | order 1 | 0.002 [-0.001, 0.004] | 0.000 [-0.002, 0.003] | -0.001 (0.002) |
| Michigan | tract | median_hh_income | order 2 | -0.000 [-0.003, 0.003] | 0.000 [-0.002, 0.003] | 0.001 (0.002) |
| Michigan | tract | median_hh_income | order 3 | 0.001 [-0.001, 0.002] | 0.001 [-0.000, 0.002] | 0.001 (0.001) |
| Michigan | tract | median_hh_income | beyond order 3 | 0.001 [-0.000, 0.002] | 0.001 [0.000, 0.002] | -0.000 (0.001) |
| Michigan | tract | median_hh_income | order 1 / within county | 0.001 [-0.001, 0.004] | 0.000 [-0.002, 0.003] | -0.001 (0.002) |
| Michigan | tract | median_hh_income | order 2 / within county | -0.000 [-0.004, 0.004] | -0.000 [-0.002, 0.002] | 0.000 (0.002) |
| Michigan | tract | median_hh_income | order 2 / across county | -0.000 [-0.004, 0.004] | 0.002 [-0.002, 0.006] | 0.002 (0.003) |
| Michigan | tract | median_hh_income | order 1 / across county | 0.005 [-0.006, 0.016] | -0.001 [-0.011, 0.009] | -0.006 (0.008) |
| Michigan | tract | median_hh_income | order 3 / across county | 0.001 [-0.002, 0.004] | 0.001 [-0.001, 0.004] | 0.000 (0.002) |
| Michigan | tract | median_hh_income | order 3 / within county | 0.000 [-0.001, 0.002] | 0.001 [-0.000, 0.002] | 0.001 (0.001) |
| Michigan | tract | median_hh_income | beyond order 3 / within county | 0.001 [-0.001, 0.003] | 0.001 [0.001, 0.002] | 0.000 (0.001) |
| Michigan | tract | median_hh_income | beyond order 3 / across county | 0.001 [-0.001, 0.002] | 0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | population | order 2 | -0.008 [-0.013, -0.003] | -0.009 [-0.013, -0.004] | -0.001 (0.003) |
| New York | tract | population | order 1 | -0.039 [-0.060, -0.018] | -0.040 [-0.060, -0.020] | -0.001 (0.015) |
| New York | tract | population | order 3 | -0.002 [-0.004, -0.001] | -0.003 [-0.004, -0.002] | -0.001 (0.001) |
| New York | tract | population | beyond order 3 | -0.001 [-0.002, -0.000] | -0.001 [-0.002, -0.001] | -0.000 (0.000) |
| New York | tract | population | order 2 / within county | -0.010 [-0.016, -0.004] | -0.010 [-0.016, -0.005] | -0.001 (0.004) |
| New York | tract | population | order 1 / within county | -0.042 [-0.065, -0.018] | -0.043 [-0.065, -0.020] | -0.001 (0.017) |
| New York | tract | population | order 3 / within county | -0.003 [-0.005, -0.001] | -0.004 [-0.006, -0.003] | -0.001 (0.001) |
| New York | tract | population | order 3 / across county | 0.001 [-0.002, 0.003] | -0.001 [-0.003, 0.002] | -0.001 (0.002) |
| New York | tract | population | order 2 / across county | -0.000 [-0.004, 0.004] | 0.001 [-0.003, 0.005] | 0.001 (0.003) |
| New York | tract | population | order 1 / across county | 0.000 [-0.008, 0.009] | 0.002 [-0.006, 0.009] | 0.001 (0.006) |
| New York | tract | population | beyond order 3 / within county | -0.002 [-0.002, -0.001] | -0.001 [-0.002, -0.001] | 0.000 (0.000) |
| New York | tract | population | beyond order 3 / across county | 0.000 [-0.001, 0.001] | -0.001 [-0.002, 0.000] | -0.001 (0.001) |
| New York | tract | households | order 2 | 0.005 [0.002, 0.008] | -0.003 [-0.006, -0.001] | -0.008 (0.002) |
| New York | tract | households | order 1 | 0.012 [0.003, 0.020] | -0.015 [-0.022, -0.008] | -0.027 (0.006) |
| New York | tract | households | order 3 | 0.002 [0.001, 0.004] | -0.002 [-0.003, -0.001] | -0.005 (0.001) |
| New York | tract | households | beyond order 3 | -0.000 [-0.001, 0.001] | -0.001 [-0.001, -0.000] | -0.001 (0.001) |
| New York | tract | households | order 2 / within county | 0.007 [0.004, 0.009] | -0.004 [-0.008, -0.001] | -0.011 (0.002) |
| New York | tract | households | order 1 / within county | 0.013 [0.003, 0.022] | -0.016 [-0.024, -0.008] | -0.029 (0.006) |
| New York | tract | households | order 3 / within county | 0.004 [0.002, 0.006] | -0.003 [-0.004, -0.002] | -0.007 (0.001) |
| New York | tract | households | order 3 / across county | -0.001 [-0.004, 0.002] | -0.000 [-0.003, 0.002] | 0.001 (0.002) |
| New York | tract | households | order 2 / across county | -0.003 [-0.010, 0.003] | 0.002 [-0.002, 0.006] | 0.005 (0.004) |
| New York | tract | households | order 1 / across county | -0.004 [-0.013, 0.006] | -0.000 [-0.011, 0.011] | 0.003 (0.007) |
| New York | tract | households | beyond order 3 / within county | -0.001 [-0.002, 0.000] | -0.001 [-0.002, -0.001] | -0.000 (0.001) |
| New York | tract | households | beyond order 3 / across county | 0.001 [-0.001, 0.003] | 0.000 [-0.001, 0.001] | -0.001 (0.001) |
| New York | tract | poverty_count | order 2 | -0.000 [-0.002, 0.001] | -0.001 [-0.002, 0.001] | -0.000 (0.001) |
| New York | tract | poverty_count | order 1 | -0.003 [-0.005, -0.001] | -0.002 [-0.003, 0.000] | 0.001 (0.001) |
| New York | tract | poverty_count | order 3 | -0.001 [-0.002, 0.000] | 0.000 [-0.001, 0.001] | 0.001 (0.001) |
| New York | tract | poverty_count | beyond order 3 | 0.000 [-0.000, 0.001] | -0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | poverty_count | order 2 / within county | -0.000 [-0.002, 0.001] | -0.000 [-0.002, 0.001] | -0.000 (0.001) |
| New York | tract | poverty_count | order 1 / within county | -0.003 [-0.005, -0.001] | -0.002 [-0.004, -0.000] | 0.001 (0.001) |
| New York | tract | poverty_count | order 3 / within county | -0.001 [-0.002, -0.000] | 0.000 [-0.001, 0.002] | 0.002 (0.001) |
| New York | tract | poverty_count | order 3 / across county | 0.000 [-0.003, 0.003] | -0.001 [-0.004, 0.002] | -0.001 (0.002) |
| New York | tract | poverty_count | order 2 / across county | 0.000 [-0.004, 0.004] | -0.002 [-0.006, 0.002] | -0.002 (0.003) |
| New York | tract | poverty_count | order 1 / across county | 0.004 [-0.006, 0.013] | 0.004 [-0.005, 0.013] | 0.000 (0.007) |
| New York | tract | poverty_count | beyond order 3 / within county | -0.000 [-0.001, 0.001] | -0.000 [-0.002, 0.002] | -0.000 (0.001) |
| New York | tract | poverty_count | beyond order 3 / across county | 0.000 [-0.001, 0.001] | 0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | poverty_rate | order 2 | -0.000 [-0.001, 0.001] | -0.000 [-0.001, 0.001] | 0.000 (0.001) |
| New York | tract | poverty_rate | order 1 | -0.001 [-0.002, 0.001] | -0.000 [-0.002, 0.002] | 0.001 (0.001) |
| New York | tract | poverty_rate | order 3 | -0.001 [-0.002, 0.001] | 0.000 [-0.001, 0.001] | 0.001 (0.001) |
| New York | tract | poverty_rate | beyond order 3 | 0.000 [-0.000, 0.001] | 0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | poverty_rate | order 2 / within county | -0.000 [-0.001, 0.001] | 0.000 [-0.001, 0.001] | 0.000 (0.001) |
| New York | tract | poverty_rate | order 1 / within county | -0.001 [-0.003, 0.001] | -0.000 [-0.002, 0.002] | 0.001 (0.001) |
| New York | tract | poverty_rate | order 3 / within county | -0.001 [-0.002, 0.000] | 0.000 [-0.001, 0.002] | 0.001 (0.001) |
| New York | tract | poverty_rate | order 3 / across county | -0.000 [-0.003, 0.003] | -0.001 [-0.004, 0.002] | -0.001 (0.002) |
| New York | tract | poverty_rate | order 2 / across county | 0.000 [-0.003, 0.004] | -0.002 [-0.006, 0.003] | -0.002 (0.003) |
| New York | tract | poverty_rate | order 1 / across county | 0.003 [-0.008, 0.013] | 0.003 [-0.007, 0.013] | 0.001 (0.007) |
| New York | tract | poverty_rate | beyond order 3 / within county | 0.000 [-0.001, 0.001] | -0.000 [-0.002, 0.002] | -0.000 (0.001) |
| New York | tract | poverty_rate | beyond order 3 / across county | 0.000 [-0.001, 0.001] | 0.000 [-0.000, 0.001] | 0.000 (0.001) |
| New York | tract | owner_share | order 2 | 0.000 [-0.001, 0.002] | 0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | owner_share | order 1 | 0.000 [-0.002, 0.002] | -0.001 [-0.002, 0.001] | -0.001 (0.001) |
| New York | tract | owner_share | order 3 | 0.000 [-0.001, 0.002] | 0.001 [-0.000, 0.002] | 0.000 (0.001) |
| New York | tract | owner_share | beyond order 3 | 0.000 [-0.000, 0.001] | 0.000 [-0.000, 0.001] | 0.000 (0.000) |
| New York | tract | owner_share | order 2 / within county | 0.000 [-0.001, 0.002] | 0.000 [-0.001, 0.001] | -0.000 (0.001) |
| New York | tract | owner_share | order 1 / within county | 0.000 [-0.002, 0.002] | -0.001 [-0.003, 0.001] | -0.001 (0.001) |
| New York | tract | owner_share | order 3 / within county | 0.000 [-0.001, 0.002] | 0.001 [-0.001, 0.002] | 0.000 (0.001) |
| New York | tract | owner_share | order 3 / across county | 0.000 [-0.003, 0.003] | 0.001 [-0.001, 0.004] | 0.001 (0.002) |
| New York | tract | owner_share | order 2 / across county | 0.002 [-0.003, 0.006] | -0.000 [-0.005, 0.004] | -0.002 (0.003) |
| New York | tract | owner_share | order 1 / across county | 0.001 [-0.009, 0.011] | 0.004 [-0.007, 0.015] | 0.003 (0.007) |
| New York | tract | owner_share | beyond order 3 / within county | 0.001 [-0.000, 0.001] | 0.000 [-0.000, 0.001] | -0.000 (0.001) |
| New York | tract | owner_share | beyond order 3 / across county | 0.000 [-0.001, 0.002] | 0.001 [-0.000, 0.001] | 0.000 (0.001) |
| New York | tract | median_hh_income | order 2 | 0.000 [-0.001, 0.002] | 0.001 [-0.000, 0.002] | 0.001 (0.001) |
| New York | tract | median_hh_income | order 1 | 0.002 [-0.001, 0.004] | 0.000 [-0.002, 0.002] | -0.001 (0.002) |
| New York | tract | median_hh_income | order 3 | 0.001 [-0.001, 0.002] | -0.000 [-0.001, 0.001] | -0.001 (0.001) |
| New York | tract | median_hh_income | beyond order 3 | 0.000 [-0.000, 0.001] | 0.001 [0.000, 0.001] | 0.001 (0.000) |
| New York | tract | median_hh_income | order 2 / within county | 0.000 [-0.001, 0.002] | 0.001 [0.000, 0.003] | 0.001 (0.001) |
| New York | tract | median_hh_income | order 1 / within county | 0.001 [-0.001, 0.003] | 0.000 [-0.002, 0.003] | -0.001 (0.002) |
| New York | tract | median_hh_income | order 3 / within county | 0.001 [-0.001, 0.003] | 0.000 [-0.001, 0.002] | -0.000 (0.001) |
| New York | tract | median_hh_income | order 3 / across county | 0.001 [-0.001, 0.004] | -0.001 [-0.005, 0.002] | -0.003 (0.002) |
| New York | tract | median_hh_income | order 2 / across county | 0.000 [-0.004, 0.005] | -0.001 [-0.006, 0.003] | -0.002 (0.003) |
| New York | tract | median_hh_income | order 1 / across county | 0.009 [-0.003, 0.021] | 0.000 [-0.009, 0.010] | -0.008 (0.008) |
| New York | tract | median_hh_income | beyond order 3 / within county | 0.000 [-0.001, 0.001] | 0.001 [0.000, 0.001] | 0.001 (0.000) |
| New York | tract | median_hh_income | beyond order 3 / across county | 0.000 [-0.001, 0.001] | 0.001 [-0.001, 0.002] | 0.001 (0.001) |
| Michigan | county | households | order 1 | 0.023 [-0.004, 0.050] | 0.026 [0.001, 0.051] | 0.003 (0.019) |
| Michigan | county | households | order 3 | 0.016 [-0.002, 0.035] | 0.027 [0.005, 0.049] | 0.011 (0.015) |
| Michigan | county | households | order 2 | 0.027 [0.008, 0.047] | 0.031 [0.009, 0.052] | 0.003 (0.015) |
| Michigan | county | households | beyond order 3 | 0.023 [0.009, 0.038] | 0.033 [0.015, 0.051] | 0.010 (0.012) |
| Michigan | county | poverty_count | order 1 | -0.007 [-0.031, 0.016] | -0.005 [-0.027, 0.017] | 0.002 (0.016) |
| Michigan | county | poverty_count | order 3 | 0.008 [-0.005, 0.022] | 0.005 [-0.014, 0.024] | -0.003 (0.012) |
| Michigan | county | poverty_count | order 2 | 0.011 [-0.006, 0.028] | 0.009 [-0.007, 0.026] | -0.002 (0.012) |
| Michigan | county | poverty_count | beyond order 3 | 0.003 [-0.005, 0.010] | 0.002 [-0.007, 0.010] | -0.001 (0.006) |
| Michigan | county | poverty_rate | order 1 | -0.007 [-0.030, 0.016] | -0.005 [-0.027, 0.017] | 0.002 (0.016) |
| Michigan | county | poverty_rate | order 3 | 0.008 [-0.005, 0.022] | 0.005 [-0.014, 0.024] | -0.003 (0.012) |
| Michigan | county | poverty_rate | order 2 | 0.012 [-0.005, 0.028] | 0.009 [-0.007, 0.026] | -0.002 (0.012) |
| Michigan | county | poverty_rate | beyond order 3 | 0.003 [-0.005, 0.010] | 0.002 [-0.007, 0.010] | -0.001 (0.006) |
| Michigan | county | owner_share | order 1 | -0.001 [-0.023, 0.021] | -0.003 [-0.025, 0.018] | -0.002 (0.016) |
| Michigan | county | owner_share | order 3 | 0.005 [-0.010, 0.021] | 0.022 [0.006, 0.039] | 0.017 (0.012) |
| Michigan | county | owner_share | order 2 | 0.016 [-0.003, 0.036] | 0.018 [-0.001, 0.036] | 0.001 (0.014) |
| Michigan | county | owner_share | beyond order 3 | 0.006 [-0.002, 0.014] | 0.001 [-0.006, 0.009] | -0.005 (0.006) |
| Michigan | county | median_hh_income | order 1 | 0.005 [-0.016, 0.026] | 0.003 [-0.016, 0.022] | -0.002 (0.014) |
| Michigan | county | median_hh_income | order 3 | 0.003 [-0.012, 0.018] | 0.002 [-0.015, 0.020] | -0.001 (0.012) |
| Michigan | county | median_hh_income | order 2 | 0.008 [-0.008, 0.024] | 0.002 [-0.014, 0.018] | -0.006 (0.011) |
| Michigan | county | median_hh_income | beyond order 3 | 0.001 [-0.008, 0.009] | -0.000 [-0.007, 0.007] | -0.001 (0.006) |
| New York | county | households | order 3 | 0.028 [0.008, 0.049] | 0.025 [0.001, 0.050] | -0.003 (0.016) |
| New York | county | households | order 1 | 0.015 [-0.017, 0.047] | 0.021 [-0.005, 0.048] | 0.006 (0.021) |
| New York | county | households | order 2 | 0.005 [-0.023, 0.032] | 0.024 [-0.001, 0.049] | 0.019 (0.019) |
| New York | county | households | beyond order 3 | 0.031 [0.013, 0.050] | 0.031 [0.015, 0.047] | -0.000 (0.012) |
| New York | county | poverty_count | order 3 | -0.001 [-0.021, 0.019] | 0.019 [-0.006, 0.045] | 0.021 (0.017) |
| New York | county | poverty_count | order 1 | 0.014 [-0.014, 0.043] | 0.003 [-0.025, 0.031] | -0.011 (0.020) |
| New York | county | poverty_count | order 2 | 0.001 [-0.019, 0.021] | 0.006 [-0.012, 0.024] | 0.006 (0.014) |
| New York | county | poverty_count | beyond order 3 | 0.007 [-0.001, 0.016] | 0.008 [-0.003, 0.019] | 0.000 (0.007) |
| New York | county | poverty_rate | order 3 | -0.002 [-0.022, 0.019] | 0.019 [-0.006, 0.045] | 0.021 (0.017) |
| New York | county | poverty_rate | order 1 | 0.016 [-0.013, 0.044] | 0.003 [-0.025, 0.031] | -0.012 (0.020) |
| New York | county | poverty_rate | order 2 | 0.001 [-0.019, 0.020] | 0.006 [-0.012, 0.024] | 0.006 (0.014) |
| New York | county | poverty_rate | beyond order 3 | 0.007 [-0.001, 0.016] | 0.008 [-0.003, 0.019] | 0.001 (0.007) |
| New York | county | owner_share | order 3 | 0.018 [-0.008, 0.043] | -0.002 [-0.021, 0.017] | -0.020 (0.016) |
| New York | county | owner_share | order 1 | 0.028 [-0.007, 0.063] | -0.002 [-0.034, 0.030] | -0.030 (0.024) |
| New York | county | owner_share | order 2 | 0.025 [0.002, 0.047] | 0.010 [-0.015, 0.035] | -0.015 (0.017) |
| New York | county | owner_share | beyond order 3 | 0.036 [0.018, 0.055] | 0.012 [0.002, 0.022] | -0.024 (0.011) |
| New York | county | median_hh_income | order 3 | 0.004 [-0.019, 0.028] | 0.004 [-0.015, 0.024] | 0.000 (0.015) |
| New York | county | median_hh_income | order 1 | 0.024 [-0.003, 0.052] | 0.006 [-0.019, 0.031] | -0.018 (0.019) |
| New York | county | median_hh_income | order 2 | 0.006 [-0.019, 0.030] | -0.008 [-0.031, 0.015] | -0.013 (0.017) |
| New York | county | median_hh_income | beyond order 3 | 0.011 [-0.001, 0.022] | 0.012 [0.002, 0.021] | 0.001 (0.008) |

## Findings

Numbers below refer to the tables in this report (2020-2024 release unless
stated). r is the replicate correlation of two areas' sampling errors.

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
   far more negative than the mean (a few tracts sharing a county's controlled total). These are
   2020-2024 numbers; in 2015-2019 population behaves the same but households do not (6).
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
6. **Stability across releases (tracts and counties, 2015-2019 vs 2020-2024).** One 2015-2019 file,
   Michigan tract total population (`2019/data/5-year/140/B01003_26.csv.zip`), is refused by census.gov's
   firewall ('Request Rejected', support ID 13979921158196382640) for every client, while every other
   2019 tract file, including New York's B01003_36 from the same directory, downloads normally. Its only
   line is read instead from line 1 ("Total:") of the same release's Michigan tract B01001 file
   (B01001_26; see Data: substituted files). The two lines are the same total population: in the New York
   2015-2019 tract files B01003 line 1 and B01001 line 1 agree in ESTIMATE, MOE and all 80 replicates for
   all 4,918 tracts (0 differing cells), a check re-run by tests/test_replicate_live.py. All variables are
   therefore analysed in both states and releases. Results: (a) **Uncontrolled variables are stable.**
   Poverty count, poverty rate, owner share and median income stay at 0 to within +/-0.005 at every order
   in both states and releases; of 108 tract class comparisons for them, 3 differ by more than 2 combined
   SEs (about 5 expected by chance; largest |z| 2.5, all differences <= 0.004). (b) **Population is stable
   in both states.** New York: order 1 -0.039 [-0.060, -0.018] in 2015-2019 vs -0.040 [-0.060, -0.020]
   (within county -0.042 vs -0.043), the same fast decay, a county aggregation ratio of 0.000 again (tract
   errors sum to zero within a county), and split-half pair reliability 0.60 (2020-2024: 0.61). Michigan
   (B01001 line 1): order 1 -0.066 [-0.088, -0.044] vs -0.071 [-0.090, -0.052]; within county -0.075
   [-0.103, -0.046] vs -0.081 [-0.106, -0.056]; across county -0.005 [-0.014, 0.004] vs +0.003 [-0.007,
   0.014]; order 2 within county -0.019 vs -0.018; rook vs corner-only -0.075 vs -0.028 (2020-2024: -0.080
   vs -0.029); aggregation ratio 0.000 in both releases; split-half pair reliability at order 1 0.64
   (2020-2024: 0.67). Every Michigan population class difference between releases is at most 0.008
   (order 1 across county, 1.1 combined SEs). As in 2020-2024, Michigan's within-county order-1 correlation is nearly twice New York's. (c)
   **Households change sign.** Occupied housing units (B25003_001) in 2015-2019 have *positive* mean r
   between neighbouring tracts in the same county - Michigan order 1 +0.025 [0.016, 0.033] (within county
   +0.028), New York +0.012 [0.003, 0.020] - against -0.025 and -0.015 in 2020-2024 (differences 8.4 and
   4.7 combined SEs), still zero across county lines (+0.001 / -0.004) and beyond order 3. Their
   aggregation ratio is 0.90 (Michigan) and 1.00 (New York) instead of 0.22 / 0.26: in 2015-2019 the tract
   household errors are not constrained to (nearly) cancel within a county. The pair reliability (0.37 /
   0.28) shows the pattern is real, not replicate noise. The study cannot say why from the replicates
   alone; a change between the releases in how occupied-unit estimates relate to the weighting controls
   is the obvious candidate, not verified here. (d) County class means agree within about 1.5 combined
   SEs for every variable and order except New York owner share beyond order 3 (+0.036 vs +0.012,
   difference -0.024, SE 0.011) - the county-wide offset in (4), not a neighbour effect.


## C2 conclusion

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
  same county (order 1: -0.08 to -0.04 for population, -0.03 to -0.015 for households in 2020-2024;
  population equally in 2015-2019, households there +0.01 to +0.03 instead, see Findings 6), and summarised
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
  -0.01) for controlled counts in tracts; the only positive neighbour correlations beyond the design-wide
  county offset of about +0.03 are 2015-2019 tract households, +0.01 to +0.03 (95% bounds up to +0.033)
  within county. The release matters for controlled counts. Grid values of +0.1..0.5 are therefore
  stress tests outside the observed range, not plausible ACS conditions for these variables; the
  realistic end of the grid is -0.1..0.05. Correlated-error scenarios with large positive rho should be
  stated as hypothetical (for example for variables or survey designs not studied here).
* Limits: two states, two releases at tract level (2015-2019 Michigan population read from B01001 line
  1, verified identical to B01003 line 1; Findings 6), 80 replicates (about 77 effective), and counties as a proxy for weighting
  areas. The 95% |r| of 0.23 for a single pair means no individual pair can be
  flagged; only class means are evidence.


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
