# Round 16: Validating Spatial Methods Under ACS Sampling Error

**Date:** 2026-11-08
**Status:** Approved by the owner/planner with qualifications (2026-11-08); implementation in progress
**Baseline:** v0.14.0 (spreg 1.9.1, esda 2.10.0, libpysal 4.15.1)
**Companions:** [PLAN_ROUND_15.md](PLAN_ROUND_15.md) (spatial coverage studies), [docs/remote_access_decisions.md](docs/remote_access_decisions.md) (remote access follows this round)

## 0. Principle and findings carried in

**Validation first:** understand each failure, test competing explanations, and only then change an estimator. New estimators or substantial methods come in only when the experiments **and** accessible methodological sources support them. Otherwise findings are documented and an implementation round is proposed.

| ID | Finding (Round 15) | Correct reading |
| :--- | :--- | :--- |
| F1 | Error-model λ coverage 0.16-0.58 under outcome sampling error | λ is spatial autocorrelation of the **disturbances**, not a substantive covariate spillover. The failure is **attenuation** of λ by measurement error in y (no-sampling-error reference covers 0.88-0.90). Earlier summaries calling λ a "spillover term" were imprecise |
| F2 | Lag model reports β and ρ only | β is not the marginal effect in a lag model. There is no direct/indirect/total effects output, so users can misread β |
| F3 | LISA false clusters in 38% of maps ("patterned" scenario) | That scenario has **spatially heterogeneous but independent** sampling errors (CVs sorted along a gradient). It is **not** evidence about spatially **correlated** sampling errors, which are untested |

**Two different false-cluster situations**, kept distinct throughout:

| Scenario | Definition |
| :--- | :--- |
| Heterogeneous variances | Sampling variances vary across space, errors independent |
| Correlated errors | Sampling errors covary between areas (survey design, weighting controls) |

A method that handles one may fail the other. FDR control does not repair p-values computed under a wrong null.

## 1. Workstream A: spatial regression (P0)

**A1. λ attenuation (error model).**
- Establish, from the original source, the assumptions under which a measurement-error correction for λ is valid (candidate: Lorincz-Comi et al. 2021, *Spatial Statistics* 45:100523; others if accessible). In particular, does it need only the error variances, or their covariance?
- Compare uncorrected and corrected estimators under known DGPs: bias, RMSE, interval coverage, numerical stability (non-convergence, boundary estimates).
- Sensitivity to the weights matrix (rook, queen, kNN) and to measurement-error magnitude (CV profiles).
- Run under independent and under spatially correlated errors separately. A correction validated under independence is not assumed valid under correlation.
- Implement the correction in the tool only if it validates and its assumptions are verified from an accessible source; otherwise document and propose.

**A2. Effects for the lag model (approved).**
- First inspect spreg 1.9.1 for established impacts functionality (spatial multiplier, direct/indirect/total effects, inference) and use it if available.
- Verify the reported effects against independently computed impacts from S(W) = (I - ρW)^-1 β: the direct effect is the mean of the diagonal, the total effect is the mean row sum, and indirect = total - direct.
- Report the effects, with uncertainty, in the tool output and results. Make it impossible to mistake β for the marginal effect: the label and notes say β is not the marginal effect, and the effects table comes first.
- Measure effects coverage in the simulation.

**A3. DGP set (spatial Durbin model approved as a DGP only).**
- Simulate:
  - SEM (no substantive spillovers);
  - SAR lag with known effects;
  - SDM, y = ρWy + Xβ + WXθ + ε, with known effects;
  - an omitted spatially correlated covariate.
- Fit the tool's models to each. Report parameter bias and coverage, and recovery of direct/indirect/total effects where they are defined.
- Separate the recovery of disturbance parameters from the recovery of substantive effects.
- **No production SDM estimator in this round.**

**A4. Common-factor test (SEM as SDM with θ = -ρβ).**
- Evaluate a likelihood-ratio test only where both models can be fitted reliably. Confirm:
  - nesting and the restriction;
  - comparable likelihoods (same data, same Jacobian treatment);
  - finite-sample size and power.
- Asymptotic Wald tests are known to be parameterization-dependent (Political Analysis 2020), so use LR, with simulation or bootstrap critical values if the LR size is off.
- Add the test to the tool only if it earns its place; no checklist additions.

## 2. Workstream B: LISA under sampling error (P0)

**B1. Five scenarios** (approved): no true clustering with
1. independent homogeneous errors;
2. heterogeneous independent errors;
3. spatially correlated errors;

plus true clusters with
4. independent errors;
5. correlated errors.

Correlation structures in scenarios 3 and 5 are parameterized, with ranges informed by C1 and stated as assumptions.

**B2. Four candidate methods**, all on the same simulated maps:
1. conventional LISA (current default);
2. `replicate_quadrant_share` (a stability diagnostic, not a calibrated probability);
3. LISA on spatial Fay-Herriot EBLUPs. Explicitly test whether the smoothing **induces or alters** spatial patterns (false clusters created by smoothing; true clusters attenuated);
4. a Wong (2021) threshold-first, uncertainty-aware approach, implemented only as far as the open-access paper specifies it.

**B3. Metrics, defined in the plan and the report:**
- **false-positive rate:** per-area share of null areas flagged;
- **FDR:** the expected share of flagged areas that are null;
- **false-cluster rate:** the share of maps with at least one flagged contiguous cluster, of size ≥ k, made only of null areas; k is stated;
- **power:** the share of true-cluster areas flagged;
- **cluster-location accuracy:** for example the Jaccard overlap of the flagged cluster with the true cluster;
- **sensitivity to W.**

Every metric carries a Monte Carlo SE.

Decompose errors attributable to sampling uncertainty from errors attributable to misspecified spatial dependence (for example by comparing the same maps with and without sampling error, and under the correct vs a misspecified W).

A preferred method is never chosen by minimizing false positives alone: power and smoothing bias are weighed.

**B4. Three questions kept separate:**
- stability of a classification across replicate maps;
- statistical significance;
- evidence of a genuine underlying pattern.

## 3. Workstream C: ACS sampling-error covariance from replicates (P0, central question)

**C1. Measure it, and measure how reliably it can be measured.**
- Use the ACS VRE replicates for Michigan and New York tracts (and counties), for several variables of different types (a count, a proportion, a median if replicated).
- Estimate the sampling covariance between estimates with the ACS replicate formula, Cov = (4/80) Σ_r (X_r - X)(Y_r - Y), consistent with the variance scaling.
- Analyse:
  - neighbouring-pair correlations against non-neighbour pairs, by distance or contiguity order, and inside vs across county lines (weighting controls);
  - structured summaries instead of unrestricted matrices, since 80 replicates give a rank-limited covariance across hundreds of tracts: mean or median neighbour correlation, a fitted structured model (for example one correlation parameter per contiguity order), shrinkage or regularization where justified;
  - the stability of those summaries: bootstrap or jackknife over replicates, and split-half agreement;
  - the noise floor: what pairwise correlation magnitude 80 replicates can distinguish from zero.
- Pairwise correlations are never treated as established individually.

**C2. Report** whether the covariance structure is estimable well enough to use in a correction (A1) or a null model (B). If so, that is a methodological contribution to the engine; if not, the limit is documented with evidence. Results feed the B1 correlation parameters as stated assumptions.

## 4. Workstream D: literature notes (P1)

`docs/literature/spatial_methods.md` lists the references the owner's planner provided. Each entry records:
- full citation and DOI/URL;
- **verification level:**
  - (1) verified from full text;
  - (2) supported by abstract or accessible excerpt only;
  - (3) a hypothesis or implementation suggestion still to verify;
- what it is used for, and which experiment tests it.

The planner's summaries are not substitutes for the sources. Where access is restricted, document it, and don't implement methods whose essential assumptions cannot be verified.

References:
- Lacombe & LeSage 2015
- Koley & Bera 2024
- Political Analysis 2020 (common-factor Wald test)
- IJGIS 2018 (ACS spatial autocorrelation critique)
- Wong 2021
- SBC 2019
- Bayesian areal ACS 2021
- Sun et al. 2015
- Arthur 2024
- LeSage & Pace 2014
- Lorincz-Comi et al. 2021
- Rogerson 2024

## 5. Scope

- No unrelated architectural changes; preserve the existing safeguards, reproducibility contracts and working functionality.
- Studies are `info`. Gating is an owner decision after the evidence is in.
- Out of scope this round:
  - hierarchical Bayesian measurement-error models (a later research extension);
  - Arthur-style constrained resampling and spatial FDR procedures (Sun et al.), unless B shows the basic null-model problem is solved first;
  - a production SDM estimator.
- Remote laptop access follows this round.

## 6. Release

v0.15.0 with a Zenodo DOI, after code review, fixes, the full gate, and a test run on both Python 3.10 and 3.14 before the push.
