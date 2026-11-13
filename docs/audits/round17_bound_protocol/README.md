# Round 17 evidence archive

Start with [REPORT.md](REPORT.md) for the actual local-model results and
[validation.json](validation.json) for software checks. This archive copies
the 83 original Round 17 files byte-for-byte from the local ignored literature
directory. It contains no literature PDFs or older benchmark payloads.
Original files remain untouched. `.gitattributes` disables line-ending
conversion so response bodies and checksums survive cross-platform checkouts.

The primary experiment contains four single attempts. All four obtained
correctly bound, replayable engine evidence; none passed strict final-answer
format/completion checks. A separately declared Gemma JSON-mode follow-up
passed. These are integration observations, not reliability estimates or a
controlled comparison with earlier model protocols. Automated sample-template
reviews are not independent human attestations.

`ARCHIVE_SHA256.json` hashes every archived file except itself. Each run's
`run.json` also hashes its own retained payloads. Historical source hashes
identify recorded working-tree states, not separate Git commits. Original
absolute paths in logs are provenance, not portable commands. The 74 older
benchmark hashes preserve the comparison baseline; their ignored payloads
are not required to replay these five new records.

From the repository root, verify the archive without services:

```python
from pathlib import Path
import hashlib
import json

root = Path("docs/audits/round17_bound_protocol")
for name, expected in json.loads((root / "ARCHIVE_SHA256.json").read_text()).items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
```

Replay the archived evidence and strict protocol scores using the existing
editable installation. This creates no approval and contacts no service:

```python
from pathlib import Path
import json
from census.verification import BoundRequest, strict_json
from scripts.verification_eval import score

root = Path("docs/audits/round17_bound_protocol")
requests = {
    strict_json(path.read_bytes())["proposition"]["proposition_id"]: path
    for path in root.glob("*.request.json")
}
for path in sorted(root.glob("*/run.json")):
    run = json.loads(path.read_text(encoding="utf-8"))
    identity = run["prepared_case"]["request_arguments"]["proposition_id"]
    bound = BoundRequest.from_file(requests[identity])
    result = score(bound, run["transcript"])
    assert result["protocol_passed"] == run["protocol_passed"]
    print(path.parent.name, result["protocol_passed"])
```

Expected strict scores: four `False` primary runs and one `True` JSON-mode
follow-up. Do not strip Markdown fences or replace failed final outputs when
replaying those primary scores. Raw entity bodies are unsanitized local model
records; no remote upload or publication occurred during collection.
