# PLAN_ROUND_8.md: An Inference Framework That Respects ACS Sampling Error

**Planning Round:** 8
**Date:** 2026-11-02
**Status:** Implemented (section 15)
**Branch:** `phase8/inference-framework` (from `defaultBranch` after the Rounds 1–7 merge)

---

## 1. Summary

Rounds 5–7 made the data and the arithmetic trustworthy: every value traces to one release and one reviewed concept, every derived estimate carries a margin of error and the method that produced it, and the calculations reproduce numbers the Census Bureau publishes. Round 8 builds the layer researchers actually test hypotheses with: models.

The central problem is that ACS estimates are not data in the usual sense. Each one is a survey estimate with known, often large, sampling error, and standard statistical software ignores that:

1. **Outcomes with sampling error.** A regression of tract poverty rates on covariates treats a rate with a 15-point MOE the same as one with a 2-point MOE. Standard errors are wrong and small-area noise dominates.
2. **Covariates with sampling error.** Measurement error in a predictor biases its coefficient toward zero (attenuation). Ordinary regression cannot detect this.
3. **Overlapping periods are correlated.** Consecutive 5-year releases share 4 of 5 sample years. A panel of annual 5-year estimates looks like 10 observations per tract but contains far less independent information; naive panel models overstate precision.
4. **Many tests at once.** Round 7 added corrections for batch comparisons; models need the same discipline across hypotheses.
5. **Analytic flexibility.** With thousands of tables and geographies, results can be found by searching. Pre-registration is the standard defense, and nothing in the toolkit supports it yet.

Round 8 delivers models that account for these, a pre-registration workflow that locks an analysis before data are pulled, and, as in Round 7, proof: simulation studies showing that the intervals cover the truth at the stated rate, and reproduction of published reference results.

| Priority | Workstream | Section |
| :--- | :--- | :--- |
| P0 | A. Analysis data contract (panels with SEs and optional replicates) | 3 |
| P0 | B. Regression with known sampling error (replicate and model-based) | 4 |
| P0 | F. Inference validation: coverage simulations and reference results | 8 |
| P1 | C. Small-area estimation (Fay–Herriot) | 5 |
| P1 | D. Pre-registered hypothesis manifests | 6 |
| P1 | E. Planning tools: minimum detectable differences and reliability screens | 7 |
| P2 | G. Time and overlap-aware designs | 9 |
| P0 | H. Agent interface and reporting | 10 |
| P1 | I. Publication outputs: figures, correlation matrices, tables | 10a |
| P0 | J. Local-model readiness: an MCP server small open models can use | 10b |

The toolkit must remain usable after development without Claude: by researchers directly (library and CLI) and by agents running on local open-weights models through MCP. Workstream J makes that a tested requirement, not a hope.

---

## 2. Principles

1. **Never discard the MOE.** Every model input keeps its standard error; every model output reports which sources of uncertainty its standard errors include (sampling error of inputs, model error, or both).
2. **Prefer design-based variance.** Where Census publishes replicate estimates, sampling uncertainty in model coefficients comes from refitting on all 80 replicates, which requires no modeling assumptions about measurement error. Model-based corrections are the fallback, and results say which was used.
3. **Validate inference, not just arithmetic.** A confidence-interval method is accepted only after simulation shows it covers the true value at close to the nominal rate under ACS-like error, and after it reproduces a published reference result where one exists.
4. **Pre-register, then run.** Confirmatory analyses run from a locked manifest; anything not in the manifest is labeled exploratory in outputs.
5. **Established libraries only.** Statistics come from `scipy` and `statsmodels`, figures from `matplotlib`: widely cited, peer-reviewed or long-standing projects with stable APIs (decision 1). We implement only what they lack (replicate variance, ACS-specific corrections, Fay–Herriot) and cross-check our code against them wherever they overlap.
6. **Same discipline as before.** Census and published methodological sources are the specification, every result records its method and provenance, and agents can run every step through MCP or the CLI.

---

## 3. Workstream A (P0): analysis data contract

Round 7 section 14 defined the contract; Round 8 implements it.

- **A1. `analysis_frame(...)`** builds a model-ready table from one or more extractions: one row per geography (and release, for panels), with `<var>` (estimate), `<var>_se`, `<var>_cv`, `<var>_method`, and provenance columns (release, concept ID, GEOID). Derived variables (rates, aggregates, medians) come through `census.derived` / `census.aggregate` / `census.medians` with their SEs.
- **A2. Replicate attachment.** `include_replicates=True` attaches the 80 replicate values for every variable where Census publishes them (`census.replicate`), stored in a companion long frame (`GEOID, variable, rep_1..rep_80`) or Parquet file to keep the main table narrow. Derived variables get replicate values by evaluating the derivation on each replicate.
- **A3. Coverage report.** For each variable: whether replicates exist, the share of rows with undefined SEs, and the CV distribution, so an agent can see before modeling which variables are noisy or lack exact variances.
- **A4. Panel outputs gain SE columns** consistently (`*_se` in addition to `*_moe`), so `statsmodels` weights and measurement-error models can be applied directly.

---

## 4. Workstream B (P0): regression with known sampling error

### B1. Replicate-based inference (preferred)

For a model with coefficient vector β fit on full-sample estimates, refit it on each of the 80 replicate datasets (all ACS variables replaced by their r-th replicate values) and compute

Var_sampling(β) = 4/80 · Σ_r (β_r − β)².

This captures sampling error in every ACS variable, outcome and covariates, including covariances, without assuming a measurement-error model. It is the regression analogue of the Round 7 replicate statistics, and it is standard practice for ACS PUMS regression with replicate weights.

- `fit_replicate(model_spec, frame, replicates)`: wraps any `statsmodels` formula model (OLS, WLS, GLM, logit on proportions) and returns coefficients, sampling SEs from replicates, model SEs from the full-sample fit, and a combined SE.
- How sampling and model variance combine is a methodological choice (decision 2). Default: report both components and a total that adds them, with a note on the interpretation.

### B2. Model-based corrections (when replicates are unavailable)

- **Known-variance outcome (meta-regression form).** Outcome y_i observed with known sampling variance s_i²: fit y_i = x_iβ + u_i + e_i with Var(e_i) = s_i² known and Var(u_i) = σ_u² estimated (the Fay–Herriot structure; shares estimation code with workstream C). Weights 1/(σ_u² + s_i²), not 1/s_i², which over-trusts precise areas.
- **Covariates with error.** Two documented options:
  - *Regression calibration*: replace each noisy covariate by its best linear predictor given its estimate and SE.
  - *SIMEX* (simulation-extrapolation, Cook & Stefanski 1994): add extra simulated error at increasing multiples of each covariate's known variance, refit, and extrapolate back to zero error.
  Output includes the naive coefficient alongside the corrected one, so attenuation is visible.
- **Reliability screen.** Before fitting, list observations whose CV exceeds a threshold (default 30%, Census's "low reliability"); the model runs either way, but the screen is reported (decision 4).

### B3. Supported models

OLS/WLS, binomial-family GLM for proportions (with an effective sample size derived from the proportion and its SE), Poisson/negative binomial for counts with exposure, and fixed-effects models for panels (subject to section 9). Formula interface (`patsy`), so agents write `"poverty_rate ~ median_rent + unemployment_rate"`.

### B4. Diagnostics

Residual plots are out of scope (no plotting in the core), but numeric diagnostics are reported: the share of residual variance attributable to known sampling error, influence of high-CV observations, and coefficient stability across replicates.

---

## 5. Workstream C (P1): small-area estimation (Fay–Herriot)

Small areas (tracts, block groups, small counties) often have estimates too noisy to use. The Fay–Herriot area-level model borrows strength from covariates:

- Model: ŷ_i = θ_i + e_i, θ_i = x_iβ + u_i, with e_i ~ N(0, s_i²) known (from the ACS MOE) and u_i ~ N(0, σ_u²).
- Estimation of σ_u² by REML (default), ML, or Fay–Herriot moment method.
- EBLUP: θ̃_i = γ_i ŷ_i + (1 − γ_i) x_iβ̂, γ_i = σ_u² / (σ_u² + s_i²).
- MSE by the Prasad–Rao analytic approximation (with the Datta–Lahiri correction under REML), with a parametric bootstrap option.
- Outputs: EBLUP, its MSE and 90% interval, the shrinkage factor γ_i, and the direct estimate, so users can see how much each area was pulled toward the model.
- Guardrails: refuse areas with undefined sampling variance; report when σ_u² is estimated at zero (complete shrinkage to the synthetic estimate), a known failure mode.
- Validation (section 8): reproduce published reference results for the Fay–Herriot EBLUP and MSE (for example the worked examples accompanying Molina and Marhuenda's `sae` package paper, R Journal 2015), cited during implementation.

---

## 6. Workstream D (P1): pre-registered hypothesis manifests

A manifest is a JSON (or YAML) file that fixes an analysis before data are pulled:

- hypotheses, each with a direction (two-sided or one-sided) and the statistic that tests it;
- variables (`YEAR:CODE` or concept IDs), geographies, releases, and any aggregation groups;
- models and tests (from workstreams B, C, and Round 7), confidence level, multiple-comparison correction and family definition;
- exclusion rules (for example the CV screen) and the handling of missing or undefined estimates;
- authors (same identity convention as overrides) and a creation timestamp.

Workflow:

1. `create_manifest` validates the manifest (every variable resolves, every test is supported) and records its SHA-256. Optionally the hash is committed to the repository or a timestamping service before any data are pulled (decision 3).
2. `run_manifest` executes it exactly: extraction (with `plan_extraction` first), derivations, models, corrections. Results are written with the manifest hash.
3. Anything run outside the manifest on the same data is labeled **exploratory** in outputs.
4. Deviations (an API failure forcing a different release, a variable blocked by concordance review) are recorded explicitly, never silently substituted.
5. A generated **methods section** describes the data sources (releases, tables, concordance decisions), the derivations and their MOE methods, the models, and the corrections, with citations, so the write-up matches what was run.

---

## 7. Workstream E (P1): planning tools

- **Minimum detectable difference.** Given two estimates' MOEs (or typical MOEs for a table and geography level), the smallest difference detectable at a chosen confidence level and power. Answers "can these data detect a 2-point change in tract poverty?" before an analysis is designed.
- **Precision forecast for aggregation.** How much combining tracts into groups of size k reduces the CV, using approximate and (where available) replicate variances.
- **Reliability profile.** For a table and geography level, the distribution of CVs across all areas, so a manifest can choose a geography level where the variables are usable.

---

## 8. Workstream F (P0): inference validation

As in Round 7, nothing is accepted on reasoning alone.

### F1. Coverage simulations

Simulate populations where the truth is known, generate ACS-like estimates by adding sampling error with realistic SEs (drawn from real tract and county MOE distributions, including correlated replicate structure), run each method, and measure:

- coverage of 90% intervals for coefficients and small-area means (target 88–92%);
- bias of coefficients (naive vs corrected, demonstrating attenuation and its correction);
- Fay–Herriot MSE accuracy (estimated vs empirical MSE).

Simulations are seeded and small enough to run in CI in reduced form; the full study runs on demand and writes `docs/audits/inference_validation.md`.

### F2. Reference results

- Reproduce published Fay–Herriot results (section 5).
- Reproduce replicate-based regression SEs on a published ACS PUMS example where Census provides worked results, or cross-check against `statsmodels` survey-weighted estimates on synthetic data.
- Cross-check SIMEX and regression calibration against published worked examples (cited at implementation).

### F3. Round 7 battery stays green

The Round 7 validation battery continues to run in CI and weekly live.

---

## 9. Workstream G (P2): time and overlapping periods

- **Design checks.** Panel models over consecutive 5-year releases are flagged: observations share sample years, so the effective number of independent periods is roughly the span divided by five. The tooling suggests non-overlapping releases (for example 2013, 2018, 2023) for confirmatory trend tests.
- **Trend tests** on non-overlapping periods using the Round 7 z-tests, extended to linear trends across three or more periods with known variances.
- **Overlap-aware variance for differences across overlapping periods** remains the Census approximation (Round 7); exact covariance across releases is not published and is out of scope.

---

## 10. Workstream H (P0): agent interface and reporting

New MCP tools and CLI commands mirror each workstream:

| Tool | Purpose |
| :--- | :--- |
| `build_analysis_frame` | Model-ready table with SEs, optional replicates, and a coverage report |
| `correlation_matrix` | Correlations with intervals, replicate SEs, optional disattenuation |
| `make_figure`, `make_table` | Publication figures and tables with captions and provenance |
| `get_recipe` | Step-by-step workflow recipes for smaller models |
| `fit_model` | Regression with replicate or model-based sampling error; returns coefficients, SE components, method |
| `small_area_estimates` | Fay–Herriot EBLUPs with MSE, intervals, and shrinkage |
| `minimum_detectable_difference`, `reliability_profile` | Planning |
| `create_manifest`, `run_manifest` | Pre-registration and execution |
| `run_inference_validation` | Coverage simulations and reference checks |

`docs/agent_protocol.md` gains an inference section: when to use replicate inference, how to read SE components, the CV screen, overlap rules for trends, and the manifest workflow. Model results are written with the same provenance metadata as extractions, plus the model specification and the manifest hash when one was used.

---

## 10a. Workstream I (P1): publication outputs

Figures and tables suitable for journal submission, generated from results objects so that every number in a figure traces to a result and its provenance.

### I1. Figures (`census/figures.py`, matplotlib)

| Figure | Shows |
| :--- | :--- |
| Estimates with intervals | Point estimates with 90% intervals from MOEs (dot-and-whisker), sorted, with low-reliability estimates marked |
| Coefficient plot | Model coefficients with intervals; naive vs corrected side by side to show attenuation |
| Correlation matrix | Heatmap of correlations among ACS variables, with significance or interval information (I2) |
| Forest plot | Many-area comparisons from `batch_significance`, highlighting results significant after correction |
| Small-area shrinkage | Direct estimates vs Fay–Herriot EBLUPs with intervals, showing how much each area moved |
| Trend plot | Non-overlapping periods with intervals and the trend test result |
| Reliability profile | CV distribution by geography level (planning) |

Requirements:

- **Vector output** (PDF, SVG, EPS) plus PNG at a chosen DPI; sizes for one- and two-column journal widths; embedded fonts.
- **Accessible defaults:** colorblind-safe palettes, line styles and markers that survive grayscale printing, readable minimum font sizes.
- **Every figure ships with** a CSV of the plotted values, a caption draft stating the data source (releases, tables), interval level, and methods, and a JSON sidecar with provenance (result ID, manifest hash when used).
- **Reproducible bytes:** fixed metadata (no creation timestamps; matplotlib's SVG hash salt fixed) so regenerating a figure from the same results gives an identical file.
- **Tested by content, not by eye:** tests check plotted data, axis labels, interval endpoints, and caption text, and include a small set of image-regression baselines.

### I2. Correlation matrices that respect sampling error

Correlations between noisy estimates are biased toward zero (attenuation), and their uncertainty includes sampling error.

- `correlation_matrix(frame, variables, method="pearson"|"spearman")` via `scipy.stats`, returning the matrix, pairwise n, p-values, and confidence intervals (Fisher z for Pearson).
- With replicates: the correlation recomputed on each of the 80 replicates gives a design-based standard error for every coefficient.
- Optional disattenuation (Spearman's correction using each variable's reliability, computed from its known sampling variance), reported alongside, never instead of, the observed correlation.
- Multiple-comparison correction across the matrix (Round 7 `adjust_pvalues`).

### I3. Tables

- Regression, descriptive, and comparison tables as LaTeX (booktabs), Markdown, and CSV, with standard errors or intervals, significance markers after correction, notes on SE components, and the data citation.
- A results bundle per analysis: tables, figures, captions, the methods section (workstream D), and provenance in one folder.

## 10b. Workstream J (P0): local-model readiness

Goal: an agent running on a local open-weights model (via Ollama, LM Studio, llama.cpp, or vLLM) can complete the standard workflows through MCP without Claude.

Small models struggle with long tool lists, long descriptions, nested parameters, and ambiguous next steps. Today's server has 32 tools with long docstrings, written for a frontier model.

### J1. Tool profiles

- **`core` profile (default for local models, about 10 tools):** find a table, describe a variable, plan, download, derive, compare, fit a model, make a figure, run a manifest, get help. Each tool covers one task end to end.
- **`full` profile:** everything, as today.
- **`review` profile:** concordance review and rule tools only, for reviewer agents.
- Selected with `python mcp_server.py --profile core` or an environment variable.

### J2. Tool design rules (enforced by tests)

- Descriptions: one sentence on what the tool does, one line per parameter, one example call; length budget enforced by a test.
- Flat parameters with enums instead of free text where possible; no nested objects in the core profile.
- Every result starts with a short `summary` sentence and, on success or failure, a `next_step` suggestion naming the tool to call next.
- Errors: a fixed set of codes (already in place) plus a plain-language fix.
- Large results are written to files and summarized; the tool returns the path and a preview, never thousands of rows.

### J3. Workflow recipes

Short, numbered recipes (for example "compare a rate between two counties", "map-free tract trend test", "pre-registered regression") returned by a `get_recipe` tool, so a small model follows steps instead of planning from scratch.

### J4. Local-model evaluation harness

- A scripted benchmark of tasks with known correct outcomes (for example "What was the poverty rate in Travis County in 2023, with its MOE?", "Is the change in median rent between 2018 and 2023 in county X significant?", "Fit the pre-registered model in this manifest").
- Runs an MCP client against a local model through an OpenAI-compatible endpoint (Ollama and most local servers expose one), scores tool choice, argument correctness, and final answers, and writes `docs/audits/local_model_eval.md`.
- Target: at least one widely available open-weights model in the 7–14B range completes the core recipes reliably (decision 7). The harness is model-agnostic so results can be compared across models.
- The harness itself is optional to run (it needs a local model server) and is not part of CI.

### J5. No Claude-specific dependencies

The server, docs, and recipes avoid assumptions about a particular client: standard MCP over stdio, plain JSON results, and setup instructions for common local MCP clients alongside Claude Code.

## 11. Out of scope (Round 9 and later)

- **Spatial models and maps.** Spatial autocorrelation (Moran's I) and spatial regression need tract adjacency, which needs TIGER/Line boundaries. Planned with mapping output in Round 9.
- **PUMS microdata** and custom tabulations with person/household replicate weights.
- **ACS 1-year and decennial data.**
- **Bayesian hierarchical models** beyond Fay–Herriot (for example spatial Fay–Herriot, multivariate models).

---

## 12. Implementation roadmap

```text
PHASE 8a: Foundations
  a.1 analysis_frame and replicate attachment (A)
  a.2 simulation harness with realistic ACS error (F1 scaffolding)

PHASE 8b: Models (parallel streams, disjoint files)
  b.1 replicate regression and model-based corrections (B)
  b.2 Fay-Herriot (C)
  b.3 planning tools (E)
  b.4 overlap design checks and trend tests (G)

PHASE 8c: Discipline and proof
  c.1 manifests and methods-section generation (D)
  c.2 coverage studies and reference results for every method (F)

PHASE 8d: Outputs and interface
  d.1 figures, correlation matrices, tables, results bundles (I)
  d.2 MCP tools, CLI, protocol docs, README (H)
  d.3 tool profiles, design-rule tests, recipes (J1-J3)
  d.4 local-model evaluation harness and first report (J4)
```

8a comes first because every model consumes the analysis frame and every method is validated with the simulation harness.

---

## 13. Decisions

| # | Question | Decision |
| :--- | :--- | :--- |
| 1 | Dependencies | **Decided:** lean on established libraries as required dependencies: `scipy` (statistics), `statsmodels` (regression, GLMs, mixed models, multiple testing), `matplotlib` (figures). Considered and not adopted as dependencies: `seaborn` (convenience over matplotlib; not needed), `scikit-learn` (prediction-oriented), `linearmodels` (panel/IV; revisit if needed), `samplics` (SAE and replicate weights; used only as a test-time cross-check if installed), `pingouin` (less established) |
| 2 | Combining replicate and model variance | **Decided, then revised by validation:** all three SEs are reported, but intervals use `target="population"` (model SE, coverage 0.896) by default, or `"these_areas"` (replicate SE, 0.895). The sum (`"conservative"`) double-counts outcome sampling error and over-covers (0.961) |
| 3 | Recording pre-registration hashes | **Decided:** commit the manifest to the repository; external timestamping (for example OSF) optional |
| 4 | High-CV observations | **Decided:** keep and report by default; manifests may exclude explicitly |
| 5 | Fay–Herriot validation | **Decided:** reproduce published `sae` worked examples in Python, without R |
| 6 | Figure style | Default: a neutral journal style (serif-free sans fonts, colorblind-safe palette, grayscale-safe markers) with presets for one- and two-column widths; journal-specific presets added on request |
| 7 | Local-model target | Default: evaluate with Ollama against at least one open-weights model in the 7–14B range with tool-calling support (for example a current Qwen or Llama instruct model), chosen at implementation from what is available; tell us if you already run a particular model |

---

## 14. Acceptance criteria

1. Every model result states which uncertainty sources its standard errors include and the method used.
2. Replicate regression reproduces exact replicate variances on synthetic data with known answers.
3. Coverage simulations show 88–92% coverage for nominal 90% intervals for each supported method under ACS-like error; methods that fail are documented and not exposed as defaults.
4. Corrected methods remove the attenuation bias that naive regression shows in simulation.
5. Fay–Herriot EBLUP and MSE reproduce a published reference result to its published precision.
6. A manifest runs end to end from MCP or the CLI, its hash appears in every output, and runs outside it are labeled exploratory.
7. The Round 7 validation battery still passes; CI stays green on Linux and Windows, Python 3.10–3.12.
8. Figures are produced as vector files with a values CSV, caption, and provenance sidecar, and regenerate byte-identically from the same results.
9. Correlation matrices report intervals and, when replicates exist, design-based standard errors.
10. The `core` MCP profile passes the design-rule tests, and the local-model evaluation reports a completion rate for each core recipe with at least one local model.

---

## 15. Implementation status

| Workstream | Files | Result |
| :--- | :--- | :--- |
| Contract | `census/results.py` | `ModelResult`, `SmallAreaResult`, `CorrelationResult`, analysis-frame convention |
| A Analysis frames | `census/analysis.py`, panel `*_se` columns | Derived variables evaluated on each replicate |
| B Models | `census/models.py` | Replicate refit regression (OLS/WLS/GLM), regression calibration, SIMEX, reliability screen; interval targets |
| C Fay–Herriot | `census/sae.py` | Reproduces the R `sae` milk example (sigma2_u 0.0185503 vs 0.01855022; betas and SEs within 2e-6) |
| D Manifests | `census/manifest.py`, `examples/manifests/` | Validate, hash, run, deviations, methods section |
| E, G Planning and trends | `census/planning.py`, `census/trends.py` | MDD, aggregation forecasts, overlap checks, GLS trend test |
| F Validation | `census/simulation.py`, `scripts/inference_validation.py` | Coverage studies; findings below |
| I Outputs | `census/correlation.py`, `census/figures.py`, `census/report_tables.py` | Reproducible journal figures, LaTeX/Markdown tables, results bundles |
| H, J Interfaces | `census/workflows_inference.py`, `census/mcp_profiles.py`, `census/recipes.py`, `scripts/local_model_eval.py` | 44 tools (13 in `core`), recipes, design-rule tests, local-model harness |

**Validation findings that changed the plan.** (1) Adding model and replicate variance (decision 2) double-counts outcome sampling error: coverage 0.961. Intervals now default to the model SE for population inference (0.896) or replicate SE for these-areas inference (0.895). (2) Quadratic SIMEX under-corrects attenuation and under-covers (0.81; 0.29 at reliability 0.38); it is documented as not validated and regression calibration (0.89) is the recommended correction. (3) Replicate refits do not correct attenuation from noisy predictors (coverage 0.54 in that scenario). (4) Correlations among noisy estimates are attenuated: intervals cover the observed-estimate correlation (0.895) but not the true-value correlation (0.45); disattenuation is reported alongside.

**Local-model evaluation.** Qwen3 8B (CPU with partial GPU offload) passed 5 of 6 tasks through the `core` profile, about 5–7 minutes per task. The failure (a multi-step question answered without the recipe) led to a recipe-first instruction; the run also exposed a misleading overlapping-trend error, now fixed.

**Code review (PR #2).** Ten findings: nine fixed with regression tests (`tests/test_review_fixes_r8.py`), including a Fay–Herriot collapse when one area has zero sampling variance, non-JSON tool results, and SEs in tables that did not match the p-values. One is partial: pipeline outputs store controlled-estimate MOEs (-555555555) as missing rather than 0, because the Round 7 F1 check requires every sentinel to be removed; `census.stats.moe_to_se` now applies one rule everywhere it sees the raw code.

**Carried to Round 9:** controlled-estimate MOEs preserved as 0 through the pipeline (with F1 updated to match); spatial models and maps; slimmer core wrapper for `batch_download` (26 parameters); native `summary`/`next_step` in Round 7 tools; tract-level CV profiles from real tract MOEs for the simulations (currently assumed); rerun of the local-model evaluation with an 8K context.

