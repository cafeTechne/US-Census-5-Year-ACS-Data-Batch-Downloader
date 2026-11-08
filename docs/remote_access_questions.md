# Remote MCP access: questions for the project planner

**Answered 2026-11-08:** see [remote_access_decisions.md](remote_access_decisions.md).

**Context.** The Census Research Engine (this repository) runs on the owner's Linux desktop (Ubuntu, 128 GB RAM, wired LAN `192.168.1.0/24`). It exposes its tools through an MCP server (`mcp_server.py`, Python `mcp` 1.30, FastMCP). Today the server runs over **stdio only**, so only a client on the desktop can use it. The plan is for a **laptop** running other models and tools to call this server over the network.

**Current host facts (checked 2026-11-08):**
- `ufw` is active and denies incoming connections.
- No SSH server is installed.
- The engine holds the owner's Census API key in a local `.env`.

The engine never prints or ships the key; published users bring their own.

Tool profiles exist: `core` (small local models), `review`, and `full`.

**Sequencing (owner decision):** remote access comes after Round 15, which covers multi-release confirmatory studies, coverage studies for spatial regression and local Moran's I, and possibly another Census program. Cross-device testing time is limited, so the design should need little iteration.

Please answer what you can. "Don't know yet" is fine; the engine side will take the default in brackets.

## A. Clients and models

1. **Laptop MCP clients.** Which MCP clients will the laptop run? Examples: LM Studio, Ollama with an MCP-capable front end (Open WebUI, Continue, a custom agent), Claude Desktop, Claude Code, or your own agent framework. Which transports does each support: stdio-over-SSH, streamable HTTP, SSE? [default: support both stdio-over-SSH and streamable HTTP]
2. **Clients beyond the laptop.** Will anything other than the laptop connect, such as phones, a second desktop, cloud agents, or scheduled jobs? [default: the laptop only, on the home LAN]
3. **Local-model sizes.** What model sizes will the laptop run? This decides whether the `core` profile stays the default for remote clients. [default: `core` for local models, `full` on request]
4. **Concurrency.** Will more than one client or agent call the server at the same time, for example parallel agents during a study? [default: a few concurrent sessions; long jobs queued]

## B. Network and access

5. **Network scope.** Is home-LAN access enough, or must it also work away from home: travel, another network, mobile hotspot? If away from home: a VPN such as Tailscale or WireGuard, or nothing public? [default: LAN only; no port exposed to the internet]
6. **SSH.** Is installing `openssh-server` on the desktop acceptable, with key-only login and a `ufw` rule allowing port 22 from `192.168.1.0/24` only? The laptop would then reach the MCP server through an SSH tunnel, and the MCP port would never be opened. [default: yes; this is the recommended route]
7. **Direct HTTP.** If not SSH, is a `ufw` rule for one HTTP port from the LAN subnet (or one laptop IP) acceptable? Should the laptop get a fixed IP or DHCP reservation so the rule can name it? [default: SSH first; HTTP bound to localhost behind the tunnel]
8. **TLS.** Is it required on the LAN, given that the SSH tunnel already encrypts? [default: none inside the tunnel; required for any non-tunnelled HTTP]
9. **Uptime.** Should the server always be running (a systemd user service started at boot), or started on demand per session? Must it survive reboots and logouts? [default: on demand, plus an optional systemd unit]

## C. Authentication and authority

10. **Authentication.** SSH keys only, or also a bearer token for HTTP clients? One shared credential, or one per client so access can be revoked separately? [default: per-client SSH keys; per-client tokens if HTTP is used]
11. **API key spending.** Remote clients spend the owner's Census API key. Is that acceptable for every authenticated client? Should there be a daily call or download budget, or a per-client cap? [default: allowed; a soft daily cap with logging]
12. **Tool restrictions for remote clients.** Should anything be off-limits remotely? Candidates:
    - writing outside a workspace
    - recording concordance overrides (`record_override`, `record_rule_review`)
    - deleting caches
    - running long jobs such as full validation or PUMS downloads

    Are overrides human-only, so the owner must approve them? [default: remote clients may draft overrides but not record them; destructive and long actions need an explicit flag]
13. **Audit log.** Who called which tool with which arguments, and when? How long is it kept? [default: a local JSONL log with the client name, kept 90 days, with secrets redacted]

## D. Files and outputs (the main design question)

Most tools take input paths and write outputs (maps, figures, tables, bundles, manifests) on the desktop's filesystem. A remote client can neither send its own files nor open the returned paths.

14. **Where analysis files live.** On the desktop only, with the laptop treating them as remote? Synced, for example through a shared folder, Syncthing or Nextcloud? Or a mix? [default: a desktop-side workspace, fetched on demand]
15. **Getting outputs to the laptop.** Through the MCP itself (MCP resources, or base64 content in tool results, both with size limits), through a separate download path such as HTTP file links inside the tunnel or `scp`, or a shared folder? [default: MCP resources for small artifacts (≤ 10 MB); `scp`/`rsync` over the same SSH for bundles]
16. **Uploads.** Will the laptop upload its own inputs: manifests, custom crosswalks, its own CSVs, replicate files? Through MCP, `scp`, or a shared folder? [default: manifests and small CSVs through an MCP upload tool into the workspace; larger files by `scp`]
17. **Workspaces.** One per client, per study, or one shared? Should tool paths be confined to a workspace root, rejecting any path outside it? [default: one workspace per study under a configured root; paths confined]
18. **Size ceilings.** What are the largest outputs to transfer? PUMS extracts and national boundary files can be hundreds of MB. [default: never transfer PUMS or boundaries; send derived outputs only]

## E. Reproducibility and provenance

19. **Recording the client.** Should manifests and results bundles record which client or model ran the analysis: model name and version, client name, laptop host? Is the agent identity in `authors` (for example `agent:<model>`) enough? [default: record the client name and model if the client sends them; never record the laptop's username or IP]
20. **Pre-registration timing.** When a laptop agent pre-registers a study, where is the manifest committed so the time-stamp is trustworthy: the desktop's git repository, a study repository, or both? [default: a study repository on the desktop; the engine reports the hash]
21. **Engine version for a study.** Should remote clients be able to pin it, for example refusing to run if the desktop has moved to a newer release mid-study? [default: report the version and warn on change]

## F. Operations

22. **Long jobs.** Full validation, PUMS downloads, big map batches: should they be asynchronous, returning a job id to poll, or block the call? What timeout do the laptop clients use? [default: anything expected over about 60 s becomes a job]
23. **Desktop availability.** If the desktop is asleep or off when the laptop wants it, is wake-on-LAN wanted, or is it simply unavailable? [default: unavailable]
24. **Updates.** When the engine updates, who restarts the server? Should the laptop be told about new tools? [default: the owner restarts; a `server_info` tool reports the version and profile]
25. **Acceptance test.** What counts as "remote access done"? Proposed: from the laptop, with the chosen client, run the Michigan/New York confirmatory case study end to end and retrieve its maps and bundle, with the firewall still denying everything except the agreed rule.

## G. Larger-project fit

26. **The larger project.** What else does it contain that this engine must interoperate with: other MCP servers, a study notebook or writing tool, a reference manager, an orchestration agent? Which output formats do they expect: Parquet, CSV, GeoPackage, PDF or SVG figures, LaTeX or Markdown tables, BibTeX citations?
27. **Studies outside the engine.** Will complete studies combine this engine's outputs with non-Census data? If so, which side owns provenance and the combined manifest?
28. **Other users.** Will anyone other than the owner use this server: collaborators, students? This changes authentication, audit and API key policy (published users bring their own key).
