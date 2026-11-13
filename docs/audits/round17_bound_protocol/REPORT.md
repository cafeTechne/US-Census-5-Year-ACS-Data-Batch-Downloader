# Round 17 local model integration experiment

Four frozen primary attempts, then one separately declared post-hoc JSON-mode follow-up. This is an integration smoke test, not a reliability estimate or a controlled comparison with earlier benchmark protocols.

Runtime: Ollama 0.40.15, observed through its local version API. Installed model digests were unchanged before and after the experiment. Requests used temperature 0, max_tokens 1500 and a 600-second whole-run budget. Native thinking/context defaults and hardware load were not controlled.

Operator request reviews are automated frozen-template fixtures, not independent human attestations. Models never approved their own requests. No Census endpoint, remote model provider or new dependency was used.

## Frozen primary results

| Model | Claim | Binding and evidence replay | Strict protocol pass | Final-answer observation | Seconds |
| :--- | :--- | :--- | :--- | :--- | ---: |
| gemma4:e4b | false_diagonal_formula | yes | no | Markdown fences; inner fields match (diagnostic only) | 59.1 |
| gemma4:e4b | zero_diagonal | yes | no | Markdown fences; inner fields match (diagnostic only) | 44.8 |
| qwen3.5:4b | false_diagonal_formula | yes | no | Final token limit; no final content | 196.7 |
| qwen3.5:4b | zero_diagonal | yes | no | Final token limit; no final content | 170.8 |

All four primary runs made one correctly bound verification call and obtained independently replayable engine evidence: the false formula was refuted, and the zero-diagonal identity proved. Each used two model requests and one tool invocation.

The strict primary result is 0/2 for each model. Gemma's two answers contain exact correct fields inside Markdown fences, but their raw JSON parsing remains failed. Qwen's two final responses hit finish_reason=length with empty final content; this is completion failure, not an observed wrong mathematical conclusion. The separate content inspection does not relabel parsing or confer human review.

## Separate JSON-mode follow-up

After recording the formatting failures, a single Gemma false-formula attempt requested response_format=json_object only in the final phase. It passed strict parsing, exact evidence references and the false outcome, in 59.091 seconds. The original four attempts and settings remain unchanged. This single success does not establish model reliability or demonstrate that JSON mode fixes Qwen completion.

## Reproduction and retained evidence

- [Prespecified primary study](study.json), [primary summary](summary.json), [content inspection](content-inspection.json).
- [Follow-up declaration and source hashes](json-final-followup-study.json), [follow-up summary](json-final-followup-summary.json).
- Each run directory contains bounded HTTP entity bodies, HTTP metadata, the MCP result and run.json with file hashes. Per-attempt source hashes are retained beside primary runs.
- [Observed initial runtime version](ollama-version.json), [initial model catalog/digests](ollama-tags.json), [final version](ollama-version-after.json), [final catalog/digests](ollama-tags-after.json).
- [Original 74 record hashes](original-record-hashes.json); all still match. Raw traces are unsanitized local records; no failed attempt was overwritten or automatically retried.

Use the structured engine result as mathematical authority. A model final response must still pass binding, replay and reference checks. Treat runtime/preflight failures, completion and formatting separately from scientific correctness.

## Final software validation

Full offline suite: **1476 passed, 37 skipped, 47 subtests passed, no deselections**, 239.72 seconds. Both socket/stdio checks ran outside the sandbox; live Census tests were disabled. The 64 required cases from actual JUnit results passed the embedded release gate locally. All five actual run records replayed and their payload hashes matched. See [validation.json](validation.json). Remote CI and the full supported interpreter/Windows matrix were not run.
