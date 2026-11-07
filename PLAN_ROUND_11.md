# Round 11: Statistical Correctness and Audit Follow-up

**Date:** 2026-11-05
**Status:** Plan drafted; first systematic evidence pass complete; integration reproductions and policy decisions pending
**Context:** Audit of the existing Round 9 implementation and intentional Round 10 working-tree changes

## 1. Objective and boundaries

Resolve concrete scientific correctness defects and define the remaining reliability decisions without superseding Round 9 or Round 10. Findings describe the working tree inspected during the audit, not a released version. Revalidate each finding after Claude Code resumes: code and line numbers may change.

The user authorized creation and iterative refinement of this plan and the next-round draft, PLAN_ROUND_12.md, including parallel read-only reviews. This does not authorize source fixes, new test files, dependency installation, live calls, data regeneration, commits, or pushes. Only those two planning files may be edited during this follow-up. PLAN_ROUND_9.md and PLAN_ROUND_10.md remain untouched. Treat all other modified and untracked files as intentional work in progress.

Future implementation must be coordinated with the owner of Round 10, use the current intended baseline, and preserve existing edits. Do not implement a competing model, feature, or migration. No task below is complete merely because a proposed fix appears straightforward.

## 2. Evidence and limitations

The original audit used source and test inspection plus isolated in-memory arithmetic. Selected functions were extracted through Python AST rather than importing the package. No application or live endpoint was run. Python 3.14 and NumPy are available locally; pandas, SciPy, and statsmodels were unavailable. Full pytest and integration results are therefore **not established**.

Confirmed defects are distinguished from risks and policy questions. The ACS successive-difference factor 4/80 is consistent in the reviewed shared variance, PUMS regression, and Moran calculations. No confirmed error was identified in the reviewed RPP scaling or inflation-factor direction. Documented conservative Moran intervals and incomplete Round 10 integration are not defects by themselves. No critical security exploit was established; this is not a security certification.

## 3. Prioritized work inventory

| ID | Severity/type | Work | Relationship | Implementation status |
| :--- | :--- | :--- | :--- | :--- |
| C1 | High / defect | Undefined PUMS replicates must not imply zero uncertainty | Round 10 A/B | Fixed; tests/test_audit_r11.py::test_c1_* |
| C2 | High / defect | Weighted medians must ignore zero-weight insertions | Round 10 A/B | Fixed; tests/test_audit_r11.py::test_c2_* |
| C3 | High / defect | Reject ambiguous spatial replicate release alignment | Round 9 spatial inference | Fixed; tests/test_audit_r11.py::test_c3_* |
| C4 | Medium / defect | Align scalar/MCP Census sentinel handling with pipeline | Round 9 controlled MOEs | Fixed; tests/test_audit_r11.py::test_c4_* |
| C5 | Medium / defect | Handle missing PUMS grouping keys as documented | Round 10 A | Fixed; tests/test_audit_r11.py::test_c5_* |
| C6 | Medium / defect | Validate or correctly difference post-treatment DiD references | Round 10 D | Fixed (D5: pre-treatment reference required); tests/test_audit_r11.py::test_c6_* |
| C7 | Medium / guidance defect | Report the uncertainty basis actually used; qualify SIMEX guidance | Existing inference recipes and wrappers | Fixed; tests/test_audit_r11.py::test_c7_* |
| R1 | Medium / risk | Clarify and enforce intended runtime price-data integrity | Existing inflation; Round 10 E | Decided 2026-11-07: kept as a documented risk; integrity verified at generation and by tests, not at each load (docs/agent_protocol.md section 6) |
| R2 | Medium / risk | Characterize PUMS peak-memory amplification | Round 10 A | Measured 2026-11-07: ~3.6 GB peak at California scale with needed columns; warning added for all-column reads of files over 200 MB |
| Q1 | Question | Define DiD serial-correlation inference scope | Round 10 D | Measured 2026-11-07: coverage 0.896-0.905 under AR(1) rho 0.5 and 0.8 (study_did_serial); no estimator added; CR1 cluster sandwich measured and not better; independence assumption documented. 2-period rho=0 cell rechecked at 3,000 reps: 0.900 |
| Q2 | Question | Define policy for incomplete model replicates | Existing inference; Round 10 D | Decided 2026-11-07: replicate_policy strict for manifests (confirmatory), flag otherwise with an understated-SE note (tests/test_audit_decisions.py) |

## 4. Confirmed defects and acceptance criteria

### C1. Undefined PUMS replicate statistics

**Evidence:** `census/pums.py:579-585`, `_finish`, uses `np.nansum` for squared replicate deviations. `_safe_div` at line 525 returns NaN for a zero denominator. A finite full estimate of 10 with 80 undefined replicate estimates produces SE 0 and MOE 0. One undefined replicate is silently omitted while retaining the factor 4/80. The shared `census/variance.py:77` instead propagates NaN through `np.sum`.

**Why wrong:** Undefined replicate statistics are not evidence of no sampling error. This can silently produce false precision.

**Expected behavior:** Reject incomplete replicate statistics or return explicitly undefined uncertainty. Any alternative handling requires a documented statistical justification; do not renormalize or impute silently.

**Likely future targets:** `census/pums.py`, `tests/test_pums.py`; inspect the shared variance contract before choosing a local change.

**Regression criteria:** Cover all-undefined and partially undefined replicates; zero replicate denominator with a valid full denominator; ordinary complete replicates; grouped and ungrouped statistics. Undefined uncertainty must not become zero MOE or a reassuring reliability classification. Construct expected results independently.

**Confidence:** Very high for the arithmetic defect. Full public-API reproduction remains F1.

### C2. Zero-weight records change a weighted median

**Evidence:** `census/pums.py:530-555`, especially lines 553-554. At an exact half-total boundary, the implementation averages the current value with the next record, irrespective of the next record's weight.

**Reproduced example:** Values [0, 100, 200] with weights [1, 0, 1] return 50. Removing the zero-weight record gives [0, 200] with weights [1, 1] and returns 100 under the documented midpoint convention.

**Expected behavior:** Zero-weight insertion must not change the weighted distribution. At a halfway boundary, use the next positive-weight observation under the selected median convention. Assess negative replicate weights separately rather than silently extending this rule to them.

**Likely future targets:** `census/pums.py`, `tests/test_pums.py`.

**Regression criteria:** Leading, internal, trailing, and repeated zero weights; exact-half boundaries; tied values; full and replicate columns; all-zero total remains undefined. Existing `test_weighted_median_exact_half_and_bins` does not cover zero-weight invariance.

**Confidence:** Very high; the extracted function was executed in memory.

### C3. Ambiguous spatial replicate alignment

**Evidence:** `census/spatial.py:207-217`, `_replicate_matrix`, filters by variable and then `drop_duplicates("_gid")`, retaining the first area row without selecting a release. `morans_i` consumes the resulting matrix at lines 305-313. The file workflow in `census/workflows_geo.py:182-185` supplies a requested year to boundary handling but passes the replicate frame through without selecting its year.

**Example:** A 2023 estimate frame and a replicate frame containing 2018 and 2023 rows for the same areas/variable can use 2018 replicates if those appear first. Reordering rows changes inference.

**Expected behavior:** Require unique, release-consistent area/variable replicate rows or explicitly select and validate the intended release. Do not resolve conflicting duplicates by file order. Preserve valid single-release inputs without a Year column where that contract is supported.

**Likely future targets:** `census/spatial.py`, affected geography workflow, `tests/test_spatial.py`, and workflow tests if the contract changes.

**Regression criteria:** Conflicting duplicate GEOIDs; two releases in reversed order; missing areas; shuffled valid rows; Moran SE, LISA stability, and spatial regression paths that share this helper. Decide whether identical duplicates also fail. Existing tests compare complete single-release replicates against PySAL but do not test ambiguous replicates.

**Confidence:** Very high from source tracing; no PySAL integration execution.

### C4. Scalar/MCP sentinel semantics diverge

**Evidence:** `census/stats.py:136-175`; `mcp_server.py:867-877` delegates to the scalar `calculate_cv`. `standard_error` takes the absolute MOE without sentinel decoding. The array helper `stats.moe_to_se` and cleaned pipeline recognize controlled and missing codes.

**Reproduced example:** `calculate_cv(1000, -555555555)` gives SE approximately 337723741.64, CV approximately 33772374.16%, reliability Low, and interval [-555554555, 555556555]. The pipeline convention for that controlled MOE is zero uncertainty. The missing code -222222222 also gives a huge finite SE.

**Expected behavior:** Controlled MOE becomes zero; missing/suppressed MOE yields undefined uncertainty. Preserve deliberate compatibility for ordinary negative MOEs only after distinguishing them from sentinel codes. Check sentinel estimate inputs too.

**Likely future targets:** `census/stats.py`, `tests/test_stats.py`, `tests/test_controlled_moe.py`, relevant MCP tests. Do not change shared signatures unnecessarily.

**Regression criteria:** Table-driven scalar/array/cleaned-frame/MCP parity for controlled, suppressed, missing, zero, normal positive and ordinary negative MOEs. Existing scalar tests explicitly accept -1645; retain or deliberately document that behavior. No fabricated finite interval from a missing code.

**Confidence:** Very high; selected scalar functions were executed in memory.

### C5. Missing PUMS grouping keys produce float indices

**Evidence:** `census/pums.py:501-522`, `_groups`, `_group_sum`, `_group_count`. `ngroup` with excluded missing keys produces float codes containing NaN. Filtering NaNs does not convert remaining labels to integer indices. NumPy array indexing and `bincount` require integers.

**Example:** Group labels [0.0, NaN] leave [0.0] after filtering; `np.bincount` raises TypeError. This downstream failure was reproduced with NumPy. The full pandas path has not been executed locally.

**Expected behavior:** Drop missing-key rows as `tabulate` documents, then aggregate valid integer group codes with accurate counts.

**Likely future targets:** `census/pums.py`, `tests/test_pums.py`.

**Regression criteria:** One missing key, multiple grouping keys, all missing keys, no observed groups, and crosstab paths. Verify actual retained estimates, counts, and replicate SEs rather than merely absence of an exception. Existing missing-value test addresses the outcome, not grouping keys.

**Confidence:** High; source-established pandas path and reproduced NumPy failure. Full public-API reproduction remains F1.

### C6. Post-treatment reference in two-group DiD

**Evidence:** `census/did.py:109-111` accepts any observed reference. Lines 128-138 difference outcomes against it but construct the effect column as treated * post rather than treated * (post - post_reference).

**Example:** With one pre and one post period and a post-period reference, retained rows are pre-period rows, making the treatment column identically zero. The design is singular. The design arithmetic was reproduced in memory; no full model fit was run.

**Expected behavior:** Either reject post-treatment references clearly or difference the regressor consistently. Choose the documented contract before implementing. Recheck event-style interpretation separately; do not assume the two-group fix applies unchanged.

**Likely future targets:** `census/did.py`, `tests/test_did.py`.

**Regression criteria:** Explicit pre reference, default reference, post reference, nonexistent reference, and independent fixed-effects/design-matrix comparison. Existing tests use the default pre-treatment reference.

**Confidence:** Very high for the design defect.

### C7. Regression guidance contradicts the chosen uncertainty target

**Evidence added during parallel planning review:** `census/models.py:178-206` maps population to se_model, these_areas to se_sampling, and conservative to se_total. `census/report_tables.py:176-185` already uses `interval_se_column` for the reported SE. However, `census/workflows_inference.py:194` and `docs/recipes/regression_sampling_error.md:10` unconditionally tell users to report se_total. Recipe line 9 also offers SIMEX as a fallback without the confirmatory-use qualification stated in README's Inference section.

**Why wrong:** A reader can report an SE that does not support the displayed CI/p-value, or infer that a method with documented undercoverage is an interchangeable confirmatory fallback. This is a defect in instructions, not proof that the underlying result table is calculated incorrectly.

**Expected behavior:** Guidance identifies the result's actual SE basis and target, preserves method-specific uncertainty caveats, and distinguishes exploratory SIMEX use from validated confirmatory alternatives. Reuse `interval_se_column`; do not hard-code a second target mapping. Do not claim that replicate refits eliminate bias from noisy covariates.

**Likely future targets:** Existing regression recipe, `census/workflows_inference.py`, focused workflow/recipe tests. Broader output interpretation belongs to Round 12 B.

**Regression criteria:** Population/these_areas/conservative examples with deliberately different SE columns yield guidance consistent with the serialized result, table and figure basis. Method-specific corrected models retain their actual basis. Recipe guidance does not recommend SIMEX for confirmatory use. Assert scientific meaning, not the entire wording of prose.

**Confidence:** Very high from direct source comparison; no wrapper or export execution performed.

## 5. Risks and design questions

### R1. Runtime integrity of price data

`census/inflation.py:55-62` and `census/rpp.py:66-78` parse numerical CSVs without validating sidecar hashes. Valid-looking local corruption can change scientific results while original source metadata remains available. This is a risk, not evidence of corrupted data.

`tests/test_inflation.py` and `tests/test_rpp.py` do verify shipped checksums. Resolve whether integrity is guaranteed only at generation/test time or also at runtime. If runtime verification is adopted, define custom-data behavior, cache invalidation, error reporting, and verification cost before editing loaders. Use temporary synthetic data for tampering tests; never modify real price files. Follow-up F2.

### R2. PUMS memory amplification

`census/pums.py:213-228` materializes response content; lines 311-322 retain parsing chunks before concatenation and conversion; `_weight_matrix` creates an 81-column float64 array. Chunk size does not bound total memory. At 1.9 million records the weight matrix alone occupies 1231200000 bytes (about 1.23 GB / 1.15 GiB), excluding frames, strings, and temporaries.

No peak-memory benchmark has been run. Preserve the Round 10 decision to download full state files on demand. Characterize realistic resource costs before proposing streaming or architectural changes. Follow-up F3.

### Q1. DiD serial correlation

`census/did.py:5-20` explicitly assumes independent area-by-period model errors. Non-overlapping ACS periods remove shared sample years, not temporal correlation in economic shocks. This is a scope/design question, not a demonstrated algebra error. Decide whether to document the restricted model, add sensitivity diagnostics, or later support serial covariance. Coverage claims must state the generating process. Follow-up F4.

### Q2. Incomplete model replicates

`census/models.py:258-273` holds incomplete replicate rows at full-sample values; the result reports understatement warnings. Similar behavior exists in multilevel and DiD refits. It is deliberate and disclosed, unlike C1's silent NaN summation. Decide whether confirmatory/these-areas inference should fail, require explicit opt-in, or retain the existing warning policy. Quantify the impact before changing defaults. Follow-up F5.

## 6. Systematic follow-up register

Each follow-up records evidence, outcome, remaining dependency, and next action. A planning follow-up can be resolved while implementation remains not started. Do not equate a proposed validation with an executed test.

| ID | Question / evidence needed | Status | Next action |
| :--- | :--- | :--- | :--- |
| F1 | Reproduce C1-C6 through public APIs; distinguish arithmetic from integration evidence | Partial: arithmetic rechecked; integration pending environment | Use an existing supported environment with dependencies, once its use is authorized |
| F2 | Is runtime integrity promised, and what does existing checksum coverage establish? | Evidence pass complete; runtime policy unresolved | Owner decides runtime verification contract; no current checksum mismatch found |
| F3 | What memory bounds can be established without large allocations or downloads? | Static bound complete; measurement pending | Benchmark only after scope/resource approval; no large allocation performed |
| F4 | Do DiD coverage studies test serially correlated model errors? | Evidence pass complete: reviewed generators use independent shocks | Decide documented model scope versus a future serial-correlation sensitivity study |
| F5 | Where are missing replicate rows held fixed, and how is this disclosed? | Evidence pass complete; policy unresolved | Decide strictness for confirmatory inference before changing defaults |
| F6 | Do guidance and outputs name the same uncertainty basis? | Source pass complete: C7 added | Run target-specific wrapper/table/figure checks in an authorized environment |

### First evidence pass: 2026-11-05

**F1 — Reproduction readiness.** Rechecked module availability without package imports: NumPy available; pandas, SciPy, statsmodels, and esda unavailable. Re-executed the AST-extracted weighted median: [0,100,200]/[1,0,1] gives 50; [0,200]/[1,1] gives 100. Rechecked the C1 expression with 80 NaNs: SE is 0. These confirm arithmetic/function behavior, not end-to-end tabulation or model results. C4 scalar behavior, C5 downstream NumPy type failure, and C6 differenced-design arithmetic were reproduced in the original audit; they were not rerun as integration tests in this pass. C3 remains source-established. No dependencies were installed and no pytest invocation was made. Remaining public-API cases are specified in section 4; run them only in a suitable authorized environment.

**F2 — Integrity evidence and scope.** Read README section Methodology, `docs/agent_protocol.md:112-124`, both loaders, and checksum tests. The reviewed documentation describes source checksums but does not explicitly promise hash verification at each load. Do not upgrade R1 to a confirmed broken runtime promise. Independently hashed existing CSV bytes with the standard library: `data/price_indexes/cpi_u.csv`, `c_cpi_u.csv`, and `r_cpi_u_rs.csv` match each adjacent JSON's `csv_sha256`; `data/price_parities/rpp.csv` and `pce_price_index.csv` match `rpp.json`'s `csv_sha256` mapping. This checks five current CSVs, not every source archive, provenance field, or future load. No corruption was found in those checks. Still unresolved: runtime verification policy and treatment of intentionally supplied custom tables/directories.

**F3 — Memory bound and lifetimes.** Recomputed 1900000 * 81 * 8 = 1231200000 bytes = 1.1466443539 GiB for one tabulation weight matrix. The download path materializes the compressed response before returning it; the reader then retains parsed chunks and creates concatenation/conversion output. These are different phases: do not add every allocation as if all were guaranteed to coexist. Within parsing, retained chunks and concatenation overlap; within tabulation the input frame and converted matrix coexist. The static lower bound does not establish peak RSS. A future benchmark should record compressed bytes, row/column counts, selected columns, chunk size, baseline/peak RSS for each phase, platform and dependency versions, and verify equal estimates across chunk sizes. Prefer bounded synthetic inputs first; no download or large benchmark was performed.

**F4 — DiD coverage scope.** `census/simulation.py:663-666` generates a fresh independent model-error vector for each period, in addition to a shared area intercept. The shared intercept cancels under differencing; this is not a test of serially correlated residual shocks. The two-group and event studies at lines 674-713 add independent sampling noise by period. `tests/test_did.py:155-157` is only a fast smoke gate (coverage > 0.75), not evidence for the full 0.88-0.92 target or serial-correlation robustness. Its Card-Krueger checks validate the separate `did_from_changes` helper, not the complete GLS/REML panel estimator. If a serial-correlation sensitivity study is approved later, specify the residual process (e.g. several positive AR(1) correlations), independent versus correlated sampling error, nominal level, Monte Carlo SE, and estimand before judging coverage. No new coverage results are claimed.

**F5 — Missing-replicate policy traced.** `census/models.py:258-273` replaces an entire area/variable replicate row with its full-sample value when any of its 80 entries is nonfinite; warnings are attached at lines 350-355. `census/multilevel.py:506-557` reuses that helper and reports the fixed-row counts. `census/did.py:294-327` implements its own unit-period fallback and adds an understatement note. Default zero tolerated failed refits does not prevent these fallbacks: missing input rows are treated as fixed before refitting rather than counted as failed fits. Thus a these-areas target plus a nonempty replicate frame does not establish complete replicate coverage. This remains a disclosed policy concern, not a claim that a new feature is missing. A future policy decision should distinguish truly fixed predictors, unavailable replicates for stochastic variables, and partially missing replicate rows. Recommended future tests: complete versus removed/partially missing rows, warning/count assertions, all-fallback inputs, and any explicit strict-mode contract. No strict mode or new defaults were implemented.

**F6 — Guidance follow-up: 2026-11-05.** Three parallel read-only reviews examined scientific evidence, researcher workflows, and maintainability. The parent independently checked the C7 source locations. The existing reporting helper already selects the interval SE; the correction should be local to contradictory guidance rather than another result schema. Recipe executability and complete methods text remain Round 12 work. Recommendations from the reviews are proposals, not external statistical verification or executed tests.

### Remaining dependencies and next pass

- **Environment dependency:** Public-API reproduction and test execution require an existing supported environment or separately authorized dependency setup. Do not mark F1 complete on arithmetic alone.
- **Owner decisions:** R1 runtime verification, Q1 DiD scope, Q2 incomplete-replicate strictness, and C6 reference contract require explicit recorded choices. The proposed tests do not silently choose those policies.
- **Measurement dependency:** R2 peak RSS requires an approved bounded benchmark; static arithmetic alone cannot resolve it.
- **Coordination dependency:** Revalidate source locations and outcomes against Claude Code's resumed Round 10 baseline before implementation. This plan does not reserve or take over those files.
- **Next iteration order:** Complete available public-API reproductions; record policy decisions; implement only a specifically approved defect scope; run focused regressions and applicable offline validation; update status with actual results.

## 7. Future implementation sequence and verification

1. Reconcile this inventory with the resumed Round 10 baseline and obtain approval for a specific fix scope.
2. Reproduce C1 and C2, then C3. Add independent mathematical or invariance checks before minimal fixes.
3. Address C4 sentinel parity, C5 grouping, C6 reference handling, and C7 target-sensitive guidance with focused regressions.
4. Resolve R1/Q1/Q2 policy questions explicitly. Measure R2 before selecting an architectural remedy.
5. Run relevant tests in an existing supported Python 3.10-3.12 environment; do not infer success from isolated arithmetic. With implementation authorization, broaden to the offline suite and concordance validation after focused checks pass. Keep live tests disabled unless separately authorized.
6. Use existing frozen Census/reference fixtures and applicable simulation studies to verify scientific behavior. Fast smoke thresholds alone do not establish nominal coverage. Do not regenerate fixtures or weaken acceptance thresholds to make checks pass.
7. Record changed files, exact checks/results, baseline failures versus regressions, remaining limitations, and Round 10 coordination needs here or in the approved handoff. No commits or pushes without permission.

## 8. Completion criteria

- Each defect is revalidated against the intended baseline and has a recorded disposition: an independent regression test plus a minimal reviewed fix; evidence explaining why it no longer applies; or an owner-accepted release limitation with its consequences recorded. An accepted limitation remains unresolved behavior, not a fixed defect or proof of a clean baseline.
- Risks are measured or have explicit, documented policy decisions; questions are not silently treated as defects or implemented defaults.
- CI-relevant offline checks pass on the supported matrix, or failures are explicitly documented and attributed.
- All scientific claims distinguish exact formula checks, fixture reproduction, and simulation coverage.
- Round 9/10 plans and unrelated working-tree content remain preserved.

## 9. Execution packages and coordination

This is an execution design for a later authorized implementation, not permission to start it. Preserve C1-C7 identifiers in review and handoff notes. Relative size means review complexity, not a promised schedule: small = localized behavior/doc change; medium = several consuming paths or a policy boundary.

| Package | Scope and output | Dependencies | Likely targets | Size / owner boundary |
| :--- | :--- | :--- | :--- | :--- |
| S0 Baseline | Record intended revision, existing edits, supported environment and relevant preexisting failures; reproduce findings before fixing | Round 10 handoff | Read-only inspection; authorized temporary test outputs | Small; one coordinator |
| S1 PUMS | C1, C2, C5 as individually reviewable fixes with independent edge cases | S0; median weight-domain clarification | pums.py and its current tests | Medium; one owner because fixes share the file |
| S2 Spatial alignment | C3 and direct tests for all consumers of the helper | S0; release-key contract | spatial.py, affected geo wrapper/tests only if needed | Medium; coordinate with Round 10 geography work |
| S3 Sentinel parity | C4 and raw-versus-cleaned input contract | S0 | stats.py and scalar/MCP tests; trace variance helper before touching it | Small to medium; no broad numeric cleanup |
| S4 DiD reference | C6, a recorded reference contract, independently constructed design check | S0; reference decision | did.py and its tests | Small to medium; coordinate with Round 10 designs |
| S5 Guidance | C7, actual target/basis in existing next steps and regression recipe | S0; preserve current model contracts | inference workflow, recipe, focused tests | Small; no new tools or model options |
| S6 Evidence closeout | Resolve or explicitly defer R1/R2/Q1/Q2 with reasons; focused checks, applicable offline validation, handoff | S1-S5 and supported environment | Existing audit/validation paths only when approved | Medium; do not turn risks into mandatory new subsystems |

Default implementation order is S0, S1, S2, S3, S4, S5, S6. S5 may move earlier for immediate documentation value. Parallel review of independent packages is useful; concurrent edits to the same source/test file are not. Implementation branches are a future decision based on the then-current checkout, not a reason to switch away from the active Round 10 work now.

For each package retain: reproduction input, independent expectation, before/after behavior, affected callers, test results, compatibility impact, remaining limitation, and rollback scope. A documentation-only fix need not acquire a broad test framework. If a fix needs a new dependency, new public tool, or substantial model refactor, stop that package at an evidenced proposal and reconsider its scope.

## 10. Proposed decisions, not adopted defaults

| Decision | Recommended starting point | Compatibility and evidence needed |
| :--- | :--- | :--- |
| D1 Undefined tabulation replicates | Preserve a finite point estimate with undefined SE/MOE and an explicit reason, or reject where the existing API cannot represent this safely | Never silently substitute zero or rescale 4/80; validate flags and JSON behavior |
| D2 Median weight domain | Ignore zero-weight records for midpoint selection; preserve the documented median convention | Establish release-specific handling of negative replicate weights before extending nonnegative-distribution reasoning; do not infer validity from synthetic tests or clip weights |
| D3 Spatial duplicates | Reject duplicate area/variable rows by default; where a year is explicitly available, select and validate it before uniqueness checks | Decide identical-duplicate behavior; no silent first-row selection or heuristic year guessing |
| D4 Scalar MOEs | Decode Census sentinels before scalar conversion; preserve explicitly supported ordinary negative-MOE behavior unless intentionally changed | Trace callers of stats.moe_to_se versus variance.moe_to_se; raw input decoding and cleaned mathematical primitives need not be identical APIs |
| D5 DiD reference | Initially require a pre-treatment reference for the two-group API unless arbitrary references are an intended supported requirement | Explicit error before fitting; event-style reference semantics assessed independently; no new estimator |
| D6 Runtime price integrity | Keep R1 as a documented risk until the runtime/custom-data guarantee is chosen | Existing shipped CSV hashes match; do not imply current corruption or force a new verification service |
| D7 Incomplete inference replicates | Prefer an explicit strict contract for confirmatory inference, with documented exploratory fallback if retained | Distinguish fixed covariates, missing stochastic variables, missing rows, and failed fits; defaults require a compatibility decision, not a silent switch |
| D8 DiD serial shocks | State current independent-shock assumptions and limits; consider a bounded sensitivity study before another model | No claim that non-overlap alone justifies serial independence; avoid adding cluster/serial estimators without a separate scientific design |

The final choice can differ if Census specifications or the intended API require it. Record the rationale and supporting evidence; this table is not a statistical source. External verification remains a later separately authorized activity.

## 11. Scientific contracts to preserve

- **Raw versus cleaned values:** Recognize sentinels at ingestion boundaries. Do not globally reinterpret mathematical primitives that deliberately accept already-cleaned numbers without tracing callers.
- **Identity:** Match on GEOID, variable, and release/product where present; validate geography vintage at the boundary that has that context. Do not repair lost leading zeros by guessing or silently pick among conflicting identities.
- **Missingness:** Distinguish a true zero, a controlled estimate, unavailable uncertainty, excluded records, and unavailable replicates. Row counts and uncertainty counts answer different questions.
- **Inference:** The estimand, selected SE, confidence level, and interval distribution/df must agree. Do not reconstruct every CI as normal when a fitted method reports t-based inference.
- **Oracles:** Hand calculations, exact reference matrices, and invariances must not call the production helper being checked to generate their expected values. Published count verification alone does not validate means, medians, or regressions.
- **Interfaces:** Compare shared Python workflow, CLI and MCP paths for operations they actually share. The maintenance-mode GUI does not need all new inference features.
- **Compatibility:** Preserve valid supported results within declared numerical tolerances. Deliberate changes in invalid-input behavior must have clear errors/notes and targeted regression evidence.

## 12. Verification tiers and release decision

| Tier | Purpose | Evidence required | Limits |
| :--- | :--- | :--- | :--- |
| T0 Source/arithmetic | Localize failure and construct an expected answer | Explicit assumptions and function/expression executed | Not public-API or integration success |
| T1 Focused offline regression | Verify approved fixes through consuming APIs | Deterministic cases, invariances, warnings/errors, result serialization | Does not establish broad coverage |
| T2 Offline references/integration | Detect scientific drift and adapter divergence | Existing frozen Census/reference checks and applicable workflow tests | Synthetic and published evidence must be labeled separately |
| T3 Full seeded studies | Support specified nominal coverage claims | Estimand, generating process, seed, independent repetitions, MC SE, fixed acceptance rule, environment/revision | Smoke thresholds and repeated reruns are not substitute evidence; correlated terms/areas are not independent repetitions |
| T4 Optional live drift checks | Verify endpoint/schema/source changes | Separate authorization and clear source/date | Network failures are not numerical regressions; not needed to implement these fixes |

Run the smallest meaningful T1 checks first, then applicable T2 checks. Run T3 only for a changed estimator or unresolved scientific concern. Keep current 0.88-0.92 coverage rules unless a scientific decision changes them before results are seen. Do not tune seeds, broaden bands, or relabel failed gates after inspecting outcomes. Keep informational/sensitivity rows distinguishable from claims being gated.

Before treating a command as a release gate, verify that it signals gated failures to its caller. `scripts/inference_validation.py:38-49` distinguishes FAIL from info in its report, while `main` at line 121 currently returns zero. This source fact does not prove that the report command was intended as a CI gate; Round 12 A must establish report-versus-gate semantics rather than silently changing compatibility. No report command was executed here because its default full run writes repository artifacts.

Round 11 is ready to close when confirmed findings are fixed and tested, shown inapplicable on the intended baseline, or explicitly accepted as release limitations by the owner with their consequences recorded. Do not declare a clean scientific baseline while a high-severity defect remains unexplained. Risks can remain deferred with documented boundaries; optional feature work is not required for closure. Round 12 implementation is a separate scope decision.

## 13. Scope limits and handoff to Round 12

Round 11 adds no estimator families, providers, public MCP tools, mandatory dependencies, storage format, or replacement orchestration layer. Prefer existing test modules and one shared helper only when at least two reviewed consumers require identical behavior. Do not standardize every result/error across the repository merely because a touched path needs a fix.

The next-round draft, [PLAN_ROUND_12.md](PLAN_ROUND_12.md), proposes three bounded improvements: independent validation evidence, consistent interpretation of existing results, and one executable research workflow. These are improvements to existing research capabilities, not a continuation of every possible feature in earlier plans. Incomplete Round 10 PUMS, 1-year, RPP, map, or design integration stays with Round 10.

Planning review used three parallel read-only reviewers. No review produced code changes, dependency installations, integration results, or external source verification. Remaining decisions are visible above; no empty follow-up is marked complete merely to finish the plan.

**Current handoff:** Planning only. No source fixes, test additions, dataset updates, integration tests, commits, or pushes have been performed for Round 11.
