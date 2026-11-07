# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.pums: offline tests on a real PUMS subset and synthetic known answers."""

import hashlib
import io
import json
import os
import zipfile

import numpy as np
import pandas as pd
import pytest

from census import boundaries as B
from census import pums as P
from census.models import INTERVAL_TARGETS

FX = os.path.join(os.path.dirname(__file__), "fixtures", "pums")
SUBSET = os.path.join(FX, "psam_p56_2024_1yr_subset.csv.gz")
DICT = os.path.join(FX, "PUMS_Data_Dictionary_2024_excerpt.csv")
REPS = [f"PWGTP{i}" for i in range(1, 81)]


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture(scope="module")
def real():
    return P.read_pums_csv(SUBSET, "person")


def synthetic(n=400, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"x": rng.normal(10, 2, n), "g": rng.choice(["a", "b"], n),
                       "flag": rng.integers(0, 2, n)})
    df["y"] = 1.5 + 0.8 * df["x"] + rng.normal(0, 1, n)
    w = rng.integers(5, 50, n).astype(float)
    df["PWGTP"] = w
    for r in range(1, 81):
        df[f"PWGTP{r}"] = np.maximum(w * rng.uniform(0.3, 1.7, n), 0) - (r % 7 == 0) * 2
    return df


def hand_se(full, reps):
    return np.sqrt(4 / 80 * np.sum((np.asarray(reps) - full) ** 2))


# --------------------------------------------------------------------------- fixture and URLs

def test_fixture_sidecar_hashes():
    with open(SUBSET + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    for name, digest in meta["files"].items():
        assert _sha(os.path.join(FX, name)) == digest
    assert meta["source_url"] == P.pums_url("WY", 2024, "1-year", "person")
    assert os.path.getsize(SUBSET) < 1_000_000


def test_urls():
    base = "https://www2.census.gov/programs-surveys/acs/data/pums/2024/"
    assert P.pums_url("56", 2024, "1-year", "person") == base + "1-Year/csv_pwy.zip"
    assert P.pums_url("Wyoming", 2024, "5-year", "housing") == base + "5-Year/csv_hwy.zip"
    assert P.pums_url("ca", 2023, "acs5", "p").endswith("2023/5-Year/csv_pca.zip")
    dd = "https://www2.census.gov/programs-surveys/acs/tech_docs/pums/data_dict/"
    assert P.data_dictionary_url(2024, "1-year") == dd + "PUMS_Data_Dictionary_2024.csv"
    assert P.data_dictionary_url(2024, "5-year") == dd + "PUMS_Data_Dictionary_2020-2024.csv"
    with pytest.raises(ValueError):
        P.data_dictionary_url(2015, "1-year")


def test_2020_one_year_is_opt_in(tmp_path):
    with pytest.raises(ValueError, match="experimental"):
        P.fetch_pums("56", 2020, "1-year", cache_dir=str(tmp_path))


def test_fetch_uses_checksummed_cache(tmp_path):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("psam_p56.csv", pd.read_csv(SUBSET, dtype=str).head(50).to_csv(index=False))
    data = raw.getvalue()

    class Resp:
        status_code, content, headers = 200, data, {"Content-Type": "application/zip"}

    class Session:
        calls = 0

        def get(self, url, **kw):
            Session.calls += 1
            return Resp()

    s = Session()
    a = P.fetch_pums("56", 2024, "1-year", columns=["AGEP"], cache_dir=str(tmp_path), session=s)
    b = P.fetch_pums("56", 2024, "1-year", columns=["AGEP"], cache_dir=str(tmp_path), session=s)
    assert Session.calls == 1 and len(a) == len(b) == 50
    assert a.attrs["sha256"] == hashlib.sha256(data).hexdigest()
    assert list(a.columns[:4]) == ["SERIALNO", "SPORDER", "STATE", "PUMA"]
    assert "AGEP" in a and "PWGTP80" in a and "SCHL" not in a
    with pytest.raises(KeyError, match="not in this PUMS file"):
        P.fetch_pums("56", 2024, "1-year", columns=["NOPE"], cache_dir=str(tmp_path), session=s)


def test_read_types(real):
    # text (object in pandas 2, "str" in pandas 3), never numeric: leading zeros kept
    assert pd.api.types.is_string_dtype(real["PUMA"]) and real["PUMA"].str.len().eq(5).all()
    assert real["SERIALNO"].str.startswith("2024").all()
    assert pd.api.types.is_numeric_dtype(real["AGEP"])
    assert real["PWGTP"].dtype == np.float32 and len(real) == 1281


def test_data_dictionary_labels():
    with open(DICT, "rb") as f:
        d = P.parse_data_dictionary(f.read(), "ACS 2024 1-year PUMS")
    assert d.describe("SEX") == "Sex"
    assert d.labels("SEX") == {"1": "Male", "2": "Female"}
    lab = d.label(pd.Series([1, 2, np.nan], name="SEX"))
    assert lab.tolist()[:2] == ["Male", "Female"]
    ten = d.label(pd.Series([np.nan, 3.0], name="TEN"))
    assert ten.iloc[0].startswith("N/A") and ten.iloc[1] == "Rented"
    assert d.label(pd.Series([1], name="SCHL")).iloc[0] == "No schooling completed"
    assert d.label(pd.Series([42], name="AGEP")).iloc[0] is not None  # range label


# --------------------------------------------------------------------------- tabulation known answers

def test_count_matches_hand_formula():
    df = synthetic()
    t = P.tabulate(df, "count", by="g", return_replicates=True)
    for _, row in t.iterrows():
        sub = df[df.g == row.g]
        full, reps = sub.PWGTP.sum(), sub[REPS].sum().to_numpy()
        assert row.estimate == pytest.approx(full)
        assert row.se == pytest.approx(hand_se(full, reps))
        assert row.moe == pytest.approx(1.645 * row.se)
        assert row.n_unweighted == len(sub)
        assert row.rep_17 == pytest.approx(sub.PWGTP17.sum())


def test_mean_proportion_ratio_median():
    df = synthetic()
    w = df.PWGTP
    m = P.tabulate(df, "mean", "x").iloc[0]
    assert m.estimate == pytest.approx((w * df.x).sum() / w.sum())
    reps = [(df[c] * df.x).sum() / df[c].sum() for c in REPS]
    assert m.se == pytest.approx(hand_se(m.estimate, reps))
    p = P.tabulate(df, "proportion", "x > 11").iloc[0]
    assert p.estimate == pytest.approx((w * (df.x > 11)).sum() / w.sum())
    r = P.tabulate(df, "ratio", "y", denominator="x").iloc[0]
    assert r.estimate == pytest.approx((w * df.y).sum() / (w * df.x).sum())
    md = P.tabulate(df, "median", "x").iloc[0]
    o = np.argsort(df.x.to_numpy())
    cw = np.cumsum(w.to_numpy()[o])
    assert md.estimate == pytest.approx(df.x.to_numpy()[o][np.searchsorted(cw, cw[-1] / 2)])
    assert np.isfinite(md.se) and md.se > 0


def test_weighted_median_exact_half_and_bins():
    assert P.weighted_median(np.array([1.0, 2, 3, 4]), np.ones(4))[0] == 2.5
    assert P.weighted_median(np.array([1.0, 2, 3]), np.array([1.0, 1, 5]))[0] == 3
    # Census bracket method: 10 in [0,10), 30 in [10,20): median at 50% of 40 -> 10 + (20-10)/30*10
    x = np.array([5.0] * 10 + [15.0] * 30)
    assert P.binned_median(x, np.ones(40), [0, 10, 20])[0] == pytest.approx(10 + 10 / 3)
    with pytest.raises(ValueError):
        P.binned_median(np.array([25.0]), np.ones(1), [0, 10, 20])


def test_flags_not_suppressed():
    df = synthetic(n=60)
    t = P.tabulate(df.head(20), "count")
    assert bool(t.flag_small_n.iloc[0]) and "n<30" in t.reliability_flags.iloc[0] and np.isfinite(t.estimate.iloc[0])
    big = P.tabulate(df, "count").iloc[0]
    assert not big.flag_small_n
    d = df.copy()
    d["z"] = 0.0
    d.loc[0, "z"] = 1.0
    t2 = P.tabulate(d, "proportion", "z").iloc[0]
    assert t2.flag_high_cv and np.isfinite(t2.estimate)


def test_where_and_missing():
    df = synthetic()
    df.loc[:9, "x"] = np.nan
    t = P.tabulate(df, "mean", "x", where="g == 'a'").iloc[0]
    keep = (df.g == "a") & df.x.notna()
    assert t.n_unweighted == keep.sum()


def test_crosstab_row_shares():
    df = synthetic()
    ct = P.crosstab(df, "g", "flag", normalize="row")
    for g, sub in ct.groupby("g"):
        assert sub.estimate.sum() == pytest.approx(1.0)
    one = ct[(ct.g == "a") & (ct.flag == 1)].iloc[0]
    a = df[df.g == "a"]
    reps = [a.loc[a.flag == 1, c].sum() / a[c].sum() for c in REPS]
    assert one.se == pytest.approx(hand_se(one.estimate, reps))


def test_real_subset_tabulation(real):
    tot = P.tabulate(real, "count").iloc[0]
    assert tot.estimate == pytest.approx(real.PWGTP.astype(float).sum())
    by_sex = P.tabulate(real, "count", by="SEX")
    assert by_sex.estimate.sum() == pytest.approx(tot.estimate)
    inc = P.tabulate(real, "median", "PINCP", where="AGEP >= 15")
    assert inc.n_unweighted.iloc[0] == real.query("AGEP >= 15").PINCP.notna().sum()
    assert inc.se.iloc[0] > 0


# --------------------------------------------------------------------------- regression

def test_regression_replicate_se_matches_hand_formula():
    df = synthetic()
    res = P.pums_regression("y ~ x", df)
    X = np.column_stack([np.ones(len(df)), df.x])

    def wls(w):
        return np.linalg.solve(X.T @ (X * w[:, None]), X.T @ (w * df.y.to_numpy()))

    b0 = wls(df.PWGTP.to_numpy())
    B_ = np.vstack([wls(df[c].to_numpy()) for c in REPS])
    se = np.sqrt(4 / 80 * ((B_ - b0) ** 2).sum(axis=0))
    c = res.coefficients.set_index("term")
    assert c.loc["x", "estimate"] == pytest.approx(b0[1])
    assert c.loc["x", "se_sampling"] == pytest.approx(se[1])
    assert res.diagnostics["interval_target"] == "these_areas"
    assert c.loc["x", "ci_upper"] - c.loc["x", "estimate"] == pytest.approx(1.6448536 * se[1], rel=1e-6)
    for t in INTERVAL_TARGETS:
        P.pums_regression("y ~ x", df, target=t)


def test_regression_logit_matches_statsmodels_point():
    import statsmodels.api as sm

    df = synthetic()
    res = P.pums_regression("flag ~ x", df, model="logit")
    ref = sm.GLM(df.flag, sm.add_constant(df.x), family=sm.families.Binomial(),
                 freq_weights=df.PWGTP).fit()
    assert res.coefficients.estimate.to_numpy() == pytest.approx(ref.params.to_numpy(), rel=1e-6)


def test_regression_coverage_simulation():
    """Replicate SEs from independent-perturbation replicates cover the true slope."""
    rng = np.random.default_rng(42)
    hits, n_sim = 0, 200
    for s in range(n_sim):
        n = 300
        x = rng.normal(0, 1, n)
        y = 2.0 + 0.5 * x + rng.normal(0, 1, n)
        df = pd.DataFrame({"x": x, "y": y, "PWGTP": np.ones(n)})
        # Replicate factors 1 + 0.5 h, h = +/-1 independent: E[4/80 sum (b_r - b)^2]
        # = 4 * 0.25 * sum(influence^2), the linearization variance of b.
        h = rng.choice([-1.0, 1.0], size=(n, 80))
        reps = 1.0 + 0.5 * h
        for r in range(80):
            df[f"PWGTP{r + 1}"] = reps[:, r]
        c = P.pums_regression("y ~ x", df).coefficients.set_index("term").loc["x"]
        hits += c.ci_lower <= 0.5 <= c.ci_upper
    assert 0.83 <= hits / n_sim <= 0.96


# --------------------------------------------------------------------------- PUMA geography

def test_puma_vintage_and_geoid(real):
    assert P.puma_vintage(2024, "1-year") == 2020
    assert P.puma_vintage(2021, "5-year") == 2010
    assert P.puma_vintage(2016, "5-year") == 2010
    with pytest.raises(ValueError):
        P.puma_vintage(2014, "5-year")
    g = P.puma_geoid(real)
    assert g.str.len().eq(7).all() and g.str.startswith("56").all()


def test_puma_boundary_level():
    assert B.boundary_vintage(2024, "puma") == 2020
    assert B.boundary_vintage(2021, "puma") == 2019
    assert B.boundary_url("puma", 2020, "56").endswith("GENZ2020/shp/cb_2020_56_puma20_500k.zip")
    assert B.boundary_url("puma", 2024, "56", "tiger").endswith("TIGER2024/PUMA20/tl_2024_56_puma20.zip")
    assert B.boundary_url("puma", 2022, "56", "tiger").endswith("TIGER2022/PUMA/tl_2022_56_puma20.zip")
    assert B.boundary_url("puma", 2021, "56", "tiger").endswith("TIGER2021/PUMA/tl_2021_56_puma10.zip")
    with pytest.raises(ValueError):
        B.boundary_url("puma", 2022, "56")
    B.check_boundary_vintage(2023, 2020, "puma")
    with pytest.raises(B.BoundaryVintageError):
        B.check_boundary_vintage(2023, 2019, "puma")
