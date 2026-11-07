# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.did: published reference, analytic special cases, overlap enforcement, replicates.

Reference: Card, D. & Krueger, A.B. (1994), "Minimum Wages and Employment: A
Case Study of the Fast-Food Industry in New Jersey and Pennsylvania", AER
84(4), 772-793, Table 3 (mean FTE employment per store, SEs in parentheses;
also reproduced in Angrist & Pischke 2009, Mostly Harmless Econometrics,
Table 5.2.1):

                         PA            NJ            NJ - PA
    1. before         23.33 (1.35)  20.44 (0.51)  -2.89 (1.44)
    2. after          21.17 (0.94)  21.03 (0.52)  -0.14 (1.07)
    3. change         -2.16 (1.25)   0.59 (0.54)   2.76 (1.36)

Rows 1-2 combine independent state samples, so their difference SEs are
sqrt(se_PA^2 + se_NJ^2); row 3 uses the change estimates (the same stores
in both waves, so the change SEs are not functions of the level SEs).
"""

import math
import unittest

import numpy as np
import pandas as pd

from census.did import did_from_changes, difference_in_differences
from census.simulation import make_replicates, replicate_long, run_study, designs_config

CK = {"PA": {"before": (23.33, 1.35), "after": (21.17, 0.94), "change": (-2.16, 1.25)},
      "NJ": {"before": (20.44, 0.51), "after": (21.03, 0.52), "change": (0.59, 0.54)}}


class TestCardKrueger(unittest.TestCase):
    def test_did_from_published_changes(self):
        r = did_from_changes([CK["PA"]["change"][0], CK["NJ"]["change"][0]],
                             [CK["PA"]["change"][1], CK["NJ"]["change"][1]], [0, 1])
        # 0.59 - (-2.16) = 2.75 from the rounded table entries; published 2.76 from unrounded means
        self.assertAlmostEqual(r["estimate"], 2.76, delta=0.011)
        self.assertAlmostEqual(r["se"], 1.36, delta=0.005)

    def test_level_differences_independent_samples(self):
        for wave, (est, se) in (("before", (-2.89, 1.44)), ("after", (-0.14, 1.07))):
            r = did_from_changes([0.0, CK["NJ"][wave][0] - CK["PA"][wave][0]],
                                 [CK["PA"][wave][1], CK["NJ"][wave][1]], [0, 1])
            self.assertAlmostEqual(r["estimate"], est, delta=0.006)
            self.assertAlmostEqual(r["se"], se, delta=0.005)


def _panel(n=120, years=(2008, 2013, 2018, 2023), delta=3.0, sigma_u=1.0, seed=0, se_scale=2.0):
    rng = np.random.default_rng(seed)
    T = (np.arange(n) < n // 2).astype(int)
    alpha = rng.normal(50, 10, n)
    rows = []
    for k, yr in enumerate(years):
        se = rng.uniform(0.5, 1.5, n) * se_scale
        th = alpha + 2 * k + delta * T * (yr >= 2018) + sigma_u * rng.standard_normal(n)
        y = th + se * rng.standard_normal(n)
        rows.append(pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(n)], "Year": yr, "y": y, "y_se": se,
                                  "treated": T, "theta": th}))
    return pd.concat(rows, ignore_index=True)


def _coef(res, term, col="estimate"):
    return float(res.coefficients.set_index("term").loc[term, col])


class TestAnalyticCases(unittest.TestCase):
    def test_two_period_equal_variance_equals_difference_of_mean_changes(self):
        f = _panel(years=(2013, 2018))
        f["y_se"] = 1.0  # equal variances: GLS = OLS on changes = difference of mean changes
        r = difference_in_differences(f, "y", "treated", 2018)
        w = f.pivot(index="GEOID", columns="Year", values="y")
        ch = w[2018] - w[2013]
        tr = f.drop_duplicates("GEOID").set_index("GEOID")["treated"]
        expect = ch[tr == 1].mean() - ch[tr == 0].mean()
        self.assertAlmostEqual(_coef(r, "did"), expect, places=8)
        # SE: residual variance of changes pooled across groups (REML)
        resid = np.concatenate([ch[tr == 1] - ch[tr == 1].mean(), ch[tr == 0] - ch[tr == 0].mean()])
        s2 = resid @ resid / (len(ch) - 2)
        se = math.sqrt(s2 * (1 / (tr == 1).sum() + 1 / (tr == 0).sum()))
        self.assertAlmostEqual(_coef(r, "did", "se_model"), se, delta=1e-4 * se)
        self.assertIsNone(r.diagnostics["parallel_trends"])

    def test_noise_free_recovers_delta(self):
        f = _panel(sigma_u=0.0, se_scale=1e-6)
        f["y"] = f["theta"]
        r = difference_in_differences(f, "y", "treated", 2018)
        self.assertAlmostEqual(_coef(r, "did"), 3.0, places=4)

    def test_event_design_and_parallel_trends(self):
        f = _panel(years=(2003, 2008, 2013, 2018, 2023))
        r = difference_in_differences(f, "y", "treated", 2018, design="event")
        terms = set(r.coefficients["term"])
        self.assertTrue({"lead[2003]", "lead[2008]", "lag[2018]", "lag[2023]"} <= terms)
        self.assertNotIn("lead[2013]", terms)  # reference period
        pt = r.diagnostics["parallel_trends"]
        self.assertEqual(pt["df"], 2)
        self.assertGreater(pt["p_value"], 0.001)
        # violated trends are flagged
        g = f.copy()
        g.loc[(g.treated == 1), "y"] += (g.loc[g.treated == 1, "Year"] - 2013) * 1.0
        r2 = difference_in_differences(g, "y", "treated", 2018)
        self.assertLess(r2.diagnostics["parallel_trends"]["p_value"], 0.01)
        self.assertTrue(any("PARALLEL TRENDS" in n for n in r2.notes))

    def test_fewer_than_three_pre_periods_no_diagnostic(self):
        r = difference_in_differences(_panel(), "y", "treated", 2018)
        self.assertIsNone(r.diagnostics["parallel_trends"])
        self.assertTrue(any(">= 3" in n for n in r.notes))


class TestValidation(unittest.TestCase):
    def test_overlapping_periods_refused(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            difference_in_differences(_panel(years=(2010, 2013, 2018, 2023)), "y", "treated", 2018)

    def test_one_year_periods_allowed(self):
        r = difference_in_differences(_panel(years=(2016, 2017, 2018, 2019)), "y", "treated", 2018, period_years=1)
        self.assertEqual(r.model_type, "did_two_group")

    def test_varying_treatment_refused(self):
        f = _panel()
        f.loc[0, "treated"] = 1 - f.loc[0, "treated"]
        with self.assertRaises(ValueError):
            difference_in_differences(f, "y", "treated", 2018)

    def test_these_areas_needs_replicates(self):
        with self.assertRaises(ValueError):
            difference_in_differences(_panel(), "y", "treated", 2018, target="these_areas")


class TestReplicates(unittest.TestCase):
    def test_replicate_se_close_to_analytic(self):
        f = _panel(n=200, seed=2)
        rng = np.random.default_rng(9)
        parts = []
        for yr, sub in f.groupby("Year"):
            y = sub["y"].to_numpy()[:, None]
            parts.append(replicate_long(sub["GEOID"], ["y"], y,
                                        make_replicates(y, sub[["y_se"]].to_numpy(), rng)).assign(Year=yr))
        r = difference_in_differences(f, "y", "treated", 2018, replicates=pd.concat(parts), target="these_areas")
        se_rep = _coef(r, "did", "se_sampling")
        self.assertLess(abs(se_rep / r.diagnostics["se_sampling_analytic"]["did"] - 1), 0.35)
        self.assertLess(se_rep, _coef(r, "did", "se_model"))
        self.assertIn("sampling_replicate", r.uncertainty_sources)
        self.assertEqual(r.method, "gls_reml_replicate_full")
        g = difference_in_differences(f, "y", "treated", 2018, replicates=pd.concat(parts), target="these_areas",
                                      replicate_refit="gls")
        self.assertEqual(g.method, "gls_reml_replicate_gls")
        with self.assertRaises(ValueError):
            difference_in_differences(f, "y", "treated", 2018, replicate_refit="bogus")


class TestSimulationSmoke(unittest.TestCase):
    def test_did_coverage_fast(self):
        df = run_study(designs_config(fast=True), studies=["did_two_group"])
        self.assertGreater(float(df.iloc[0]["coverage"]), 0.75)


if __name__ == "__main__":
    unittest.main()


class TestTwoGroupDenseReference(unittest.TestCase):
    """Independent two-group DiD reference from hand-built differences (PLAN_ROUND_12 A1).

    Model (census.did docstring): y_it = alpha_i + lambda_t + delta w_it + u_it
    + e_it, u ~ N(0, A), e ~ N(0, SE_it^2) independent. A is estimated by
    REML in production; this test reads the reported A and (a) rebuilds the
    GLS answer at that A from first principles and (b) checks that A is a
    stationary point of an independently written REML likelihood. The
    expected values never call census.did/census.multilevel helpers:

    * Differences: for unit i with levels covariance Sigma_i = A I +
      diag(SE_i^2), the contrast C_i (rows e_t - e_ref) gives d_i = C_i y_i
      with covariance C_i Sigma_i C_i' (this carries the shared reference-
      period sampling error without writing it as a formula).
    * Design rows per difference (t vs ref): period[t] dummies and
      did = treated_i * 1[t >= treatment_period].
    * Cross-check: GLS on LEVELS with explicit unit dummies and Sigma =
      blockdiag(Sigma_i) must give the same period/DiD coefficients.
    Edge cases: any pre-treatment reference gives the same answer (it would
    not if the reference-period error term were dropped); a post-treatment
    reference raises ValueError.
    """

    YEARS = [2008, 2013, 2018]  # non-overlapping 5-year releases
    TREAT = 2018
    # unit: (treated, levels by year, SEs by year)
    UNITS = {
        "u1": (1, [10.0, 13.5, 15.8], [0.8, 0.5, 1.4]),
        "u2": (1, [14.0, 13.2, 20.1], [1.6, 0.9, 0.6]),
        "u3": (1, [9.0, 12.8, 12.0], [0.4, 1.2, 0.7]),
        "u4": (0, [12.0, 11.1, 14.9], [1.1, 0.6, 1.0]),
        "u5": (0, [8.5, 12.4, 10.2], [0.7, 1.5, 0.5]),
        "u6": (0, [15.0, 14.2, 18.3], [0.9, 0.4, 1.3]),
    }

    def _frame(self):
        rows = [{"GEOID": u, "Year": t, "treated": tr, "y": ys[k], "y_se": ss[k]}
                for u, (tr, ys, ss) in self.UNITS.items() for k, t in enumerate(self.YEARS)]
        return pd.DataFrame(rows)

    def _differenced(self, A, ref):
        others = [t for t in self.YEARS if t != ref]
        r = self.YEARS.index(ref)
        blocks_d, blocks_X, blocks_V, blocks_S = [], [], [], []
        for tr, ys, ss in self.UNITS.values():
            C = np.zeros((len(others), len(self.YEARS)))
            for k, t in enumerate(others):
                C[k, self.YEARS.index(t)] = 1.0
                C[k, r] = -1.0
            D = np.diag(np.square(ss))
            blocks_d.append(C @ np.array(ys))
            blocks_V.append(C @ (A * np.eye(len(self.YEARS)) + D) @ C.T)
            blocks_S.append(C @ D @ C.T)
            blocks_X.append(np.array([[1.0 * (t == p) for p in others] + [tr * (t >= self.TREAT)] for t in others]))
        m = len(others)
        n = m * len(blocks_d)
        V = np.zeros((n, n))
        S = np.zeros((n, n))
        for i, (Vb, Sb) in enumerate(zip(blocks_V, blocks_S)):
            V[i * m:(i + 1) * m, i * m:(i + 1) * m] = Vb
            S[i * m:(i + 1) * m, i * m:(i + 1) * m] = Sb
        return np.concatenate(blocks_d), np.vstack(blocks_X), V, S, [f"period[{p}]" for p in others] + ["did"]

    @staticmethod
    def _gls(d, X, V):
        Vi = np.linalg.inv(V)
        Q = np.linalg.inv(X.T @ Vi @ X)
        return Q @ X.T @ Vi @ d, Q, Vi

    def _levels(self, A):
        """GLS on levels with unit dummies (no differencing): period/did columns only returned."""
        ys, X, var = [], [], []
        units = list(self.UNITS)
        for i, (tr, yl, ss) in enumerate(self.UNITS.values()):
            for k, t in enumerate(self.YEARS):
                unit_d = [1.0 * (i == j) for j in range(len(units))]
                period_d = [1.0 * (t == p) for p in self.YEARS[:-1]]  # last period is the omitted category
                X.append(unit_d + period_d + [tr * (t >= self.TREAT)])
                ys.append(yl[k])
                var.append(A + ss[k] ** 2)
        b, Q, _ = self._gls(np.array(ys), np.array(X), np.diag(var))
        return b[-1], Q[-1, -1]

    def _reml(self, A, d, X):
        V = self._differenced(A, self.YEARS[1])[2]
        b, Q, Vi = self._gls(d, X, V)
        r = d - X @ b
        return -0.5 * (np.linalg.slogdet(V)[1] - np.linalg.slogdet(Q)[1] + r @ Vi @ r)

    def test_matches_hand_built_differences_and_levels(self):
        res = difference_in_differences(self._frame(), "y", "treated", self.TREAT, se="y_se")
        A = res.diagnostics["A_area_period_var"]
        # Data chosen so REML A is interior (about 2.75, comparable to SE^2), so
        # both A and the known variances shape V.
        self.assertGreater(A, 0.5)
        d, X, V, S, names = self._differenced(A, ref=2013)
        beta, Q, Vi = self._gls(d, X, V)
        tab = res.coefficients.set_index("term")
        # Tolerance: dense problem of 12 rows with cond(V) < 1e2 (asserted),
        # so double-precision paths agree to ~1e-13; rtol 1e-9 is ample and
        # far tighter than the effect of any error checked below (>1e-2).
        self.assertLess(np.linalg.cond(V), 1e2)
        np.testing.assert_allclose(tab.loc[names, "estimate"].to_numpy(), beta, rtol=1e-9)
        np.testing.assert_allclose(tab.loc[names, "se_model"].to_numpy(), np.sqrt(np.diag(Q)), rtol=1e-9)
        Ssamp = Q @ X.T @ Vi @ S @ Vi @ X @ Q
        np.testing.assert_allclose([res.diagnostics["se_sampling_analytic"][n] for n in names],
                                   np.sqrt(np.diag(Ssamp)), rtol=1e-9)
        # Levels + unit dummies: the fixed-effects GLS the differencing claims to equal.
        did_lv, var_lv = self._levels(A)
        self.assertAlmostEqual(tab.loc["did", "estimate"], did_lv, delta=1e-9 * abs(did_lv))
        self.assertAlmostEqual(tab.loc["did", "se_model"], math.sqrt(var_lv), delta=1e-9)
        # A is a REML stationary point of the independent likelihood: a 1%
        # move either way does not increase it (production maximizer
        # tolerance is far below 1%; the loglik is smooth in A).
        ll = self._reml(A, d, X)
        for f in (0.99, 1.01):
            self.assertGreaterEqual(ll + 1e-10, self._reml(A * f, d, X))
        # Sensitivity: dropping the shared reference-period sampling error
        # (V without the off-diagonal D_ref terms) visibly changes the answer:
        # measured did 1.640 -> 1.551 and Var(did) 3.64 -> 4.18.
        V_wrong = V.copy()
        for i in range(0, len(d), 2):
            V_wrong[i, i + 1] = V_wrong[i + 1, i] = A
        bw, Qw, _ = self._gls(d, X, V_wrong)
        self.assertGreater(abs(bw[-1] - beta[-1]) + abs(Qw[-1, -1] - Q[-1, -1]), 1e-2)

    def test_reference_period_edge_cases(self):
        frame = self._frame()
        default = difference_in_differences(frame, "y", "treated", self.TREAT, se="y_se")
        early = difference_in_differences(frame, "y", "treated", self.TREAT, se="y_se", reference=2008)
        self.assertEqual(early.diagnostics["reference_period"], 2008)
        # Any pre-treatment reference is a full-rank contrast of the same
        # levels model, so the DiD and its SE are invariant (REML A too).
        a, b = (r.coefficients.set_index("term").loc["did"] for r in (default, early))
        # rtol 1e-6: A comes from a 1-D optimizer run separately for each
        # reference, so agreement is limited by its tolerance, not algebra.
        self.assertAlmostEqual(a["estimate"], b["estimate"], delta=1e-6 * abs(a["estimate"]))
        self.assertAlmostEqual(a["se_model"], b["se_model"], delta=1e-6 * a["se_model"])
        # Independent check at reference 2008 too.
        d, X, V, _, names = self._differenced(early.diagnostics["A_area_period_var"], ref=2008)
        beta = self._gls(d, X, V)[0]
        np.testing.assert_allclose(early.coefficients.set_index("term").loc[names, "estimate"], beta, rtol=1e-9)
        with self.assertRaisesRegex(ValueError, "pre-treatment reference"):
            difference_in_differences(frame, "y", "treated", self.TREAT, se="y_se", reference=2018)
