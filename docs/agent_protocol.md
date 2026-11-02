# Agent Protocol: Extraction, Concordance Review, and Overrides

This protocol governs how agents (and humans) extract multi-year ACS data, decide whether variables are comparable across releases, and record those decisions. The MCP server returns this document from `get_research_protocol`; its rules are enforced in code, not only described here.

Agents are expected to run every step below, including review. The safeguards come from independence (the drafter never reviews its own decision), evidence the code generates and re-verifies, and primary sources, not from requiring a human.

## 1. Why this exists

ACS variable codes do not have stable meanings across releases. The offline audit (`docs/audits/concordance_audit.md`) found, across 2009–2024:

- lines renumbered under new codes (for example, DP02 "average household size" moved from `DP02_0015` to `DP02_0016` in 2019);
- codes reused for different measures (`DP02_0002` was "family households" in 2018 and "married couple family" in 2019);
- definitions changed under the same code (`B27001_006` went from "6 to 17 years" to "6 to 18 years" in 2017);
- large label restructurings in subject tables (2013, 2017) and data profiles.

A panel keyed by code silently mixes these. The pipeline therefore keys columns by **concept** and refuses to merge anything it cannot verify.

## 2. Standard extraction workflow

1. **Find the table and variables.** `search_tables`, then `list_variables` or `describe_variable(code, year)`. Codes are vintage-specific, so check the label in the years you need.
2. **Name your variables.** Pass `variables=[...]` with the codes your analysis uses. Accepted forms:
   - `CODE` (for example `B19013_001` or `DP02_0016PE`): resolved in the **latest** requested year where the code exists;
   - `YEAR:CODE` (for example `2018:DP02_0002`): the measure that code denoted in that year;
   - a concept ID (`c_…`) from a concordance report.
   Always read the plan's `selection` field to confirm each input resolved to the measure you meant.
3. **Plan.** `plan_extraction(...)` with the same arguments as `batch_download`. It returns API call counts, cached calls, the concordance summary, issues, advisories, and `would_block`. It downloads no data; it makes at most one national-level call per year for the continuity check.
4. **If `would_block` is false,** run `batch_download`.
5. **If `would_block` is true,** read `issues` (blocking ones first) and follow section 3. Do not switch to `concordance_mode="code"` or `"strict"` to get around a block for confirmatory analysis.

Single-year requests never block on concordance, because there is nothing to compare. Collating single-year pulls yourself is allowed, but compare codes and labels across years with `describe_variable` or `check_variable_concordance` before pooling them. That is exactly what the concordance does for you.

## 3. Concordance modes

| Mode | Behavior | Use for |
| :--- | :--- | :--- |
| `fail` (default) | Stops with `CONCORDANCE_REVIEW_REQUIRED` if any blocking issue touches a selected variable | Hypothesis tests, anything published or reused |
| `strict` | Proceeds; unresolved items stay in separate columns; issues are listed in the result | Exploration and whole-table browsing |
| `code` | Keys columns by raw code with no checks; multi-year needs `allow_unverified_concordance=True` | Reproducing legacy outputs only |

`fail` is the default because a silently pooled column changes test results without any visible symptom, while a block only costs a review step.

## 4. Issue kinds

| Kind | Blocking | Meaning | Usual resolution |
| :--- | :--- | :--- | :--- |
| `relabeled` | Yes | Same code, similar but different label across years | Review: `merge` if the wording change is cosmetic, `split` if the measure changed |
| `ambiguous` | Yes | Two codes in one year share a label | Review against the table shell; `split` or `exclude` |
| `universe_changed` | Yes | Table universe text differs across years | Review; usually `split` |
| `continuity_flag` | Yes | National values jump significantly and implausibly between adjacent years | Investigate; `split`, or `merge` with a written `continuity_exception` |
| `continuity_unavailable` | Yes | National data missing, so continuity could not be checked | Retry when the API is reachable |
| `metadata_unavailable` | Yes (multi-year) | A year's variable metadata could not be loaded | Refresh the archive (`scripts/fetch_metadata_archive.py`) |
| `invalid_override` | Yes | A crosswalk row failed validation | Fix or remove the row |
| `redefined` | No | Same code, clearly different measure; already split | None |
| `possible_continuation` | No | One concept ends where a similar one starts; kept separate | Optionally `merge` through review |
| `table_absent` | No | The table is not published in that year | None |

Thresholds for `continuity_flag`: the change is significant at 90% (Z-test with the overlapping-period adjustment), and it exceeds 25% relative change (counts and dollars) or 10 percentage points (percentages).

## 5. Override workflow (draft, then independent review)

Decisions are recorded in `crosswalks/<dataset>/<table>.csv`. Each row maps one (year, code) to a decision.

| Decision | Effect | When |
| :--- | :--- | :--- |
| `merge` | Moves the (year, code) into the target concept, so one continuous column | Same measure, cosmetic relabel or renumbering that automatic matching missed |
| `split` | Confirms the (year, code) is its own concept and resolves the review issue | The measure changed |
| `exclude` | Drops the (year, code) from outputs | The line should not be used at all |

**When in doubt, choose `split`.** Keeping two measures apart never corrupts data, while a wrong merge does.

### Step A: Drafter

1. Run `check_variable_concordance(table_code, start_year, end_year, variables=[...])` to get the issues, concept IDs, and evidence.
2. Consult a Census primary source: the release's table and geography changes documentation, table shells, or user notes on `census.gov`.
3. Call `draft_crosswalk_override(...)` with your identity in `drafted_by`. The tool generates the evidence (labels, universes, similarity) from archived metadata, runs the continuity check for merges, saves `crosswalks/evidence/<evidence_id>.json`, and returns draft rows with an empty `reviewed_by`.
4. Hand the draft rows and evidence ID to a different reviewer. Do not review your own draft.

### Step B: Reviewer (agent or human)

1. Use a different identity from the drafter (a different agent session, a different model, or a human).
2. Re-derive the conclusion independently: read the evidence file, open the cited `source_url`, and inspect the labels yourself with `describe_variable`. Do not rely on the drafter's rationale.
3. If you agree, call `record_crosswalk_review(row, reviewed_by=<your identity>)`. If you disagree, do not record the row; draft the opposite decision instead, which then needs its own reviewer.

### Requirements enforced by `validate_crosswalks` and at runtime

All rows:
- `rationale`, `drafted_by`, `reviewed_by`, and `reviewed_on` are present;
- identities look like `human:<name>` or `agent:<model>/<session>`, and `reviewed_by` differs from `drafted_by`;
- the code exists in that year's archived metadata.

`merge` rows additionally:
- `source_url` is on `census.gov`;
- `evidence_id` names a file in `crosswalks/evidence/` whose content hash matches its ID and whose metadata still matches the archive when regenerated, so hand-edited or stale evidence is rejected;
- the evidence's continuity check passed, or `continuity_exception` explains why the discontinuity is real.

Invalid rows are never applied; they surface as `invalid_override` issues.

### Identity convention

- Humans: `human:<name or handle>`
- Agents: `agent:<model-id>/<session-or-run-id>`, for example `agent:claude-opus-5-5/2026-11-01T21-40Z-a7f3`

Use a stable session identifier, so that an audit can confirm the drafter and reviewer were different sessions.

## 6. Price indexes

Constant-dollar conversion defaults to **R-CPI-U-RS**, the index the Census Bureau uses for ACS inflation adjustment. CPI-U is available with `price_index="cpi-u"`. Index data lives in `data/price_indexes/` with source checksums. If a year is missing, the run fails with `PRICE_INDEX_UNAVAILABLE`. Do not supply guessed values through `cpi_table`.

Home values in releases before 2022 were not on the period end-year dollar basis (ACS user note 2023-07). Plans include an advisory when such tables are deflated.

## 7. Error codes

Branch on `error_code`, not on message text: `INVALID_INPUT`, `INVALID_GEO_LEVEL`, `INVALID_OUTPUT_FORMAT`, `INVALID_DATASET`, `INVALID_VARIABLES`, `VINTAGE_UNAVAILABLE`, `PRICE_INDEX_UNAVAILABLE`, `CONCORDANCE_REVIEW_REQUIRED`, `UNVERIFIED_CONCORDANCE`, `METADATA_UNAVAILABLE`, `API_HTTP_ERROR`, `NO_DATA`.

## 8. Outputs to cite and keep

Every run writes `<table>_extraction_metadata.json`. It records the package version, git commit, metadata checksum per vintage, the concordance mode, the normalization version, the overrides applied, the price index and its source, and the advisories. Keep it alongside results. `<table>_concordance.csv` maps every output column back to its code in each year.
