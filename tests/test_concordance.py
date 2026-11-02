"""Tests for cross-vintage concordance, continuity, and reviewed overrides.

Fixtures are real ACS metadata from the offline archive (metadata/):

* DP02 2018 -> 2019: "average household size" moved from DP02_0015 to DP02_0016
  (renumbered), and DP02_0002 changed from "family households" to "married
  couple family" (code reused for a different measure).
* B27001 2016 -> 2017: B27001_006 changed from "6 to 17 years" to "6 to 18
  years" under the same code (relabeled; needs review).
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from census.api import CensusAPIClient
from census.concordance import (
    ISSUE_CONTINUITY_FLAG,
    ISSUE_REDEFINED,
    ISSUE_RELABELED,
    STATUS_RENUMBERED,
    build_concordance,
    canonical_label,
    match_key,
    validate_all_crosswalks,
)
from census.continuity import check_concepts
from census.pipeline import ResearchOptions, build_entities, plan_extraction, run_batch
from census.review import draft_override, record_override


class TestCanonicalLabel(unittest.TestCase):
    def test_measurement_prefixes(self):
        self.assertEqual(canonical_label("Estimate!!Total:!!Male:"), "total!!male")
        self.assertEqual(canonical_label("Percent!!EMPLOYMENT STATUS!!In labor force"), "employment status!!in labor force")
        self.assertEqual(
            canonical_label("2013-2017 Estimates!!EMPLOYMENT STATUS!!Population 16 years and over"),
            "employment status!!population 16 years and over",
        )
        self.assertEqual(canonical_label("Number!!Estimate!!ANCESTRY!!Scottish"), "ancestry!!scottish")
        self.assertEqual(
            canonical_label("Percent Estimate!!HOUSEHOLDS BY TYPE!!Total households"),
            "households by type!!total households",
        )

    def test_mid_label_estimate_marker(self):
        self.assertEqual(
            canonical_label("Veterans!!Estimate!!EDUCATIONAL ATTAINMENT"),
            canonical_label("Veterans!!EDUCATIONAL ATTAINMENT"),
        )

    def test_dollar_year_and_whitespace(self):
        a = "Estimate!!Median earnings in the past 12 months (in 2020 inflation-adjusted dollars) --!!Total:!!Female --!!Total "
        b = "Estimate!!Median earnings in the past 12 months (in 2021 inflation-adjusted dollars) --!!Total:!!Female --!!Total"
        self.assertEqual(canonical_label(a), canonical_label(b))
        self.assertEqual(
            canonical_label("Total!!Earnings (in 2022 inflation adjusted dollars)"),
            canonical_label("Total!!Earnings (in 2023 inflation-adjusted dollars)"),
        )

    def test_hyphens_and_subject_placeholder(self):
        self.assertEqual(
            canonical_label("Foreign born; entered before 2000!!SEX"),
            canonical_label("Foreign-born; entered before 2000!!SEX"),
        )
        self.assertEqual(canonical_label("With a disability!!Subject!!AGE"), canonical_label("With a disability!!AGE"))

    def test_real_differences_survive(self):
        self.assertNotEqual(canonical_label("Total!!Male!!6 to 17 years"), canonical_label("Total!!Male!!6 to 18 years"))
        self.assertNotEqual(canonical_label("Total!!Male!!Under 5"), canonical_label("Total!!Female!!Under 5"))

    def test_match_key_is_order_insensitive(self):
        self.assertEqual(match_key("a!!b!!c"), match_key("b!!a!!c"))


class TestRealConcordances(unittest.TestCase):
    def test_renumbered_line_is_one_concept(self):
        conc = build_concordance("DP02", "profile", [2018, 2019])
        cid = conc.code_map(2018)["DP02_0015"]
        concept = conc.concepts[cid]
        self.assertIn("average household size", concept.canonical)
        self.assertEqual(concept.codes, {2018: "DP02_0015", 2019: "DP02_0016"})
        self.assertIn(STATUS_RENUMBERED, concept.statuses)
        self.assertEqual(conc.rename_map(2018)["DP02_0015"], conc.rename_map(2019)["DP02_0016"])

    def test_reused_code_never_shares_a_column(self):
        conc = build_concordance("DP02", "profile", [2018, 2019])
        a, b = conc.code_map(2018)["DP02_0002"], conc.code_map(2019)["DP02_0002"]
        self.assertNotEqual(a, b)
        reps = conc.representative_bases()
        self.assertNotEqual(reps[a], reps[b])
        self.assertTrue(any(i.kind == ISSUE_REDEFINED and {a, b} <= set(i.concept_ids) for i in conc.issues))

    def test_relabel_needs_review(self):
        conc = build_concordance("B27001", "acs5", [2016, 2017])
        issues = [i for i in conc.issues if i.kind == ISSUE_RELABELED and i.evidence["code"] == "B27001_006"]
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].blocking)
        self.assertIn("6 to 17", issues[0].evidence["before"]["label"])
        self.assertIn("6 to 18", issues[0].evidence["after"]["label"])

    def test_property_every_cell_maps_to_one_concept(self):
        for table, dataset, years in (
            ("DP02", "profile", [2017, 2018, 2019, 2020]),
            ("S1701", "subject", [2015, 2016, 2017, 2018]),
            ("CP03", "cprofile", [2021, 2022]),
        ):
            conc = build_concordance(table, dataset, years)
            reps = conc.representative_bases()
            self.assertEqual(len(set(reps.values())), len(reps), table)
            for year in years:
                bases = list(conc.code_map(year))
                self.assertEqual(len(bases), len(set(bases)), (table, year))

    def test_stable_detailed_table(self):
        conc = build_concordance("B19013", "acs5", list(range(2015, 2023)))
        self.assertEqual(conc.summary()["status_counts"], {"stable": 1})
        self.assertEqual([i for i in conc.issues if i.blocking], [])

    def test_comparison_profile_roles(self):
        conc = build_concordance("CP03", "cprofile", [2021, 2022])
        current = conc.code_map(2022)["CP03_2022_001"]
        self.assertEqual(conc.concepts[current].role, "current")
        self.assertEqual(conc.concepts[current].codes.get(2021), "CP03_2021_001")


def _us_row(header, values):
    return [header, values]


class FakeClient(CensusAPIClient):
    """Serves DP02-like national and state rows per year."""

    def __init__(self, avg_size=(2.63, 2.62), married=(48.2, 47.9)):
        super().__init__(api_key="", cache=None)
        self.avg_size = dict(zip((2018, 2019), avg_size))
        self.married = dict(zip((2018, 2019), married))

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        size_code = "DP02_0015" if year == 2018 else "DP02_0016"
        header = ["NAME", f"{size_code}E", f"{size_code}M", "DP02_0002E", "DP02_0002M", "DP02_0002PE", "DP02_0002PM"]
        values = ["X", str(self.avg_size[year]), "0.01", "1000", "10", str(self.married[year]), "0.2"]
        if kwargs.get("geo_level") == "us":
            return _us_row(header + ["us"], values + ["1"])
        return [header + ["state"], values + ["36"]]

    def fetch_variable_metadata(self, *a, **k):  # type: ignore[override]
        return {}


class TestPipelineConcordance(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.xw = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"CENSUS_CROSSWALK_DIR": self.xw})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.xw, ignore_errors=True)

    def _run(self, variables, mode="fail", client=None):
        return run_batch(
            "DP02",
            build_entities("state", "36"),
            [2018, 2019],
            self.dir,
            "panel",
            ResearchOptions(use_cache=False, concordance_mode=mode, include_codebook=True),
            client=client or FakeClient(),
            variables=variables,
        )

    def test_renumbered_variable_forms_one_column(self):
        res = self._run(["DP02_0016"])
        self.assertTrue(res["success"], res.get("error"))
        panel = pd.read_csv(res["panel_path"])
        size_cols = [c for c in panel.columns if c.endswith("_est") and "average_household_size" in c]
        self.assertEqual(len(size_cols), 1)
        self.assertEqual(panel.sort_values("year")[size_cols[0]].tolist(), [2.63, 2.62])
        concordance = pd.read_csv(res["concordance_path"])
        chosen = concordance[concordance["selected"]]
        self.assertEqual(sorted(chosen["code"]), ["DP02_0015", "DP02_0016"])
        self.assertGreater(len(concordance), len(chosen))  # whole table kept for reference
        codebook = pd.read_csv(res["codebook_path"])
        self.assertIn("Concept_ID", codebook.columns)

    def test_bare_code_resolves_in_latest_year(self):
        plan = plan_extraction(
            "DP02", build_entities("state", "36"), [2018, 2019],
            options=ResearchOptions(use_cache=False), variables=["DP02_0002"], client=FakeClient(),
        )
        self.assertEqual(plan["selected_concepts"], 1)
        self.assertEqual(plan["selection"][0]["resolved_in"], 2019)
        self.assertIn("married couple", plan["selection"][0]["label"])

    def test_reused_code_splits_into_two_columns(self):
        res = self._run(["2018:DP02_0002", "2019:DP02_0002"])
        self.assertTrue(res["success"], res.get("error"))
        panel = pd.read_csv(res["panel_path"])
        pct_cols = [c for c in panel.columns if c.endswith("_pct")]
        self.assertEqual(len(pct_cols), 2)  # family households (2018) vs married couple (2019)
        for col in pct_cols:
            self.assertEqual(panel[col].notna().sum(), 1)

    def test_continuity_flag_blocks(self):
        res = self._run(["DP02_0016"], client=FakeClient(avg_size=(2.6, 5.9)))
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "CONCORDANCE_REVIEW_REQUIRED")
        kinds = {i["kind"] for i in res["plan"]["issues"]}
        self.assertIn(ISSUE_CONTINUITY_FLAG, kinds)
        self.assertTrue(os.path.exists(res["issues_path"]))

    def test_strict_mode_proceeds_and_reports(self):
        res = self._run(["DP02_0016"], mode="strict", client=FakeClient(avg_size=(2.6, 5.9)))
        self.assertTrue(res["success"])
        self.assertTrue(any(i["kind"] == ISSUE_CONTINUITY_FLAG for i in res["concordance_issues"]))

    def test_code_mode_multi_year_requires_opt_in(self):
        res = self._run(["DP02_0016"], mode="code")
        self.assertEqual(res["error_code"], "UNVERIFIED_CONCORDANCE")

    def test_unknown_variable(self):
        res = self._run(["DP02_9999"])
        self.assertEqual(res["error_code"], "INVALID_VARIABLES")

    def test_plan_reports_without_downloading(self):
        client = FakeClient()
        plan = plan_extraction(
            "B27001", build_entities("state", "36"), [2016, 2017],
            options=ResearchOptions(use_cache=False), variables=["B27001_006"], client=client,
        )
        self.assertTrue(plan["would_block"])
        self.assertEqual(plan["block_reason"], "CONCORDANCE_REVIEW_REQUIRED")
        self.assertTrue(any(i["kind"] == ISSUE_RELABELED for i in plan["issues"]))


class TestReviewedOverrides(unittest.TestCase):
    def setUp(self):
        self.xw = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"CENSUS_CROSSWALK_DIR": self.xw})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.xw, ignore_errors=True)

    def _draft(self, decision="split", concept_id=""):
        return draft_override(
            "B27001", "acs5", [(2016, "B27001_006"), (2017, "B27001_006")], decision,
            drafted_by="agent:drafter/session-1",
            rationale="Age band changed from 6-17 to 6-18 in 2017; different measures.",
            source_url="https://www.census.gov/programs-surveys/acs/technical-documentation/table-and-geography-changes.html",
            target_concept_id=concept_id,
        )

    def test_reviewer_must_differ_from_drafter(self):
        draft = self._draft()
        res = record_override(draft["draft_rows"][0], "agent:drafter/session-1", "acs5", "B27001")
        self.assertFalse(res["success"])
        self.assertIn("differ", res["error"])

    def test_agent_reviewer_split_resolves_issue(self):
        draft = self._draft()
        for row in draft["draft_rows"]:
            res = record_override(row, "agent:reviewer/session-2", "acs5", "B27001")
            self.assertTrue(res["success"], res.get("error"))
        self.assertTrue(validate_all_crosswalks()["valid"])
        plan = plan_extraction(
            "B27001", build_entities("state", "36"), [2016, 2017],
            options=ResearchOptions(use_cache=False, continuity_check=False),
            variables=["B27001_006"], client=FakeClient(),
        )
        self.assertFalse(plan["would_block"], plan["issues"])

    def test_merge_requires_census_source_and_continuity(self):
        conc = build_concordance("B27001", "acs5", [2016, 2017])
        target = conc.code_map(2017)["B27001_006"]
        draft = draft_override(
            "B27001", "acs5", [(2016, "B27001_006"), (2017, "B27001_006")], "merge",
            drafted_by="human:drafter", rationale="mechanics test",
            source_url="https://example.org/blog", target_concept_id=target,
        )
        res = record_override(draft["draft_rows"][0], "human:reviewer", "acs5", "B27001")
        self.assertFalse(res["success"])
        self.assertIn("census.gov", res["error"])
        self.assertIn("continuity", res["error"])

    def test_tampered_evidence_rejected(self):
        draft = self._draft()
        path = os.path.join(self.xw, "evidence", f"{draft['evidence_id']}.json")
        with open(path, "a", encoding="utf-8") as f:
            f.write(" ")
        import json

        with open(path, encoding="utf-8") as f:
            evidence = json.load(f)
        evidence["records"][0]["label"] = "edited by hand"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(evidence, f)
        row = dict(draft["draft_rows"][0], decision="merge", concept_id="c_x", continuity_exception="n/a")
        res = record_override(row, "human:reviewer", "acs5", "B27001")
        self.assertFalse(res["success"])
        self.assertIn("evidence", res["error"])


class TestContinuityThresholds(unittest.TestCase):
    def test_small_change_passes_large_change_flags(self):
        conc = build_concordance("DP02", "profile", [2018, 2019])
        cid = conc.code_map(2018)["DP02_0015"]
        ok = {2018: {"DP02_0015E": "2.63", "DP02_0015M": "0.01"}, 2019: {"DP02_0016E": "2.62", "DP02_0016M": "0.01"}}
        bad = {2018: {"DP02_0015E": "2.63", "DP02_0015M": "0.01"}, 2019: {"DP02_0016E": "5.90", "DP02_0016M": "0.01"}}
        self.assertTrue(check_concepts(conc, [cid], ok)[1][cid]["passed"])
        issues, report = check_concepts(conc, [cid], bad)
        self.assertFalse(report[cid]["passed"])
        self.assertEqual(issues[0].kind, ISSUE_CONTINUITY_FLAG)


if __name__ == "__main__":
    unittest.main()


class TestContinuityThresholds(unittest.TestCase):
    def test_thresholds_scale_with_year_gap(self):
        from census.continuity import implausibility_thresholds

        rel1, pp1 = implausibility_thresholds(1)
        self.assertAlmostEqual(rel1, 0.25)
        self.assertAlmostEqual(pp1, 10.0)
        rel4, pp4 = implausibility_thresholds(4)
        self.assertAlmostEqual(rel4, 1.25**4 - 1)
        self.assertAlmostEqual(pp4, 40.0)
        # Validation F9: B19001_017 rose 73% nationally 2019 -> 2023 with an
        # unchanged definition; that must not exceed the 4-year threshold,
        # while the same jump between adjacent releases still would.
        self.assertLess(0.73, rel4)
        self.assertGreater(0.73, rel1)
