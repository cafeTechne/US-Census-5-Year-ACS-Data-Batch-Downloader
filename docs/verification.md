# Exact proposition evidence (Round 17)

This opt-in local capability checks a small set of typed matrix statements.
It does not translate arbitrary prose, search for theorems, run model code,
or establish that an empirical study is confirmatory.

The supervised caller pins the exact proposition before tool selection. An
LLM choosing the name of a related true theorem cannot change that request.
The original text, typed expression, definitions, symbols, assumptions,
quantifiers and immutable provenance are hashed separately from a research
manifest. Expression and assumption digests are also returned. Equivalent
but differently written expressions need not have the same identity.

## Supervised local example

Use the existing editable installation. No extra dependencies or model
runtime are needed. Choose a new working directory outside existing results.
First construct a **pending** request and inspect its entire `body`:

```python
from pathlib import Path
from census.verification import canonical_json, golden_proposition, request_arguments

record = golden_proposition("false_diagonal_formula", original_text=(
    "For every zero-diagonal square W and diagonal Omega, "
    "is (W Omega)[i,i] equal to sum_j W[i,j] Omega[j,j]?"
))
print(canonical_json(record["body"]))
Path("request.json").write_text(canonical_json({"proposition": record, "review": None}), encoding="utf-8")
print(canonical_json(request_arguments(record)))  # exact tool arguments
```

Only after the operator has reviewed that the expression and assumptions
faithfully represent the question, perform this separate local action:

```python
from pathlib import Path
from census.verification import strict_json, canonical_json, operator_review

path = Path("request.json")
request = strict_json(path.read_text(encoding="utf-8"))
request["review"] = operator_review(request["proposition"], reviewer="local operator")
path.write_text(canonical_json(request), encoding="utf-8")
```

This records an operator attestation, not a digital signature or independent
authentication. Keep the request under trusted operator control and outside
the verification client's write permissions. Do not expose the approval
constructor through model tools. A reviewer string, hash, or `approved=true`
field submitted by a model does not create approval.

Set `CENSUS_VERIFICATION_REQUEST` to the absolute path of `request.json`
**before starting the process**. Use your shell's environment-setting syntax.
The filename is not a tool argument. The CLI shares the ordinary tool route:

```text
python -m census tool verify_proposition '<exact JSON tool arguments printed above>' --json
python mcp_server.py --profile verification
```

The `verification` profile exposes only this tool; `core` remains unchanged.
The full profile also discovers it, but cannot certify anything without the
operator's request. The response has a canonical `verification` record and
transport-level summary/next_step. For this false claim, success is true
because the check completed, status is `refuted`, claim_holds is false,
and evidence_verified is true. W=[[0,1],[0,0]], Omega=diag(2,3), i=1 gives
left=0 and right=3. This is not a substituted proof of diag(W Omega)=0.
Failures also contain a nested verification error. If no evidence record
could be produced, identity_available is false and no identity is fabricated.

These related statements require different proposition identities. Under
the same assumptions (square zero-diagonal W, diagonal Omega), compare:

| Exact statement, for every valid i | Reviewed result | Evidence |
| :--- | :--- | :--- |
| `(W Omega)[i,i] = 0` | `proved`, `claim_holds=true` | Only the diagonal product `W[i,i] Omega[i,i]` remains, and `W[i,i]=0` |
| `(W Omega)[i,i] = sum_j W[i,j] Omega[j,j]` | `refuted`, `claim_holds=false` | W=[[0,1],[0,0]], Omega=diag(2,3), i=1 gives left=0 and right=3 |

Both checks can complete with `success=true`; success describes the check,
not the truth of the statement. Without a valid operator review, both stay
`inconclusive` with `claim_holds=null`. Supplying the first statement's IDs
to a process pinned to the second produces `PROPOSITION_MISMATCH`, even
though the first statement is true. Changing source text or provenance can
also change the full identity: obtain IDs from the actual pinned record.

The library route makes the separate trusted context explicit:

```python
from census.verification import BoundRequest, request_arguments
bound = BoundRequest.from_file("request.json")
result = bound.verify(**request_arguments(request["proposition"]))
bound.check_evidence(result)  # replay, not trust a saved verified Boolean
```

The running workflow pins the first operator-supplied proposition. Changing
the file's proposition requires a restart/rebind. Its review is rechecked
on every call; a `revoked` decision immediately prevents certification.
Changing reviewer identity changes the review/evidence digests. Deleting
or corrupting the request fails closed.

## Evidence and scope

- `proved`: an exact checked rule matches the entire expression and premises.
- `refuted`: an exact valid witness contradicts that same universal claim.
- `diagnostic_only`: finite exact calculation, not a universal proof.
- `inconclusive`: unsupported rule or required formalization review pending.
- `error`: schema, identity, evidence, resource, or file failure.

Only proved/refuted records have evidence_verified=true. Unreviewed
translations remain pending even if a similar formula is provable. A final
LLM narrative is not authoritative: consumers must match its references
and display the bound statement. Zero covariance does not prove independence.

Checked rules cover the zero diagonal of W Omega; its row sum; the diagonal
of W Omega W-transpose; centering idempotence; the false diagonal formula
counterexample; and a centering-independence counterexample using independent
finite-variance Rademacher errors. Rules match the exact typed AST/premises,
not prose similarity or a claim label. This is a small trusted algebra-rule
implementation, not a formally verified general proof kernel. Instance
expressions use exact rational arithmetic. `heteroscedastic_covariance`
computes one M Omega M-transpose and deliberately returns diagnostic evidence.

Inputs are strict JSON: no duplicate keys, nonfinite/float mathematical
values, arbitrary objects, unknown nodes, or executable expression strings.
Rationals use integer numerator/nonzero integer denominator; signs/common
factors normalize. Indices are explicit and one-based. Limits: 32768 JSON
bytes, 128 expression nodes, depth 16, concrete dimension 16, integer/rational
sizes 512 bits. Output-budget failure is a typed error, never partial proof.
Invalid UTF-8 and unpaired Unicode surrogates produce typed schema errors;
valid non-ASCII source text is preserved in the proposition identity.
The MCP schema version is a strict integer: booleans, floats and numeric
strings cannot be coerced into an accepted version. Invalid MCP arguments
produce a transport validation error rather than verification evidence.

## Existing-format bundles and compatibility

Optional output_dir writes a fresh ordinary bundle with
results/verification.json, methods, review/proposition/evidence identity and
file hashes. Evidence is replay-checked immediately before writing. It does
not create or upgrade empirical analysis_status. Import with
BoundRequest.check_evidence against trusted context; a hash alone proves
neither authenticity nor correctness.

All results bundles now require a fresh or empty destination. Reused or
partially populated destinations are rejected before a new run overwrites
them. Use another run directory; no old or unrelated files are deleted.
Table, figure and result names must be single portable filename components;
path separators, drive/stream syntax, reserved Windows names and
case-insensitive collisions within a category are rejected before any bundle
output is written. Spaces and valid non-ASCII labels remain supported.
Final figure filenames are also checked together: distinct labels cannot
overwrite the same file after extensions are applied. Figures rendered for a
bundle use private temporary folders, removed on success or failure, before
being copied into the bundle. A collision leaves the bundle destination
unwritten. Nested payloads named `bundle.json` are hashed; only the root
inventory file excludes itself.
ACS1 analysis inputs retain their product identity and cannot take five-year
VRE uncertainty. Material central-estimate mismatches fail; ordinary
published rounding is retained.

The tag workflow runs mandatory verification tests at the tagged commit
before publishing and rejects failed, missing or skipped required tests.
Ordinary offline CI runs them too. Existing statistical smoke/full/info
study rules remain separate. No model benchmark, remote access, general
symbolic parser or new statistical estimator is introduced here.

## Benchmark protocol checks

`scripts/local_model_eval.py` now preserves malformed or missing tool
arguments as `invalid_tool_arguments`, instead of replacing them with an
empty object. Duplicate keys, non-object JSON and nonfinite numbers
(including overflowing exponents) fail before tool execution. Ordinary
finite research floats remain supported. Recorded results distinguish
`runtime_error`, `turn_limit` and `empty_answer` from argument failures.
Existing tool/argument/answer scores remain separate.

Run `python -m pytest -q tests/test_local_model_eval_protocol.py` for fake
chat/tool regressions; these tests contact no runtime or external service.
They validate transport behavior, not model accuracy or scientific claims.
Original benchmark records are preserved; none of these checks constitutes
a rerun of the saved model comparisons.

## Offline verification transcript evaluation

`scripts/verification_eval.py` prepares a frozen prompt or scores an exact
JSON transcript against a separately supplied operator request. It calls
neither a model runtime nor the MCP server. Use the supervised request above:

```text
python scripts/verification_eval.py --request request.json --out prepared-case.json
python scripts/verification_eval.py --request request.json --transcript trace.json --out score.json
```

Omitting `--transcript` returns the prompt, its digest, checker version,
review digest and expected final fields. Only the `prompt` belongs in a
future model request: `expected_final` is the scoring reference and must
not be sent to the model. Review prompts for answer-revealing labels and use
neutral source questions; the script preserves the pinned source faithfully.
It requires a current approved local review and does not create approvals.
Output files must be new; it refuses to overwrite a request or prior result.

The transcript has exactly three fields. The final answer remains a raw
string so malformed or truncated model output can be scored without being
silently repaired:

```json
{
  "schema_version": 1,
  "calls": [
    {
      "tool": "verify_proposition",
      "arguments": {"proposition_id": "...", "expression_digest": "...", "assumptions_digest": "..."},
      "result": {"verification": {}}
    }
  ],
  "final": "{\"proposition_id\":\"...\",\"evidence_digest\":\"...\",\"verification_status\":\"refuted\",\"claim_holds\":false}"
}
```

Replace placeholders with actual IDs and the complete unmodified evidence
record. A call's result can also be the bare evidence record; arguments can
be a JSON object or the original JSON string. At most eight call records
are accepted, also subject to the core's 32 KiB JSON limit. `output_dir`
is outside this frozen evaluation protocol. This is a new narrow transcript
contract; old benchmark records are not rewritten or treated as compatible.

Scores separate tool choice, argument parsing, request binding, evidence
replay, duplicate requests, completion, final JSON/schema and exact final
field consistency. A protocol pass requires at least one valid call, no
duplicates and an exact final reference to replayed evidence. A diagnostic
can pass that protocol while `mathematical_evidence_verified` stays false.
Missing or invalid bound evidence cannot acquire mathematical verification
from a correct-looking final answer. Exit codes are 0 for a prepared case
or protocol pass, 1 for a scored failure, and 2 for invalid input/preconditions.

This checks structured-field consistency, not arbitrary narrative meaning,
trace authenticity, formalization fidelity or model reliability. Preserve
raw runtime traces and model/version/settings/latency separately for any
future authorized run; these metrics are not fabricated by offline replay.
Requests/traces are read without modification. Reports retain per-call
error text and identities, not a sanitized replacement for raw traces.
Actual runtime/model preflight and a service transport remain conditional.

### Bounded client controls without a default runtime

The same script exposes `async run_protocol(bound, chat=..., invoke=...,
tools=..., timeout=30)` for an authorized caller to supply two async
transports. No CLI mode selects an endpoint, and there is no default model
or service connection. Tests use fake callbacks only.

- `chat(messages=..., tools=..., timeout=..., final=...)` returns a JSON
  assistant message. The first request discovers tool calls; the second is
  explicitly final, has no tools, and rejects any returned tool call.
- `invoke(name=..., arguments=..., timeout=...)` returns complete structured
  verification evidence or its workflow envelope. Only the host-pinned IDs,
  strict schema version 1 and the verification tool are accepted; no
  `output_dir` or approval argument is dispatched.
- `tools` contains the actual verification tool in OpenAI function format.
  Preflight requires exactly that tool and all three required string IDs.
  The result records its schema digest as well as the prompt digest.

There are at most two model callback invocations and eight tool requests in
the first message. Duplicate requests, including an omitted versus explicit
default schema version, reuse replay-checked evidence and do not invoke the
tool transport again. Review revocation is checked on reuse. Duplicate
requests still count against the model's protocol score; suppression is a
client resource control, not a corrected model performance result.

One shared monotonic deadline covers both model phases and tool execution.
The default is 30 seconds; caller-supplied budgets must be finite, positive
and at most 3600 seconds. Async waits are cancelled at the remaining budget,
which is also passed to the transport. Callbacks must honor cancellation
and enforce that timeout in their underlying I/O. This does not forcibly
terminate blocking native code. Counter fields count attempted injected
callback invocations, not independently observed remote execution.

The returned client envelope keeps the exact transcript, validated JSON
assistant responses, suppression/callback counts, measured local elapsed
time, errors and the separate protocol score. Its elapsed time is ordinary
metadata, not exact mathematical JSON; serialize that envelope with ordinary
JSON, and pass only its `transcript` to the exact offline scorer. Transports
must retain original request/response bytes separately, including rejected
oversized or malformed responses, and record actual model/runtime/version
and settings. The helper does not authenticate a trace or fabricate those
runtime details. No real model timing or accuracy result is claimed by the
fake-transport tests.

Scoring protocol `r17-offline-verification-2` normalizes the optional default
schema version for duplicate counting; transcript input `schema_version`
remains 1. Earlier reports are preserved rather than silently relabelled.

### Explicit local runtime transport

`scripts/verification_live.py` wires the controls to a loopback model endpoint
and a verification-only MCP subprocess. It makes no service calls on import
and refuses execution without `--run-model`. Example after choosing a reviewed
request, exact installed model ID and observed runtime version:

```text
python scripts/verification_live.py --run-model --request request.json --base-url http://127.0.0.1:11434/v1 --model gemma4:e4b --runtime-version "Ollama <version>" --timeout 600 --out-dir new-model-run
```

The exact model ID must appear in `/models` and chat responses must identify
that same model. There is no selection or download fallback. Only HTTP(S)
loopback URLs without URL credentials/query/fragment are accepted; redirects
and environment proxies are disabled. Tool-schema preflight precedes the
catalog query. There is at most one catalog GET and two chat POSTs, without
application retries. Final mode sets `tool_choice=none`. Temperature is 0
and `max_tokens` is 1500; native thinking/context defaults are not overridden.
Optional `--json-final` additionally requests `response_format=json_object`
only for the final phase and records that condition in settings. It does not
change tool binding, evidence replay, or the strict scorer, and has no silent
fallback when unsupported. Compare its results as a separate experimental
condition; it does not guarantee completion within a reasoning-token budget.

The output directory must be new. Request/response entity bodies, HTTP
status/headers, MCP result text, settings, observed model names and the
client report are retained with file hashes. `runtime-version` is labelled
operator-declared; independent runtime observations/model digests should
accompany a study. Bodies are retained without sanitization. Each is bounded
to 1 MiB; oversized responses retain an explicitly marked prefix and fail.
Identity encoding is requested; unexpected compression is retained raw and
rejected. These are entity-body records, not packet captures. Failed parsing
preserves the received body. Unsuccessful attempts must not be repaired or
replaced in place, or counted as mathematical errors without inspecting the
separate preflight, transport, formatting and evidence outcomes.

An outer cooperative timeout covers startup/preflight as well as evaluation.
The transport uses async HTTP and cancellable MCP waits; the limitations on
hard preemption described above still apply.

The completed Round 17 integration study, raw evidence and offline replay
instructions are preserved in the [versioned audit archive](audits/round17_bound_protocol/README.md).
