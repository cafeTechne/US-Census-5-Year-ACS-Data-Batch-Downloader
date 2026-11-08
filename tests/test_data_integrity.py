# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load-time checksum verification of bundled price tables (Round 13 D1, risk R1).

Tampering tests work on temporary copies of the bundled directories, made to
look bundled by patching ``bundled_price_index_dir``/``bundled_price_parity_dir``;
the real data files are never modified.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from census import data_integrity, inflation, rpp
from census.data_integrity import BundledDataIntegrityError
from census.inflation import C_CPI_U, CPI_U, R_CPI_U_RS, load_price_index, price_index_info

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_DIR = os.path.join(ROOT, "data", "price_indexes")
PARITY_DIR = os.path.join(ROOT, "data", "price_parities")
NO_CUSTOM_DIRS = {"CENSUS_PRICE_INDEX_DIR": "", "CENSUS_PRICE_PARITY_DIR": ""}


def _script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tamper(path):
    """Change one index value in a copy (same row count, still parseable)."""
    with open(path, "rb") as f:
        raw = f.read()
    with open(path, "wb") as f:
        f.write(raw.replace(b"\n2010,", b"\n2010,9", 1) if b"\n2010," in raw else raw + b"\n")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, NO_CUSTOM_DIRS)
        self.env.start()
        self._clear()

    def tearDown(self):
        self.env.stop()
        self._clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear():
        load_price_index.cache_clear()
        rpp.clear_cache()

    def _copy(self, src):
        dst = os.path.join(self.tmp, os.path.basename(src))
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("source"))
        return dst


class TestBundledVerification(_Base):
    def test_bundled_tables_load_and_match(self):
        for index in (CPI_U, C_CPI_U, R_CPI_U_RS):
            self.assertTrue(load_price_index(index))
            self.assertNotIn("custom_table", price_index_info(index))
        self.assertFalse(rpp.load_price_parities().empty)
        self.assertTrue(rpp.load_pce_price_index())
        self.assertNotIn("custom_tables", rpp.price_parity_info())
        self.assertNotIn("custom_table", rpp.place_parity("us", 2020))

    def test_tampered_price_index_raises(self):
        copy = self._copy(INDEX_DIR)
        path = os.path.join(copy, "cpi_u.csv")
        expected = _sha(path)
        _tamper(path)
        with patch.object(inflation, "bundled_price_index_dir", return_value=copy):
            with self.assertRaises(BundledDataIntegrityError) as ctx:
                load_price_index(CPI_U)
            # Untouched tables in the same directory still load.
            self.assertTrue(load_price_index(R_CPI_U_RS))
        msg = str(ctx.exception)
        for part in (path, expected, _sha(path), "modified or corrupted", "scripts/update_price_indexes.py"):
            self.assertIn(part, msg)

    def test_missing_sidecar_checksum_raises_for_bundled(self):
        copy = self._copy(INDEX_DIR)
        sidecar = os.path.join(copy, "c_cpi_u.json")
        with open(sidecar, encoding="utf-8") as f:
            meta = json.load(f)
        del meta["csv_sha256"]
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        with patch.object(inflation, "bundled_price_index_dir", return_value=copy):
            with self.assertRaises(BundledDataIntegrityError):
                load_price_index(C_CPI_U)

    def test_tampered_price_parities_raise(self):
        copy = self._copy(PARITY_DIR)
        for name, loader in (("pce_price_index.csv", rpp.load_pce_price_index), ("rpp.csv", rpp.load_price_parities)):
            path = os.path.join(copy, name)
            _tamper(path)
            with patch.object(rpp, "bundled_price_parity_dir", return_value=copy):
                rpp.clear_cache()
                with self.assertRaises(BundledDataIntegrityError) as ctx:
                    loader()
            self.assertIn(name, str(ctx.exception))
            self.assertIn("scripts/update_price_parities.py", str(ctx.exception))

    def test_verified_once_per_file(self):
        with patch.object(data_integrity, "sha256_bytes", wraps=data_integrity.sha256_bytes) as spy:
            for _ in range(3):
                load_price_index(CPI_U)
                price_index_info(CPI_U)
            self.assertEqual(spy.call_count, 1)
            load_price_index(R_CPI_U_RS)
            self.assertEqual(spy.call_count, 2)
            for _ in range(3):
                rpp.load_price_parities()
                rpp.place_parity("us", 2020)
                rpp.price_parity_info()
            self.assertEqual(spy.call_count, 4)  # rpp.csv and pce_price_index.csv, once each

    def test_file_changed_after_verification_is_rechecked(self):
        copy = self._copy(INDEX_DIR)
        path = os.path.join(copy, "cpi_u.csv")
        with patch.object(inflation, "bundled_price_index_dir", return_value=copy):
            self.assertTrue(load_price_index(CPI_U))
            with open(path, "ab") as f:
                f.write(b"2099,1.000\n")
            inflation._read_index_table.cache_clear()
            with self.assertRaises(BundledDataIntegrityError):
                load_price_index(CPI_U)


class TestCustomTables(_Base):
    def test_custom_index_dir_not_verified_hash_reported(self):
        copy = self._copy(INDEX_DIR)
        path = os.path.join(copy, "cpi_u.csv")
        _tamper(path)
        with patch.dict(os.environ, {"CENSUS_PRICE_INDEX_DIR": copy}):
            load_price_index.cache_clear()
            self.assertTrue(load_price_index(CPI_U))
            custom = price_index_info(CPI_U)["custom_table"]
        self.assertEqual(custom["sha256"], _sha(path))
        self.assertEqual(os.path.normcase(custom["path"]), os.path.normcase(os.path.abspath(path)))
        self.assertFalse(custom["verified"])

    def test_custom_index_reported_by_mcp(self):
        import mcp_server

        copy = self._copy(INDEX_DIR)
        with patch.dict(os.environ, {"CENSUS_PRICE_INDEX_DIR": copy}):
            load_price_index.cache_clear()
            out = mcp_server.adjust_inflation(100.0, 2015, 2020)
        self.assertTrue(out["success"])
        self.assertEqual(out["custom_table"]["sha256"], _sha(os.path.join(copy, "r_cpi_u_rs.csv")))

    def test_custom_parity_dir_not_verified_hash_in_notes(self):
        copy = self._copy(PARITY_DIR)
        path = os.path.join(copy, "rpp.csv")
        _tamper(path)
        with patch.dict(os.environ, {"CENSUS_PRICE_PARITY_DIR": copy}):
            rpp.clear_cache()
            described = rpp.describe_place_adjustment("us", 2020)
            info = rpp.price_parity_info()
        self.assertEqual(described["custom_table"]["sha256"], _sha(path))
        self.assertTrue(any(_sha(path) in n and "CUSTOM RPP TABLE" in n for n in described["notes"]))
        self.assertEqual(set(info["custom_tables"]), {"rpp.csv", "pce_price_index.csv"})


class TestUpdateScriptsWriteMatchingHashes(_Base):
    def test_update_price_indexes_output_verifies(self):
        module = _script("update_price_indexes")
        with patch.object(module, "OUT_DIR", self.tmp):
            module.write_index("cpi_u", {2010: 218.056, 2011: 224.939}, {"name": "CPI-U"})
        with patch.object(inflation, "bundled_price_index_dir", return_value=self.tmp):
            self.assertEqual(load_price_index(CPI_U), {2010: 218.056, 2011: 224.939})

    def test_update_price_parities_output_verifies(self):
        module = _script("update_price_parities")
        sha = module._write_csv(os.path.join(self.tmp, "pce_price_index.csv"), ["year", "value"],
                                [{"year": 2017, "value": 100.0}, {"year": 2018, "value": 102.0}])
        with open(os.path.join(self.tmp, "rpp.json"), "w", encoding="utf-8") as f:
            json.dump({"csv_sha256": {"pce_price_index.csv": sha}}, f)
        with patch.object(rpp, "bundled_price_parity_dir", return_value=self.tmp):
            self.assertEqual(rpp.load_pce_price_index(), {2017: 100.0, 2018: 102.0})


if __name__ == "__main__":
    unittest.main()
