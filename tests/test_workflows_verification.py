# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest

from census import workflows_verification as w
from census.verification import (
    BoundRequest,
    canonical_json,
    golden_proposition,
    operator_review,
    request_arguments,
)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    monkeypatch.setattr(w, "_BOUND", None)
    monkeypatch.delenv("CENSUS_VERIFICATION_REQUEST", raising=False)


def configure(tmp_path, monkeypatch, case="false_diagonal_formula"):
    p = golden_proposition(case)
    path = tmp_path / "pinned.json"
    path.write_text(canonical_json({"proposition": p, "review": operator_review(p, "test operator")}))
    monkeypatch.setenv("CENSUS_VERIFICATION_REQUEST", str(path))
    return p, path


def test_unconfigured_engine_cannot_certify_a_claim():
    p = golden_proposition("zero_diagonal")
    r = w.verify_proposition(**request_arguments(p))
    assert not r["success"] and r["error_code"] == "FORMALIZATION_REVIEW_REQUIRED"
    assert r["verification"]["verification_status"] == "error"
    assert not r["verification"]["identity_available"]


def test_library_workflow_mcp_and_cli_agree_without_network(tmp_path, monkeypatch):
    import mcp_server

    p, path = configure(tmp_path, monkeypatch)
    expected = BoundRequest.from_file(path).verify(**request_arguments(p))
    assert w.verify_proposition(**request_arguments(p))["verification"] == expected
    server = mcp_server.server_for_profile("verification")
    tools = server._tool_manager.list_tools()
    assert [t.name for t in tools] == ["verify_proposition"]
    assert "verify_proposition" not in mcp_server.mcp_profiles.PROFILES["core"]
    assert tools[0].fn(**request_arguments(p))["verification"] == expected
    # The workflow performs no HTTP operations; the CLI uses only the pinned local file.
    run = subprocess.run(
        [
            os.sys.executable,
            "-B",
            "-m",
            "census",
            "tool",
            "verify_proposition",
            json.dumps(request_arguments(p)),
            "--json",
        ],
        text=True,
        capture_output=True,
        env=dict(os.environ),
        check=True,
    )
    assert json.loads(run.stdout)["verification"] == expected


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_mcp_validation_does_not_coerce_invalid_schema_versions(tmp_path, monkeypatch, version):
    import mcp_server
    from mcp.server.fastmcp.exceptions import ToolError

    record, _ = configure(tmp_path, monkeypatch)
    arguments = dict(request_arguments(record), schema_version=version)
    direct = w.verify_proposition(**arguments)
    assert direct["verification"]["error_code"] == "PROPOSITION_MISMATCH"
    tool = mcp_server.server_for_profile("verification")._tool_manager.get_tool("verify_proposition")
    # Exercise FastMCP's argument-validation path, not only its stored function.
    with pytest.raises(ToolError, match="schema_version"):
        asyncio.run(tool.run(arguments))


def test_actual_mcp_stdio_preserves_binding_evidence_and_live_revocation(tmp_path, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record, path = configure(tmp_path, monkeypatch)
    expected = BoundRequest.from_file(path).verify(**request_arguments(record))
    root = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        CENSUS_LIVE_TESTS="0",
        CENSUS_CACHE_PATH=str(tmp_path / "stdio-cache.db"),
        MPLCONFIGDIR=str(tmp_path / "mpl"),
    )

    async def exercise():
        server = StdioServerParameters(
            command=os.sys.executable,
            args=["-B", str(root / "mcp_server.py"), "--profile", "verification"],
            cwd=str(root),
            env=env,
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["verify_proposition"]

                async def invoke(args):
                    response = await session.call_tool("verify_proposition", args)
                    assert not response.isError
                    payload = json.loads("".join(c.text for c in response.content if c.type == "text"))
                    return payload

                result = await invoke(request_arguments(record))
                assert result["verification"] == expected
                invalid_version = await session.call_tool(
                    "verify_proposition", dict(request_arguments(record), schema_version=True)
                )
                assert invalid_version.isError
                wrong = await invoke(request_arguments(golden_proposition("zero_diagonal")))
                assert wrong["verification"]["error_code"] == "PROPOSITION_MISMATCH"
                assert wrong["verification"]["claim_holds"] is None
                document = json.loads(path.read_text(encoding="utf-8"))
                document["review"]["decision"] = "revoked"
                path.write_text(canonical_json(document), encoding="utf-8")
                revoked = await invoke(request_arguments(record))
                assert revoked["verification"]["verification_status"] == "inconclusive"
                assert not revoked["verification"]["evidence_verified"]

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_bundle_contains_replayable_evidence_and_refuses_reuse(tmp_path, monkeypatch):
    p, path = configure(tmp_path, monkeypatch)
    out = tmp_path / "bundle"
    r = w.verify_proposition(**request_arguments(p), output_dir=str(out))
    assert r["success"] and r["verification"]["claim_holds"] is False
    saved = json.loads((out / "results/verification.json").read_text())
    assert BoundRequest.from_file(path).check_evidence(saved) == r["verification"]
    assert r["bundle"]["provenance"]["proposition_id"] == p["proposition_id"]
    assert "analysis_status" not in r["bundle"]["provenance"]
    old = (out / "bundle.json").read_bytes()
    failed = w.verify_proposition(**request_arguments(p), output_dir=str(out))
    assert not failed["success"] and failed["verification"]["verification_status"] == "error"
    assert (out / "bundle.json").read_bytes() == old


def test_wrong_claim_never_creates_bundle(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    wrong = golden_proposition("zero_diagonal")
    out = tmp_path / "wrong"
    result = w.verify_proposition(**request_arguments(wrong), output_dir=str(out))
    assert not result["success"]
    assert result["verification"]["error_code"] == "PROPOSITION_MISMATCH"
    assert not out.exists()


def test_session_pin_and_review_revocation(tmp_path, monkeypatch):
    p, path = configure(tmp_path, monkeypatch)
    assert w.verify_proposition(**request_arguments(p))["verification"]["evidence_verified"]
    doc = json.loads(path.read_text())
    doc["review"]["decision"] = "revoked"
    path.write_text(canonical_json(doc))
    assert not w.verify_proposition(**request_arguments(p))["verification"]["evidence_verified"]
    other = golden_proposition("zero_diagonal")
    path.write_text(canonical_json({"proposition": other, "review": operator_review(other, "test operator")}))
    assert (
        w.verify_proposition(**request_arguments(other))["verification"]["error_code"]
        == "PROPOSITION_MISMATCH"
    )


def test_malformed_pinned_file_reports_typed_error(tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_text('{"proposition": NaN}')
    monkeypatch.setenv("CENSUS_VERIFICATION_REQUEST", str(path))
    p = golden_proposition("zero_diagonal")
    r = w.verify_proposition(**request_arguments(p))
    assert not r["success"] and r["error_code"] == "VERIFICATION_SCHEMA"
    assert r["verification"]["claim_holds"] is None


def test_cli_does_not_expose_approval_file_writer_helpers(tmp_path, monkeypatch, capsys):
    from census.__main__ import main

    p, path = configure(tmp_path, monkeypatch)
    before = path.read_bytes()
    forgery = {"proposition": p, "review": operator_review(p, "forged operator")}
    code = main(["tool", "save_json", json.dumps({"result": forgery, "path": str(path)}), "--json"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_INPUT"
    assert path.read_bytes() == before
