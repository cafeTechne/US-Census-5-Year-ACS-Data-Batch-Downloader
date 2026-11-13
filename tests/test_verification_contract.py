# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import json

import pytest

from census.verification import (
    BoundRequest,
    VerificationError,
    canonical_json,
    golden_proposition,
    operator_review,
    proposition,
    request_arguments,
    strict_json,
    validate_record,
)


def approved(case="zero_diagonal"):
    p = golden_proposition(case)
    return p, BoundRequest.bind(p, operator_review(p, "supervised operator"))


def test_canonical_roundtrip_full_digests_and_literal_normalization():
    p, _ = approved()
    assert len(p["proposition_id"]) == len(p["expression_digest"]) == 64
    assert validate_record(strict_json(canonical_json(p))) == p
    body = copy.deepcopy(golden_proposition("centering_idempotence")["body"])
    body["definitions"]["M"]["right"]["left"]["left"]["value"] = {"numerator": -2, "denominator": -2}
    assert proposition(body) == golden_proposition("centering_idempotence")


def test_source_provenance_is_not_rewritten_as_a_mathematical_literal():
    b = copy.deepcopy(golden_proposition("zero_diagonal")["body"])
    b["provenance"] = {"numerator": 2, "denominator": 2}
    p = proposition(b)
    assert p["body"]["provenance"] == b["provenance"]
    b["provenance"] = {"numerator": 1, "denominator": 1}
    assert proposition(b)["proposition_id"] != p["proposition_id"]


@pytest.mark.parametrize("raw", ["\ud800", '"\\ud800"', '"\\udfff"', b'"\xff"'])
def test_invalid_unicode_is_a_typed_schema_error(raw):
    with pytest.raises(VerificationError) as error:
        strict_json(raw)
    assert error.value.code == "VERIFICATION_SCHEMA"


def test_invalid_unicode_in_proposition_or_json_key_cannot_acquire_an_identity():
    body = copy.deepcopy(golden_proposition("zero_diagonal")["body"])
    body["original_text"] = "unpaired surrogate: \ud800"
    with pytest.raises(VerificationError, match="UTF-8"):
        proposition(body)
    with pytest.raises(VerificationError, match="UTF-8"):
        canonical_json({"\ud800": "invalid key"})


def test_non_ascii_source_text_roundtrips_without_rewriting():
    body = copy.deepcopy(golden_proposition("zero_diagonal")["body"])
    body["original_text"] = "Zéro diagonale : WΩ = 0? 🧮"
    record = proposition(body)
    assert strict_json(canonical_json(record)) == record
    assert record["body"]["original_text"] == body["original_text"]


@pytest.mark.parametrize("field", ["proposition_id", "expression_digest", "assumptions_digest"])
def test_swapped_identity_rejected_even_when_other_claim_is_proved(field):
    p, bound = approved("false_diagonal_formula")
    q, other = approved("row_sum" if field == "assumptions_digest" else "zero_diagonal")
    assert other.verify(**request_arguments(q))["claim_holds"]
    args = request_arguments(p)
    args[field] = q[field]
    result = bound.verify(**args)
    assert not result["success"] and result["error_code"] == "PROPOSITION_MISMATCH"
    assert result["claim_holds"] is None and not result["evidence_verified"]


def test_pending_and_revoked_reviews_never_certify_original_prose():
    p, _ = approved()
    for review in (None, operator_review(p, "operator", "revoked")):
        r = BoundRequest.bind(p, review).verify(**request_arguments(p))
        assert r["verification_status"] == "inconclusive"
        assert r["formalization_status"] == "pending_review"
        assert not r["evidence_verified"]


def test_mismatched_and_forged_review_fields_fail_closed():
    p, _ = approved()
    q, _ = approved("row_sum")
    for review in (operator_review(q, "operator"), {"approved": True, "reviewed_by": "human:fake"}):
        r = BoundRequest.bind(p, review).verify(**request_arguments(p))
        assert r["verification_status"] == "error" and not r["evidence_verified"]


def test_live_review_revocation_and_changed_pinned_file(tmp_path):
    p, _ = approved()
    path = tmp_path / "request.json"
    doc = {"proposition": p, "review": operator_review(p, "operator")}
    path.write_text(canonical_json(doc))
    bound = BoundRequest.from_file(path)
    assert bound.verify(**request_arguments(p))["claim_holds"] is True
    doc["review"]["decision"] = "revoked"
    path.write_text(canonical_json(doc))
    assert not bound.verify(**request_arguments(p))["evidence_verified"]
    q, _ = approved("row_sum")
    doc = {"proposition": q, "review": operator_review(q, "operator")}
    path.write_text(canonical_json(doc))
    assert bound.verify(**request_arguments(p))["error_code"] == "PROPOSITION_MISMATCH"


@pytest.mark.parametrize(
    "mutation", ["text", "transpose", "sign", "dimension", "quantifier", "assumption", "index", "schema"]
)
def test_record_mutations_cannot_reuse_identity(mutation):
    p, _ = approved("weighted_squares")
    bad = copy.deepcopy(p)
    b = bad["body"]
    if mutation == "text":
        b["original_text"] += " different original claim"
    elif mutation == "transpose":
        b["expression"]["left"]["matrix"]["right"] = {"op": "symbol", "name": "W"}
    elif mutation == "sign":
        b["expression"]["right"]["body"]["op"] = "subtract"
    elif mutation == "dimension":
        b["quantifiers"]["min_n"] = 2
    elif mutation == "quantifier":
        b["quantifiers"]["indices"] = []
    elif mutation == "assumption":
        b["assumptions"] = []
    elif mutation == "index":
        b["expression"]["left"]["column"] = 1
    else:
        b["schema_version"] = 2
    with pytest.raises(VerificationError):
        validate_record(bad)


@pytest.mark.parametrize(
    "raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', r'{"x":"\Omega"}', "", "{} garbage"]
)
def test_json_failures_are_input_errors(raw):
    with pytest.raises(VerificationError):
        strict_json(raw)


@pytest.mark.parametrize("value", [float("nan"), 1.25, object(), 2**600])
def test_unknown_inexact_or_oversized_values_not_stringified(value):
    with pytest.raises(VerificationError):
        canonical_json({"x": value})


def test_limits_shapes_cycles_free_indices_and_witness_assumptions():
    p, _ = approved()
    for change in (
        lambda b: b["expression"].update(op="exec"),
        lambda b: b["symbols"].update(W="vector"),
        lambda b: b["definitions"].update(W={"op": "symbol", "name": "W"}),
        lambda b: b["expression"].update(extra=True),
    ):
        b = copy.deepcopy(p["body"])
        change(b)
        with pytest.raises(VerificationError):
            proposition(b)
    b = copy.deepcopy(golden_proposition("heteroscedastic_covariance")["body"])
    b["instance"]["values"]["Omega"][0][1] = 1
    with pytest.raises(VerificationError, match="diagonal"):
        proposition(b)
    b["instance"]["n"] = 1000
    with pytest.raises(VerificationError):
        proposition(b)
    with pytest.raises(VerificationError):
        strict_json('"' + "x" * 40000 + '"')


def test_evidence_import_replays_instead_of_trusting_verified_boolean():
    p, bound = approved("false_diagonal_formula")
    result = bound.verify(**request_arguments(p))
    assert bound.check_evidence(json.loads(json.dumps(result))) == result
    for key, value in (("claim_holds", True), ("proposition_id", "0" * 64), ("evidence_digest", "a" * 64)):
        altered = copy.deepcopy(result)
        altered[key] = value
        with pytest.raises(VerificationError):
            bound.check_evidence(altered)
    altered = copy.deepcopy(result)
    altered["evidence"]["right"] = 0
    with pytest.raises(VerificationError):
        bound.check_evidence(altered)


def test_approved_review_identity_is_part_of_evidence(tmp_path):
    p, _ = approved()
    path = tmp_path / "request.json"
    path.write_text(canonical_json({"proposition": p, "review": operator_review(p, "operator A")}))
    bound = BoundRequest.from_file(path)
    old = bound.verify(**request_arguments(p))
    path.write_text(canonical_json({"proposition": p, "review": operator_review(p, "operator B")}))
    new = bound.verify(**request_arguments(p))
    assert old["proposition_id"] == new["proposition_id"]
    assert old["review_digest"] != new["review_digest"]
    assert old["evidence_digest"] != new["evidence_digest"]
    with pytest.raises(VerificationError):
        bound.check_evidence(old)


@pytest.mark.parametrize(
    "case,node",
    [
        ("zero_diagonal", {"op": "symbol", "name": []}),
        (
            "centering_independence",
            {"op": "independent_components", "transform": {"op": "identity"}, "errors": []},
        ),
    ],
)
def test_unhashable_symbol_inputs_are_typed_errors(case, node):
    b = copy.deepcopy(golden_proposition(case)["body"])
    b["expression"] = node
    with pytest.raises(VerificationError):
        proposition(b)


def test_large_pinned_file_rejected(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_text(" " * 40000)
    with pytest.raises(VerificationError):
        BoundRequest.from_file(path)


@pytest.mark.parametrize("case", ["zero_diagonal", "row_sum", "weighted_squares", "centering_idempotence"])
def test_fresh_identity_and_new_review_do_not_authorize_a_changed_proof(case):
    p, _ = approved(case)
    for field in ("operator", "premise", "transpose"):
        b = copy.deepcopy(p["body"])
        if field == "operator":
            b["expression"]["op"] = "subtract"  # ill-typed proposition, not a new proof
        elif field == "premise":
            if b["assumptions"]:
                b["assumptions"] = []
            else:
                b["definitions"]["M"] = {"op": "identity"}
        else:
            b["expression"]["left"] = {
                "op": "transpose",
                "arg": {"op": "symbol", "name": "W" if "W" in b["symbols"] else "M"},
            }
        try:
            changed = proposition(b)
        except VerificationError:
            continue
        result = BoundRequest.bind(changed, operator_review(changed, "operator")).verify(
            **request_arguments(changed)
        )
        assert result["verification_status"] != "proved"


def test_output_budget_failure_is_a_typed_result(monkeypatch):
    import census.verification as v

    p, bound = approved()
    real = v._mathematics

    def oversized(record):
        status, evidence = real(record)
        evidence["oversized"] = "x" * (v.MAX_BYTES + 1)
        return status, evidence

    monkeypatch.setattr(v, "_mathematics", oversized)
    result = bound.verify(**request_arguments(p))
    assert not result["success"] and result["error_code"] == "VERIFICATION_LIMIT"
    assert result["claim_holds"] is None
