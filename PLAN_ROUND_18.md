# Round 18: Metadata Integrity and a Reproducible Research Workflow

**Date:** 2026-11-13
**Status:** Complete. All three implementation slices, final offline validation and all six Linux/Windows CI jobs passed.
**Baseline:** Round 17 integrated into `defaultBranch`; release candidate 0.16.0 at `d33c7259181194c881dfc325cff1cfa5d53354d4`. Publication status and final checks are recorded below.

## 1. Outcome

A researcher should be able to explain which metadata bytes informed an analysis,
reproduce one supported workflow from stored inputs, and distinguish empirical
results from separately reviewed mathematical evidence. Deliver that through the
existing loaders, workflows and documentation, with a small maintenance scope.

This round resolves Q17-1 by making bundled metadata checksums a runtime
contract under the bounded defaults adopted with the implementation request.
This is an input integrity policy, not a newly confirmed mathematical bug.
The scope remains the existing loaders and researcher path, without a new
research platform or estimator suite.

## 2. Preserve the completed foundation

| Existing capability | Reuse in this round |
| :--- | :--- |
| Round 12/13 reference workflow and acceptance tests | Extend only where the metadata policy or Round 17 behavior needs coverage; do not rebuild the workflow |
| Product/vintage safeguards, extraction provenance and crosswalk validation | Preserve source identity, missing-vintage behavior and existing comparison rules |
| Price-table integrity loaders and typed errors | Reuse the design where appropriate; metadata repair guidance must identify metadata, not price files |
| Round 17 proposition binding, review, proof/refutation and evidence replay | Preserve the protected operator context and narrow domain; never add model-facing approval |
| Fresh bundles, filename confinement, complete inventories and figure collision guards | Retain the existing tests and destination policy |
| Archived actual model study | Keep four primary strict failures and separate JSON-mode success unchanged; neither model is established as reliable |
| CLI/MCP workflow adapters and the 14-tool core profile | Reuse the existing routes and discovery boundaries |

No evidence or earlier plan is superseded by omission. Original datasets,
checksummed fixtures, supplied architecture documents and benchmark records
remain unchanged. New reproductions use temporary synthetic archive copies.

## 3. Decisions to freeze before coding

| Decision | Recommended bounded policy | Required clarification/evidence |
| :--- | :--- | :--- |
| D18-1 Bundled archives | Verify expected checksum on first actual read, before parsing/normalization/use | Confirm this is the supported runtime contract and distinguish absent archive from present-but-corrupt archive |
| D18-2 Custom metadata directories | Record actual consumed-byte hashes and an honest caller-supplied/unverified status; do not imply official verification from directory selection | Decide how an explicitly supplied custom manifest is treated; preserve documented custom-data use |
| D18-3 Hash and cache identity | Hash the decompressed JSON bytes already read, matching the current manifest convention; cache verification with that source | Check resolved archive-root identity and cache clearing/configuration behavior; do not introduce a cache-manager service |
| D18-4 Failure semantics | Corrupt bundled metadata fails before extraction or fallback; missing archive retains its documented missing/API behavior | Keep source missing, integrity failure, malformed JSON and transport failure distinct; no silent API substitution for corruption |
| D18-5 Workflow boundary | One existing stored-input workflow plus a separately supervised evidence appendix | No mandatory proof step for every analysis; no prose translation or review automatically approved by a model |

The instruction to implement this plan adopted D18-1 through D18-5. Custom
manifest checksums are recorded as caller-supplied expectations, never promoted
to official verification. Resolved selection of the bundled root still enforces
the bundled contract. The implementation and compatibility policy are recorded
in [docs/metadata_integrity.md](docs/metadata_integrity.md).

## 4. Implementation slices

### 18.A — Metadata integrity contract and minimal enforcement (P0)

Inspect `census/metadata.py`: `_read_archive`, `_load_json`,
`_archived_variables`, `load_manifest` and `verify_archive_file`. Trace metadata
provenance through extraction and concordance consumers. Currently the explicit
verification utility reads through `_read_archive`; avoid introducing recursion
by having that reader call the utility back.

1. Reproduce the current behavior using a valid temporary gzip archive and
   manifest, then change a plausible label/concept without updating its checksum.
   Record what the public variable/group loader and extraction provenance report.
2. Freeze D18-1 through D18-4 and implement at the shared read seam. All archive
   consumers must receive the same verified bytes or the same explicit failure.
3. Preserve public lookup return shapes and supported valid data. A checksum is
   integrity against the trusted manifest, not authentication of the manifest or
   a guarantee of Census semantic correctness.
4. Reuse typed error handling and actionable repair paths across library, CLI,
   MCP and applicable GUI extraction paths. Do not silently downgrade an integrity
   failure into ordinary missing data or generic successful provenance.
5. Hash already-read decompressed bytes once per cached source. Avoid a second
   gzip read solely for verification. Define how archive-root/cache identity is
   retained; do not promise hot reload unless it is implemented and tested.

**Done when:** healthy bundled inputs preserve their results; present-but-corrupt
or unverifiable bundled inputs fail before use; custom-source status and actual
hashes follow the chosen policy; no provenance claims an unchecked expected
hash as verification of different consumed bytes.

### 18.B — Pressure-test the supported researcher path (P1)

Reuse `docs/reference_workflow.md`, `tests/test_reference_workflow.py`, existing
manifest examples and frozen inputs. The reference workflow already checks
reproducibility and library/CLI/MCP agreement; add only new contract cases.

- Run the existing healthy path into a fresh directory and compare substantive
  results with its established independent expectations.
- Exercise an integrity failure through a consuming entry point, not only a
  helper. Assert no extraction API call, current-run bundle or success claim is
  produced after a corrupt bundled archive is detected.
- Preserve ACS1/ACS5 identity and replicate restrictions and explicit legacy
  compatibility. Matching geography/year alone does not establish a product.
- Link the existing verification guide as a separate operator-supervised
  appendix. Its reviewed request and evidence retain their own identities;
  empirical analysis status is never upgraded by a proof or model answer.
- Keep the reference document's existing CLI-first contract. Do not replace it
  with a notebook, a second CLI application or an automatic approval command.

**Done when:** another researcher can replay the stored-input workflow and its
failure case using the documented commands, identify the metadata source/hash
and empirical status, and separately inspect/replay any included exact evidence.

### 18.C — Fix only observed friction and consolidate the handoff (P1)

Keep a short list of concrete problems found in 18.A/B: source file/function,
reproducer, expected behavior, affected interfaces and validation. Fix the small
adapter/documentation issues needed by that path. Questions remain questions;
unrelated features return to a separate backlog.

Update the existing guide, CHECK_MAP and changelog. Record compatibility changes
and the agreed metadata/cache policy in one short note. Reuse current provenance
fields/helpers where possible. A breaking manifest migration, new dependency or
wide refactor requires a separate proposal, not silent expansion of this slice.

## 5. Regression and validation requirements

- Temporary healthy, checksum-mismatched, missing-checksum, missing-file,
  malformed-gzip and malformed-JSON inputs; include valid JSON with scientifically
  plausible altered labels so parsing success cannot mask a mismatch.
- Correct decompressed-byte convention: gzip-header/recompression differences
  must not be confused with changed JSON payloads.
- Public variable and group consumers; custom-directory behavior; cache/source
  identity and the documented repair/restart path. A failed read must not poison
  a later healthy run or reuse a different archive root's approved bytes.
- Mock the API fallback and assert corruption causes zero calls. Missing-source
  fallback is tested separately under the existing policy.
- Library/CLI/MCP parity and applicable GUI error propagation. Preserve meaningful
  metadata-specific repair guidance and useful source information.
- Existing source identity, reference workflow, bundle, proof/evidence and release
  controls remain passing. Check archived evidence hashes and strict replay scores
  without rewriting records or reinterpreting model formatting failures.

Use independent expected bytes/values and consuming APIs. Do not create tests
that merely repeat a new helper's implementation or weaken scientific tolerances.
Run focused tests first, then the existing offline suite and Linux/Windows
Python 3.10–3.12 CI. Run applicable numerical studies only if a statistical method
or assumption changes; metadata work is not a reason to rerun every estimator.
Record representative first/warm-read cost without a flaky absolute timing gate.

## 6. Explicit exclusions

No general proof engine, new estimators/spatial models, literature retrieval,
vector store, workflow service, job database, GUI redesign, new model sweep,
automatic natural-language formalization approval or default model promotion.
No live Census requests, archive regeneration or source-data changes are needed
for the proposed reproductions. Local model calls are unnecessary for this round's
acceptance path; existing authorization is not a reason to spend model time.
No old plan, fixture, source document or archived experiment is rewritten.

## 7. Order and stop conditions

1. Confirm the baseline and freeze D18-1–D18-4 with the temporary reproducer.
2. Implement the shared loader/provenance/error contract and focused controls.
3. Extend the existing consuming workflow and fix the resulting narrow friction.
4. Run the appropriate suite/CI, record results and make a reviewable integration.

Complete the round when those three slices pass and the policy is documented.
Do not use open-ended research-tool ambition to add unrelated functionality.
If custom-data policy or compatibility cannot be reconciled within a small change,
stop that dependent slice with a concrete decision question; continue independent
reproduction/documentation work. Do not mark an unresolved implementation complete.

## 8. Baseline validation and publication state

The following checks actually completed before this plan was written:

- [Integration CI 43535062102](https://github.com/cafeTechne/Census-Research-Engine/actions/runs/43535062102): all six Linux/Windows Python 3.10–3.12 jobs passed at `6ed979a`.
- [Release-candidate CI 43537388947](https://github.com/cafeTechne/Census-Research-Engine/actions/runs/43537388947): all six jobs passed at `d33c7259181194c881dfc325cff1cfa5d53354d4`.
- Full offline baseline: 1476 passed, 37 skipped, 47 subtests passed, no exclusions. Release metadata/packaging checks: 59 passed. These counts overlap and are not additive.
- The predefined local inference smoke gate passed. The full numerical release gate also passed: 114 rows, all 11 gated rows passing under the unchanged full criteria, seed 20261102, Python 3.14.4, 17.9 minutes. Informational methods remain informational; criteria were not relaxed.
- Candidate 0.16.0 sdist/wheel assets built offline. Version consistency and an exact refutation were checked from the wheel outside the checkout. This is not a claim of general non-editable data-resource support.

Round 17 was fast-forwarded into `defaultBranch` and pushed through the terminal.
Version 0.16.0 is prepared with a local annotated tag at the tested candidate.
No release tag was pushed; no public GitHub release or Zenodo archive was
triggered. Publishing the tag is a separate action from the requested release
preparation and from this Round 18 planning document.

The release notes, candidate assets, smoke/full numerical reports and readiness
checks are retained under `/tmp/r17-release-preparation/` on this workstation.
That temporary directory is a local handoff, not a versioned publication.
The permanent Round 17 experiment remains in
[the versioned evidence archive](docs/audits/round17_bound_protocol/README.md).
No older plan, source document, benchmark record or research dataset was changed
while writing this plan.

## 9. Round 18 implementation and evidence

### Delivered slices

- **18.A:** One root-aware cached archive read hashes decompressed JSON before
  parsing, enforces bundled checksums and retains actual hashes for custom data.
  Variable and group consumers, including the older `census.tables` path, use
  that seam. A linked payload inside a bundled archive cannot bypass its checksum.
  Normalized-variable cache keys include the consumed digest, preventing stale
  values after eviction of a different bounded byte-cache entry.
- **18.B:** Existing stored-input reference commands and independent scientific
  expectations remain. Acceptance checks independently hash variable/group bytes.
  Corruption tests consume the library, CLI, MCP (including actual schema
  validation), and headless GUI worker, asserting zero API calls and no extraction
  output. Manifest validation checks present archives before geography probes;
  runs fail before creating current-run artifacts. Supervised exact evidence is
  linked as a separate optional appendix.
- **18.C:** The CLI/MCP adapters preserve the typed failure and metadata-specific
  repair step. Legacy lookup caches distinguish source paths and clear with the
  shared reset. Removed the GUI worker's redundant, unguarded default-metadata
  read so the pipeline reports corruption through its existing error queue.
  Workflow manifest adapters retain integrity failures instead of relabeling them
  as ordinary missing data. CHECK_MAP, changelog and policy/reference guides updated.

### Reproduction and frozen policy

Before implementation, a temporary valid gzip with `Total population` changed to
`Adult population` returned the altered public variable label while
`verify_archive_file` returned false. Extraction provenance used the manifest's
expected SHA rather than the consumed bytes. No repository archive was modified.
The new regression cases reproduce that plausible alteration for variables and
universes. Missing files still permit existing fallback; present corrupt sources
fail explicitly. Repair uses `census.metadata.clear_cache()` or process restart;
no file watcher, cache service or automatic archive regeneration was added.

Successful first-read snapshots are cached by resolved source root. Custom
manifest matches and mismatches both remain `caller_supplied_unverified`.
Existing lookup tuple shapes and healthy values are unchanged. Provenance additions
are additive; older replay records do not retroactively claim runtime verification.
The MCP table-search success value remains a list; its output type also permits
structured integrity failures. No dependency, estimator, release number or archive
format changed. Prepared plans must not be reused after changing input configuration.

### Validation record

- Final-source full offline suite: **1,507 passed, 37 skipped, 47 subtests
  passed**, Python 3.14.4, 252.55 seconds. All 30 new metadata integrity
  cases passed. The existing reference document executes twice into fresh
  directories and retains its independent scientific expectations and route parity.
- `python -m census validate-crosswalks`: valid. Focused changed-module lint
  (E4/E7/E9/F) and `git diff --check`: passed. Unrelated pre-existing lint
  findings in the CLI/manifest were not reformatted or folded into this round.
- Implementation CI: [43545297502](https://github.com/cafeTechne/Census-Research-Engine/actions/runs/43545297502),
  implementation commit `16acbbc0f2fdafbba41afddf09318dc8bf7108e8`. All six
  Linux/Windows Python 3.10–3.12 offline jobs **passed**, including crosswalk
  validation. Live validation was intentionally skipped. Windows skips the
  symlink-creation test because it requires optional privileges; that test passes
  on Linux. Existing optional/version-dependent skips account for other platform
  count differences. No workflow dispatch or release/tag publication.
- Representative actual bundled ACS5 2023 variable read, Python 3.14.4 on this
  workstation: **28,299 variables**, first verified read **0.0757 seconds**;
  mean warm lookup over 1,000 calls **9.04 microseconds**. Informational only,
  with no timing threshold. A synthetic control asserts one gzip read for lookup,
  checksum verification and provenance together.
- All **74** original benchmark JSON records and both supplied architecture/audit
  documents match their pre-Round-18 hashes. All **85** versioned Round 17 archive hashes verified. All five archived strict
  protocol scores replayed unchanged: four primary failures and one JSON-mode
  follow-up success. No model calls, approvals or external Census requests.
- No numerical method changed, so no new estimator simulation was required.
  Round 17's numerical release evidence remains unchanged.

### Handoff

Implementation is committed and pushed on `phase18/metadata-integrity` at
`16acbbc0f2fdafbba41afddf09318dc8bf7108e8`; the following documentation-only
commit records these validation results and does not change tested source.
All Round 18 slices are complete, with no implementation stubs left open.
The default branch and prepared local v0.16.0 release tag remain at the Round 17
baseline; this round does not merge or publish a release.
