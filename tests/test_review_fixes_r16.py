# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the Round 16 code-review findings (offline).

1. The A1 studies' diagonal-Sigma rows call the shipped correct_outcome_error
   estimator (census.spatial.error_model_known_variances), not me_sem_fit.
2. correct_outcome_error refuses frames above CORRECTION_MAX_N areas.
3. Manually placed / downloaded / cached PUMS and block relationship files are
   validated; invalid ones get no sidecar and are moved aside (do not stick).
4. SE == 0 is refused; a singular V at the optimum raises RuntimeError.
5. Lag refits with |rho| >= 1 are failed refits; a main fit with |rho| >= 1
   reports undefined effects.
6. census.http paces per host without sleeping under the lock.
7. W eigenvalues are cached (eigvalsh for symmetric-pattern W); spreg impacts
   are off in our fits; results unchanged.
8. diagnostics['effects_multipliers'] account for islands like the effects table.
9. Retryable streamed responses are closed before the retry.
10. scripts/acs_error_covariance: a zero ratio denominator in a replicate
    invalidates the area for that variable.
"""

import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
import warnings
import zipfile
from unittest import mock

import numpy as np
import pandas as pd

from census import http as H

try:
    import libpysal
    import spreg  # noqa: F401
    HAVE_PYSAL = True
except ImportError:  # pragma: no cover
    HAVE_PYSAL = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PAGE = b"<!DOCTYPE html><html><head><title>Error</title></head><body>Not found</body></html>"


def setUpModule():
    import census.simulation  # noqa: F401  (registers census.simulation_spreg)


def _a1(kind="queen", lam=0.3, cv=1.0):
    from tests.test_spatial_lambda_correction import _a1_draw
    return _a1_draw(kind, lam, cv)


def _frame(g, x, y, s):
    from tests.test_spatial_lambda_correction import _frame as f
    return f(g, x, y, s)


# ---------------------------------------------------------------------------
# 1, 2, 4: correct_outcome_error
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestStudyUsesShippedEstimator(unittest.TestCase):
    def test_diagonal_rows_call_the_tool_function(self):
        from census import simulation_spreg as S, spatial
        cfg, seed, g, w, W, _ = _a1("rook", 0.6)
        calls = []
        real = spatial.error_model_known_variances

        def spy(*a, **k):
            calls.append(a[4] if len(a) > 4 else k.get("method"))
            return real(*a, **k)

        with mock.patch.object(spatial, "error_model_known_variances", spy), \
                mock.patch.object(S, "me_sem_fit", side_effect=AssertionError("diagonal rows must not use me_sem_fit")):
            rows = S._attenuation_cell(cfg, 1, seed, "rook", 0.6, 1.0, None)
        self.assertEqual(sorted(calls), ["ML", "REML"])
        self.assertTrue(all(r["fit_failures"] == 0 for r in rows))

    def test_oracle_still_uses_dense_sigma(self):
        from census import simulation_spreg as S
        cfg, _, g, w, W, _ = _a1("rook", 0.0)
        real = S.me_sem_fit
        seen = []

        def spy(y, X, W_, Sig, **k):
            seen.append(np.count_nonzero(Sig - np.diag(np.diag(Sig))) > 0)
            return real(y, X, W_, Sig, **k)

        with mock.patch.object(S, "me_sem_fit", spy):
            S._attenuation_cell(cfg, 1, 7, "rook", 0.0, 1.0, 0.6)
        self.assertEqual(seen, [True])  # only the oracle, with a dense Sigma

    def test_cell_equals_tool(self):
        from census import simulation_spreg as S
        from census.spatial import spatial_regression
        cfg, seed, g, w, W, draws = _a1("knn", 0.3)
        x, y, s = draws[0]
        rows = S._attenuation_cell(cfg, 1, seed, "knn", 0.3, 1.0, None)
        tab = spatial_regression("y ~ x", _frame(g, x, y, s), w, model="error", correct_outcome_error=True,
                                 se_col="y_se").coefficients.set_index("term")
        reml = next(r for r in rows if "REML" in r["estimator"] and r["term"] == "lambda")
        beta = next(r for r in rows if "REML" in r["estimator"] and r["term"] == "x")
        self.assertEqual(reml["mean_estimate"], tab.loc["lambda", "estimate"])
        self.assertEqual(beta["mean_estimate"], tab.loc["x", "estimate"])


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestCorrectionLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, _, cls.g, cls.w, cls.W, draws = _a1("queen", 0.3)
        cls.x, cls.y, cls.s = draws[0]

    def fit(self, fr, **kw):
        from census.spatial import spatial_regression
        return spatial_regression("y ~ x", fr, self.w, model="error", correct_outcome_error=True, se_col="y_se", **kw)

    def test_size_limit(self):
        from census import spatial
        self.assertEqual(spatial.CORRECTION_MAX_N, 2000)
        self.assertIn("n=2025", spatial.CORRECTION_COST_NOTE)
        with mock.patch.object(spatial, "CORRECTION_MAX_N", 100), \
                mock.patch.object(spatial, "error_model_known_variances",
                                  side_effect=AssertionError("must refuse before fitting")):
            with self.assertRaisesRegex(ValueError, r"limited to 100 areas \(got 144\).*county level"):
                self.fit(_frame(self.g, self.x, self.y, self.s))

    def test_zero_se_refused(self):
        fr = _frame(self.g, self.x, self.y, self.s)
        fr.loc[[3, 7], "y_se"] = 0.0
        with self.assertRaisesRegex(ValueError, r"is 0 in 2 area\(s\)"):
            self.fit(fr)

    def test_singular_optimum_is_runtime_error(self):
        from census import sae_spatial
        from scipy import linalg as sla

        def singular(*a, **k):
            raise sla.LinAlgError("2-th leading minor not positive definite")

        with mock.patch.object(sae_spatial, "_fit", singular):
            with self.assertRaisesRegex(RuntimeError, "singular at the optimum.*not substituted"):
                self.fit(_frame(self.g, self.x, self.y, self.s))


# ---------------------------------------------------------------------------
# 5, 7, 8: lag model
# ---------------------------------------------------------------------------


def _lag_with_reps(seed=3, side=7, island=False):
    from census.simulation import lattice_weights, make_replicates, replicate_long, sar_inverse
    rng = np.random.default_rng(seed)
    w = lattice_weights(side)
    if island:
        nb = dict(w.neighbors)
        nb["island"] = []
        w = libpysal.weights.W(nb, id_order=list(w.id_order) + ["island"], silence_warnings=True)
        w.transform = "r"
    n = w.n
    ids = list(w.id_order)
    x = rng.uniform(0, 10, n)
    y = sar_inverse(w.full()[0], 0.4) @ (5 + 2 * x + rng.normal(0, 2, n))
    reps = replicate_long(ids, ["y"], y[:, None], make_replicates(y[:, None], np.ones((n, 1)), rng))
    return w, pd.DataFrame({"GEOID": ids, "y": y, "x": x}), reps


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestLagRhoBounds(unittest.TestCase):
    def _patched(self, bad_calls):
        from census import spatial
        real = spatial._spreg_fit
        count = [0]

        def fit(*a, **k):
            r, b, se, sp = real(*a, **k)
            i = count[0]
            count[0] += 1
            if i in bad_calls:
                b = b.copy()
                b[-1] = 1.05
                sp = 1.05
            return r, b, se, sp
        return mock.patch.object(spatial, "_spreg_fit", fit)

    def test_explosive_refits_are_failures(self):
        from census.spatial import spatial_regression
        w, df, reps = _lag_with_reps()
        with self._patched({2, 5, 9}):  # call 0 is the main fit
            with self.assertRaisesRegex(RuntimeError, r"3 of 80 replicate refits failed"):
                spatial_regression("y ~ x", df, w, model="lag", replicates=reps, target="conservative")
        with self._patched({2, 5, 9}):
            res = spatial_regression("y ~ x", df, w, model="lag", replicates=reps, target="conservative",
                                     max_failed_replicates=5)
        self.assertEqual(res.diagnostics["replicate_failures"], 3)
        self.assertEqual(res.diagnostics["replicate_refits_rho_out_of_bounds"], 3)
        self.assertIn("replicates_incomplete", res.diagnostics)
        self.assertTrue(any("|rho| >= 1" in n for n in res.notes))
        # no NaN silently zeroed: every effect has a finite sampling SE from the 77 usable refits
        self.assertTrue(np.isfinite(res.effects["se_sampling"]).all())
        self.assertTrue((res.effects["se_total"] > res.effects["se_model"]).all())

    def test_explosive_main_fit(self):
        from census.spatial import spatial_regression
        w, df, _ = _lag_with_reps()
        with self._patched({0}):
            res = spatial_regression("y ~ x", df, w, model="lag")
        self.assertTrue(res.diagnostics["effects_undefined"])
        self.assertTrue(res.notes[0].startswith("rho = 1.0500 is outside (-1, 1)"))
        self.assertTrue(res.effects["estimate"].isna().all())
        self.assertTrue(res.effects["ci_lower"].isna().all())


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestEigenvaluesAndImpacts(unittest.TestCase):
    def test_eigvalsh_equals_eigvals(self):
        from census.simulation_spreg import lattice_w
        from census.spatial import _eigenvalues_dense
        for kind in ("rook", "queen", "knn"):
            W = lattice_w(6, kind).full()[0]
            ref = np.sort_complex(np.linalg.eigvals(W))
            got = np.sort_complex(np.asarray(_eigenvalues_dense(W), complex))
            np.testing.assert_allclose(got, ref, atol=1e-10, err_msg=kind)
        w, _, _ = _lag_with_reps(island=True)
        W = w.full()[0]
        np.testing.assert_allclose(np.sort(np.real(_eigenvalues_dense(W))), np.sort(np.real(np.linalg.eigvals(W))),
                                   atol=1e-10)
        self.assertTrue(np.isrealobj(_eigenvalues_dense(W)))  # symmetric pattern: eigvalsh path

    def test_cached_on_weights(self):
        from census import spatial
        w, _, _ = _lag_with_reps()
        with mock.patch.object(spatial, "_eigenvalues_dense", wraps=spatial._eigenvalues_dense) as dense:
            a = spatial.weights_eigenvalues(w)
            b = spatial.weights_eigenvalues(w)
            self.assertEqual(dense.call_count, 1)
            self.assertIs(a, b)
            w.transform = "b"  # different matrix: recomputed
            spatial.weights_eigenvalues(w)
            self.assertEqual(dense.call_count, 2)
            w.transform = "r"

    def test_results_unchanged(self):
        from census import spatial
        w, df, reps = _lag_with_reps(island=True)
        reps = reps[reps["GEOID"].isin(df["GEOID"])]
        new = {m: spatial.spatial_regression("y ~ x", df, w, model="lag", method=m, replicates=reps)
               for m in ("gm", "ml")}
        old_eig = lambda w_: np.linalg.eigvals(np.asarray(w_.full()[0], float))  # noqa: E731
        with mock.patch.object(spatial, "weights_eigenvalues", old_eig):
            for m in ("gm", "ml"):
                old = spatial.spatial_regression("y ~ x", df, w, model="lag", method=m, replicates=reps,
                                                 spat_impacts="simple")
                for attr in ("coefficients", "effects"):
                    a, b = getattr(new[m], attr), getattr(old, attr)
                    num = a.select_dtypes("number").columns
                    np.testing.assert_allclose(a[num].to_numpy(float), b[num].to_numpy(float), rtol=1e-9,
                                               atol=1e-12, err_msg=f"{m} {attr}")

    def test_spreg_impacts_off(self):
        from census import spatial
        w, df, _ = _lag_with_reps()
        y, X = df[["y"]].to_numpy(), df[["x"]].to_numpy()
        for m in ("gm", "ml"):
            r, *_ = spatial._spreg_fit(y, X, w, "lag", m, ["x"], "y", "full", {})
            self.assertIsNone(getattr(r, "sp_multipliers", None), m)


@unittest.skipUnless(HAVE_PYSAL, "PySAL stack not installed")
class TestIslandMultipliers(unittest.TestCase):
    def test_diagnostics_match_effects_table(self):
        from census.spatial import spatial_regression
        w, df, _ = _lag_with_reps(island=True)
        res = spatial_regression("y ~ x", df, w, model="lag")
        beta = res.coefficients.set_index("term").loc["x", "estimate"]
        eff = res.effects.set_index("effect")["estimate"]
        m = res.diagnostics["effects_multipliers"]
        for e in ("direct", "indirect", "total"):
            self.assertAlmostEqual(m[e] * beta, eff[e], places=10, msg=e)
        rho = res.coefficients.set_index("term").loc["rho", "estimate"]
        s = 1 - 1 / w.n
        self.assertAlmostEqual(m["total"], (1 - s) + s / (1 - rho), places=12)


# ---------------------------------------------------------------------------
# 6, 9: census.http
# ---------------------------------------------------------------------------


class TestPacingThreads(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.dict(os.environ, {"CENSUS_WWW2_MIN_INTERVAL": "1"}),
                  mock.patch.dict(H._last_request, clear=True),
                  mock.patch.object(H, "_monotonic", lambda: 1000.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_sleep_outside_the_lock(self):
        release = threading.Event()
        sleeping = []
        lock = threading.Lock()

        def blocking_sleep(secs):
            with lock:
                sleeping.append((threading.current_thread().name, secs))
            release.wait(10)

        H._last_request["www2.census.gov"] = 1000.0  # a request just went out
        with mock.patch.object(H, "_sleep", blocking_sleep):
            ts = [threading.Thread(target=H._pace, args=("www2.census.gov",), name=f"t{i}") for i in range(2)]
            other = threading.Thread(target=H._pace, args=("www.census.gov",), name="other")
            for t in ts:
                t.start()
            other.start()
            other.join(5)
            self.assertFalse(other.is_alive(), "a wait for one host blocked another host")
            for _ in range(500):  # both same-host threads reach their (blocking) sleep concurrently
                with lock:
                    if len(sleeping) == 2:
                        break
                threading.Event().wait(0.01)
            release.set()
            for t in ts:
                t.join(5)
        self.assertEqual(sorted(s for _, s in sleeping), [1.0, 2.0])  # successive slots, per host
        self.assertEqual(H._last_request["www2.census.gov"], 1002.0)
        self.assertEqual(H._last_request["www.census.gov"], 1000.0)


class _Resp:
    def __init__(self, status, content=b"PK", headers=None):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": "application/zip", **(headers or {})}
        self.closed = False

    def close(self):
        self.closed = True


class TestStreamedRetryClosed(unittest.TestCase):
    def test_closed_before_retry(self):
        first, second = _Resp(503), _Resp(200)
        page = _Resp(200, b"<html><title>Request Rejected</title>Your support ID is: 12</html>",
                     {"Content-Type": "text/html"})
        session = mock.Mock()
        session.get.side_effect = [first, page, second]
        with mock.patch.dict(os.environ, {"CENSUS_WWW2_MIN_INTERVAL": "0", "CENSUS_WWW2_REJECTED_RETRY_DELAY": "0"}), \
                mock.patch.object(H, "_sleep", lambda s: None), mock.patch.object(H, "_jitter", lambda: 1.0):
            out = H.polite_get("https://www2.census.gov/f.zip", user_agent="ua", session=session, stream=True)
        self.assertIs(out, second)
        self.assertTrue(first.closed)
        self.assertTrue(page.closed)
        self.assertFalse(second.closed)


# ---------------------------------------------------------------------------
# 3: manually placed / cached / downloaded files
# ---------------------------------------------------------------------------


def _zip(member, text="a,b\n1,2\n"):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr(member, text)
    return raw.getvalue()


class _Session:
    def __init__(self, items):
        self.items, self.calls = list(items), 0

    def get(self, url, **kw):
        self.calls += 1
        return _Resp(200, self.items.pop(0))


class TestManualFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.dict(os.environ, {"CENSUS_WWW2_MIN_INTERVAL": "0"}),
                  mock.patch.object(H, "_sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_payload_problem(self):
        self.assertIsNone(H.payload_problem(_zip("psam_p56.csv"), ".csv"))
        self.assertIn("no .csv member", H.payload_problem(_zip("readme.txt"), ".csv"))
        self.assertIn("not a zip", H.payload_problem(b"a,b\n1,2\n", ".csv"))
        self.assertIn("HTML", H.payload_problem(HTML_PAGE, ".csv"))
        self.assertIn("HTML", H.payload_problem(b"\xef\xbb\xbf  <html>", None))
        self.assertIn("firewall", H.payload_problem(b"<title>Request Rejected</title>", None))
        self.assertIn("empty", H.payload_problem(b"", None))
        self.assertIsNone(H.payload_problem(b"NAME,TYPE\nAGEP,N\n", None))

    def test_pums_manual_html_refused_and_moved_aside(self):
        from census import pums as P
        url = P.pums_url("56", 2024, "1-year", "person")
        path = os.path.join(self.tmp, os.path.basename(url))
        with open(path, "wb") as f:
            f.write(HTML_PAGE)
        with self.assertRaisesRegex(P.PumsError, r"not usable: it is an HTML.*moved aside to .*\.invalid"):
            P.cached_download(url, cache_dir=self.tmp, session=_Session([]))
        self.assertFalse(os.path.exists(path + ".json"))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".invalid"))
        # does not stick: the next call downloads
        good = _zip("psam_p56.csv")
        s = _Session([good])
        p2, meta = P.cached_download(url, cache_dir=self.tmp, session=s)
        self.assertEqual((s.calls, p2), (1, path))
        self.assertNotIn("source", meta)

    def test_pums_manual_zip_without_csv_refused(self):
        from census import pums as P
        url = P.pums_url("56", 2024, "1-year", "person")
        path = os.path.join(self.tmp, os.path.basename(url))
        with open(path, "wb") as f:
            f.write(_zip("notes.txt"))
        with self.assertRaisesRegex(P.PumsError, "no .csv member"):
            P.read_cache_entry(path, url)
        self.assertFalse(os.path.exists(path + ".json"))

    def test_pums_bad_download_not_cached(self):
        from census import pums as P
        url = P.pums_url("56", 2024, "1-year", "person")
        path = os.path.join(self.tmp, os.path.basename(url))
        with self.assertRaisesRegex(P.PumsError, "not the data file.*Nothing was cached"):
            P.cached_download(url, cache_dir=self.tmp, session=_Session([HTML_PAGE]))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".json"))

    def test_pums_invalid_cached_file_is_replaced(self):
        from census import pums as P
        url = P.pums_url("56", 2024, "1-year", "person")
        path = os.path.join(self.tmp, os.path.basename(url))
        P.write_cache_entry(path, HTML_PAGE, url)  # e.g. cached before validation existed
        s = _Session([_zip("psam_p56.csv")])
        with warnings.catch_warnings(record=True) as rec:
            warnings.simplefilter("always")
            P.cached_download(url, cache_dir=self.tmp, session=s)
        self.assertEqual(s.calls, 1)
        self.assertTrue(any("moved aside" in str(r.message) for r in rec))
        self.assertTrue(zipfile.is_zipfile(path))
        self.assertTrue(os.path.exists(path + ".invalid"))

    def test_block_relationship_files(self):
        from census import block_weights as BW
        with mock.patch.dict(os.environ, {"CENSUS_BLOCK_DIR": self.tmp}):
            url = BW.REL_URL.format(state="11")
            path = os.path.join(self.tmp, os.path.basename(url))
            with open(path, "wb") as f:
                f.write(HTML_PAGE)
            with self.assertRaisesRegex(BW.BlockDataError, r"Manually placed file .* not usable.*moved aside"):
                BW.load_block_relationships("11", session=_Session([]))
            self.assertFalse(os.path.exists(path + ".json"))
            self.assertTrue(os.path.exists(path + ".invalid"))
            with self.assertRaisesRegex(BW.BlockDataError, r"no \.txt member.*Nothing was cached"):
                BW.load_block_relationships("11", session=_Session([_zip("x.csv")]))
            self.assertFalse(os.path.exists(path))

    def test_boundary_manual_file_moved_aside(self):
        from census import boundaries as bd
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"<html>")
        with self.assertRaisesRegex(bd.BoundaryError, "moved aside"):
            bd._read_cached(bad, "https://www2.census.gov/geo/tiger/bad.zip")
        self.assertFalse(os.path.exists(bad + ".json"))
        self.assertTrue(os.path.exists(bad + ".invalid"))


# ---------------------------------------------------------------------------
# 10: scripts/acs_error_covariance.py
# ---------------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("acs_error_covariance",
                                                  os.path.join(ROOT, "scripts", "acs_error_covariance.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("acs_error_covariance", mod)
    spec.loader.exec_module(mod)
    return mod


class TestCovarianceZeroDenominator(unittest.TestCase):
    def test_zero_replicate_denominator_invalidates_area(self):
        from census.variance import REPLICATE_COLUMNS
        mod = _load_script()
        rng = np.random.default_rng(0)
        geoids = ["a", "b", "c"]
        rows = []
        for var, base in (("B17001_002", 20.0), ("B17001_001", 100.0)):
            for g in geoids:
                reps = base + rng.normal(0, 2, 80)
                if var == "B17001_001" and g == "b":
                    reps[4] = 0.0  # replicate 5: no persons in the poverty universe
                rows.append({"GEOID": g, "variable": var, "estimate": base, "controlled": False,
                             **dict(zip(REPLICATE_COLUMNS, reps))})
        frames = {"B17001": pd.DataFrame(rows)}
        est, reps, valid, info = mod.variable_values(frames, "poverty_rate", geoids, 2024)
        self.assertEqual(valid.tolist(), [True, False, True])
        self.assertTrue(np.isnan(reps[1]).all())
        self.assertIn("1 area(s) with a zero replicate denominator excluded", info["note"])
        # never a fabricated replicate ratio of 0 for a valid area
        self.assertTrue((reps[valid] > 0).all())


if __name__ == "__main__":
    unittest.main()
