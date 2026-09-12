# Amazon Q Developer as MCP Client: Server Lifecycle and Sessions

Date: 2026-09-12
Question: user says "Amazon Quick" — what product is that, how does it manage MCP stdio server lifecycle, and does server-side session persistence help?

## 0. TL;DR

- "Amazon Quick" is almost certainly **Amazon Q Developer** (CLI and/or IDE plugin), not **Amazon Quick / Quick Suite**. Quick Suite is a separate BI/research/automation workspace. Q Developer is the coding assistant that speaks MCP as a client.
- Q Developer connects via **stdio (local subprocess) and HTTP (remote, incl. OAuth)** with per-server `timeout` plus a global `mcp.initTimeout`.
- CLI spawns stdio servers **per `q chat` session** in the background; a failed/timeout server stays failed for that session (no auto-reconnect, no hot reload — restart required). IDE connects on add/enable/config-save and stops on disable/delete/exit.
- Spec-compliant clients send `initialize` → `notifications/initialized`, and `notifications/cancelled` on stdio; Q error logs (`-32002 initialize response`) prove it sends `initialize`.
- Server-side persistence of cwd/cursors/metadata **helps resume UX after restart** but **cannot resurrect live PIDs/child processes** killed with the old stdio server. MCP is explicitly stateless — design for rehydration + re-spawn, not process resurrection.

## 1. What is the product actually called?

### 1.1 Amazon Q Developer (the MCP client in question)

- **Amazon Q Developer** is AWS's AI coding assistant family, delivered as **Q Developer CLI** (`q chat` / `qchat`) and **Q Developer in the IDE** (VS Code, JetBrains, Visual Studio). The User Guide treats CLI and IDE as two surfaces of the same Q Developer product with separate MCP config pages ([Using MCP with Amazon Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)).
- MCP architecture doc names the host explicitly: "**MCP Hosts**: Programs like **Amazon Q Developer CLI** that want to access data through MCP" and "**MCP Clients**: Protocol clients that maintain **1:1 connections** with servers" ([Using MCP with Amazon Q Developer — MCP architecture](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)).
- CLI source of truth for agent/MCP schema is the Q CLI GitHub repo (`aws/amazon-q-developer-cli`), e.g. agent-format docs for `mcpServers`, `useLegacyMcpJson`, `timeout` ([agent-format.md — McpServers Field](https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#mcpservers-field), [UseLegacyMcpJson Field](https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#uselegacymcpjson-field)).

### 1.2 Amazon Quick / Amazon Quick Suite (different product)

- **Amazon Quick** is "the AI companion built for work" that "turns your questions into actions using agentic teammates for research, business insights, and automation" ([AI Assistant - Amazon Quick - AWS](https://aws.amazon.com/quick/)).
- **Amazon Quick Suite** GA announcement (Oct 2025) frames it as "a new set of agentic teammates" for answers + actions across business data (Jira, ServiceNow, etc.) ([Introducing Amazon Quick Suite](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-quick-suite-agentic-ai-powered-workspace/)).
- Quick docs: "**Amazon Quick** is an AI-powered service for automating tasks, analyzing data, building web applications, and conducting research" with modules Quick Sight / Flows / Automate / Index / Research; "Amazon Quick **evolved from Amazon QuickSight**" ([What is Amazon Quick?](https://docs.aws.amazon.com/quick/latest/userguide/what-is.html), [Amazon Quick Documentation](https://docs.aws.amazon.com/quick/)).
- Quick also consumes MCP, but server-side via **AgentCore Gateway**: "Amazon Quick → MCP Client → AgentCore Gateway → Your Tools/APIs", configured in Quick Console → Integrations → Add Integration → MCP Server URL + auth ([How to Connect Amazon Quick with MCP](https://repost.aws/articles/AR3VlBarnjS4eBr0lB-R1EXg/how-to-connect-amazon-quick-with-mcp-model-context-protocol-integrate-external-tools-systems)).
- Practical rule: if user runs `q chat`, edits `~/.aws/amazonq/mcp.json`, or clicks the tools icon in VS Code Q panel, that is **Q Developer**, not Quick ([MCP configuration for Q Developer in the IDE](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).

### 1.3 How Q Developer connects (stdio vs HTTP, local vs remote)

- IDE supports "**two primary transport mechanisms**: **STDIO and HTTP**" ([MCP configuration for Q Developer in the IDE — Adding an MCP server](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).
- CLI supports "**both local MCP servers (that run as processes) and remote MCP servers (that communicate over HTTP). Remote servers can use OAuth authentication or be open**" ([Using MCP with Amazon Q Developer — MCP configuration](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)).
- Remote HTTP config uses `type: http` + `url` (+ optional `headers`), e.g. `{"mcpServers":{"find-a-domain":{"type":"http","url":"https://api.findadomain.dev/mcp"}}}`; OAuth flow requires starting a Q CLI session with that agent, then `/mcp` → browser auth while keeping CLI open ([MCP configuration in the CLI — Remote MCP servers](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-config-CLI.html)). Same HTTP+OAuth support announced for CLI and IDE plugins ([Amazon Q Developer CLI announces support for remote MCP servers](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-q-developer-remote-mcp-servers/)).
- Local stdio config is `command` (required) + `args` / `env` / `timeout` (optional, ms, default 120000) per server ([agent-format.md — McpServers Field](https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#mcpservers-field) and mirror at [The Agent Format](https://aws.github.io/amazon-q-developer-cli/agent-format.html)).
- Security model: "**Local Execution**: MCP servers run locally", "**Isolation**: Each MCP server runs as a **separate process**" ([MCP security - Amazon Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-security.html)).
- Early-2025 blog noted "at the time of writing, **only the stdio transport is supported** in Amazon Q Developer CLI" — now superseded by HTTP support above ([Building AIOps with Amazon Q Developer CLI and MCP Server](https://aws.amazon.com/blogs/machine-learning/building-aiops-with-amazon-q-developer-cli-and-mcp-server/)).

## 2. When does Q start/stop/restart a stdio server?

### 2.1 CLI (`q chat`)

- **Start: per chat session, in background.** "Amazon Q **loads MCP servers in the background**, allowing you to start interacting immediately without waiting for all servers to initialize. Tools become available progressively" ([Using MCP with Amazon Q Developer — MCP server loading](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)). Startup banner shows e.g. `0 of 1 mcp servers initialized. ctrl-c to start chatting now` and `/tools` shows still-loading vs available ([bug: My tool was not getting listed — issue #1998](https://github.com/aws/amazon-q-developer-cli/issues/1998)). Background-load was an intentional change: "relegates `tools/list` ... to background" plus a configurable init timeout ([Background server load — PR #1775](https://github.com/aws/amazon-q-developer-cli/pull/1775)).
- **Init timeout (global gate, not per-server kill):** `q settings mcp.initTimeout [value]` in **milliseconds** "controls how long Amazon Q will wait for servers to initialize before allowing you to start interacting" ([Using MCP with Amazon Q Developer — Configuring server initialization](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)). Stragglers "continue to load in the background" past this gate ([Background server load — PR #1775](https://github.com/aws/amazon-q-developer-cli/pull/1775)).
- **Per-server request timeout:** `timeout` in agent/`mcp.json` is **milliseconds** (default 120000). Misreading it as seconds (e.g. `60` = 60 ms) is a recurring "servers still loading" cause; fix is `60000` ([bug: My tool was not getting listed — issue #1998](https://github.com/aws/amazon-q-developer-cli/issues/1998), [agent-format.md](https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#mcpservers-field)).
- **Config change: restart required (no hot reload).** Feature request "Allow reload of MCP servers during session ... It **requires user to restart the session**" is open; saving a long conversation then restarting loses the MCP context the user wanted ([Allow reload of MCP servers during session — issue #1943](https://github.com/aws/amazon-q-developer-cli/issues/1943)). Management commands (`qchat mcp add/remove/list/import/status`) edit stored config; status is queried per server ([MCP configuration in the CLI](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-config-CLI.html), [feat: Add MCP CLI subcommands — PR #1792](https://github.com/aws/amazon-q-developer-cli/pull/1792)).
- **Failure: sticky for session, no auto-reconnect.** "Q CLI **doesn't reconnect to failed MCP servers**, making them unusable for **session duration**" — when a server errors/timeouts, Q "maintains the stale connection"; workaround is restart or telling Q to forget that server ([Q CLI doesn't reconnect to failed MCP servers — issue #2708](https://github.com/aws/amazon-q-developer-cli/issues/2708)).
- **Stop: on `/quit` / session exit — but leaky.** Clean `/quit`/Ctrl-C normally ends servers, yet "**MCP server processes persist after Q CLI terminal closure** - orphaned Python processes accumulate" (80+ `uv` cached Python procs observed) ([bug: MCP server processes persist — issue #3272](https://github.com/aws/amazon-q-developer-cli/issues/3272)). Docker-based servers show the same class: quitting IDE leaves containers running; adding `--init` to `docker run` helped reaping ([[MCP] Docker container keep running after IDE exit — issue #1863](https://github.com/aws/amazon-q-developer-cli/issues/1863)).
- **Scope: per-agent, global vs workspace.** Global CLI config lives under `~/.aws/amazonq/cli-agents`; agents declare their own `mcpServers` plus `useLegacyMcpJson` to also pull legacy `~/.aws/amazonq/mcp.json` (global) and `cwd/.amazonq/mcp.json` (workspace) ([Using MCP with Amazon Q Developer — Setting up MCP servers with the Q CLI](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html), [UseLegacyMcpJson Field](https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#uselegacymcpjson-field)). Workspace file not loading was a real bug fixed in `load workspace MCP config in default agent` ([bug: Workspace specific mcp config not loaded — issue #2478](https://github.com/aws/amazon-q-developer-cli/issues/2478)).

### 2.2 IDE (VS Code / JetBrains)

- **Start: on add/save.** "After you add an MCP server in the IDE, **Amazon Q will attempt to connect** to it. If there are connection issues, an alert appears ... until the alert is resolved" via **Fix Configuration** ([MCP configuration for Q Developer in the IDE — Troubleshooting](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).
- **Toggle:** explicit **Enable** (MCP Servers panel → Enable), **Disable MCP Server** (panel → three dots → Disable), **Delete** (different flow for enabled vs disabled server) ([MCP configuration for Q Developer in the IDE — Enabling/Disabling/Deleting](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).
- **Scope:** global `~/.aws/amazonq/default.json` vs local `.amazonq/default.json` (workspace wins); legacy `~/.aws/amazonq/mcp.json` and `.amazonq/mcp.json` still honored when `useLegacyMcpJson` is true (default) ([MCP configuration for Q Developer in the IDE — Understanding files](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).
- **Per-server timeout in IDE UI is seconds** in the documented example ("recommended value of **60 (seconds)**" for the AWS Docs server), unlike CLI/agent JSON milliseconds — mind the unit when copying configs ([MCP configuration for Q Developer in the IDE — Adding a STDIO MCP server](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).
- **Governance override:** Pro-tier admin registry: "Q Developer **fetches the MCP registry at startup and every 24 hours**. During periodic sync, if a locally installed MCP server is **no longer in the registry, Q Developer terminates that server** ... If version differs, Q Developer **relaunches** the server with the registry version" ([MCP governance for Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-governance.html)).

## 3. Notifications, initialization, and session reuse

### 3.1 What the MCP spec mandates (all transports)

- Lifecycle is **Initialization → Operation → Shutdown**; client MUST start with `initialize` (protocol version + capabilities + clientInfo), server responds, then client MUST send **`notifications/initialized`** before normal operations ([Lifecycle — Initialization](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)).
- Shutdown has **no protocol message** — use the transport: for **stdio** the client SHOULD "close the input stream ..., wait ..., send **SIGTERM** ..., then **SIGKILL**"; for HTTP, close the connection(s) ([Lifecycle — Shutdown](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)).
- Timeouts: implementations SHOULD set per-request timeouts; on expiry the sender SHOULD issue a **cancellation notification** and stop waiting ([Lifecycle — Timeouts](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)).
- Cancellation: either side sends **`notifications/cancelled`** with `{requestId, reason}`; `initialize` MUST NOT be cancelled; receivers SHOULD stop work, free resources, and send no response ([Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)).
- stdio framing: "the **client launches the MCP server as a subprocess**", client writes requests/notifications to `stdin` (one JSON-RPC per line), MUST NOT write responses; to cancel, client MUST send `notifications/cancelled` since there is no per-request stream ([stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)). Transports overview: "on **stdio** the client sends `notifications/cancelled`; on **Streamable HTTP** it **closes the request's response stream**" ([Transports overview](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)).

### 3.2 What Q Developer observably does

- **Sends `initialize`:** failures surface as `Mcp error: -32002: connection closed: initialize response` across many servers/versions, i.e. Q opened the stdio pipe and waited for the initialize reply ([bug: MCP servers failed to load — issue #2906](https://github.com/aws/amazon-q-developer-cli/issues/2906), [All MCP servers fail: -32002 — issue #3744](https://github.com/aws/amazon-q-developer-cli/issues/3744)). Stdout pollution breaks this (servers MUST log to stderr), confirming Q parses strict newline-delimited JSON-RPC on stdout ([same #2906 thread](https://github.com/aws/amazon-q-developer-cli/issues/2906)).
- **`notifications/initialized`:** no Q doc exempts it; spec says client MUST send it after initialize response, so a compliant Q client sends it ([Lifecycle — Initialization](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)). No primary source found showing Q skipping it.
- **`notifications/cancelled`:** no Q-specific doc found; spec behavior (MUST on stdio, stream-close on HTTP) is the default assumption for any compliant client ([stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio), [Transports overview](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)). Timeouts SHOULD trigger cancellation per spec ([Lifecycle — Timeouts](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)).
- **Reuse model: one process per configured server per CLI session / IDE connection, shared across turns.** Evidence: 1:1 client↔server mapping in Q docs ([MCP architecture](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html)); per-session load banner and `/tools` + `/mcp` status showing "loaded vs still loading" within that session ([issue #1998](https://github.com/aws/amazon-q-developer-cli/issues/1998)); failure sticks for "session duration" rather than per-turn respawn ([issue #2708](https://github.com/aws/amazon-q-developer-cli/issues/2708)); remote OAuth requires keeping "the Q CLI session open" during browser auth, then tools appear in that session ([Remote MCP servers](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-config-CLI.html)). No primary source shows Q spawning a fresh stdio server per chat turn or per tool call.
- **Not per-chat-session isolation on IDE side:** IDE servers are global/workspace-scoped and toggled explicitly, surviving across individual chats until disabled/deleted/exited ([MCP configuration for Q Developer in the IDE](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).

## 4. Does server-side session persistence help? Limits

### 4.1 Where it helps (Q's actual lifecycle)

- **CLI restart-to-reload gap:** because config changes need a session restart ([issue #1943](https://github.com/aws/amazon-q-developer-cli/issues/1943)) and failed servers poison the session ([issue #2708](https://github.com/aws/amazon-q-developer-cli/issues/2708)), persisting **workspace_root, cwd, cursor IDs, allowed roots, background-task metadata** to disk lets the *next* server process resume UX (re-list tasks, re-attach logs, re-offer same tool surface) without user re-setup. This matches localforge-style config (`workspace_root`, `allowed_read_roots`, `default_timeout_seconds` in `localforge.example.json`).
- **IDE toggle/disable churn:** Enable→Disable→Enable and registry-driven terminate+relaunch ([MCP governance](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-governance.html)) are process restarts from the server's view; persisted metadata restores continuity.
- **Crash/orphan recovery:** given orphan leaks on abrupt exit ([issue #3272](https://github.com/aws/amazon-q-developer-cli/issues/3272), [issue #1863](https://github.com/aws/amazon-q-developer-cli/issues/1863)), a new process that reads last-known state is strictly better than starting blank.

### 4.2 Hard limits (what persistence cannot do)

- **stdio death = process death.** Spec shutdown is stdin-close → SIGTERM → SIGKILL; there is no "suspend stdio server" primitive ([Lifecycle — Shutdown](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)). Once Q kills (or orphans-then-user-kills) the old PID, its **live children, open handles, sockets, and in-memory progress die with it**. A restarted server gets a **new PID** and cannot `wait()`/signal/adopt the old tree.
- **MCP is stateless by design (2026 spec):** "A server processes each request independently; **no state should be inferred from previous requests**, even those on the same connection"; "Servers SHOULD be prepared to handle requests associated with **multiple tasks/threads/conversations**"; "Servers SHOULD NOT require that a client **reuse the same connection or process**"; "Clients SHOULD NOT use an individual task/thread/conversation as the lifetime boundary for the stdio process"; cross-request state "**MUST be referenced by an explicit identifier** the client passes on each request" ([Basic — protocol semantics](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)). So Q is *allowed* to interleave unrelated chats on one stdio pipe and to replace the process at will — server must key state by explicit IDs (session/task/cursor), never by "I am process N".
- **Q already isolates at the process level:** "Each MCP server runs as a **separate process**" ([MCP security](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-security.html)). Persistence does not add isolation; it only restores *metadata*. Anything requiring liveness (tail -f a child, hold a lock, keep a PTY) must be **re-spawned and re-attached** (e.g. re-run command, re-open log file offset from persisted cursor), not "resumed".
- **Cancellation is advisory and racy:** receivers MAY ignore unknown/already-done requests; sender SHOULD ignore late responses ([Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)). A persisted "task was running" flag may already be stale on restart — always reconcile (probe PID/file/marker) before reporting.
- **Timeout units bite:** persisted `timeout` values must record their unit (CLI ms vs IDE-example seconds) or a restored `60` flips between 60 ms and 60 s ([issue #1998](https://github.com/aws/amazon-q-developer-cli/issues/1998), [Adding a STDIO MCP server](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html)).

### 4.3 Design guidance for localforge-mcp

1. Persist **identifiers + intents** (session id, cwd, command line, env hash, log path, cursor/offset, exit-code cache), never raw PIDs/handles as truth.
2. On startup, **reconcile**: stale-PID → mark `orphaned/unknown`, re-spawn on demand, resume log tail from offset.
3. Make every long-running unit **re-runnable** (idempotency key) since Q may restart the server between the call and the poll.
4. Keep per-request `timeout` in ms in stored config; convert only at IDE-UI boundary.

## 5. Sources (primary)

- Q Developer MCP hub: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/qdev-mcp.html
- Q CLI MCP config + remote/OAuth: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-config-CLI.html
- Q IDE MCP config (stdio/HTTP, enable/disable, timeouts): https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html
- Q MCP security (separate process): https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-security.html
- Q MCP governance (registry fetch/terminate/relaunch): https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-governance.html
- Q CLI agent format (mcpServers, timeout ms, useLegacyMcpJson): https://github.com/aws/amazon-q-developer-cli/blob/main/docs/agent-format.md#mcpservers-field
- Q CLI agent format mirror: https://aws.github.io/amazon-q-developer-cli/agent-format.html
- Remote MCP announcement: https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-q-developer-remote-mcp-servers/
- AIOps blog (early stdio-only note + global/workspace mcp.json): https://aws.amazon.com/blogs/machine-learning/building-aiops-with-amazon-q-developer-cli-and-mcp-server/
- Background server load PR #1775: https://github.com/aws/amazon-q-developer-cli/pull/1775
- MCP CLI subcommands PR #1792: https://github.com/aws/amazon-q-developer-cli/pull/1792
- Issue #1998 (background load, ms timeout): https://github.com/aws/amazon-q-developer-cli/issues/1998
- Issue #1943 (restart required, no hot reload): https://github.com/aws/amazon-q-developer-cli/issues/1943
- Issue #2708 (no reconnect, sticky failure): https://github.com/aws/amazon-q-developer-cli/issues/2708
- Issue #3272 (orphaned procs after CLI close): https://github.com/aws/amazon-q-developer-cli/issues/3272
- Issue #1863 (docker container survives IDE exit): https://github.com/aws/amazon-q-developer-cli/issues/1863
- Issue #2478 (workspace mcp.json load fix): https://github.com/aws/amazon-q-developer-cli/issues/2478
- Issue #2906 (-32002 initialize failures): https://github.com/aws/amazon-q-developer-cli/issues/2906
- MCP lifecycle (initialize/initialized/shutdown/timeouts): https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- MCP cancellation: https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation
- MCP stdio transport: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
- MCP transports overview: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
- MCP statelessness (2026 spec): https://modelcontextprotocol.io/specification/2026-07-28/basic/index
- Amazon Quick: https://aws.amazon.com/quick/
- Amazon Quick docs: https://docs.aws.amazon.com/quick/ and https://docs.aws.amazon.com/quick/latest/userguide/what-is.html and https://docs.aws.amazon.com/quick/latest/userguide/how-quicksuite-works.html
- Quick Suite GA: https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-quick-suite-agentic-ai-powered-workspace/
- Quick + MCP via AgentCore: https://repost.aws/articles/AR3VlBarnjS4eBr0lB-R1EXg/how-to-connect-amazon-quick-with-mcp-model-context-protocol-integrate-external-tools-systems
