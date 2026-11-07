# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline acceptance test for docs/reference_workflow.md (PLAN_ROUND_12 C1-C3).

Executes every ``bash`` block of the document in order (``export``,
``python -m census ...`` and ``python - <<'EOF'`` heredocs) with all network
access refused, into a fresh temporary directory; then executes the whole
document again into a second directory and checks that the substantive
results agree. Input data are the synthetic frozen fixture
tests/fixtures/reference_workflow/frozen_api_responses.json.
"""

import contextlib
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
MANIFEST_HASH = "42297cbe9d8a19d3b21d26cc0bfc53e94b30fbb97d034d25c0fc392cac97c45b"


def _refuse(*args, **kwargs):
    raise OSError("network access refused by the offline acceptance test")


def bash_blocks(path=DOC):
    with open(path, encoding="utf-8") as f:
        return re.findall(r"```bash\n(.*?)```", f.read(), flags=re.S)


def run_document(workdir):
    """Execute the document's commands; returns the CLI JSON outputs by command."""
    cli_outputs = []
    env = {"RW": workdir}
    from census.__main__ import main

    with mock.patch.dict(os.environ, env), \
            mock.patch.object(socket.socket, "connect", _refuse), \
            mock.patch.object(socket, "create_connection", _refuse), \
            mock.patch.object(requests.Session, "request", _refuse):
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            for block in bash_blocks():
                lines = block.splitlines()
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    i += 1
                    if not line:
                        continue
                    if line.startswith("export "):
                        name, value = line[len("export "):].split("=", 1)
                        value = value.strip('"')
                        os.environ[name] = workdir if value == "$(mktemp -d)" else os.path.expandvars(value)
                    elif line == "python - <<'EOF'":
                        end = lines.index("EOF", i)
                        code = "\n".join(lines[i:end])
                        i = end + 1
                        exec(compile(code, f"{DOC}:heredoc", "exec"), {"__name__": "__main__"})
                    elif line.startswith("python -m census "):
                        argv = shlex.split(os.path.expandvars(line))[3:]
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                            code = main(argv)
                        out = json.loads(buf.getvalue())
                        if code != 0 or not out.get("success"):
                            raise AssertionError(f"{line} -> exit {code}: {out.get('error')}")
                        cli_outputs.append((argv[0] if argv[0] != "tool" else argv[1], out))
                    else:
                        raise AssertionError(f"undocumented command form in {DOC}: {line}")
        finally:
            os.chdir(cwd)
    return cli_outputs


def substantive(run_dir):
    """Results that must agree across re-executions (not paths, times or figure bytes)."""
    b = os.path.join(run_dir, "bundle")

    def j(p):
        with open(os.path.join(b, p), encoding="utf-8") as f:
            return json.load(f)

    m1, r1, run = j("results/m1.json"), j("results/r1.json"), j("results/run.json")
    return {
        "manifest_hash": j("bundle.json")["provenance"]["manifest_hash"],
        "hypotheses": {r["id"]: {k: r.get(k) for k in ("estimate", "se", "se_basis", "ci_lower", "ci_upper",
                                                         "p_value", "p_adjusted", "significant", "status")}
                       for r in j("results/hypotheses.json")["rows"]},
        "coefficients": {c["term"]: {k: c[k] for k in ("estimate", "se_model", "se_sampling", "se_total",
                                                        "ci_lower", "ci_upper")} for c in m1["coefficients"]},
        "interval": m1["interval"],
        "warnings": sorted(m1["notes"]) + sorted(r1["notes"]),
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


class TestReferenceWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dirs = [tempfile.mkdtemp(prefix="refwf_a_"), tempfile.mkdtemp(prefix="refwf_b_")]
        fixture = os.path.join(ROOT, "tests", "fixtures", "reference_workflow", "frozen_api_responses.json")
        with open(fixture, "rb") as f:
            cls.fixture_bytes = f.read()
        cls.outputs = [run_document(d) for d in cls.dirs]

    @classmethod
    def tearDownClass(cls):
        for d in cls.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def test_frozen_inputs_untouched(self):
        fixture = os.path.join(ROOT, "tests", "fixtures", "reference_workflow", "frozen_api_responses.json")
        with open(fixture, "rb") as f:
            self.assertEqual(f.read(), self.fixture_bytes)

    def test_documented_cli_steps(self):
        out = dict(self.outputs[0])
        self.assertEqual(out["create_manifest"]["hash"], MANIFEST_HASH)
        plans = [o for name, o in self.outputs[0] if name == "plan"]
        self.assertEqual(len(plans), 2)
        for p in plans:
            self.assertEqual(p["cached_calls"], p["api_calls"])
            self.assertEqual(p["concordance_summary"]["status_counts"], {"stable": 1})
        self.assertEqual(out["run_manifest"]["deviations"], [])
        self.assertTrue(out["replay"]["verified"])
        self.assertEqual(out["replay"]["n_cells_changed"], 0)

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
                          (s["hypotheses"]["H2"]["estimate"], 0.9260)):
            self.assertAlmostEqual(got, want, places=4)
        # the interval text quoted in the document is the single-source description
        with open(DOC, encoding="utf-8") as f:
            doc = " ".join(f.read().split())
        quoted = s["interval"]["text"]
        self.assertIn(" ".join(quoted.split()), doc.replace("> ", ""))
        self.assertIn("an association, not a causal effect", quoted)

    def test_reexecution_agrees(self):
        """Second full execution from the same frozen inputs, fresh directory."""
        a = substantive(os.path.join(self.dirs[0], "run1"))
        b = substantive(os.path.join(self.dirs[1], "run1"))
        assert_agree(self, a, b)
        # incidental differences are expected and ignored: paths and extraction timestamps
        with open(os.path.join(self.dirs[1], "run1", "bundle", "results", "run.json"), encoding="utf-8") as f:
            self.assertNotIn(self.dirs[0], f.read())

    def test_cli_mcp_python_semantic_agreement(self):
        py = substantive(os.path.join(self.dirs[0], "run1"))
        cli = substantive(os.path.join(self.dirs[0], "cli_run"))
        for hid in py["hypotheses"]:
            for k in ("estimate", "se", "se_basis", "ci_lower", "ci_upper", "p_adjusted", "status"):
                assert_agree(self, py["hypotheses"][hid][k], cli["hypotheses"][hid][k], f"{hid}.{k}")
        self.assertEqual(py["interval"]["basis_column"], cli["interval"]["basis_column"])
        self.assertEqual(py["exclusions"], cli["exclusions"])
        self.assertEqual(py["manifest_hash"], cli["manifest_hash"])
        # the CLI adapter cannot take replicates: documented, and visible in its notes
        self.assertTrue(any("no replicates" in n for n in cli["warnings"]))
        from census import workflows, workflows_inference
        try:
            import mcp_server
        except ImportError:  # pragma: no cover - MCP extra not installed
            return
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
            self.assertEqual(json.load(f)["provenance"]["deviations"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.dirs[0], "blocked", "bundle", "results", "m1.json")))


if __name__ == "__main__":
    unittest.main()
