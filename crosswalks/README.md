# Concordance overrides

Reviewed decisions that the automatic concordance cannot make. One CSV per table:
`crosswalks/<dataset>/<TABLE>.csv` (for example `crosswalks/profile/DP02.csv`), with this header:

```
year,code,concept_id,decision,rationale,source_url,evidence_id,continuity_exception,drafted_by,reviewed_by,reviewed_on
```

- `decision`: `merge`, `split`, or `exclude`.
- `evidence/<evidence_id>.json`: machine-generated evidence, never edited by hand (its hash is its ID).

Create rows only through `draft_crosswalk_override` and `record_crosswalk_review` (MCP), or `census.review.draft_override` and `record_override`. The rules are in [docs/agent_protocol.md](../docs/agent_protocol.md). Check every file with `python -m census validate-crosswalks`.
