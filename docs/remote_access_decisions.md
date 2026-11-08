# Remote MCP access: planner decisions (2026-11-08)

Answers to [remote_access_questions.md](remote_access_questions.md) from the owner's project
planner. They are provisional unless stated otherwise. Remote access is implemented
**after Round 15**; Round 15 makes no networking changes.

## Summary

| Area | Decision |
| :--- | :--- |
| Transport | MCP stdio over SSH, server started on demand by the SSH session. No Streamable HTTP, persistent service, or TLS layer unless a chosen client requires it |
| Network | Trusted home LAN first; WireGuard later for access away from home; nothing publicly exposed |
| Host setup | OpenSSH, key-only login, a dedicated key per device, a restricted account or forced command, narrowly scoped `ufw` rule. Verify key login before disabling passwords |
| Profiles | `core` by default, `review`/`full` as appropriate. Profiles are not authorization |
| Concurrency | One active research session; no scheduler |
| Census API key | Remote clients may use the desktop key. Requests logged; configurable soft usage limits |
| Authority | Existing safeguards preserved. Destructive operations, concordance overrides and methodological exceptions need explicit human authorization; remote agents cannot approve their own exceptions |
| Audit | Local JSONL: client identity, tool, timestamp, sanitized arguments; 90-day retention; never credentials |
| Files | The desktop is authoritative. Small results and artifact metadata go over MCP; large outputs and all uploads use `scp`/`rsync` over SSH. One workspace per study under a configured root; paths confined (symlinks, traversal). No automatic transfer of raw datasets, PUMS or boundaries |
| Artifact references | Study ID, artifact ID, relative path, content type, checksum |
| Provenance | Record the declared client name, model and version, kept distinct from the authenticated device identity; no unnecessary personal identifiers. Manifests in a dedicated study repository with commit hashes (local commits are not independent timestamps). Exact engine commit and environment recorded; warn or refuse when a study pins a version |
| Operations | Async/resumable jobs only when real workloads need them. Desktop unavailable when asleep. Owner-controlled updates; a lightweight info mechanism reports version, commit and profile |
| Acceptance | From the laptop: run the Michigan/New York confirmatory study through MCP, retrieve maps and bundles, verify provenance, confirm the firewall restrictions remain intact |

## Larger platform

- The engine is one independent scientific component of an interdisciplinary research platform. The platform may also include:
  - Google Data Commons MCP;
  - Semantic Scholar, OpenScholar, PaperQA2;
  - local models and other domain engines.
- Interactive assistants (Claude Code, Codex, Gemini) offer critique when needed. Human oversight remains authoritative. No paid model API integrations are planned.
- Preferred formats: Parquet/CSV, GeoPackage, SVG/PDF, Markdown, BibTeX/CSL JSON.
- The platform owns combined-study provenance. This engine owns its own methodological records and validation.
- **Google Data Commons** is approved provisionally as a separate engine, connected through its own MCP service. Don't duplicate its retrieval here. Add interoperability tools only after testing shows a concrete need. No Data Commons work in Round 15.
- Owner-only server. Public source code does not mean public access; published users bring their own credentials.

## Implementation boundary

After Round 15, implement the minimum secure SSH-based remote access needed for one real laptop-to-desktop research workflow. Without a demonstrated requirement, do not build:
- Streamable HTTP;
- a persistent service;
- custom file-transfer APIs;
- job queues;
- Data Commons adapters.

Document unresolved questions rather than resolving them speculatively.
