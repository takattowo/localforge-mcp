# LocalForge MCP 1.2.0 — agent ergonomics + robustness (no new tools)

Date: 2026-09-12
Status: design approved (scope locked), awaiting implementation plan
Goal: Quick / Q Developer + LocalForge handles single-workspace Windows coding end-to-end (read, search, run, tail logs, git diff) and survives client restarts without re-setup. No new MCP tools. Six tools stay six.

## 1. Background and success criteria

LocalForge MCP (6 tools: `workspace`, `filesystem`, `search`, `git`, `execute`, `process`) already gives a sandboxed MCP client real workspace access. Baseline audit (2026-09-12) found: folder had no `.git` (initialized, fetched `origin/main` at `c0771b2`), local tree ahead of remote with uncommitted v1.1.1 auto-shell changes plus untracked `.github/` + `.gitignore`, and 1 red test (`test_execute_results_shell_timeout_large_env_and_network`, stale shell expectation at `tests/test_runtime.py:64`).

Success demo for 1.2.0: fresh Q session, toggle connection off/on mid-task, then bugfix loop (search, line read, edit via existing `write`/`replace_text`, run tests, tail background process, `git diff`) with zero re-setup and zero `Internal error` responses. No `apply_patch` tool, no log resurrection, no todo/diagnostics tools — all explicitly deferred (see section 7).

## 2. Architecture (unchanged shape)

```
Q Developer (stdio client, 1:1 per server process)
  -> agent_runtime/server.py Server.handle/call (JSON-RPC, stdout stays pure)
    -> Capabilities (workspace/filesystem/search/git/execute)
    -> ProcessManager (background procs, in-memory buffers)
    -> NEW StateStore (agent_runtime/state.py, cwd only, atomic sidecar JSON)
    -> PathPolicy + Policy (roots, modes, network classification)
```

Key constraint driving the design: Q kills the stdio server on session exit / toggle / registry relaunch (stdin-close, SIGTERM, SIGKILL). `atexit` cleanup may never run. Therefore anything that must survive a restart has to be written through during operation, never at shutdown. Full log persistence was rejected for 1.2.0 for exactly this reason (write-through tails + reconcile + PID-reuse guards = new daemon semantics). The only persisted field is `cwd`, written on every `set_cwd`.

## 3. Components (surgical per-file changes, no renames)

### 3.1 Schemas + docs (`agent_runtime/server.py:14-35`, `README.md`, `CHANGELOG.md`)

- Add `description` to every property in all six `SCHEMAS` entries (same pattern 1.1.1 used for `command`/`timeout`/`shell`). Document units explicitly: `timeout` seconds, `wait_ms` milliseconds, `limit_bytes`/`max_bytes` bytes, `offset` bytes (legacy mode only).
- Fix two contradictions: README shell paragraph (code auto-enables shell for string commands since 1.1.1, README still says `shell=true` required) and timeout examples (bare `600` with no unit).
- Add one JSON example per tool in README. Add `CHANGELOG.md` 1.2.0 entry. Bump version to `1.2.0` in `pyproject.toml` and `agent_runtime/__init__.py`.

### 3.2 Error contract (`agent_runtime/capabilities.py:42-114`)

- Wrap all filesystem `OSError`/`FileNotFoundError`/`PermissionError`/`NotADirectoryError` paths (`stat`, `mkdir`, `delete`, `move` incl. missing dest parent, `replace_text` read/write, `list` stat per entry stays best-effort) into `RuntimeFault` codes: `path_not_found`, `not_file`, `not_directory`, `permission_denied`, `io_error` fallback. `content_mismatch` behavior unchanged.
- `server.py` mapping stays: `RuntimeFault` -> structured `isError` content, anything else -> `Internal error`. After this change no filesystem path raises raw.

### 3.3 Search upgrades, no new tool (`agent_runtime/capabilities.py:127-199`)

Backward-compatible new optional params: `context_lines` (default 0, max 5), `include_hidden` (default False), `max_file_size_bytes` (default 1000000). `path` accepts a file (search within it) as well as a directory. `max_results` clamp 1..5000 unchanged, documented.

- `files_only=true` with `query` set -> `invalid_arguments` (currently silently ignores query).
- Default excludes are always applied, unioned with any caller `exclude` list: `.git/**`, `.venv/**`, `__pycache__/**`, `node_modules/**`, `.hg/**`, `target/**`, `dist/**`, `build/**`, `*.egg-info/**`.
- Numeric clamping (documented, no errors): `context_lines` clamped to 0..5, `max_results` already clamps 1..5000. Non-positive `max_file_size_bytes` -> `invalid_arguments`. `context_lines` is ignored when `files_only=true`.
- ripgrep path: pass `--hidden` only when `include_hidden=true`; always add `-g !<pattern>` for applied excludes; add `-C <n>` when `context_lines > 0`; keep 60s timeout, document it.
- Python fallback: same excludes, binary sniff (skip file if NUL byte in first 8192 bytes), size guard (skip files larger than `max_file_size_bytes`), same `context_lines` support, never read more than `max_file_size_bytes + 1` bytes per file. Glob semantics documented as fnmatch on repo-relative posix path (matches current implementation).
- Response: existing keys unchanged (`engine`, `results`, `exit_code` for rg, `truncated`). Add `applied_excludes` (list actually used) to both engines. No new counts (rg cannot report skips cheaply; parity over precision).

### 3.4 Filesystem list pagination + filter (`agent_runtime/capabilities.py:63-74`)

New optional params: `limit` (default 200, clamped 1..1000), `offset` (default 0, negative clamped to 0), `glob` (array of fnmatch patterns on entry name), `include_hidden` (default False, skips dotfiles unless true). Sort order unchanged (dirs first, case-insensitive name). Response keeps `path` + `entries` (sliced) and adds `total` (count after hidden/glob filtering, before slicing), `offset`, `limit`.

### 3.5 Filesystem read line mode (`agent_runtime/capabilities.py:49-62`)

- Neither `line_start` nor `line_end` given -> legacy byte mode, response shape byte-for-byte identical.
- Either given -> line mode: `line_start` 1-based default 1, `line_end` inclusive default end-of-file; `line_end < line_start` -> `invalid_arguments`; out-of-range ends clamp to the file. Byte params (`offset`, `max_bytes`) ignored in line mode and documented as such. Binary guard kept (NUL in first 8192 bytes -> `binary_file`). Text decoded utf-8 with replace. Response: `{path, lines: [{no, text}], line_start, line_end, total_lines, truncated}` where `line_start`/`line_end` echo the effective (clamped) range and `truncated` is true when the file holds lines beyond the returned range.
- New schema params: `line_start`, `line_end` (integers). No other schema key changes.

### 3.6 Execute fixes (`agent_runtime/capabilities.py:229-253`)

- Byte-correct truncation: encode output to utf-8, slice to `max_capture_bytes` bytes, decode with replace. Flags `stdout_truncated`/`stderr_truncated` compare the same byte lengths (fixes current char-slice vs byte-compare mismatch on non-ASCII).
- New optional `input` (string, max 65536 chars -> `invalid_arguments` above): passed as process stdin, documented. Nothing else changes (policy, timeouts, redaction stay).

### 3.7 Process fixes, no log persistence (`agent_runtime/processes.py`)

- Split-multibyte fix: per-stream `codecs.IncrementalDecoder("utf-8", errors="replace")` in `_pump` instead of per-chunk `decode`. Chunk boundaries no longer corrupt CJK/emoji.
- Byte accounting fix: store raw byte length per chunk, subtract raw length on eviction (fixes current raw-in vs redacted-out drift).
- GC: new config `process_ttl_seconds` (default 3600) + `max_processes` (default 50), both validated positive. On `start`/`list`, prune exited entries older than TTL, then oldest-exited-first until under cap. If active processes exceed cap, `start` raises `process_limit`. `restart` preserves shell/env (existing 1.1.x behavior) and resets the entry clock.
- Every `process` response already echoes the original command/cwd, so agents can re-run after a server restart. `read` after restart reports `process_not_found` (honest) instead of fake resurrection. Documented in README limitations.

### 3.8 Cwd-only persistence (new `agent_runtime/state.py`)

- `StateStore(path)`: `{version: 1, cwd: str}`. Default path from new `state_file` config, default `<config-dir>/.localforge-state.json` where `<config-dir>` is the directory of the loaded config file (`Config.load` records its resolved source path for this). Atomic writes (tmp file + `os.replace`, same pattern as `_atomic_write`). Written on every `workspace set_cwd` after the in-memory update; a failed sidecar write warns to stderr and keeps the in-memory value (best-effort, never fails the tool call).
- Load in `Capabilities.__init__`: missing/corrupt file -> warn to stderr, fall back to workspace root, never fail startup. Loaded `cwd` validated inside current read roots, else workspace root.
- New config field `state_file: str | None = None` (section 3.9). No process, log, cursor, or secret persistence. Ever. State file holds one path string.

### 3.9 Config (`agent_runtime/config.py:10-56`, `localforge.example.json`)

New fields with defaults: `state_file` (None -> default sidecar), `process_ttl_seconds` (3600), `max_processes` (50). Positive validation extended to the two numeric fields. Roots behavior unchanged (must exist) but the error names which root is missing. Example JSON documents all three.

### 3.10 Observability

- `LOCALFORGE_LOG=1` writes one stderr line per `tools/call`: `{tool, action, duration_ms, ok | fault_code}`. Default off. stdout remains pure newline-delimited JSON-RPC (Q parses it strictly; stdout pollution breaks `initialize`).

## 4. Data flow

- `set_cwd`: resolve + policy check (unchanged) -> update memory -> atomic sidecar write -> return workspace info.
- Server boot: load config -> `StateStore` load (best-effort) -> validate cwd -> serve. No migration: unknown `version` -> ignore file + warn.
- `search`: resolve path (file or dir) -> applied excludes = user list or defaults -> rg or python fallback with same excludes + size/binary guards -> slice to `max_results` -> return with `applied_excludes`.
- `filesystem list/read`: resolve -> policy -> paginate / byte-or-line branch -> return.
- `execute`: resolve -> authorize -> spawn -> communicate (optional stdin) -> redact -> byte-slice -> return.
- `process start/read/write/stop`: unchanged paths plus decoder/accounting/GC fixes. No disk writes in this flow.

## 5. Error handling

Full code list agents can branch on (no new transport-level codes): existing `tool_not_found`, `invalid_arguments`, `invalid_action`, `path_denied`, `path_outside_roots`, `path_not_found`, `permission_denied`, `not_file`, `not_directory`, `not_directory` (search root), `binary_file`, `content_mismatch`, `execution_denied`, `invalid_command`, `shell_denied`, `network_denied`, `invalid_shell`, `environment_denied`, `process_exists`, `process_not_found`, `process_exited`, `stdin_closed`, plus new `process_limit` (cap hit) and `io_error` (unclassified OSError). Raw `OSError` never reaches the client from filesystem paths. `move` to missing parent returns `path_not_found`, not `Internal error`. `line_end < line_start`, `files_only` + `query`, oversized `input` return `invalid_arguments`.

## 6. Testing

- Fix red test: string command without `shell` now succeeds via auto-shell; assert success + marker in stdout instead of `pytest.raises`.
- New tests (all in `tests/test_runtime.py`, runtime target under ~30s): line-mode read (slice, numbers, `line_end < line_start` rejection, byte-mode unchanged); list pagination/filter/hidden; search defaults (excludes applied, `.git`/`.venv` skipped in fallback via monkeypatched `shutil.which -> None`, `files_only` + `query` rejection, single-file path, context lines); execute byte truncation with multibyte + `input` roundtrip (`python -c` stdin read); error codes (`stat` missing -> `path_not_found`, `move` missing parent -> `path_not_found`); cwd persistence roundtrip with tmp `state_file` (write, reload, corrupt-file fallback); process GC (cap + TTL prune, `process_limit`).
- Existing CI (`.github/workflows/tests.yml`: Windows, 3.11/3.12, `pytest -q`, `compileall`) unchanged.

## 7. Explicitly out of scope (deferred with reasons)

- `apply_patch` / multi-file diff tool: duplicates Q built-in `fs_write` when it works; adds diff-header path-traversal + patch-bomb surface. Revisit only if `replace_text` single-occurrence guard blocks real Quick tasks (unproven).
- Full process/log persistence and resurrection: cannot bring dead PIDs back after SIGKILL; needs write-through tails + reconcile + PID-reuse guards. Revisit only after measured restart pain; cwd-only + re-runnable commands cover 80%.
- Todo/plan, diagnostics/LSP, webfetch tools: Q ships `todo_list`, `fs_read`/`fs_write`, fetch equivalents. No duplication without evidence.
- HTTP/SSE transport, remote daemon, config auto-create, per-tool timeouts: no requesting client; YAGNI.

## 8. Rollout

1. Implement sections 3.1-3.10 + tests (single feature branch).
2. Version `1.2.0` (`pyproject.toml`, `agent_runtime/__init__.py`, `CHANGELOG.md`).
3. Commit includes: 1.2.0 changes plus currently uncommitted v1.1.1 leftovers (7 modified files) and untracked `.github/` + `.gitignore` (`git add -A` minus ignored: `.venv/`, `*.egg-info/`, `localforge.json`). Push only on explicit user approval.
4. Verify: `pytest -q`, `compileall`, manual Quick toggle test (set cwd, toggle off/on, confirm cwd kept; start process, restart server, confirm honest `process_not_found` + re-runnable command).
