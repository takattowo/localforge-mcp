# LocalForge MCP

Local execution and repository runtime for MCP coding agents on Windows.

LocalForge MCP gives an MCP client structured access to a real workspace, files, code search, Git, foreground commands, and long-running development processes. It runs on the host machine under the current Windows user account. It does not create a fake filesystem or hide projects inside a virtual sandbox.

> Beta software with powerful host access. Read [Security model](#security-model) before use.

**Disclaimer:** This is a small, quick project that was partially vibe-coded. While I used AI agents to assist with development, I personally reviewed, tested, and validated the code rather than relying on generated output without verification. This project is primarily for personal use, especially to make `Amazon Quick` more useful in my company and bring it closer to the experience of a real coding agent.

## Features

- Workspace root, current directory, and Git-root discovery.
- Real filesystem list, stat, bounded read, atomic write, guarded text replacement, move, mkdir, and delete operations.
- Ripgrep-backed code and filename search with Python fallback.
- Structured Git operations plus generic Git argument arrays.
- Foreground execution with separate stdout/stderr, timeout, truncation, duration, and exit metadata.
- Long-running process IDs, incremental split-stream logs, stdin, status, restart, stop, and process-tree cleanup.
- Canonical path checks with separate read/write roots and denied paths.
- Explicit environment inheritance, secret-name filtering, and basic output redaction.
- Six stable MCP tools rather than many command wrappers.
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
│   ├── processes.py
│   ├── security.py
│   └── server.py
├── tests/
├── localforge.example.json
├── pyproject.toml
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## Install on Windows

```powershell
cd D:\Repositories\localforge-mcp

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
  "workspace_root": "D:\\Repositories\\MyApp",
  "allowed_read_roots": ["D:\\Repositories\\MyApp"],
  "allowed_write_roots": ["D:\\Repositories\\MyApp"]
}
```

For multiple repositories under one parent:

```json
{
  "workspace_root": "D:\\Repositories",
  "allowed_read_roots": ["D:\\Repositories"],
  "allowed_write_roots": ["D:\\Repositories"]
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
D:\Repositories\localforge-mcp\.venv\Scripts\python.exe
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
LOCALFORGE_CONFIG=D:\Repositories\localforge-mcp\localforge.json
```

Set startup timeout to `60` seconds, save, then toggle the MCP connection off and on after configuration changes.

The older environment variable `WIN_AGENT_RUNTIME_CONFIG` remains accepted for compatibility.

## Manual smoke test

Start server:

```powershell
$env:LOCALFORGE_CONFIG = "D:\Repositories\localforge-mcp\localforge.json"
.\.venv\Scripts\python.exe -m agent_runtime.server
```

A blank terminal means the server is waiting for newline-delimited JSON-RPC input. Press `Ctrl+C` to stop it.

## MCP tools

### `workspace`

- `get`: return workspace root, current directory, and detected Git root.
- `set_cwd`: change current directory inside read policy.

### `filesystem`

- `list`, `stat`, `read`
- `write`, `replace_text`, `apply_patch`, `mkdir`, `move`, `delete`, `copy`

`read` supports byte offset and bounded output. `replace_text` defaults to exactly one expected occurrence, preventing accidental broad replacements. `apply_patch` takes a unified diff for multi-hunk or multi-file edits: exact context, validated end-to-end before anything is written, all-or-nothing. Prefer it over writing plus executing a script to edit code. `copy` duplicates a file (or a directory with `recursive`) under policy checks — prefer it over shell copy commands, which mis-handle bracketed filenames like `[KB1] - doc.pdf` without `-LiteralPath`.

### `search`

Search text or filenames recursively with include/exclude globs, case control, fixed-string or regex matching, and result limits.

### `git`

Presets: `status`, `diff`, `log`, `show`, `branch`, and `root`.
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

For SSH, prefer key auth with fail-fast flags so prompts never hang the call:

```json
{"command":["ssh","-i","C:\\Keys\\deploy.pem","-o","BatchMode=yes","-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=15","ubuntu@host","docker ps"]}
```

Two Windows gotchas: keys under Downloads usually have permissive ACLs, which ssh rejects (`Permissions ... are too open`) — fix with `icacls key.pem /inheritance:r /grant:r "$env:USERNAME:F"`. And ssh requires `%ProgramData%` in the environment (inherited by default since 1.4.0); without it ssh dies instantly with exit 255 and no output.

Complex quoting (multi-line PowerShell with `$vars`, nested `python -c`, bracketed paths) breaks when passed as a command string. Write the script with `filesystem write` to a temp file, then run it by path (`powershell -ExecutionPolicy Bypass -File script.ps1`, `python script.py`). Prefer `filesystem copy` over shell copy commands.

### `process`

Actions: `start`, `read`, `write`, `status`, `list`, `restart`, and `stop`.

Use `after` with the previous `next_after` value for incremental log reads. `cursor_lost=true` means older output was evicted from the bounded buffer.

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

- `powershell`
- `pwsh`
- `cmd`
- `sh`

`execute.timeout` is seconds. `process.wait_ms` is milliseconds (max 60000). `limit_bytes`, `max_bytes`, and `max_file_read_bytes` are bytes. `filesystem offset` is a byte offset in legacy read mode; use `line_start`/`line_end` for line mode.

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
- Shell command network detection is intentionally conservative and cannot inspect arbitrary scripts.
- File writes are atomic at replacement time but cannot preserve every filesystem-specific metadata attribute.
- MCP transport is stdio only.

## License

MIT. See `LICENSE`.
