# Metadata integrity policy (Round 18)

Bundled `metadata/<dataset>/<year>/{variables,groups}.json.gz` files are checked
when first consumed, including through the legacy table loaders. SHA-256 covers
the **decompressed JSON bytes**, matching `metadata/manifest.json`; changing gzip
headers or compression alone does not change identity. Verification establishes
agreement with the shipped manifest, not authentication of that manifest or
scientific correctness of its contents.

A present bundled file with a missing/mismatched checksum, invalid gzip or invalid
JSON raises `MetadataIntegrityError` (a `BundledDataIntegrityError`). Application
boundaries return `success: false`, `error_code: BUNDLED_DATA_INTEGRITY` and
metadata-specific repair guidance; the CLI exits 4. Planning checks all present
variable/group archives before metadata fallback or extraction requests. Manifest
runs check these inputs before creating current-run output. They do not produce a
successful bundle from corrupt metadata. An **absent** archive retains the existing
missing/API fallback behavior; an API failure remains a transport/data failure.

`CENSUS_METADATA_DIR` selects caller-supplied metadata. Its files record actual
consumed-byte hashes and `integrity: caller_supplied_unverified`, whether its
optional manifest matches or not. A matching custom manifest is not promoted to
official verification. A custom directory without a manifest works when a vintage
is explicit; automatic vintage discovery still needs the manifest. Present custom
files must still be readable, valid JSON metadata. A malformed supplied manifest
is an explicit failure. Selecting the resolved bundled directory through the
environment variable retains bundled verification.

Plans expose `metadata_vintages`; extraction metadata stores it under
`concordance.metadata_vintages`. The existing `source`, `sha256` and `url` keys
remain; `sha256` now describes actual consumed variable bytes. Added fields are
`expected_sha256`, `integrity`, and `groups` (its own archive digest/status, or
null when no group archive was used). API/missing variables do not borrow a
manifest hash. Replay compares new integrity/group fields when the original
record contains them; older extraction records remain readable without claiming
those older hashes were runtime-verified.

Caches use resolved archive roots. Successful reads are bounded process snapshots,
not hot reload: after repairs or intentional edits, call
`census.metadata.clear_cache()` or restart the CLI/server/GUI. That reset also clears
legacy table lookup caches and API metadata memoization. Failed archive reads are
not cached. Switching roots cannot reuse another root's manifest or variables;
API memoization also distinguishes the client base URL. Do not change environment
configuration concurrently with an active extraction or reuse a prepared plan
after changing its input configuration.

Restore damaged archives and their manifest together from a trusted checkout or
reinstall; repair custom files at their source. Do not silently replace corrupt
bundled metadata with API metadata or regenerate research inputs during analysis.
Regression reproductions use temporary synthetic archives only:

```sh
python -m pytest -q tests/test_metadata_integrity.py tests/test_reference_workflow.py
```

These tests exercise the consuming library, CLI, MCP and GUI worker, assert zero
API calls on corruption, and check that current-run artifacts are absent. The
reference workflow's frozen responses and replicates remain unchanged.
