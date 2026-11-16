# LocalForge MCP

Local execution and repository runtime for MCP coding agents on Windows.

LocalForge MCP gives an MCP client structured access to a real workspace, files, code search, Git, foreground commands, and long-running development processes. It runs on the host machine under the current Windows user account. It does not create a fake filesystem or hide projects inside a virtual sandbox.

> Beta software with powerful host access. Read [Security model](#security-model) before use.

**Disclaimer:** This is a small, quick project that was partially vibe-coded. While I used AI agents to assist with development, I personally reviewed, tested, and validated the code rather than relying on generated output without verification. This project is primarily for personal use, especially to make `Amazon Quick` more useful in my company and bring it closer to the experience of a real coding agent.

## Features

- Workspace root, current directory, and Git-root discovery; `workspace get` also returns the project's `AGENTS.md`/`CLAUDE.md`.
- Real filesystem list, stat, bounded read, atomic write, guarded text replacement, move, mkdir, and delete operations. Edits return a short diff and can run a per-file check (`edit_checks`).
- Ripgrep-backed code and filename search with Python fallback.
- Batched `multi_search`: up to 16 parallel searches in one round-trip.
- Batched `batch`: run any mix of tool calls in parallel in one round-trip, or in order with `sequential`, stopping at the first failure.
- `multi_edit`: several exact replacements per file in one call, validated end-to-end and written atomically.
- Tolerant argument aliases (`old`→`old_text`, `replacement`→`new_text`, `text`→`content`, `file`→`path`, `pattern`→`query`, `cmd`→`command`, and more), so mis-remembered parameter names do not cost a failed round-trip.
- Persistent `todo` tool: TodoWrite-style task list backed by the state file; survives restarts.
- MCP 2025-06-18 protocol with the `logging` capability; `structuredContent` tool results are opt-in (`structured_content: true`).
- Long-session guards: Git-root caching, whole-file-edit size limits, and a fixed-size striped path-lock table.
- Structured Git operations plus generic Git argument arrays.
- Foreground execution with separate stdout/stderr, timeout, truncation, duration, and exit metadata. Long output is cut to head and tail and saved to a log file (up to `max_capture_bytes`; output past `process_buffer_bytes` loses its oldest part); oversized results of any tool are saved to a file instead of failing the host relay.
- Long-running process IDs, incremental split-stream logs, stdin, status, restart, stop, and process-tree cleanup.
- Canonical path checks with separate read/write roots and denied paths.
- Explicit environment inheritance, secret-name filtering, and basic output redaction.
- Concurrent tool execution: pipelined tool calls run in parallel instead of queueing behind the slowest call; edits to the same file serialize while distinct files edit in parallel.
- Nine stable MCP tools rather than many command wrappers.
- No runtime approval tool; approval remains the responsibility of the MCP host such as Amazon Quick.

## Requirements

- Windows 10 or Windows 11.
- Python 3.11 or newer.
- Git for Git tools.
- Ripgrep is optional but recommended for fast search.
- Developer tools required by your projects, such as Node.js, .NET SDK, Python, Java, or Docker.

## Repository layout

```text
localforge-mcp/
├── agent_runtime/
│   ├── capabilities.py
│   ├── config.py
│   ├── errors.py
│   ├── patch.py
│   ├── processes.py
│   ├── security.py
│   ├── server.py
│   ├── state.py
│   └── todos.py
├── tests/
├── localforge.example.json
├── QUICK.md
├── pyproject.toml
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## Install on Windows

```powershell
cd C:\path\to\localforge-mcp

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .

Copy-Item localforge.example.json localforge.json
notepad localforge.json
```

Set `workspace_root`, `allowed_read_roots`, and `allowed_write_roots` before starting.

For one repository:

```json
{
  "workspace_root": "C:\\src\\my-app",
  "allowed_read_roots": ["C:\\src\\my-app"],
  "allowed_write_roots": ["C:\\src\\my-app"]
}
```

For multiple repositories under one parent:

```json
{
  "workspace_root": "C:\\src",
  "allowed_read_roots": ["C:\\src"],
  "allowed_write_roots": ["C:\\src"]
}
```

Keep the other properties from `localforge.example.json`.

## Amazon Quick setup

Create a **Local** MCP connection with these values.

**ID**

```text
localforge-mcp
```

**Name**

```text
LocalForge MCP
```

**Command**

```text
C:\path\to\localforge-mcp\.venv\Scripts\python.exe
```

**Arguments**

```text
-m agent_runtime.server
```

**Description**

```text
Local Windows coding runtime with workspace files, code search, Git, command execution, and long-running development processes.
```

Add environment variable:

```text
LOCALFORGE_CONFIG=C:\path\to\localforge-mcp\localforge.json
```

Set startup timeout to `60` seconds, save, then toggle the MCP connection off and on after configuration changes.

**Agent prompt:** paste the rules in [`QUICK.md`](QUICK.md) into the connection's agent/system prompt. They steer the model to batch-first dispatch, filesystem-first edits, and `process` for long waits — the behaviors that make Quick feel like a CLI coding agent.

The older environment variable `WIN_AGENT_RUNTIME_CONFIG` remains accepted for compatibility.

## Manual smoke test

Start server:

```powershell
$env:LOCALFORGE_CONFIG = "C:\path\to\localforge-mcp\localforge.json"
.\.venv\Scripts\python.exe -m agent_runtime.server
```

A blank terminal means the server is waiting for newline-delimited JSON-RPC input. Press `Ctrl+C` to stop it.

## MCP tools

### `workspace`

- `get`: return workspace root, current directory, detected Git root, and `project_instructions`: the contents of the `project_instruction_files` (default `AGENTS.md` and `CLAUDE.md`) in every folder from the Git root down to the cwd, so a subfolder's rules apply when working in it, or, with no Git root, from `workspace_root` down to the cwd (just the cwd when it is outside `workspace_root`). Outermost folder first, all within one `max_project_instruction_bytes` budget (default 8000) and the read policy; a file past the budget is listed as `{"path", "skipped": "budget"}`. Set `load_project_instructions: false` in the config to read no instruction files at all (no `project_instructions` key).
- `set_cwd`: change current directory inside read policy; returns the same fields.

### `filesystem`

- `list`, `stat`, `read`
- `write`, `replace_text`, `apply_patch`, `mkdir`, `move`, `delete`, `copy`

A negative `line_start` reads from the end: `line_start: -50` returns the last 50 lines (at most 2000), numbered with their real line numbers, with `line_start`/`line_end` set to the lines shown and `total_lines`; a file shorter than that comes back whole. It cannot be combined with `line_end`. When `max_read_chars` cannot hold all of them, the newest that fit come back with `truncated: true` and a `note`.

A UTF-8 byte order mark is hidden and kept, like line endings: reads do not return it, edits (`replace_text`, `multi_edit`, `apply_patch`, `write`) match and diff without it and write it back on a file that had one. `write` keeps it unless the new content starts with its own.

`replace_all: true` (on `replace_text`, or per `multi_edit` entry) replaces every exact match of `old_text` without giving the count; it must find at least one (`content_mismatch` otherwise) and cannot be combined with `expected_occurrences` (`invalid_arguments`). It matches exactly only: the typography and trailing-whitespace fallbacks are not tried, since a wrong fallback match would be repeated at every match.

A relative path that is missing from the runtime cwd but exists from the Git root (a path copied from `git status` after `set_cwd` into a subfolder, say) is used from the Git root by the read-only actions `read`, `stat`, `list` and `search` (its `path`), which then add a `note` naming the path used. Edits and other writes are not retried: their `path_not_found` error names the Git-root path to pass instead. Either way the path must pass the same read or write policy.

`read` supports byte offset and bounded output. On a directory, `read` lists it as `list` does (same `limit`, `offset`, `glob`, `include_hidden`, and read policy) and adds a `note` saying it was a directory, instead of failing with `not_file`. `replace_text` defaults to exactly one expected occurrence, preventing accidental broad replacements. With no exact match, `replace_text` and `multi_edit` retry ignoring typographic variants (curly quotes, dash variants, no-break and other Unicode spaces), then whole lines ignoring trailing whitespace; a fallback applies only with exactly the expected number of non-overlapping matches (otherwise the next tier is tried), never for an edit with a lone CR, and the result then says `"match": "typography"` or `"trailing_whitespace"`. When a fallback tier matches the wrong number of times, the error gives each tier's count (e.g. `found 0 exact, 2 ignoring typography, ...`) and the lines its matches start on. When `old_text` is not found, the `content_mismatch` error says so if `new_text` (20+ characters) is already in the file, or else shows the closest lines (at most 12, numbered `N<TAB>line` like line-mode `read`, each cut to 200 characters), found by comparing lines with whitespace ignored, so the next attempt can be fixed without re-reading the file. When the text matches more than once but one match was expected, the error lists the lines the matches start on (up to 10). A `path_not_found` from `read`, `stat`, `list`, `replace_text`, `multi_edit`, `delete`, `move`, or `copy` suggests up to 3 similar names when the parent folder exists (same name ignoring case, same stem with another extension, one name containing the other, or a small typo in the stem); when the folder is missing, it names the first missing folder and the nearest existing parent, with similar names there. Only paths the read policy allows are suggested. `multi_edit` applies several exact replacements to one file in order — the whole set is validated before anything is written, so a failing edit leaves the file untouched; prefer it for multi-part changes to a single file. `apply_patch` takes a unified diff for multi-hunk or multi-file edits, validated end-to-end before anything is written, all-or-nothing. git's own header lines (`diff --git`, `index`, `new file mode`, ...) are skipped. Each hunk is placed by content: its context and removed lines must match exactly, tried first at the line the header states, then at the nearest line that matches, after the previous hunk (hunks apply in order and never overlap); two equally near matches fail with both line numbers. Header line counts are not checked, the body decides. A rename or copy (`rename from`/`rename to`, or `a/` and `b/` paths that differ) is refused with `invalid_arguments`: move the file first, then patch it under its new name. Prefer it over writing plus executing a script to edit code. `copy` duplicates a file (or a directory with `recursive`) under policy checks — prefer it over shell copy commands, which mis-handle bracketed filenames like `[KB1] - doc.pdf` without `-LiteralPath`. `move` and `copy` refuse a destination that already exists: a file only gives way with `overwrite: true`, and an existing directory is never replaced or merged, nor is a file moved or copied into it (give the full destination path).

`write` (on an existing file), `replace_text`, and `multi_edit` return a `diff` of the change (unified, 2 context lines, capped at `edit_diff_max_bytes`), or `diff_skipped` (`too_large` above 128000 characters, `binary` with NUL bytes). With `edit_checks` configured, every edit of a matching file also runs a check and returns `check`:

```json
"edit_checks": {
  "*.py": ["python", "-c", "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])", "{path}"],
  "*.js": ["node", "--check", "{path}"]
}
```

The first matching glob wins; `{path}` becomes the edited file. A failing check does not undo the edit. Keep checks as argv arrays: wrapping one in `cmd /c`, `powershell -Command` or a `.cmd` shim lets a crafted file name (`&`, `;`, `$()`) inject shell syntax.

Argument names are tolerant: the server accepts common model guesses as aliases — `old`/`find` for `old_text`, `new`/`replacement` for `new_text`, `text` for `content`, `file`/`target` for `path`, `start`/`end` for `line_start`/`line_end`, `dest` for `destination`, `pattern` for `query` (search), `cmd` for `command` (execute/process), `id` for `process_id`, and `dir` for `path` (workspace). A canonical name always wins when both are sent.

Argument values are fixed to the schema type when the form is unambiguous: `"true"`/`"false"` (any case) become booleans, digit strings become integers or numbers, a bare string becomes a one-item list for list-of-string arguments (`glob`, `exclude`, `args`), and a JSON string holding the expected array or object is parsed (`edits`, `calls`, `searches`, `env`, batch `arguments`). This also applies inside `batch` calls, `multi_search` entries and `multi_edit` edits. Any other string for a boolean, number, array or object fails the call with one `invalid_arguments` error that lists every bad argument, e.g. `recursive: expected true or false, got 'maybe'`.

### `search`

Search text or filenames recursively with include/exclude globs, case control, fixed-string or regex matching, and result limits.

Content matches come back grouped by file, with the path once per file:

```json
{"engine": "ripgrep", "results": [
  {"path": "src\\app.py", "matches": [
    {"line": 12, "text": "svc = UserService()"},
    {"line": 40, "text": "class UserService:"}]}],
 "match_count": 2, "truncated": false, "exit_code": 0, "applied_excludes": ["**/.git/"]}
```

`before`/`after` appear only with `context_lines`. `max_results` counts matches, not files; `match_count` is the number of matches returned and `truncated` says more were cut. `files_only` returns `{"path"}` entries. Paths under the runtime cwd are relative to it. A line longer than 2000 characters (a minified file, say) comes back cut to its first 2000, marked `...[line cut: N chars]` with N its full length, as match `text` and in `before`/`after`; line-mode `read` cuts lines the same way. A ripgrep run that takes longer than 60 s fails with code `timeout` and a message that says to narrow the `path`, add a `glob`, or exclude big folders. A regex that ripgrep's default engine rejects but PCRE2 handles (look-around such as `foo(?=bar)`, backreferences) is run again with `--pcre2` when ripgrep has it, and the result adds a `note`; otherwise ripgrep's error comes back as before.

### `multi_search`

Run up to 16 independent searches in one call. Each entry takes the same arguments as `search` (plus an optional `label` echoed back with its results), and all entries execute in parallel. Use it to fan out several queries in a single round-trip instead of one `search` per call — especially useful with clients that issue one tool call per turn. A failing entry is reported per-entry (`error`) without failing the whole batch.

```json
{"searches": [
  {"query": "UserService", "glob": ["*.py"], "label": "users"},
  {"query": "TODO", "files_only": true, "label": "todos"}
]}
```

Responses are matched to requests by id, so when a client sends several tool calls in one turn they run concurrently and may complete in any order. Tune the worker count with `max_concurrency` (default 8).

### `batch`

Run up to 16 tool calls of any type in one call, in parallel — the closest match to how a CLI agent dispatches several tools in a single turn. Each entry names a tool and its arguments exactly as a `tools/call` request, with an optional `label` echoed back. Distinct paths run concurrently; edits to the same file serialize on that path (a second edit sees a stale base and fails with `content_mismatch` rather than overwriting). A failing call is reported per-call (`error`) without failing the batch. Nested `batch` calls are rejected.

```json
{"calls": [
  {"tool": "filesystem", "arguments": {"action": "read", "path": "src/app.py"}, "label": "app"},
  {"tool": "search", "arguments": {"query": "UserService", "glob": ["*.py"]}, "label": "users"},
  {"tool": "git", "arguments": {"action": "status"}, "label": "git"}
]}
```

For dependent steps, pass `"sequential": true`: the calls run one at a time in the given order, so a later call sees an earlier call's edit, and the run stops at the first failed call. A call fails when it raises (an `error` entry) or returns a result with `"success": false` (`execute` and `git`, including a backgrounded `execute`); every later call is returned as `{"index", "tool", "label", "skipped": true, "reason": "an earlier call failed (index N)"}` without being run. The reply adds `completed` (calls run without failure) and `stopped_at` (index of the failed call, or `null`). One server worker runs the whole batch, so once the run has taken longer than `execute_background_after_seconds` (default 120), the calls left are skipped too (`reason` names the time budget, `stopped_at` is the first of them): the client may have given up by then, and a commit must not run unseen. Other results never stop the run: an edit whose `check` has `ok: false` still counts as done, so check it yourself. Without `sequential` (default false) calls run in parallel and the reply is `{"results": [...]}` as before.

```json
{"sequential": true, "calls": [
  {"tool": "filesystem", "arguments": {"action": "replace_text", "path": "src/app.py", "old_text": "retries = 1", "new_text": "retries = 3"}},
  {"tool": "execute", "arguments": {"command": ["python", "-m", "pytest", "-q"]}, "label": "tests"},
  {"tool": "git", "arguments": {"action": "run", "args": ["commit", "-m", "raise retries", "--", "src/app.py"]}}
]}
```

A batch entry may name a tool as other agents call it: `bash`, `shell`, `sh`, `powershell`, `pwsh` and `cmd` run as `execute`, and `grep`, `rg` and `ripgrep` as `search`. The entry then reports the tool that ran and a `note` (`There is no "bash" tool, so this ran as "execute". Call "execute" directly.`).

Prefer `filesystem write`, `replace_text`, `multi_edit`, and `apply_patch` for file edits over shelling out to python or shell commands: they are atomic, policy-checked, and give structured errors. `execute` and `process` are for running programs, builds, and tests — those legitimately write files as the current user.

### `todo`

Persistent task list (TodoWrite-style) with `get`, `add`, `update`, `delete`, and `clear`.

```json
{"action":"add","title":"Fix login bug"}
{"action":"update","id":1,"status":"in_progress"}
{"action":"get"}
```

Keep it current across turns: add before starting work, mark `in_progress` while working and `completed` when done, delete stale entries. State is stored next to the config (`.localforge-state.json`) and survives server restarts; without a state file it is in-memory only.

### `git`

Presets: `status`, `diff`, `log`, `show`, `branch`, and `root`.

`diff` with `include_untracked: true` also shows new files: the plain `git diff` (with `args`) runs against the real index, then the untracked files that `.gitignore` does not hide are added as intent-to-add to a fresh throwaway index (`GIT_INDEX_FILE`) and diffed with the same `args`, so they appear as added files after the tracked changes. The real index is never touched, so they stay untracked. Each new file's part is cut to 8000 bytes (`...[new file cut: showing B of T bytes; read the file for the rest]`), and the result adds `untracked_files` (how many were found). It works with `diff` only (`invalid_arguments` otherwise).

Broad `git add -A` / `--all` / `.` is blocked (stage explicit files instead), as is staging private keys (`*.pfx`, `*.p12`, `*.pem`, `*.key`).

Use `run` with an argument array for other operations:

```json
{"action":"run","args":["add","src/app.py"]}
```

### GitHub via `gh` CLI

There is no GitHub API tool; use `gh` through `execute` (one-time `gh auth login` on the box — tokens cannot ride `env` overrides by design, and stored credentials work since processes run as your user):

```json
{"command":["gh","pr","create","--title","x","--body","y"]}
{"command":["gh","issue","list","--limit","20"]}
```

Prefer `gh ... --json <fields>` for machine-readable output. Note `gh` counts as network activity when `network` is `disabled`.

### `execute`

Prefer argument arrays:

```json
{"command":["npm","test"],"cwd":".","timeout":600}
```

String commands automatically use the configured shell:

```json
{"command":"npm test | Select-String failed"}
```

Pass `shell` explicitly only to force array commands through the shell. Argument arrays bypass shell parsing and stay preferred.

With `default_shell: "auto"` (the default), string commands run in PowerShell 7 (`pwsh`) when it is installed, which has `&&` and `||`; install it with `winget install --id Microsoft.PowerShell --source winget`. Windows PowerShell 5.1 has no `&&` or `||`; chain with `;` or make separate calls (a failed `&&` command gets a `hint`). With `default_shell: "powershell"`, a command that mentions `curl` or `wget` has those aliases removed first, so `curl` is the real `curl.exe`; other commands run as written.

Each stream returns at most `max_inline_output_bytes`, head and tail. When output is cut, it is saved (up to `max_capture_bytes`, head and tail) and its path returned as `stdout_path`/`stderr_path`; read it with `filesystem read` or `search`.

A command still running at its timeout is not killed: it keeps running as a `process` entry (id `exec-…`), and the result returns the output so far with `success: false`, `exit_code: null`, `error_type: "backgrounded"`, `process_id`, `next_after` (the cursor for `process read`'s `after`, so only new output comes back), and a `hint`. Follow it with `process read` (with `wait_ms`) and end it with `process stop`. Without a `timeout`, `execute` waits `execute_background_after_seconds` (default 120). Pass `kill_on_timeout: true` to stop the command at its timeout instead (`error_type: "timeout"`; the wait then defaults to `default_timeout_seconds`). When `max_processes` processes are already running or still holding their output, the command is stopped at its timeout and the `hint` says so. A backgrounded command's stdin is already closed; with no `input`, a command's stdin is empty. A command that exits while a process it started still holds its output open is kept the same way, with its `exit_code` (stopped at the timeout instead with `kill_on_timeout`), and `process stop` stops what it left running. Output past `process_buffer_bytes` (default 4000000) drops the oldest part, marked at the start of the stream that lost it (`stdout`, `stderr`, or both).

Output is decoded as UTF-8, and children are set up to write it: on Windows the server switches its console (which children share) to code page 65001 at startup and back at a clean exit, and every child gets `PYTHONUTF8=1` (Python 3.7+ UTF-8 mode) unless the config or the call sets it. So `é` and `日本` come back intact from PowerShell, cmd and Python, including through a Python program's own `subprocess` pipes. UTF-8 mode also makes `open()` without an `encoding` read and write UTF-8, and a Python program's `subprocess.run(..., text=True)` decode a child tool's output as UTF-8; if that tool writes in the ANSI code page (cp1252 on US English), the script now raises `UnicodeDecodeError`, so pass `encoding=` there. Pass `env: {"PYTHONUTF8": "0"}` (or set it in `extra_environment`) for a program that needs the legacy behaviour. Python 2 and 3.6 ignore `PYTHONUTF8` and keep the code page. A program that writes in a fixed legacy code page regardless can still come back garbled. Batch files with non-ASCII text should be saved as UTF-8, since cmd now reads them as UTF-8. If a server run in your own terminal is hard-killed, the terminal stays at code page 65001; run `chcp` with the old page (437 on US English) to switch it back.

Environment variables set inside one command do not carry over to the next, as in Claude Code; pass `env` per call. Unlike Claude Code, `cd` inside a command does not carry over either; use `workspace` `set_cwd`.

For SSH, prefer key auth with fail-fast flags so prompts never hang the call:

```json
{"command":["ssh","-i","C:\\Keys\\deploy.pem","-o","BatchMode=yes","-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=15","ubuntu@host","docker ps"]}
```

Two Windows gotchas: keys under Downloads usually have permissive ACLs, which ssh rejects (`Permissions ... are too open`) — fix with `icacls key.pem /inheritance:r /grant:r "$env:USERNAME:F"`. And ssh requires `%ProgramData%` in the environment (inherited by default since 1.4.0); without it ssh dies instantly with exit 255 and no output.

Complex quoting (multi-line PowerShell with `$vars`, nested `python -c`, bracketed paths) breaks when passed as a command string. Write the script with `filesystem write` to a temp file, then run it by path (`powershell -ExecutionPolicy Bypass -File script.ps1`, `python script.py`). Prefer `filesystem copy` over shell copy commands.

### `process`

Actions: `start`, `read`, `wait`, `write`, `status`, `list`, `restart`, and `stop`.

`wait` blocks until the process exits, a finished output line (stdout or stderr) matches the regex `pattern`, or `wait_ms` passes (default and max 60000), whichever comes first, so a build or test run needs no `read` polling. `pattern` is optional and looks only at output after `after` (default 0: all kept output); while the process runs, a line still being written is not matched, and once it exits its unfinished last line is. The result is the `status` plus `reason` (`matched`, `exited`, or `timeout`), `matched` (the matching line, or `null`), `tail` (the last `tail_lines` lines of the kept output, default 40, max 1000, both streams in arrival order, within `max_inline_output_bytes`; `tail_truncated` when cut), and `next_after`. An invalid `pattern` fails with `invalid_arguments`.

Use `after` with the previous `next_after` value for incremental log reads. `cursor_lost=true` means older output was evicted from the bounded buffer. `execute` commands that outlive their timeout show up here with `exec-…` ids.

### Quick examples

```json
{"action":"get"}
{"action":"read","path":"src/app.py","line_start":1,"line_end":80}
{"query":"UserService","glob":["*.py"],"context_lines":2}
{"action":"status"}
{"command":["npm","test"],"cwd":".","timeout":600}
{"action":"start","command":["npm","run","dev"]}
```

## Configuration

### Modes

- `READ_ONLY`: filesystem inspection only.
- `WORKSPACE`: workspace filesystem changes, but no process execution.
- `DEVELOPMENT`: workspace access and process execution.
- `FULL_ACCESS`: dedicated filesystem tools may access paths outside configured roots, while denied paths still apply.

### Network

- `unrestricted`: runtime does not block recognized network commands.
- `disabled`: blocks recognized package, Git, and URL-oriented commands.

This is command classification, not packet filtering. Use Windows Firewall, a controlled proxy, AppContainer, container, or VM for enforceable network restrictions.

### Shells

Supported `default_shell` values:

- `auto` (default): `pwsh` when PowerShell 7 is installed, on any OS (on Windows it is also found in its MSI or Store/winget install folder when it is not on `PATH`); otherwise `powershell` (Windows PowerShell 5.1) on Windows and `sh` elsewhere. Resolved once at startup; restart the server after installing PowerShell 7. Shell commands under `pwsh` get `NO_COLOR=1`, so errors come back without ANSI colour codes.
- `powershell`
- `pwsh`
- `cmd`
- `sh`

`execute.timeout` is seconds; `execute_background_after_seconds` (default 120) is the foreground wait of an `execute` with no `timeout`, after which the command moves to the background. `process.wait_ms` is milliseconds (max 60000). `limit_bytes`, `max_bytes`, and `max_file_read_bytes` are bytes. `filesystem offset` is a byte offset in legacy read mode; use `line_start`/`line_end` for line mode. `max_concurrency` bounds how many tool calls run in parallel (default 8). `log_to_client` (default false) emits an MCP logging notification per tool call (tool, duration, outcome) visible in the client; `LOCALFORGE_LOG=1` writes the same line to stderr. `replace_text` and `multi_edit` refuse files larger than `max_file_read_bytes` with `file_too_large`. `kill_children_on_exit` (default true) puts every spawned child into a Windows Job Object that the kernel terminates when the server exits, however it exits; set it to false to let children outlive the server. This covers apps and launchers started through `execute` or `process` too (`Start-Process`, `start`, GUI apps): they close when the server exits, for example on a Quick restart. Line-mode `read` returns numbered text (`N<TAB>line`), capped at 2000 lines or `max_read_chars` (default 60000, minimum 64), each line cut to 2000 characters (`...[line cut: N chars]`); continue from `next_line`; `total_lines` appears only when the read reaches the end of the file. A `line_start` past the end returns, instead of an error, a `note` (`line_start N is past the end; the file has M lines.`) and the file's last lines (up to 100, numbered, within `max_read_chars`). `structured_content` (default false) adds `structuredContent` to tool results. `max_inline_output_bytes` (default 30000) caps each `execute` stream in the reply and the default `process read` page. `max_result_bytes` (default 200000) caps any tool result; a bigger one is saved whole to a file (`result_path`) and the reply keeps the result's own fields with long strings cut to their first lines (marked `...[cut: showing lines 1-N of M; full result in result_path]`, or `B of T bytes` for one long line) and, if still too big, long lists cut to their first items plus a marker entry, until it fits in `max_result_bytes / 4` (50 KB by default). The reply adds `result_too_large: true`, `bytes`, `result_path`, and a `hint` up front; these four replace any same-named key of the result, except that a `hint` of the result's own is kept after the added one. Paths and ids (keys `cwd`, `…path`, `…_id`) are never cut. In every object that holds a cut (a string or list of its own, or anything nested inside one, such as the chunk texts of a `process read` with `format: "chunks"`), cursors that described the uncut value (`next_line`, `next_offset`, `next_after`, `line_end`, `bytes_returned`) are dropped and `truncated` (or the child's own `stdout_truncated` and the like) is set, so a paged read is continued from `result_path`, not from a cursor that would skip the cut part; the `hint` then names the dropped cursors and says to take them from `result_path` or pass a smaller `limit_bytes` or line range. A string only slightly over the cut stays whole, and a cut snaps to a line end only when that keeps at least half of it. A result that is not an object, or cannot be cut to fit, gets a short raw-JSON `preview` instead. When saving fails, `result_path` is `null` and the `hint` says so. `max_result_bytes` must be at least 1024 (checked whenever a config is built), so the reply always fits; below about 3 KB (depending on the length of `result_path`) the reply is always the raw-JSON `preview`, as a quarter of it cannot hold the added keys plus a cut string. Saved output goes to `spill_dir` (default: a per-config folder under `%TEMP%\localforge-results`; relative paths resolve against `workspace_root`). It is added to the read roots, and only its `lf-*` files are pruned (newest 100 kept). `edit_diff_max_bytes` (default 4000, 0 = off) caps the diff edits return. `edit_checks` (default `{}`) and `edit_check_timeout_seconds` (default 30) configure checks after edits. `load_project_instructions` (default true), `project_instruction_files` (default `["AGENTS.md", "CLAUDE.md"]`) and `max_project_instruction_bytes` (default 8000) control what `workspace get` returns.

## Security model

LocalForge MCP provides application-level mediation, not an OS security boundary.

Dedicated filesystem tools canonicalize paths and resolve existing links before checking roots. However, once command execution is allowed, the child process has the current Windows user's rights. Build scripts, package lifecycle hooks, Git hooks, interpreters, native binaries, and child processes can access anything that user can access.

For untrusted agents or repositories, run LocalForge MCP under a dedicated low-privilege Windows account and add OS-level isolation. See `SECURITY.md` for remaining reparse-point race and network limitations.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q agent_runtime
```

## Status and limitations

- Windows-native process-tree behavior requires Windows testing; cross-platform tests cover portable logic.
- Process state is in-memory and does not survive runtime restart.
- Children die with the server however it exits on Windows (Job Object); on POSIX they die only on explicit stop/restart or clean exit.
- Shell command network detection is intentionally conservative and cannot inspect arbitrary scripts.
- File writes are atomic at replacement time but cannot preserve every filesystem-specific metadata attribute.
- MCP transport is stdio only.

## License

MIT. See `LICENSE`.
