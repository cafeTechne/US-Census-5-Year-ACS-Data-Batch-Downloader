# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.medians: bracket parsing, interpolation, MOEs, and
agreement with Census-published medians (frozen fixtures, offline).

Fixtures (tests/fixtures/medians/, provenance in SOURCE.json): ACS 2019-2023
5-year values for all 21 New Jersey counties from the Census Data API
(B19001 + B19013, B25063 + B25064) and the matching rows of the county-level
Variance Replicate Estimate files for B19001 and B25063.
"""

import json
import math
import os
import unittest

import numpy as np
import pandas as pd

from census.medians import (
    MOE_METHOD_DESIGN_FACTOR,
    MOE_METHOD_GVF_FITTED,
    MOE_METHOD_REPLICATE,
    PUMS_5YR_FACTOR,
    brackets_from_variables,
    distribution_brackets,
    fitted_gvf_factor,
    median_confidence_bounds,
    median_for_group,
    median_for_rows,
    median_from_distribution,
    median_moe_approximate,
    median_moe_replicates,
    parse_bracket_label,
    se_50_percent,
)
from census.variance import (
    METHOD_MEDIAN_INTERPOLATED,
    REPLICATE_COLUMNS,
    Z90,
    replicate_moe,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "medians")
INF = math.inf


def load_api_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        rows = json.load(f)
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df.insert(0, "GEOID", df["state"] + df["county"])
    return df


def load_vre_fixture(table):
    """VRE CSV -> long replicate frame (census.replicate contract)."""
    raw = pd.read_csv(
        os.path.join(FIXTURES, f"vre_2023_050_{table}_nj.csv.gz"), dtype={"GEOID": str}
    )
    raw = raw[raw["GEOID"].notna()].copy()
    out = pd.DataFrame(
        {
            "GEOID": raw["GEOID"].str.split("US").str[1],
            "NAME": raw["NAME"],
            "variable": table + "_" + raw["ORDER"].astype(int).map("{:03d}".format),
            "estimate": raw["ESTIMATE"].astype(float),
            "moe": raw["MOE"].astype(float),
        }
    )
    for i, col in enumerate(REPLICATE_COLUMNS, start=1):
        out[col] = raw[f"Var_Rep{i}"].astype(float).to_numpy()
    return out.reset_index(drop=True)


def income_frame():
    return load_api_fixture("nj_counties_2023_b19001_b19013.json")


def rent_frame():
    est = load_api_fixture("nj_counties_2023_b25063_estimates_b25064.json")
    moe = load_api_fixture("nj_counties_2023_b25063_moes.json")
    return est.merge(moe.drop(columns=["NAME", "state", "county"]), on="GEOID")


def bounds(brackets):
    return [b[1] for b in brackets], [b[2] for b in brackets]


class TestBracketLabels(unittest.TestCase):
    def test_parse_labels(self):
        cases = {
            "Estimate!!Total:!!Less than $10,000": (0.0, 10000.0),
            "Estimate!!Total:!!$10,000 to $14,999": (10000.0, 15000.0),
            "Estimate!!Total:!!$200,000 or more": (200000.0, INF),
            "Estimate!!Total:!!With cash rent:!!Less than $100": (0.0, 100.0),
            "Estimate!!Total:!!With cash rent:!!$3,500 or more": (3500.0, INF),
            "Estimate!!Total:!!$2,000,000 or more": (2000000.0, INF),
            "Estimate!!Total:!!$1,000 to $1,249": (1000.0, 1250.0),
            "Estimate!!Total:!!10.0 to 14.9 percent": (10.0, 15.0),
            "Estimate!!Total:!!Less than 10.0 percent": (0.0, 10.0),
            "Estimate!!Total:!!50.0 percent or more": (50.0, INF),
        }
        for label, expected in cases.items():
            self.assertEqual(parse_bracket_label(label), expected, label)

    def test_non_bracket_labels(self):
        for label in (
            "Estimate!!Total:",
            "Estimate!!Total:!!With cash rent:",
            "Estimate!!Total:!!No cash rent",
            "Estimate!!Median gross rent",
            "Estimate!!Total:!!Not computed",
        ):
            self.assertIsNone(parse_bracket_label(label), label)

    def test_b19001_2023(self):
        br = distribution_brackets("B19001", 2023)
        self.assertEqual(len(br), 16)
        self.assertEqual(br[0][:3], ("B19001_002", 0.0, 10000.0))
        self.assertEqual(br[-1][:3], ("B19001_017", 200000.0, INF))
        self.assertNotIn("B19001_001", [b[0] for b in br])

    def test_b25063_excludes_total_subtotal_and_no_cash_rent(self):
        br = distribution_brackets("B25063", 2023)
        codes = [b[0] for b in br]
        self.assertEqual(len(br), 24)
        for excluded in ("B25063_001", "B25063_002", "B25063_027"):
            self.assertNotIn(excluded, codes)
        self.assertEqual(br[0][1:3], (0.0, 100.0))
        self.assertEqual(br[-1][1:4], (3500.0, INF, "$3,500 or more"))

    def test_brackets_follow_the_release(self):
        # Gross rent and home value brackets were extended in the 2015 release.
        self.assertEqual(distribution_brackets("B25063", 2014)[-1][1], 2000.0)
        self.assertEqual(distribution_brackets("B25063", 2015)[-1][1], 3500.0)
        self.assertEqual(distribution_brackets("B25075", 2014)[-1][1], 1000000.0)
        self.assertEqual(distribution_brackets("B25075", 2023)[-1][1], 2000000.0)

    def test_contiguous(self):
        for table in ("B19001", "B25063", "B25075"):
            br = distribution_brackets(table, 2023)
            for a, b in zip(br, br[1:]):
                self.assertEqual(a[2], b[1])

    def test_multiple_parents_need_selection(self):
        variables = {
            "X_001E": {"label": "Estimate!!Total:"},
            "X_002E": {"label": "Estimate!!Total:!!Owner:!!Less than $10"},
            "X_003E": {"label": "Estimate!!Total:!!Owner:!!$10 or more"},
            "X_004E": {"label": "Estimate!!Total:!!Renter:!!Less than $10"},
            "X_005E": {"label": "Estimate!!Total:!!Renter:!!$10 or more"},
        }
        with self.assertRaises(ValueError):
            brackets_from_variables(variables)
        br = brackets_from_variables(variables, parent="renter")
        self.assertEqual([b[0] for b in br], ["X_004", "X_005"])

    def test_gap_is_refused(self):
        variables = {
            "X_002E": {"label": "Estimate!!Total:!!Less than $10"},
            "X_003E": {"label": "Estimate!!Total:!!$20 to $29"},
        }
        with self.assertRaises(ValueError):
            brackets_from_variables(variables)


class TestInterpolation(unittest.TestCase):
    LO = [0, 10, 20]
    HI = [10, 20, INF]

    def test_hand_computed_linear(self):
        # total 40, half 20; 10 below the 10-20 bracket holding 20 -> 10 + 10/20*10
        r = median_from_distribution(self.LO, self.HI, [10, 20, 10])
        self.assertAlmostEqual(r.estimate, 15.0)
        self.assertEqual(r.method, METHOD_MEDIAN_INTERPOLATED)
        self.assertTrue(math.isnan(r.moe))
        self.assertEqual(r.components["interpolation"], "linear")
        self.assertEqual(r.components["bracket"], [10.0, 20.0])

    def test_boundary_and_empty_brackets(self):
        # Exactly half below 10 and the next bracket non-empty -> the boundary value.
        r = median_from_distribution([0, 10, 20, 30], [10, 20, 30, INF], [5, 5, 0, 0])
        self.assertAlmostEqual(r.estimate, 10.0)
        # Exactly half below 10 with 10-19 empty -> midpoint of the empty gap, as
        # Census publishes it (2019 B25064, Sterling County TX: $850 from a gap at $800-$899).
        r = median_from_distribution([0, 10, 20, 30], [10, 20, 30, INF], [5, 0, 5, 0])
        self.assertAlmostEqual(r.estimate, 15.0)
        r = median_from_distribution([0, 700, 800, 900], [700, 800, 900, INF], [10, 17, 0, 27])
        self.assertAlmostEqual(r.estimate, 850.0)
        # An empty bracket is skipped.
        r = median_from_distribution([0, 10, 20, 30], [10, 20, 30, INF], [4, 0, 6, 0])
        self.assertAlmostEqual(r.estimate, 20 + (5 - 4) / 6 * 10)

    def test_pums_worked_example_median(self):
        # PUMS Accuracy of the Data 2019-2023, Example 3 (household income, MA).
        lo, hi, counts = pums_example()
        r = median_from_distribution(lo, hi, counts)
        half = sum(counts) / 2
        self.assertAlmostEqual(r.estimate, 60000 + (half - 1139123) / 231284 * 15000, places=6)

    def test_pareto(self):
        lo, hi, counts = [0, 10, 20], [10, 20, INF], [30, 40, 30]
        r = median_from_distribution(lo, hi, counts, method="pareto")
        s_lo, s_hi = 0.7, 0.3
        theta = math.log(s_lo / s_hi) / math.log(20 / 10)
        self.assertAlmostEqual(r.estimate, 10 * (s_lo / 0.5) ** (1 / theta))
        self.assertEqual(r.components["interpolation"], "pareto")
        # Pareto needs a positive lower bound; bracket starting at 0 falls back to linear.
        r = median_from_distribution(lo, hi, [60, 20, 20], method="pareto")
        self.assertAlmostEqual(r.estimate, 50 / 60 * 10)

    def test_auto_is_linear(self):
        a = median_from_distribution(self.LO, self.HI, [10, 20, 10], method="auto")
        self.assertEqual(a.components["interpolation"], "linear")
        with self.assertRaises(ValueError):
            median_from_distribution(self.LO, self.HI, [10, 20, 10], method="spline")

    def test_top_coded(self):
        r = median_from_distribution(self.LO, self.HI, [1, 1, 10])
        self.assertEqual(r.estimate, 20.0)
        self.assertTrue(r.components["top_coded"])
        self.assertIn("20+", r.notes[0])
        for res in (
            median_moe_approximate(self.LO, self.HI, [1, 1, 10], count_moes=[1, 1, 2]),
            median_moe_replicates(self.LO, self.HI, [1, 1, 10], np.tile([1, 1, 10], (80, 1))),
        ):
            self.assertEqual(res.estimate, 20.0)
            self.assertTrue(math.isnan(res.moe))

    def test_bottom_coded(self):
        r = median_from_distribution(self.LO, self.HI, [10, 2, 2])
        self.assertAlmostEqual(r.estimate, 7 / 10 * 10)  # lower bound 0 assumed
        self.assertTrue(r.components["bottom_coded"])
        self.assertIn("10-", r.notes[0])
        res = median_moe_approximate(self.LO, self.HI, [10, 2, 2], count_moes=[3, 2, 2])
        self.assertTrue(math.isnan(res.moe))

    def test_zero_total_and_sentinels(self):
        r = median_from_distribution(self.LO, self.HI, [0, 0, 0])
        self.assertTrue(math.isnan(r.estimate))
        self.assertIn("zero", r.notes[0])
        r = median_from_distribution(self.LO, self.HI, [10, -666666666, 10])
        self.assertTrue(math.isnan(r.estimate))
        self.assertIn("sentinel", r.notes[0])

    def test_bad_bounds(self):
        with self.assertRaises(ValueError):
            median_from_distribution([0, 10], [10], [1, 1])
        with self.assertRaises(ValueError):
            median_from_distribution([0, 10, 20], [10, INF, 30], [1, 1, 1])
        with self.assertRaises(ValueError):
            median_from_distribution([0, 10], [10, 20], [1, 1, 1])


def pums_example():
    """Distribution from PUMS Accuracy of the Data 2019-2023, Example 3."""
    labels = [
        ("Less than $10,000", 153739), ("$10,000 to $14,999", 130852),
        ("$15,000 to $19,999", 113550), ("$20,000 to $24,999", 105230),
        ("$25,000 to $29,999", 95824), ("$30,000 to $34,999", 102957),
        ("$35,000 to $39,999", 90972), ("$40,000 to $44,999", 87818),
        ("$45,000 to $49,999", 83793), ("$50,000 to $59,999", 174388),
        ("$60,000 to $74,999", 231284), ("$75,000 to $99,999", 318700),
        ("$100,000 to $124,999", 244795), ("$125,000 to $149,999", 178104),
        ("$150,000 to $199,999", 202797), ("$200,000 or more", 234913),
    ]
    parsed = [parse_bracket_label(lab) for lab, _ in labels]
    return [p[0] for p in parsed], [p[1] for p in parsed], [c for _, c in labels]


class TestApproximateMOE(unittest.TestCase):
    def test_pums_formula_reproduces_worked_examples(self):
        # Example 3: B = 2,549,716, DF = 1.5 -> SE(50%) = 0.20 percentage points.
        se50, method = se_50_percent(2549716, design_factor=1.5)
        self.assertEqual(round(se50, 2), 0.20)
        self.assertEqual(method, MOE_METHOD_DESIGN_FACTOR)
        # Example 2 (percent formula, same constant 19): 22.4190%, B = 3,039,780,
        # DF = 1.5 -> SE 0.1564.
        p, b = 22.4190, 3039780
        self.assertAlmostEqual(1.5 * math.sqrt(PUMS_5YR_FACTOR / b * p * (100 - p)), 0.1564, places=4)

    def test_pums_worked_example_bounds(self):
        # The document interpolates on its rounded cumulative percents
        # (C1 = 44.68, C2 = 53.75) with SE(50%) rounded to 0.20.
        cum = [6.03, 11.16, 15.62, 19.74, 23.50, 27.54, 31.11, 34.55, 37.84,
               44.68, 53.75, 66.25, 75.85, 82.83, 90.79, 100.00]
        pct = np.diff([0.0] + cum)
        lo, hi, _ = pums_example()
        lower, upper = median_confidence_bounds(lo, hi, pct, 0.20)
        self.assertAlmostEqual(lower, 68467.48, delta=0.01)
        self.assertAlmostEqual(upper, 69129.00, delta=0.01)
        self.assertAlmostEqual((upper - lower) / 2, 330.76, delta=0.01)

    def test_design_factor_moe(self):
        lo, hi, counts = pums_example()
        r = median_moe_approximate(lo, hi, counts, design_factor=1.5)
        se50 = 1.5 * math.sqrt(19 / sum(counts) * 2500)
        c1, c2 = 1139123 / sum(counts) * 100, 1370407 / sum(counts) * 100
        expected_se = (2 * se50) / (c2 - c1) * 15000 / 2
        self.assertAlmostEqual(r.se, expected_se, places=6)
        self.assertAlmostEqual(r.moe, Z90 * expected_se, places=6)
        self.assertEqual(r.components["moe_method"], MOE_METHOD_DESIGN_FACTOR)

    def test_fitted_gvf_factor(self):
        y = np.array([100.0, 300.0, 600.0])
        g = 4.0
        se = np.sqrt(g * y * (1 - y / y.sum()))
        self.assertAlmostEqual(fitted_gvf_factor(y, se * Z90), g)
        # zero-count brackets and sentinels are ignored
        self.assertAlmostEqual(
            fitted_gvf_factor(np.append(y, 0), np.append(se * Z90, -555555555)), g
        )

    def test_requires_moes_or_design_factor(self):
        with self.assertRaises(ValueError):
            median_moe_approximate([0, 10, 20], [10, 20, INF], [10, 20, 10])


class TestReplicateMOE(unittest.TestCase):
    LO, HI = [0, 10, 20], [10, 20, INF]

    def test_matches_variance_module(self):
        rng = np.random.default_rng(7)
        counts = np.array([100.0, 200.0, 100.0])
        reps = counts + rng.normal(0, 10, size=(80, 3))
        r = median_moe_replicates(self.LO, self.HI, counts, reps)
        rep_medians = [median_from_distribution(self.LO, self.HI, row).estimate for row in reps]
        self.assertAlmostEqual(r.moe, float(replicate_moe(r.estimate, rep_medians)))
        self.assertEqual(r.components["moe_method"], MOE_METHOD_REPLICATE)
        # transposed input (brackets x 80) is accepted
        r2 = median_moe_replicates(self.LO, self.HI, counts, reps.T)
        self.assertAlmostEqual(r.moe, r2.moe)

    def test_identical_replicates_give_zero(self):
        counts = [100, 200, 100]
        r = median_moe_replicates(self.LO, self.HI, counts, np.tile(counts, (80, 1)))
        self.assertEqual(r.moe, 0.0)

    def test_bad_shape(self):
        with self.assertRaises(ValueError):
            median_moe_replicates(self.LO, self.HI, [1, 2, 1], np.ones((79, 3)))


class TestGroupAndRows(unittest.TestCase):
    def setUp(self):
        self.df = rent_frame()
        self.brackets = distribution_brackets("B25063", 2023)

    def test_group_sums_counts(self):
        geoids = ["34001", "34009", "34011"]
        r = median_for_group(self.df, "B25063", 2023, geoids=geoids)
        sub = self.df[self.df["GEOID"].isin(geoids)]
        counts = [sub[b[0] + "E"].astype(float).sum() for b in self.brackets]
        lo, hi = bounds(self.brackets)
        self.assertAlmostEqual(r.estimate, median_from_distribution(lo, hi, counts).estimate)
        self.assertEqual(r.components["n_geographies"], 3)
        self.assertEqual(r.components["moe_method"], MOE_METHOD_GVF_FITTED)
        self.assertGreater(r.moe, 0)
        self.assertIn("summed over 3", r.notes[0])

    def test_group_replicate_moe(self):
        rf = load_vre_fixture("B25063")
        r = median_for_group(self.df, "B25063", 2023, geoids=["34001", "34009"], replicate_frame=rf)
        self.assertEqual(r.components["moe_method"], MOE_METHOD_REPLICATE)
        self.assertGreater(r.moe, 0)

    def test_group_without_moes(self):
        cols = ["GEOID"] + [b[0] + "E" for b in self.brackets]
        r = median_for_group(self.df[cols], "B25063", brackets=self.brackets)
        self.assertTrue(math.isnan(r.moe))
        self.assertEqual(r.components["n_geographies"], 21)

    def test_group_errors(self):
        with self.assertRaises(KeyError):
            median_for_group(self.df, "B25063", 2023, geoids=["99999"])
        with self.assertRaises(ValueError):
            median_for_group(self.df, "B25063")  # neither year nor brackets

    def test_rows_match_scalar(self):
        out = median_for_rows(self.df, "B25063", 2023)
        self.assertEqual(list(out.index), list(self.df.index))
        lo, hi = bounds(self.brackets)
        for i in (0, 7, 20):
            row = self.df.iloc[i]
            counts = [float(row[b[0] + "E"]) for b in self.brackets]
            moes = [float(row[b[0] + "M"]) for b in self.brackets]
            scalar = median_moe_approximate(lo, hi, counts, count_moes=moes)
            self.assertAlmostEqual(out["estimate"].iloc[i], scalar.estimate)
            self.assertAlmostEqual(out["moe"].iloc[i], scalar.moe)
        self.assertTrue((out["method"] == METHOD_MEDIAN_INTERPOLATED).all())

    def test_rows_flags(self):
        df = pd.DataFrame(
            {"A_001E": [1, 10, 0], "A_002E": [1, 2, 0], "A_003E": [10, 2, 0]}
        )
        br = [("A_001", 0.0, 10.0, "Less than 10"), ("A_002", 10.0, 20.0, "10 to 19"),
              ("A_003", 20.0, INF, "20 or more")]
        out = median_for_rows(df, "A", brackets=br, approximate_moe=False)
        self.assertEqual(list(out["top_coded"]), [True, False, False])
        self.assertEqual(list(out["bottom_coded"]), [False, True, False])
        self.assertTrue(math.isnan(out["estimate"].iloc[2]))


class TestAgreementWithPublishedMedians(unittest.TestCase):
    """Validation check F5 (offline): rebuild published county medians."""

    def test_gross_rent_reproduces_b25064(self):
        # B25063 uses the same 24 brackets as the gross-rent standard distribution,
        # so linear interpolation should reproduce B25064 to rounding.
        df = rent_frame()
        out = median_for_rows(df, "B25063", 2023)
        published = df["B25064_001E"].astype(float)
        diff = (out["estimate"] - published).abs()
        self.assertLessEqual(diff.max(), 1.0)
        self.assertLess(((out["estimate"] - published) / published).abs().max(), 0.001)

    def test_household_income_b19001_vs_b19013(self):
        # B19013 is computed from a $2,500-interval distribution; B19001 is coarser.
        # Observed (2023, 21 NJ counties): median |error| 0.51%, max 2.84% (Cumberland,
        # median inside the $60,000-$74,999 bracket). Nationwide (3,220 counties):
        # median 0.61%, 95th percentile 3.1%.
        df = income_frame()
        out = median_for_rows(df, "B19001", 2023)
        published = df["B19013_001E"].astype(float)
        rel = ((out["estimate"] - published) / published).abs()
        self.assertLess(rel.median(), 0.01)
        self.assertLess(rel.max(), 0.03)
        self.assertGreaterEqual((rel <= 0.02).mean(), 0.9)

    def test_approximate_moe_tracks_published(self):
        for df, table, pub in (
            (income_frame(), "B19001", "B19013_001M"),
            (rent_frame(), "B25063", "B25064_001M"),
        ):
            out = median_for_rows(df, table, 2023)
            ratio = out["moe"] / df[pub].astype(float)
            self.assertTrue(ratio.between(0.75, 1.6).all(), (table, ratio.describe()))
            self.assertTrue(0.9 <= ratio.median() <= 1.3, (table, ratio.median()))

    def test_replicate_moe_reproduces_published(self):
        cases = (
            (income_frame(), "B19001", "B19013_001", (0.75, 1.3)),
            (rent_frame(), "B25063", "B25064_001", (0.95, 1.05)),
        )
        for df, table, pub, (low, high) in cases:
            rf = load_vre_fixture(table)
            lo, hi = bounds(distribution_brackets(table, 2023))
            codes = [b[0] for b in distribution_brackets(table, 2023)]
            ratios = []
            for _, row in df.iterrows():
                sub = rf[rf["GEOID"] == row["GEOID"]].set_index("variable").loc[codes]
                # VRE estimates are the same published estimates as the API
                api = [float(row[c + "E"]) for c in codes]
                self.assertEqual(sub["estimate"].tolist(), api)
                r = median_moe_replicates(lo, hi, api, sub[REPLICATE_COLUMNS].to_numpy().T)
                ratios.append(r.moe / float(row[pub + "M"]))
            ratios = np.array(ratios)
            self.assertTrue(((ratios >= low) & (ratios <= high)).all(), (table, ratios))


if __name__ == "__main__":
    unittest.main()
