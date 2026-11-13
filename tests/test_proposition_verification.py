# SPDX-License-Identifier: AGPL-3.0-or-later
"""Independent finite controls plus exact, narrowly scoped proof-rule evidence."""

from copy import deepcopy
from fractions import Fraction

import pytest

from census.verification import (
    BoundRequest,
    golden_proposition,
    operator_review,
    proposition,
    request_arguments,
)


def checked(record):
    return BoundRequest.bind(record, operator_review(record, "local test operator")).verify(
        **request_arguments(record)
    )


@pytest.mark.parametrize("case", ["zero_diagonal", "row_sum", "weighted_squares", "centering_idempotence"])
def test_exact_typed_rules_prove_only_declared_statement(case):
    record = golden_proposition(case)
    result = checked(record)
    assert result["verification_status"] == "proved"
    assert result["claim_holds"] is True and result["evidence_verified"] is True
    assert result["proposition"] == record["body"]
    assert result["evidence"]["steps"]


def test_false_diagonal_formula_has_its_own_checked_witness():
    record = golden_proposition("false_diagonal_formula")
    result = checked(record)
    assert result["success"] and result["verification_status"] == "refuted"
    assert result["claim_holds"] is False and result["evidence_verified"]
    witness = result["evidence"]
    w, o = witness["instance"]["values"]["W"], witness["instance"]["values"]["Omega"]
    assert all(w[i][i] == 0 for i in range(2))
    assert all(o[i][j] == 0 for i in range(2) for j in range(2) if i != j)
    # Deliberately independent scalar products; no production evaluator.
    lhs = sum(w[0][j] * o[j][0] for j in range(2))
    rhs = sum(w[0][j] * o[j][j] for j in range(2))
    assert (lhs, rhs) == (witness["left"], witness["right"]) == (0, 3)


def test_centering_dependence_witness_from_enumerated_independent_distribution():
    result = checked(golden_proposition("centering_independence"))
    assert result["verification_status"] == "refuted"
    # Enumerate four equally likely outcomes of independent Rademacher errors.
    centered = [(Fraction(x - y, 2), Fraction(y - x, 2)) for x in (-1, 1) for y in (-1, 1)]
    cross_moment = sum(x * y for x, y in centered) / 4
    assert cross_moment == Fraction(-1, 2)
    assert result["evidence"]["covariance"][0][1] == {"numerator": -1, "denominator": 2}


def test_instance_covariance_is_not_universal_proof():
    result = checked(golden_proposition("heteroscedastic_covariance"))
    assert result["verification_status"] == "diagnostic_only"
    assert result["claim_holds"] is None and not result["evidence_verified"]
    # M = [[1/2,-1/2],[-1/2,1/2]], Omega=diag(2,3).
    assert result["evidence"]["value"] == [
        [{"numerator": 5, "denominator": 4}, {"numerator": -5, "denominator": 4}],
        [{"numerator": -5, "denominator": 4}, {"numerator": 5, "denominator": 4}],
    ]


def test_matching_small_numeric_cases_does_not_prove_unsupported_universal_claim():
    body = deepcopy(golden_proposition("centering_idempotence")["body"])
    body["expression"]["left"] = {"op": "transpose", "arg": {"op": "symbol", "name": "M"}}
    # Symmetry is true, but not one of this version's checked universal rules.
    result = checked(proposition(body))
    assert result["verification_status"] == "inconclusive"
    assert result["claim_holds"] is None


def test_independent_finite_positive_controls_for_arbitrary_small_matrices():
    # These validate mathematical rule implementations as finite controls, not proofs.
    for n in range(1, 5):
        w = [[Fraction(0 if i == j else (2 * i - j)) for j in range(n)] for i in range(n)]
        d = [Fraction(j + 1) for j in range(n)]
        wo = [[w[i][j] * d[j] for j in range(n)] for i in range(n)]
        assert all(wo[i][i] == 0 for i in range(n))
        assert all(sum(wo[i]) == sum(w[i][j] * d[j] for j in range(n)) for i in range(n))
        assert all(
            sum(wo[i][j] * w[i][j] for j in range(n)) == sum(w[i][j] ** 2 * d[j] for j in range(n))
            for i in range(n)
        )
        m = [[Fraction(i == j) - Fraction(1, n) for j in range(n)] for i in range(n)]
        assert [[sum(m[i][k] * m[k][j] for k in range(n)) for j in range(n)] for i in range(n)] == m
