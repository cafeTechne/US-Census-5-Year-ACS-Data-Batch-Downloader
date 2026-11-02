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
"""

import csv
import os
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from census.concordance import (
    CROSSWALK_COLUMNS,
    DECISIONS,
    Concept,
    Concordance,
    build_evidence,
    crosswalk_path,
    save_evidence,
    validate_crosswalk_row,
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
