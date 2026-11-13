# Census Research Engine — Codebase-to-Requirements Gap Audit

**Audit date:** 2026-11-11  
**Authoritative planning reference:** *Census Research Engine & Research Intelligence Platform — Architecture, Scientific Integrity, and Development Plan*, v10 (2026-11-11).  
**Public repository:** `cafeTechne/Census-Research-Engine`  
**Inspected branch and commit:** `defaultBranch` at `faae55038e61c1239282bfd3c57ae42fd5b9396e`  
**Method:** Read-only static inspection of repository source, tests, workflow configuration, and documentation via the connected GitHub repository interface. **No code executed, tests run, or repository files changed.** The owner's local, possibly uncommitted branch was **not inspected**.

## 1. Executive determination

**Recommendation: extend the existing research-integrity architecture; do not create an independent, competing verification platform.** The project already possesses canonical manifest hashing, independent evidence checks, provenance-bearing results, preregistration/exploratory separation, MCP tool profiles, typed failure codes, extensive statistical testing, and differentiated release gates. These features are implemented, though this review has not independently executed their tests.

The newly identified unmet requirement is narrower but serious: **a general contract that binds the exact mathematical proposition requested to its formalization, assumptions, verification procedure, and returned evidence**. The current repository is not shown to implement such a contract. This conclusion is based on the inspected relevant files and repository searches; it is not a guarantee about every file, branch, or uncommitted change.

### Recommended priority

- **P0 — Before claiming verified mathematics:** build a narrowly scoped proposition/evidence contract with fail-closed mismatch handling and a human-reviewable natural-language formalization step.
- **P1 — Same implementation milestone:** add exact symbolic/counterexample verification for the matrix identities motivating this work, with a strict evidence taxonomy and parser-safe outputs.
- **P1 — Integrate, don't duplicate:** expose this capability through existing library/MCP/CLI patterns and run its frozen regressions in the current CI matrix.
- **Defer:** general-purpose theorem proving, autonomous formalization approvals, an independent verification server, a new database, or a new orchestrator framework.

## 2. Requirement-to-code evidence matrix

| v10 obligation | Published repository evidence | Assessment | Minimum remaining work |
|---|---|---|---|
| Stable canonical representations and digests | `census/manifest.py` lines 228–236: `canonical_json` and `manifest_hash`, SHA-256 of canonical JSON; `tests/test_manifest.py` lines 68–79 verify key-order invariance and mutation sensitivity. | **Existing pattern, not proposition-specific** | Define **versioned, typed proposition AST**; hash expression, assumptions, dimensions, quantifiers, scope and relevant formalization provenance. Keep proposition IDs separate from analysis-manifest hashes. |
| Evidence must correspond to the claimed object | `census/concordance.py` lines 744–755 gives evidence a digest-derived ID; lines 810–825 rehash saved evidence, check its covered (year, code), regenerate source evidence, and reject stale metadata. | **Strong existing precedent, not mathematical binding** | Require strict request/response equality of `proposition_id`, full digest, assumptions digest, and expression/schema version; reject related-but-different claims even when a tool reports `holds=true`. |
| Preregistration and fail-closed analysis status | `census/manifest.py` lines 120–138, 160–171, 979–996, 1131–1178; `_tag` around 1119; `census/workflows_inference.py` lines 742–782. | **Implemented for analyses** | Never elevate an unreviewed formalization or a proposition mismatch to `verified`; keep proposition verification distinct from `confirmatory` analysis status. |
| Provenance and result contracts | `census/results.py` lines 267–350 has `ModelResult`, `SmallAreaResult`, `CorrelationResult` with provenance; `census/manifest.py` lines 1880–1935 binds bundle outputs to manifest hash. | **Implemented, extensible** | Add a dedicated `VerificationResult` or clearly namespaced record, not an overloaded `ModelResult`. Preserve assertion, evidence class, method, assumptions, versions, inputs, and optional witness. Include links in existing bundles where applicable. |
| Deterministic mathematical authority | Statistical tools use NumPy/SciPy/PySAL/statsmodels; `census/validation.py` has independent Census-published reference checks. `pyproject.toml` lines 22–41 does not list SymPy. | **Strong computational baseline; general formal proof not demonstrated** | Implement restricted symbolic operations using trusted construction (optionally SymPy); prove within supported domain or return `inconclusive`; allow exact counterexample to refute a universal identity; numeric coincidence never proves one. |
| Restricted AST and safe expression handling | Manifest validates supported methods, variables, formula scopes (`census/manifest.py`); no arbitrary proposition AST or verification-safe formula grammar was identified in inspected paths. | **Gap** | Reject unsupported expressions, unknown symbols, incompatible shapes, unauthorized functions, ambiguous quantification; never `eval` arbitrary model expressions or `sympify` untrusted strings without a restrictive parser. |
| Natural-language-to-formal equivalence | `docs/agent_protocol.md` and crosswalk review require independent review for ambiguous source meaning. No general mathematical formalization approval workflow identified. | **Gap with reusable review principle** | Keep original text plus generated formalization. Mark it `pending_review` until approved by an appropriate reviewer or independently justified narrow template; hashing cannot establish semantic faithfulness. |
| Structured tool errors and bounded orchestration | `census/errors.py` lines 1–45 standardizes `success=false`, `error_code`; `census/workflows_inference.py` and `census/mcp_profiles.py` provide summaries and next-step guidance. | **Partially implemented** | Add typed verification errors (proposition mismatch, unsupported grammar, shape, missing evidence, inconclusive). Duplicate suppression and bounded final-answer behavior belong mainly to the client/orchestration layer; verification must remain fail-closed regardless. |
| MCP exposure to small local models | `mcp_server.py` lines 1050–1124 registers shared workflow tools; `census/mcp_profiles.py` lines 30–64 and 228–320 defines `core`, `core_spatial`, `review`, `full`. `tests/test_mcp_profiles.py` restricts core size/description complexity. | **Existing extensibility** | Introduce an explicit verification tool/adapter; avoid silently bloating the 14-tool `core` profile. Consider a dedicated compact `verification` profile; test schema and tool discovery. No model runtime belongs inside the scientific engine. |
| Independent test/reference framework | `tests/test_interpretation_contract.py`, `tests/test_spatial_lambda_correction.py`, `census/validation.py`, `docs/audits/CHECK_MAP.md`, `scripts/inference_validation.py`, `.github/workflows/ci.yml`. | **Implemented; no identified proposition substitution regression** | Add frozen adversarial cases and positive controls as ordinary pytest tests; document verification cases in `CHECK_MAP.md`. Tests must run on Linux/Windows, Python 3.10–3.12 CI. |
| Release blocking for mathematical verification | `.github/workflows/ci.yml` runs pytest matrix on pushes/PRs; `scripts/inference_validation.py --fast --gate` on PRs; `CHECK_MAP.md` distinguishes `gated` from `info` studies. | **Existing gate mechanism with scope limitation** | New verification tests should hard-fail pytest/CI. Do not rely on an `info`-only spatial study to enforce the mathematical invariant. Decide separately whether any expensive verification study merits a release-time gate. |
| Scientific spatial validation | `PLAN_ROUND_16.md` says implemented/released as v0.15.0; `census/lisa_validation.py`, `tests/test_spatial_lambda_correction.py`, `docs/audits/lisa_validation.md`, `docs/audits/spatial_regression_validation.md`. | **Substantial existing research** | Do not schedule the already implemented Round 16 studies as fresh work; preserve their limitations (many rows remain informational, not release-gated). |

## 3. What must be kept separate

**(a) The research manifest** describes a preregistered data/analysis design and determines confirmatory vs incomplete vs exploratory **analysis** status. **(b) The proposition record** identifies an exact mathematical statement, formal assumptions, scope, and canonical representation. **(c) The verification evidence** records what deterministic computation established **about that exact proposition**, and whether its status is proof, counterexample, diagnostic, inconclusive, or error. **(d) Formalization approval** establishes whether a proposed formal expression is an adequate representation of the researcher's original statement; a content hash can preserve the choice, but cannot prove the translation is faithful.

A valid manifest does not imply the mathematical statements in narrative conclusions were verified. A valid mathematical proof does not imply the associated empirical research is confirmatory. Preserve both statuses independently.

## 4. The narrowly scoped implementation seam

### Phase 0 — local-tree reconciliation (no changes)

Before implementation, compare the public commit above against the actual protected working tree. Several hundred modified/untracked local files were reported previously; do not replace, clean, or automatically move them. Check whether an in-progress mathematical verification module already exists and whether Round 16 source changes have additional unpublished work.

### Phase 1 — contract and hand-checkable golden cases

Implement a versioned, allowlisted proposition representation for a limited matrix-algebra domain: `matrix_multiply`, `transpose`, `diagonal`, `row_sum`, `indexed_sum`, `equality`, matrix/vector dimensions, symmetric/diagonal/zero-diagonal assumptions, and universal quantification over valid shapes. Define exact representations for symbols and index binding. For the first iteration, **do not** accept arbitrary text-to-SymPy evaluation; parse a typed JSON AST and construct trusted SymPy objects from approved nodes, or use exact arithmetic for small witnesses.

Proposed fields: `proposition_id`, `schema_version`, `original_text`, `formal_expression`, `formalization_status`, `assumptions`, `expression_digest`, `assumptions_digest`, `submitted_by`, `reviewed_by` (where applicable). All identity-sensitive fields must be covered by an immutable record identity or independently checked digests. Define canonicalization precisely; raw Unicode or `repr` should not establish mathematical equivalence.

Verification result fields: `proposition_id`, `expression_digest`, `assumptions_digest`, `verification_status` (`proved`, `refuted`, `diagnostic_only`, `inconclusive`, `error`), `claim_holds` (`true`, `false`, or `null`), `evidence_verified` (whether the proof or counterexample was independently checked), `evidence_kind`, `method`, `tool_version`, `witness`/`derivation`, `reproducer`, and an error code when not verifiable. `evidence_verified=true` must never be inferred merely from an LLM summary or a result of a different proposition. A valid refutation has `claim_holds=false` while the counterexample itself may be verified; avoid conflating verified **evidence** with a true **claim**.

### Phase 2 — tests before MCP exposure

Add `tests/test_proposition_verification.py`, ideally also `tests/test_verification_contract.py`, with independently constructed expected results. Required cases:

1. `diag(W Ω) = 0` where `W` has zero diagonal and `Ω` is diagonal: prove within the stated domain.
2. `(W Ω)_{ii} = Σ_j w_ij σ_j²` under the same assumptions: **refute** by an exact valid counterexample; never substitute case 1. For example, `W=[[0,1],[0,0]]` and `Ω=diag(2,3)` give `(WΩ)₁₁=0` but `Σⱼ w₁ⱼσⱼ²=3`.
3. `(W Ω 1)_i = Σ_j w_ij σ_j²`: prove under matching dimensions.
4. `diag(W Ω Wᵀ)_i = Σ_j w_ij² σ_j²`: prove under diagonal Ω.
5. Centering idempotence `M²=M`, where `M=I-11ᵀ/n`, `n≥1`: prove.
6. Centered independent nondegenerate homoscedastic errors are independent: refute for `n≥2`, using off-diagonal covariance. Explicitly state any probabilistic assumptions needed to interpret covariance as independence.
7. **Binding attacks:** swap claim IDs, indices, transpose, row sum vs diagonal, dimensions, sign, quantifier, assumptions, digests, schema version, and tool-result IDs; each must change identity or fail closed.
8. **Evidence distinctions:** a numerical match is `diagnostic_only`; a single exact counterexample to a universal claim is `refuted`; unsupported symbolic cases are `inconclusive`; an exception is `error`.
9. **Interchange/serialization:** JSON rejects invalid escapes rather than silently converting them to mathematical errors; all supported output is schema-valid and reproducible.
10. **Unreviewed formalization:** even a valid proof of an LLM-proposed expression cannot certify equivalence to the original prose without the specified review procedure.

Do not use the same helper under test to construct the expected identity or witness. Derive some outcomes independently by hand or from algebraically different trusted implementations. Record exact assumptions with every test.

### Phase 3 — minimal integration into existing interfaces

- **Library:** a narrow `census/verification.py` (or `census/verification/` if contracts/operations warrant separation). Keep public APIs domain-neutral enough to move to a shared package later; don't create a new microservice merely for directory structure.
- **Workflow adapter:** `census/workflows_verification.py` following `workflows_inference.py` JSON/error patterns; add a narrowly scoped tool to `mcp_server.py` and, if warranted, to `python -m census tool` dispatch.
- **Profiles:** add a small, explicitly opt-in verification profile, not another complex nested-argument tool in the local-model `core` profile without measuring usability.
- **Provenance:** link result records to the verified proposition digest and study/manifest bundle, keeping *analysis status* and *verification status* separate.
- **Errors:** add verification-specific codes and fail-closed response payloads to `census/errors.py`.
- **Tests and documentation:** register new tests in ordinary CI, update `docs/audits/CHECK_MAP.md` and `docs/agent_protocol.md` with binding and human-review rules, and add a reproducible example and a changelog note when shipped.

### Phase 4 — model orchestration, outside mathematical authority

Only after the deterministic contract passes, rerun Gemma and Qwen on proposition-preserving multi-step tasks. Report separately: original-text formalization correctness, correct tool arguments, duplicate calls, completion, schema adherence, final interpretation, latency. Require models to carry the immutable record ID; **the server validates it independently**. Bounded tool cycles and forced final-answer phases are client policies, not proof-producing algorithms.

## 5. Mandatory release-blocking acceptance criteria

A mathematical result may be marked **verified** only if all of the following hold:

1. The submitted claim's formalization is supported and has the required review status; its original statement remains linked and unaltered.
2. Every referenced symbol, index, dimension, assumption and quantifier is accepted by the restricted parser.
3. The verifier checks a frozen canonical proposition/digest; request, evidence and response have matching identities.
4. The deterministic operation returns a supported evidence class: exact proof within its assumptions or a valid exact counterexample; partial/numeric checks are not mislabeled as universal proof.
5. No tool error, schema failure, identity mismatch, unsupported operation, ambiguous formalization, or incomplete evidence has been masked by model prose.
6. The frozen adversarial test suite passes in the existing CI matrix. A failing or omitted required regression blocks release of the **verification feature**. Any broader product release with the feature disabled should explicitly avoid claiming mathematical verification.

## 6. Planning-document corrections suggested (not applied)

1. Change the P0 description from **“build a new scientific verification core”** to **“audit and extend existing `census` integrity, evidence, result, MCP and test contracts with proposition-specific safeguards.”**
2. Name the existing **concordance evidence revalidation** mechanism as a preferred design precedent (with a separate mathematical schema and full digest).
3. Introduce an explicit **formalization-review** requirement. This is not solved by proposition hashing alone.
4. Separate **verified mathematics** from **confirmatory empirical analysis** in both schemas and documentation.
5. Record Round 16 as implemented/released on the reviewed public branch. Replace stale references to “work toward Round 15”; remote access remains deferred pending a renewed security/readiness review rather than being assumed approved by elapsed round number.
6. Make the release gate explicit: CI's ordinary pytest matrix is blocking, but many spatial simulations in `CHECK_MAP.md` are **informational**, not gates.
7. Preserve the local branch's uncommitted work as a hard implementation constraint. Require reconciliation before any agent edits code.

## 7. Issues intentionally unresolved by this static audit

- **Working-tree divergence:** The public commit cannot establish what the owner's local branch already contains; no statement here should be interpreted as a review of 200+ local edits or untracked files.
- **Runtime behavior:** Source inspection is not test execution. CI green status, numerical outputs, tool-response transport, and performance were not independently rechecked.
- **Complete architecture:** This audit targeted the four agreed interface families and related code; it is not a security audit or every-line repository review.
- **SymPy dependency decision:** SymPy is not a declared core dependency in `pyproject.toml` at the reviewed commit. Decide whether to add an optional `verification` extra plus a dedicated CI job, or introduce a core dependency, **after** the restricted contract is specified; keep the dependency declarations synchronized.
- **Proof scope:** A symbolic algebra engine may show an identity under a specified domain, but unrestricted mathematical claims cannot be automatically certified. A finite witness refutes a universal proposition only when it satisfies the assumptions and refutes the **same formal proposition**.

## 8. Source index (immutable commit links)

- Manifest hashing, preregistration, statuses: [census/manifest.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/manifest.py#L228-L236), [manifest tests](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/tests/test_manifest.py#L68-L79).
- Evidence digests and review: [census/concordance.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/concordance.py#L744-L825), [agent protocol](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/docs/agent_protocol.md).
- Results/provenance: [census/results.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/results.py#L267-L350), [bundle assembly](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/manifest.py#L1880-L1935).
- MCP and errors: [mcp_server.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/mcp_server.py#L1050-L1124), [census/mcp_profiles.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/mcp_profiles.py#L228-L320), [census/errors.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/errors.py).
- Validation: [census/validation.py](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/census/validation.py), [CHECK_MAP.md](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/docs/audits/CHECK_MAP.md), [CI workflow](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/.github/workflows/ci.yml), [Round 16](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/PLAN_ROUND_16.md).
- Packaging and dependency baseline: [pyproject.toml](https://github.com/cafeTechne/Census-Research-Engine/blob/faae55038e61c1239282bfd3c57ae42fd5b9396e/pyproject.toml).

---
**Bottom line:** *Implement a verified proposition-to-evidence link by reusing the repository's existing manifest, evidence, results, error, MCP, and CI patterns. Do not reproduce the project's already mature validation infrastructure. First reconcile the public commit with the active local branch; then implement the narrow contract and frozen adversarial tests.*
