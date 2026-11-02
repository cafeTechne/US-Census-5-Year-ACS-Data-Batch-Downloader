# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.aggregate (custom geography aggregation)."""

import math
import unittest

import numpy as np
import pandas as pd

from census.aggregate import (
    NonAdditiveError,
    aggregate_geographies,
    aggregate_then_derive,
    non_additive_reason,
    normalize_geoid,
)
from census.derived import proportion, sum_estimates
from census.variance import REPLICATE_COLUMNS, replicate_moe

# Handbook 2020 ch. 8, Tables 8.1 and 8.4 (2015 ACS 1-year, B12001).
HB = pd.DataFrame({
    "GEOID": ["51059", "51013", "51510"],
    "NAME": ["Fairfax County", "Arlington County", "Alexandria city"],
    "B12001_012E": [135173, 43104, 24842],
    "B12001_012M": [3860, 2642, 1957],
    "B12001_011E": [466037, 97360, 67101],
    "B12001_011M": [391, 572, 459],
})
ALL_ONE = {"51059": "NoVa", "51013": "NoVa", "51510": "NoVa"}

METADATA = {
    "B12001_011E": {"label": "Estimate!!Total:!!Female:", "concept": "Sex by Marital Status"},
    "B12001_012E": {"label": "Estimate!!Total:!!Female:!!Never married", "concept": "Sex by Marital Status"},
    "B19013_001E": {
        "label": "Estimate!!Median household income in the past 12 months (in 2022 inflation-adjusted dollars)",
        "concept": "Median Household Income in the Past 12 Months (in 2022 Inflation-Adjusted Dollars)",
    },
    "B19025_001E": {
        "label": "Estimate!!Aggregate household income in the past 12 months (in 2022 inflation-adjusted dollars)",
        "concept": "Aggregate Household Income in the Past 12 Months (in 2022 Inflation-Adjusted Dollars)",
    },
    "B19301_001E": {"label": "Estimate!!Per capita income in the past 12 months", "concept": "Per Capita Income"},
    "B25010_001E": {"label": "Estimate!!Average household size --!!Total:", "concept": "Average Household Size"},
    "B01002_001E": {"label": "Estimate!!Median age --!!Total:", "concept": "Median Age by Sex"},
    "B25070_005E": {
        "label": "Estimate!!Total:!!20.0 to 24.9 percent",
        "concept": "Gross Rent as a Percentage of Household Income in the Past 12 Months",
    },
    "B17002_002E": {"label": "Estimate!!Total:!!Under .50", "concept": "Ratio of Income to Poverty Level in the Past 12 Months"},
    "S2301_C04_001E": {"label": "Estimate!!Unemployment rate!!Population 16 years and over", "concept": "Employment Status"},
    "S1701_C03_001E": {"label": "Estimate!!Percent below poverty level!!Population for whom poverty status is determined", "concept": "Poverty Status"},
    "B08301_001E": {"label": "Estimate!!Total:", "concept": "Means of Transportation to Work"},
}


def replicate_frame(df, variables, seed=0, scale=50.0):
    rng = np.random.default_rng(seed)
    rows = []
    for _, r in df.iterrows():
        for var in variables:
            est = float(r[f"{var}E"])
            rep = est + rng.normal(0, scale, 80)
            row = {"GEOID": r["GEOID"], "NAME": r.get("NAME", ""), "variable": var,
                   "estimate": est, "moe": float(replicate_moe(est, rep))}
            row.update(dict(zip(REPLICATE_COLUMNS, rep)))
            rows.append(row)
    return pd.DataFrame(rows)


class TestApproximate(unittest.TestCase):
    def test_handbook_example1(self):
        out = aggregate_geographies(HB, ALL_ONE, ["B12001_012E"], method="approximate")
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual(row["group_id"], "NoVa")
        self.assertEqual(row["member_count"], 3)
        self.assertEqual(row["members"], "51059;51013;51510")
        self.assertEqual(row["B12001_012E"], 203119)
        self.assertEqual(round(row["B12001_012M"]), 5070)
        self.assertEqual(round(row["B12001_012_SE"]), 3082)
        self.assertEqual(round(row["B12001_012_CV"], 1), 1.5)
        self.assertEqual(row["B12001_012_METHOD"], "sum")

    def test_groups_and_sentinels(self):
        df = pd.DataFrame({
            "GEO_ID": ["1400000US01001020100", "1400000US01001020200", "1400000US01001020300"],
            "B01003_001E": [100, 0, 50], "B01003_001M": [10, -555555555, -222222222],
        })
        mapping = pd.DataFrame({"GEOID": ["01001020100", "01001020200", "01001020300"], "group_id": ["a", "a", "b"]})
        out = aggregate_geographies(df, mapping, metadata=METADATA)
        self.assertEqual(list(out["group_id"]), ["a", "b"])
        self.assertEqual(list(out["B01003_001E"]), [100, 50])
        self.assertEqual(out["B01003_001M"][0], 10)
        self.assertIn("controlled", out["B01003_001_NOTES"][0])
        self.assertTrue(math.isnan(out["B01003_001M"][1]))
        self.assertIn("01001020300 MOE undefined", out["B01003_001_NOTES"][1])

    def test_matches_sum_estimates_and_long(self):
        rng = np.random.default_rng(3)
        n = 12
        df = pd.DataFrame({
            "GEOID": [f"{i:05d}" for i in range(n)],
            "B01003_001E": rng.integers(0, 5000, n), "B01003_001M": rng.integers(1, 400, n),
        })
        df.loc[[0, 3, 6, 7], "B01003_001E"] = 0  # zero estimates in groups g0 and g1
        mapping = {g: f"g{i % 3}" for i, g in enumerate(df["GEOID"])}
        for rule in ("all", "largest"):
            out = aggregate_geographies(df, mapping, output="long", zero_estimate_rule=rule)
            self.assertEqual(list(out.columns[-6:]), ["estimate", "moe", "se", "cv_percent", "method", "notes"])
            self.assertEqual(len(out), 3)
            for _, row in out.iterrows():
                members = row["members"].split(";")
                sub = df[df["GEOID"].isin(members)]
                ref = sum_estimates(list(sub["B01003_001E"]), list(sub["B01003_001M"]),
                                    zero_estimate_rule=rule, names=list(sub["GEOID"]))
                self.assertEqual(row["estimate"], ref.estimate)
                self.assertAlmostEqual(row["moe"], ref.moe)
                self.assertEqual(row["method"], ref.method)
                for note in ref.notes:
                    self.assertIn(note, row["notes"])

    def test_missing_member_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_geographies(HB, {**ALL_ONE, "51600": "NoVa"})

    def test_duplicate_rows_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_geographies(pd.concat([HB, HB.iloc[:1]]), ALL_ONE)

    def test_normalize_geoid(self):
        self.assertEqual(normalize_geoid("0500000US48201"), "48201")
        self.assertEqual(normalize_geoid("05000US48201"), "48201")
        self.assertEqual(normalize_geoid("0100000US"), "US")
        self.assertEqual(normalize_geoid(" 48201 "), "48201")


class TestRefusal(unittest.TestCase):
    def frame(self, code):
        return pd.DataFrame({"GEOID": ["1", "2"], f"{code}E": [1.0, 2.0], f"{code}M": [1.0, 1.0]})

    def test_median_names_census_medians(self):
        with self.assertRaises(NonAdditiveError) as ctx:
            aggregate_geographies(self.frame("B19013_001"), {"1": "g", "2": "g"}, metadata=METADATA)
        self.assertIn("census.medians", str(ctx.exception))
        with self.assertRaises(NonAdditiveError):
            aggregate_geographies(self.frame("B01002_001"), {"1": "g", "2": "g"}, metadata=METADATA)

    def test_other_non_additive(self):
        for code in ("B19301_001", "B25010_001", "S2301_C04_001", "S1701_C03_001"):
            with self.assertRaises(NonAdditiveError, msg=code) as ctx:
                aggregate_geographies(self.frame(code), {"1": "g", "2": "g"}, metadata=METADATA)
            self.assertIn("aggregate_then_derive", str(ctx.exception))

    def test_percent_columns_refused_without_metadata(self):
        df = pd.DataFrame({"GEOID": ["1"], "DP03_0062PE": [5.0], "DP03_0062PM": [1.0]})
        with self.assertRaises(NonAdditiveError):
            aggregate_geographies(df, {"1": "g"}, ["DP03_0062PE"])

    def test_counts_allowed(self):
        for code in ("B19025_001", "B25070_005", "B17002_002", "B08301_001", "B12001_012"):
            self.assertIsNone(non_additive_reason(f"{code}E", METADATA), code)
            aggregate_geographies(self.frame(code), {"1": "g", "2": "g"}, metadata=METADATA)

    def test_assume_additive_override(self):
        out = aggregate_geographies(self.frame("B25010_001"), {"1": "g", "2": "g"},
                                    metadata=METADATA, assume_additive=["B25010_001E"])
        self.assertEqual(out["B25010_001E"][0], 3.0)

    def test_aggregate_then_derive_refuses_median(self):
        df = self.frame("B19013_001")
        with self.assertRaises(NonAdditiveError):
            aggregate_then_derive(df, {"1": "g", "2": "g"},
                                  [{"name": "x", "op": "ratio", "num": "B19013_001E", "den": "B19013_001E"}],
                                  metadata=METADATA)


class TestReplicate(unittest.TestCase):
    def setUp(self):
        self.vars = ["B12001_012", "B12001_011"]
        self.rep = replicate_frame(HB, self.vars)
        self.mapping = {"51059": "A", "51013": "A", "51510": "B"}

    def test_replicate_equals_sum_of_replicates(self):
        out = aggregate_geographies(HB, self.mapping, self.vars, method="replicate", replicate_frame=self.rep)
        for _, row in out.iterrows():
            members = row["members"].split(";")
            for var in self.vars:
                sub = self.rep[(self.rep["variable"] == var) & self.rep["GEOID"].isin(members)]
                est = sub["estimate"].sum()
                reps = sub[REPLICATE_COLUMNS].sum().to_numpy()
                self.assertEqual(row[f"{var}E"], est)
                self.assertAlmostEqual(row[f"{var}M"], float(replicate_moe(est, reps)))
                self.assertEqual(row[f"{var}_METHOD"], "replicate")

    def test_auto_falls_back_when_member_missing(self):
        rep = self.rep[~((self.rep["GEOID"] == "51510") & (self.rep["variable"] == "B12001_011"))]
        out = aggregate_geographies(HB, self.mapping, self.vars, method="auto", replicate_frame=rep)
        self.assertEqual(list(out["B12001_012_METHOD"]), ["replicate", "replicate"])
        self.assertEqual(list(out["B12001_011_METHOD"]), ["sum", "sum"])
        self.assertIn("replicate variance not used", out["B12001_011_NOTES"][0])
        with self.assertRaises(ValueError):
            aggregate_geographies(HB, self.mapping, self.vars, method="replicate", replicate_frame=rep)

    def test_auto_without_frame_is_approximate(self):
        out = aggregate_geographies(HB, self.mapping, self.vars)
        self.assertEqual(set(out["B12001_012_METHOD"]), {"sum"})

    def test_release_mismatch_rejected(self):
        rep = self.rep.copy()
        rep.loc[0, "estimate"] += 100
        with self.assertRaises(ValueError):
            aggregate_geographies(HB, self.mapping, self.vars, method="replicate", replicate_frame=rep)

    def test_replicate_geoid_prefix_and_suffix(self):
        rep = self.rep.copy()
        rep["GEOID"] = "0500000US" + rep["GEOID"]
        rep["variable"] = rep["variable"] + "E"
        out = aggregate_geographies(HB, self.mapping, self.vars, method="replicate", replicate_frame=rep)
        self.assertEqual(set(out["B12001_012_METHOD"]), {"replicate"})


class TestAggregateThenDerive(unittest.TestCase):
    spec = [{"name": "nm_pct", "op": "proportion", "num": "B12001_012E", "den": "B12001_011E", "as_percent": True}]

    def test_approximate_handbook_example3(self):
        out = aggregate_then_derive(HB, ALL_ONE, self.spec, method="approximate")
        self.assertEqual(round(out["nm_pct_estimate"][0], 1), 32.2)
        self.assertEqual(round(out["nm_pct_moe"][0], 1), 0.8)
        self.assertEqual(out["nm_pct_method"][0], "proportion")
        ref = proportion(203119, out["B12001_012M"][0], 630498, out["B12001_011M"][0], as_percent=True)
        self.assertAlmostEqual(out["nm_pct_moe"][0], ref.moe)

    def test_auto_op_uses_metadata(self):
        spec = [{"name": "nm", "op": "auto", "num": "B12001_012E", "den": "B12001_011E"}]
        out = aggregate_then_derive(HB, ALL_ONE, spec, metadata=METADATA)
        self.assertEqual(out["nm_method"][0], "proportion")

    def test_replicate_exact_ratio(self):
        rep = replicate_frame(HB, ["B12001_012", "B12001_011"], seed=5)
        mapping = {"51059": "A", "51013": "A", "51510": "B"}
        out = aggregate_then_derive(HB, mapping, self.spec, replicate_frame=rep)
        for _, row in out.iterrows():
            members = row["members"].split(";")
            num = rep[(rep["variable"] == "B12001_012") & rep["GEOID"].isin(members)]
            den = rep[(rep["variable"] == "B12001_011") & rep["GEOID"].isin(members)]
            est = 100 * num["estimate"].sum() / den["estimate"].sum()
            reps = 100 * num[REPLICATE_COLUMNS].sum().to_numpy() / den[REPLICATE_COLUMNS].sum().to_numpy()
            self.assertAlmostEqual(row["nm_pct_estimate"], est)
            self.assertAlmostEqual(row["nm_pct_moe"], float(replicate_moe(est, reps)))
            self.assertEqual(row["nm_pct_method"], "replicate")

    def test_chained_entries(self):
        spec = [
            {"name": "nm", "op": "proportion", "num": "B12001_012E", "den": "B12001_011E"},
            {"name": "nm_pct", "op": "scale", "num": "nm", "k": 100},
        ]
        out = aggregate_then_derive(HB, ALL_ONE, spec, method="approximate")
        self.assertAlmostEqual(out["nm_pct_estimate"][0], 100 * out["nm_estimate"][0])
        self.assertAlmostEqual(out["nm_pct_moe"][0], 100 * out["nm_moe"][0])


if __name__ == "__main__":
    unittest.main()
