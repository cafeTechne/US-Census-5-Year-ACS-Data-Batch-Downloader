# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded client controls using fake awaitable transports, never a model service."""

import asyncio
import copy
import json

import pytest

from census.verification import (
    BoundRequest,
    canonical_json,
    golden_proposition,
    operator_review,
    request_arguments,
)
from scripts.verification_eval import FINAL_FIELDS, run_protocol


def fixture():
    record = golden_proposition(
        "false_diagonal_formula", original_text="Check the displayed matrix statement."
    )
    bound = BoundRequest.bind(record, operator_review(record, "automated client test fixture"))
    arguments = request_arguments(record)
    evidence = bound.verify(**arguments)
    schema = {
        "type": "function",
        "function": {
            "name": "verify_proposition",
            "parameters": {
                "type": "object",
                "required": list(arguments),
                "properties": {key: {"type": "string"} for key in arguments},
            },
        },
    }
    request = {"id": "call1", "function": {"name": "verify_proposition", "arguments": arguments}}
    final = {key: evidence[key] for key in FINAL_FIELDS}
    return bound, schema, request, evidence, final


def execute(requests=None, final_tool=False):
    bound, schema, request, evidence, final = fixture()
    events = []

    async def chat(*, messages, tools, timeout, final):
        events.append(("chat", final, timeout))
        assert timeout > 0
        if final:
            assert tools == []
            assert messages[-1]["role"] == "user"
            assert "tool phase is closed" in messages[-1]["content"]
            return {"tool_calls": [request]} if final_tool else {"content": json.dumps(final_fields)}
        assert tools == [schema]
        return {"tool_calls": requests if requests is not None else [request]}

    async def invoke(*, name, arguments, timeout):
        events.append(("invoke", name, timeout))
        assert arguments == dict(request["function"]["arguments"], schema_version=1)
        return {"success": True, "verification": evidence}

    final_fields = final
    result = asyncio.run(run_protocol(bound, chat=chat, invoke=invoke, tools=[schema]))
    return result, events


def test_two_phases_replay_evidence_and_end_without_tools():
    result, events = execute()
    assert result["protocol_passed"] and result["failure"] is None
    assert result["model_requests"] == 2 and result["tool_executions"] == 1
    assert [event[0] for event in events] == ["chat", "invoke", "chat"]
    assert all(events[i][2] >= events[i + 1][2] for i in range(len(events) - 1))
    assert len(result["raw_messages"]) == 2


def test_duplicate_default_variants_execute_once_but_remain_visible_to_scoring():
    _, _, request, _, _ = fixture()
    second = copy.deepcopy(request)
    second["id"] = "call2"
    second["function"]["arguments"]["schema_version"] = 1
    result, events = execute([request, second])
    assert result["failure"] is None and result["tool_executions"] == 1
    assert result["duplicate_requests_suppressed"] == 1
    assert sum(event[0] == "invoke" for event in events) == 1
    assert result["score"]["duplicate_call_count"] == 1
    assert not result["protocol_passed"]


@pytest.mark.parametrize("mutation", ["substitute", "write_path", "malformed", "wrong_tool", "version"])
def test_invalid_requests_are_rejected_before_transport_execution(mutation):
    _, _, request, _, _ = fixture()
    args = request["function"]["arguments"]
    if mutation == "substitute":
        args.update(request_arguments(golden_proposition("zero_diagonal")))
    elif mutation == "write_path":
        args["output_dir"] = "/must-not-write"
    elif mutation == "malformed":
        request["function"]["arguments"] = "{"
    elif mutation == "wrong_tool":
        request["function"]["name"] = "approve_proposition"
    else:
        args["schema_version"] = True
    result, events = execute([request])
    assert result["failure"] and not result["protocol_passed"]
    assert result["tool_executions"] == 0 and result["model_requests"] == 1
    assert [event[0] for event in events] == ["chat"]


def test_final_phase_tool_request_is_never_executed():
    result, events = execute(final_tool=True)
    assert "forbidden" in result["failure"]["message"]
    assert result["tool_executions"] == 1 and len(events) == 3
    assert not result["protocol_passed"]


@pytest.mark.parametrize("count", [0, 9])
def test_batch_request_limit_is_checked_before_execution(count):
    _, _, request, _, _ = fixture()
    result, events = execute([dict(request, id=f"call{i}") for i in range(count)])
    assert result["failure"] and result["tool_executions"] == 0
    assert len(events) == 1


def test_global_async_deadline_cancels_the_injected_model():
    bound, schema, _, _, _ = fixture()
    cancelled = []

    async def chat(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    async def invoke(**kwargs):
        pytest.fail("deadline must prevent tool execution")

    result = asyncio.run(run_protocol(bound, chat=chat, invoke=invoke, tools=[schema], timeout=0.01))
    assert result["failure"]["kind"] == "deadline" and cancelled == [True]
    assert result["model_requests"] == 1 and result["tool_executions"] == 0


def test_revocation_is_rechecked_before_reusing_cached_evidence(tmp_path, monkeypatch):
    bound, schema, request, evidence, _ = fixture()
    record = json.loads(bound.record_json)
    path = tmp_path / "request.json"
    document = {"proposition": record, "review": json.loads(bound.review_json)}
    path.write_text(canonical_json(document), encoding="utf-8")
    pinned = BoundRequest.from_file(path)
    original = BoundRequest.check_evidence

    def revoke_after_first_replay(self, result):
        checked = original(self, result)
        document["review"]["decision"] = "revoked"
        path.write_text(canonical_json(document), encoding="utf-8")
        return checked

    monkeypatch.setattr(BoundRequest, "check_evidence", revoke_after_first_replay)

    async def chat(**kwargs):
        return {"tool_calls": [request, dict(request, id="call2")]}

    async def invoke(**kwargs):
        return evidence

    result = asyncio.run(run_protocol(pinned, chat=chat, invoke=invoke, tools=[schema]))
    assert result["failure"] and not result["protocol_passed"]
    assert result["tool_executions"] == 1 and result["model_requests"] == 1
    assert result["duplicate_requests_suppressed"] == 1


def test_schema_preflight_rejects_missing_identities_before_callbacks():
    bound, schema, _, _, _ = fixture()
    schema["function"]["parameters"]["required"] = []

    async def unexpected(**kwargs):
        pytest.fail("invalid preflight must not call a transport")

    with pytest.raises(ValueError, match="three string identities"):
        asyncio.run(run_protocol(bound, chat=unexpected, invoke=unexpected, tools=[schema]))


def test_deadline_also_cancels_tool_transport_before_final_model_request():
    bound, schema, request, _, _ = fixture()
    cancelled = []

    async def chat(**kwargs):
        return {"tool_calls": [request]}

    async def invoke(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    result = asyncio.run(run_protocol(bound, chat=chat, invoke=invoke, tools=[schema], timeout=0.01))
    assert result["failure"]["kind"] == "deadline" and cancelled == [True]
    assert result["model_requests"] == 1 and result["tool_executions"] == 1


def test_actual_mcp_schema_passes_preflight_without_calling_a_service():
    import mcp_server

    bound, _, request, evidence, final = fixture()
    tool = asyncio.run(mcp_server.server_for_profile("verification").list_tools())[0]
    schema = {"type": "function", "function": {"name": tool.name, "parameters": tool.inputSchema}}

    async def chat(**kwargs):
        return {"content": json.dumps(final)} if kwargs["final"] else {"tool_calls": [request]}

    async def invoke(**kwargs):
        return evidence

    assert asyncio.run(run_protocol(bound, chat=chat, invoke=invoke, tools=[schema]))["protocol_passed"]


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 3601, 2**600])
def test_invalid_deadline_configuration_is_rejected_before_callbacks(timeout):
    bound, schema, _, _, _ = fixture()

    async def unexpected(**kwargs):
        pytest.fail("invalid deadline must not call a transport")

    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(run_protocol(bound, chat=unexpected, invoke=unexpected, tools=[schema], timeout=timeout))


def test_forged_tool_evidence_is_rejected_before_final_answer_phase():
    bound, schema, request, evidence, _ = fixture()
    evidence["claim_holds"] = True

    async def chat(**kwargs):
        assert not kwargs["final"], "unreplayed evidence must not reach a final-answer request"
        return {"tool_calls": [request]}

    async def invoke(**kwargs):
        return evidence

    result = asyncio.run(run_protocol(bound, chat=chat, invoke=invoke, tools=[schema]))
    assert result["failure"] and not result["protocol_passed"]
    assert result["model_requests"] == 1 and result["tool_executions"] == 1
