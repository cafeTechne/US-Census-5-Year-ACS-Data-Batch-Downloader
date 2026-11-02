# PLAN_ROUND_6.md: Vintage-Safe Variables, CPI-U-RS, and an Agent-First Interface

**Planning Round:** 6
**Date:** 2026-11-01
**Status:** Implemented on `phase6/vintage-safe-pipeline` (see section 13), with one owner action outstanding
**Branch:** `phase6/vintage-safe-pipeline` (from `phase5/research-grade-pipeline`)

---

## 1. Summary

Round 5 produced a working research pipeline, but two of its assumptions are unsafe for multi-year research:

1. **A variable code is assumed to mean the same thing in every year.** Panels are keyed by variable code, and labels come from one metadata vintage (bundled 2021 `variables.json` for detailed tables, the final requested year for profile and subject tables). If the Census Bureau renumbers a Data Profile or Subject table line, or reuses a code for a different measure, the panel silently merges different quantities into one column, or splits one quantity across two columns. Nothing in the output reveals this.
2. **Constant dollars use CPI-U.** The Census Bureau uses CPI-U-RS for its own cross-year income comparisons. Our adjusted values will therefore not match Census-published comparisons.

Round 6 fixes both and treats agents (MCP and CLI) as the primary interface. The GUI moves to maintenance mode.

| Priority | Workstream |
| :--- | :--- |
| P0 | Vintage-aware metadata and a variable concordance that panels are built from |
| P0 | R-CPI-U-RS as the default deflator, with sourced and checksummed index data |
| P1 | Agent-first interface: dry-run planning, concordance tools, structured errors, CLI |
| P1 | Live API contract tests and real-data fixtures |
| P2 | GUI maintenance mode |

---

## 2. Problem: variable meaning drifts across ACS releases

### 2.1 How it fails today

| Change between releases | Example shape | Current behavior | Consequence |
| :--- | :--- | :--- | :--- |
| Line renumbered | Measure X is `DP02_0067PE` in year A and `DP02_0068PE` in year B | Two columns, each half-empty | Panel looks like the measure stops and a new one starts |
| Code reused | `DP02_0068PE` is X in year A and Y in year B | One column | Different measures pooled; regressions are silently wrong |
| Label reworded | Same code, edited wording | One column, latest label | Usually fine, but a wording change can hide a definition or universe change |
| Universe changed | Same code and label, different denominator | One column | Rates not comparable across years |
| Added or discontinued | Code exists only in some years | Partially empty column | Mostly visible, but undocumented |

Detailed tables (`B`/`C`) change less often, but they are exposed to the same failure because every year is labelled from one bundled vintage. Comparison Profiles embed the period in the code (`CP03_2017_001E`), so codes never match across releases and panels from them are currently not usable.

The examples above are illustrative. We have not yet measured how often each case occurs. The first deliverable (6a.1) measures it.

### 2.2 Design principle

The unit of a panel column is a **concept** (one measured quantity), not a variable code. Every (year, code) cell is mapped to a concept through an explicit, inspectable concordance. Matches are only automatic when they are exact. Anything else is reported and needs an explicit decision, either a reviewed override file or a user flag. The pipeline never guesses silently.

---

## 3. Workstream A (P0): vintage-aware metadata and concordance

### A1. Vintage discovery

- Read the Census API catalog (`https://api.census.gov/data.json`) to list the available years per dataset (`acs/acs5`, `acs/acs5/profile`, `acs/acs5/subject`, `acs/acs5/cprofile`).
- This replaces hard-coded year bounds (`MIN_YEAR`, `MAX_YEAR`, the `2022` default year in MCP tools) and makes new releases appear without code changes.
- New function `census.metadata.available_vintages(dataset) -> list[int]`. Results are cached with a time-to-live, because the catalog changes when new releases are published.

### A2. Per-vintage metadata registry

- **Offline archive (decided, done).** `metadata/<dataset>/<year>/{variables,groups}.json.gz` holds every published vintage: acs5 and profile 2009–2024, subject 2010–2024, cprofile 2015–2024 (114 files, 19 MB). Source URL, retrieval time, and SHA-256 of each raw response are in `metadata/manifest.json`. `scripts/fetch_metadata_archive.py` adds new releases, and with `--refresh` it reports, rather than overwrites, any published file whose checksum changed.
- Vintages missing from the archive fall back to the API (cached in SQLite), and the source used is recorded per year.
- `resolve_metadata(table, dataset, year)` always returns metadata for **that** year.
- Offline prefetch: `python -m census metadata prefetch --dataset profile --years 2012-2024`.

**Bundled metadata policy (decided, revised).** The root `variables.json` (2021 release) and `groups.json` (2020 release) were two different vintages. They are removed; identical content lives in the archive. The archive is now the offline fallback for every vintage. It supports table search and lookup without network access and keeps the test suite deterministic. A single vintage is never enough on its own for a multi-year panel in `fail` mode.

### A3. Label normalization

`canonical_label(label, concept, dataset)` produces the key used for matching:

- Strip measurement prefixes (`Estimate!!`, `Percent!!`, `Margin of Error!!`) and comparison-period prefixes (`2012-2016 Estimates!!`).
- Remove vintage-specific dollar text (`(in 2021 inflation-adjusted dollars)`).
- Normalize case, whitespace, trailing colons (`Total:` vs `Total`), and separator variants (`--`, `!!`).
- Keep the full hierarchy path, not only the leaf. `Total!!Male!!Under 5 years` and `Total!!Female!!Under 5 years` must stay distinct.
- Normalization rules are a versioned, tested list. Each rule has a test with a real before/after label pair.

### A4. Concordance builder

`build_concordance(table, dataset, years) -> Concordance` assigns every (year, code) to a `concept_id` and a status:

| Status | Rule | Auto-merged? |
| :--- | :--- | :--- |
| `stable` | Same code, same canonical label, same universe in every year | Yes |
| `renumbered` | Same canonical label and universe, different code | Yes, under one concept |
| `redefined` | Same code, different canonical label | No: split into separate concepts |
| `relabeled` | Same code, labels differ but similarity is above threshold | No: `needs_review` |
| `universe_changed` | Same code and label, different table universe | No: `needs_review` |
| `ambiguous` | Canonical label matches more than one code in one vintage | No: `needs_review` |
| `added` / `discontinued` | Present in only some years | n/a: recorded |

Details:

- A `concept_id` is a stable hash of (dataset, table, canonical label). Slugs are derived from the concept, not the code.
- Estimate, MOE, percent, and annotation suffixes travel together. `E`/`M` and `PE`/`PM` pairs are matched as a unit, so an estimate can never be paired with another year's MOE.
- Comparison Profiles: the period segment is removed from the code before matching, and the `_SS` significance flags attach to their concept.
- Similarity scoring uses `difflib` token ratios, which adds no dependencies. It is used only to rank candidate pairs for review, never to merge.

### A5. Reviewed overrides

- Directory `crosswalks/<dataset>/<table>.csv` with columns `year, code, concept_id, decision, rationale, source_url, evidence_id, drafted_by, reviewed_by, reviewed_on`.
- Decisions: `merge` (accept a `needs_review` pair), `split`, `exclude`. When in doubt, `split`. Leaving two concepts apart never corrupts data; a wrong merge does.
- Overrides take precedence over automatic rules.

**Review requirements (decided).** A `merge` row is valid only if all of these hold, and CI rejects the file otherwise:

1. **Machine evidence.** `evidence_id` references an evidence record generated by `check_variable_concordance`. It contains both vintages' labels, concepts, universes, and similarity score. The evidence is generated by code, never typed by hand.
2. **Primary-source citation.** `source_url` points to Census Bureau documentation that supports the decision, such as the release's table and geography changes documentation or the table shells. Third-party crosswalks (for example, NHGIS) may be cited as supporting evidence but never as the only source.
3. **Continuity check passes** (A9), or the rationale explains a real-world reason for the discontinuity.
4. **Independent sign-off (decided, revised).** Agents are expected to make these decisions. `drafted_by` and `reviewed_by` are identities of the form `human:<name>` or `agent:<model>/<session>`, and they must differ. An agent reviewer must be a different session from the drafter, and it re-derives the conclusion from the evidence and the cited source instead of relying on the drafter's rationale. The workflow is documented in the MCP server instructions, in `get_research_protocol`, and in `docs/agent_protocol.md`.
5. **Codes exist.** Every referenced (year, code) exists in that vintage's metadata.

### A6. Pipeline integration

- `run_batch` builds the concordance before downloading, using metadata only, which needs no data calls.
- New argument `variables=[...]` on `run_batch`, `batch_download`, and the CLI. It lists the variables (codes in any one vintage, or concept IDs) that an analysis depends on. Omitted means the whole table.
- New option `ResearchOptions.concordance_mode`:
  - `fail` (default): return `success: False` with `error_code: CONCORDANCE_REVIEW_REQUIRED` if any `needs_review` item, failed continuity check, or fallback-only metadata affects the selected variables. Drift in unselected lines of the table is reported but does not block.
  - `strict`: build columns by concept. `needs_review` items stay split by vintage segment (for example, `..._est__2015_2018` and `..._est__2019_2022`) and are listed under `concordance_issues`. The run succeeds. Intended for exploratory whole-table pulls.
  - `code`: Round 5 behavior (key by code). Allowed only for single-year requests, or with an explicit `allow_unverified_concordance=True`, which is recorded in the metadata.

**Why `fail` is the default (decided).** The panels feed hypothesis tests. A silently pooled column changes test results without any visible symptom, while a blocked run costs only a review step. Scoping the check to `variables=` makes this strictness practical: you name the variables your hypothesis uses, those are verified, and unrelated drift elsewhere in the table does not get in the way. This also works as lightweight pre-registration, because the selected variables and their concordance are recorded in the extraction metadata.
- New output `<table>_concordance.csv` with columns `concept_id, slug, year, code, label, status, decided_by`.
- The codebook gains `Concept_ID`, `Years_Available`, `Source_Codes_By_Year`, and `Concordance_Status`.
- Extraction metadata records the metadata checksum for each vintage used and the override files applied.

### A7. Scale audit (first deliverable)

`scripts/audit_concordance.py` sweeps every profile, subject, and comparison-profile table, and a sample of detailed tables, across all vintages. It reports counts per status and lists every `renumbered`, `redefined`, and `needs_review` case. Outputs:

1. A report committed under `docs/audits/` that quantifies the problem.
2. Real cases that become test fixtures (A8).
3. Evidence for whether the similarity threshold and the default mode are reasonable.

### A9. Continuity check (independent safety net)

Label matching can be fooled: an identical label can hide a definitional change. So every merged concept, whether matched automatically or through an override, is also checked against the data:

- Fetch the concept at the national level (`us`) for each vintage. This is one cached call per year.
- For each adjacent pair of years, run `census.stats.z_test` with the overlapping-period adjustment, and compute the relative change.
- Flag a concept when the change is both statistically significant and implausibly large. The default threshold is relative change > 25% in one year for counts and > 10 percentage points for percentages; the audit (A7) will calibrate these.
- Flags appear in `concordance_issues` with `status: continuity_flag` and block selected variables in `fail` mode.

This turns a matching error into a visible numeric discontinuity instead of a silent one. It does not prove that two vintages measure the same thing, so primary-source review is still required for overrides.

### A8. Tests

- Fixtures: recorded real `variables.json` excerpts for at least two vintages of one table with a confirmed renumbering, and one with a confirmed redefinition, both taken from the audit.
- Property tests: a column never mixes concepts; every panel cell traces to exactly one (year, code); E/M pairs are never split across concepts.
- Regression: a panel built from fixtures with a known renumbering produces one continuous column under `strict`, and a code reuse produces two columns.

---

## 4. Workstream B (P0): R-CPI-U-RS deflator

### B1. Rationale

The Census Bureau adjusts historical income for inflation with the CPI-U-RS research series (published by BLS as R-CPI-U-RS), not with CPI-U. Using the same index keeps our constant-dollar figures consistent with Census-published comparisons. Task B5 confirms the exact ACS guidance wording and cites it in the docs and code.

### B2. Index data as sourced artifacts

- `data/price_indexes/r_cpi_u_rs.csv` and `data/price_indexes/cpi_u.csv` with columns `year, value`.
- Each has a sidecar `.json` recording the source URL, series identifier, base period, retrieval date, and SHA-256 of the raw download.
- `scripts/update_price_indexes.py` downloads from BLS, parses, and writes both files. Values are never hand-typed. This replaces the hard-coded `CPI_U_ANNUAL` dictionary in `census/inflation.py`, which was entered from memory.
- A test verifies that the vendored CSV matches its recorded checksum and that years are contiguous.

### B3. API changes

- `ResearchOptions.price_index: Literal["r-cpi-u-rs", "cpi-u"] = "r-cpi-u-rs"`.
- `adjust_inflation(value, from_year, to_year, index="r-cpi-u-rs")`. The MCP tool gains an `index` parameter and returns the index used and its source.
- Codebook `Unit` and extraction metadata name the index, for example `Dollars (constant 2022 dollars, R-CPI-U-RS)`.
- `cpi_table=` remains available for custom indexes.

### B4. Year coverage

R-CPI-U-RS annual values are published the spring after each year, before the corresponding ACS 5-year release, so coverage should not lag. If a needed year is missing, the run fails with a structured error (`PRICE_INDEX_YEAR_MISSING`) that names the update script.

### B5. Verification

- Cross-check at least two adjustment factors against a Census Bureau publication that states its CPI-U-RS factors (to be identified during implementation; for example, an Income in the United States report table).
- Confirm in Census documentation whether within-period ACS income is already adjusted with CPI-U-RS, and document the answer.

---

## 5. Workstream C (P1): agent-first interface

The GUI predates agentic workflows. The primary users are now agents that call MCP tools or run scripts. Agents benefit most from being able to plan before acting, from structured errors, and from compact results.

### C1. Dry-run planning

`plan_extraction(...)`, exposed as a library function and an MCP tool, takes the same arguments as `batch_download` and returns, without downloading data:

- the number of API calls, and how many are already cached;
- the vintages requested and their availability (from A1);
- the concordance summary and any `needs_review` items (from A4);
- the boundary and comparability advisories;
- the estimated row count and output size.

### C2. New MCP tools

| Tool | Purpose |
| :--- | :--- |
| `list_vintages(dataset)` | Available years from the API catalog |
| `plan_extraction(...)` | Dry run as described in C1 |
| `check_variable_concordance(table_code, start_year, end_year)` | Concordance report with statuses, candidate pairs, and evidence (labels, universes, similarity) |
| `describe_variable(code, year)` | Label, concept, and universe for one code in one vintage |

### C3. Structured results and errors

- Every tool returns `success`, plus `error_code` and `error` on failure. Error codes are a documented enumeration (for example `INVALID_GEO_LEVEL`, `VINTAGE_UNAVAILABLE`, `PRICE_INDEX_YEAR_MISSING`, `CONCORDANCE_REVIEW_REQUIRED`, `API_HTTP_ERROR`).
- Large data stays on disk. Tools return paths and summaries, and a row preview capped by `max_rows`.

### C4. Command-line interface

`python -m census` with subcommands `extract`, `plan`, `concordance`, `metadata prefetch`, and `cache stats|clear`.

- `--json` prints a single JSON result object to stdout. Progress lines go to stderr.
- Exit codes: `0` success, `2` invalid input, `3` concordance review required (in `fail` mode), `4` API failure.

### C5. Provenance

Extraction metadata adds the package version and git commit (when available), the metadata checksum for each vintage, the concordance and override files used, and the price index source. Two runs with the same inputs and cache produce identical outputs, apart from timestamps.

---

## 6. Workstream D (P1): validation

- **Live contract tests** behind a pytest marker (`pytest -m live`), skipped by default. They cover each geography level, each dataset, a ZCTA list containing a missing code, catalog discovery, and metadata fetches. Round 5's real bugs (duplicate `NAME`, comparison-profile code shapes, bracket counts treated as dollars) were all found only against the live API.
- **Recorded fixtures** of real responses for the offline suite, refreshed by a script rather than edited by hand.

---

## 7. Workstream E (P2): GUI maintenance mode

- No new features. Keep the existing tests passing and keep the GUI working on top of `run_batch`.
- When Workstreams A and B land, the GUI inherits the new defaults (`strict` concordance, R-CPI-U-RS) and shows `concordance_issues` and advisories in its log. It gets no new controls.
- README labels it as legacy.

---

## 8. Out of scope (candidates for Round 7)

- **Geographic crosswalks across the 2010/2020 boundary change.** This means apportioning tract-level estimates using Census relationship files (or LTDB/NHGIS weights), including MOE propagation for apportioned values. It is a large methodological project, and today's advisory is the interim measure.
- ACS 1-year data and the decennial census.
- Derived ratio and proportion MOE formulas beyond sums.

---

## 9. Implementation roadmap

```text
PHASE 6.0: Interim guard (superseded: the full concordance shipped in this round)
  - multi-year panel/parquet requests for profile, subject, and cprofile tables
    return error_code UNVERIFIED_CONCORDANCE unless allow_unverified_concordance=True
  - single-year requests unaffected; detailed tables get an advisory only
  - legacy CSV/Excel outputs (one file per year) get an advisory only

PHASE 6a: Measure and model drift
  a.1 audit_concordance.py and docs/audits report (quantifies the problem)
  a.2 vintage discovery (A1) and per-vintage metadata registry (A2)
  a.3 canonical_label rules and tests (A3)

PHASE 6b: Concordance in the pipeline
  b.1 build_concordance with statuses (A4) and override files (A5)
  b.2 concept-keyed panels, concordance_mode, concordance.csv, codebook columns (A6)
  b.3 fixture and property tests from audit cases (A8)

PHASE 6c: Price index
  c.1 update_price_indexes.py and vendored R-CPI-U-RS / CPI-U with checksums (B2)
  c.2 price_index option, MCP parameter, codebook units (B3, B4)
  c.3 cross-check against Census-published factors (B5)

PHASE 6d: Agent interface and validation
  d.1 plan_extraction, list_vintages, check_variable_concordance, describe_variable (C1, C2)
  d.2 structured error codes (C3) and the python -m census CLI (C4)
  d.3 provenance fields (C5) and live contract tests (D)
```

6.0 ships first because it closes the silent-error path in Round 5 at low cost. 6a.1 comes next because its findings may change the thresholds and defaults in 6b.

---

## 10. Acceptance criteria

1. No panel column contains values from two different concepts. This is enforced by a property test and by a runtime assertion in `build_tidy_panel`.
2. Every panel cell traces to one (year, code) through `<table>_concordance.csv`.
3. A known renumbering from the audit yields one continuous column. A known code reuse yields two columns.
4. Under the default `fail` mode, any unresolved drift item or continuity flag affecting the selected variables stops the run. Under `strict`, every such item appears in `concordance_issues`.
5. Every `merge` override in `crosswalks/` passes the five review requirements in A5, enforced by CI.
6. Constant-dollar values match a Census-published CPI-U-RS factor to within rounding.
7. Price-index data is reproducible from `update_price_indexes.py` and matches its recorded checksum.
8. An agent can go from a plain-language request to a planned, reviewed, and executed extraction through MCP or the CLI alone.

---


## 11. Decisions

| # | Question | Decision | Section |
| :--- | :--- | :--- | :--- |
| 1 | Default concordance mode | `fail`, scoped to the variables named in `variables=`; `strict` is opt-in for exploration | A6 |
| 2 | Bundled `variables.json` / `groups.json` | Keep as a vintage-stamped offline fallback; never sufficient alone for multi-year panels in `fail` mode | A2 |
| 3 | Override review | Agents (or humans) draft and review; the drafter and reviewer identities must differ. Five enforced requirements: generated evidence, census.gov source, continuity check, independent sign-off, codes exist. `split` when in doubt. Documented in the MCP server | A5, A9 |
| 4 | Interim behavior | Superseded: the full concordance shipped in this round. Single-year pulls never block, and agents may collate them, checking codes with `describe_variable` / `check_variable_concordance` against the archive | A6 |
| 5 | License | AGPL-3.0-or-later with a Contributor License Agreement (`LICENSE`, `CLA.md`, `CONTRIBUTING.md`) | 12 |
| 6 | Bare codes in `variables=` | Resolve in the latest requested year; `YEAR:CODE` pins a vintage, because a code can denote different measures in different years | A6 |

## 12. Licensing (decided: AGPL-3.0-or-later with a CLA; not legal advice)

Goal as stated: accept contributions while preventing others from taking the project and presenting it as their own.

Every common license, including permissive ones, already requires forks to keep the original copyright notice, so misattribution is a license violation under any option below. The real choice is whether forks may be closed or commercialized.

| Option | Forks can be closed or proprietary? | Commercial use | Effect on contributions |
| :--- | :--- | :--- | :--- |
| No license (current) | No one may legally use or modify the code | Not permitted | Contributions are legally unclear; discourages use |
| MIT / Apache-2.0 | Yes, with attribution | Allowed | Easiest to contribute to |
| GPL-3.0 | No, distributed forks must stay GPL | Allowed if source is shared | Common in research software |
| AGPL-3.0 | No, and it also covers software offered over a network (for example, a hosted MCP server) | Allowed if source is shared | Same as GPL, with stronger reciprocity |
| PolyForm Noncommercial | Source-available, not open source | Prohibited | Fewer outside contributors |

**Recommendation:** AGPL-3.0-or-later, plus:

- a **Contributor License Agreement** (or at minimum a DCO `Signed-off-by` requirement), so the owner keeps the right to relicense later;
- a **`CITATION.cff`** file, so academic users cite the project, which matters more for research credit than the license itself;
- a `NOTICE` or README attribution section naming the original author.

Choosing and adding the license is an owner action and is not part of the implementation tasks.

## 13. Implementation status

| Item | Status | Notes |
| :--- | :--- | :--- |
| A1 vintage discovery | Done | Archive manifest plus optional live catalog (`list_vintages refresh=True`) |
| A2 per-vintage metadata | Done | `census/metadata.py`, 114-file archive |
| A3 normalization | Done | Version 2: prefixes ("Estimate", "Percent Estimate", period), mid-label markers, dollar year, hyphens, "Subject" placeholder, order-insensitive match key |
| A4 concordance | Done | `census/concordance.py`; code reassignments explained by renumbering are non-blocking |
| A5 overrides | Done | `census/review.py`; evidence is hash-identified and regenerated during validation |
| A6 pipeline | Done | `fail` / `strict` / `code`; `variables=` with `YEAR:CODE`; `concordance.csv`; codebook concept columns |
| A7 audit | Done | `docs/audits/concordance_audit.md` (detail CSV is regenerable and gitignored) |
| A8 tests | Done | Real fixtures: DP02 2018→2019 renumbering and code reuse; B27001 2016→2017 age-band change |
| A9 continuity | Done | National Z-test plus magnitude thresholds; unverifiable merged concepts block |
| B R-CPI-U-RS | Code done; **data pending owner action** | BLS blocks anonymous downloads of the Excel file. Download it in a browser and run `scripts/update_price_indexes.py --r-cpi-u-rs-xlsx <file>`. CPI-U comes from the BLS API (computed values match published ones to ±0.001; 2025 omitted because October 2025 was not published) |
| B5 home values | Done | Advisory for pre-2022 home-value tables (ACS user note 2023-07) |
| C1–C5 agent interface | Done | `plan_extraction`, 22 MCP tools with server instructions, error codes, `python -m census`, provenance |
| D validation | Done | `tests/test_live.py` (opt-in); all live contracts pass |
| E GUI | Done | Maintenance mode; inherits defaults |
| Bug found in passing | Fixed | Legacy `compiled_unmelted_data.csv` appended rows under the first year's header when columns differed |

**Audit findings that shape follow-up work:** the subject tables' 2013 and 2017 redesigns produce about 12,000 `relabeled` items, many of them added header parts such as "total population". Variable-scoped review handles individual analyses, but resolving a table as a whole would need **table-scoped rule overrides**: reviewed, regex-like label rewrites for one table and year boundary, validated by regenerated evidence and continuity checks. These are proposed for Round 7, together with the 2010/2020 geographic crosswalk.

