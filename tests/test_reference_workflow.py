# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline acceptance test for docs/reference_workflow.md (PLAN_ROUND_12 C1-C3).

Executes every ``bash`` block of the document in order (``export`` and
``python -m census ...``; a leading ``!`` marks a command expected to fail)
with all network access refused, into a fresh temporary directory; then
executes the whole document again into a second directory and checks that the
substantive results agree. The document has no Python steps (PLAN_ROUND_13 B2).
Input data are the synthetic frozen fixtures in tests/fixtures/reference_workflow/.
Also checks that the Python, CLI and MCP routes of ``run_manifest`` give the
same results and provenance hashes (PLAN_ROUND_13 B1).
"""

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import socket
import tempfile
import unittest
from unittest import mock

import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOC = os.path.join(ROOT, "docs", "reference_workflow.md")
MANIFEST = os.path.join(ROOT, "examples", "manifests", "income_population_tracts.json")
MANIFEST_HASH = "cfa2208fab380730a82ed4a2511236bb22d79a6de634b9f6c984b9415cd4b6c8"
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "reference_workflow")
FIXTURE = os.path.join(FIXTURES, "frozen_api_responses.json")
REPLICATES = os.path.join(FIXTURES, "synthetic_replicates.csv")
REPLICATES_PARTIAL = os.path.join(FIXTURES, "synthetic_replicates_missing_tract.csv")
REPLICATES_SHA256 = "59d38ae0e29334dc03029621551168870185b859376dd7c4e91dbbdf0dd118f7"


def _refuse(*args, **kwargs):
    raise OSError("network access refused by the offline acceptance test")


def bash_blocks(path=DOC):
    with open(path, encoding="utf-8") as f:
        return re.findall(r"```bash\n(.*?)```", f.read(), flags=re.S)


@contextlib.contextmanager
def offline():
    with mock.patch.object(socket.socket, "connect", _refuse), \
            mock.patch.object(socket, "create_connection", _refuse), \
            mock.patch.object(requests.Session, "request", _refuse):
        yield


def run_document(workdir):
    """Execute the document's commands; returns (command name, JSON output) per CLI command."""
    cli_outputs = []
    env = {"RW": workdir}
    from census.__main__ import main

    with mock.patch.dict(os.environ, env), offline():
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            for block in bash_blocks():
                for line in block.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    expect_failure = line.startswith("! ")
                    command = line[2:] if expect_failure else line
                    if command.startswith("export "):
                        name, value = command[len("export "):].split("=", 1)
                        value = value.strip('"')
                        os.environ[name] = workdir if value == "$(mktemp -d)" else os.path.expandvars(value)
                    elif command.startswith("python -m census "):
                        argv = shlex.split(os.path.expandvars(command))[3:]
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                            code = main(argv)
                        out = json.loads(buf.getvalue())
                        if expect_failure and (code == 0 or out.get("success")):
                            raise AssertionError(f"{line} -> exit {code}: expected a failure")
                        if not expect_failure and (code != 0 or not out.get("success")):
                            raise AssertionError(f"{line} -> exit {code}: {out.get('error')}")
                        name = argv[0] if argv[0] not in ("tool", "cache") else argv[1]
                        cli_outputs.append((name, out))
                    else:
                        raise AssertionError(f"undocumented command form in {DOC}: {line}")
        finally:
            os.chdir(cwd)
    return cli_outputs


def substantive(run_dir):
    """Results that must agree across re-executions (not paths, times or figure bytes).
    A blocked run writes no model results, so m1/r1 entries are then None."""
    b = os.path.join(run_dir, "bundle")

    def j(p):
        if not os.path.exists(os.path.join(b, p)):
            return None
        with open(os.path.join(b, p), encoding="utf-8") as f:
            return json.load(f)

    m1, r1, run, prov = j("results/m1.json"), j("results/r1.json"), j("results/run.json"), j("bundle.json")["provenance"]
    return {
        "manifest_hash": prov["manifest_hash"],
        "replicates": prov.get("replicates"),
        "data_source_synthetic": prov["data_source"]["synthetic"],
        "deviation_count": prov["deviations"],
        "hypotheses": {r["id"]: {k: r.get(k) for k in ("estimate", "se", "se_basis", "ci_lower", "ci_upper",
                                                         "p_value", "p_adjusted", "significant", "status")}
                       for r in j("results/hypotheses.json")["rows"]},
        "coefficients": m1 and {c["term"]: {k: c[k] for k in ("estimate", "se_model", "se_sampling", "se_total",
                                                               "ci_lower", "ci_upper")} for c in m1["coefficients"]},
        "interval": m1 and m1["interval"],
        "warnings": sorted((m1 or {}).get("notes", [])) + sorted((r1 or {}).get("notes", [])),
        "exclusions": run["exclusions"], "deviations": run["deviations"], "n_units": run["n_units"],
        "status": run["analysis_status"],
    }


def assert_agree(tc, a, b, path=""):
    if isinstance(a, dict):
        tc.assertEqual(set(a), set(b), path)
        for k in a:
            assert_agree(tc, a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        tc.assertEqual(len(a), len(b), path)
        for i, (x, y) in enumerate(zip(a, b)):
            assert_agree(tc, x, y, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, (int, float)) and not isinstance(b, bool):
        tc.assertTrue(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) or (math.isnan(a) and math.isnan(b)),
                      f"{path}: {a} != {b}")
    else:
        tc.assertEqual(a, b, path)


RUNS = ("run1", "run2", "blocked")


def _mcp_call(server, name, arguments):
    """Call a tool through the MCP layer (argument validation included); returns the result dict."""
    import asyncio

    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):  # (content blocks, structured result)
        return result[1].get("result", result[1])
    if isinstance(result, dict):
        return result.get("result", result)
    return json.loads(result[0].text)


class TestReferenceWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dirs = [tempfile.mkdtemp(prefix="refwf_a_"), tempfile.mkdtemp(prefix="refwf_b_")]
        cls.fixture_bytes = {}
        for name in sorted(os.listdir(FIXTURES)):
            with open(os.path.join(FIXTURES, name), "rb") as f:
                cls.fixture_bytes[name] = f.read()
        cls.outputs = [run_document(d) for d in cls.dirs]

    @classmethod
    def tearDownClass(cls):
        for d in cls.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def outputs_named(self, name, execution=0):
        return [o for n, o in self.outputs[execution] if n == name]

    def test_frozen_inputs_untouched(self):
        for name, data in self.fixture_bytes.items():
            with open(os.path.join(FIXTURES, name), "rb") as f:
                self.assertEqual(f.read(), data, name)

    def test_document_is_cli_only(self):
        """PLAN_ROUND_13 B2: the reference workflow has no Python steps."""
        with open(DOC, encoding="utf-8") as f:
            doc = f.read()
        self.assertNotIn("python - <<", doc)
        self.assertNotIn("```python", doc)
        for block in bash_blocks():
            for line in filter(None, (ln.strip() for ln in block.splitlines())):
                self.assertTrue(line.startswith(("export ", "python -m census ", "! python -m census ")), line)
        self.assertNotIn("Python route", doc)

    def test_documented_cli_steps(self):
        imported = self.outputs_named("import")
        self.assertEqual(len(imported), 1)
        imp = imported[0]
        self.assertEqual(imp["imported"], 2)
        self.assertTrue(imp["synthetic"])
        self.assertEqual([(s["tables"], s["synthetic"]) for s in imp["sources"]], [(["B01003", "B19013"], True)])
        sha = hashlib.sha256(self.fixture_bytes["frozen_api_responses.json"]).hexdigest()
        self.assertEqual(imp["sources"][0]["sha256"], sha)
        with open(DOC, encoding="utf-8") as f:
            doc = f.read()
        self.assertIn(sha, doc)
        self.assertIn(REPLICATES_SHA256, doc)
        self.assertEqual(self.outputs_named("create_manifest")[0]["hash"], MANIFEST_HASH)
        plans = self.outputs_named("plan")
        self.assertEqual(len(plans), 2)
        for p in plans:
            self.assertEqual(p["cached_calls"], p["api_calls"])
            self.assertEqual(p["concordance_summary"]["status_counts"], {"stable": 1})
        run1, run2, blocked = self.outputs_named("run_manifest")
        for out in (run1, run2):
            self.assertTrue(out["success"])
            self.assertEqual(out["analysis_status"], "confirmatory")
            self.assertEqual(out["deviations"], [])
            self.assertTrue(out["data_source"]["synthetic"])
            self.assertEqual(out["replicates"], {"source": "caller_supplied", "synthetic": True, "rows": 60,
                                                 "sha256": REPLICATES_SHA256})
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["analysis_status"], "confirmatory_incomplete")
        replay = self.outputs_named("replay")[0]
        self.assertTrue(replay["verified"])
        self.assertEqual(replay["n_cells_changed"], 0)

    def test_replicate_fixtures(self):
        """The replicate files are what the document says: regenerated from the frozen
        responses (seed 2019) they hash the same, and the partial file lacks one tract."""
        import numpy as np
        from census.manifest import read_replicates, replicates_sha256
        from census.simulation import make_replicates, replicate_long

        frozen = json.loads(self.fixture_bytes["frozen_api_responses.json"].decode("utf-8"))

        def published(table, code):
            header, *rows = frozen["requests"][table]
            e, m = header.index(code + "E"), header.index(code + "M")
            return {r[0][-11:]: (float(r[e]), float(r[m]) / 1.645) for r in rows}

        inc, pop = published("B19013", "B19013_001"), published("B01003", "B01003_001")
        geoids = sorted(inc)
        est = np.array([[inc[g][0], pop[g][0]] for g in geoids])
        se = np.array([[inc[g][1], pop[g][1]] for g in geoids])
        reps = replicate_long(geoids, ["income", "pop"], est, make_replicates(est, se, np.random.default_rng(2019)))
        reps["Year"] = 2019
        path = os.path.join(self.dirs[0], "regenerated_replicates.csv")
        reps.to_csv(path, index=False)
        full = read_replicates(REPLICATES)
        self.assertEqual(replicates_sha256(full), REPLICATES_SHA256)
        self.assertEqual(replicates_sha256(read_replicates(path)), REPLICATES_SHA256)
        partial = read_replicates(REPLICATES_PARTIAL)
        self.assertEqual(replicates_sha256(partial),
                         replicates_sha256(full[full["GEOID"] != "36061000100"]))
        self.assertEqual(len(full) - len(partial), 2)

    def test_local_inputs(self):
        """Step 6: one row per tract, one release, identical tract sets, SE = MOE/1.645."""
        import pandas as pd

        for d in self.dirs:
            panels = {}
            for table in ("B19013", "B01003"):
                df = pd.read_csv(os.path.join(d, "run1", "extraction", table, f"{table}_tract_panel_2019.csv"),
                                 dtype={"geoid": str})
                self.assertFalse(df.duplicated(["geoid", "year"]).any(), table)
                self.assertEqual(set(df["year"]), {2019})
                est = [c for c in df.columns if c.endswith("_est")][0]
                moe, se = est[:-4] + "_moe", est[:-4] + "_se"
                self.assertTrue((df[moe] >= 0).all())
                self.assertTrue(((df[se] - df[moe] / 1.645).abs() < 1e-9).all())
                panels[table] = set(df["geoid"])
            self.assertEqual(len(panels["B19013"]), 30)
            self.assertEqual(panels["B19013"], panels["B01003"])

    def test_documented_expected_values(self):
        s = substantive(os.path.join(self.dirs[0], "run1"))
        self.assertEqual(s["manifest_hash"], MANIFEST_HASH)
        self.assertEqual(s["status"], "confirmatory")
        self.assertEqual(s["n_units"], 30)
        self.assertEqual(s["deviations"], [])
        self.assertEqual(s["exclusions"], [{"rule": "cv_screen", "threshold": 30.0, "variables": ["income"],
                                            "action": "flag", "rows_flagged": 0}])
        h1, pop = s["hypotheses"]["H1"], s["coefficients"]["pop"]
        self.assertEqual(h1["se_basis"], "se_model")
        for got, want in ((h1["estimate"], 4.2811), (pop["se_model"], 0.3299), (pop["se_sampling"], 0.3360),
                          (pop["se_total"], 0.4709), (h1["ci_lower"], 3.7386), (h1["ci_upper"], 4.8237),
                          (s["hypotheses"]["H2"]["estimate"], 0.9260), (s["hypotheses"]["H2"]["ci_lower"], 0.8650),
                          (s["hypotheses"]["H2"]["ci_upper"], 0.9600)):
            self.assertAlmostEqual(got, want, places=4)
        # provenance: synthetic source, caller-supplied replicates hashed into bundle.json
        bdir = os.path.join(self.dirs[0], "run1", "bundle")
        with open(os.path.join(bdir, "bundle.json"), encoding="utf-8") as f:
            bundle = json.load(f)
        prov = bundle["provenance"]
        self.assertEqual(prov["analysis_status"], "confirmatory")
        self.assertTrue(prov["data_source"]["synthetic"])
        self.assertIn("not about Manhattan", prov["data_source"]["description"])
        self.assertIn("SYNTHETIC", bundle["citation"])
        self.assertEqual(prov["replicates"]["sha256"], REPLICATES_SHA256)
        self.assertEqual(prov["replicates"]["source"], "caller_supplied")
        self.assertTrue(prov["replicates"]["synthetic"])
        with open(os.path.join(bdir, "methods.md"), encoding="utf-8") as f:
            methods = f.read()
        self.assertNotIn("Retrieved via the Census Data API", methods)
        self.assertNotIn("80 ACS replicate estimates", methods)
        self.assertIn("synthetic caller-supplied replicate estimates", methods)
        # the interval text quoted in the document is the single-source description
        with open(DOC, encoding="utf-8") as f:
            doc = " ".join(f.read().split())
        quoted = s["interval"]["text"]
        self.assertIn(" ".join(quoted.split()), doc.replace("> ", ""))
        self.assertIn("an association, not a causal effect", quoted)

    def test_reexecution_agrees(self):
        """The documented commands, run twice with the network refused, agree: run1 and
        run2 within one execution, and every run across the two executions."""
        assert_agree(self, substantive(os.path.join(self.dirs[0], "run1")),
                     substantive(os.path.join(self.dirs[0], "run2")))
        for run in RUNS:
            assert_agree(self, substantive(os.path.join(self.dirs[0], run)),
                         substantive(os.path.join(self.dirs[1], run)), run)
        for name in ("create_manifest", "plan"):
            for a, b in zip(self.outputs_named(name, 0), self.outputs_named(name, 1)):
                self.assertEqual(a, b, name)
        imports = [self.outputs_named("import", i)[0] for i in (0, 1)]
        self.assertEqual(*[(o["imported"], o["sources"], o["synthetic"], o["entries"]) for o in imports])
        runs = [[{k: o[k] for k in ("manifest_hash", "analysis_status", "data_source", "replicates", "deviations",
                                    "hypotheses")} for o in self.outputs_named("run_manifest", i)] for i in (0, 1)]
        assert_agree(self, runs[0], runs[1])
        # incidental differences are expected and ignored: paths and extraction timestamps
        with open(os.path.join(self.dirs[1], "run1", "bundle", "results", "run.json"), encoding="utf-8") as f:
            self.assertNotIn(self.dirs[0], f.read())

    def test_python_cli_mcp_routes_agree(self):
        """PLAN_ROUND_13 B1: replicates_path through the CLI and MCP gives the same results and
        provenance hashes as the Python route, including the strict-policy blocked case."""
        from census import workflows, workflows_inference
        from census.manifest import read_replicates, run_manifest

        try:
            import mcp_server
        except ImportError:  # pragma: no cover - MCP extra not installed
            mcp_server = None
        work = tempfile.mkdtemp(prefix="refwf_routes_")
        self.addCleanup(shutil.rmtree, work, True)
        env = {"CENSUS_CACHE_PATH": os.path.join(self.dirs[0], "cache.db")}
        with mock.patch.dict(os.environ, env), offline():
            for name, reps_path, want in (("run1", REPLICATES, "confirmatory"),
                                          ("blocked", REPLICATES_PARTIAL, "confirmatory_incomplete")):
                cli = substantive(os.path.join(self.dirs[0], name))
                py_dir = os.path.join(work, "python_" + name)
                res = run_manifest(MANIFEST, py_dir, replicates=read_replicates(reps_path))
                self.assertEqual(res["analysis_status"], want)
                assert_agree(self, substantive(py_dir), cli, "python " + name)
                if mcp_server is None:
                    continue
                schema = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}["run_manifest"].parameters
                self.assertIn("replicates_path", schema["properties"])
                mcp_dir = os.path.join(work, "mcp_" + name)
                out = _mcp_call(mcp_server, "run_manifest", {"manifest_path": MANIFEST, "output_dir": mcp_dir,
                                                             "replicates_path": reps_path})
                self.assertEqual(out["analysis_status"], want)
                self.assertEqual(out["success"], want == "confirmatory")
                assert_agree(self, substantive(mcp_dir), cli, "mcp " + name)
        self.assertEqual(substantive(os.path.join(work, "python_run1"))["replicates"]["sha256"], REPLICATES_SHA256)
        if mcp_server is not None:
            self.assertIs(mcp_server.run_manifest, workflows_inference.run_manifest)
            self.assertIs(mcp_server.create_manifest, workflows_inference.create_manifest)
            self.assertIs(mcp_server.replay_extraction, workflows.replay_extraction)

    def test_blocked_incomplete_replicates(self):
        with open(os.path.join(self.dirs[0], "blocked", "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        kinds = [d["kind"] for d in run["deviations"]]
        self.assertEqual(kinds.count("replicates_incomplete"), 2)
        self.assertEqual(kinds.count("hypothesis_not_tested"), 2)
        with open(os.path.join(self.dirs[0], "blocked", "bundle", "results", "hypotheses.json"),
                  encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        self.assertTrue(all(r["status"] == "not_tested" and r["estimate"] is None for r in rows))
        with open(os.path.join(self.dirs[0], "blocked", "bundle", "bundle.json"), encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertEqual(prov["deviations"], 4)
        self.assertEqual(prov["analysis_status"], "confirmatory_incomplete")
        self.assertFalse(os.path.exists(os.path.join(self.dirs[0], "blocked", "bundle", "results", "m1.json")))
        self.assertEqual(run["analysis_status"], "confirmatory_incomplete")
        with open(os.path.join(self.dirs[0], "blocked", "bundle", "methods.md"), encoding="utf-8") as f:
            methods = f.read()
        self.assertIn("replicates_incomplete", methods)
        self.assertIn("results are not confirmatory", methods)


class TestCacheImport(unittest.TestCase):
    """``python -m census cache import``: errors and the real-data (unmarked) case."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def cli(self, *argv, env=None):
        from census.__main__ import main

        buf = io.StringIO()
        env = env or {"CENSUS_CACHE_PATH": os.path.join(self.dir, "cache.db")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf):
            code = main(["cache", *argv, "--json"])
        return code, json.loads(buf.getvalue())

    def test_real_source_is_not_marked_synthetic(self):
        from census.cache import QueryCache

        with open(FIXTURE, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["synthetic"]
        path = os.path.join(self.dir, "real.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        code, out = self.cli("import", path)
        self.assertEqual(code, 0)
        self.assertEqual(out["imported"], 2)
        self.assertFalse(out["synthetic"])
        self.assertIsNone(QueryCache(os.path.join(self.dir, "cache.db")).synthetic_marker())
        code, out = self.cli("import", FIXTURE)
        self.assertTrue(out["synthetic"])
        self.assertEqual(out["entries"], 2)  # same request keys: replaced, not duplicated

    def test_errors(self):
        code, out = self.cli("import")
        self.assertNotEqual(code, 0)
        self.assertFalse(out["success"])
        code, out = self.cli("import", os.path.join(self.dir, "missing.json"))
        self.assertNotEqual(code, 0)
        code, out = self.cli("import", self.dir)  # a directory without frozen files
        self.assertNotEqual(code, 0)
        self.assertIn("No frozen response files", out["error"])
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"request": {"year": 2019, "colour": "red"}, "requests": {"B19013": [["GEO_ID"]]}}, f)
        code, out = self.cli("import", bad)
        self.assertNotEqual(code, 0)
        self.assertIn("bad 'request'", out["error"])
        code, out = self.cli("import", FIXTURE, env={"CENSUS_CACHE_DISABLED": "1"})
        self.assertNotEqual(code, 0)
        self.assertIn("disabled", out["error"])


if __name__ == "__main__":
    unittest.main()
