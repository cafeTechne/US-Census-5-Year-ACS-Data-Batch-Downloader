# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draft, review, and record concordance overrides.

Workflow (docs/agent_protocol.md):

1. A drafter (human or agent) calls ``draft_override``: evidence is generated
   from archived metadata, a continuity check runs on national data, and the
   evidence file is saved under crosswalks/evidence/. The draft row has an
   empty ``reviewed_by``.
2. A different reviewer (human or agent, with its own identity) independently
   inspects the evidence and the Census source, then calls ``record_override``
   with the row and its own identity. The row is validated against every review
   requirement before it is appended to crosswalks/<dataset>/<table>.csv.

Table-scoped rules (one regex rewrite covering many lines at one year boundary)
use the same two-identity process: ``draft_rule_override``, then
``record_rule_review``, written to crosswalks/<dataset>/<table>_rules.csv. See
docs/rule_overrides.md.
"""

import csv
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from census.concordance import (
    CROSSWALK_COLUMNS,
    DECISIONS,
    RULE_COLUMNS,
    STATUS_OVERRIDDEN,
    Concept,
    Concordance,
    boundary_is_adjacent,
    build_evidence,
    build_rule_evidence,
    crosswalk_path,
    evidence_id_for,
    load_rules,
    rule_cross_conflicts,
    rule_guardrail_errors,
    rules_path,
    save_evidence,
    validate_crosswalk_row,
    validate_rule_row,
)
from census.continuity import check_concepts, national_rows
from census.metadata import table_variables
from census.stats import parse_variable_code


def _base(code: str) -> str:
    parsed = parse_variable_code(code.strip())
    return parsed[0] if parsed else code.strip()


def continuity_for_entries(
    table: str, dataset: str, entries: Sequence[Tuple[int, str]], client: Any
) -> Dict[str, Any]:
    """Continuity check for a hypothetical merge of the given (year, code) entries."""
    years = sorted({int(y) for y, _ in entries})
    conc = Concordance(table, dataset, years)
    for year in years:
        conc.year_metadata[year] = table_variables(table, dataset, year, client)[0]
    concept = Concept("candidate", table.upper(), dataset, "value", "")
    for year, code in entries:
        concept.codes[int(year)] = _base(code)
    conc.concepts["candidate"] = concept
    national = national_rows(conc, client, years)
    _, report = check_concepts(conc, ["candidate"], national)
    result = report.get("candidate", {"checked_pairs": 0, "flags": [], "missing_years": years, "passed": False})
    result.pop("exemption", None)
    return result


def draft_override(
    table: str,
    dataset: str,
    entries: Sequence[Tuple[int, str]],
    decision: str,
    drafted_by: str,
    rationale: str,
    source_url: str = "",
    target_concept_id: str = "",
    client: Any = None,
    run_continuity: bool = True,
) -> Dict[str, Any]:
    """Generate and save evidence, and return draft crosswalk rows (unreviewed).

    ``entries`` are the (year, code) cells the decision covers. For a merge,
    list every code that should end up in one concept; one row is drafted per
    entry whose year differs from the target concept's anchor year.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    entries = sorted((int(y), _base(c)) for y, c in entries)
    continuity = (
        continuity_for_entries(table, dataset, entries, client)
        if run_continuity and client is not None and decision == "merge"
        else None
    )
    evidence = build_evidence(table, dataset, entries, client, continuity)
    evidence_id = save_evidence(evidence)
    rows: List[Dict[str, str]] = []
    for year, code in entries:
        rows.append(
            {
                "year": str(year),
                "code": code,
                "concept_id": target_concept_id,
                "decision": decision,
                "rationale": rationale,
                "source_url": source_url,
                "evidence_id": evidence_id,
                "continuity_exception": "",
                "drafted_by": drafted_by,
                "reviewed_by": "",
                "reviewed_on": "",
            }
        )
    return {
        "evidence_id": evidence_id,
        "evidence": evidence,
        "draft_rows": rows,
        "continuity_passed": bool(continuity and continuity.get("passed")),
        "next_step": (
            "A different reviewer must inspect the evidence and the cited Census source, "
            "then call record_override with reviewed_by set to their own identity."
        ),
    }


def record_override(
    row: Dict[str, str],
    reviewed_by: str,
    dataset: str,
    table: str,
    reviewed_on: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Validate a reviewed row and append it to the table's crosswalk file."""
    final = {c: str(row.get(c, "") or "") for c in CROSSWALK_COLUMNS}
    final["reviewed_by"] = reviewed_by
    final["reviewed_on"] = reviewed_on or date.today().isoformat()
    errors = validate_crosswalk_row(final, dataset, table, client)
    if errors:
        return {"success": False, "error_code": "INVALID_INPUT", "error": "; ".join(errors), "errors": errors}
    path = crosswalk_path(dataset, table)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CROSSWALK_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(final)
    return {"success": True, "crosswalk_path": path, "row": final}


# ---------------------------------------------------------------------------
# Table-scoped rule overrides
# ---------------------------------------------------------------------------


def continuity_for_rule(evidence: Dict[str, Any], client: Any) -> Dict[str, Any]:
    """National continuity check for every merge a rule would create.

    Informational at draft time: extraction re-runs the check on every selected
    merged concept, and a rule never grants a continuity exemption.
    """
    table, dataset = evidence["table"], evidence["dataset"]
    y1, y2 = int(evidence["from_year"]), int(evidence["to_year"])
    matches = evidence.get("matches", [])
    conc = Concordance(table, dataset, [y1, y2])
    for year in (y1, y2):
        conc.year_metadata[year] = table_variables(table, dataset, year, client)[0]
    for i, m in enumerate(matches):
        cid = f"rule_match_{i}"
        concept = Concept(cid, table, dataset, m.get("role", "value"), m.get("label", ""))
        concept.codes = {y1: m["from_code"], y2: m["to_code"]}
        concept.statuses = [STATUS_OVERRIDDEN]
        conc.concepts[cid] = concept
    national = national_rows(conc, client, [y1, y2])
    _, report = check_concepts(conc, list(conc.concepts), national)
    flagged: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []
    for cid, r in sorted(report.items()):
        m = matches[int(cid.rsplit("_", 1)[1])]
        if r["flags"]:
            flagged.append({"from_code": m["from_code"], "to_code": m["to_code"], "flags": r["flags"]})
        elif not r["passed"]:
            unverified.append(
                {"from_code": m["from_code"], "to_code": m["to_code"], "missing_years": r["missing_years"]}
            )
    return {
        "checked": len(report),
        "passed": sum(1 for r in report.values() if r["passed"]),
        "flagged": flagged,
        "unverified": unverified,
    }


def draft_rule_override(
    table: str,
    dataset: str,
    from_year: int,
    to_year: int,
    pattern: str,
    replacement: str,
    drafted_by: str,
    rationale: str,
    source_url: str = "",
    client: Any = None,
    run_continuity: bool = True,
) -> Dict[str, Any]:
    """Generate rule evidence; return an unreviewed draft rule row and a preview.

    The rule rewrites the canonical labels (``canonical_label``) of the
    ``from_year`` lines of one table with ``re.sub(pattern, replacement,
    label)``. A rewritten line whose match key equals a ``to_year`` line's key
    is merged with it. The preview counts labels rewritten, new matches, and
    rewritten lines that still match nothing, and ``guardrail_errors`` lists
    anything that would make review fail. Evidence is saved under
    crosswalks/evidence/ only when the guardrails pass. With a client, the
    national continuity check is previewed for every new match.
    """
    table = table.upper()
    from_year, to_year = int(from_year), int(to_year)
    try:
        re.compile(pattern).sub(replacement, "")
    except (re.error, IndexError) as exc:
        msg = f"pattern/replacement is not a valid regular expression: {exc}"
        return {"success": False, "error_code": "INVALID_INPUT", "error": msg}
    problems: List[str] = []
    if not boundary_is_adjacent(dataset, from_year, to_year):
        problems.append(f"{from_year} -> {to_year} is not one adjacent vintage boundary")
    evidence = build_rule_evidence(table, dataset, from_year, to_year, pattern, replacement, client)
    problems.extend(rule_guardrail_errors(evidence))
    saved = not problems
    evidence_id = save_evidence(evidence) if saved else evidence_id_for(evidence)
    continuity = (
        continuity_for_rule(evidence, client)
        if run_continuity and client is not None and evidence["matches"]
        else None
    )
    row = {
        "table": table,
        "from_year": str(from_year),
        "to_year": str(to_year),
        "pattern": pattern,
        "replacement": replacement,
        "rationale": rationale,
        "source_url": source_url,
        "evidence_id": evidence_id if saved else "",
        "drafted_by": drafted_by,
        "reviewed_by": "",
        "reviewed_on": "",
    }
    return {
        "success": True,
        "evidence_id": evidence_id,
        "evidence_saved": saved,
        "evidence": evidence,
        "draft_row": row,
        "preview": {
            "labels_rewritten": len(evidence["rewrites"]),
            "new_matches": len(evidence["matches"]),
            "still_unmatched": len(evidence["unmatched"]),
            "conflicts": len(evidence["conflicts"]),
            "numeric_changes": len(evidence["numeric_changes"]),
            "sample_rewrites": evidence["rewrites"][:5],
            "sample_matches": evidence["matches"][:5],
        },
        "guardrail_errors": problems,
        "continuity": continuity,
        "next_step": (
            "Revise the rule; review would reject it: " + "; ".join(problems)
            if problems
            else "A different reviewer must inspect every rewrite and match in the evidence "
            "file and the cited Census source, then call record_rule_review with reviewed_by "
            "set to their own identity. Merged concepts are still continuity-checked at extraction."
        ),
    }


def record_rule_review(
    row: Dict[str, str],
    reviewed_by: str,
    dataset: str,
    table: Optional[str] = None,
    reviewed_on: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Validate a reviewed rule row and append it to crosswalks/<dataset>/<table>_rules.csv."""
    table = (table or str(row.get("table", "") or "")).strip().upper()
    if not table:
        return {"success": False, "error_code": "INVALID_INPUT", "error": "table is required", "errors": ["table is required"]}
    final = {c: str(row.get(c, "") or "") for c in RULE_COLUMNS}
    final["table"] = final["table"].strip().upper() or table
    final["reviewed_by"] = reviewed_by
    final["reviewed_on"] = reviewed_on or date.today().isoformat()
    errors = validate_rule_row(final, dataset, table, client)
    key = (final["from_year"].strip(), final["to_year"].strip(), final["pattern"], final["replacement"])
    existing_rows = load_rules(dataset, table)
    for existing in existing_rows:
        other = (
            str(existing.get("from_year", "")).strip(),
            str(existing.get("to_year", "")).strip(),
            existing.get("pattern", "") or "",
            existing.get("replacement", "") or "",
        )
        if other == key:
            errors.append("an identical rule is already recorded")
            break
    if not errors:
        # Rules on one boundary must not claim the same earlier or later line.
        checked = []
        for idx, existing in enumerate(existing_rows, start=2):
            try:
                ev = build_rule_evidence(
                    table, dataset, int(existing["from_year"]), int(existing["to_year"]),
                    existing.get("pattern", ""), existing.get("replacement", "") or "", client,
                )
            except (KeyError, ValueError, re.error, IndexError):
                continue
            checked.append((idx, existing, ev))
        new_line = len(existing_rows) + 2
        checked.append((new_line, final, build_rule_evidence(
            table, dataset, int(final["from_year"]), int(final["to_year"]),
            final["pattern"], final["replacement"], client,
        )))
        errors.extend(rule_cross_conflicts(checked).get(new_line, []))
    if errors:
        return {"success": False, "error_code": "INVALID_INPUT", "error": "; ".join(errors), "errors": errors}
    path = rules_path(dataset, table)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RULE_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(final)
    return {"success": True, "rules_path": path, "row": final}
