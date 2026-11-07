# Round 12: Verifiable and Reproducible Research Workflows

**Date:** 2026-11-05
**Status:** Implementation authorized by the owner (2026-11-07); Round 11 closed (C1-C7 fixed; R1/R2/Q1/Q2 decided, see PLAN_ROUND_11.md)
**Prerequisite:** Reconcile the final Round 10 baseline and close or explicitly disposition Round 11's relevant correctness findings
**Companion:** [PLAN_ROUND_11.md](PLAN_ROUND_11.md)

## 1. The improvement this round should deliver

A researcher should be able to use an existing supported analysis, understand exactly what its interval means, and hand its outputs to a second researcher who can check the result. A maintainer should be able to verify that behavior without maintaining another analysis engine or an expanding catalog of examples.

This round improves the usefulness of the capabilities already present. It does not add estimator families or finish features reserved to Round 10. The recommended core is three workstreams: independent scientific evidence, consistent interpretation, and one executable reference workflow. Work on one implementation slice at a time, each ending in a usable reviewable result.

The user authorized planning and parallel read-only review. During this planning task only PLAN_ROUND_11.md and this file may change. Do not install dependencies, access external services, run live endpoints, change datasets, generate reports into the checkout, or change source/tests. Implementation, environment setup, external verification and release actions remain future scope decisions. Earlier planning files and all unrelated working-tree edits remain protected.

## 2. Research outcomes and evidence of need

| Desired outcome | Existing foundation | Why improvement is worthwhile |
| :--- | :--- | :--- |
| A reported number can be checked independently | Frozen Census/reference fixtures, simulation harness, ordinary pytest tests | Some tests compare implementations with themselves or cover simpler special cases than the public claim |
| A reader knows which uncertainty supports an interval | ModelResult, diagnostics/notes/provenance, interval_se_column, report tables | Round 11 C7 demonstrates that correct calculations can be paired with contradictory instructions |
| Another researcher can reproduce the substantive analysis | Manifests, hashes, bundles, extraction replay, recipes, fake clients | Individual capabilities need one verified path with exact inputs, transformations and supported artifacts |
| Maintenance grows slower than scientific value | Shared Python workflows, CLI/MCP adapters, plain result objects | Extending those contracts is cheaper than another interface, workflow runner, or metadata framework |

Success is measured in correct interpretation, independent evidence, and a complete workflow, not a higher tool count. Do not claim a measured usability improvement until someone has actually followed the example; this plan defines acceptance exercises rather than reporting their outcomes.

## 3. Admission rule and scope budget

An item enters the round only if it has a concrete researcher problem, an existing extension point, an independent validation strategy, a bounded maintenance cost, and an explicit stop condition. Record an existing alternative before adding capability. Prefer deleting contradictory instructions or duplicated mappings over adding options.

**Proposed core budget:** Three workstreams, one canonical workflow, no new required dependencies, no new public MCP tools, no new estimator families or data providers, no new bundle/manifest format, and no new persistence or orchestration layer. Implement the contracts needed by selected paths, not a universal framework. Existing commands may need a narrowly reviewed correction; do not expand their parameter surfaces by default.

A second workflow is conditional, not a promised deliverable. It is admitted only if the first demonstrably cannot exercise an important existing boundary, can reuse existing fixtures/adapters, and replaces or completes an existing recipe rather than starting an example gallery. If new infrastructure becomes necessary, reduce scope or defer with evidence.

## 4. Baseline and overlap boundaries

- **Round 10 owns:** PUMS and verification, ACS 1-year support, multilevel/DiD model completion, RPP integration, crosswalk/map carryovers, local-model evaluation, and its release/documentation work. Do not duplicate or redesign them here.
- **Round 11 owns:** C1-C7 fixes and their direct regressions, scientific input contracts at affected boundaries, and disposition of R1/R2/Q1/Q2. Round 12 consumes those decisions; it does not silently pick missing-replicate, median-weight, DiD-reference, or runtime-integrity policy.
- **Round 12 owns:** Additional independent evidence beyond direct bug tests, coherent interpretation across existing selected artifacts, and one existing workflow made executable and reviewable end to end.
- **Current architecture is intentional:** `pyproject.toml:55-61` locates resources in the clone/editable install. Standalone-wheel resource packaging and dependency splitting are not assumed defects. The GUI remains in maintenance mode; do not require new-model feature parity.

When resumed code already satisfies an item, record evidence and retain only useful regression/documentation work. Never implement a second equivalent path merely to claim plan completion.

## 5. Workstream A (P0): Independent scientific evidence

### A1. Two targeted reference checks

**Need:** Expected values should not inherit the algebra error they are intended to detect.

**Evidence boundaries observed:**

- `tests/test_did.py:34-46` checks the separate published-change helper, not the complete GLS/REML panel estimator. `census/simulation.py:733-738` constructs one simulation target with production `_prepare`, `_differences`, and `BlockGLS`. This does not invalidate the study; it limits its independence as an algebra check.
- `tests/test_multilevel.py:59-66` reproduces a documented example with zero known sampling variance. That particular test does not establish the nonzero-known-variance extension.
- `census/pums_validation.py:163-167` documents its compressed cell fixtures as count verification. Those fixtures and the current verification path are designed and tested for published counts; they do not establish general mean, median, or regression correctness. Do not broaden published-verification claims beyond that evidence.

**Proposed scope:** Add or strengthen at most two independent reference cases in existing tests: (1) a tiny known-variance GLS/block problem with nonzero heterogeneous sampling variances and nonzero within-block covariance, checked against directly assembled dense matrices; (2) a two-group DiD case with an independently constructed fixed-effects/differenced design, including a reference-period edge case if Round 11 has not already supplied the full oracle. Reuse direct Round 11 PUMS hand/invariance cases instead of starting another validation battery.

The independent reference may use basic matrix algebra, but must not import the production design builder, covariance builder, optimizer helper or result table constructor to generate its answer. Fixed-variance algebra checks validate that layer only; do not describe them as validation of estimated variance components or small-sample corrections. Match the expected estimand and method assumptions explicitly.

**Acceptance:** A reader can derive the expected coefficient/covariance from the supplied small matrices and see why the test would fail under an incorrect reference transformation, omitted covariance term, or misuse of known variance. Tolerances have a numerical justification. Current published-reference tests remain intact. No large new data archive or parameter grid.

**Likely targets:** Existing `tests/test_did.py`, `tests/test_multilevel.py`, applicable existing simulation/reference notes. Source changes only if these cases establish a separately scoped defect.

### A2. Make validation evidence usable as a release decision

Use Round 11's T0-T4 tiers. Keep fast deterministic regressions on ordinary changes; use applicable full seeded studies for release evidence or changed scientific behavior. Record method, estimand, generating process, independent repetitions, nominal level, acceptance rule, Monte Carlo precision, revision, configuration and environment. Count independent simulation repetitions, not correlated areas or coefficients, when quantifying Monte Carlo uncertainty.

`scripts/inference_validation.py:38-49` already distinguishes gated FAIL rows from info rows; line 121 currently returns zero. Decide whether the existing command is a report generator, a gate, or both before changing exit behavior. A future gate must fail when any required study fails or is missing, while informational rows do not fail it. Preserve report-only compatibility if that is the intended contract; use existing script/CI machinery rather than another runner.

Do not infer nominal coverage from `tests/test_did.py:157`'s >0.75 smoke threshold. Do not convert that smoke check into a slow, noisy full-coverage assertion on every PR. Reconcile with any completed Round 10 runner integration before editing.

**Acceptance:** One documented check map identifies the focused offline command for a touched method and the applicable release studies. A controlled synthetic report-result case establishes success/failure/info/missing-study gate semantics without running long simulations in unit tests. Full studies, when actually run, retain failed results and fixed rules; no seed searching or post hoc threshold adjustment.

**Maintenance ceiling:** Existing scripts/test modules and existing CI jobs only. No benchmark service, new CI provider, multi-version dependency resolver, or automatic fixture refresh. Runtime budgets are measured and recorded before they become gates.

## 6. Workstream B (P0): Results that explain their scientific meaning

### B1. One interpretation contract using existing result fields

Reuse `census/results.py:60-89` (ModelResult), `census/models.py:175-206` (target/SE mapping and interval construction), `census/report_tables.py:176-187` (actual SE basis), and existing figure/serialization paths. The aim is consistent use of existing fields, not another result class or metadata language.

For the selected workflow and touched inference paths, a reader should be able to answer:

| Question | Existing location to inspect/reuse | Required treatment |
| :--- | :--- | :--- |
| What quantity is estimated? | Formula, method, diagnostics interval_target, methods text | Name the estimand; an association is not a causal effect |
| Which uncertainty makes the interval? | interval_se_column, coefficient SE fields, CI/p-value | Use the actual selected basis, not always se_total |
| What confidence level/distribution was used? | Result confidence_level, coefficient df and method notes where applicable | Preserve normal versus t/df distinctions; do not reconstruct stored intervals with the wrong distribution |
| What sample contributed? | n_obs, exclusions, replicate coverage and fallback diagnostics | Distinguish retained units from complete replicate coverage; disclose dropped/fixed rows |
| What error remains outside the result? | Notes, uncertainty_sources and provenance | Explain missing predictor uncertainty, design assumptions, RPP error if relevant, and documented approximation limits |
| Where did the data come from? | Existing extraction/manifest/bundle provenance | Preserve release/product/geography and transformation identity where supplied; do not invent unavailable metadata |

Fields already present should be reused. Add a small missing field only after proving it is needed by the selected workflow and cannot be represented honestly in existing notes/diagnostics/provenance. Treat any schema compatibility impact explicitly; do not migrate unrelated result types.

### B2. Agreement across actual consumers

**Acceptance:** For population, these_areas and conservative targets with deliberately unequal SE columns, serialized results, exported tables, figure intervals, captions/methods and next-step guidance agree on basis, confidence level and interpretation. Include an existing t/df result as a reporting check if that path is supported at the resumed baseline. Test content and numerical meaning, not byte equality of every string or image.

Undefined uncertainty remains visibly undefined, not zero-width or an apparently significant result. Missingness and incomplete-replicate decisions come from Round 11, not ad hoc formatting. Broad raw/sentinel normalization remains out of scope beyond those decisions.

Check shared Python workflow, CLI and MCP adapter behavior for the operations exercised here. Normalize timestamps and output paths only for comparison; compare estimates, chosen uncertainty, warnings, and meaningful error codes. Do not force unsupported parameters into the core MCP profile or require GUI modernization. Preserve method-specific uncertainty limitations rather than flattening every method to one claim.

**Likely targets:** Existing result/interval helper only as necessary, `workflows_inference.py`, `report_tables.py`, `figures.py`, and corresponding existing tests/docs. Round 11 C7 should already remove the immediate contradictory instruction; avoid reopening it as a second implementation.

**Stop condition:** One interpretation table and selected consumer tests suffice. If the proposal requires a universal result schema, extensible plugin layer or repository-wide error rewrite, defer that redesign.

## 7. Workstream C (P0): One executable reference workflow

### C1. Finish a path the project already supports

**Recommended first path:** A single-release area-level regression with an explicit estimand, known inputs, an uncertainty/readiness check, and an existing results bundle. Start from `examples/manifests/income_population_tracts.json`, existing manifest tests/fake clients, and the regression/preregistered recipes. The existing example is an analysis-mechanics demonstration, not evidence that population causes household income. Label synthetic data as synthetic and keep conclusions descriptive.

Choose exact variables, units, geography, sample and target before execution. Prefer current frozen inputs and test clients. If those do not provide a defensible real-data example, keep the deterministic example honestly synthetic rather than downloading data or inventing an empirical result. Production data acquisition is a separate use of the existing extraction interface.

The workflow must make all necessary steps explicit: input identity, variable selection, planning, validated local input, any table join and one-row-per-unit check, derivation and units, exclusions/readiness, model/target, table or figure, methods/limitations, and provenance. Joined inputs must be explicit in the example; do not build a generalized join engine to support one recipe. Do not advertise recipe placeholders such as poverty_rate or median_rent unless their derivation and source columns are actually supplied.

### C2. Reuse current artifacts and replay honestly

Existing support includes `census/manifest.py:529-543` (execution and manifest tagging), `:720-729` (exclusions/deviations/sample), `census/report_tables.py:336-343` (bundle artifacts and hashes), and `census/workflows.py:544-559` (extraction replay checks). The deliverable is a verified path through these, not a new bundle format or a second workflow engine.

From existing artifacts, another researcher must identify release/product where available, geography/vintage, exact variables/derivations/units, retained sample, exclusions, estimand, uncertainty basis, corrections/families if used, manifest hash, deviations and limitations. If identity was never provided, say unavailable rather than inferring it from a filename.

Separate three claims: extraction replay checks acquisition outputs; manifest execution reruns an analysis; a bundle preserves results and provenance. None alone guarantees a historical external API will return unchanged bytes. An offline acceptance path must inject the existing frozen/fake source and refuse all network access. Use fresh temporary output directories, preserve original inputs, and compare substantive numerical results. Byte equality is required only where the existing format promises determinism; environment-dependent figure bytes are not the general success criterion.

### C3. Researcher and maintainer acceptance exercises

1. Follow exact documented commands in a supported existing environment using bounded local inputs. No placeholder column names or undocumented editing is needed; expected result and its independent check are supplied.
2. Produce the existing result/table/methods/provenance artifacts in a temporary directory. The meaning of each interval and the limits of the conclusion are stated beside the result.
3. Re-execute from the same frozen inputs into a second directory. Check estimates, uncertainty basis, exclusions, warnings and manifest identity; distinguish stable numerical results from incidental path/time differences.
4. Exercise one meaningful blocked/degraded case, such as incomplete required replicates, using Round 11's adopted policy. It must not be presented as a clean confirmatory result. Existing concordance guards remain intact; never bypass them to make the example run.
5. Compare the shared workflow and the supported CLI/MCP adapters for semantic agreement without live services. Where an adapter does not expose a step, document the supported route instead of adding a tool solely for symmetry.
6. Have a second reviewer follow the instructions and identify estimand, SE basis, exclusions and limitations from the artifacts. Until this exercise is done, report workflow readiness as unverified rather than claiming usability success.

**Likely targets:** Existing regression/preregistered recipes, existing example manifest if needed, `tests/test_manifest.py`, `tests/test_replay.py`, workflow/adapter tests and narrow bundle/report corrections. Reuse fixtures; add at most one small synthetic fixture only if existing ones cannot express the case, with generator/expected values documented. Do not change existing scientific fixture values to fit outputs.

**Optional second path:** Complete the existing county poverty-rate comparison recipe only if a separate scalar-to-derived-rate boundary remains untested. It must reuse existing APIs/fixtures and contribute a distinct independent check. Default scope remains one workflow; maps, PUMS and causal designs do not become extra examples this round.

## 8. Execution order, review and stop points

| Slice | Dependency | Reviewable result | Completion evidence |
| :--- | :--- | :--- | :--- |
| 12.0 Baseline | Final Round 10 state; Round 11 disposition | Confirm which proposals remain necessary, exact supported environment, existing artifacts | Read-only inventory and recorded scope |
| 12.A Independent evidence | Baseline and relevant Round 11 fixes | Two targeted independent cases plus check-tier/release-gate contract | Focused tests; full studies only when scientifically required |
| 12.B Interpretation | Adopted Round 11 input/inference policies | Consistent meaning in selected serialized and human outputs | Unequal-SE and applicable df examples; adapter semantic checks |
| 12.C Executable workflow | A/B for selected path | One complete existing recipe/manifest path and artifact handoff | Frozen-input rerun, blocked case, second-reader review |
| 12.Close | A-C | Short evidence/limitations handoff and deferred decisions | No unsupported validation or reproducibility claims |

Each slice can ship independently after approval and checks. An owner can stop after A or B with useful improvements; do not call the entire round complete unless its agreed core is done. Coordinate shared-file edits serially. Parallel read-only scientific, workflow and maintenance review is useful; parallel editing of the same result helpers is not.

Maintain a short evidence ledger in existing audit documentation when implementation is approved: claim, scope, oracle, configuration/input identity, outcome, uncertainty/limitation, revision, and responsible reviewer. Extend an existing report or table rather than creating a registry, database, or standalone evidence platform. Clear source-backed documentation can be the correct outcome when an optional feature would add more maintenance than research value.

## 9. Decisions and follow-up register

These are explicit planning stubs with current evidence and a bounded next action. Recommendations are not adopted defaults or permission to implement.

| ID | Status / known evidence | Recommended next action | Stop or escalation condition |
| :--- | :--- | :--- | :--- |
| P12-1 Baseline readiness | Pending; active Round 10 changes remain protected | Revalidate Round 11 findings and final contracts at handoff | No implementation against an assumed historical baseline |
| P12-2 Independent references | Source pass complete; A1 identifies limited-independence examples, not invalid results | Select two smallest nonredundant reference cases; derive expected values independently | If Round 11 supplied an equivalent oracle, reuse it and reduce scope |
| P12-3 Report versus gate | **Decided 2026-11-07: both.** The command stays a report by default (exit 0, compatible); a `--gate` flag exits nonzero when any required study fails or is missing; info rows never fail the gate | Implement in scripts/inference_validation.py with synthetic report tests | No silent exit-code change or second validation runner |
| P12-4 Interpretation fields | Existing ModelResult, interval helper and table basis already present | Inventory selected artifacts against B1 before adding fields | No universal schema or duplicated target mapping |
| P12-5 Example choice | Recommend existing income/population manifest and regression/preregistered recipes | Confirm a complete deterministic input/derivation/target path; label synthetic evidence | If completion requires new data acquisition/engine, simplify the example |
| P12-6 Reproducibility limits | Existing bundle, manifest and extraction replay have different roles | Verify exact supported promises using current tests and frozen adapters | No claim of full analysis replay from extraction metadata alone |
| P12-7 Resource/runtime budget | No benchmark or full-suite runtime measured in planning | Measure the selected checks before setting PR/release budgets | Do not require long stochastic suites on every documentation change |
| P12-8 Human readability | Not evaluated; no user study claimed. **Owner decision: an independent agent session may act as the second reader** (agents may review, as for overrides) | Second reader follows the canonical workflow and interprets artifacts | Correct confusing instructions before adding another example |

## 10. Explicit deferrals and reopening criteria

| Deferred work | Reason | Reopen only when |
| :--- | :--- | :--- |
| New Bayesian/causal/spatial/survey estimator families | Scientific and maintenance burden; existing methods need firmer evidence | A concrete research question cannot be answered responsibly by supported methods and has an independent validation design |
| More Census products/providers or external datasets | Coverage expansion is not the current bottleneck | An evidenced workflow need survives review of existing data and Round 10 scope |
| Dashboard, hosted service, GUI redesign, notebook gallery | Additional interface/documentation surface | A supported user workflow demonstrably requires it and has a maintenance owner |
| General plugin/schema/workflow/provenance frameworks | Duplicates existing plain objects, manifests and bundles | Multiple current consumers prove a small shared contract insufficient |
| Broad dependency splitting, wheel resource migration, whole-repo lint/type cleanup | Intentional clone architecture and large protected diff | A reproducible supported-installation or changed-code problem justifies a separate bounded proposal |
| Distributed execution and full PUMS streaming rewrite | Requires measured resource need and larger design | Round 11 R2 measurements demonstrate the bottleneck and a bounded remedy is insufficient |

Deferral is not a future promise. Keep this list short; do not convert every idea into another round. A new item should replace lower-value scope or have separate authorization, a maintenance owner and a clear completion criterion.

## 11. Definition of done

- The agreed independent cases use genuinely independent expectations and state exactly which scientific layer they validate.
- Selected user-visible artifacts and supported adapters agree on estimand, interval basis, confidence level/distribution, sample handling and limitations.
- One existing workflow runs offline with exact inputs and instructions, produces existing reviewable artifacts, reruns substantively, and handles a meaningful blocked case honestly.
- Applicable focused and offline reference checks pass in the supported environment; relevant full studies, if run, are reported with fixed rules and Monte Carlo precision. Skipped, unrun and informational checks are labeled.
- No new estimator families, providers, required dependencies, public tools, persistence systems or bundle/manifest formats were introduced; any scope exception is justified separately.
- A maintainer can identify the smallest relevant check and the release evidence for the touched path, and a second reader can understand the result without reading code.
- Earlier plans, unrelated edits, datasets and credentials remain preserved. Implementation handoff lists actual changes, results, compatibility impact, remaining decisions and deferred work; no commits or pushes without permission.

**Current handoff:** Draft informed by three parallel read-only reviews and parent source verification. No implementation, dependency setup, new runtime results, external verification or release action is claimed.
