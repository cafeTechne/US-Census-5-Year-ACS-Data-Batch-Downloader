# Contributing

Contributions are welcome under these terms.

## Licensing

- The project is licensed under the **GNU Affero General Public License v3.0 or later** ([LICENSE](LICENSE)). Distributed or network-hosted modifications must remain available under the same license.
- Before a pull request can be merged, its author must sign the [Contributor License Agreement](CLA.md) by commenting on the pull request: `I have read the CLA Document and I hereby sign the CLA.` The CLA lets the maintainer relicense the project in the future. You keep the copyright in your work.
- New source files start with `# SPDX-License-Identifier: AGPL-3.0-or-later`.

## Contributions produced with AI agents

Agent-assisted contributions are accepted. The person or organization operating the agent signs the CLA and is responsible for the contribution, as with any other tool. Commit messages and pull request descriptions should not add AI co-author or "generated with" attribution lines.

## Concordance overrides (`crosswalks/`)

Override rows follow [docs/agent_protocol.md](docs/agent_protocol.md): generated evidence, a census.gov source for merges, a passed continuity check or a written exception, and a reviewer different from the drafter. Agents may draft and review, as long as the drafter and reviewer have different identities. Run `python -m census validate-crosswalks` before opening a pull request. CI-equivalent checks run in the test suite.

## Development checks

```bash
python -m pytest -q                                   # offline suite
CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_live.py   # live API contracts (optional)
python -m census validate-crosswalks
python -m census validate --offline                   # calculations vs Census-published values
```

Regenerate derived data with the scripts in `scripts/`. Never hand-edit `metadata/`, `data/price_indexes/`, or `crosswalks/evidence/`; they are checksummed.
