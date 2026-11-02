# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import numpy as np
import pytest

from census.stats import z_test
from census.trends import linear_trend_test, overlap_design_check, suggest_nonoverlapping


def test_design_check_annual_panel():
    d = overlap_design_check(range(2014, 2024))
    assert d["overlapping"] and d["n_releases"] == 10
    assert d["effective_independent_periods"] == pytest.approx(14 / 5)
    assert d["suggested_nonoverlapping"] == [2018, 2023]
    assert suggest_nonoverlapping([2013, 2014, 2018, 2023]) == [2013, 2018, 2023]
    clean = overlap_design_check([2013, 2018, 2023])
    assert not clean["overlapping"] and clean["effective_independent_periods"] == 3


def test_trend_matches_wls_closed_form():
    t = np.array([2013, 2018, 2023.0])
    y = np.array([10.0, 12.0, 15.0])
    se = np.array([1.0, 0.5, 2.0])
    r = linear_trend_test(t, y, ses=se)
    w = 1 / se ** 2
    tb = np.sum(w * t) / np.sum(w)
    slope = np.sum(w * (t - tb) * y) / np.sum(w * (t - tb) ** 2)
    assert r["slope"] == pytest.approx(slope)
    assert r["se_slope"] == pytest.approx(1 / math.sqrt(np.sum(w * (t - tb) ** 2)))
    assert r["lack_of_fit_df"] == 1 and not r["overlapping_periods"]


def test_uninformative_middle_reduces_to_z_test():
    # Endpoints only informative (huge middle SE): slope*10 ~ difference z-test.
    r = linear_trend_test([2013, 2018, 2023], [10.0, 0.0, 14.0], moes=[1.645, 1e6, 3.29])
    z = z_test(14.0, 3.29, 10.0, 1.645)
    assert r["z_statistic"] == pytest.approx(z["z_statistic"], rel=1e-6)


def test_overlap_refused_by_default_and_flagged_otherwise():
    yrs, y, se = [2019, 2021, 2023], [10.0, 11.0, 12.0], [1.0, 1.0, 1.0]
    with pytest.raises(ValueError, match="non-overlapping"):
        linear_trend_test(yrs, y, ses=se)
    a = linear_trend_test(yrs, y, ses=se, overlap="census_approx")
    i = linear_trend_test(yrs, y, ses=se, overlap="independent")
    assert a["overlapping_periods"] and a["notes"] and i["notes"]
    assert a["se_slope"] < i["se_slope"]  # positive covariance makes differences more precise
    assert a["slope"] == pytest.approx(0.5)


def test_input_validation():
    with pytest.raises(ValueError, match=">= 3"):
        linear_trend_test([2013, 2018], [1, 2], ses=[1, 1])
    with pytest.raises(ValueError):
        linear_trend_test([2013, 2018, 2023], [1, 2, np.nan], ses=[1, 1, 1])
