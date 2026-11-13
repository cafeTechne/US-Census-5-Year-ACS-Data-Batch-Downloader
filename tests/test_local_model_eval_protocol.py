# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark transport regressions with fake chat and tools, no model runtime."""

import argparse
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "r17_local_model_eval", Path(__file__).parents[1] / "scripts/local_model_eval.py"
)
EVAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EVAL
SPEC.loader.exec_module(EVAL)


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(text='{"value": 1.25}')])


def run(monkeypatch, replies, turns=3):
    replies = iter(replies)
    monkeypatch.setattr(EVAL, "_chat", lambda *a: next(replies))
    task = EVAL.Task(
        "numeric",
        "Compute the value",
        "compute",
        lambda a: a["value"] == 1.25,
        lambda text: text == "1.25",
        "1.25",
    )
    args = argparse.Namespace(no_think=False, max_turns=turns, base_url="unused", model="fake", timeout=1)
    session = FakeSession()
    result = asyncio.run(EVAL._run_task(session, [], task, args))
    return result, session


def call(arguments):
    return {
        "content": "",
        "tool_calls": [{"id": "call1", "function": {"name": "compute", "arguments": arguments}}],
    }


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "null",
        "[]",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":1e999}',
        '{"nested":[-1e999]}',
        '{"value":1,"value":2}',
        '{"nested":{"a":1,"a":2}}',
        {"value": float("inf")},
        {1: 1.25},
        None,
    ],
)
def test_malformed_arguments_are_recorded_without_calling_tool(monkeypatch, raw):
    result, session = run(monkeypatch, [call(raw)])
    assert not result["passed"] and result["error_kind"] == "invalid_tool_arguments"
    assert result["tool_ok"] and not result["args_ok"]
    assert result["calls"][0]["args"] is None
    assert result["calls"][0]["argument_error"]
    assert session.calls == []


def test_missing_arguments_are_not_silently_replaced(monkeypatch):
    reply = call(None)
    del reply["tool_calls"][0]["function"]["arguments"]
    result, session = run(monkeypatch, [reply])
    assert result["error_kind"] == "invalid_tool_arguments"
    assert session.calls == []


@pytest.mark.parametrize("raw", ['{"value":1.25}', {"value": 1.25}])
def test_valid_research_floats_preserve_tools_and_final_scoring(monkeypatch, raw):
    result, session = run(monkeypatch, [call(raw), {"content": "1.25"}])
    assert result["passed"] and result["error_kind"] == ""
    assert session.calls == [("compute", {"value": 1.25})]


def test_tool_loop_exhaustion_is_distinct_from_argument_failure(monkeypatch):
    result, session = run(monkeypatch, [call('{"value":1.25}')] * 2, turns=2)
    assert not result["passed"] and result["error_kind"] == "turn_limit"
    assert result["tool_ok"] and result["args_ok"] and len(session.calls) == 2
    assert result["answer"] == ""


@pytest.mark.parametrize("content", ["", "<think>internal reasoning only</think>"])
def test_empty_final_answer_is_not_a_turn_limit(monkeypatch, content):
    result, session = run(monkeypatch, [{"content": content}])
    assert not result["passed"] and result["error_kind"] == "empty_answer"
    assert session.calls == []


def test_runtime_failure_is_distinct_from_protocol_failure(monkeypatch):
    def fail(*a):
        raise TimeoutError("fake local runtime timeout")

    monkeypatch.setattr(EVAL, "_chat", fail)
    task = EVAL.Task("test", "test", "compute", lambda a: True, lambda a: True, "unused")
    args = argparse.Namespace(no_think=False, max_turns=2, base_url="unused", model="fake", timeout=1)
    result = asyncio.run(EVAL._run_task(FakeSession(), [], task, args))
    assert not result["passed"] and result["error_kind"] == "runtime_error"
    assert "TimeoutError" in result["error"]
