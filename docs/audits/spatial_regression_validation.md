# Spatial regression under ACS sampling error: validation (PLAN_ROUND_16 workstream A)

**Date:** 2026-11-08. **Status:** findings and proposals; every study row is `info` (gating is an owner decision).
**Code:** `census/simulation_spreg.py` (studies and study-only estimators), `census/spatial.py` (lag-model effects).
**Tests:** `tests/test_spatial_regression_validation.py`.
**Reproduce:** `python scripts/inference_validation.py --studies spatial_error_attenuation spatial_error_attenuation_correlated spatial_dgp_recovery common_factor_lr common_factor_lr_acs --jobs 5 --out /tmp/r16a.md`
(seed 20261102; Python 3.14, spreg 1.9.1; BLAS pinned to one thread; about 28 min wall with 5 processes on a loaded 16-core machine).
Single-process runtimes: `spatial_error_attenuation` 28 min, `spatial_error_attenuation_correlated` 27 min, `spatial_dgp_recovery` 26 min, `common_factor_lr` 27 min, `common_factor_lr_acs` 6.5 min.
All exceed the 5-minute budget, so none is in the default full report; they run only when named.

Principle (PLAN_ROUND_16 sec. 0): validation first. No estimator was added to the tool in this workstream. The tool gained the lag-model effects (A2, owner-approved) and measured caveats.

## Common design

- **Lattice and weights.** 12 x 12 lattice (144 areas), row-standardized W: rook, queen, or kNN with k = 6. kNN uses cell centres with a fixed 1e-6 jitter, so ties break the same way on every machine. The kNN W is asymmetric.
- **Variables.** x ~ N(50, 24^2), beta = 0.5, innovations e ~ N(0, 8^2). These match the Round 15 study `spatial_regression_coverage`.
- **Outcome sampling error.** Independent d_i ~ N(0, s_i^2). s_i = CV_i x mean(y*), with CV_i from the tract profile (median 13.4%; `census.simulation.CV_PROFILES`). "CV x k" scales every CV by k.
- **Correlated sampling error (stated assumption).** d = S L z, where S = diag(s) and L L' = R(gamma). R(gamma) is the correlation matrix of a SAR(gamma) process on the same W. With gamma = 0.3 and 0.6, the mean first-order neighbour error correlation is:
  - rook: 0.17 and 0.38;
  - queen: 0.11 and 0.29;
  - kNN: 0.12 and 0.31.

  This is a hypothesis about the structure, not an estimate of the ACS covariance (workstream C).
- **Inference reported.** Nominal 90% Wald intervals.
- **Monte Carlo SEs.**
  - coverage and rejection rates: sqrt(p(1-p)/R);
  - bias: sd / sqrt(R);
  - RMSE: by the delta method.
- **Seeds.** Every cell has its own seed, cfg.seed + a fixed offset from the cell's position, so results do not depend on run order or process. `tests/test_spatial_regression_validation.py` checks this.

## Verification of the study estimators (offline tests)

| Check | Result |
| :--- | :--- |
| `sem_ml` and `sdm_ml` (eigenvalue-based concentrated ML) vs spreg `ML_Error` and `ML_Lag(slx_lags=1)`, rook/queen/kNN | log-likelihoods equal to 5 decimals; rho and coefficients equal |
| Nesting: SDM log-likelihood at the SEM estimates with theta = -lambda beta (constant (1 - lambda) alpha) | equals the SEM log-likelihood (same data, same full Jacobian, same constants); LR >= 0 |
| `me_sem_fit` (diagonal Sigma, ML) vs `census.sae_spatial` ML | rho, A and beta equal (4 decimals) |
| `me_sem_fit` (diagonal Sigma, REML) vs `spatial_fay_herriot` | rho and rho_se equal (4 decimals) |
| `me_sem_fit` with Sigma = 0 | equals `sem_ml` |
| Lag effects vs independent S(W) = (I - rho W)^-1 algebra, with and without an island | equal to 10 decimals (GMM and ML) |
| Lag effects vs spreg `sp_multipliers['full']` | equal to 6 decimals |
| Delta-method SEs vs numerical gradient | equal (rtol 1e-6) |
| Delta-method SEs vs Monte Carlo draws, n = 400 | within 5% |
| spreg RS additivity (Koley & Bera): RS_rho,gamma = RS_rho + RS*_gamma = RS*_rho + RS_gamma | holds, but by construction: spreg defines the robust tests as these differences |

## A1. Attenuation of lambda in the error model

### Source verification

- **Lorincz-Comi et al. (2021), *Spatial Statistics* 45:100523: not implemented.**
  - The abstract is accessible, but it does not state the lambda correction.
  - The coordinator's full-text reading (docs/literature/full_text_reading_notes.md) shows it is a method-of-moments **covariate** measurement-error correction for GWR. It does not address lambda or SEM/SAR models. Its global analogue is the existing `regression_calibration`.
- **The candidate tested is the engine's existing spatial Fay-Herriot model** (`census.sae_spatial`; Petrucci & Salvati 2006; Molina & Marhuenda 2015, *The R Journal* 7(1), eqs. (1) and (6), read in full; Donegan, Chun & Griffith 2021 per the coordinator's notes):
  - y = X beta + u + e, with u = rho W u + eps (SAR(1));
  - e_d independent N(0, psi_d), psi_d **known**.

  This is algebraically the SEM with classical independent outcome error: V = sigma^2 [(I - rho W)'(I - rho W)]^-1 + diag(SE^2). Its rho is the SEM's lambda corrected for that error.
- **What the correction needs.** Only the sampling **variances**, and it **assumes independence**: the source's model statement is "e_d ind N(0, psi_d)". No accessible source gives a version with error covariances. The study's dense-Sigma variant (V = A C(lambda) + Sigma) is an **oracle** that knows the true Sigma, not a method.

### Design

- **Independent errors** (`spatial_error_attenuation`): W in {rook, queen, kNN}, lambda in {0, 0.3, 0.6}, CV x {0, 0.5, 1, 2}; 300 repetitions per cell. Estimators:
  - the tool default, spreg ML_Error ("naive");
  - spatial Fay-Herriot rho by REML (`spatial_fay_herriot`'s default; SE from the REML information, as its `rho_se`);
  - spatial Fay-Herriot rho by ML.
- **Correlated errors** (`spatial_error_attenuation_correlated`): the same W and lambda, CV x 1, gamma in {0.3, 0.6}, plus the oracle.

### Results: lambda, independent errors (300 reps per cell; MC SE of coverage <= 0.029, of bias <= 0.02)

Coverage of nominal 90% intervals:

| W | lambda | CV x | naive | SFH REML | SFH ML |
| :--- | ---: | ---: | ---: | ---: | ---: |
| rook | 0 | 1 | 0.920 | 0.889 | 0.865 |
| rook | 0.3 | 0.5 / 1 / 2 | 0.790 / 0.533 / 0.247 | 0.873 / 0.879 / 0.872 | 0.860 / 0.880 / 0.876 |
| rook | 0.6 | 0.5 / 1 / 2 | 0.657 / 0.140 / 0.003 | 0.912 / 0.863 / 0.863 | 0.909 / 0.869 / 0.883 |
| queen | 0.3 | 0.5 / 1 / 2 | 0.823 / 0.683 / 0.420 | 0.899 / 0.893 / 0.884 | 0.879 / 0.883 / 0.896 |
| queen | 0.6 | 0.5 / 1 / 2 | 0.690 / 0.343 / 0.033 | 0.880 / 0.933 / 0.899 | 0.880 / 0.933 / 0.906 |
| kNN | 0.3 | 0.5 / 1 / 2 | 0.850 / 0.660 / 0.417 | 0.917 / 0.900 / 0.866 | 0.906 / 0.890 / 0.883 |
| kNN | 0.6 | 0.5 / 1 / 2 | 0.757 / 0.330 / 0.017 | 0.883 / 0.886 / 0.919 | 0.892 / 0.886 / 0.920 |

Bias (lambda-hat minus lambda) at CV x 1:

| W | lambda | naive | SFH REML | SFH ML |
| :--- | ---: | ---: | ---: | ---: |
| rook | 0.3 / 0.6 | -0.175 / -0.274 | -0.025 / -0.017 | -0.045 / -0.035 |
| queen | 0.3 / 0.6 | -0.178 / -0.254 | -0.020 / -0.031 | -0.053 / -0.056 |
| kNN | 0.3 / 0.6 | -0.183 / -0.248 | -0.033 / -0.031 | -0.063 / -0.056 |

RMSE at CV x 1, lambda 0.6:

| Estimator | rook | queen | kNN |
| :--- | ---: | ---: | ---: |
| naive | 0.294 | 0.286 | 0.283 |
| SFH REML | 0.116 | 0.131 | 0.145 |

At lambda 0 the corrected estimators have larger RMSE than the naive one (rook 0.166 vs 0.105), the price of estimating one more variance component.

**Other observations:**
- **Bias grows with the error.** Naive bias at lambda 0.6 is -0.12, -0.27 and -0.46 at CV x 0.5, 1 and 2 (rook).
- **The tool's error-model beta is unaffected.** Its intervals covered 0.85-0.93 in every cell, corrected or not (the outcome error is independent of x).
- **SE calibration.** The ratio mean SE / SD of the estimates is 0.84-1.08 for every estimator.
- **Numerical stability.**
  - Boundary estimates (|lambda| >= 0.98, or the variance component A at 0): 0 for naive. For SFH, 0 except at CV x 2 with lambda <= 0.3 on queen/kNN (0.3-2.0%).
  - Non-convergence (L-BFGS-B not reporting success): REML 50 and ML 37 of 8,100 fits (0.6%). Naive: 0.

### Results: correlated errors (300 reps per cell)

Coverage of nominal 90% intervals:

| W | lambda | gamma | naive | SFH REML | SFH ML | oracle (true Sigma) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| rook | 0 | 0.3 / 0.6 | 0.840 / 0.517 | 0.830 / 0.593 | 0.860 / 0.644 | 0.900 / 0.883 |
| rook | 0.3 | 0.3 / 0.6 | 0.763 / 0.890 | 0.786 / 0.644 | 0.813 / 0.682 | 0.847 / 0.856 |
| rook | 0.6 | 0.3 / 0.6 | 0.303 / 0.633 | 0.916 / 0.809 | 0.920 / 0.830 | 0.920 / 0.913 |
| queen | 0 | 0.3 / 0.6 | 0.827 / 0.550 | 0.832 / 0.657 | 0.839 / 0.693 | 0.880 / 0.889 |
| queen | 0.3 | 0.3 / 0.6 | 0.810 / 0.867 | 0.883 / 0.740 | 0.909 / 0.760 | 0.936 / 0.893 |
| queen | 0.6 | 0.3 / 0.6 | 0.517 / 0.750 | 0.869 / 0.826 | 0.887 / 0.873 | 0.886 / 0.930 |
| kNN | 0 | 0.3 / 0.6 | 0.820 / 0.517 | 0.803 / 0.600 | 0.836 / 0.643 | 0.863 / 0.897 |
| kNN | 0.3 | 0.3 / 0.6 | 0.833 / 0.883 | 0.867 / 0.716 | 0.896 / 0.768 | 0.923 / 0.896 |
| kNN | 0.6 | 0.3 / 0.6 | 0.487 / 0.717 | 0.870 / 0.796 | 0.876 / 0.817 | 0.890 / 0.857 |

Bias at lambda 0 with gamma 0.6:

| Estimator | rook | queen | kNN |
| :--- | ---: | ---: | ---: |
| naive | +0.18 | +0.19 | +0.21 |
| SFH REML | +0.19 | +0.20 | +0.22 |
| oracle | -0.001 | -0.05 | -0.03 |

**How to read this:**
- **Spatially correlated sampling error is read as spatial dependence of the disturbances.** With no true dependence (lambda 0), both the naive estimate and the independence-based correction report lambda of about 0.2.
- **The independence-based correction breaks under correlation**, as expected:
  - coverage 0.59-0.69 at gamma 0.6 and lambda 0;
  - 0.64-0.77 at lambda 0.3.
- **The naive coverage of 0.87-0.89 at lambda 0.3, gamma 0.6 is a coincidence**: attenuation from the error variance and inflation from the error correlation cancel.
- **The oracle stays near nominal (0.847-0.936)**, so the failure comes from the covariance assumption, not from the estimator.
- **beta intervals stayed at 0.85-0.94** in every correlated cell.

### Conclusion and proposal (A1)

- **Independent errors: the existing spatial Fay-Herriot model validates as the lambda correction.**
  - REML coverage 0.86-0.93 in every lambda > 0 cell (all W, CV x 0.5-2), vs 0.003-0.85 for the tool default; 0.83-0.89 at lambda 0 (lowest at CV x 2 on rook).
  - Bias at most 0.11 in magnitude (CV x 2), usually <= 0.035.
  - Stable: 0.6% non-convergence, rare boundary fits.
- **It fails when sampling errors are spatially correlated.** Whether ACS errors are correlated enough to matter is the workstream C question (C1/C2).
- **Not implemented as a default** (PLAN_ROUND_16 A1: proposal only; and its key assumption is untested for the ACS until C reports).
- **Proposal for the owner:** expose `spatial_fay_herriot(...).provenance['rho']` / `rho_se` as the "lambda corrected for independent outcome sampling error" route, from `spatial_regression(model='error')`:
  - either as an option, `correct_outcome_error=True`, that refits through `census.sae_spatial` and reports rho, rho_se and the GLS beta;
  - or as a cross-reference in the error-model notes.

  The route should carry the independence caveat, with the C1 neighbour-correlation evidence attached. If C shows non-negligible neighbour correlation, the oracle results say a correction would need an estimated error covariance (a research extension).
- **Wording.** In the SEM, lambda describes the **diffusion of shocks** through the disturbances (Lacombe & LeSage 2015), not a substantive spillover from covariates. The attenuation is measurement error in y diluting that diffusion.

## A2. Effects for the lag model

### What spreg 1.9.1 provides

`ML_Lag`, `GM_Lag` and the regime and panel variants accept `spat_impacts` = "simple" / "full" / "power" / "all":
- `spreg.sputils.spmultiplier` computes the average multipliers (ati = 1/(1-rho); adi = tr(S)/n by "full" (dense inverse) or "power" (power series); aii = ati - adi);
- `spreg.output._summary_impacts` / `sputils._sp_effects` multiply them by beta;
- results go to `reg.sp_multipliers` and the printed summary;
- `i_multipliers` gives observation-level multipliers.

**There is no inference** (no SEs or intervals for the impacts), and the default "simple" method sets adi = 1, i.e. direct = beta. That is not the LeSage & Pace direct effect.

### Tool change

`spatial_regression(model='lag')` returns `ModelResult.effects`: per covariate, the average direct, indirect and total effects.

- **Point estimates.** Computed from the eigenvalues of W, once per fit: direct = beta x mean 1/(1 - rho w_i); total = beta / (1 - rho); with islands, total = beta (1 - s + s/(1 - rho)), s the share of areas with neighbours. These equal spreg's "full" multipliers and the independent S(W) algebra (tests).
- **Uncertainty.**
  - se_model: delta method from the fit's (beta, rho) covariance;
  - se_sampling: from the 80 replicate refits when replicates are given;
  - intervals follow the fit's target.

  The coefficient covariance is in `diagnostics['coefficient_vcov']`, so users can draw simulated (LeSage & Pace) intervals. Effects are not computed above 5,000 areas (dense eigenvalues); a note says so.
- **Effects first:**
  - `to_dict()` puts `effects` before `coefficients`;
  - `summary()` starts "EFFECTS FIRST ...";
  - the first note says beta is NOT the marginal effect, and the coefficient table has an `interpretation` column saying so;
  - `report_tables.effects_table` renders Direct / Indirect / Total;
  - `regression_table` adds a note for lag models;
  - the workflow's `next_step` says to report the effects table first.
- **Labelling of the rho/lambda p-value.** A note says it tests the parameter = 0 within the fitted model and "is not a specification test" (Juhl 2021, sec. 3.2).

### Effects coverage (from A3, SAR lag DGP, rook 12x12, 400 reps; MC SE <= 0.025)

| rho | outcome error | estimator | direct | indirect | total | indirect, simulated interval | total, simulated interval |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| 0.3 | ACS | GMM [default] | 0.918 | 0.868 | 0.890 | 0.898 | 0.910 |
| 0.6 | ACS | GMM [default] | 0.870 | 0.805 | 0.832 | 0.965 | 0.980 |
| 0.3 | none | GMM | 0.912 | 0.892 | 0.892 | 0.882 | 0.880 |
| 0.6 | none | GMM | 0.902 | 0.928 | 0.925 | 0.905 | 0.905 |
| 0.3 | ACS | ML | 0.902 | 0.400 | 0.580 | 0.490 | 0.648 |
| 0.6 | ACS | ML | 0.902 | 0.002 | 0.018 | 0.008 | 0.032 |
| 0.3 / 0.6 | none | ML | 0.908 / 0.890 | 0.865 / 0.845 | 0.858 / 0.860 | 0.878 / 0.872 | 0.872 / 0.880 |

**Delta method vs simulated intervals:**
- The tool's delta-method intervals are near nominal for GMM without sampling error and at rho 0.3 with it.
- At rho 0.6 with ACS error, GMM's rho is imprecise (SD about 0.26). Some samples have rho-hat near 1, so the indirect and total effects are heavy-tailed:
  - mean bias +0.79, RMSE 4.6, against a true indirect effect of 0.685;
  - the delta method under-covers (0.80-0.83);
  - simulated percentile intervals over-cover (0.97-0.98).
- ML lag effects inherit ML's rho failure under outcome error (Round 15).
- The direct effect is always well covered (about beta).

A measured caveat (`LAG_EFFECTS_CAVEAT`) is now in the lag-model notes.

**Proposal:**
1. Add simulated (LeSage & Pace) percentile intervals and medians as an option, `effects_intervals="simulated"`. The draws are cheap once the eigenvalues are known.
2. Consider making them the default at high rho, after the owner reviews this table. The full text of LeSage & Pace 2014 recommends simulated intervals and medians, because total effects can be asymmetric.

## A3. DGP set: what each model recovers (rook 12x12, 400 reps per cell, with and without ACS outcome error)

DGPs:
- **SEM:** lambda 0.6.
- **SAR lag:** rho 0.3 and 0.6.
- **SDM:** rho 0.4, beta 0.5, theta 0.3.
- **Omitted spatially correlated covariate:** z SAR(0.7), corr(x, z) = 0.5, coefficient 8 on standardized z.
- **Juhl (2021) Eq. 5:** y = x beta + (I - rho W)^-1 (x gamma + v), rho 0.5, gamma 0.25 (= beta/2). This is an SDM with beta + gamma and theta = -rho beta.

All four tool estimators were fitted through `spatial_regression`. The true average effects follow LeSage & Pace (dy/dx' = (I - rho W)^-1 (beta I + theta W)).

### Disturbance parameter (error model, SEM DGP)

lambda 0.6 is recovered without sampling error:
- ML: coverage 0.872, bias -0.026;
- GMM: coverage 0.882, bias -0.022.

With ACS error it is attenuated:
- ML: coverage 0.205, bias -0.263;
- GMM: coverage 0.170, bias -0.264.

Remedy: A1. The error model's beta (= its direct and total effect) covers 0.88-0.90 either way. In DGPs without an SEM structure, lambda has no true value. Mean lambda-hat with ACS error was 0.09-0.27, always positive: it absorbs any unmodelled spatial structure.

### Substantive effects (coverage of nominal 90% intervals; ACS outcome error / no sampling error)

| DGP (true direct, indirect, total) | lag GMM [default]: dir / ind / tot | lag ML: dir / ind / tot | error ML [default]: beta covers direct / total |
| :--- | :--- | :--- | :--- |
| SEM lambda 0.6 (0.5, 0, 0.5) | 0.92/0.91/0.82 ; 0.92/0.83/0.74 | 0.89/0.26/0.48 ; 0.82/0.02/0.07 | 0.88/0.88 ; 0.90/0.90 |
| SAR rho 0.3 (0.513, 0.201, 0.714) | 0.92/0.87/0.89 ; 0.91/0.89/0.89 | 0.90/0.40/0.58 ; 0.91/0.87/0.86 | 0.89/0.01 ; 0.77/0.00 |
| SAR rho 0.6 (0.565, 0.685, 1.25) | 0.87/0.81/0.83 ; 0.90/0.93/0.93 | 0.90/0.00/0.02 ; 0.89/0.85/0.86 | 0.84/0.00 ; 0.01/0.00 |
| SDM rho 0.4, theta 0.3 (0.561, 0.772, 1.333) | 0.63/0.63/0.63 ; 0.83/0.85/0.85 | 0.88/0.00/0.00 ; 0.74/0.84/0.81 | 0.79/0.00 ; 0.01/0.00 |
| Omitted covariate (0.5, 0, 0.5) | 0.06/0.45/0.02 ; 0.00/0.17/0.00 | 0.04/0.30/0.01 ; 0.00/0.05/0.00 | 0.05/0.05 ; 0.02/0.02 |
| Juhl rho 0.5, gamma 0.25 (0.770, 0.230, 1.0) | 0.86/0.78/0.81 ; 0.88/0.75/0.78 | 0.86/0.85/0.87 ; 0.87/0.82/0.82 | 0.80/0.00 ; 0.62/0.00 |

**Findings:**
1. **Where the lag model fits, the default GMM lag recovers the effects.** This covers the SEM (indirect effect about 0), SAR and Juhl DGPs, with and without outcome error, with the rho 0.6 / ACS caveat above.
   - ML lag reports spurious indirect effects under an SEM (mean 0.16-0.25, coverage 0.02-0.26), because ML rho picks up the disturbance dependence.
   - GMM lag does not (rho about 0).
2. **Under an SDM with theta > 0, GMM lag breaks down.** Its instruments (W X, W^2 X) are excluded regressors in the true model. Without sampling error, rho-hat averages 0.89 (coverage 0), and the indirect and total effects explode (mean bias +5.3 / +5.4 without error, +6.9 / +7.1 with). Their delta-method coverage (0.85) is meaningless.
   - ML lag has rho bias +0.21 without sampling error and is moderately biased in the effects (indirect -0.09; coverage 0.84).
   - **No production SDM estimator** was added (scope). The tool caveat now warns about W X effects.
3. **The error model's beta is the direct = total effect only when the DGP has no substantive spillover.** Under SAR/SDM/Juhl it misses the total effect entirely (coverage 0.00-0.02). Under the Juhl DGP it is biased by gamma (+0.25), as Juhl and Lacombe & LeSage state: an SEM with a correlated omitted variable is not unbiased.
4. **An omitted spatially correlated covariate that is correlated with x biases every model** (beta +0.13 to +0.15). The lag models turn the omission into a spurious indirect effect (0.15-0.24). No model in the tool fixes omitted-variable bias.
5. **Outcome sampling error mainly hurts ML** (lag rho, effects, error lambda); GMM lag is robust apart from the high-rho tails.
6. **OLS-based RS tests (spreg; Koley & Bera 2024)**, rejection rates at 0.05 (400 reps):

   | DGP, scenario | RS_lag | RS_WX | RS joint | RS*_WX (robust) | RS*_lag (robust) |
   | :--- | ---: | ---: | ---: | ---: | ---: |
   | SEM lambda 0.6, no error | 0.975 | 0.118 | 1.000 | 0.965 | 0.998 |
   | SEM lambda 0.6, ACS error | 0.748 | 0.078 | 0.812 | 0.462 | 0.858 |
   | SAR rho 0.6, no error | 1.000 | 0.992 | 1.000 | 0.025 | 1.000 |
   | SDM, no error | 1.000 | 1.000 | 1.000 | 0.970 | 0.948 |
   | Juhl, no error | 0.992 | 0.702 | 0.998 | 0.628 | 0.985 |

   - Under a pure SEM the robust W X test rejects 97% of the time (gamma = -beta lambda is not 0) and the robust lag test 99.8%. These tests point to an SDM or lag model when the truth is an SEM.
   - They are not SEM-vs-SDM tests and are **not exposed** in the tool. The SEM-vs-SDM question needs the common-factor test (A4).
   - Sampling error lowers their power.

## A4. Common-factor LR test (SEM as SDM with theta = -rho beta)

### Checks

- **Nesting.** (I - lambda W) y = (I - lambda W) X beta + e gives y = lambda W y + X beta - lambda W X beta + e. That is the SDM with rho = lambda, theta = -lambda beta and constant (1 - lambda) alpha (row-standardized W). There is one restriction per covariate.
- **Likelihood comparability.** spreg's `ML_Lag(slx_lags=1)` (the SDM) and `ML_Error` share the same constants, the full log|I - rho W| Jacobian, sigma^2 = e'e/n and rho, lambda in (-1, 1). This was verified in code and numerically (tests).
- **The studies** use eigenvalue-based reimplementations equal to spreg to 5 decimals.

### Size and power, Juhl's design (no sampling error)

Design: queen W, beta 2, x, v ~ N(0, 1), 1000 repetitions per cell (MC SE <= 0.016). Residual bootstrap on the first 200 (MC SE <= 0.035; B = 100; Juhl sec. 3.3).

Size (gamma = 0), chi-square(1) critical value, nominal 0.05:

| rho | n = 49 | 100 | 225 | 400 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0.066 | 0.054 | 0.052 | 0.060 |
| 0.2 | 0.067 | 0.059 | 0.053 | 0.044 |
| 0.4 | 0.077 | 0.049 | 0.064 | 0.049 |
| 0.6 | 0.078 | 0.073 | 0.070 | 0.060 |
| 0.8 | 0.090 | 0.066 | 0.041 | 0.052 |

Size with Juhl's residual-bootstrap critical value (200 reps per cell):

| rho | n = 49 | 100 | 225 | 400 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0.045 | 0.050 | 0.065 | 0.070 |
| 0.2 | 0.080 | 0.080 | 0.035 | 0.045 |
| 0.4 | 0.065 | 0.050 | 0.095 | 0.040 |
| 0.6 | 0.065 | 0.075 | 0.055 | 0.070 |
| 0.8 | 0.080 | 0.080 | 0.040 | 0.050 |

Power at gamma = 0.5 and 1.0 (chi-square(1)):

| rho | gamma | n = 49 | 100 | 225 | 400 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.2 | 0.5 / 1.0 | 0.082 / 0.104 | 0.070 / 0.113 | 0.114 / 0.224 | 0.128 / 0.317 |
| 0.4 | 0.5 / 1.0 | 0.108 / 0.201 | 0.123 / 0.338 | 0.212 / 0.584 | 0.321 / 0.825 |
| 0.6 | 0.5 / 1.0 | 0.191 / 0.386 | 0.211 / 0.629 | 0.398 / 0.908 | 0.605 / 0.990 |
| 0.8 | 0.5 / 1.0 | 0.263 / 0.587 | 0.340 / 0.828 | 0.608 / 0.982 | 0.803 / 1.000 |

At rho = 0 the restriction holds for any gamma (theta = 0 = -0 x beta), so the "power" there is size (0.044-0.075).

**Reading:**
- **The LR test's size is close to nominal for n >= 100** (0.041-0.073). It is mildly liberal at n = 49 (0.066-0.090) and at rho 0.6 (0.060-0.078).
- **This confirms that the LR test does not have the Wald test's problem** (Juhl, Table 2: 0.0-11.5%), but it is not exact.
- **The residual bootstrap gives 0.035-0.095.** Within its larger MC error (0.015 at 0.05) it is not demonstrably better. It is not needed for n >= 100.
- **Power is low** for small rho or small n. For gamma / beta = 0.25 and rho <= 0.4 it is under 0.35 at every n.

### With ACS outcome error (`common_factor_lr_acs`)

Design: queen 12x12, the scale of the other studies; 500 reps for chi-square, the first 200 for the bootstraps.

| rho | gamma | error | chi-square(1) | Juhl bootstrap | ME-aware bootstrap (adaptation) |
| ---: | ---: | :--- | ---: | ---: | ---: |
| 0.3 | 0 (H0) | none / ACS | 0.058 / 0.060 | 0.065 / 0.045 | - / 0.065 |
| 0.6 | 0 (H0) | none / ACS | 0.054 / 0.060 | 0.050 / 0.060 | - / 0.055 |
| 0.3 | 0.25 | none / ACS | 0.172 / 0.118 | 0.170 / 0.150 | - / 0.135 |
| 0.6 | 0.25 | none / ACS | 0.518 / 0.388 | 0.495 / 0.415 | - / 0.410 |

Independent outcome error keeps the size (the SEM-plus-noise null is not an SEM, but the LR barely reacts) and costs power (0.52 to 0.39 at rho 0.6).

### SEM estimates across gamma (Lacombe & LeSage 2015; Juhl design, no sampling error)

Mean ML estimates, 1000 repetitions per cell (true beta = 2; the DGP's rho is the SEM's "lambda" when gamma = 0):

| rho | gamma | SEM lambda-hat, n = 49 / 100 / 225 / 400 | SEM beta-hat, n = 400 |
| ---: | ---: | :--- | ---: |
| 0.4 | 0 | 0.281 / 0.336 / 0.369 / 0.384 | 1.999 |
| 0.4 | 0.5 | 0.298 / 0.363 / 0.384 / 0.395 | 2.487 |
| 0.4 | 1.0 | 0.316 / 0.379 / 0.408 / 0.413 | 2.976 |
| 0.6 | 0 | 0.481 / 0.545 / 0.572 / 0.583 | 1.999 |
| 0.6 | 0.5 | 0.507 / 0.562 / 0.588 / 0.599 | 2.476 |
| 0.6 | 1.0 | 0.536 / 0.608 / 0.631 / 0.644 | 2.943 |
| 0.8 | 1.0 | 0.765 / 0.816 / 0.837 / 0.841 | 2.910 |

**Reading** (consistent with Lacombe & LeSage 2015 and Juhl 2021):
- **ML lambda has a small-sample downward bias** (-0.12 at n = 49, rho 0.6, gamma 0).
- **An omitted variable correlated with x pushes the SEM's lambda up.** At n >= 225 with gamma = 1, lambda-hat exceeds the true rho.
- **The SEM's beta absorbs gamma.** beta-hat is about beta + gamma (2.48 at gamma 0.5, 2.94 at gamma 1), slightly less at high rho. The SEM reports it as both the direct and the total effect, while the true total effect is beta + gamma / (1 - rho). The SEM therefore misses the indirect effect gamma rho / (1 - rho) entirely (see A3 for the Juhl DGP).
- **The SDM's rho-hat matches the SEM's lambda-hat under H0** (the two models coincide).
- **A diagnostic for users:**
  - a gap between the SEM and OLS beta;
  - significant lag-model indirect effects;
  - a rejected common-factor LR.

  Each is an omitted-variable signal (Lacombe & LeSage). None is decisive alone, given the LR's power.

Reproducibility: the `common_factor_lr` rows from the 5-process run and a separate single-process rerun are identical.

### Conclusion (A4)

**The LR common-factor test is valid** (size 0.04-0.09, near nominal for n >= 100) **but weak** in the sample sizes typical of county or tract analyses in one state. A significant result is informative; a non-significant one is weak evidence for the SEM.

It is **not added to the tool** in this round, for two reasons:
1. It needs the SDM fit (spreg ML_Lag with slx_lags=1), which the plan excludes as a production estimator.
2. Its power is low.

**Proposal:** if the owner wants an SEM-vs-SDM check, add `common_factor_test(formula, data, w)`. It would:
- fit both models by spreg ML on the same data;
- report the LR statistic with the chi-square(1) p-value per restriction (k - 1 df);
- add Juhl's residual bootstrap p-value for n < 100 (measured: chi-square size up to 0.09 at n = 49);
- carry a power caveat from the table above, and no SDM coefficients as results;
- not label the OLS RS tests or the rho = 0 test as specification tests.

The Bayesian model-probability approach (Lacombe & LeSage 2015) needs an SDM estimator and marginal likelihoods: proposal only.

## Files

- `census/spatial.py`: `weights_eigenvalues`, `lag_multipliers`, `lag_effects`, effects in `spatial_regression`, `LAG_EFFECTS_NOTE`, `LAG_EFFECTS_CAVEAT`, `SPATIAL_PARAMETER_TEST_NOTE`, `diagnostics['coefficient_vcov']`, and the `interpretation` column.
- `census/results.py`: `ModelResult.effects`, ordered first in `to_dict()`, plus the summary lead.
- `census/report_tables.py`: `effects_table`, and the lag-model note in `regression_table`.
- `census/workflows_geo.py`: docstring and `next_step` (effects first).
- `census/simulation_spreg.py` (new): the studies, `me_sem_fit`, `sem_ml`, `sdm_ml`, `common_factor_lr`, `lr_residual_bootstrap`, `true_effects` and lattice helpers.
- `census/simulation.py`: registration (group `spreg_validation`).
- `scripts/inference_validation.py`: routing when named.
- `tests/test_spatial_regression_validation.py` (new) and `tests/test_workflows_geo.py`.
