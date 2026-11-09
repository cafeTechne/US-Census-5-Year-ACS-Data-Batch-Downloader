# Full-text reading notes: Round 16 spatial methods

Read 2026-11-08 from the full texts retrieved through open-access routes:
- PubMed Central articles: the NCBI E-utilities XML, equations included;
- Juhl: the Cambridge Core CC-BY PDF;
- Arthur: the arXiv v1 PDF.

Copyrighted text is not committed; only short quotations and section/equation references.

**Status labels:**
- **VERIFIED:** stated in the full text I read.
- **ADAPTATION:** my inference for this engine, not a claim of the paper. It must be tested before use.
- **NOT READ:** supplements and web appendices.

**Retrieved by the owner (2026-11-08):** these two were blocked to scripted requests (HTTP 403) and were not circumvented. The owner downloaded them in a browser into `literature_pdfs/`, which is gitignored; see sections 7 and 8.
- LeSage & Pace (2014): MDPI version of record.
- Koley & Bera: SSRN preprint, abstract 4252849. The journal version may differ.

---

## 1. Wong (2021): cluster detection and estimate error

*IJERPH* 18(18):9848, PMC8466737. Full text read.

**VERIFIED:**
- **What local statistics detect:** local Moran's I and local G detect *similarity* among neighbours, not extreme values. Extreme units with dissimilar neighbours can be missed, and moderate-value units can form clusters (sec. 3.1; simulated hexagon examples).
- **Estimate error:** local statistics assume accurate or uniformly reliable estimates (sec. 3.2). Within one detected cluster, estimates can differ significantly, while units inside and outside it may not (Table 2, obesity data).
- **Class separability classification** (sec. 4.1): class breaks chosen to maximize the confidence that estimates on either side differ. The top and bottom classes are read as hot and cold spots, which need not be contiguous.
- **Heuristic hot/cold-spot procedure** (sec. 4.2):
  1. The analyst sets low and high thresholds from domain knowledge (a "global criterion").
  2. Units whose *estimates* are beyond a threshold are seeds.
  3. First-order neighbours are compared with the **seed** estimate by "a t-test given a confidence level". A neighbour **not significantly different** joins the cluster.
  4. Growth continues to higher-order neighbours, always comparing against the seed. It stops when all frontier neighbours differ.
  5. A seed with no such neighbour is a single-unit spot.
- **Multiple testing:** Bonferroni is "not applicable" because membership rests on *failing to reject*. A binomial probability, Eq. (1), "assumes the independence of events".
- **Error size drives membership:** larger errors make estimates "more likely to be statistically indifferent and thus produced larger HSCS" (sec. 5.2).
- **Simulations:**
  - errors random, proportional or inversely proportional to the estimates, "associated with the estimates, not with locations";
  - no accuracy metrics reported;
  - the empirical example is state BRFSS obesity, not ACS.
- **Not specified:** the t-test formula (how SEs combine, degrees of freedom, sidedness); tie-breaking when clusters from different seeds meet.

**ADAPTATION / implications:**
- **Different estimand from Local Moran's I.** It flags units *beyond a threshold* plus neighbours not shown to differ. Under "no spatial clustering" it still reports spots wherever extreme values occur, so false-positive comparisons with Local Moran's I compare unlike targets. Evaluate it against its **own** target: recovery of units whose *true* value is beyond the threshold, and the inclusion of neighbours whose true value is near the seed's.
- **Absence of evidence becomes membership.** Larger SEs mean larger clusters. This is a measurable property, and expected to be worse for high-CV tracts.
- **Correlated errors break the test.** Positively correlated errors between a seed and its neighbour make an independence-based difference test under-reject, so clusters grow. Negatively correlated errors do the opposite. This is a hypothesis to measure in B scenarios 3 and 5.

## 2. Koo, Wong & Chun (2019): global spatial autocorrelation with reliability information

*The Professional Geographer* 71(3):551-565, PMC6884366. Full text read; equations recovered from MathML.

**VERIFIED:**
- **What "biased upward" means.** The claim that conventional statistics ignoring error are biased upward comes from comparing published estimates with **new draws from N(estimate, SE)**, i.e. a second layer of noise on top of the sampling error already in the estimates. In their words, estimates "with larger errors are less positively autocorrelated". This is **consistent with**, not contrary to, attenuation of the *true* process's autocorrelation by sampling error.
- **The SBC** (Eqs. 2-4): the Bhattacharyya coefficient between neighbouring N(μᵢ, σᵢ²) and N(μⱼ, σⱼ²), averaged over binary neighbour weights. Eq. (3) as printed has a sign slip; the coefficient is exp(-BD).
  - Significance is by permutation, shuffling estimates and SEs separately.
  - On the 50×50 lattice, the SBC is significant "even with a large CV ... although the estimates do not show spatial autocorrelation (ρ = 0)". The authors say errors have "an overwhelming influence".
  - It "is not compatible" with conventional statistics for the direction of autocorrelation.

**ADAPTATION / implications:**
- **Not a correction.** The SBC measures overlap of *error distributions*, not autocorrelation of the latent process. It can be significant with no spatial structure, so it is neither a correction nor a test of true spatial autocorrelation for our purposes. At most it is a descriptive reliability index. **Do not implement as a correction.**

## 3. Donegan, Chun & Griffith (2021): Bayesian inference with survey SEs and spatial structure

*IJERPH* 18(13):6856, PMC8297362. Full text read (main text; online supplement not read).

**VERIFIED:**
- **Classical measurement error** (Eq. 1): observed = true + error. With spatial data, "observational error tends to decrease the degree of observed SA", and spatial variation in data quality produces spatially varying consequences (sec. 2.3).
- **Empirical illustration:** Milwaukee tract insurance coverage has SAR ρ of 0.82 raw against 0.89 (0.86-0.92) after modelling (sec. 3.3).
- **Data model:** Gauss(true value, SE), i.e. independent errors with the published SEs. Process model: CAR, (I − ρW)⁻¹M (Eq. 5). The model is applied to a **covariate** (errors-in-variables); the outcome is a Poisson count (Eq. 8).
- **ACS facts used:**
  - SEs are spatially autocorrelated (SAR ρ 0.46-0.69 for the SEs themselves; Table 1);
  - MOEs come from Successive Differences Replication;
  - variance replicate tables let users build SEs for composite variables (their ICE).
- **Limitations stated:**
  - non-sampling errors are not modelled;
  - "errors themselves are correlated due to bias in the sampling design or survey implementation" is possible, and would need additional information.
  - The naive model attenuated the covariate effect relative to the measurement-error model (Fig. 12).

**ADAPTATION / implications:**
- **The attenuation reading of F1 is supported.** Sampling error added to the *outcome* of a spatial error model inflates the independent part of the disturbance, so the estimated λ is attenuated.
- **The corresponding likelihood**, with independent errors and known sampling variances, has covariance σ²[(I − λW)ᵀ(I − λW)]⁻¹ + diag(SE²). That is the **SAR random-effects (spatial Fay-Herriot) model** the engine already implements in `census.sae_spatial`, where it is a gated, passing release study.
- **Hypothesis for A1:** the spatial Fay-Herriot ρ estimate (and β) is a validated correction for λ attenuation that needs only the sampling variances and assumes independent errors. It must be tested:
  - coverage of ρ, not only of the EBLUP;
  - behaviour under correlated sampling errors;
  - weights sensitivity.
- **Diagonal error covariance:** every method in these papers assumes it. A correction built from MOE variances is independence-based by construction.

## 4. Sun, Reich, Cai, Guindani & Schwartzman (2015): FDR control in spatial multiple testing

*JRSS-B* 77(1):59-83, PMC4310249 (author manuscript). Main text read; web appendix with the Theorems 2 and 4 proofs not read.

**VERIFIED:**
- **Model:** X(s) = μ(s) + ε(s), a latent process plus noise (Eq. 1.1). Hypotheses are about the **latent signal relative to an indifference region**: H₀: μ(s) ∈ A, for example μ ≤ μ₀ (Eq. 2.1). They are not about local autocorrelation.
- **Oracle statistic:** T_OR(s) = P(θ(s) = 0 | data) (Eq. 3.4). Procedure 1 rejects the largest set whose running average of T_OR is ≤ α (Eq. 4.1). For a discrete domain the FDR control is exact (Corollary 1), **given the true model parameters**.
- **Data-driven version:** uses MCMC posterior samples. "The precision of our testing procedure shows some sensitivity to model misspecification" (sec. 6).
- **The FCR** (Eq. 2.3) is a weighted rate over **predefined** clusters with partial-conjunction nulls. This is **not** the same as a "maps with ≥ 1 false contiguous cluster" rate.
- **Simulations:**
  - the error covariance (1 − r)I + r·exp(-d/ρε), with r = 0.9, i.e. mostly spatially *correlated* errors;
  - BH and related methods were "very conservative".
- **Ozone application:** β̂(s) = β(s) + w(s)ε(s), with known per-site SEs w(s). This is the same structure as ACS estimates with published SEs.

**ADAPTATION / implications:**
- **Three questions to keep apart in B and in the tool:**
  1. "Is this area's latent value elevated?" (Sun et al.'s and Wong's question);
  2. "Is this area locally similar to its neighbours beyond chance?" (Local Moran's I);
  3. "Is the classification stable across replicates?"
- **A principled route for question 1:** posterior exceedance probabilities from a spatial model with known sampling variances, thresholded with Procedure 1.
  - Spatial Fay-Herriot gives a Gaussian approximation (EBLUP, MSE). This would be an adaptation **without** full parameter uncertainty, so it needs its own validation.
  - Out of scope for production this round; candidate for a later round.
- **Name collision:** our "false-cluster rate" must be renamed or defined explicitly to avoid confusion with their FCR.

## 5. Juhl (2021): the Wald test of common factors

*Political Analysis* 29(2):193-211, doi 10.1017/pan.2020.23, CC BY. Main text read; Supplementary Materials A-C not read.

**VERIFIED:**
- **Models:** the SEM y = xβ + (I − ρW)⁻¹u is the SDM y = ρWy + xβ + Wxθ + u under θ = −ρβ (Eqs. 1, 3, 4).
- **Omitted variables:** an omitted variable that is spatially clustered **and correlated with x** gives an SDM-like DGP (Eq. 5). The SDM then has E(ρ̂) = ρ and E(θ̂) = −ρβ, but β̂ is biased by γ. If the omitted variable is uncorrelated with x, the result is an SEM and "even a nonspatial OLS model specification would provide unbiased but inefficient parameter estimates".
- **Wald non-invariance:** asymptotic Wald tests of the nonlinear restriction are not invariant to algebraically equivalent forms (Table 1: H0(I)-(IV)).
  - Type I error ranged from 0.0% to 11.5% at a nominal 5% (Table 2; n = 49 to 400, queen contiguity, β = 2, ρ from 0 to 0.8, 1000 samples).
  - H0(IV) has a non-monotone power function.
- **Bootstrap fix** (sec. 3.3):
  1. fit the SEM;
  2. resample its residuals to build 100 bootstrap samples under the SEM DGP;
  3. recompute w for each;
  4. use the (1 − α) quantile as the critical value.

  This brings the size to about 4-7% for all forms (Table 3).
- **LR test:** invariant to reparameterization. Its good finite-sample performance is asserted via **Supplementary Material C.7 (NOT READ)**.
- **What software reports:** the "Wald tests" reported by spatial software test ρ = 0, not common factors (sec. 3.2).
- **Interpretation:** inference should rest on direct and indirect **impacts**, not coefficients (footnote 23). The SDM is not a general solution to omitted variables: identification is weak and depends on perfect knowledge of W (citing Gibbons & Overman 2012).

**ADAPTATION / implications for A3/A4:**
- **Use Juhl's design as the template** for the omitted-variable DGP (γ grid) and the size/power study.
- **Measure the LR common-factor test's size directly.** The LR claim rests on an unread supplement. If the size is off, implement Juhl's bootstrap (it is fully specified in the main text).
- **The tool must not label its ρ = 0 test a specification test.**

## 6. Arthur (2024/2025): resampling autocorrelated spatial data

*Geographical Analysis* 57(2):302-319, doi 10.1111/gean.12417 (journal version not retrievable). arXiv:2401.05728v1 read in full.

**VERIFIED:**
- **Method:** null distributions for **bivariate association statistics** (Pearson, Spearman, Kendall correlation between two maps) are built by permuting values and then greedily optimizing so each resample matches the observed global Moran's I.
- **Results:** comparable to sampling from the true DGP in the authors' power-law-spectrum lattice tests.
- **Caveats:**
  - Moran's I saturates; the method over-rejects at high autocorrelation and is "not recommend[ed]" above I ≈ 0.7;
  - it is for statistics "which sum over all the data points";
  - no treatment of local statistics or of sampling error;
  - Python package `spatialsignificance`.

**ADAPTATION / implications:**
- **Not applicable to the Local Moran's I problem.** It could inform a later null model for *bivariate* map comparisons (for example our `correlation` tools), only with validation and only for I ≤ 0.7.

## 7. LeSage & Pace (2014): "The Biggest Myth in Spatial Econometrics"

*Econometrics* 2(4):217-249, doi 10.3390/econometrics2040217. Full text read (numeric table rows skimmed).

**VERIFIED:**
- **Effects:** for the SDM y = αι + ρWy + Xβ + WXθ + ε, the partial derivatives are ∂y/∂xᵣ′ = (I − ρW)⁻¹(Iβᵣ + Wθᵣ) (Eq. 32). The SAR case is θ = 0 (Eqs. 18-19). The diagonal holds the direct (own-region) effects; the off-diagonal elements hold the indirect (spillover) effects (sec. 3.3).
- **Interpretation:**
  - β is not the marginal effect when the model contains Wy.
  - When W changes, ρ and β shift in offsetting ways ("required to maintain relatively stable partial derivatives"; Fig. 1). The effects stay stable (Tables 5-8).
  - So sensitivity checks must compare **effects, not coefficients** (sec. 3.1, 3.3).
- **Robustness:** observation-level (n²k) effects are **not** robust to W. Scalar summaries averaged over observations are (sec. 4, 5).
- **Effect draws:** medians of simulated effect draws are suggested, since total effects can be asymmetric (footnote 12). Draws from the ML estimates and their covariance are "equivalent" to their MCMC draws.
- **Misspecification signal:** sensitivity to W is often a symptom of model misspecification. For a correctly specified SEM, OLS β is unbiased, so large OLS-versus-SEM differences in β point to misspecification. The Bell & Bockstael re-analysis is the example: 5 of 10 coefficients differ by more than 1.67 SD. A Hausman test is cited to Pace & LeSage (2008; **NOT READ**).
- **Weights similarity:** corr(Wₐu, W_b u) = (mₐ/m_b)^½ for nearest-neighbour matrices (Eq. 7), and the correlation of effects across W is given by Eq. (27).
- **Model choice:** weight matrices can be compared by Bayesian posterior model probabilities (Eq. 34). Flexibility in modelling the spillovers matters more than flexibility in the disturbances (sec. 4).

**ADAPTATION / implications:**
- **Effects in the tool:** the lag-model output reports average direct, indirect and total effects with simulated intervals, and labels β as not the marginal effect.
- **W sensitivity:** A's weights-sensitivity results compare effects across W, not ρ/β.
- **OLS-versus-SEM β gap:** a candidate diagnostic for the SEM route. It is cheap. The formal Hausman version is unread, so report the gap descriptively unless validated.

## 8. Koley & Bera: "To use, or not to use the spatial Durbin model?"

SSRN 4252849 preprint; the journal version is *Spatial Economic Analysis* 19(1):30-56 (2024). Preprint read in full; proofs in its appendix skimmed.

**VERIFIED:**
- **The hypothesis tested** is the **linear** H0D: γ = 0, the Durbin (WX) term, in y = λWy + αι + Xβ + WXγ + ε. Here λ is the **lag** coefficient. This is **not** the common-factor hypothesis H0S: γ + βλ = 0 (sec. 2):
  - accepting H0S points to the SEM;
  - accepting H0D points to the SAR.
- **Tests:** the joint RS_λγ, plus Bera-Yoon robust versions RS*_λ and RS*_γ. All need **only OLS** (sec. 3-4).
  - Robust to **local** misspecification only, with Monte Carlo suggesting some non-local robustness.
  - Additivity identity (Eq. 3.9/4.16): RS_λγ = RS_λ + RS*_γ = RS*_λ + RS_γ. They recommend using it to check computations.
- **Assumptions:** normal errors (asymptotic validity under non-normality is asserted via Fang et al. 2014, not shown); increasing-domain asymptotics; bounded W row sums; nonstochastic X.
- **Recommended sequence:** run RS_λγ first; if it rejects, run RS*_λ and RS*_γ.
- **Monte Carlo:**
  - Design: Columbus 49-district W (and W⊗I₂, n = 98); α = 1, β = 1.5; ε ~ N(0, 2); x ~ N(0, 4); λ ∈ [0, 0.6]; γ ∈ [0, 0.35]; 5000 reps.
  - Results: at n = 49 the unadjusted tests over-reject, while the adjusted and joint tests under-reject slightly. RS*_γ has flat size under λ ≠ 0, at a cost in power.
  - **No SEM DGP is simulated.**

**ADAPTATION / implications:**
- **They do not identify the SEM.** Under an SEM DGP the SDM representation has γ = −βλ ≠ 0, so RS*_γ is expected to reject and point toward an SDM. That is my derivation, not shown in the paper. The SEM-versus-SDM decision therefore still needs the common-factor test (Juhl's bootstrap or the LR test).
- **They are already in spreg 1.9.1** (`lm_wx`, `lm_spdurbin`, `rlm_wx`, `rlm_durlag`). If the tool adds them, use spreg's implementation and verify it with the paper's additivity identity, not a reimplementation.

## 9. Lorincz-Comi, Ajayakumar, J. Curtis, Zhang, A. Curtis & Lovell (2021): "Addressing uncertainty in census estimates"

*Spatial Statistics* 45:100523. Owner-supplied PDF, main text read in full. The supplement (estimator variances, multivariable extension, simulation generators) was **NOT READ**.

**VERIFIED:**
- **Scope:** this is a method-of-moments measurement-error correction for **geographically weighted regression**, with location-weighted moments.
  - Classical model: x = X + u, y = Y + q, with errors normal around the truth.
  - The naive slope is attenuated by κ = σ²X/(σ²X + σ²u) when σXu = σuq = 0 (Eq. 2). The correction solves for β₁.
  - It allows "nonclassical" σXu ≠ 0 (error correlated with the true value), in which case the bias can go either way (Fig. 1), and correlated x/y errors σuq.
- **Inputs:** σu² is the distance-weighted mean of (MOE/1.645)².
- **Corrected correlation** (Eq. 3) and a **corrected Moran's I** (Eq. 4), which subtracts the error covariance between locations σuq and the error variance σu² from the cross- and own-moments.
- **Inference:** variances of the corrected estimators are only in the supplement (a proposed jackknife). The authors state that existing variance estimators "cannot be applied".
- **What it does not do:** it addresses neither spatial error / lag models nor λ.

**ADAPTATION / implications:**
- **A1:** confirms that this paper is **not** a λ correction. Its global (non-GWR) analogue is the engine's existing `regression_calibration`.
- **Eq. 4 corrected Moran's I:** the between-area error covariance term is exactly what workstream C estimates from replicates. This makes a corrected global Moran's I a candidate for a later round. Do not implement before: its variance/inference is verified from the supplement (unread), and a simulation validates it under independent and correlated errors.

## 10. Jung, Thill & Issel (2019): spatial autocorrelation and ACS data uncertainty, a critique

*IJGIS* 33(6):1155-1175, doi 10.1080/13658816.2018.1554811. Owner-supplied PDF, read in full.

**VERIFIED:**
- **Global Moran's I is attenuated by reliability:** with independent errors, uncorrelated with the truth, observed I ≈ [Var(θ)/Var(x)] × true I (Eq. 4). This is regression dilution.
- **Ground truth:** Mecklenburg County tract teen birth rates against 2010 census plus birth-certificate counts. Global I was 0.46 on the census-based rates, against 0.128 and 0.025 on the ACS-based rates, matching their reliability ratios of 15.23% and 0.43%.
- **Local Moran's I:** mostly **deflated** in true hot and cold spots, so clusters were missed. Two spurious hot spots appeared. Tests used 10,000 permutations with queen contiguity.
- **Remedies:** empirical Bayes and small-area estimation (the Fay-Herriot family).

**My reading (not established):**
- **Eq. 9:** the extra local term diag(WΩ)/Var(ε) is zero when Ω (the error covariance) is diagonal, because W has a zero diagonal. So under the paper's own independence assumption, heteroscedastic errors change the *variance*, not the *expected value*, of local I. The term is non-zero only when **neighbouring errors are correlated**: diag(WΩ)ᵢ = Σⱼ wᵢⱼ Cov(εᵢ, εⱼ).
- This matches workstream B: independent errors mainly cost power, while correlated errors add false structure. It also ties directly to C's covariance estimates.

## 11. Rogerson (2025): "Testing Hypotheses When You Have More Than a Few"

*Geographical Analysis* 57(2):175-190, doi 10.1111/gean.12412. Owner-supplied PDF, read (closing pages skimmed).

**VERIFIED:**
- **Family-wise error rate (FWER) methods:** Bonferroni, Šidák, Holm (step-down), Hochberg (step-up, which needs independence or positive dependence).
- **FDR control:** Benjamini-Hochberg (BH) controls the FDR under independence or positive dependence. It notes that nearby local statistics "will almost always be positively correlated". Benjamini-Yekutieli covers general dependence.
- **Ignored autocorrelation inflates FDR:** if spatial autocorrelation is ignored in the individual tests, the realized FDR exceeds the nominal level (an algebraic argument).
- **Estimating the number of true nulls (m₀):** Schweder-Spjøtvoll, Nettleton et al., Storey's λ estimator (λ = 0.5 "usually serves well"). **Adaptive two-stage BH** (Benjamini et al. 2006) uses m₀ for more power.
- **Cluster-level testing** (Heller et al. 2006; Pacifico et al. 2004; Chumbley & Friston 2009) "should often replace" area-level perspectives.
  - Example: one 95-unit true cluster plus 5 false units gives an area-level FDR of 5% but a cluster-level FDR of 83%.
  - q = 0.2-0.3 "might conceivably be tolerable" for clusters.
- **Illustration:** Gi* on a 55×55 grid. FDR adds rejections over Bonferroni, and adaptive BH adds one more.

**ADAPTATION / implications:**
- **B:** the current Local Moran's I default uses BH, which is valid under positive dependence. Adaptive (Storey) BH is a candidate for more power and is cheap.
- **Cluster-level reporting:** our "false-cluster map rate" is a step toward it. Report flagged results as clusters with a cluster-level error rate. Proposal only, to be validated with B's harness.

## 12. Lacombe & LeSage (2015): Bayesian posterior model probabilities and omitted variables

*Papers in Regional Science* 94(2):365-384, doi 10.1111/pirs.12070. Owner-supplied PDF, read (Monte Carlo tables and appendix skimmed).

**VERIFIED:**
- **Omitted variables decide the model:**
  - an omitted variable uncorrelated with x gives an SEM, with no spillovers; OLS is consistent for β, but its inference is not valid;
  - spatially dependent disturbances plus an omitted variable correlated with x give an SDM, y = ρWy + x(β + γ) + Wx(−ρβ) + ε (Eqs. 10-13).
- **γ is identified:** β = θ/(−ρ), then γ = ϕ − β. A significant γ is a test for omitted-variable bias on that variable. They also suggest the t-statistics of the **indirect effects** as a simpler analogous test.
- **SEM disturbances are "diffusion of shocks", not spillovers.**
- **Bayesian model comparison** (LeSage & Parent 2007) gets SEM against SDM right at correlation ≈ 0 and ≈ 0.7 between x and the omitted variable. In between it depends on n: at correlation ≈ 0.2 it points to the SEM at n = 506 and to the SDM at n = 3,107 and 10,000.
- **Biases:** the SEM overestimates ρ when the omitted variable is correlated with x. ML ρ is biased downward in small samples (citing Bao & Ullah 2007).
- **Census data:** variables at county, tract and block-group level show spatial dependence of 0.75-0.95 (citing Pace et al. 2012).

**ADAPTATION / implications:**
- **A3:** the omitted-correlated-covariate DGP is the central stress test. The Juhl grid already uses this DGP, with γ the same parameter.
- **A4:** evaluate the common-factor/γ route next to LR and the bootstrap. Bayesian model probabilities need a production SDM estimator and a marginal-likelihood implementation, neither of which the engine has, so they stay a proposal.
- **Interpretation:** the lag tool's notes should say that SEM disturbances are diffusion of shocks, not spillovers. This matches the corrected λ wording.

---

## Consequences for Round 16

- **A1:** don't implement Lorincz-Comi et al. (closed access, assumptions unverifiable). First test whether the **existing spatial Fay-Herriot model** recovers λ/ρ with correct coverage under outcome sampling error. That is the independence-based correction the literature's data model implies. Then test it under correlated errors.
- **A2:** impacts, not coefficients (Juhl fn. 23; LeSage & Pace 2014, Eq. 32). Report averaged direct/indirect/total effects with simulated intervals. Check W sensitivity on effects, not on ρ/β.
- **A3/A4:** Koley & Bera's OLS-only robust RS tests (in spreg) answer *SAR vs SDM*, not *SEM vs SDM*; use them only alongside a common-factor test. Juhl's DGP grid. Measure LR size, and use the bootstrap critical value if needed. A ρ = 0 test is not a specification test.
- **B:** keep three questions distinct:
  1. latent elevation (Wong, Sun et al.);
  2. local similarity (Local Moran's I);
  3. replicate stability.

  Evaluate Wong's method against its own target, and record its larger-SE-larger-cluster property and its sensitivity to correlated errors. Rename our false-cluster metric, or define it explicitly against Sun et al.'s FCR. Arthur is out of scope for Local Moran's I.
- **Between-area error covariance** (added after sections 9-12) is the common thread. It is:
  - the σuq term in the corrected Moran's I (Lorincz-Comi Eq. 4);
  - the only source of a non-zero expectation shift in local Moran's I (Jung et al. Eq. 9, as I read it);
  - what makes Fay-Herriot smoothing create false clusters (workstream B).

  C's estimates therefore decide which of these corrections is usable.
- **C:** none of these papers estimates between-area sampling covariance; all assume diagonal error covariance. Measuring it from VRE replicates fills a gap the literature leaves open (Donegan et al. name the possibility).
