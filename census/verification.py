# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded exact matrix evidence. No expression strings, services or proof search.

The trusted caller pins a proposition AND an operator review outside tool
arguments. Review attestations assume trusted local storage; they are not
authentication. Four explicit indexed-algebra rules prove only their exact
typed expressions. Exact witnesses refute those expressions, never a nearby
theorem. This is a small checked-rule domain, not a general theorem prover.
"""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
METHOD_VERSION = "bounded-matrix-1"
DOMAIN = "matrix_identity_v1"
MAX_BYTES, MAX_NODES, MAX_DEPTH, MAX_DIMENSION, MAX_BITS = 32768, 128, 16, 16, 512
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")


class VerificationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fail(message, code="VERIFICATION_SCHEMA"):
    raise VerificationError(code, message)


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            _fail(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _plain(value, depth=0):
    if depth > MAX_DEPTH + 8:
        _fail("JSON nesting limit exceeded", "VERIFICATION_LIMIT")
    if value is None or type(value) in (bool, str):
        return
    if type(value) is int:
        if value.bit_length() > MAX_BITS:
            _fail("integer bit limit exceeded", "VERIFICATION_LIMIT")
        return
    if type(value) is list:
        for item in value:
            _plain(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _plain(item, depth + 1)
        return
    _fail("only explicit JSON values and integer/rational mathematics are accepted")


def canonical_json(value):
    _plain(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError:
        _fail("JSON strings must contain valid UTF-8 Unicode scalars")
    if size > MAX_BYTES:
        _fail("JSON byte limit exceeded", "VERIFICATION_LIMIT")
    return raw


def strict_json(raw):
    try:
        if (
            not isinstance(raw, (str, bytes))
            or len(raw.encode() if isinstance(raw, str) else raw) > MAX_BYTES
        ):
            _fail("JSON byte limit exceeded", "VERIFICATION_LIMIT")
        obj = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=lambda s: _fail(f"nonfinite number {s}")
        )
        canonical_json(obj)
        return obj
    except VerificationError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        _fail(f"invalid JSON: {exc}")


def _digest(kind, value):
    return hashlib.sha256((kind + ":v1\0" + canonical_json(value)).encode("utf-8")).hexdigest()


def _fields(obj, required, optional=()):
    if type(obj) is not dict or set(obj) - set(required) - set(optional) or set(required) - set(obj):
        _fail(f"expected fields {sorted(required)}, optional {sorted(optional)}")


def _rational(value):
    if type(value) is int:
        result = Fraction(value)
    elif type(value) is dict and set(value) == {"numerator", "denominator"}:
        a, b = value["numerator"], value["denominator"]
        if type(a) is not int or type(b) is not int or not b:
            _fail("rational requires integer numerator and nonzero integer denominator")
        result = Fraction(a, b)
    else:
        _fail("expected exact integer or rational")
    if max(result.numerator.bit_length(), result.denominator.bit_length()) > MAX_BITS:
        _fail("rational bit limit exceeded", "VERIFICATION_LIMIT")
    return result


def _exact(value):
    if isinstance(value, Fraction):
        return (
            value.numerator
            if value.denominator == 1
            else {"numerator": value.numerator, "denominator": value.denominator}
        )
    return [_exact(x) for x in value]


def _normalize_literals(value):
    if type(value) is dict:
        if set(value) == {"numerator", "denominator"}:
            return _exact(_rational(value))
        return {k: _normalize_literals(v) for k, v in value.items()}
    if type(value) is list:
        return [_normalize_literals(x) for x in value]
    return value


def _validate(body):
    canonical_json(body)
    _fields(
        body,
        {
            "schema_version",
            "domain",
            "original_text",
            "expression",
            "symbols",
            "definitions",
            "assumptions",
            "quantifiers",
            "provenance",
        },
        {"instance"},
    )
    if type(body["schema_version"]) is not int or body["schema_version"] != 1 or body["domain"] != DOMAIN:
        _fail("unsupported schema/domain")
    if type(body["original_text"]) is not str or not body["original_text"].strip():
        _fail("original_text is required")
    symbols, definitions = body["symbols"], body["definitions"]
    if type(symbols) is not dict or not symbols or len(symbols) > 12:
        _fail("provide 1..12 typed symbols")
    for name, kind in symbols.items():
        if not _NAME.fullmatch(name) or kind not in ("matrix", "vector", "scalar", "errors"):
            _fail("invalid symbol name/type")
    if type(definitions) is not dict or set(definitions) - set(symbols):
        _fail("definitions must name declared symbols")
    q = body["quantifiers"]
    _fields(q, {"scope", "min_n", "indices"})
    if q["scope"] not in ("universal", "instance") or type(q["min_n"]) is not int or q["min_n"] < 1:
        _fail("scope must be universal or instance, with positive min_n")
    if (
        type(q["indices"]) is not list
        or any(x not in ("i", "j") for x in q["indices"])
        or len(set(q["indices"])) != len(q["indices"])
    ):
        _fail("invalid quantified indices")
    if type(body["provenance"]) is not dict:
        _fail("provenance must be a JSON object")
    assumptions = body["assumptions"]
    if type(assumptions) is not list:
        _fail("assumptions must be a list")
    seen = set()
    for a in assumptions:
        _fields(a, {"kind", "symbol"})
        kinds = {
            "diagonal": "matrix",
            "zero_diagonal": "matrix",
            "symmetric": "matrix",
            "independent": "errors",
            "finite_variance": "errors",
            "equal_positive_variance": "errors",
        }
        if (
            type(a["kind"]) is not str
            or type(a["symbol"]) is not str
            or a["kind"] not in kinds
            or symbols.get(a["symbol"]) != kinds[a["kind"]]
        ):
            _fail("unknown assumption or incompatible symbol")
        key = canonical_json(a)
        if key in seen:
            _fail("duplicate assumption")
        seen.add(key)
    count = [0]

    def shape(node, bound, stack=(), depth=0):
        count[0] += 1
        if count[0] > MAX_NODES or depth > MAX_DEPTH:
            _fail("expression limit exceeded", "VERIFICATION_LIMIT")
        if type(node) is not dict or type(node.get("op")) is not str:
            _fail("typed expression node required")
        op = node["op"]
        if op == "symbol":
            _fields(node, {"op", "name"})
            name = node["name"]
            if type(name) is not str or name not in symbols or name in stack:
                _fail("unknown/cyclic symbol")
            expected = {"matrix": ("n", "n"), "vector": ("n", 1), "errors": ("n", 1), "scalar": ()}[
                symbols[name]
            ]
            if (
                name in definitions
                and shape(definitions[name], bound, stack + (name,), depth + 1) != expected
            ):
                _fail("definition shape mismatch")
            return expected
        if op in ("identity", "ones", "dimension", "zero_vector"):
            _fields(node, {"op"})
            return {"identity": ("n", "n"), "ones": ("n", 1), "dimension": (), "zero_vector": ("n", 1)}[op]
        if op == "scalar":
            _fields(node, {"op", "value"})
            _rational(node["value"])
            return ()
        if op in ("transpose", "diag", "evaluate"):
            _fields(node, {"op", "arg"})
            a = shape(node["arg"], bound, stack, depth + 1)
            if len(a) != 2 or (op == "diag" and a[0] != a[1]):
                _fail("invalid matrix shape")
            return (a[1], a[0]) if op == "transpose" else (("n", 1) if op == "diag" else a)
        if op == "entry":
            _fields(node, {"op", "matrix", "row", "column"})
            a = shape(node["matrix"], bound, stack, depth + 1)
            if len(a) != 2:
                _fail("entry needs a matrix/vector")
            for index, size in ((node["row"], a[0]), (node["column"], a[1])):
                if type(index) is str and (index not in bound or size == 1):
                    _fail("free/unbound index")
                if type(index) is not str and (
                    type(index) is not int or index < 1 or (size == 1 and index != 1)
                ):
                    _fail("indices are one-based positive integers or bound symbols")
                if type(index) is int and size == "n" and index > q["min_n"] and q["scope"] == "universal":
                    _fail("fixed index not valid for every quantified dimension")
            return ()
        if op == "sum":
            _fields(node, {"op", "index", "body"})
            index = node["index"]
            if index not in ("i", "j") or index in bound:
                _fail("invalid/captured summation index")
            if shape(node["body"], bound | {index}, stack, depth + 1) != ():
                _fail("indexed sum requires scalar body")
            return ()
        if op == "independent_components":
            _fields(node, {"op", "transform", "errors"})
            if (
                type(node["errors"]) is not str
                or symbols.get(node["errors"]) != "errors"
                or shape(node["transform"], bound, stack, depth + 1) != ("n", "n")
            ):
                _fail("independence requires declared errors and square transform")
            return "bool"
        if op in ("matmul", "add", "subtract", "scalar_multiply", "divide", "scale", "equality"):
            _fields(node, {"op", "left", "right"})
            a, b = (shape(node[k], bound, stack, depth + 1) for k in ("left", "right"))
            if op == "matmul" and len(a) == len(b) == 2 and a[1] == b[0]:
                return (a[0], b[1])
            if op in ("add", "subtract", "equality") and a == b and a != "bool":
                return "bool" if op == "equality" else a
            if op in ("scalar_multiply", "divide") and a == b == ():
                return ()
            if op == "scale" and a == () and len(b) == 2:
                return b
            _fail("operator shape mismatch")
        _fail(f"unsupported syntax node {op!r}")

    for name in definitions:
        shape({"op": "symbol", "name": name}, set(q["indices"]))
    result = shape(body["expression"], set(q["indices"]))
    if body["expression"]["op"] != "evaluate" and result != "bool":
        _fail("a proposition must be equality, independence, or a concrete evaluation")
    if q["scope"] == "instance":
        if q["indices"]:
            _fail("concrete evaluations cannot have unresolved quantified indices")
        inst = body.get("instance")
        _fields(inst, {"n", "values"})
        n = inst["n"]
        if type(n) is not int or not q["min_n"] <= n <= MAX_DIMENSION:
            _fail("concrete dimension limit/constraint violated", "VERIFICATION_LIMIT")
        _environment(body, inst)
    elif "instance" in body:
        _fail("universal propositions cannot silently bind a finite instance")
    return body


def proposition(body):
    """Validate and freeze a proposition; this does not approve its prose mapping."""
    canonical_json(body)
    body = copy.deepcopy(body)
    for field in ("expression", "definitions", "instance"):
        if field in body:
            body[field] = _normalize_literals(body[field])
    _validate(body)
    return {
        "body": body,
        "proposition_id": _digest("proposition", body),
        "expression_digest": _digest(
            "expression",
            {k: body[k] for k in ("domain", "expression", "definitions", "symbols", "quantifiers")},
        ),
        "assumptions_digest": _digest("assumptions", body["assumptions"]),
    }


def validate_record(record):
    _fields(record, {"body", "proposition_id", "expression_digest", "assumptions_digest"})
    expected = proposition(record["body"])
    if record != expected:
        _fail("proposition identity does not match its body", "PROPOSITION_MISMATCH")
    return expected


def operator_review(record, reviewer, decision="approved"):
    """Supervised LOCAL attestation constructor. Never expose as a model tool.

    Caller/storage access is the trust boundary, not the reviewer string.
    """
    validate_record(record)
    if type(reviewer) is not str or not reviewer.strip() or decision not in ("approved", "revoked"):
        _fail("review requires a named operator and approved/revoked decision")
    return {
        "policy_version": 1,
        "proposition_id": record["proposition_id"],
        "reviewer": reviewer,
        "decision": decision,
    }


def _mat(n, m, fn):
    return [[fn(i, j) for j in range(m)] for i in range(n)]


def _mul(a, b):
    if len(a[0]) != len(b):
        _fail("concrete matrix shape mismatch")
    return _mat(
        len(a), len(b[0]), lambda i, j: _checked(sum((a[i][k] * b[k][j] for k in range(len(b))), Fraction()))
    )


def _transpose(a):
    return [list(x) for x in zip(*a)]


def _checked(x):
    _rational(_exact(x))
    return x


def _environment(body, instance):
    n, values = instance["n"], instance["values"]
    if type(values) is not dict or set(values) != set(body["symbols"]) - set(body["definitions"]):
        _fail("concrete values must cover exactly the undefined declared symbols")
    env = {}
    for name, raw in values.items():
        kind = body["symbols"][name]
        if kind == "scalar":
            env[name] = _rational(raw)
        else:
            cols = n if kind == "matrix" else 1
            if (
                type(raw) is not list
                or len(raw) != n
                or any(type(row) is not list or len(row) != cols for row in raw)
            ):
                _fail("concrete symbol dimensions mismatch")
            env[name] = [[_rational(x) for x in row] for row in raw]
    for a in body["assumptions"]:
        x = env.get(a["symbol"])
        if x is None:
            _fail("assumptions on defined symbols unsupported for concrete inputs")
        if a["kind"] == "diagonal" and any(x[i][j] for i in range(n) for j in range(n) if i != j):
            _fail("witness violates diagonal assumption")
        if a["kind"] == "zero_diagonal" and any(x[i][i] for i in range(n)):
            _fail("witness violates zero-diagonal assumption")
        if a["kind"] == "symmetric" and x != _transpose(x):
            _fail("witness violates symmetric assumption")
        if body["symbols"][a["symbol"]] == "errors":
            _fail("error distributions require the dedicated checked independence rule")
    return env


def _eval(node, body, n, env, indices):
    op = node["op"]
    if op == "symbol":
        name = node["name"]
        return env[name] if name in env else _eval(body["definitions"][name], body, n, env, indices)
    if op == "scalar":
        return _rational(node["value"])
    if op == "dimension":
        return Fraction(n)
    if op == "identity":
        return _mat(n, n, lambda i, j: Fraction(i == j))
    if op == "ones":
        return [[Fraction(1)] for _ in range(n)]
    if op == "zero_vector":
        return [[Fraction(0)] for _ in range(n)]
    if op in ("transpose", "diag", "evaluate"):
        a = _eval(node["arg"], body, n, env, indices)
        return _transpose(a) if op == "transpose" else ([[a[i][i]] for i in range(n)] if op == "diag" else a)
    if op == "entry":
        a = _eval(node["matrix"], body, n, env, indices)
        i, j = [indices[v] if type(v) is str else v - 1 for v in (node["row"], node["column"])]
        if not (0 <= i < len(a) and 0 <= j < len(a[0])):
            _fail("concrete index out of range")
        return a[i][j]
    if op == "sum":
        return _checked(
            sum(
                (_eval(node["body"], body, n, env, dict(indices, **{node["index"]: j})) for j in range(n)),
                Fraction(),
            )
        )
    a, b = [_eval(node[k], body, n, env, indices) for k in ("left", "right")]
    if op == "equality":
        return a == b
    if op == "matmul":
        return _mul(a, b)
    if op == "scale":
        return [[_checked(a * x) for x in row] for row in b]
    if op == "scalar_multiply":
        return _checked(a * b)
    if op == "divide":
        if not b:
            _fail("division by zero")
        return _checked(a / b)
    if isinstance(a, Fraction):
        return _checked(a + b if op == "add" else a - b)
    return [[_checked(x + y if op == "add" else x - y) for x, y in zip(ar, br)] for ar, br in zip(a, b)]


def _s(name):
    return {"op": "symbol", "name": name}


def _u(op, arg):
    return {"op": op, "arg": arg}


def _b(op, left, right):
    return {"op": op, "left": left, "right": right}


def _entry(matrix, row="i", column="j"):
    return {"op": "entry", "matrix": matrix, "row": row, "column": column}


def _centering():
    return _b(
        "subtract",
        {"op": "identity"},
        _b(
            "scale",
            _b("divide", {"op": "scalar", "value": 1}, {"op": "dimension"}),
            _b("matmul", {"op": "ones"}, _u("transpose", {"op": "ones"})),
        ),
    )


def golden_proposition(case, original_text=None):
    """Construct explicit sample records for supervised inspection, NOT approvals."""
    w, o, m = _s("W"), _s("Omega"), _s("M")
    wo = _b("matmul", w, o)
    rowsum = {"op": "sum", "index": "j", "body": _b("scalar_multiply", _entry(w), _entry(o, "j", "j"))}
    exprs = {
        "zero_diagonal": _b("equality", _u("diag", wo), {"op": "zero_vector"}),
        "false_diagonal_formula": _b("equality", _entry(wo, "i", "i"), rowsum),
        "row_sum": _b("equality", _entry(_b("matmul", wo, {"op": "ones"}), "i", 1), rowsum),
        "weighted_squares": _b(
            "equality",
            _entry(_b("matmul", wo, _u("transpose", w)), "i", "i"),
            {
                "op": "sum",
                "index": "j",
                "body": _b(
                    "scalar_multiply", _b("scalar_multiply", _entry(w), _entry(w)), _entry(o, "j", "j")
                ),
            },
        ),
        "centering_idempotence": _b("equality", _b("matmul", m, m), m),
        "centering_independence": {"op": "independent_components", "transform": m, "errors": "eps"},
        "heteroscedastic_covariance": _u("evaluate", _b("matmul", _b("matmul", m, o), _u("transpose", m))),
    }
    if case not in exprs:
        _fail("unknown example")
    centered = case.startswith("centering") or case == "heteroscedastic_covariance"
    symbols = {"M": "matrix"} if centered else {"W": "matrix", "Omega": "matrix"}
    assumptions = []
    if not centered:
        assumptions = [{"kind": "diagonal", "symbol": "Omega"}]
        if case in ("zero_diagonal", "false_diagonal_formula"):
            assumptions.append({"kind": "zero_diagonal", "symbol": "W"})
    if case == "centering_independence":
        symbols["eps"] = "errors"
        assumptions = [
            {"kind": k, "symbol": "eps"}
            for k in ("independent", "finite_variance", "equal_positive_variance")
        ]
    body = {
        "schema_version": 1,
        "domain": DOMAIN,
        "original_text": original_text or case,
        "expression": exprs[case],
        "symbols": symbols,
        "definitions": {"M": _centering()} if centered else {},
        "assumptions": assumptions,
        "quantifiers": {
            "scope": "universal",
            "min_n": 2 if case == "centering_independence" else 1,
            "indices": ["i"] if case in ("false_diagonal_formula", "row_sum", "weighted_squares") else [],
        },
        "provenance": {},
    }
    if case == "heteroscedastic_covariance":
        symbols["Omega"] = "matrix"
        body["assumptions"] = [{"kind": "diagonal", "symbol": "Omega"}]
        body["quantifiers"]["scope"] = "instance"
        body["instance"] = {"n": 2, "values": {"Omega": [[2, 0], [0, 3]]}}
    return proposition(body)


def _mathematics(record):
    body = record["body"]
    if body["expression"]["op"] == "evaluate" and body["quantifiers"]["scope"] == "instance":
        n = body["instance"]["n"]
        env = _environment(body, body["instance"])
        return "diagnostic_only", {
            "kind": "exact_instance",
            "value": _exact(_eval(body["expression"], body, n, env, {})),
            "reason": "exact finite calculation; no universal proof",
        }
    for case in (
        "zero_diagonal",
        "row_sum",
        "weighted_squares",
        "centering_idempotence",
        "false_diagonal_formula",
        "centering_independence",
    ):
        expected = golden_proposition(case)["body"]
        # No name or natural-language similarity controls mathematical selection.
        # All typed premises/definitions/quantifiers must match this checked rule.
        if any(
            body[k] != expected[k]
            for k in ("expression", "symbols", "definitions", "assumptions", "quantifiers")
        ):
            continue
        if case == "false_diagonal_formula":
            inst = {"n": 2, "values": {"W": [[0, 1], [0, 0]], "Omega": [[2, 0], [0, 3]]}}
            env = _environment(body, inst)
            lhs = _eval(body["expression"]["left"], body, 2, env, {"i": 0})
            rhs = _eval(body["expression"]["right"], body, 2, env, {"i": 0})
            if lhs == rhs:
                _fail("counterexample check failed", "EVIDENCE_INVALID")
            return "refuted", {
                "kind": "exact_counterexample",
                "instance": inst,
                "indices": {"i": 1},
                "left": _exact(lhs),
                "right": _exact(rhs),
            }
        if case == "centering_independence":
            transform = _eval(body["expression"]["transform"], body, 2, {}, {})
            covariance = _mul(transform, _transpose(transform))
            if not covariance[0][1]:
                _fail("dependence witness check failed", "EVIDENCE_INVALID")
            return "refuted", {
                "kind": "exact_covariance_counterexample",
                "n": 2,
                "distribution": "independent Rademacher errors (+/-1 with probability 1/2)",
                "covariance": _exact(covariance),
                "rule": "finite second moments: independence implies zero covariance",
            }
        derivations = {
            "zero_diagonal": [
                "(W Omega)[i,i] = sum_j W[i,j] Omega[j,i]",
                "Omega diagonal: only j=i remains",
                "W[i,i] Omega[i,i] = 0",
            ],
            "row_sum": [
                "(W Omega 1)[i] = sum_j sum_k W[i,k] Omega[k,j]",
                "Omega diagonal collapses k=j: sum_j W[i,j] Omega[j,j]",
            ],
            "weighted_squares": [
                "(W Omega W^T)[i,i] = sum_j sum_k W[i,j] Omega[j,k] W[i,k]",
                "Omega diagonal collapses k=j: sum_j W[i,j]^2 Omega[j,j]",
            ],
            "centering_idempotence": ["J=11^T; J^2=n J; n>=1", "(I-J/n)^2=I-2J/n+nJ/n^2=I-J/n"],
        }
        return "proved", {
            "kind": "checked_index_derivation",
            "rule": case,
            "steps": derivations[case],
            "scope": "exact matched typed expression and premises; no general proof search",
        }
    return "inconclusive", {"kind": "unsupported", "reason": "no checked rule for this exact proposition"}


@dataclass(frozen=True)
class BoundRequest:
    """Immutable host-pinned record plus trusted local review, never tool arguments."""

    record_json: str
    review_json: str
    source_path: Optional[str] = None

    @classmethod
    def bind(cls, record, review=None):
        validate_record(record)
        return cls(canonical_json(record), canonical_json(review))

    @classmethod
    def from_file(cls, path):
        with open(path, "rb") as source:
            envelope = strict_json(source.read(MAX_BYTES + 1))
        _fields(envelope, {"proposition", "review"})
        bound = cls.bind(envelope["proposition"], envelope["review"])
        return cls(bound.record_json, bound.review_json, str(Path(path).resolve()))

    def verify(self, proposition_id, expression_digest, assumptions_digest, schema_version=1):
        record = strict_json(self.record_json)
        base = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "proposition_id": record["proposition_id"],
            "expression_digest": record["expression_digest"],
            "assumptions_digest": record["assumptions_digest"],
            "proposition": record["body"],
            "formalization_status": "pending_review",
        }
        try:
            validate_record(record)
            if (
                type(schema_version) is not int
                or schema_version != 1
                or (proposition_id, expression_digest, assumptions_digest)
                != (record["proposition_id"], record["expression_digest"], record["assumptions_digest"])
            ):
                _fail("request does not match the host-pinned proposition", "PROPOSITION_MISMATCH")
            review = strict_json(self.review_json)
            if self.source_path:
                current = BoundRequest.from_file(self.source_path)
                if current.record_json != self.record_json:
                    _fail(
                        "operator changed the pinned proposition; rebind explicitly", "PROPOSITION_MISMATCH"
                    )
                review = strict_json(current.review_json)
            if review is not None:
                _fields(review, {"policy_version", "proposition_id", "reviewer", "decision"})
                if (
                    type(review["policy_version"]) is not int
                    or review["policy_version"] != 1
                    or review["proposition_id"] != record["proposition_id"]
                ):
                    _fail("stale or mismatched formalization review", "FORMALIZATION_REVIEW_REQUIRED")
                if (
                    type(review["reviewer"]) is not str
                    or not review["reviewer"].strip()
                    or review["decision"] not in ("approved", "revoked")
                ):
                    _fail("invalid operator review", "FORMALIZATION_REVIEW_REQUIRED")
            if review is None or review["decision"] != "approved":
                status, evidence = (
                    "inconclusive",
                    {"kind": "review_required", "reason": "supervised formalization review required"},
                )
            else:
                base["formalization_status"] = "approved"
                status, evidence = _mathematics(record)
            base["formalization_review"] = review
            base["review_digest"] = _digest("formalization-review", review)
            base.update(
                success=True,
                verification_status=status,
                claim_holds={"proved": True, "refuted": False}.get(status),
                evidence_verified=status in ("proved", "refuted"),
                evidence=evidence,
            )
        except (VerificationError, OSError) as exc:
            base.update(
                success=False,
                verification_status="error",
                claim_holds=None,
                evidence_verified=False,
                error_code=getattr(exc, "code", "EVIDENCE_INVALID"),
                error=str(exc),
            )
        try:
            base["evidence_digest"] = _digest("verification-evidence", base)
        except VerificationError:
            # A bounded input can expand during exact evaluation. Never return
            # a partial proof or allow an output-budget exception to escape.
            base = {
                "schema_version": 1,
                "method_version": METHOD_VERSION,
                **{k: record[k] for k in ("proposition_id", "expression_digest", "assumptions_digest")},
                "success": False,
                "verification_status": "error",
                "claim_holds": None,
                "evidence_verified": False,
                "error_code": "VERIFICATION_LIMIT",
                "error": "verification output exceeds the exact JSON resource budget",
            }
            base["evidence_digest"] = _digest("verification-evidence", base)
        return base

    def check_evidence(self, result):
        """Recompute evidence against the trusted context, not a file's verified flag."""
        record = strict_json(self.record_json)
        actual = self.verify(
            record["proposition_id"], record["expression_digest"], record["assumptions_digest"]
        )
        if canonical_json(result) != canonical_json(actual):
            _fail("evidence does not replay against the pinned proposition/review", "EVIDENCE_INVALID")
        return actual


def request_arguments(record):
    validate_record(record)
    return {k: record[k] for k in ("proposition_id", "expression_digest", "assumptions_digest")}
