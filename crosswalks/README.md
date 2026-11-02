# Concordance overrides

Reviewed decisions that the automatic concordance cannot make. One CSV per table:
`crosswalks/<dataset>/<TABLE>.csv` (for example `crosswalks/profile/DP02.csv`), with this header:

```
year,code,concept_id,decision,rationale,source_url,evidence_id,continuity_exception,drafted_by,reviewed_by,reviewed_on
```

- `decision`: `merge`, `split`, or `exclude`.
- `evidence/<evidence_id>.json`: machine-generated evidence, never edited by hand (its hash is its ID).

Create rows only through `draft_crosswalk_override` and `record_crosswalk_review` (MCP), or `census.review.draft_override` and `record_override`. The rules are in [docs/agent_protocol.md](../docs/agent_protocol.md). Check every file with `python -m census validate-crosswalks`.

## Table-scoped rules

`crosswalks/<dataset>/<TABLE>_rules.csv` holds reviewed regex rewrites that link many relabeled lines across one adjacent year boundary at once (for example, a header inserted into every label by a table redesign). Header:

```
table,from_year,to_year,pattern,replacement,rationale,source_url,evidence_id,drafted_by,reviewed_by,reviewed_on
```

Create rows only through `census.review.draft_rule_override` and `record_rule_review`. A rule that changes a number in any label, maps two earlier codes to one later line, or matches nothing is rejected. Merged concepts are still continuity-checked. See [docs/rule_overrides.md](../docs/rule_overrides.md).
