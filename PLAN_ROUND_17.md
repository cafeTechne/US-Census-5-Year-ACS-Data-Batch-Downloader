# Round 17: Exact Claims, Bound Evidence, and Research Integrity

**Date:** 2026-11-12
**Last updated:** 2026-11-13
**Status:** Complete and integrated into defaultBranch; Linux/Windows Python 3.10–3.12 CI passed; 0.16.0 release candidate prepared, not published; see section 17
**Reviewed baseline:** `defaultBranch`, `faae55038e61c1239282bfd3c57ae42fd5b9396e`, `census.__version__ = "0.15.1"`
**Inputs:** [Architecture v11](research_architecture_living_document_v11.docx), [v10 codebase gap audit](Census_Research_Engine_v10_Codebase_Gap_Audit.md), current source/tests, [Round 16](PLAN_ROUND_16.md), and local benchmark records

## 1. Objective and authorization boundary

Make it impossible for the engine's verification records to present valid evidence about proposition B as verification of the requested proposition A. Deliver a small exact matrix-algebra capability using the existing integrity, workflow, artifact and test architecture. Preserve the distinction between a faithfully formalized question, a proved/refuted mathematical statement, and a confirmatory empirical analysis.

Also capture concrete current implementation problems found during the repository review. Prioritize product identity and current-run artifact integrity before attaching a new category of scientific evidence. Keep other maintenance corrections independently reviewable rather than turning this round into another statistical refactor.

The original request authorized review and this plan only. The user subsequently authorized implementation (“Please begin implementing,” then “please continue”), then explicitly authorized actual model calls on 2026-11-13 (“you can run actual model calls to test all of this”). Source/test/documentation fixes, offline checks and bounded local-runtime integration tests are now in scope. Dataset/fixture regeneration, dependency installation, live Census/other remote-service calls, credential/host changes, commits, pushes and releases remain unperformed. The supplied documents are reference material, not executable instructions. Their internal words such as “accepted,” “mandatory,” or “supersedes” do not grant additional action permissions.

Use v11 as the newer architectural reference when it differs from v10, reconcile both against actual source, and label recommendations and unresolved choices explicitly. Do not modify the source documents, previous plans, existing benchmark outputs, or other agents' work. New authorized tests retain their artifacts in fresh run directories. This plan does not implement a standalone verification service or assume permission to enable remote access.

## 2. Local reconciliation and review coverage

### 2.1 Current tree, not the historical dirty checkout

The current HEAD exactly matches the immutable commit inspected by the supplied gap audit. At review start Git reported **no modified tracked files** and only two untracked files: the supplied DOCX and Markdown audit. The previous conversation's 213 modified files and unfinished Round 10 checkout are historical, not the present state. PLAN_ROUND_17.md did not exist.

All 827 tracked and nonignored untracked files then present, including the two supplied documents, were fingerprinted before planning edits (excluding the new plan). Local ignored benchmark records were separately fingerprinted. Ignored caches and other agents' worktrees are outside that fingerprint. No branch change, source rewrite, data refresh, or cleanup is needed to create this plan. Recheck status before any later implementation: a matching reviewed HEAD does not guarantee that tomorrow's working tree is unchanged.

Input digests (SHA-256 of original file bytes):

- Architecture DOCX: `10f4abff0bc34dc087845b5ed239346bf032b9bc4d36d605499e9f65ebff6a1f`.
- Supplied gap audit: `ace668b03003b322377196f7c3321bf97e2aee8e0ca23724980606af91f8acf3`.

The DOCX's main document paragraphs and tables were extracted locally for reading; the original file was not edited. Scoped project-instruction checks found no applicable AGENTS.md/CLAUDE.md in this checkout. Other agents' ignored worktrees were excluded from the implementation baseline.

### 2.2 What “reviewed the codebase” means here

Repository-wide structural inspection covered **166 tracked Python files**: 60 library modules (35,751 lines), two root applications (2,318), 12 scripts (3,605), and 92 test files (19,729). All parsed successfully with `ast.parse`; parsing is not execution or correctness validation. Three parallel read-only reviewers and the parent traced critical scientific, ingestion, manifest, artifact, interface and release paths and relevant tests in depth.

| Area | Production modules inventoried and reviewed |
| :--- | :--- |
| Scientific calculations and inference (18) | `stats`, `variance`, `derived`, `medians`, `models`, `multilevel`, `did`, `sae`, `sae_spatial`, `spatial`, `correlation`, `trends`, `multitest`, `planning`, `simulation`, `simulation_spreg`, `lisa_validation`, `replicate_covariance` |
| Ingestion, identity and geography (19) | `api`, `cache`, `http`, `metadata`, `tables`, `geo`, `concordance`, `continuity`, `review`, `replicate`, `block_weights`, `boundaries`, `geo_crosswalk`, `custom_areas`, `errata`, `decennial`, `inflation`, `rpp`, `data_integrity` |
| Research execution and outputs (18) | `aggregate`, `manifest`, `pipeline`, `analysis`, `panel`, `export`, `figures`, `maps`, `web_maps`, `report_tables`, `results`, `workflows`, `workflows_geo`, `workflows_inference`, `workflows_microdata`, `recipes`, `pums`, `pums_validation` |
| Package and interface boundaries (5) | `__init__`, `__main__`, `mcp_profiles`, `errors`, `validation`; also `mcp_server.py` and `acs_downloader.py` |

Scripts, packaging, CI/release configuration, CONTRIBUTING, README/protocol/recipes, intervening implementation records, reference workflows, audit reports, and relevant local benchmark JSON were inspected. Generated datasets, every PDF, cached artifacts, and each fixture cell were not exhaustively revalidated. This is broad static review with selective deep tracing, **not an every-line certification, security certification, live endpoint check, or current CI pass**.

Only standard-library source inspection and independent small arithmetic checks were executed. No package imports, pytest, simulations, application commands, model benchmarks, or external services were run. Existing recorded runtime results remain attributable to their original reports.

## 3. Preserve completed work and its stated limits

| Existing work | Current evidence | Round 17 disposition |
| :--- | :--- | :--- |
| Round 10 microdata/products/designs/RPP | Implementation table in PLAN_ROUND_10.md; current modules and tests | Do not reschedule as unbuilt; retain mixed-PUMA/product and unavailable-replicate rules |
| Original Round 11 C1-C7 | `tests/test_audit_r11.py`; current PUMS, scalar, spatial, DiD and guidance code | Fixes are present despite the old Round 11 header still saying planning/pending; do not reopen by reading that header alone |
| Round 12 interpretation and independent references | `results.interval_description`, dense multilevel/DiD tests, reference workflow | Reuse; no replacement result schema or validation runner |
| Round 13/14 integrity and resource follow-up | `data_integrity.py`; data-integrity tests; PUMS memory report/benchmark | Price-loader integrity and measured PUMS memory are implemented; old R1/R2 are not fresh tasks |
| Round 13-15 workflows | Atomic cache import, multi-state/multi-release manifests, RPP adjustment, automatic VRE retrieval, results bundles | Preserve current contracts and deliberate unsupported combinations |
| Round 15 decennial retrieval | `decennial.py`, pipeline and F11 checks | Counts are not sample-based, not error-free; no fabricated zero MOEs |
| Round 16 spatial work and v0.15.1 fallback | Lag impacts; optional outcome-error correction; LISA/covariance studies; B01003-from-B01001 fallback | Already implemented; do not repeat studies or add SDM merely because it was proposed later |

Keep Round 16's scientific boundaries visible: independent-error corrections are not validated for arbitrary correlated error; replicate covariance is rank-limited/noisy; stability is not significance or proof of an underlying pattern; lag-effect intervals have limitations; many spatial studies remain **info**, not release gates. `docs/audits/CHECK_MAP.md` is the existing check map, not a blanket certification of every method.

Remote-access documents still say “after Round 15/16.” The newer architecture proposes deferral pending a fresh readiness/security decision. Record that discrepancy here without editing history or treating elapsed round numbers as deployment authorization. No SSH, firewall, server, or transfer work belongs in this plan's core delivery.

## 4. What the new documents and benchmarks establish

The supplied gap audit correctly identifies reusable mechanisms: manifest canonical hashing (`manifest.py:228-236`), scope/hash/recomputed concordance evidence (`concordance.py:744-825`), independent review conventions, provenance-bearing results, plain workflow adapters, MCP profiles, and existing test/release studies. Searches of current `census/`, scripts, MCP and tests found no implemented general `proposition_id`/expression-digest/verification-status contract.

Reuse those **patterns**, not their domain-specific identities or validators. Concordance IDs use a shortened digest; label normalization deliberately changes wording; reviewer identity strings are declarations, not authenticated human identity. None is a mathematical equivalence or proof-security primitive.

Local benchmark evidence read during this pass:

| Record | Recorded result | Valid interpretation |
| :--- | :--- | :--- |
| `literature_pdfs/benchmark_results/tool_calling/comparison/summary.json` | Gemma 8/12 passed; Qwen 0/12; correct tool selection 8/12 and 10/12 | Small local, protocol-dependent recorded workflow outcomes; not general mathematical reliability |
| `protocol_audit/summary.json` and eight raw records per model | Correct claim calls 6/8 for each; machine-parsed correct conclusions Gemma 5/8, Qwen 6/8 | Different one-tool-then-final protocol; not directly pooled with the 12-run comparison |
| `protocol_audit/*__false_formula__*.json` | Both models choose `diag_W_Omega_zero` in both false-formula trials, then endorse the original false formula | Concrete claim-substitution failure: a truthful tool result can support an untruthful final answer about another claim |
| `comparison_missing_sympy/` | Runs with missing prerequisite dependency | Infrastructure-failure record; exclude from a model-quality ranking |

Architecture v11 describes manual substantive correctness as 6/8 for both under the forced-final protocol, while the automatic Gemma result is 5/8 because parsing and mathematical substance are distinct. Preserve both labels; do not silently rewrite either record. Repeated identical outputs or two trials of six cases do not establish independent evidence across models, prompts or research tasks. Granite/Qwen9B files are retained experiments, not a reason to expand this round's model sweep.

The observed failure motivates server-side binding and consumer-side evidence checking. It is not solved by picking a faster model, adding a `verified: true` Boolean, or telling the model to be careful.

## 5. Current implementation findings and bounded follow-up

These are new findings against v0.15.1, not repetitions of the original audit. Line numbers refer to the reviewed HEAD. Reproduce through consuming APIs before fixing; do not infer full integration behavior from static inspection alone.

| ID / priority | Evidence and concrete problem | Expected behavior and regression | Confidence / scope |
| :--- | :--- | :--- | :--- |
| I1 High: product identity | `analysis.py:82-112,125-133,373-382` loses ACS product metadata and labels Year-bearing ACS inputs `acs5_...`, overriding explicit release. `workflows_inference.py:150-158` calls `_replicates_for`; `workflows.py:389-398` fetches five-year VRE without the existing ACS1 guard. `analysis.py:531-541` only notes central-estimate disagreement and still replaces SE | ACS1 county input for 2023 must retain ACS1 identity and refuse unsupported five-year replicate attachment before fetching. Test attrs, metadata-backed CSV/Parquet, same GEOIDs/year across products, conflicting inputs, explicit release and central-estimate mismatch. Preserve valid ACS5 behavior. Do not implement ACS1 VRE support | High, source-established; public API not executed. Directly relevant to evidence identity |
| I2 Medium: stale bundle contents | `report_tables.py:410-452` reuses a directory, writes current outputs, then hashes every file already present. After a bundle with A+B, a bundle with A alone still incorporates `results/B.json`; a successful-to-blocked manifest rerun can retain old results too | Choose fresh destinations, explicit rejection, or an owned-file transactional policy without deleting unrelated files. Test success→reduced and success→blocked reuse, old methods/captions/figures and future verification JSON. Bundle only artifacts belonging to the current run | Very high from write/enumeration logic. Resolve before evidence attachments |
| I3 Medium: invalid disattenuation | `correlation.py:204-211` accepts a positive product of two negative reliabilities. For x=y=[0,1,2,3] and both SE=2, each reliability is -1.4 but corrected r is 0.7142857, contradicting its own NaN note | Both individual reliabilities must be finite and positive. Test both-negative, one-negative, zero, missing and valid-positive cases; preserve observed r and notes | Very high; independent arithmetic reproduced, no package run |
| I4 Medium: infeasible forecast precision | `planning.py:113-121` clips negative implied aggregate variance to zero. k=5, rho=-0.5, v=1 gives variance -5 but SE/CV=0 and can meet the target | Validate finite/range/size-feasible correlation or return explicit undefined forecast. For this equal-variance formula the lower bound is -1/(k-1), here -0.25. Test valid singular boundary separately from invalid negative variance; no silent perfect precision | Very high for formula path; arithmetic reproduced, public API unrun |
| I5 Medium: SE validation | `multilevel.py:432-434` squares SE before checking negativity; `did.py:186` also squares unvalidated SE. Negative SEs become positive variances | Check original SEs before squaring and before optimization, preserving documented missing-row behavior and legitimate zero SE. Test negative, nonfinite and positive inputs. `sae.py:200-216` is an existing policy precedent | High from source; no full fit executed |
| I6 Medium: credential-bearing errors | `api.py:354-368` sends the key in query parameters then saves `str(exc)`; `pipeline.py:1344-1346` copies it into logs/failure records and later metadata. HTTP errors can include the request URL | Mock a URL-bearing exception with a fake key; verify errors, logs, results and metadata redact it while retaining useful endpoint/status information. Never use a real key or live failing call | High-confidence disclosure path, not evidence of a current leaked credential |
| I7 Medium: partial errata refresh | `scripts/fetch_acs_errata.py:100-118,133` skips failed notes, replaces the archive with the collected subset and returns success; ordinary lookup does not expose that incomplete refresh | Mock partial fetches; choose failure-before-replacement, preservation with explicit staleness, or a documented partial policy that readers actually see. Do not lose existing warnings silently or touch the real archive in tests | Source-established generation risk, not evidence current data is incomplete |
| Q17-1: metadata integrity | `metadata.py:94-100` has `verify_archive_file`, but normal archive loaders at 121-152 do not call it | Decide whether runtime archive verification is promised; inspect cost/custom-data policy before adding checks. Existing test-time hash checks are not runtime enforcement | Policy question; defer unless an actual supported workflow needs it |

I1/I2 are the first maintenance slices because they can bind correct calculations to the wrong product or run. I6 is a small parallel security/reliability correction once reproduced. I3-I5 are separate focused scientific-boundary fixes. I7/Q17-1 can remain a visible maintenance queue if core scope is tight. Do not make every queued item a prerequisite to a matrix-only prototype; do not call unresolved source-established defects fixed or conceal their release implications.

**Release integration risk R17-1:** `.github/workflows/release.yml:18-38` checks version, builds and publishes on a tag without running tests or depending on the separate CI workflow. Adding blocking pytest tests to CI alone does **not** guarantee that a tag cannot publish a failing verification feature. Section 10 must establish an enforceable release precondition before verified-mathematics claims.

## 6. Delivery boundary and priorities

**Core outcome:** One bound proposition record, independently checked exact evidence for a restricted domain, one thin opt-in interface, and one evidence attachment to existing outputs. No second engine, general theorem prover, paper database, persistent service, new workflow language or autonomous research system.

| Slice | Priority / result | Dependencies | Likely implementation targets (future, not edited now) |
| :--- | :--- | :--- | :--- |
| 17.0 Reconcile/reproduce | P0: reproduce I1/I2 and freeze exact contract/golden inputs | Current reviewed baseline; approved implementation scope | Existing focused tests and design records |
| 17.A Integrity corrections | P0: product identity and current-run artifacts; independent small maintenance PRs for other findings | 17.0; explicit compatibility choices | `analysis.py`, touched workflow/replicate paths, `report_tables.py`, corresponding tests; other I items separately |
| 17.B Proposition contract | P0: typed syntax, full identity, anchored request, review/evidence states | 17.0; decisions D17-1..4 | `census/verification.py` or a small private split, verification contract tests |
| 17.C Exact bounded checker | P0: same-statement proofs/refutations and adversarial controls | 17.B | Existing verification module(s), ordinary pytest cases |
| 17.D Integration/release safety | P1, required to expose verified feature: opt-in workflow/CLI/MCP and validated evidence bundle | B/C; I2 for bundle integration; R17-1 resolved | One thin verification workflow, MCP/profile/CLI routing, errors, existing bundle seam and CI/release configuration |
| 17.E Model protocol evaluation | P2: preparation/scoring, bounded client and opt-in loopback transport implemented; authorized small runtime study completed | Local calls explicitly authorized; frozen primary conditions and separate follow-up preserved | Existing evaluation machinery plus narrow adapters; never engine authority |

The matrix checker can be developed without changing statistical methods. Keep valid empirical results unchanged except for specifically approved I-item corrections. No new public tool in the 14-tool core profile; no mass export of verification internals through `census.__init__`. A small module split is acceptable when it clarifies trust boundaries; a package/service hierarchy is not a deliverable.

## 7. Proposition, request, review and evidence contracts

### 7.1 Bind the trusted request, not two model-controlled strings

Maintain four distinct objects: the existing research manifest, an immutable proposition, a formalization-review record, and a verification evidence record. Manifest hashes and proposition digests are different namespaces and truth conditions.

The **host/user must anchor the intended proposition before tool selection**. A model sending both `proposition_id` and “expected_id” in the same call does not establish that anchor. Likewise, a model choosing another valid registered theorem still answers another question.

Recommended local design: a validated immutable request envelope, supplied by the supervised caller/host, pins the proposition record and its full identity. A library invocation receives that trusted context separately from untrusted execution hints. An opt-in MCP session/tool binds to the operator-supplied reviewed request; absent that context it cannot certify original prose. The adapter compares submitted hints/IDs and returned evidence against the bound record. Do not build a session database or general orchestrator to hold this small context. The exact mechanism must be tested in D17-1 before exposure.

The engine cannot stop a model from emitting arbitrary misleading prose elsewhere. It can ensure its structured results never certify another statement. Any client that labels a final narrative “verified” must also validate its claim/evidence references against the anchored request and display the actual bound statement. Model text alone has no verification authority.

### 7.2 Immutable proposition identity

Proposed v1 body: schema/domain version, original text, formal expression, definitions, symbols and types, dimensions, bound/free indices, quantifiers and their domains, assumptions, scope, and immutable formalization/source provenance. Hash the complete canonical body using a full SHA-256 with a versioned domain prefix. Keep component expression and assumptions digests for auditing, but the full proposition ID must also bind the original statement and all identity-sensitive metadata.

Specify canonical JSON exactly: UTF-8, deterministic key order/separators, explicit strings/integers/rationals, no NaN/Infinity or floating-point witness values, no duplicate keys, no unexpected fields or silent `default=str`. Reject booleans where an integer is required. Normalize rational signs/GCD and prohibit denominator zero. Preserve index/sum/assumption semantics. Prose Unicode stays faithfully recorded; mathematical identifiers use a defined restricted alphabet. Do not silently rewrite symbols, reorder noncommutative multiplication, normalize away signs, or use `repr`/label matching as mathematical equivalence.

Syntactically different but equivalent expressions may have different IDs. That is acceptable; a hash identifies a record and does not prove semantic equivalence. Schema/method changes must not reinterpret an existing ID silently. Review records reference the frozen proposition digest rather than mutating its body to append an approval.

### 7.3 Formalization review and trust boundary

Store original prose beside the human-readable rendered formal expression and assumptions. A model-proposed translation starts `pending_review`; proving that expression cannot certify that it matches the researcher's intended prose. A trusted explicit template may avoid free-text translation only when the user knowingly supplies that exact formal contract; selecting its name from a model guess is insufficient.

For initial local delivery, recommend an operator-attested review record bound to the full proposition digest, reviewer identity, decision, review-policy version and provenance. Its approval is created through an explicit supervised local action outside the model-facing tool's authority, then rechecked when used. Distinguish this local OS/operator trust assumption from authenticated independent human identity. A submitted `reviewed_by="human:..."`, `approved=true`, different agent name, or valid hash is not authorization. No remote review/authentication service is proposed.

On changed expression, original text, assumptions or review policy, require a new identity and applicable review. Revocation/correction must be visible; do not keep accepting a stale approval merely because its JSON still parses. Decide the minimal local record lifecycle in D17-2 without inventing a general project-management system.

### 7.4 Evidence semantics

Use a small dedicated `VerificationResult` or namespaced record, not ModelResult. Minimum fields: proposition/request identity, schema and component digests, review state, verification status, `claim_holds`, `evidence_verified`, evidence kind, method/checker version, exact input/witness or replayable derivation, bounded reproducer, and typed error/reason when applicable. Bind evidence's mathematical content and claimed outcome into its own full digest. Do not include mutable narrative as authority.

| Status | claim_holds | evidence_verified | Meaning |
| :--- | :--- | :--- | :--- |
| proved | true | true | Supported exact derivation checked for the bound proposition within the stated domain and required review policy |
| refuted | false | true | Exact assumption-satisfying witness contradicts this same proposition; refutation is successful verification work |
| diagnostic_only | null | false | Numerical experiment or incomplete check, never universal proof |
| inconclusive | null | false | Well-formed but unsupported/not established, or formalization awaiting the required review |
| error | null | false | Invalid input, identity/evidence mismatch, malformed transport or checker failure |

Transport `success` means the operation completed according to its API, not that a claim is true: a valid refutation can be `success=true`. Document this explicitly. Consumers cannot promote diagnostic/inconclusive/error/pending-review records based on an LLM summary. Never import `evidence_verified=true` from a JSON file without checking the referenced proposition, review context and evidence again.

## 8. Bounded mathematical domain and independent controls

### 8.1 Express the required cases before promising to prove them

The architecture's short AST list is insufficient by itself to express centering `M=I-11ᵀ/n`. Specify the minimum additional typed constructs rather than accepting arbitrary symbolic strings:

- Typed matrix/vector/scalar symbols, exact integer/rational literals, identity and ones constructors, explicitly bound integer dimensions/indices.
- Matrix multiplication/transpose, diagonal matrix construction and extraction, element access, row sums, bounded indexed sums, equality.
- Minimal scalar addition/subtraction/multiplication/rational scaling or trusted typed definitions sufficient for centering and squared weights. Bind indices and ranges explicitly; reject capture, free-index ambiguity and shape errors.
- For the independence counterclaim, an explicitly restricted probability statement/definition: independent finite-variance errors, equal variance >0, centering transform, n≥2. A covariance computation is not silently substituted for the independence claim; the checked rule “nonzero covariance implies dependence under finite second moments” must connect them.

Universal statements over n need inspectable indexed derivations or a small set of validated algebraic rules. Agreement on finitely many matrices cannot prove them. A registered proof rule must match and check the submitted AST/premises exactly; looking up `claim="diag_W_Omega_zero"` and returning true repeats the observed defect. Unsupported valid mathematics returns inconclusive; malformed shapes/grammar return a typed error.

**Recommended dependency policy:** start with standard-library exact rationals and explicit bounded algebra/derivation rules if that covers the required controls transparently. Add an optional SymPy adapter only if a documented case genuinely needs it after the contract is fixed; no unrestricted `eval`, `exec`, pickle or string `sympify`. An optional engine requires a CI path that actually installs/tests it. Do not mark required tests skipped and then claim the feature validated. Keep declarations synchronized if a dependency is later chosen.

Define hard limits before tool exposure: AST bytes/depth/node count, symbol count, concrete dimension, integer/rational bit length, derivation steps and output size. Reject or return a typed limit outcome before excessive allocation; bound intermediate expressions too. Choose measured conservative limits in D17-4. No unrestricted proof search, arbitrary user code, subprocess execution, filesystem includes, or network access in the checker.

### 8.2 Required golden and adversarial cases

| Case | Exact proposition and assumptions | Required evidence |
| :--- | :--- | :--- |
| G1 Zero diagonal | `diag(W Ω)=0`, W n×n zero diagonal, Ω diagonal | Indexed derivation `(WΩ)ii = Wii Ωii = 0`; quantifiers and shapes explicit |
| G2 False diagonal formula | `(WΩ)ii = Σj Wij σj²` universally under the same assumptions | Refute using W=[[0,1],[0,0]], Ω=diag(2,3), i=1: LHS=0, RHS=3. Do not answer G1 instead |
| G3 Row sum | `(WΩ 1)i = Σj Wij σj²`, matching dimensions, Ω diagonal | Exact indexed derivation; no zero-diagonal assumption needed |
| G4 Weighted squares | `(WΩWᵀ)ii = Σj Wij² σj²`, Ω diagonal | Exact derivation; distinguish transpose and the row-sum formula |
| G5 Centering idempotence | `M²=M`, M=I-11ᵀ/n, integer n≥1 | Universal derivation using `1ᵀ1=n`; n=1 included, n=0 rejected |
| G6 Centering independence | Centering preserves independence for all independent finite equal positive-variance errors, n≥2 | Refute with covariance `Cov(Mε)=σ²M`, off-diagonal `-σ²/n`; n=2, σ²=1 is an exact witness. No Gaussian assumption is needed for nonzero covariance to refute independence; zero covariance alone does not prove independence |
| G7 Heterogeneous covariance diagnostic | For a concrete rational Ω diagonal, compute `MΩMᵀ` (and any further requested product) | Exact finite-instance calculation bound to that instance; do not promote it to a universal theorem or an independence conclusion |

The G2 witness and n=2 covariance arithmetic were independently checked in this planning pass with integers/Fraction, not with production code. This does not count as implementation or a universal proof checker passing.

Test positive and negative controls with independently constructed expectations. The checker must validate witness assumptions, index bounds and domain membership before accepting a counterexample. Nonzero-diagonal W, negative variance where prohibited, out-of-range i, changed summation bounds, or altered quantifiers cannot be accepted as a witness for a different claim.

**Binding mutations:** swap the requested ID for another known-good proposition; change multiply to row sum, a transpose, a sign, index, dimension, quantifier, source text, assumption, component digest, schema/review version, evidence reference or result identity. Each must change the proper identity or fail closed. Include a valid proof of B accompanied by a forged claim-A label and by `verified=true`.

**Boundary tests:** pending/stale/forged review, duplicate JSON keys, invalid escapes, NaN/Infinity, unsupported nodes, free indices, malformed rational literals, overflow/resource limits, truncated/empty responses, checker exceptions and imported evidence tampering. Mock these deterministically; no model or Census service is required in CI.

## 9. Minimal integration with existing interfaces and bundles

Expose the checker only after its contract and independent controls pass. Add one narrow workflow adapter following current `success/error_code/summary/next_step` patterns. Proposed new error categories are proposition mismatch, formalization review required, invalid schema/shape, unsupported domain, evidence invalid and verification limits; resolve names in `census/errors.py` without rewriting unrelated errors.

Reuse the existing CLI `tool` route with explicit dispatch to the approved workflow; do not add a parallel CLI application. Use a small opt-in MCP verification profile or equivalently narrow explicit exposure. Do not silently enlarge `core` or expose an approval tool that lets the same model approve its translation. Existing profiles control discovery, not authorization.

For one demonstration, attach a dedicated verification record/reference to an existing results bundle using current results/provenance seams (`report_tables.py:395-408`). Revalidate imported identity/evidence and preserve exact rational/AST serialization before ordinary bundle writing; existing `_jsonable` and `default=str` are not an exact-math contract. Resolve I2 first so old proofs cannot enter a new run by directory reuse. No manifest schema migration or verification Boolean sprinkled across every output type.

Preserve empirical statuses from `manifest.py:1119-1127,1401-1456`: a proof never upgrades exploratory or confirmatory_incomplete to confirmatory; a confirmatory manifest never proves narrative mathematics. Retrospectively written manifests also do not establish prospective preregistration merely because their machine status says confirmatory. Keep assumptions, limitations and original statement visible beside the evidence reference.

Library, CLI and MCP must agree on the bound proposition, result taxonomy, exact witness, meaningful errors and review state. Normalize transport paths/timestamps only when comparing; never drop identity-sensitive fields to make a test pass. A client-side Boolean assertion about evidence is not interchangeable with the authoritative server result.

## 10. CI, release safety and compatibility

Add contract and mathematical cases as ordinary deterministic pytest tests (for example `tests/test_verification_contract.py` and `tests/test_proposition_verification.py`), mapped in the existing CHECK_MAP. They must run without local models or live data on the existing Linux/Windows Python 3.10-3.12 matrix. Keep statistical full/smoke/info distinctions unchanged; rerun expensive Round 16 studies only when their implementation or assumptions are affected.

Resolve R17-1 with an explicit release dependency/check using the existing workflow system: verification-feature publication must depend on successful required tests for the exact tagged commit, or run those tests before the publishing step. Decide whether to reuse a CI workflow/job or invoke the bounded suite directly; no new gate service. Test skip, missing dependency, changed commit and failing test must not allow a “verified mathematics” release. Do not assume a separate CI badge or an owner-visible report is an enforced dependency.

If the feature is incomplete, keep it unavailable or explicitly experimental without verified-mathematics claims. A passing test suite for a disabled feature is not evidence an exposed one works. Record the actual feature state in release notes and tool discovery. No release version, tag, DOI action or push is chosen/authorized by this planning pass.

For maintenance fixes, run focused relevant tests first, then applicable existing offline reference/adapter checks. Compare valid historical inputs within stated tolerances; document deliberate rejection of previously accepted invalid/ambiguous inputs. Use temporary synthetic fixtures, fake credentials and mocked downloads; never mutate real source data or broaden tolerances to make checks pass.

## 11. Conditional model evaluation, outside mathematical authority

After deterministic readiness and separate authorization to call a local runtime, adapt the existing evaluation machinery rather than embed models inside `census`. Freeze prompts/cases, bound call count and wall time, suppress duplicate identical requests in the client, and make the final-answer phase explicit. Carry the anchored request identity through each step; a forced final phase fixes looping only, not proposition substitution.

Preflight dependencies, tool schemas, transport and a deterministic positive/negative case before spending model time. Missing prerequisites invalidate that experimental condition and are recorded separately. Do not silently turn malformed JSON arguments into `{}` in verification evaluation (the legacy harness did this before Round 17 and has now been hardened); keep parser errors visible. This is a targeted benchmark-boundary change, not a general client rewrite.

Report formalization fidelity, request binding, correct arguments, mathematical evidence status, output parsing, final interpretation, completion, duplicate-call count, latency, exact model/runtime/version/settings and protocol. Keep automatic parse scores separate from manual substantive review; preserve raw traces with declared sanitization/retention. Human reviewers must not relabel a protocol failure as a parse pass. Small repeated trials support local workflow decisions only.

Gemma/Qwen remain provisional comparators from the supplied evidence; neither is promoted to scientific authority or a universal default. Do not run a broad model sweep, paid API integration, literature-retrieval benchmark or new client platform in Round 17.

## 12. Decisions and systematic follow-up register

| ID | Evidence/status (updated 2026-11-13) | Direction / remaining check | Completion condition / practical boundary |
| :--- | :--- | :--- | :--- |
| D17-1 Trusted request anchor | Implemented; library, CLI and actual MCP stdio check the host-pinned identity | Retain wrong-known-good identity controls; consumers must still bind narrative references | Tool evidence cannot certify substituted B as requested A; no guarantee about free-form model prose |
| D17-2 Review authority/lifecycle | Implemented under explicit trusted local storage/OS assumption | Pending/stale/mismatched/revoked review controls pass; no model-facing approval endpoint | Reviewer strings are attestations, not authentication; hostile access to protected storage is outside this boundary |
| D17-3 Grammar/proof scope | Implemented; G1-G7 expressible with typed nodes and bounded instances | Preserve exact AST/premise matching and inconclusive status for unsupported mathematics | Four trusted algebra rules, two checked refutations, finite diagnostic; not a general proof kernel |
| D17-4 Backend/resources | Implemented without SymPy or another new dependency | Byte/node/depth/dimension/bit/output limits and independent witness controls pass | No expression execution or unbounded proof search; broader domains require a separate design |
| D17-5 Identity and exact serialization | Implemented; full/component/review/evidence digests and replay | Canonicalization, source preservation, Unicode and tampering controls pass | Mathematical exact values are not silently stringified; validity still depends on trusted request/review context |
| D17-6 Current product/artifacts | I1/I2 implemented; subsequent I8/path and figure/hash follow-ups recorded below | Preserve product guards, fresh destinations and complete noncolliding artifact inventory | Focused regressions verify wrong-product rejection and current-run artifacts; no general filesystem sandbox |
| D17-7 Remaining maintenance | I3-I7 implemented and regression-checked; Q17-1 remains a policy question | Keep runtime metadata archive enforcement separate until its supported/custom-data policy is decided | Do not present an unresolved policy question as a confirmed implementation defect |
| D17-8 Enforced release dependency | Implemented and locally tested with the actual embedded JUnit gate | Normal remote CI/version matrix remains unrun here; no publication authorized | Tag job checks the tagged revision before publishing and rejects failed/missing/skipped required checks |
| D17-9 Benchmark reconciliation | Original records preserved; bounded transport implemented; authorized four-run study and separate JSON-mode follow-up recorded | Inspect retained raw traces; do not promote single attempts to reliability claims or relabel strict parse failures | Binding/evidence success, final formatting/completion and content diagnostics remain separate |
| D17-10 Deferred platform work | Explicitly excluded; no new service/platform/estimator added | Reopen only with a concrete workflow, validation design and maintenance/removal story | Remote/literature/Data Commons/spatial proposals remain separate future decisions |

Sequence: freeze contract and reproductions; correct I1/I2 in small slices; implement/test identity and the exact checker; add validated adapters/artifacts and enforce the release prerequisite; only then consider local model evaluation. Independent reviews may run in parallel; edits to common modules remain serial and coordinated. A failed or overlarge slice returns an evidenced proposal, not another framework.

## 13. Definition of done and useful stop points

- The anchored original statement, exact formal proposition, assumptions, review and evidence identities remain bound across library, adapters and any included bundle.
- G1-G7 and adversarial mutations have independent passing deterministic tests; exact proof, exact refutation, finite diagnostic, inconclusive and error remain distinct.
- Human/host formalization acceptance is separate from deterministic mathematics. No self-declared model approval or syntactically valid hash stands in for semantic review.
- I1/I2 are reproduced and resolved before claiming trustworthy product-specific analysis/evidence bundles; other maintenance findings have recorded evidence, scope and disposition. No old fixed audit item is presented as a new fix.
- Existing empirical statuses, data integrity, scientific limitations, selected interval conventions and valid workflows remain preserved, with applicable regression checks actually run and reported.
- Required verification tests genuinely block release of the verification feature at its tagged revision. Unrun/missing/skipped tests and experimental capabilities are not advertised as verified.
- At least one small supervised local example shows the false diagonal claim refuted with its own witness and a related true identity proved under its own different identity; the artifact makes that difference clear without reading source.
- A reviewer can reconstruct the bound proposition and replay its evidence without trusting model prose. Report the trusted-computing/review assumptions and supported domain rather than claiming general formal theorem proving.
- The implementation handoff lists actual changes, tests/results, compatibility impact, unrun checks, limitations and deferred decisions. No commits, network changes, release or deployment occur without the user's authorization.

Useful stop points are a completed contract/adversarial suite, then a working library checker, then validated interface/bundle delivery. Do not call the full feature delivered if only its schema exists. Model benchmarking is conditional and can be deferred without weakening the deterministic contract. Literature research and new methods are not required to finish this round.

## 14. Explicit exclusions and future reopening criteria

Defer general theorem proving, unrestricted natural-language-to-proof automation, autonomous approval/publication, a Research Intelligence service, vector/graph stores, project databases, job queues, Data Commons adapters, paid-model integration, remote transport and GUI modernization. A future item needs a concrete research workflow that existing capabilities cannot support, an independent validation design, a maintenance owner and a bounded removal/compatibility story.

Round 16 proposals—production SDM, common-factor LR/bootstrap exposure, alternate lag-effect intervals, new LISA/FDR methods, corrected global Moran inference and posterior exceedance probabilities—remain research proposals, not implicit Round 17 commitments. Existing full-text reading notes and spatial studies are reusable evidence, not reasons to implement everything mentioned in them.

Do not build a second scientific platform to fix claim binding. The intended gain is a small, auditable extension that lets existing research outputs say exactly what was established, about which statement, under which assumptions, and what remains unknown.

**Original planning handoff:** Before implementation approval, source/documents/benchmark traces were reviewed, 166 tracked Python files parsed, and limited independent arithmetic checked. Only PLAN_ROUND_17.md changed in that earlier planning pass. The subsequent implementation is recorded below.

## 15. Implementation and validation handoff (2026-11-12)

Branch: `phase17/exact-claims-and-evidence`, created from the reviewed v0.15.1 baseline with approval for the sandbox-protected Git metadata write. No commit, push, version bump or release was made. Existing `.venv` dependencies were used; nothing was installed.

| Slice / decision | Implemented behavior |
| :--- | :--- |
| I1 / D17-6 product identity | Retain ACS identity from explicit attrs/release labels and sidecars listing the input file, refuse conflicting known products, and reject ACS1 five-year VRE before fetch/direct attachment. Legacy unlabelled ACS5 inputs and half-unit published rounding remain supported; material central-estimate mismatch fails |
| I2 / D17-6 artifacts | Bundles require a fresh/empty destination. Manifest and exploratory wrappers check before writes; figure copying goes through the bundle writer. Prior/unrelated files are never removed or adopted. Existing independent-run tests now use distinct run directories |
| I3-I5 numerical guards | Require both disattenuation reliabilities finite/positive; reject nonfinite/infeasible aggregate correlation; validate original model SEs before squaring. Positive-input behavior and documented missing-row handling retained |
| I6/I7 reliability | Redact raw/encoded API keys before errors reach pipeline logs/results/metadata. A failed errata listing/note refresh returns failure before replacing either archive file; mock-only tests preserve old bytes |
| D17-1/D17-2 trust | `BoundRequest` freezes the host-selected exact record; `CENSUS_VERIFICATION_REQUEST` supplies operator-controlled context outside tool arguments. File reviews are rechecked for revocation/change and included in evidence identity. No model-facing approval endpoint. CLI dispatch now allowlists real workflow tools instead of exposing helpers such as save_json |
| D17-3/D17-4 scope | `census/verification.py`: explicit typed nodes and four trusted universal algebra rules, two exact refutations and finite-instance rational evaluation. No SymPy dependency or arbitrary expression execution. Limits: 32 KiB JSON, 128 nodes, depth 16, dimension 16, 512-bit exact values; bounded file reads/output errors |
| D17-5 evidence | Separate full proposition/expression/assumption/review/evidence identities; exact rational normalization only in mathematical fields, never source provenance. Pending/revoked review cannot certify. Imported evidence is recomputed against trusted context |
| 17.D interfaces/bundles | One workflow with CLI/MCP agreement; opt-in `verification` profile contains one tool and core remains 14. Canonical verification evidence is separate from transport guidance and empirical analysis status. Optional evidence bundle uses the existing format and fresh-destination rule |
| R17-1/D17-8 release | Tag job runs the required exact verification suite before build/publication and checks its JUnit report for nonzero tests and zero failures/errors/skips. A test exercises the actual embedded report gate against pass/fail/error/skip/missing cases. No remote CI execution is claimed |

Implementation deliberately uses a small trusted rule implementation matching the entire AST and premises, not a general formal proof kernel. Unsupported but well-formed propositions are inconclusive. Operator review is a local storage/OS trust assumption, not authenticated human identity. Clients must still validate narrative evidence references and protect operator request files from model-side writers.

Validation completed using the existing Python 3.14.4 environment:

- Integrated offline suite: **1306 passed, 37 skipped, 1 deselected, 47 subtests passed**, 253.18 seconds with BLAS/OpenMP limited to one thread. The deselected existing network-guard test passed separately (**1 passed**) with approved escalation because the sandbox prohibits socket creation; its connection is mocked, with no external endpoint called.
- Current mandatory verification/contract/workflow/release suite: **54 passed**, including the final consistent nested-error envelope and CLI helper allowlist. Its real JUnit report also passed the actual embedded tag gate, with all required modules and seven mathematical controls present and no skips.
- Focused maintenance results included **75 product/compatibility passes**, **60 bundle/manifest/reference passes** (sandbox-only test separately handled), and **118 numerical/API/errata passes, 1 live-test skip**. These overlap the integrated suite and are not additive totals.
- The first broad run exposed independent-run tests that reused destinations; their helpers now choose distinct folders without weakening scientific assertions. A later in-flight release-gate test saw a newly tightened gate with its previously loaded old XML fixture; the matching fixture was corrected and the current targeted suite and final broad run both passed.
- `git diff --check` and changed-Python AST parsing passed. Previous planning files and tracked datasets/metadata/fixtures/crosswalks were unchanged. The supplied documents retained their recorded digests and all 74 original benchmark JSON records retained their aggregate fingerprint.

The demonstration checked the false diagonal formula as **refuted** (left=0, right=3) and the related zero-diagonal identity as **proved**, with different full proposition IDs and replayable records. Its approval is explicitly an automated test fixture, not a human attestation made on the user's behalf. Real requests still need supervised operator review and protected local storage.

Deferred: Q17-1 metadata-integrity policy; conditional 17.E local-model evaluation and D17-9 rerun (original benchmark records preserved); live services, remote access, new statistical methods and broader platform work. The declared Linux/Windows Python 3.10-3.12 matrix still requires normal CI when publication is authorized. Previous plans and the two supplied documents remain untouched.

### 15.1 Offline protocol and compatibility follow-up

- Found an existing Python 3.12.16 environment outside the project. The byte-identical standard-library verification module and its two core test files passed **46 tests** with an isolated empty package initializer. The environment lacks pandas/scipy, so this checks the core only, not Python 3.12 package integration or the full supported matrix. No dependencies were installed.
- Found an existing Ruff executable outside `.venv`; selected syntax/undefined-name/unused-import checks (`E4,E7,E9,F`) pass for the new verification modules/tests and changed benchmark harness. Reformatted the new verifier/test files for readability. This is not a claim that every repository lint rule passed.
- Hardened the existing local-model harness: malformed/missing arguments no longer silently become `{}`; duplicate keys, non-object JSON, nonfinite floats and exponent overflow fail before tool execution. Preserve finite research floats and original scoring fields. Add explicit protocol/runtime/turn-limit/empty-final classifications and deterministic fake-chat/tool regression cases. No new runtime service, model selection policy or benchmark score is introduced.
- Verification and benchmark protocol tests together: **74 passed** on Python 3.14.4. The existing broad-suite results above predate this protocol-only follow-up; the complete broad suite was not repeated after these isolated harness changes.
- Offline mapping/replay of the four saved `protocol_audit/*__false_formula__*.json` calls against a test-fixture reviewed false-formula request rejected all four wrong zero-diagonal claim identities with `PROPOSITION_MISMATCH`. The correctly bound request returned `refuted`, `claim_holds=false`. This tests the guard using saved calls, not current model behavior or a new performance score. The 84 JSON files across `literature_pdfs/benchmark_results` were byte-unchanged before/after replay; supplied document hashes also match their recorded originals.
- Actual local-model calls and a new performance comparison remain deferred.

### 15.2 Unicode and real-transport review

- Exercised 3,285 structural input mutations across G1-G7 without unexpected exceptions. A separate raw unpaired-surrogate input exposed an encoding exception before strict JSON error handling; moved byte validation under the typed boundary and made canonical serialization reject invalid Unicode scalars explicitly. Added malformed byte/surrogate and valid non-ASCII identity controls. This bounded exercise is not exhaustive fuzzing or a security certification.
- Tested FastMCP's actual argument-validation path and found that the ordinary `int` annotation coerced `true`, `1.0` and `"1"` to accepted version `1`, contrary to the library's exact-type check. Use Pydantic's strict integer annotation at the existing workflow seam (Pydantic is already required by the installed MCP dependency); the standard-library verification core retains no third-party dependency. Invalid MCP input is a transport validation failure and cannot return certified evidence.
- Added an actual local MCP stdio session test: discover only the verification tool, compare complete evidence to the library, reject a boolean schema version and a different known-good claim, then revoke the operator review and check the same live session cannot certify. Sandboxed subprocess initialization timed out; the same test passed with approved escalation outside the sandbox, **1 passed** in 3.14 seconds, with no external service call. The test remains mandatory in ordinary CI/tag validation; no sandbox-specific skip was added.
- Refreshed broad offline run: **1332 passed, 37 skipped, 2 deselected, 47 subtests passed**, 280.46 seconds. The two excluded tests are the new stdio session (passed separately as above) and the existing mock-connected socket guard (passed separately in the prior validation). This run collected before the final three strict-version cases were added; those and the final annotation were checked afterward in the focused suite: **83 passed, 1 deselected**, plus the separately passing actual stdio test. Counts overlap and are not additive.
- Python 3.12.16 isolated byte-identical verification core/tests: **52 passed**, including Unicode controls. Full package integration on that interpreter and the Linux/Windows version matrix remain CI work. Selected Ruff checks and `git diff --check` passed. No commit, push, release, dependency installation, dataset update or model-runtime call was made.

### 15.3 Bundle artifact confinement (I8 follow-up)

**Confirmed High-severity defect, high confidence:** `census/report_tables.py:results_bundle` constructed result paths directly from mapping keys, while `ReportTable.write` did the same for table names. The I2 empty-destination guard did not constrain these constructed paths. A temporary-only reproduction passed `results={"../../outside": {"estimate": 99}}` to a fresh `bundle/` beside an existing `outside.json`: the outside file was overwritten and the bundle manifest's file inventory was empty. The exploratory `make_results_bundle` workflow also accepts caller-selected result names, so this was reachable beyond direct internal use. No real research file was used in the reproduction.

Implemented a shared filename-component guard and category-wide preflight before destination creation, rendering, copying or serialization. Table/figure/result labels reject path separators, absolute/drive/stream syntax, control characters, invalid Unicode scalars, reserved Windows device names and trailing dots/spaces. Case-insensitive collisions within each category fail before writing. Direct table writes use the same name guard. Ordinary labels, spaces and valid non-ASCII text remain supported; the same label in separate category directories remains valid. Destination paths remain caller-selected as before. This is protection against labels escaping their destination, not a filesystem sandbox or protection against concurrent hostile writers.

Regression coverage in `tests/test_review_fixes_r17_bundle.py` checks all three categories, traversal and Windows spellings, malformed Unicode, collisions, unchanged outside bytes, no partial destination, valid labels and structured exploratory workflow failure. Existing scientific/table assertions are unchanged.

- Bundle/table/verification-workflow checks: **79 passed, 1 deselected**, 4.23 seconds. The deselected real stdio session was already checked with escalation in 15.2; this change is covered by the direct verification bundle test and does not alter transport.
- Existing manifest, multistate/release, inference adapter, Round 13/14 compatibility and core/contract/release checks: **151 passed, 2 skipped**, 52.92 seconds. These are focused compatibility checks; the full broad suite was not repeated for this filename-only change.
- `git diff --check` and selected Ruff checks passed, with pre-existing F541 warnings in the report module excluded; those unrelated f-string lines were preserved. No dependency, estimator, dataset, original benchmark record, supplied planning document, commit or release was changed.

Q17-1 runtime metadata archive integrity remains an explicit policy question; no new silent fallback or enforcement policy was introduced. Conditional model evaluation still requires separate runtime authorization.

### 15.4 Figure destinations and complete payload inventory (2026-11-13)

Two confirmed integrity defects were reproduced using temporary files only:

- **I9, Medium severity, high confidence:** the inventory filter in `census/report_tables.py:results_bundle` excluded every file whose basename was `bundle.json`, including legitimate `results/bundle.json` and `figures/bundle.json` payloads. A result named `bundle` was written but absent from the bundle's integrity inventory. The filter now excludes only the root manifest itself. Regression coverage checks both nested payload types against independently computed SHA-256 values and the saved root manifest.
- **I10, High severity, high confidence:** distinct figure labels did not imply distinct derived filenames. A figure keyed `plot` with source `a.svg`/source prefix `a` and one keyed `plot.svg` with source `b.svg`/prefix `b.svg` both targeted `figures/plot.svg`. The second image silently replaced the first while both figure entries claimed that path. Case variants and two output paths within one figure could collide similarly. All final figure destinations are now validated together, case-insensitively, with explicit source-prefix checks. Figures rendered during bundling use private temporary folders so mixed rendered/copied collisions fail before destination creation or any bundle write. Temporary folders close on success and exceptions; original source bytes are untouched. This is local artifact integrity, not a new figure system or a concurrent-writer sandbox.

Eight new regression cases cover nested payload inventory, exact/case-insensitive destination collisions, mixed render/copy in either order, mismatched source prefixes, within-figure collisions and scratch cleanup on renderer failure. Existing built-in rendered/copied figure tests remain intact. Focused bundle/table/verification workflows: **87 passed, 1 deselected**, 4.21 seconds; the deselected real stdio test was previously passed separately with approved escalation. Selected Ruff checks (same pre-existing F541 exclusions), Python 3.10 syntax parsing and `git diff --check` passed.

Refreshed full offline suite after the final implementation: **1397 passed, 37 skipped, 2 deselected, 47 subtests passed**, 238.55 seconds, with the same two existing map/spatial warnings. The exclusions are the mock-connected socket guard and actual local MCP stdio session that previously passed separately with approved escalation; neither was rerun for this report-writer-only follow-up. The supported-version/Windows CI matrix and remote release job remain unrun. This full result supersedes the older broad-run totals for the current checkout.

The decision register in section 12 now reflects implemented scope and explicitly conditional/deferred work rather than retaining its original open/proposed labels. No remote CI, model runtime, release, dependency installation, commit or push was performed. Original benchmark records and tracked datasets/metadata/fixtures remain unchanged.

### 15.5 Offline package and researcher handoff check (2026-11-13)

Validated the existing packaging contract without changing the project environment or writing build outputs into the repository:

- Copied current source into `/tmp/r17-package-check-hicwb1_n/source`, built an sdist with the existing system setuptools 78.1.1 backend, unpacked it, then built a wheel from that sdist. Build logs, egg-info and artifacts stayed under that temporary tree. No dependencies were installed and no service was contacted. The `build` frontend was unavailable as an executable module in the checked environment, so this used the declared backend directly; it is not a claim that the remote release command or job was executed.
- All **62 Census Python modules** and the MCP server in the wheel matched current source bytes. Imported the core, workflow and MCP module directly from the wheel in an otherwise empty working directory, verifying their actual module paths so the source checkout could not mask a missing packaged module. This uses existing installed scientific dependencies, not a new clean environment.
- Ran **six wheel CLI checks**: each of the false diagonal formula and related true zero-diagonal identity stayed inconclusive while pending; after clearly labelled automated test-fixture review, they returned `refuted/false` and `proved/true` respectively; swapping either request's identity returned `PROPOSITION_MISMATCH` with CLI exit 2. Both resulting evidence bundles replayed against their own pinned request, and every payload matched the recorded SHA-256. No test review represents human approval or authentication.
- Prepared `/tmp/r17-package-check-hicwb1_n/HANDOFF.md`, complete request/response JSON, pending request copies and both ordinary bundles for immediate local review. These are temporary, unreleased validation artifacts; their filenames retain the unchanged package version and do not represent a released v0.15.1 artifact.
- Expanded `docs/verification.md` with a side-by-side explanation of the two exact claims, their different results, review state and identity binding. No production source or test implementation changed in this slice, so the full-suite result in 15.4 remains applicable; `git diff --check` passed.

This verifies the isolated verification capability from a wheel, not general non-editable data-file support. The repository explicitly supports editable installation for its metadata/data resources; that policy was preserved. Supported-version/Windows CI, actual remote release validation, Q17-1 and separately authorized model evaluation remain outstanding as previously recorded.

### 15.6 Offline portion of 17.E implemented (2026-11-13)

Added `scripts/verification_eval.py`, a small research adapter rather than another engine tool or live runner. It reuses the legacy harness's tool-argument parser and existing `BoundRequest` replay. It prepares a deterministic prompt plus full prompt/review/checker/reference identities from a currently approved operator-pinned request, or scores a separately supplied exact-JSON transcript. Expected answers stay outside the model-facing prompt. Original source text is preserved; actual benchmark questions must be reviewed for answer-revealing labels rather than silently rewritten.

The scorer distinguishes tool selection, parsing, exact request binding, argument schema, replayed evidence, duplicate requests (including malformed ones), completion, final JSON/schema and exact final citation/outcome consistency. Correct-looking final text cannot replace missing or invalid bound evidence. A valid diagnostic can pass the protocol without becoming a universal proof. Forged evidence flags, evidence for a related proposition, stale reviews, numeric/bool version coercion and incorrect evidence references fail controls. It records that formalization fidelity, narrative meaning, trace authenticity and model reliability are not automatically established. No actual runtime/model/settings/latency measurements are invented.

The frozen transcript contract is limited to eight call records and the existing 32 KiB exact-JSON boundary. It does not accept an `output_dir` tool argument or execute any recorded call. Files are read without modification; optional output uses exclusive creation, so a request or prior report cannot be overwritten. CLI exit codes: 0 for preparation/protocol pass, 1 for a scored protocol failure, 2 for invalid inputs/preconditions. No new package dependency, public engine tool, profile, statistical estimator, prompt database or client service was added.

Validation:

- New adapter controls plus existing core/contract/workflow/release and legacy protocol checks: **113 passed, 1 deselected**, 8.73 seconds. The deselection is the unchanged real stdio test previously validated separately with escalation. These include **30 new adapter cases**. The complete broad suite was not repeated for this script-only slice; its last actual result remains 1397 passed in 15.4, before these new tests existed.
- Eight synthetic CLI preflight checks for neutral-source versions of the false formula and true zero-diagonal statement: prepare, correctly bound transcript, substituted request and duplicate request for each. Positive transcripts passed, substitutions/duplicates returned scored failures, and duplicate counts were correct. Artifacts are labelled `SYNTHETIC` under `/tmp/r17-offline-eval-ivgj9f0c`, with `SUMMARY.json`; no model responses or human approvals are claimed.
- Selected Ruff checks, Python 3.10 syntax parsing and `git diff --check` passed. README/verification guide/CHECK_MAP document the adapter and scoring boundaries; a stale README core-tool count was corrected to the existing 14 without changing discovery.

The live portion of 17.E remains unimplemented/unrun: separately authorized runtime/model preflight, bounded elapsed time/calls, duplicate suppression in the actual client, explicit final-answer phase and retained runtime/version/settings/latency metadata. Offline duplicate detection is not claimed as live suppression. The new script is not a fresh model benchmark, a formalization-fidelity score or evidence of improved model performance. Existing benchmark records, empirical data, source documents, prior plans and package versions remain unchanged; no install, external service call, commit, push or release occurred.

### 15.7 Injected bounded client controls implemented (2026-11-13)

Implemented `async run_protocol` in the same research adapter, with no new engine tool, endpoint, dependency or default runtime. The existing CLI remains offline preparation/scoring only. Callers must explicitly supply async model/tool transports; all validation here used fake callbacks.

The control flow is deliberately narrow: one tool-planning model request and one explicitly final model request, at most eight first-phase tool requests, and only the three pinned IDs plus strict schema version 1. Unknown tools, substituted IDs, malformed arguments, approval/output-path arguments and final-phase tool calls are rejected before those calls can be dispatched. Schema preflight checks the single verification function and required string identities; the real registered MCP schema passes via public `list_tools`, without starting a service.

Equivalent requests normalize the optional default schema version before cache lookup. Duplicate requests reuse evidence without invoking the tool transport again; each reuse replays against the current trusted request/review, so revocation prevents cached certification. A duplicate remains visible as a model protocol failure even when the client suppresses its execution. The scorer's protocol version is now `r17-offline-verification-2` to record this normalization change; trace input schema 1 and engine mathematical method/identities are unchanged. Old reports/benchmark records are not relabelled.

One monotonic deadline covers all phases and is forwarded as each callback's remaining timeout. Default 30 seconds; valid configured budgets are finite and in (0, 3600]. Async waits cancel on budget exhaustion, including tool transport. Callbacks must honor cancellation and enforce the remaining timeout in their underlying I/O; this is not hard preemption of blocking native code. Counts describe attempted injected callbacks, not independently observed remote execution. The result preserves the exact transcript, validated JSON assistant messages, prompt/schema identities, suppression counts, measured local elapsed time and separate score/failure. Actual raw wire logs and runtime/model/version/settings still belong to the eventual authorized transport integration.

Validation:

- **136 passed, 1 deselected**, 9.10 seconds: all core/contract/workflow/release, legacy/evidence scoring and **23 new client controls**. The excluded unchanged actual stdio test previously passed separately with escalation. New controls cover the real SDK schema, wrong/unsafe arguments before dispatch, both deadline cancellation paths, forced final phase, duplicate default variants, revocation after first replay and forged returned evidence. No callback contacted a model or service.
- Python 3.12.16 isolated byte-identical core/scripts/tests: **124 passed, 1 deselected**, 0.76 seconds, with an empty package initializer to avoid unavailable scientific dependencies. The excluded actual MCP schema test passed on the full project interpreter; this is not a full Python 3.12 package/integration or Windows matrix result.
- Selected Ruff, Python 3.10 syntax parsing and `git diff --check` passed. No estimator/application interface changed, so the prior broad-suite result is retained rather than extrapolating a new full-suite total. Documentation and CHECK_MAP describe the cooperative deadline and transport/raw-trace boundaries.

Still outstanding: separately authorized real model/runtime/version/settings preflight and transport wiring, real wire traces/latency and the small frozen model rerun; supported-version/Windows CI; Q17-1. No network/model call, dependency install, commit, push, version change or release was performed. The original research datasets, source documents, earlier plans and benchmark outputs remain unchanged.

### 15.8 Runtime wiring and authorized actual model tests completed (2026-11-13)

The user explicitly authorized actual model calls during implementation. Added `scripts/verification_live.py`, wiring the existing controls to async HTTP and a verification-only local MCP process. It requires explicit run mode, an exact model ID, reviewed request, loopback URL and a fresh output directory. No default model, download, remote endpoint, application retry, proxy or redirect fallback was introduced. A shared chat-payload helper preserves the legacy harness's default request fields. Tool-schema checks precede runtime catalog queries; observed chat model IDs must match the requested exact catalog ID. Raw entity bodies are saved before parsing, including errors; 1 MiB caps retain marked prefixes on overflow, and unexpected compression is rejected rather than silently transforming the retained body. Declared version/settings and observed metadata are distinguished. Existing study fixtures remain automated sample-template reviews, not independent human approvals.

**Actual experiment:** [versioned report](docs/audits/round17_bound_protocol/REPORT.md), with [prespecified study](docs/audits/round17_bound_protocol/study.json), raw requests/responses, source hashes, observed runtime metadata, model digests and per-run reports in the same directory. Section 16 archives the original local results byte-for-byte; none of the original 74 benchmark records was edited.

Ollama **0.40.15** was observed through its API; the two tested model digests and runtime version were unchanged in before/after checks. The frozen primary experiment used `gemma4:e4b` and `qwen3.5:4b`, each on the neutral-source false diagonal formula and true zero-diagonal identity, one attempt per pair. Temperature 0, max_tokens 1500, 600-second whole-run budget, at most two model requests per run; native thinking/context defaults and hardware load were not controlled. This is an integration smoke test, not a reliability estimate or controlled comparison with older protocols.

| Primary model | Correct request binding / replay | Strict final protocol | Observation |
| :--- | :--- | :--- | :--- |
| Gemma e4b | 2/2 | 0/2 | Both final answers wrapped exact matching fields in Markdown fences. Strict raw JSON parsing remains failed; fence-removed content matching is retained only as separate automated diagnostic inspection, not human review or a revised parse score |
| Qwen 3.5 4b | 2/2 | 0/2 | Both final phases hit the token limit with no final content. Completion failure, not an observed wrong mathematical conclusion |

All four primary runs made one correctly bound engine call and obtained the appropriate replayable proof/refutation. No proposition substitution was certified. Primary wall times were 59.114/44.810 seconds for Gemma and 196.731/170.770 seconds for Qwen; single attempts under uncontrolled runtime defaults do not support speed/reliability conclusions.

**Separate post-hoc follow-up:** added optional `--json-final`, requesting `response_format=json_object` only in the final phase and recording that condition. Default behavior and the four frozen attempts are unchanged. A separately declared single Gemma false-formula attempt passed strict JSON, exact references and the false outcome in **59.091 seconds**. This is not a replacement of the original failures, a reliability estimate, or evidence that JSON mode resolves Qwen's token-budget failure. The follow-up has its own declaration/source hashes/summary. Total actual inference: ten model requests across five bounded attempts, five verification invocations; no automatic retries or broad model sweep.

Validation after the final implementation:

- **Full offline suite: 1476 passed, 37 skipped, 47 subtests passed, no deselections**, 239.72 seconds, with the two existing map/spatial warnings. This ran with approved escalation so both the actual stdio test and mock-connected socket guard were included. Live Census tests stayed disabled. This supersedes prior partial/full totals for the current checkout.
- The **64 actual required verification cases** were extracted unchanged from that JUnit report and passed the actual embedded release-gate code locally, redirecting only its report filename. Remote tag publication/CI was not run or implied.
- Every one of the **five actual run records** was replayed against its pinned request; every listed run payload hash and all **74 original benchmark record hashes** matched. Initial and final runtime/model metadata were retained; the five per-run source snapshots distinguish the frozen condition from the JSON-format follow-up.
- **24 fake transport cases** cover opt-in, loopback URL validation, disabled proxies/redirects, final mode/JSON mode payloads, raw retention on HTTP/parse/compression errors, explicit truncation, exact model identity, MCP errors and exclusive output. Selected Ruff, Python 3.10 syntax checks and `git diff --check` passed.

No dependency install, live Census request, other remote-provider call, credential/host-configuration change, commit, push, version bump or release was performed. Older plans and supplied source documents remain unchanged. Remaining work is routine supported-version/Windows CI and the explicitly unresolved Q17-1 metadata-integrity policy; broader model reliability studies are outside this bounded round. Local-model authorization persists for relevant follow-up tests; it does not authorize paid/remote services or publication.

## 16. Round closure and working-tree consolidation (2026-11-13)

The user requested that all preceding uncommitted work be tied together.
This authorizes a local integration commit on `phase17/exact-claims-and-evidence`;
it does not authorize a branch merge, push, tag, release or publication.
Earlier no-commit statements in section 15 describe their historical validation
slices, before this consolidation request.

All agreed implementation slices 17.0 and 17.A–17.E are complete: I1–I7 and
the subsequent artifact-integrity fixes, pinned proposition/review/evidence
contracts, bounded checked rules, library/CLI/MCP/bundle integration, release
gating and bounded local-model evaluation. Local checks passed as recorded
above. Model final-answer failures remain failures; round completion does not
claim that either small model is scientifically reliable.

The integration includes all modified source, adapters, regression tests,
release configuration and documentation; this plan; and both supplied
architecture/audit reference documents unchanged. Previous Round 9–16 plans,
research datasets, fixtures and original benchmark files are preserved.

The previously ignored new model-study evidence is copied byte-for-byte into
[`docs/audits/round17_bound_protocol`](docs/audits/round17_bound_protocol/README.md),
with its own archive checksum inventory and line-ending protection. All five
archived runs replay against their archived pinned requests; the four primary
strict failures and separate JSON-mode success are retained. Literature PDFs
and unrelated ignored caches/outputs are excluded from the integration.

Deferred items are not hidden implementation stubs: routine Linux/Windows
Python 3.10–3.12 CI remains a publication check, Q17-1 remains an explicit
future policy decision, and broader model reliability studies/new methods
remain outside this round. The authoritative full offline result is
1476 passed, 37 skipped, 47 subtests passed, with no deselections; 64 required
verification cases passed the embedded release gate locally. Packaging and
isolated Python 3.12 checks retain their stated narrower scope.

Consolidation checks: all 83 original new-study files match their archived
copies; the 85-file archive inventory (excluding its own manifest) verifies;
all five archived strict scores replay unchanged; final runtime-study source
hashes match current code; both supplied document digests and all 74 older
benchmark hashes match. Original ignored literature/results remain in place.
Documentation/archive changes add no new implementation requiring another
model run or repetition of the already passing full suite.

All 130 staged files were checked against their working-tree bytes, including
the archive's 85 checksummed payloads. Staged whitespace checks are clean for
the implementation and archive; the supplied audit's four original Markdown
hard-break lines are preserved unchanged and excluded from that whitespace-only
check. Both input documents retain their original SHA-256 values.

## 17. Remote integration and release preparation (2026-11-13)

The user authorized push, integration, release preparation and then a Round 18
plan, and subsequently selected direct terminal integration. The connected
GitHub app could read CI but PR creation returned 403; the existing SSH key
pushed the exact local commit without changing credentials or the configured
remote. GitHub host keys were obtained over verified HTTPS and used from a
temporary file. No contributor agreement was signed by the agent.

All six Linux/Windows Python 3.10, 3.11 and 3.12 jobs passed on commit
`6ed979ac573060fa27ea973617ee436e2edc3705` in
[CI run 43535062102](https://github.com/cafeTechne/Census-Research-Engine/actions/runs/43535062102).
The clean default branch was fast-forwarded and pushed to that exact tested
commit. No force push, PR-policy override or history rewrite was used.

The PR-only inference smoke command was run locally with its existing
criteria: `scripts/inference_validation.py --fast --gate`. It passed; its
Markdown/CSV outputs are under `/tmp/r17-release-preparation`. This is a smoke
check with predetermined Monte Carlo tolerance, not full coverage evidence.
The full numerical release gate is being checked separately.

Version `0.16.0` is prepared consistently in the package, pipeline, citation
and changelog, with the user-local date 2026-11-13. Focused packaging and
verification checks passed (59 tests). Candidate sdist/wheel files built from
a temporary source snapshot; importing the wheel outside the checkout
confirmed version 0.16.0 and reproduced the exact false-formula refutation.
No dependency installation or source-data regeneration was needed.

These are release-preparation changes. No release tag was pushed, GitHub
release published or Zenodo archive triggered. The candidate metadata commit
will receive its own ordinary push CI. Round 18 planning follows successful
integration/release-readiness checks and does not imply implementation of
that next round.
