# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 17 offline CLI and injected-transport client; no built-in model runtime.

The operator supplies the trusted request separately. Transcripts cannot approve
formalizations or establish mathematical authority. Reuse the existing harness's
argument parser, and replay evidence through the existing bounded checker.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from census.verification import (  # noqa: E402
    BoundRequest,
    MAX_BYTES,
    VerificationError,
    canonical_json,
    request_arguments,
    strict_json,
)
from scripts.local_model_eval import _tool_arguments  # noqa: E402

PROTOCOL_VERSION = "r17-offline-verification-2"
MAX_CALLS = 8
FINAL_FIELDS = {"proposition_id", "evidence_digest", "verification_status", "claim_holds"}
INSTRUCTIONS = (
    "Check only the host-pinned proposition below. Call verify_proposition with the supplied exact "
    "request arguments, without an output_dir. Do not substitute a related theorem. After the tool "
    "response, return only a JSON object with proposition_id, evidence_digest, verification_status "
    "and claim_holds copied from the checked evidence. Refutation means claim_holds=false; "
    "diagnostic/inconclusive evidence does not prove a universal claim. Do not repeat identical calls."
)


def prepare(bound):
    """Freeze the prompt, without exposing the expected answer in that prompt."""
    record = strict_json(bound.record_json)
    arguments = request_arguments(record)
    expected = bound.verify(**arguments)
    if not expected["success"] or expected.get("formalization_status") != "approved":
        raise ValueError("preparation requires a currently approved, successfully checked pinned request")
    prompt = {
        "system": INSTRUCTIONS,
        "user": canonical_json({"proposition": record["body"], "request_arguments": arguments}),
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "prompt": prompt,
        "prompt_digest": hashlib.sha256(canonical_json(prompt).encode("utf-8")).hexdigest(),
        "request_arguments": arguments,
        "review_digest": expected["review_digest"],
        "checker_method_version": expected["method_version"],
        "expected_final": {field: expected[field] for field in sorted(FINAL_FIELDS)},
        "mathematical_evidence_verified": expected["evidence_verified"],
        "formalization_fidelity": "requires substantive operator review; not automatically scored",
    }


def score(bound, transcript):
    """Score the frozen protocol, not model reliability or arbitrary narrative."""
    canonical_json(transcript)  # Apply existing exact JSON/size/depth limits.
    if (
        type(transcript) is not dict
        or set(transcript) != {"schema_version", "calls", "final"}
        or type(transcript["schema_version"]) is not int
        or transcript["schema_version"] != 1
        or type(transcript["calls"]) is not list
        or len(transcript["calls"]) > MAX_CALLS
    ):
        raise ValueError("expected schema_version=1, at most eight calls, and raw final-answer text")
    case = prepare(bound)
    expected_args = case["request_arguments"]
    calls = []
    seen, duplicates = set(), 0
    for call in transcript["calls"]:
        if type(call) is not dict or set(call) != {"tool", "arguments", "result"}:
            raise ValueError("each call requires tool, arguments and result")
        item = {
            "tool_selection_correct": call["tool"] == "verify_proposition",
            "arguments_parsed": False,
            "request_binding_correct": False,
            "arguments_correct": False,
            "evidence_replayed": False,
        }
        try:
            arguments = _tool_arguments(call["arguments"])
            item["arguments_parsed"] = True
            item["request_binding_correct"] = all(arguments.get(k) == v for k, v in expected_args.items())
            version = arguments.get("schema_version", 1)
            item["arguments_correct"] = (
                item["request_binding_correct"]
                and set(arguments) <= set(expected_args) | {"schema_version"}
                and type(version) is int
                and version == 1
            )
            fingerprint = canonical_json(
                {
                    "tool": call["tool"],
                    "arguments": (
                        dict(expected_args, schema_version=1) if item["arguments_correct"] else arguments
                    ),
                }
            )
        except (ValueError, TypeError, RecursionError) as exc:
            item["argument_error"] = str(exc)
            fingerprint = canonical_json({"tool": call["tool"], "raw_arguments": call["arguments"]})
        duplicates += int(fingerprint in seen)
        seen.add(fingerprint)
        try:
            result = call["result"]
            if type(result) is dict and "verification" in result:
                result = result["verification"]
            bound.check_evidence(result)
            item["evidence_replayed"] = True
        except (ValueError, TypeError, OSError) as exc:
            item["evidence_error"] = str(exc)
        calls.append(item)
    final, parsed, schema_valid = None, False, False
    completed = type(transcript["final"]) is str and bool(transcript["final"].strip())
    if completed:
        try:
            final = strict_json(transcript["final"])
            parsed = type(final) is dict
            schema_valid = (
                parsed
                and set(final) == FINAL_FIELDS
                and all(type(final[k]) is str for k in FINAL_FIELDS - {"claim_holds"})
                and (final["claim_holds"] is None or type(final["claim_holds"]) is bool)
            )
        except VerificationError:
            pass
    interpretation = schema_valid and final == case["expected_final"]
    metrics = {
        key: bool(calls) and all(call[key] for call in calls)
        for key in (
            "tool_selection_correct",
            "arguments_parsed",
            "request_binding_correct",
            "arguments_correct",
            "evidence_replayed",
        )
    }
    return {
        "evaluation_mode": "offline_transcript_replay",
        "protocol_version": PROTOCOL_VERSION,
        "prompt_digest": case["prompt_digest"],
        "proposition_id": expected_args["proposition_id"],
        "review_digest": case["review_digest"],
        "checker_method_version": case["checker_method_version"],
        "reference_evidence_digest": case["expected_final"]["evidence_digest"],
        "formalization_fidelity": case["formalization_fidelity"],
        "mathematical_evidence_status": (
            case["expected_final"]["verification_status"]
            if metrics["evidence_replayed"] and metrics["request_binding_correct"]
            else "not_established_by_transcript"
        ),
        "mathematical_evidence_verified": (
            case["mathematical_evidence_verified"]
            and metrics["evidence_replayed"]
            and metrics["request_binding_correct"]
        ),
        **metrics,
        "call_count": len(calls),
        "duplicate_call_count": duplicates,
        "completed": completed,
        "final_json_parsed": parsed,
        "final_schema_valid": schema_valid,
        "final_interpretation_correct": bool(interpretation),
        "calls": calls,
        "protocol_passed": bool(all(metrics.values()) and interpretation and duplicates == 0),
        "runtime_metadata": "not collected by scorer; offline replay performs no model-service calls",
        "trace_authenticity": "not authenticated; retain original runtime traces separately",
    }


def tool_schema_digest(tools, arguments):
    """Shared preflight before a runtime catalog or model request is sent."""
    if (
        type(tools) is not list
        or len(tools) != 1
        or type(tools[0]) is not dict
        or tools[0].get("type") != "function"
        or type(tools[0].get("function")) is not dict
        or tools[0].get("function", {}).get("name") != "verify_proposition"
    ):
        raise ValueError("preflight requires exactly the verification tool")
    schema = tools[0]["function"].get("parameters", {})
    required = schema.get("required") if type(schema) is dict else None
    properties = schema.get("properties") if type(schema) is dict else None
    if (
        type(required) is not list
        or any(type(key) is not str for key in required)
        or type(properties) is not dict
        or not set(arguments) <= set(required)
        or any(
            type(properties.get(k)) is not dict or properties[k].get("type") != "string" for k in arguments
        )
    ):
        raise ValueError("verification schema must require all three string identities")
    return hashlib.sha256(canonical_json(tools).encode("utf-8")).hexdigest()


async def run_protocol(bound, *, chat, invoke, tools, timeout=30):
    """Two-phase client with injected async transports; no default runtime.

    Callbacks receive the remaining deadline. They must honor cancellation and
    enforce that timeout in their underlying transport; this is not process
    preemption. Only replayed bound evidence is passed to the final phase.
    """
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600 or not math.isfinite(timeout):
        raise ValueError("timeout must be finite and in (0, 3600] seconds")
    case = prepare(bound)
    schema_digest = tool_schema_digest(tools, case["request_arguments"])
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    messages = [{"role": role, "content": content} for role, content in case["prompt"].items()]
    transcript = {"schema_version": 1, "calls": [], "final": ""}
    cache, raw, model_requests, tool_executions, suppressed = {}, [], 0, 0, 0
    failure, report = None, None

    async def timed(callback, *, kind, **kwargs):
        nonlocal model_requests, tool_executions
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("verification client deadline exhausted")
        if kind == "model":
            model_requests += 1
        else:
            tool_executions += 1
        return await asyncio.wait_for(callback(timeout=remaining, **kwargs), timeout=remaining)

    try:
        message = await timed(
            chat, kind="model", messages=copy.deepcopy(messages), tools=copy.deepcopy(tools), final=False
        )
        canonical_json(message)
        raw.append(copy.deepcopy(message))
        requests = message.get("tool_calls") or []
        if type(requests) is not list or not 1 <= len(requests) <= MAX_CALLS:
            raise ValueError("tool phase requires one to eight verification requests")
        identifiers = [request.get("id") for request in requests]
        if any(type(identifier) is not str or not identifier for identifier in identifiers) or len(
            set(identifiers)
        ) != len(identifiers):
            raise ValueError("tool-call IDs must be unique nonempty strings")
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": requests}
        )
        for request in requests:
            function = request["function"]
            arguments = _tool_arguments(function.get("arguments"))
            version = arguments.get("schema_version", 1)
            if (
                function.get("name") != "verify_proposition"
                or not all(arguments.get(k) == v for k, v in case["request_arguments"].items())
                or set(arguments) - set(case["request_arguments"]) - {"schema_version"}
                or type(version) is not int
                or version != 1
            ):
                raise ValueError("tool request differs from the pinned verification contract")
            # Normalize the optional default before caching semantically identical calls.
            normalized = dict(case["request_arguments"], schema_version=1)
            key = canonical_json(normalized)
            if key in cache:
                result = copy.deepcopy(cache[key])
                suppressed += 1
            else:
                result = await timed(
                    invoke, kind="tool", name="verify_proposition", arguments=copy.deepcopy(normalized)
                )
                cache[key] = copy.deepcopy(result)
            evidence = result.get("verification", result)
            bound.check_evidence(evidence)  # Also rechecks revocation on cache hits.
            transcript["calls"].append(
                {"tool": "verify_proposition", "arguments": arguments, "result": result}
            )
            messages.append(
                {"role": "tool", "tool_call_id": request["id"], "content": canonical_json(result)}
            )
        messages.append(
            {
                "role": "user",
                "content": "The tool phase is closed. Return only the required final JSON; no tool calls.",
            }
        )
        message = await timed(chat, kind="model", messages=copy.deepcopy(messages), tools=[], final=True)
        canonical_json(message)
        raw.append(copy.deepcopy(message))
        if message.get("tool_calls"):
            raise ValueError("tool calls are forbidden in the final-answer phase")
        transcript["final"] = message.get("content") or ""
        report = score(bound, transcript)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OSError,
        RuntimeError,
        asyncio.TimeoutError,
    ) as exc:
        failure = {
            "kind": "deadline"
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
            else "protocol_or_transport_error",
            "message": str(exc),
        }
    return {
        "client_protocol_version": "r17-two-phase-client-1",
        "tool_schema_digest": schema_digest,
        "prompt_digest": case["prompt_digest"],
        "model_requests": model_requests,
        "tool_executions": tool_executions,
        "duplicate_requests_suppressed": suppressed,
        "elapsed_seconds": loop.time() - started,
        "timeout_seconds": timeout,
        "transcript": transcript,
        "raw_messages": raw,
        "runtime_metadata": "injected transports; caller must retain actual runtime/model/version/settings",
        "score": report,
        "failure": failure,
        "protocol_passed": failure is None and report is not None and report["protocol_passed"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="operator-controlled pinned request envelope")
    parser.add_argument("--transcript", help="offline exact-JSON transcript; omit to prepare a frozen case")
    parser.add_argument("--out", help="optional new output file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        bound = BoundRequest.from_file(args.request)
        if args.transcript:
            with open(args.transcript, "rb") as stream:
                transcript = strict_json(stream.read(MAX_BYTES + 1))
            result = score(bound, transcript)
        else:
            result = prepare(bound)
        serialized = canonical_json(result) + "\n"
        if args.out:
            with open(args.out, "x", encoding="utf-8") as stream:
                stream.write(serialized)
        print(serialized, end="")
        return 0 if result.get("protocol_passed", True) else 1
    except (ValueError, TypeError, OSError) as exc:
        print(
            json.dumps(
                {"success": False, "error_code": getattr(exc, "code", "INVALID_INPUT"), "error": str(exc)}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
