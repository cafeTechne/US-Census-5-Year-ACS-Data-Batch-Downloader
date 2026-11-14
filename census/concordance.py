# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-vintage variable concordance.

ACS releases occasionally renumber lines (same measure, new code), reuse codes
(same code, new measure), reword labels, or change table universes. Panels are
therefore built from *concepts*, not codes: every (year, code) is mapped to a
concept through an explicit, inspectable concordance.

Matching rules (see docs/agent_protocol.md):

* Only exact canonical-label matches are merged automatically.
* Anything weaker is reported as an issue with machine-generated evidence and
  must be resolved by a reviewed override in crosswalks/<dataset>/<table>.csv.
"""

import csv
import difflib
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from census.geo import get_resource_path
from census.metadata import SOURCE_MISSING, archived_vintages, table_universe, table_variables
from census.stats import PRIMARY_SUFFIXES, parse_variable_code

NORMALIZATION_VERSION = 2
RELABEL_SIMILARITY = 0.80  # same code, labels at least this similar -> needs review

# Issue kinds. BLOCKING_KINDS stop a run in "fail" mode when they touch a
# selected concept.
ISSUE_RELABELED = "relabeled"
ISSUE_UNIVERSE_CHANGED = "universe_changed"
ISSUE_AMBIGUOUS = "ambiguous"
ISSUE_CONTINUITY_FLAG = "continuity_flag"
ISSUE_CONTINUITY_UNAVAILABLE = "continuity_unavailable"
ISSUE_METADATA_UNAVAILABLE = "metadata_unavailable"
ISSUE_INVALID_OVERRIDE = "invalid_override"
ISSUE_REDEFINED = "redefined"
ISSUE_POSSIBLE_CONTINUATION = "possible_continuation"
ISSUE_TABLE_ABSENT = "table_absent"

BLOCKING_KINDS = {
    ISSUE_RELABELED,
    ISSUE_UNIVERSE_CHANGED,
    ISSUE_AMBIGUOUS,
    ISSUE_CONTINUITY_FLAG,
    ISSUE_CONTINUITY_UNAVAILABLE,
    ISSUE_METADATA_UNAVAILABLE,
    ISSUE_INVALID_OVERRIDE,
}

STATUS_STABLE = "stable"
STATUS_RENUMBERED = "renumbered"
STATUS_ADDED = "added"
STATUS_DISCONTINUED = "discontinued"
STATUS_OVERRIDDEN = "overridden"

CROSSWALK_COLUMNS = [
    "year",
    "code",
    "concept_id",
    "decision",
    "rationale",
    "source_url",
    "evidence_id",
    "continuity_exception",
    "drafted_by",
    "reviewed_by",
    "reviewed_on",
]
DECISIONS = ("merge", "split", "exclude")
IDENTITY_RE = re.compile(r"^(human|agent):[^\s:][^\s]*$")
PRIMARY_SOURCE_HOSTS = ("census.gov",)

# ---------------------------------------------------------------------------
# Label normalization
# ---------------------------------------------------------------------------

# Measurement/period markers dropped when they lead the label.
_PREFIX_RE = re.compile(
    r"^(estimate|percent|number|margin of error|percent estimate|percent margin of error|"
    r"\d{4}-\d{4} estimates)$",
    re.IGNORECASE,
)
# Markers dropped wherever they appear (pre-2017 subject tables put "Estimate"
# mid-label: "Veterans!!Estimate!!...").
_ANYWHERE_RE = re.compile(r"^(estimate|margin of error|subject)$", re.IGNORECASE)
_WORD_HYPHEN_RE = re.compile(r"(?<=[a-z])-(?=[a-z])", re.IGNORECASE)
_DOLLAR_YEAR_RE = re.compile(r"\((?:in )?\d{4} inflation[- ]adjusted dollars\)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def canonical_label(label: str) -> str:
    """Normalize a Census label so cosmetic vintage differences compare equal.

    Rules (NORMALIZATION_VERSION), each covered by tests:
      1. drop leading measurement/period parts (Estimate, Percent, Number,
         Margin of Error, '2013-2017 Estimates'), and 'Estimate' / 'Margin of
         Error' / 'Subject' placeholder parts anywhere
      2. replace '(in YYYY inflation-adjusted dollars)' with a year-free form
      3. per part: trim, drop trailing ':' and '--', hyphens between words
         become spaces, collapse whitespace, lowercase
      4. drop empty parts; keep the full hierarchy path

    Use ``match_key`` (order-insensitive) for matching, this for display.
    """
    parts = [p for p in str(label).split("!!")]
    while parts and _PREFIX_RE.match(parts[0].strip()):
        parts = parts[1:]
    parts = [p for p in parts if not _ANYWHERE_RE.match(p.strip())]
    out: List[str] = []
    for part in parts:
        p = _DOLLAR_YEAR_RE.sub("(inflation-adjusted dollars)", part)
        p = _SPACE_RE.sub(" ", p).strip()
        while p.endswith(":") or p.endswith("--"):
            p = p.rstrip(":").rstrip("-").strip()
        p = _WORD_HYPHEN_RE.sub(" ", p)
        p = _SPACE_RE.sub(" ", p).lower()
        if p:
            out.append(p)
    return "!!".join(out)


def match_key(canonical: str) -> str:
    """Order-insensitive key: the multiset of label parts.

    Census sometimes moves a column header from first to second position
    between releases. Two different variables in the same vintage that share a
    key are reported as ``ambiguous``, so ordering is never what separates them
    silently.
    """
    return "!!".join(sorted(canonical.split("!!")))


def label_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Line identity
# ---------------------------------------------------------------------------

_CP_BASE_RE = re.compile(r"^(?P<table>CP\d+[A-Z]*)_(?P<period>\d{4})(?:to(?P<period2>\d{4}))?_(?P<line>\d+)$")


def line_role(base: str, dataset: str, year: int) -> str:
    """Role of a variable base within its release.

    Comparison profiles carry several periods per release; codes embed the
    period, so the role (current / comparison / significance) is part of the
    concept identity. Other datasets have a single role.
    """
    if dataset != "cprofile":
        return "value"
    m = _CP_BASE_RE.match(base)
    if not m:
        return "value"
    if m.group("period2"):
        return "significance"
    return "current" if int(m.group("period")) == int(year) else "comparison"


def _concept_id(dataset: str, table: str, role: str, canonical: str) -> str:
    raw = f"{dataset}|{table}|{role}|{canonical}"
    return "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Concept:
    concept_id: str
    table: str
    dataset: str
    role: str
    canonical: str
    label: str = ""  # label from the latest year present
    concept: str = ""
    codes: Dict[int, str] = field(default_factory=dict)  # year -> variable base
    universes: Dict[int, str] = field(default_factory=dict)
    statuses: List[str] = field(default_factory=list)
    decided_by: Dict[int, str] = field(default_factory=dict)

    @property
    def years(self) -> List[int]:
        return sorted(self.codes)


@dataclass
class Issue:
    kind: str
    concept_ids: List[str]
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_KINDS

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["blocking"] = self.blocking
        return d


class Concordance:
    """Mapping of (year, variable base) -> concept for one table."""

    def __init__(self, table: str, dataset: str, years: Sequence[int]) -> None:
        self.table = table.upper()
        self.dataset = dataset
        self.years = sorted(set(int(y) for y in years))
        self.concepts: Dict[str, Concept] = {}
        self.issues: List[Issue] = []
        self.sources: Dict[int, str] = {}
        self.year_metadata: Dict[int, Dict[str, Dict[str, str]]] = {}
        self.overrides_applied: List[Dict[str, str]] = []
        self.excluded: Set[Tuple[int, str]] = set()
        # concept_id -> reviewer's written reason to accept a continuity flag
        self.continuity_exempt: Dict[str, str] = {}
        # reviewed table-scoped rules applied (see apply_rules / _rules.csv)
        self.rules_applied: List[Dict[str, Any]] = []

    # -- lookups ----------------------------------------------------------

    def code_map(self, year: int) -> Dict[str, str]:
        """variable base -> concept_id for one year."""
        return {
            c.codes[year]: cid for cid, c in self.concepts.items() if year in c.codes
        }

    def representative_bases(self) -> Dict[str, str]:
        """concept_id -> base used as the panel column stem.

        The latest year's code is used. When one code would represent several
        concepts (a reused code), each gets a ``__<first>_<last>`` segment so
        columns never mix concepts.
        """
        latest = {cid: c.codes[max(c.codes)] for cid, c in self.concepts.items() if c.codes}
        counts: Dict[str, int] = {}
        for base in latest.values():
            counts[base] = counts.get(base, 0) + 1
        out: Dict[str, str] = {}
        for cid, base in latest.items():
            if counts[base] > 1:
                years = self.concepts[cid].years
                base = f"{base}__{years[0]}_{years[-1]}"
            out[cid] = base
        return out

    def rename_map(self, year: int) -> Dict[str, str]:
        """original base in ``year`` -> representative base."""
        reps = self.representative_bases()
        return {base: reps[cid] for base, cid in self.code_map(year).items()}

    def representative_metadata(self) -> Dict[str, Dict[str, str]]:
        """Metadata keyed by representative codes (E/PE/SS) for labels and codebooks."""
        reps = self.representative_bases()
        out: Dict[str, Dict[str, str]] = {}
        for cid, c in self.concepts.items():
            year = max(c.codes)
            base = c.codes[year]
            for suffix in PRIMARY_SUFFIXES:
                meta = self.year_metadata.get(year, {}).get(f"{base}{suffix}")
                if meta:
                    out[f"{reps[cid]}{suffix}"] = dict(meta)
        return out

    def resolve_selection(
        self, variables: Optional[Iterable[str]]
    ) -> Tuple[Set[str], List[str]]:
        """Map user-supplied variables to concept IDs. Returns (concept_ids, unknown).

        Accepted forms:
          * concept ID (``c_1a2b3c...``)
          * ``YEAR:CODE`` (e.g. ``2019:DP02_0016``): the concept that code
            denotes in that vintage
          * bare code, with or without suffix (``DP02_0016``, ``DP02_0016PE``):
            resolved in the *latest* requested year where the code exists,
            because codes can denote different measures in different years
        """
        found, unknown, _ = self.explain_selection(variables)
        return found, unknown

    def explain_selection(
        self, variables: Optional[Iterable[str]]
    ) -> Tuple[Set[str], List[str], List[Dict[str, Any]]]:
        """Like resolve_selection, plus how each input was resolved."""
        if not variables:
            return set(self.concepts), [], []
        found: Set[str] = set()
        unknown: List[str] = []
        resolution: List[Dict[str, Any]] = []
        for raw in variables:
            item = str(raw).strip()
            if item in self.concepts:
                found.add(item)
                resolution.append({"input": item, "concept_id": item, "resolved_in": None})
                continue
            year: Optional[int] = None
            code = item
            if ":" in item:
                head, code = item.split(":", 1)
                try:
                    year = int(head)
                except ValueError:
                    unknown.append(item)
                    continue
            parsed = parse_variable_code(code.strip())
            base = parsed[0] if parsed else code.strip()
            candidates = [year] if year is not None else sorted(self.years, reverse=True)
            hit = None
            for y in candidates:
                cid = self.code_map(y).get(base)
                if cid:
                    hit = (y, cid)
                    break
            if hit is None:
                unknown.append(item)
                continue
            found.add(hit[1])
            c = self.concepts[hit[1]]
            resolution.append(
                {
                    "input": item,
                    "concept_id": hit[1],
                    "resolved_in": hit[0],
                    "label": c.canonical,
                    "codes_by_year": {str(y): c.codes[y] for y in c.years},
                }
            )
        return found, unknown, resolution

    def issues_for(self, concept_ids: Iterable[str]) -> List[Issue]:
        ids = set(concept_ids)
        return [i for i in self.issues if not i.concept_ids or ids & set(i.concept_ids)]

    def blocking_issues(self, concept_ids: Iterable[str]) -> List[Issue]:
        return [i for i in self.issues_for(concept_ids) if i.blocking]

    def merged_concepts(self) -> Set[str]:
        return {
            cid
            for cid, c in self.concepts.items()
            if STATUS_RENUMBERED in c.statuses or STATUS_OVERRIDDEN in c.statuses
        }

    # -- export -----------------------------------------------------------

    def to_rows(
        self, slugs: Optional[Dict[str, str]] = None, selected: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        reps = self.representative_bases()
        rows: List[Dict[str, Any]] = []
        for cid in sorted(self.concepts, key=lambda k: reps.get(k, k)):
            c = self.concepts[cid]
            for year in c.years:
                base = c.codes[year]
                meta = self.year_metadata.get(year, {})
                label = _primary_meta(meta, base).get("label", "")
                rows.append(
                    {
                        "concept_id": cid,
                        "panel_base": reps.get(cid, ""),
                        "slug": (slugs or {}).get(cid, ""),
                        "role": c.role,
                        "year": year,
                        "code": base,
                        "label": label,
                        "status": ";".join(c.statuses),
                        "decided_by": c.decided_by.get(year, "auto"),
                        "selected": selected is None or cid in selected,
                    }
                )
        return rows

    def write_csv(
        self, path: str, slugs: Optional[Dict[str, str]] = None, selected: Optional[Set[str]] = None
    ) -> str:
        rows = self.to_rows(slugs, selected)
        fields = ["concept_id", "panel_base", "slug", "role", "year", "code", "label", "status", "decided_by", "selected"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for c in self.concepts.values():
            for s in c.statuses:
                counts[s] = counts.get(s, 0) + 1
        kinds: Dict[str, int] = {}
        for i in self.issues:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        return {
            "table": self.table,
            "dataset": self.dataset,
            "years": self.years,
            "concepts": len(self.concepts),
            "status_counts": counts,
            "issue_counts": kinds,
            "blocking_issues": sum(1 for i in self.issues if i.blocking),
            "metadata_sources": {str(y): s for y, s in self.sources.items()},
            "normalization_version": NORMALIZATION_VERSION,
        }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _primary_meta(variables: Mapping[str, Any], base: str) -> Dict[str, str]:
    """Metadata of a base's primary code (E, PE, SS, or a 2020 Census count N)."""
    for suffix in PRIMARY_SUFFIXES:
        meta = variables.get(f"{base}{suffix}")
        if meta:
            return meta
    return {}


def _units_for_year(
    variables: Dict[str, Dict[str, str]], dataset: str, year: int
) -> Dict[str, Tuple[str, str, Dict[str, str]]]:
    """variable base -> (role, canonical label, metadata of its primary code)."""
    by_base: Dict[str, Dict[str, Dict[str, str]]] = {}
    for code, meta in variables.items():
        parsed = parse_variable_code(code)
        if not parsed:
            continue
        base, suffix = parsed
        by_base.setdefault(base, {})[suffix] = meta
    units: Dict[str, Tuple[str, str, Dict[str, str]]] = {}
    for base, metas in by_base.items():
        primary = next((metas[s] for s in PRIMARY_SUFFIXES if s in metas), None)
        if primary is None:
            continue
        units[base] = (line_role(base, dataset, year), canonical_label(primary["label"]), primary)
    return units


def build_concordance(
    table_code: str,
    dataset: str,
    years: Sequence[int],
    client: Any = None,
    apply_rules: bool = True,
) -> Concordance:
    """Build the concordance for a table across vintages from per-year metadata.

    With ``apply_rules`` (default), reviewed table-scoped rules in
    crosswalks/<dataset>/<table>_rules.csv link concepts across their year
    boundary before statuses and issues are derived; invalid rules are reported
    as ``invalid_override`` issues and never applied.
    """
    from census.metadata import preflight_archives

    preflight_archives(dataset, years)
    conc = Concordance(table_code, dataset, years)
    universes: Dict[int, str] = {}

    for year in conc.years:
        variables, source = table_variables(conc.table, dataset, year, client)
        conc.sources[year] = source
        conc.year_metadata[year] = variables
        universes[year] = table_universe(conc.table, dataset, year, client)
        if source != SOURCE_MISSING and not variables:
            conc.issues.append(
                Issue(
                    ISSUE_TABLE_ABSENT,
                    [],
                    f"{conc.table} is not published in the {year} {dataset} release.",
                    {"year": year},
                )
            )
            continue
        if source == SOURCE_MISSING:
            conc.issues.append(
                Issue(
                    ISSUE_METADATA_UNAVAILABLE,
                    [],
                    f"No variable metadata for {conc.table} ({dataset}) in {year}; "
                    "codes in that year cannot be verified against other years.",
                    {"year": year, "source": source},
                )
            )
            continue

        units = _units_for_year(variables, dataset, year)
        # Detect match-key collisions within a single vintage.
        seen: Dict[Tuple[str, str], List[str]] = {}
        for base, (role, canon, _) in units.items():
            seen.setdefault((role, match_key(canon)), []).append(base)

        for base, (role, canon, meta) in sorted(units.items()):
            mkey = match_key(canon)
            dupes = seen[(role, mkey)]
            key = mkey if len(dupes) == 1 else f"{mkey}##{base}"
            cid = _concept_id(dataset, conc.table, role, key)
            concept = conc.concepts.get(cid)
            if concept is None:
                concept = Concept(cid, conc.table, dataset, role, canon)
                conc.concepts[cid] = concept
            concept.canonical = canon  # display form from the latest year
            concept.codes[year] = base
            concept.label = meta.get("label", "")
            concept.concept = meta.get("concept", "")
            if universes[year]:
                concept.universes[year] = universes[year]

        for (role, mkey), bases in seen.items():
            if len(bases) > 1:
                canon = units[bases[0]][1]
                ids = [_concept_id(dataset, conc.table, role, f"{mkey}##{b}") for b in bases]
                conc.issues.append(
                    Issue(
                        ISSUE_AMBIGUOUS,
                        ids,
                        f"{len(bases)} variables in {year} share the label '{canon}'; "
                        "they are kept separate and cannot be matched to other years automatically.",
                        {"year": year, "codes": sorted(bases), "canonical_label": canon},
                    )
                )

    rule_merged = _apply_rules(conc, client) if apply_rules else set()
    _assign_statuses(conc)
    for cid in rule_merged:
        if cid in conc.concepts and STATUS_OVERRIDDEN not in conc.concepts[cid].statuses:
            conc.concepts[cid].statuses.append(STATUS_OVERRIDDEN)
    _detect_code_reuse(conc)
    _detect_possible_continuations(conc)
    _detect_universe_changes(conc, universes)
    return conc


def _assign_statuses(conc: Concordance) -> None:
    available = [y for y in conc.years if conc.year_metadata.get(y)]
    if not available:
        return
    first, last = min(available), max(available)
    for c in conc.concepts.values():
        statuses: List[str] = []
        if len(set(c.codes.values())) > 1:
            statuses.append(STATUS_RENUMBERED)
        if min(c.codes) > first:
            statuses.append(STATUS_ADDED)
        if max(c.codes) < last:
            statuses.append(STATUS_DISCONTINUED)
        c.statuses = statuses or [STATUS_STABLE]


def _detect_code_reuse(conc: Concordance) -> None:
    """Same code mapped to different concepts in different years."""
    by_code: Dict[str, List[Tuple[int, str]]] = {}
    for cid, c in conc.concepts.items():
        for year, base in c.codes.items():
            by_code.setdefault(base, []).append((year, cid))
    for base, entries in by_code.items():
        ordered = sorted(entries)
        segments: List[Tuple[str, List[int]]] = []
        for year, cid in ordered:
            if segments and segments[-1][0] == cid:
                segments[-1][1].append(year)
            else:
                segments.append((cid, [year]))
        for (cid_a, years_a), (cid_b, years_b) in zip(segments, segments[1:]):
            if cid_a == cid_b:
                continue
            a, b = conc.concepts[cid_a], conc.concepts[cid_b]
            sim = label_similarity(a.canonical, b.canonical)
            # If either concept also exists (exact label match) during the other's
            # years, the code was reassigned by renumbering: both concepts are
            # independently established, so this is not a candidate rewording.
            shifted = bool(set(a.codes) & set(years_b)) or bool(set(b.codes) & set(years_a))
            kind = ISSUE_RELABELED if sim >= RELABEL_SIMILARITY and not shifted else ISSUE_REDEFINED
            conc.issues.append(
                Issue(
                    kind,
                    [cid_a, cid_b],
                    (
                        f"{base} changed label between {years_a[-1]} and {years_b[0]} "
                        f"(similarity {sim:.2f}). "
                        + (
                            "Kept as two concepts; review whether this is a wording change "
                            "(merge) or a new measure (split)."
                            if kind == ISSUE_RELABELED
                            else (
                                "Code reassigned by renumbering; both measures match exactly "
                                "in other years and are kept as separate concepts."
                                if shifted
                                else "Treated as a different measure; kept as two concepts."
                            )
                        )
                    ),
                    {
                        "code": base,
                        "before": {"years": years_a, "label": a.canonical},
                        "after": {"years": years_b, "label": b.canonical},
                        "similarity": round(sim, 4),
                        "code_shift": shifted,
                    },
                )
            )


def _detect_possible_continuations(conc: Concordance) -> None:
    """A concept ends where a similar one (different code and label) begins."""
    ending = [c for c in conc.concepts.values() if STATUS_DISCONTINUED in c.statuses]
    starting = [c for c in conc.concepts.values() if STATUS_ADDED in c.statuses]
    reused = {cid for i in conc.issues if i.kind in (ISSUE_RELABELED, ISSUE_REDEFINED) for cid in i.concept_ids}
    for a in ending:
        if a.concept_id in reused:
            continue
        best: Optional[Tuple[float, Concept]] = None
        for b in starting:
            if b.role != a.role or b.concept_id in reused or min(b.codes) <= max(a.codes):
                continue
            sim = label_similarity(a.canonical, b.canonical)
            if sim >= RELABEL_SIMILARITY and (best is None or sim > best[0]):
                best = (sim, b)
        if best:
            sim, b = best
            conc.issues.append(
                Issue(
                    ISSUE_POSSIBLE_CONTINUATION,
                    [a.concept_id, b.concept_id],
                    f"{a.codes[max(a.codes)]} ends in {max(a.codes)} and {b.codes[min(b.codes)]} "
                    f"starts in {min(b.codes)} with a similar label ({sim:.2f}). Kept separate; "
                    "merge only through a reviewed override.",
                    {
                        "before": {"years": a.years, "code": a.codes[max(a.codes)], "label": a.canonical},
                        "after": {"years": b.years, "code": b.codes[min(b.codes)], "label": b.canonical},
                        "similarity": round(sim, 4),
                    },
                )
            )


_UNIVERSE_CODE_RE = re.compile(r"^[A-Z0-9_]+$")


def _normalize_universe(u: str) -> str:
    return _SPACE_RE.sub(" ", u).strip().lower().rstrip(".")


def _detect_universe_changes(conc: Concordance, universes: Dict[int, str]) -> None:
    # Some vintages (e.g. 2020) publish universe *codes* such as TOTAL_POP
    # instead of text; those cannot be compared with text and are skipped.
    known = {
        y: _normalize_universe(u)
        for y, u in universes.items()
        if u and not _UNIVERSE_CODE_RE.match(u.strip())
    }
    if len(set(known.values())) <= 1:
        return
    affected = [
        cid
        for cid, c in conc.concepts.items()
        if len({known[y] for y in c.codes if y in known}) > 1
    ]
    if affected:
        conc.issues.append(
            Issue(
                ISSUE_UNIVERSE_CHANGED,
                affected,
                f"Table universe for {conc.table} differs across years; values may use "
                "different denominators.",
                {"universes": {str(y): universes[y] for y in sorted(known)}},
            )
        )


# ---------------------------------------------------------------------------
# Evidence and reviewed overrides
# ---------------------------------------------------------------------------


def crosswalk_dir() -> str:
    return os.getenv("CENSUS_CROSSWALK_DIR") or get_resource_path("crosswalks")


def crosswalk_path(dataset: str, table: str) -> str:
    return os.path.join(crosswalk_dir(), dataset, f"{table.upper()}.csv")


def evidence_path(evidence_id: str) -> str:
    return os.path.join(crosswalk_dir(), "evidence", f"{evidence_id}.json")


def build_evidence(
    table_code: str,
    dataset: str,
    entries: Sequence[Tuple[int, str]],
    client: Any = None,
    continuity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Machine-generated evidence for a set of (year, variable base) codes.

    Built only from vintage metadata, so validators can regenerate and compare it.
    """
    records = []
    for year, base in sorted((int(y), b) for y, b in entries):
        variables, source = table_variables(table_code, dataset, year, client)
        meta = _primary_meta(variables, base)
        records.append(
            {
                "year": year,
                "code": base,
                "exists": bool(meta),
                "label": meta.get("label", ""),
                "canonical_label": canonical_label(meta.get("label", "")) if meta else "",
                "concept": meta.get("concept", ""),
                "universe": table_universe(table_code, dataset, year, client),
                "metadata_source": source,
            }
        )
    labels = [r["canonical_label"] for r in records]
    sims = [
        {"a": i, "b": j, "similarity": round(label_similarity(labels[i], labels[j]), 4)}
        for i in range(len(labels))
        for j in range(i + 1, len(labels))
    ]
    evidence: Dict[str, Any] = {
        "table": table_code.upper(),
        "dataset": dataset,
        "normalization_version": NORMALIZATION_VERSION,
        "records": records,
        "similarities": sims,
    }
    if continuity is not None:
        evidence["continuity"] = continuity
    return evidence


def _metadata_part(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in evidence.items() if k != "continuity"}


def evidence_id_for(evidence: Dict[str, Any]) -> str:
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def save_evidence(evidence: Dict[str, Any]) -> str:
    eid = evidence_id_for(evidence)
    path = evidence_path(eid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2, sort_keys=True)
        f.write("\n")
    return eid


def load_crosswalk(dataset: str, table: str) -> List[Dict[str, str]]:
    path = crosswalk_path(dataset, table)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def validate_crosswalk_row(
    row: Dict[str, str], dataset: str, table: str, client: Any = None
) -> List[str]:
    """Return the list of problems with one override row (empty = valid).

    Review requirements (docs/agent_protocol.md):
      merge rows need generated evidence that still matches the archive, a
      census.gov primary source, a passed continuity check (or a written
      continuity_exception), and a reviewer different from the drafter.
      split/exclude rows need a rationale and a reviewer.
    """
    errors: List[str] = []
    missing = [c for c in ("year", "code", "decision", "rationale", "drafted_by", "reviewed_by", "reviewed_on") if not str(row.get(c, "")).strip()]
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    decision = str(row.get("decision", "")).strip()
    if decision not in DECISIONS:
        errors.append(f"decision must be one of {DECISIONS}")
    drafted, reviewed = str(row.get("drafted_by", "")).strip(), str(row.get("reviewed_by", "")).strip()
    for name, value in (("drafted_by", drafted), ("reviewed_by", reviewed)):
        if value and not IDENTITY_RE.match(value):
            errors.append(f"{name} must look like 'human:<name>' or 'agent:<model>/<session>'")
    if drafted and reviewed and drafted == reviewed:
        errors.append("reviewed_by must differ from drafted_by")

    try:
        year = int(str(row.get("year", "")).strip())
    except ValueError:
        errors.append("year must be an integer")
        return errors
    code = str(row.get("code", "")).strip()
    parsed = parse_variable_code(code)
    base = parsed[0] if parsed else code
    variables, _ = table_variables(table, dataset, year, client)
    if not any(f"{base}{s}" in variables for s in PRIMARY_SUFFIXES):
        errors.append(f"{code} does not exist in {dataset} {table} {year} metadata")

    if decision == "merge":
        if not str(row.get("concept_id", "")).strip():
            errors.append("merge rows need a target concept_id")
        host = urlparse(str(row.get("source_url", "")).strip()).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in PRIMARY_SOURCE_HOSTS):
            errors.append("source_url must be a census.gov primary source")
        eid = str(row.get("evidence_id", "")).strip()
        path = evidence_path(eid) if eid else ""
        if not eid or not os.path.exists(path):
            errors.append("evidence_id must reference a file in crosswalks/evidence/")
        else:
            with open(path, encoding="utf-8") as f:
                evidence = json.load(f)
            if evidence_id_for(evidence) != eid:
                errors.append("evidence file content does not match its evidence_id")
            entries = [(r["year"], r["code"]) for r in evidence.get("records", [])]
            if (year, base) not in [(int(y), c) for y, c in entries]:
                errors.append("evidence does not cover this (year, code)")
            regenerated = build_evidence(table, dataset, entries, client)
            if _metadata_part(regenerated) != _metadata_part(evidence):
                errors.append("evidence no longer matches archived metadata; regenerate it")
            continuity = evidence.get("continuity") or {}
            if not continuity.get("passed") and not str(row.get("continuity_exception", "")).strip():
                errors.append("continuity check must pass, or continuity_exception must explain why not")
    return errors


def apply_overrides(conc: Concordance, client: Any = None) -> List[Dict[str, Any]]:
    """Apply valid rows from crosswalks/<dataset>/<table>.csv; report invalid ones."""
    rows = load_crosswalk(conc.dataset, conc.table)
    report: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=2):  # header is line 1
        errors = validate_crosswalk_row(row, conc.dataset, conc.table, client)
        if errors:
            conc.issues.append(
                Issue(
                    ISSUE_INVALID_OVERRIDE,
                    [],
                    f"crosswalk row {idx} rejected: {'; '.join(errors)}",
                    {"row": row, "errors": errors},
                )
            )
            report.append({"line": idx, "valid": False, "errors": errors})
            continue
        year = int(row["year"])
        if year not in conc.years:
            continue
        parsed = parse_variable_code(row["code"].strip())
        base = parsed[0] if parsed else row["code"].strip()
        source_cid = conc.code_map(year).get(base)
        if source_cid is None:
            continue
        decision = row["decision"].strip()
        reviewer = row["reviewed_by"].strip()
        source = conc.concepts[source_cid]
        target_id = row.get("concept_id", "").strip()
        resolved_kinds = {ISSUE_RELABELED, ISSUE_REDEFINED, ISSUE_POSSIBLE_CONTINUATION}

        if decision == "exclude":
            del source.codes[year]
            conc.excluded.add((year, base))
            involved = {source_cid}
        elif decision == "split" and (not target_id or target_id == source_cid):
            # Reviewed confirmation that this code is its own concept.
            source.decided_by[year] = reviewer
            if STATUS_OVERRIDDEN not in source.statuses:
                source.statuses.append(STATUS_OVERRIDDEN)
            involved = {source_cid}
        else:
            del source.codes[year]
            target_id = target_id or f"{source_cid}_split"
            target = conc.concepts.get(target_id)
            if target is None:
                target = Concept(
                    target_id, conc.table, conc.dataset, source.role,
                    source.canonical, source.label, source.concept,
                )
                conc.concepts[target_id] = target
            target.codes[year] = base
            target.decided_by[year] = reviewer
            if STATUS_OVERRIDDEN not in target.statuses:
                target.statuses.append(STATUS_OVERRIDDEN)
            involved = {source_cid, target_id}
            if decision == "merge":
                resolved_kinds = resolved_kinds | {ISSUE_AMBIGUOUS, ISSUE_UNIVERSE_CHANGED}
                if str(row.get("continuity_exception", "")).strip():
                    conc.continuity_exempt[target_id] = row["continuity_exception"].strip()

        # A reviewed decision resolves the review issues it answers.
        conc.issues = [
            i
            for i in conc.issues
            if not (i.kind in resolved_kinds and involved & set(i.concept_ids))
        ]
        if source_cid in conc.concepts and not conc.concepts[source_cid].codes:
            del conc.concepts[source_cid]
        conc.overrides_applied.append({**row, "line": str(idx)})
        report.append({"line": idx, "valid": True, "errors": []})
    if conc.overrides_applied:
        _assign_statuses_keep_override(conc)
    return report


def _assign_statuses_keep_override(conc: Concordance) -> None:
    overridden = {cid for cid, c in conc.concepts.items() if STATUS_OVERRIDDEN in c.statuses}
    _assign_statuses(conc)
    for cid in overridden:
        if cid in conc.concepts and STATUS_OVERRIDDEN not in conc.concepts[cid].statuses:
            conc.concepts[cid].statuses.append(STATUS_OVERRIDDEN)


# ---------------------------------------------------------------------------
# Table-scoped rule overrides (crosswalks/<dataset>/<table>_rules.csv)
# ---------------------------------------------------------------------------
#
# A rule is a regular-expression rewrite of the canonical labels of the
# *earlier* vintage of one adjacent year boundary (from_year -> to_year) of one
# table. A from_year line whose rewritten match key equals the match key of a
# to_year line is linked to it: the two concepts become one, and the merged
# concept is marked ``overridden`` so the continuity check treats it as a merge
# (no exemption). Rules never span tables or boundaries, never compose with each
# other (each sees the original canonical labels), and only ever add merges.

RULES_SUFFIX = "_rules"
RULE_COLUMNS = [
    "table",
    "from_year",
    "to_year",
    "pattern",
    "replacement",
    "rationale",
    "source_url",
    "evidence_id",
    "drafted_by",
    "reviewed_by",
    "reviewed_on",
]
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def rules_path(dataset: str, table: str) -> str:
    return os.path.join(crosswalk_dir(), dataset, f"{table.upper()}{RULES_SUFFIX}.csv")


def load_rules(dataset: str, table: str) -> List[Dict[str, str]]:
    path = rules_path(dataset, table)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def label_numbers(label: str) -> List[str]:
    """Sorted multiset of the numbers in a label (ages, dollar bounds, percents)."""
    return sorted(n.replace(",", "") for n in _NUMBER_RE.findall(label))


def rewrite_label(canonical: str, pattern: "re.Pattern[str]", replacement: str) -> str:
    """Apply a rule rewrite to a canonical label and re-normalize its parts.

    Rewritten parts are trimmed, whitespace-collapsed and lowercased, and empty
    parts are dropped, so a replacement written in any case or that deletes a
    part still yields a canonical form.
    """
    raw = pattern.sub(replacement, canonical)
    parts = [_SPACE_RE.sub(" ", p).strip().lower() for p in raw.split("!!")]
    return "!!".join(p for p in parts if p)


def boundary_is_adjacent(dataset: str, from_year: int, to_year: int) -> bool:
    """True when to_year is the next published vintage after from_year."""
    if to_year <= from_year:
        return False
    vintages = set(archived_vintages(dataset))
    if from_year in vintages and to_year in vintages:
        return not any(from_year < y < to_year for y in vintages)
    return to_year == from_year + 1


def _unique_keys(
    units: Dict[str, Tuple[str, str, Dict[str, str]]]
) -> Tuple[Dict[Tuple[str, str], str], Set[str]]:
    """(role, match key) -> base for keys held by one line; plus ambiguous bases."""
    seen: Dict[Tuple[str, str], List[str]] = {}
    for base, (role, canon, _) in units.items():
        seen.setdefault((role, match_key(canon)), []).append(base)
    unique = {k: v[0] for k, v in seen.items() if len(v) == 1}
    ambiguous = {b for v in seen.values() if len(v) > 1 for b in v}
    return unique, ambiguous


def build_rule_evidence(
    table_code: str,
    dataset: str,
    from_year: int,
    to_year: int,
    pattern: str,
    replacement: str,
    client: Any = None,
) -> Dict[str, Any]:
    """Machine-generated evidence for one rule, built only from vintage metadata.

    Lists every (code, before, after) label rewrite, the resulting new matches,
    the rewritten lines that still match nothing, and every guardrail
    violation (numeric changes, conflicting links). Validators regenerate it
    and compare, so stale or hand-edited evidence is rejected.
    """
    table = table_code.upper()
    from_year, to_year = int(from_year), int(to_year)
    compiled = re.compile(pattern)
    vars1, src1 = table_variables(table, dataset, from_year, client)
    vars2, src2 = table_variables(table, dataset, to_year, client)
    units1 = _units_for_year(vars1, dataset, from_year)
    units2 = _units_for_year(vars2, dataset, to_year)
    keys1, amb1 = _unique_keys(units1)
    keys2, amb2 = _unique_keys(units2)
    # Exact (automatic) links across the boundary, both directions.
    exact_fwd = {b1: keys2[k] for k, b1 in keys1.items() if k in keys2}
    exact_back = {b2: b1 for b1, b2 in exact_fwd.items()}

    rewrites: List[Dict[str, Any]] = []
    matches: List[Dict[str, str]] = []
    unmatched: List[Dict[str, str]] = []
    conflicts: List[Dict[str, str]] = []
    numeric: List[Dict[str, Any]] = []
    for base in sorted(units1):
        role, canon, _ = units1[base]
        after = rewrite_label(canon, compiled, replacement)
        if after == canon:
            continue
        rewrites.append({"code": base, "role": role, "before": canon, "after": after})
        if label_numbers(canon) != label_numbers(after):
            numeric.append(
                {"code": base, "before_numbers": label_numbers(canon), "after_numbers": label_numbers(after)}
            )
        target = keys2.get((role, match_key(after)))
        if base in amb1:
            unmatched.append({"code": base, "reason": f"ambiguous in {from_year}"})
        elif target is None:
            reason = "matches an ambiguous later line" if any(
                match_key(units2[b][1]) == match_key(after) for b in amb2
            ) else "no later line has this label"
            unmatched.append({"code": base, "reason": reason})
        elif exact_fwd.get(base) == target:
            continue  # already linked automatically; nothing new
        elif base in exact_fwd:
            conflicts.append(
                {
                    "code": base,
                    "to_code": target,
                    "reason": f"{base} already continues as {exact_fwd[base]} in {to_year}; "
                    "the rule would link it to a second later line",
                }
            )
        elif target in exact_back:
            conflicts.append(
                {
                    "code": base,
                    "to_code": target,
                    "reason": f"{target} already continues {exact_back[target]} from {from_year}; "
                    "the rule would map two earlier codes to one later concept",
                }
            )
        else:
            matches.append({"from_code": base, "to_code": target, "role": role, "label": units2[target][1]})
    by_target: Dict[str, List[str]] = {}
    for m in matches:
        by_target.setdefault(m["to_code"], []).append(m["from_code"])
    for target, sources in sorted(by_target.items()):
        if len(sources) > 1:
            conflicts.append(
                {
                    "code": ",".join(sources),
                    "to_code": target,
                    "reason": "the rule maps two earlier codes to one later concept",
                }
            )
    return {
        "kind": "rule",
        "table": table,
        "dataset": dataset,
        "from_year": from_year,
        "to_year": to_year,
        "pattern": pattern,
        "replacement": replacement,
        "normalization_version": NORMALIZATION_VERSION,
        "metadata_sources": {str(from_year): src1, str(to_year): src2},
        "rewrites": rewrites,
        "matches": matches,
        "unmatched": unmatched,
        "conflicts": conflicts,
        "numeric_changes": numeric,
    }


def rule_guardrail_errors(evidence: Dict[str, Any]) -> List[str]:
    """Guardrail violations recorded in rule evidence (empty = acceptable)."""
    errors: List[str] = []
    for year, source in (evidence.get("metadata_sources") or {}).items():
        if source == SOURCE_MISSING:
            errors.append(f"no variable metadata for {year}; the rule cannot be verified")
    if not evidence.get("rewrites"):
        errors.append("the rule matches zero labels")
    elif not evidence.get("matches"):
        errors.append("the rule produces no new matches")
    numeric = evidence.get("numeric_changes") or []
    if numeric:
        codes = ", ".join(n["code"] for n in numeric[:5])
        errors.append(
            f"the rule changes a number (age, dollar or other bound) in {len(numeric)} label(s), e.g. {codes}"
        )
    conflicts = evidence.get("conflicts") or []
    if conflicts:
        errors.append(f"the rule creates {len(conflicts)} conflicting link(s): {conflicts[0]['reason']}")
    return errors


def _check_rule_row(
    row: Dict[str, str], dataset: str, table: str, client: Any = None
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """(errors, regenerated evidence) for one rule row."""
    errors: List[str] = []
    required = ("table", "from_year", "to_year", "pattern", "rationale", "source_url", "evidence_id", "drafted_by", "reviewed_by", "reviewed_on")
    missing = [c for c in required if not str(row.get(c, "")).strip()]
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    row_table = str(row.get("table", "")).strip().upper()
    if row_table and row_table != table.upper():
        errors.append(f"rule table {row_table} does not match its file's table {table.upper()}")
    drafted, reviewed = str(row.get("drafted_by", "")).strip(), str(row.get("reviewed_by", "")).strip()
    for name, value in (("drafted_by", drafted), ("reviewed_by", reviewed)):
        if value and not IDENTITY_RE.match(value):
            errors.append(f"{name} must look like 'human:<name>' or 'agent:<model>/<session>'")
    if drafted and reviewed and drafted == reviewed:
        errors.append("reviewed_by must differ from drafted_by")
    host = urlparse(str(row.get("source_url", "")).strip()).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in PRIMARY_SOURCE_HOSTS):
        errors.append("source_url must be a census.gov primary source")
    try:
        from_year = int(str(row.get("from_year", "")).strip())
        to_year = int(str(row.get("to_year", "")).strip())
    except ValueError:
        errors.append("from_year and to_year must be integers")
        return errors, None
    if not boundary_is_adjacent(dataset, from_year, to_year):
        errors.append(f"{from_year} -> {to_year} is not one adjacent vintage boundary")
    pattern = str(row.get("pattern", ""))
    replacement = str(row.get("replacement", "") or "")
    try:
        compiled = re.compile(pattern)
        compiled.sub(replacement, "")  # surfaces bad group references
    except (re.error, IndexError) as exc:
        errors.append(f"pattern/replacement is not a valid regular expression: {exc}")
        return errors, None
    if not pattern:
        return errors, None

    regenerated = build_rule_evidence(table, dataset, from_year, to_year, pattern, replacement, client)
    errors.extend(rule_guardrail_errors(regenerated))
    eid = str(row.get("evidence_id", "")).strip()
    path = evidence_path(eid) if eid else ""
    if not eid or not os.path.exists(path):
        errors.append("evidence_id must reference a file in crosswalks/evidence/")
    else:
        with open(path, encoding="utf-8") as f:
            evidence = json.load(f)
        if evidence_id_for(evidence) != eid:
            errors.append("evidence file content does not match its evidence_id")
        elif evidence != regenerated:
            errors.append(
                "evidence no longer matches this rule and the archived metadata; regenerate it "
                "with draft_rule_override"
            )
    return errors, regenerated


def validate_rule_row(
    row: Dict[str, str], dataset: str, table: str, client: Any = None
) -> List[str]:
    """Return the list of problems with one rule row (empty = valid).

    Requirements: a census.gov source_url; two different identities; an
    adjacent year boundary; generated evidence whose hash matches its ID and
    that still matches the archive when regenerated; and the guardrails (the
    rule rewrites at least one label, creates at least one match, changes no
    number in any label, and never links two earlier codes to one later line
    or one earlier code to two later lines).
    """
    return _check_rule_row(row, dataset, table, client)[0]


def rule_cross_conflicts(
    checked: Sequence[Tuple[int, Dict[str, str], Optional[Dict[str, Any]]]]
) -> Dict[int, List[str]]:
    """Rules on the same boundary must not claim the same earlier or later line."""
    claimed: Dict[Tuple[int, int, str, str], int] = {}
    out: Dict[int, List[str]] = {}
    for line, _row, evidence in checked:
        if not evidence:
            continue
        b = (evidence["from_year"], evidence["to_year"])
        for m in evidence.get("matches", []):
            for side, code in (("from", m["from_code"]), ("to", m["to_code"])):
                key = (b[0], b[1], side, code)
                if key in claimed and claimed[key] != line:
                    out.setdefault(line, []).append(
                        f"{code} is already linked by the rule on line {claimed[key]}"
                    )
                claimed.setdefault(key, line)
    return out


def _apply_rules(conc: Concordance, client: Any = None) -> Set[str]:
    """Apply valid rules for the concordance's table; return merged concept IDs."""
    rows = load_rules(conc.dataset, conc.table)
    if not rows:
        return set()
    checked = []
    for idx, row in enumerate(rows, start=2):
        errors, evidence = _check_rule_row(row, conc.dataset, conc.table, client)
        checked.append((idx, row, evidence, errors))
    cross = rule_cross_conflicts([(i, r, e) for i, r, e, errs in checked if not errs])
    merged: Set[str] = set()
    for idx, row, evidence, errors in checked:
        errors = errors + cross.get(idx, [])
        if errors:
            conc.issues.append(
                Issue(
                    ISSUE_INVALID_OVERRIDE,
                    [],
                    f"rule row {idx} rejected: {'; '.join(errors)}",
                    {"row": row, "errors": errors, "file": rules_path(conc.dataset, conc.table)},
                )
            )
            continue
        assert evidence is not None
        y1, y2 = evidence["from_year"], evidence["to_year"]
        if y1 not in conc.years or y2 not in conc.years:
            continue
        reviewer = row["reviewed_by"].strip()
        map1, map2 = conc.code_map(y1), conc.code_map(y2)
        linked: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []
        for m in evidence["matches"]:
            a_id, b_id = map1.get(m["from_code"]), map2.get(m["to_code"])
            a = conc.concepts.get(a_id or "")
            b = conc.concepts.get(b_id or "")
            if a is None or b is None or a is b:
                continue
            if max(a.codes) >= y2 or min(b.codes) <= y1:
                skipped.append({**m, "reason": "concepts overlap in years"})
                continue
            for year, base in a.codes.items():
                b.codes[year] = base
                b.decided_by[year] = reviewer
                if year in a.universes:
                    b.universes[year] = a.universes[year]
            del conc.concepts[a.concept_id]
            merged.discard(a.concept_id)
            merged.add(b.concept_id)
            linked.append({**m, "concept_id": b.concept_id, "merged_concept_id": a.concept_id})
        record = {
            **{c: str(row.get(c, "") or "") for c in RULE_COLUMNS},
            "decision": "rule",
            "line": str(idx),
            "file": rules_path(conc.dataset, conc.table),
            "merges": len(linked),
        }
        conc.rules_applied.append({**record, "links": linked, "skipped": skipped})
        conc.overrides_applied.append(record)
    return merged


def validate_rule_file(dataset: str, table: str, client: Any = None) -> Dict[str, Any]:
    """Validate one crosswalks/<dataset>/<table>_rules.csv file."""
    path = rules_path(dataset, table)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header_ok = list(reader.fieldnames or []) == RULE_COLUMNS
        rows = list(reader)
    file_errors = [] if header_ok else [f"header must be: {','.join(RULE_COLUMNS)}"]
    checked = []
    for idx, row in enumerate(rows, start=2):
        errors, evidence = _check_rule_row(row, dataset, table, client)
        file_errors.extend(f"line {idx}: {e}" for e in errors)
        if not errors:
            checked.append((idx, row, evidence))
    for idx, errs in sorted(rule_cross_conflicts(checked).items()):
        file_errors.extend(f"line {idx}: {e}" for e in errs)
    return {"file": f"{dataset}/{os.path.basename(path)}", "rows": len(rows), "errors": file_errors, "kind": "rule"}


def validate_all_crosswalks(client: Any = None) -> Dict[str, Any]:
    """Validate every crosswalk and rule file (used by CI and the MCP tool)."""
    root = crosswalk_dir()
    results: List[Dict[str, Any]] = []
    if os.path.isdir(root):
        for dataset in sorted(os.listdir(root)):
            ddir = os.path.join(root, dataset)
            if dataset == "evidence" or not os.path.isdir(ddir):
                continue
            for name in sorted(os.listdir(ddir)):
                if not name.endswith(".csv"):
                    continue
                table = name[:-4]
                if table.endswith(RULES_SUFFIX):
                    results.append(validate_rule_file(dataset, table[: -len(RULES_SUFFIX)], client))
                    continue
                with open(os.path.join(ddir, name), newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    header_ok = list(reader.fieldnames or []) == CROSSWALK_COLUMNS
                    rows = list(reader)
                file_errors = [] if header_ok else [f"header must be: {','.join(CROSSWALK_COLUMNS)}"]
                for idx, row in enumerate(rows, start=2):
                    for err in validate_crosswalk_row(row, dataset, table, client):
                        file_errors.append(f"line {idx}: {err}")
                results.append({"file": f"{dataset}/{name}", "rows": len(rows), "errors": file_errors})
    return {
        "valid": all(not r["errors"] for r in results),
        "files": results,
    }
