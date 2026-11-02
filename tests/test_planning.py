# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import numpy as np
import pandas as pd
import pytest

from census.planning import aggregation_forecast, minimum_detectable_difference, reliability_profile


def test_mdd_standard_formula():
    # MOE 1.645 -> SE 1; SE_diff = sqrt(2); (1.959964 + 0.841621) * sqrt(2) at 95%/80%.
    r = minimum_detectable_difference(1.645, confidence=0.95, power=0.80)
    assert r["mdd"] == pytest.approx((1.959964 + 0.841621) * math.sqrt(2), abs=1e-5)
    r90 = minimum_detectable_difference(1.645, 1.645)
    assert r90["mdd"] == pytest.approx((1.644854 + 0.841621) * math.sqrt(2), abs=1e-5)


def test_mdd_overlap_years():
    base = minimum_detectable_difference(3.0, 4.0)["mdd"]
    r = minimum_detectable_difference(3.0, 4.0, year1=2019, year2=2021)
    assert r["overlap_years"] == 3
    assert r["mdd"] == pytest.approx(base * math.sqrt(1 - 3 / 5))
    assert minimum_detectable_difference(3.0, 4.0, year1=2013, year2=2018)["mdd"] == pytest.approx(base)


def test_mdd_arrays_and_sentinels():
    r = minimum_detectable_difference(np.array([1.0, -222222222.0]), np.array([1.0, 1.0]))
    assert np.isfinite(r["mdd"][0]) and np.isnan(r["mdd"][1])
    assert r["notes"]


def test_aggregation_forecast_sqrt_k():
    out = aggregation_forecast([100.0] * 4, [32.9] * 4, group_sizes=(1, 4, 9), target_cv=9)
    cv1 = 20.0
    assert out["cv"].tolist() == pytest.approx([cv1, cv1 / 2, cv1 / 3], rel=1e-9)
    assert out["meets_target"].tolist() == [False, False, True]
    corr = aggregation_forecast([100.0], [32.9], group_sizes=(4,), correlation=1.0)
    assert corr["cv"].iloc[0] == pytest.approx(cv1)


def test_reliability_profile_analysis_and_raw():
    f = pd.DataFrame({"pov": [10.0, 10.0, 10.0, 0.0], "pov_se": [1.0, 2.0, 4.0, 1.0]})
    p = reliability_profile(f).iloc[0]
    assert p["variable"] == "pov" and p["n_cv_defined"] == 3
    assert p["cv_median"] == pytest.approx(20.0)
    assert (p["share_high"], p["share_moderate"], p["share_low"], p["share_undefined"]) == (0.25, 0.25, 0.25, 0.25)
    raw = pd.DataFrame({"B01003_001E": [1000.0, 50.0], "B01003_001M": [16.45, -555555555]})
    q = reliability_profile(raw).iloc[0]
    # controlled estimate (-555555555 MOE) has SE 0 -> CV 0, not undefined (census.stats.moe_to_se)
    assert q["variable"] == "B01003_001" and q["cv_median"] == pytest.approx(0.5)
    assert q["share_undefined"] == 0.0 and q["share_high"] == 1.0
    with pytest.raises(ValueError):
        reliability_profile(pd.DataFrame({"a": [1]}))
