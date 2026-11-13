# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport wiring with fake HTTP/MCP contexts only; no sockets or model services."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest

from census.verification import canonical_json
from scripts import verification_live as live
from tests.test_verification_client import fixture


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body if type(body) is bytes else json.dumps(body).encode()
        self.status_code = status
        self.headers = headers or {"server": "fake-runtime/1"}

    async def aiter_raw(self):
        yield self.body

    def raise_for_status(self):
        if self.status_code != 200:
            raise ValueError(f"fake HTTP status {self.status_code}")


class HTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield next(self.responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "http://192.168.1.1/v1",
        "file:///tmp/model",
        "http://localhost.evil/v1",
        "http://user:secret@localhost/v1",
        "http://@localhost/v1",
        "http://localhost/v1?key=secret",
        "http://localhost/v1#fragment",
        "http://localhost:0/v1",
    ],
)
def test_non_loopback_or_ambiguous_urls_are_rejected(url):
    with pytest.raises(ValueError):
        live.loopback_url(url)


@pytest.mark.parametrize(
    "url", ["http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1"]
)
def test_explicit_loopback_urls_are_supported(url):
    assert live.loopback_url(url + "/") == url


@pytest.mark.parametrize("json_final", [False, True])
def test_full_cli_wiring_with_fake_transports_retains_bodies_and_metadata(tmp_path, monkeypatch, json_final):
    import httpx
    import mcp
    import mcp.client.stdio

    bound, schema, request, evidence, final = fixture()
    model = "fixture-model"
    client = HTTP(
        [
            Response({"data": [{"id": model}]}),
            Response({"model": model, "choices": [{"message": {"tool_calls": [request]}}]}),
            Response({"model": model, "choices": [{"message": {"content": json.dumps(final)}}]}),
        ]
    )
    options, parameters, invoked = [], [], []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="verify_proposition",
                        description="test",
                        inputSchema=schema["function"]["parameters"],
                    )
                ]
            )

        async def call_tool(self, name, arguments):
            invoked.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        type="text", text=canonical_json({"success": True, "verification": evidence})
                    )
                ],
            )

    @asynccontextmanager
    async def stdio(params):
        parameters.append(params)
        yield None, None

    def http(**kwargs):
        options.append(kwargs)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", http)
    monkeypatch.setattr(mcp, "ClientSession", lambda *a, **kw: Session())
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    request_path = tmp_path / "operator.json"
    request_path.write_text(
        canonical_json(
            {"proposition": json.loads(bound.record_json), "review": json.loads(bound.review_json)}
        ),
        encoding="utf-8",
    )
    before = request_path.read_bytes()
    output = tmp_path / "new-run"
    argv = [
        "--run-model",
        "--request",
        str(request_path),
        "--base-url",
        "http://localhost:11434/v1",
        "--model",
        model,
        "--runtime-version",
        "fake-runtime/1",
        "--out-dir",
        str(output),
    ]
    if json_final:
        argv.append("--json-final")
    code = live.main(argv)
    assert code == 0 and request_path.read_bytes() == before
    assert options == [{"trust_env": False, "follow_redirects": False}]
    assert parameters[0].args[-2:] == ["--profile", "verification"]
    assert parameters[0].env["CENSUS_VERIFICATION_REQUEST"] == str(request_path.resolve())
    assert [call[0] for call in client.calls] == ["GET", "POST", "POST"] and len(invoked) == 1
    planning, final_payload = (json.loads(call[2]["content"]) for call in client.calls[1:])
    assert planning["model"] == model and len(planning["tools"]) == 1
    assert final_payload["tool_choice"] == "none" and "tools" not in final_payload
    assert "response_format" not in planning
    assert final_payload.get("response_format") == ({"type": "json_object"} if json_final else None)
    assert all(call[2]["timeout"] > 0 for call in client.calls)
    report = json.loads((output / "run.json").read_text())
    assert report["protocol_passed"] and report["declared_runtime"]["source"] == "operator_declared"
    assert report["observed_runtime"]["model_responses"][1]["response_model"] == model
    for name, digest in report["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert json.loads((output / "planning.response.bin").read_bytes())["model"] == model


@pytest.mark.parametrize(
    "response,match",
    [
        (Response(b"{"), "Expecting"),
        (Response(b"no", 500), "500"),
        (Response(b"redirect", 302), "302"),
        (Response(b"compressed", headers={"content-encoding": "gzip"}), "compressed"),
    ],
)
def test_failed_responses_are_retained_before_rejection(tmp_path, response, match):
    with pytest.raises(ValueError, match=match):
        asyncio.run(
            live.exchange(
                HTTP([response]), tmp_path, "failed", "GET", "http://localhost/v1/models", timeout=1
            )
        )
    assert (tmp_path / "failed.response.bin").read_bytes() == response.body


def test_oversized_response_has_a_bounded_prefix_and_explicit_truncation_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "MAX_WIRE_BYTES", 8)
    with pytest.raises(ValueError, match="wire limit"):
        asyncio.run(
            live.exchange(
                HTTP([Response(b"x" * 100)]), tmp_path, "large", "GET", "http://localhost", timeout=1
            )
        )
    assert (tmp_path / "large.response.bin").read_bytes() == b"x" * 8
    assert json.loads((tmp_path / "large.http.json").read_text())["response_truncated"]


def test_no_service_mode_is_available_without_explicit_opt_in(tmp_path):
    with pytest.raises(SystemExit) as error:
        live.main(
            [
                "--request",
                "unused",
                "--base-url",
                "http://localhost/v1",
                "--model",
                "unused",
                "--runtime-version",
                "unused",
                "--out-dir",
                str(tmp_path / "must-not-create"),
            ]
        )
    assert error.value.code == 2 and not (tmp_path / "must-not-create").exists()


def test_existing_wire_log_is_never_overwritten(tmp_path):
    existing = tmp_path / "models.request.bin"
    existing.write_bytes(b"original log")
    client = HTTP([])
    with pytest.raises(FileExistsError):
        asyncio.run(live.exchange(client, tmp_path, "models", "GET", "http://localhost", timeout=1))
    assert existing.read_bytes() == b"original log" and client.calls == []


@pytest.mark.parametrize("condition", ["missing_model", "wrong_response_model", "mcp_error"])
def test_preflight_model_identity_and_tool_errors_cannot_be_scored_as_success(tmp_path, condition):
    bound, schema, request, _, _ = fixture()
    model = "fixture-model"

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="verify_proposition",
                        description="test",
                        inputSchema=schema["function"]["parameters"],
                    )
                ]
            )

        async def call_tool(self, *args):
            assert condition == "mcp_error"
            return SimpleNamespace(
                isError=True, content=[SimpleNamespace(type="text", text='{"error":"fixture"}')]
            )

    responses = [Response({"data": [] if condition == "missing_model" else [{"id": model}]})]
    if condition != "missing_model":
        responses.append(
            Response(
                {
                    "model": "other-model" if condition == "wrong_response_model" else model,
                    "choices": [{"message": {"tool_calls": [request]}}],
                }
            )
        )
    client = HTTP(responses)
    coroutine = live.evaluate_session(
        bound, Session(), client, base_url="http://localhost/v1", model=model, timeout=10, directory=tmp_path
    )
    if condition == "missing_model":
        with pytest.raises(ValueError, match="absent"):
            asyncio.run(coroutine)
        assert len(client.calls) == 1
    else:
        result = asyncio.run(coroutine)
        assert not result["protocol_passed"] and result["failure"] and result["score"] is None
        assert len(client.calls) == 2
        if condition == "mcp_error":
            assert json.loads((tmp_path / "mcp-result.json").read_text()) == {"error": "fixture"}
