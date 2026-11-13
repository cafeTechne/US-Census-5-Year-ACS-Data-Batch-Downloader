# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline benchmark consumer controls; no model, network, or scientific fixtures."""

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from census.verification import (
    BoundRequest,
    canonical_json,
    golden_proposition,
    operator_review,
    request_arguments,
)
from scripts import verification_eval as evaluation


def context(case="false_diagonal_formula"):
    record = golden_proposition(
        case, original_text="Check the displayed matrix statement under its assumptions."
    )
    bound = BoundRequest.bind(record, operator_review(record, "automated evaluation test fixture"))
    evidence = bound.verify(**request_arguments(record))
    final = {key: evidence[key] for key in evaluation.FINAL_FIELDS}
    trace = {
        "schema_version": 1,
        "calls": [{"tool": "verify_proposition", "arguments": request_arguments(record), "result": evidence}],
        "final": json.dumps(final),
    }
    return record, bound, trace


@pytest.mark.parametrize(
    "case,status,holds,verified",
    [
        ("zero_diagonal", "proved", True, True),
        ("false_diagonal_formula", "refuted", False, True),
        ("heteroscedastic_covariance", "diagnostic_only", None, False),
    ],
)
def test_truth_and_diagnostic_status_are_separate_from_protocol_success(case, status, holds, verified):
    _, bound, trace = context(case)
    report = evaluation.score(bound, trace)
    assert report["protocol_passed"] and report["evidence_replayed"]
    assert report["mathematical_evidence_status"] == status
    assert report["mathematical_evidence_verified"] is verified
    assert json.loads(trace["final"])["claim_holds"] is holds
    assert "not automatically scored" in report["formalization_fidelity"]


def test_frozen_prompt_is_deterministic_and_answer_is_outside_prompt():
    _, bound, _ = context()
    first = evaluation.prepare(bound)
    assert first == evaluation.prepare(bound)
    assert "expected_final" not in first["prompt"]["user"]
    assert "false_diagonal_formula" not in first["prompt"]["user"]
    assert first["expected_final"]["verification_status"] == "refuted"
    changed = golden_proposition("false_diagonal_formula", original_text="Different source question")
    other = BoundRequest.bind(changed, operator_review(changed, "automated test fixture"))
    assert evaluation.prepare(other)["prompt_digest"] != first["prompt_digest"]


def test_wrong_known_good_proposition_and_forged_labels_cannot_pass():
    _, bound, trace = context()
    other, _, other_trace = context("zero_diagonal")
    trace["calls"] = other_trace["calls"]
    trace["final"] = other_trace["final"]
    report = evaluation.score(bound, trace)
    assert not report["protocol_passed"] and not report["request_binding_correct"]
    assert not report["evidence_replayed"] and not report["final_interpretation_correct"]
    assert not report["mathematical_evidence_verified"]
    assert report["mathematical_evidence_status"] == "not_established_by_transcript"
    # Correcting a label is insufficient when the mathematical record is for B.
    trace["calls"][0]["arguments"] = evaluation.prepare(bound)["request_arguments"]
    forged = trace["calls"][0]["result"]
    forged["proposition_id"] = trace["calls"][0]["arguments"]["proposition_id"]
    assert not evaluation.score(bound, trace)["evidence_replayed"]
    assert other["proposition_id"] != forged["proposition_id"]


def test_workflow_envelope_and_string_arguments_are_supported():
    _, bound, trace = context()
    call = trace["calls"][0]
    call["arguments"] = json.dumps(call["arguments"], sort_keys=True)
    call["result"] = {"success": True, "verification": call["result"], "summary": "transport guidance"}
    assert evaluation.score(bound, trace)["protocol_passed"]


@pytest.mark.parametrize("arguments", ["{", '{"a":1,"a":2}', "null", None])
def test_malformed_arguments_remain_separate_from_correct_final_answer(arguments):
    _, bound, trace = context()
    trace["calls"][0]["arguments"] = arguments
    report = evaluation.score(bound, trace)
    assert not report["arguments_parsed"] and not report["protocol_passed"]
    assert report["final_interpretation_correct"]


@pytest.mark.parametrize("version", [True, "1", 2])
def test_invalid_schema_version_cannot_be_coerced_into_a_pass(version):
    _, bound, trace = context()
    trace["calls"][0]["arguments"]["schema_version"] = version
    report = evaluation.score(bound, trace)
    assert report["arguments_parsed"] and report["request_binding_correct"]
    assert not report["arguments_correct"] and not report["protocol_passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_holds", True),
        ("claim_holds", 0),
        ("verification_status", "proved"),
        ("evidence_digest", "0" * 64),
        ("proposition_id", "0" * 64),
    ],
)
def test_final_interpretation_must_cite_exact_evidence_and_typed_outcome(field, value):
    _, bound, trace = context()
    final = json.loads(trace["final"])
    final[field] = value
    trace["final"] = json.dumps(final)
    report = evaluation.score(bound, trace)
    assert report["evidence_replayed"] and not report["final_interpretation_correct"]
    assert not report["protocol_passed"]


def test_duplicate_requests_and_missing_calls_are_visible_without_inventing_execution_counts():
    _, bound, trace = context()
    second = copy.deepcopy(trace["calls"][0])
    second["arguments"] = json.dumps(second["arguments"], sort_keys=True)
    trace["calls"].append(second)
    report = evaluation.score(bound, trace)
    assert report["duplicate_call_count"] == 1 and report["call_count"] == 2
    assert report["final_interpretation_correct"] and not report["protocol_passed"]
    trace["calls"] = []
    missing = evaluation.score(bound, trace)
    assert not missing["protocol_passed"] and not missing["mathematical_evidence_verified"]


def test_repeated_malformed_requests_are_counted_as_duplicates():
    _, bound, trace = context()
    trace["calls"][0]["arguments"] = "{"
    trace["calls"].append(copy.deepcopy(trace["calls"][0]))
    report = evaluation.score(bound, trace)
    assert report["duplicate_call_count"] == 1 and not report["arguments_parsed"]
    assert not report["protocol_passed"]


def test_float_version_in_original_argument_text_is_not_coerced():
    _, bound, trace = context()
    arguments = trace["calls"][0]["arguments"]
    trace["calls"][0]["arguments"] = json.dumps(dict(arguments, schema_version=1.0))
    report = evaluation.score(bound, trace)
    assert report["arguments_parsed"] and report["request_binding_correct"]
    assert not report["arguments_correct"] and not report["protocol_passed"]


@pytest.mark.parametrize(
    "raw,completed,parsed",
    [("", False, False), ("{", True, False), ("[]", True, False), ('{"unrelated":true}', True, True)],
)
def test_completion_parsing_and_schema_are_separate(raw, completed, parsed):
    _, bound, trace = context()
    trace["final"] = raw
    report = evaluation.score(bound, trace)
    assert report["completed"] is completed and report["final_json_parsed"] is parsed
    assert not report["final_schema_valid"] and not report["protocol_passed"]


def test_transcript_limits_and_review_preconditions():
    record, bound, trace = context()
    trace["calls"] *= 9
    with pytest.raises(ValueError, match="eight"):
        evaluation.score(bound, trace)
    for review in (None, operator_review(record, "fixture", "revoked")):
        with pytest.raises(ValueError, match="currently approved"):
            evaluation.prepare(BoundRequest.bind(record, review))


def test_tampered_evidence_cannot_be_replaced_by_a_verified_flag():
    _, bound, trace = context()
    trace["calls"][0]["result"]["claim_holds"] = True
    trace["calls"][0]["result"]["evidence_verified"] = True
    report = evaluation.score(bound, trace)
    assert not report["evidence_replayed"] and not report["mathematical_evidence_verified"]
    assert report["final_interpretation_correct"] and not report["protocol_passed"]


def test_changed_review_invalidates_previously_saved_trace(tmp_path):
    record, bound, trace = context()
    path = tmp_path / "request.json"
    path.write_text(
        canonical_json({"proposition": record, "review": json.loads(bound.review_json)}), encoding="utf-8"
    )
    pinned = BoundRequest.from_file(path)
    assert evaluation.score(pinned, trace)["protocol_passed"]
    path.write_text(
        canonical_json({"proposition": record, "review": operator_review(record, "new reviewer")}),
        encoding="utf-8",
    )
    report = evaluation.score(pinned, trace)
    assert not report["evidence_replayed"] and not report["protocol_passed"]


def test_unrelated_tool_with_valid_record_is_still_a_tool_selection_failure():
    _, bound, trace = context()
    trace["calls"][0]["tool"] = "check_identity"
    report = evaluation.score(bound, trace)
    assert not report["tool_selection_correct"] and not report["protocol_passed"]


def test_cli_prepares_scores_and_never_overwrites_operator_request(tmp_path):
    record, bound, trace = context()
    request = tmp_path / "request.json"
    request.write_text(
        canonical_json({"proposition": record, "review": json.loads(bound.review_json)}),
        encoding="utf-8",
    )
    before = request.read_bytes()
    transcript = tmp_path / "transcript.json"
    transcript.write_text(canonical_json(trace), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts/verification_eval.py"

    def run(*extra):
        return subprocess.run(
            [sys.executable, "-B", str(script), "--request", str(request), *extra],
            capture_output=True,
            text=True,
            check=False,
        )

    prepared = run()
    assert (
        prepared.returncode == 0
        and json.loads(prepared.stdout)["protocol_version"] == evaluation.PROTOCOL_VERSION
    )
    scored = run("--transcript", str(transcript))
    assert scored.returncode == 0 and json.loads(scored.stdout)["protocol_passed"]
    trace["final"] = "{}"
    transcript.write_text(canonical_json(trace), encoding="utf-8")
    failed = run("--transcript", str(transcript))
    assert failed.returncode == 1 and not json.loads(failed.stdout)["protocol_passed"]
    transcript.write_text('{"schema_version":', encoding="utf-8")
    malformed = run("--transcript", str(transcript))
    assert malformed.returncode == 2 and not json.loads(malformed.stdout)["success"]
    refused = run("--out", str(request))
    assert refused.returncode == 2 and request.read_bytes() == before
