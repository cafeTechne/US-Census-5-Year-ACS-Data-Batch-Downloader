"""Tests for table-scoped concordance rule overrides (Round 7, Workstream G).

Real case from the concordance audit, offline metadata archive (metadata/):

* S1701 (poverty status), subject tables 2016 -> 2017. The 2017 redesign
  inserted the universe header "Population for whom poverty status is
  determined" into every line of the main block of each column (Total, Below
  poverty level, Percent below poverty level), e.g.

      2016 S1701_C01_002  total!!age!!under 18 years
      2017 S1701_C01_002  total!!population for whom poverty status is determined!!age!!under 18 years

  and "Unrelated individuals for whom poverty status is determined" into the
  unrelated-individuals block. Without a rule, 171 of the 183 lines fail to
  match across the boundary.
* S1601 (language spoken at home), 2016 -> 2017: the inserted header is
  "Population 5 years and over", which contains a number, so a rule that adds
  it is rejected by the numeric guardrail.

No network: national rows for the continuity check come from a fake client.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from census.api import CensusAPIClient
from census.concordance import (
    ISSUE_CONTINUITY_FLAG,
    ISSUE_CONTINUITY_UNAVAILABLE,
    ISSUE_INVALID_OVERRIDE,
    ISSUE_REDEFINED,
    ISSUE_RELABELED,
    RULE_COLUMNS,
    STATUS_OVERRIDDEN,
    build_concordance,
    build_rule_evidence,
    evidence_path,
    label_numbers,
    rules_path,
    validate_all_crosswalks,
    validate_rule_row,
)
from census.pipeline import ResearchOptions, build_entities, plan_extraction
from census.review import draft_rule_override, record_rule_review

SOURCE = "https://www.census.gov/programs-surveys/acs/technical-documentation/table-and-geography-changes/2017/5-year.html"
COLUMNS = "(total|below poverty level|percent below poverty level)"
MAIN_PATTERN = (
    "^" + COLUMNS + "!!(age|sex|race and hispanic or latino origin|educational attainment|"
    "employment status|work experience|all individuals with income below the following poverty ratios)(!!|$)"
)
MAIN_REPLACEMENT = r"\1!!population for whom poverty status is determined!!\2\3"
UNRELATED_PATTERN = "^" + COLUMNS + "!!(?=\\d|15 years|75 years|mean income deficit|worked|did not work)"
UNRELATED_REPLACEMENT = r"\1!!unrelated individuals for whom poverty status is determined!!"
DRAFTER = "agent:claude-opus-5-5/drafter-session"
REVIEWER = "agent:claude-opus-5-5/reviewer-session"


class NationalClient(CensusAPIClient):
    """Serves national S1701 rows: {year: {base: (estimate, moe)}}."""

    def __init__(self, values=None):
        super().__init__(api_key="", cache=None)
        self.values = values or {}
        self.calls = []

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        self.calls.append((year, table_code, kwargs.get("geo_level")))
        cells = self.values.get(year)
        if cells is None:
            return None
        header, row = ["NAME"], ["United States"]
        for base, (est, moe) in cells.items():
            header += [f"{base}E", f"{base}M"]
            row += [str(est), str(moe)]
        if kwargs.get("geo_level") == "us":
            return [header + ["us"], row + ["1"]]
        return [header + ["state"], row + ["36"]]

    def fetch_variable_metadata(self, *a, **k):  # type: ignore[override]
        return {}


class RuleTestCase(unittest.TestCase):
    def setUp(self):
        self.xw = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"CENSUS_CROSSWALK_DIR": self.xw})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.xw, ignore_errors=True)

    def draft(self, pattern=MAIN_PATTERN, replacement=MAIN_REPLACEMENT, table="S1701", **kw):
        args = dict(
            drafted_by=DRAFTER,
            rationale="2017 redesign inserted the universe header into every label; lines unchanged.",
            source_url=SOURCE,
        )
        args.update(kw)
        return draft_rule_override(table, "subject", 2016, 2017, pattern, replacement, **args)

    def record(self, draft, reviewer=REVIEWER):
        return record_rule_review(draft["draft_row"], reviewer, "subject")


class TestRuleEvidence(RuleTestCase):
    def test_real_header_insertion_is_resolved(self):
        ev = build_rule_evidence("S1701", "subject", 2016, 2017, MAIN_PATTERN, MAIN_REPLACEMENT)
        self.assertEqual(len(ev["rewrites"]), 126)  # 42 lines x 3 columns
        self.assertEqual(len(ev["matches"]), 126)
        self.assertEqual(ev["unmatched"], [])
        self.assertEqual(ev["conflicts"], [])
        self.assertEqual(ev["numeric_changes"], [])
        first = next(r for r in ev["rewrites"] if r["code"] == "S1701_C01_002")
        self.assertEqual(first["before"], "total!!age!!under 18 years")
        self.assertEqual(first["after"], "total!!population for whom poverty status is determined!!age!!under 18 years")
        # Every match is the same line number in the redesigned table.
        self.assertTrue(all(m["from_code"] == m["to_code"] for m in ev["matches"]))

    def test_draft_preview_and_saved_evidence(self):
        draft = self.draft()
        self.assertTrue(draft["success"])
        self.assertEqual(draft["guardrail_errors"], [])
        self.assertTrue(draft["evidence_saved"])
        self.assertEqual(draft["preview"]["labels_rewritten"], 126)
        self.assertEqual(draft["preview"]["new_matches"], 126)
        self.assertEqual(draft["preview"]["still_unmatched"], 0)
        self.assertTrue(os.path.exists(evidence_path(draft["evidence_id"])))
        self.assertTrue(draft["evidence_id"].startswith("ev_"))
        self.assertEqual(draft["draft_row"]["reviewed_by"], "")
        self.assertEqual(set(draft["draft_row"]), set(RULE_COLUMNS))

    def test_label_numbers(self):
        self.assertEqual(label_numbers("total!!18 to 64 years!!$1,000 or more"), ["1000", "18", "64"])


class TestRuleApplication(RuleTestCase):
    def test_rule_merges_relabeled_items(self):
        before = build_concordance("S1701", "subject", [2016, 2017])
        review_before = sum(1 for i in before.issues if i.kind in (ISSUE_RELABELED, ISSUE_REDEFINED))
        self.assertEqual(review_before, 171)
        self.assertNotEqual(before.code_map(2016)["S1701_C01_002"], before.code_map(2017)["S1701_C01_002"])

        res = self.record(self.draft())
        self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(os.path.exists(rules_path("subject", "S1701")))
        self.assertTrue(validate_all_crosswalks()["valid"], validate_all_crosswalks())

        after = build_concordance("S1701", "subject", [2016, 2017])
        cid = after.code_map(2017)["S1701_C01_002"]
        self.assertEqual(after.code_map(2016)["S1701_C01_002"], cid)
        concept = after.concepts[cid]
        self.assertEqual(concept.codes, {2016: "S1701_C01_002", 2017: "S1701_C01_002"})
        self.assertIn(STATUS_OVERRIDDEN, concept.statuses)
        self.assertEqual(concept.decided_by[2016], REVIEWER)
        self.assertIn("population for whom poverty status is determined", concept.canonical)
        review_after = sum(1 for i in after.issues if i.kind in (ISSUE_RELABELED, ISSUE_REDEFINED))
        self.assertEqual(review_before - review_after, 126)
        self.assertEqual(after.rules_applied[0]["merges"], 126)
        self.assertEqual(after.overrides_applied[0]["decision"], "rule")
        self.assertEqual(len(before.concepts) - len(after.concepts), 126)
        # Rules are scoped to their boundary: a range that does not include it is unaffected.
        other = build_concordance("S1701", "subject", [2017, 2018])
        self.assertEqual(other.rules_applied, [])

    def test_rule_extends_the_earlier_chain(self):
        self.assertTrue(self.record(self.draft())["success"])
        conc = build_concordance("S1701", "subject", [2015, 2016, 2017])
        cid = conc.code_map(2017)["S1701_C01_002"]
        self.assertEqual(conc.concepts[cid].years, [2015, 2016, 2017])

    def test_two_rules_on_one_boundary(self):
        self.assertTrue(self.record(self.draft())["success"])
        second = self.draft(UNRELATED_PATTERN, UNRELATED_REPLACEMENT)
        self.assertEqual(second["guardrail_errors"], [])
        self.assertEqual(second["preview"]["new_matches"], 39)
        self.assertTrue(self.record(second)["success"])
        self.assertTrue(validate_all_crosswalks()["valid"])
        conc = build_concordance("S1701", "subject", [2016, 2017])
        remaining = sum(1 for i in conc.issues if i.kind in (ISSUE_RELABELED, ISSUE_REDEFINED))
        self.assertEqual(remaining, 171 - 126 - 39)
        self.assertEqual([r["merges"] for r in conc.rules_applied], [126, 39])

    def test_invalid_rule_row_is_reported_not_applied(self):
        draft = self.draft()
        path = rules_path("subject", "S1701")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = dict(draft["draft_row"], reviewed_by=DRAFTER, reviewed_on="2026-11-02")  # self-review
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(RULE_COLUMNS) + "\n")
            import csv

            csv.DictWriter(f, fieldnames=RULE_COLUMNS).writerow(row)
        conc = build_concordance("S1701", "subject", [2016, 2017])
        self.assertTrue(any(i.kind == ISSUE_INVALID_OVERRIDE and i.blocking for i in conc.issues))
        self.assertEqual(conc.rules_applied, [])
        self.assertNotEqual(conc.code_map(2016)["S1701_C01_002"], conc.code_map(2017)["S1701_C01_002"])
        result = validate_all_crosswalks()
        self.assertFalse(result["valid"])
        self.assertTrue(any("differ" in e for f in result["files"] for e in f["errors"]))


class TestRuleContinuity(RuleTestCase):
    def _plan(self, client):
        return plan_extraction(
            "S1701", build_entities("state", "36"), [2016, 2017],
            options=ResearchOptions(use_cache=False), variables=["S1701_C01_002"], client=client,
        )

    def setUp(self):
        super().setUp()
        self.assertTrue(self.record(self.draft())["success"])

    def test_merged_concept_is_continuity_checked_and_passes(self):
        client = NationalClient({2016: {"S1701_C01_002": (72_000_000, 20_000)}, 2017: {"S1701_C01_002": (72_500_000, 20_000)}})
        plan = self._plan(client)
        self.assertTrue(plan["success"], plan.get("error"))
        self.assertEqual(plan["selected_concepts"], 1)
        self.assertEqual(plan["continuity"], {"checked_concepts": 1, "passed": 1})
        self.assertEqual({c[0] for c in client.calls if c[2] == "us"}, {2016, 2017})
        self.assertFalse(plan["would_block"], plan["issues"])

    def test_rule_grants_no_continuity_exemption(self):
        client = NationalClient({2016: {"S1701_C01_002": (72_000_000, 20_000)}, 2017: {"S1701_C01_002": (150_000_000, 20_000)}})
        plan = self._plan(client)
        self.assertTrue(plan["would_block"])
        self.assertIn(ISSUE_CONTINUITY_FLAG, {i["kind"] for i in plan["issues"]})

    def test_missing_national_data_blocks_merged_concept(self):
        plan = self._plan(NationalClient({2017: {"S1701_C01_002": (72_500_000, 20_000)}}))
        self.assertTrue(plan["would_block"])
        self.assertIn(ISSUE_CONTINUITY_UNAVAILABLE, {i["kind"] for i in plan["issues"]})

    def test_draft_previews_continuity(self):
        client = NationalClient({
            2016: {"S1701_C01_002": (72_000_000, 20_000), "S1701_C01_003": (19_000_000, 10_000)},
            2017: {"S1701_C01_002": (72_500_000, 20_000), "S1701_C01_003": (40_000_000, 10_000)},
        })
        preview = self.draft(UNRELATED_PATTERN, UNRELATED_REPLACEMENT, client=client)["continuity"]
        self.assertEqual(preview["checked"], 39)
        main = draft_rule_override("S1701", "subject", 2016, 2017, MAIN_PATTERN, MAIN_REPLACEMENT,
                                   DRAFTER, "r", SOURCE, client=client)["continuity"]
        self.assertEqual(main["checked"], 126)
        self.assertEqual([f["from_code"] for f in main["flagged"]], ["S1701_C01_003"])
        self.assertEqual(main["passed"], 1)


class TestRuleGuardrails(RuleTestCase):
    def test_real_numeric_header_is_rejected(self):
        draft = self.draft("^total!!", "total!!population 5 years and over!!", table="S1601")
        self.assertTrue(any("number" in e for e in draft["guardrail_errors"]), draft["guardrail_errors"])
        self.assertGreater(draft["preview"]["numeric_changes"], 0)
        self.assertFalse(draft["evidence_saved"])
        res = self.record(draft)
        self.assertFalse(res["success"])
        self.assertIn("number", res["error"])

    def test_rule_changing_a_numeric_bound_is_rejected(self):
        draft = self.draft(
            r"^(total)!!age!!under 18 years!!5 to 17 years$",
            r"\1!!population for whom poverty status is determined!!age!!under 18 years!!5 to 18 years",
        )
        self.assertTrue(any("number" in e for e in draft["guardrail_errors"]))
        # Even with evidence saved by hand, review rejects it.
        ev = build_rule_evidence("S1701", "subject", 2016, 2017, draft["draft_row"]["pattern"], draft["draft_row"]["replacement"])
        from census.concordance import save_evidence

        row = dict(draft["draft_row"], evidence_id=save_evidence(ev))
        res = record_rule_review(row, REVIEWER, "subject")
        self.assertFalse(res["success"])
        self.assertIn("number", res["error"])

    def test_two_earlier_codes_to_one_later_concept_rejected(self):
        draft = self.draft(r"^total!!sex!!(male|female)$", "total!!population for whom poverty status is determined!!sex!!male")
        self.assertTrue(any("two earlier codes" in e for e in draft["guardrail_errors"]), draft["guardrail_errors"])
        self.assertFalse(self.record(draft)["success"])

    def test_rule_matching_nothing_rejected(self):
        draft = self.draft(r"^no such header!!", "x!!")
        self.assertIn("the rule matches zero labels", draft["guardrail_errors"])
        self.assertFalse(self.record(draft)["success"])

    def test_self_review_rejected(self):
        draft = self.draft()
        res = self.record(draft, reviewer=DRAFTER)
        self.assertFalse(res["success"])
        self.assertIn("differ", res["error"])

    def test_source_must_be_census_gov(self):
        draft = self.draft(source_url="https://example.org/notes")
        res = self.record(draft)
        self.assertFalse(res["success"])
        self.assertIn("census.gov", res["error"])

    def test_boundary_must_be_adjacent(self):
        draft = draft_rule_override("S1701", "subject", 2015, 2017, MAIN_PATTERN, MAIN_REPLACEMENT, DRAFTER, "r", SOURCE)
        self.assertTrue(any("adjacent" in e for e in draft["guardrail_errors"]))

    def test_invalid_regex(self):
        res = draft_rule_override("S1701", "subject", 2016, 2017, "(", "x", DRAFTER, "r", SOURCE)
        self.assertEqual(res["error_code"], "INVALID_INPUT")

    def test_duplicate_and_overlapping_rules_rejected(self):
        self.assertTrue(self.record(self.draft())["success"])
        again = self.record(self.draft())
        self.assertFalse(again["success"])
        self.assertIn("identical", again["error"])
        # A narrower rule claiming lines the first rule already links.
        overlap = self.draft("^(total)!!(age)(!!|$)", MAIN_REPLACEMENT)
        self.assertEqual(overlap["guardrail_errors"], [])
        res = self.record(overlap)
        self.assertFalse(res["success"])
        self.assertIn("already linked", res["error"])


class TestStaleEvidence(RuleTestCase):
    def test_tampered_evidence_rejected(self):
        draft = self.draft()
        path = evidence_path(draft["evidence_id"])
        with open(path, encoding="utf-8") as f:
            evidence = json.load(f)
        evidence["matches"] = evidence["matches"][:3]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(evidence, f)
        res = self.record(draft)
        self.assertFalse(res["success"])
        self.assertIn("evidence", res["error"])

    def test_edited_pattern_does_not_match_evidence(self):
        draft = self.draft()
        row = dict(draft["draft_row"], pattern=MAIN_PATTERN.replace("|sex", ""))
        errors = validate_rule_row(dict(row, reviewed_by=REVIEWER, reviewed_on="2026-11-02"), "subject", "S1701")
        self.assertTrue(any("no longer matches" in e for e in errors), errors)

    def test_archive_change_makes_recorded_rule_stale(self):
        self.assertTrue(self.record(self.draft())["success"])
        from census import concordance as mod

        real = mod.table_variables

        def changed(table, dataset, year, client=None):
            variables, source = real(table, dataset, year, client)
            if year == 2017:
                variables = {k: v for k, v in variables.items() if not k.startswith("S1701_C01_002")}
            return variables, source

        with patch.object(mod, "table_variables", changed):
            result = validate_all_crosswalks()
            conc = build_concordance("S1701", "subject", [2016, 2017])
        self.assertFalse(result["valid"])
        self.assertTrue(any("no longer matches" in e for f in result["files"] for e in f["errors"]))
        self.assertTrue(any(i.kind == ISSUE_INVALID_OVERRIDE for i in conc.issues))
        self.assertEqual(conc.rules_applied, [])


if __name__ == "__main__":
    unittest.main()
