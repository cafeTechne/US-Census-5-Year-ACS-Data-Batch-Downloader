# SPDX-License-Identifier: AGPL-3.0-or-later
"""National-level continuity check for concepts that span several vintages.

Label matching can be fooled (an identical label can hide a definitional
change), so every concept that spans years is also checked against data: at the
national level, adjacent years should not differ both significantly and
implausibly. A flag turns a silent matching error into a visible one. Passing
does not prove two vintages measure the same thing.
"""

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from census.concordance import (
    ISSUE_CONTINUITY_FLAG,
    ISSUE_CONTINUITY_UNAVAILABLE,
    Concordance,
    Issue,
)
from census.stats import CENSUS_SENTINELS, acs5_overlap_years, z_test

# Thresholds for an "implausible" one-year change at the national level.
# 5-year periods overlap by 4 years, so true national changes are small.
COUNT_RELATIVE_CHANGE = 0.25
PERCENT_POINT_CHANGE = 10.0


def implausibility_thresholds(year_gap: int) -> Tuple[float, float]:
    """(relative, percentage-point) thresholds for releases ``year_gap`` years apart.

    The base thresholds describe one year of change. When non-adjacent releases
    are requested (e.g. 2019 and 2023), real change accumulates: the relative
    threshold compounds, (1 + 0.25) ** gap - 1, and the percentage-point
    threshold scales linearly. Without this, stable concepts such as the top
    income bracket of B19001 (nominal dollars, +73% from 2019 to 2023) were
    flagged (validation check F9).
    """
    gap = max(1, int(year_gap))
    return (1.0 + COUNT_RELATIVE_CHANGE) ** gap - 1.0, PERCENT_POINT_CHANGE * gap


def _number(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or int(x) in CENSUS_SENTINELS:
        return None
    return x


def national_rows(
    conc: Concordance, client: Any, years: Optional[Iterable[int]] = None
) -> Dict[int, Dict[str, Any]]:
    """First (only) national row per year as {column: value}; missing years omitted."""
    rows: Dict[int, Dict[str, Any]] = {}
    for year in years or conc.years:
        if not conc.year_metadata.get(year):
            continue
        data = client.fetch(
            year=year, state_code=None, table_code=conc.table, geo_level="us", dataset=conc.dataset
        )
        if data and len(data) >= 2:
            rows[year] = dict(zip(data[0], data[1]))
    return rows


def _pair(row: Dict[str, Any], base: str) -> Optional[Tuple[float, float, bool]]:
    for est, moe, pct in (("E", "M", False), ("PE", "PM", True)):
        e, m = _number(row.get(f"{base}{est}")), _number(row.get(f"{base}{moe}"))
        if e is not None and m is not None:
            return e, m, pct
    return None


def check_concepts(
    conc: Concordance,
    concept_ids: Iterable[str],
    national: Dict[int, Dict[str, Any]],
) -> Tuple[List[Issue], Dict[str, Dict[str, Any]]]:
    """Return (issues, per-concept report) for the given concepts."""
    issues: List[Issue] = []
    report: Dict[str, Dict[str, Any]] = {}
    for cid in sorted(concept_ids):
        c = conc.concepts.get(cid)
        if c is None or len(c.codes) < 2:
            continue
        missing = [y for y in c.years if y not in national]
        flags: List[Dict[str, Any]] = []
        checked = 0
        series = [(y, _pair(national[y], c.codes[y])) for y in c.years if y in national]
        series = [(y, p) for y, p in series if p is not None]
        for (y1, p1), (y2, p2) in zip(series, series[1:]):
            e1, m1, pct = p1
            e2, m2, _ = p2
            checked += 1
            result = z_test(e1, m1, e2, m2, 0.90, overlap_years=acs5_overlap_years(y1, y2))
            rel_limit, pp_limit = implausibility_thresholds(y2 - y1)
            if pct:
                change = abs(e2 - e1)
                implausible = change > pp_limit
            else:
                change = abs(e2 - e1) / abs(e1) if e1 else math.inf
                implausible = change > rel_limit
            if result["significant"] and implausible:
                flags.append(
                    {
                        "from": {"year": y1, "code": c.codes[y1], "estimate": e1, "moe": m1},
                        "to": {"year": y2, "code": c.codes[y2], "estimate": e2, "moe": m2},
                        "change": round(change, 4),
                        "change_type": "percentage_points" if pct else "relative",
                        "z_statistic": round(result["z_statistic"], 3),
                        "threshold": round(pp_limit if pct else rel_limit, 4),
                    }
                )
        exemption = conc.continuity_exempt.get(cid)
        passed = not flags and not missing and checked > 0
        report[cid] = {
            "checked_pairs": checked,
            "flags": flags,
            "missing_years": missing,
            "passed": passed,
            "exemption": exemption,
        }
        if exemption:
            continue  # reviewed override documented why the flag is acceptable
        if flags:
            transitions = ", ".join(
                "{}->{}".format(f["from"]["year"], f["to"]["year"]) for f in flags
            )
            issues.append(
                Issue(
                    ISSUE_CONTINUITY_FLAG,
                    [cid],
                    f"National values for {c.codes[max(c.codes)]} jump implausibly between "
                    f"{transitions}; the codes may not measure the same thing.",
                    {"flags": flags},
                )
            )
        elif missing:
            issues.append(
                Issue(
                    ISSUE_CONTINUITY_UNAVAILABLE,
                    [cid],
                    f"National data unavailable for {missing}; continuity of "
                    f"{c.codes[max(c.codes)]} could not be verified.",
                    {"missing_years": missing},
                )
            )
        elif checked == 0 and _is_merged(c):
            # A renumbered or overridden concept must be verified; a stable code
            # whose national values are suppressed is only reported.
            issues.append(
                Issue(
                    ISSUE_CONTINUITY_UNAVAILABLE,
                    [cid],
                    f"No usable national estimate/MOE pairs for {c.codes[max(c.codes)]}; "
                    "continuity of this merged concept could not be verified.",
                    {"codes_by_year": {str(y): c.codes[y] for y in c.years}},
                )
            )
    return issues, report


def _is_merged(concept: Any) -> bool:
    return len(set(concept.codes.values())) > 1 or "overridden" in concept.statuses


def continuity_check(
    conc: Concordance, concept_ids: Iterable[str], client: Any
) -> Tuple[List[Issue], Dict[str, Dict[str, Any]]]:
    """Fetch national rows (one cached call per year) and check the concepts."""
    ids = [cid for cid in concept_ids if cid in conc.concepts and len(conc.concepts[cid].codes) > 1]
    if not ids:
        return [], {}
    years = sorted({y for cid in ids for y in conc.concepts[cid].years})
    return check_concepts(conc, ids, national_rows(conc, client, years))
