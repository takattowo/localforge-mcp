# LocalForge MCP

Local Windows coding runtime for AWS Quick and other MCP clients. It gives an agent a real workspace: files, code search, Git, foreground commands, and long-running processes, run on the host under your own Windows account. Policy checks (modes, roots, denied paths, environment filtering) apply before the tools touch anything; it is not an OS sandbox and does not hide projects in a virtual filesystem.

> Beta software with powerful host access. Read [Safety notes](#safety-notes) and [`SECURITY.md`](SECURITY.md) before use.

## Requirements

- Windows 10 or 11 (POSIX works for most tools; see [Limitations](#limitations)).
- Python 3.11 or newer, preferably from python.org (not the Microsoft Store; see [Limitations](#limitations)).
- Git, for the `git` tool.
- ripgrep (optional, recommended): faster `search`; a Python fallback is used without it.
- PowerShell 7 (optional, recommended): `&&`/`||` in string commands. `winget install --id Microsoft.PowerShell --source winget`.
- Whatever your projects need (Node.js, .NET SDK, Java, Docker, ...).

## Install

```powershell
cd C:\path\to\localforge-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item localforge.example.json localforge.json
notepad localforge.json
```

Set `workspace_root`, `allowed_read_roots` and `allowed_write_roots` (see [Config](#config)). `localforge.json` is gitignored.

## Add to Quick

Create a **Local** MCP connection:

- Name: `LocalForge MCP` (ID `localforge-mcp`)
- Command: `C:\path\to\localforge-mcp\.venv\Scripts\python.exe`
- Arguments: `-m agent_runtime.server`
- Environment: `LOCALFORGE_CONFIG=C:\path\to\localforge-mcp\localforge.json`
- Startup timeout: `60` seconds

Toggle the connection off and on (or restart Quick) after changing the config: it is read once at startup. Other MCP clients use the same command, arguments and environment (transport is stdio, protocol `2025-06-18`). [`QUICK.md`](QUICK.md) has optional agent rules to paste into the connection's prompt.

Without `LOCALFORGE_CONFIG` the server tries the older `WIN_AGENT_RUNTIME_CONFIG`, then `localforge.json` in its working directory, then `localforge.json` next to the server (the repo folder), so the variable is optional when the config sits there.

Smoke test: run the same command in a terminal with `$env:LOCALFORGE_CONFIG` set. A blank terminal means it is waiting for newline-delimited JSON-RPC on stdin; `Ctrl+C` stops it.

## Config

One repository:

```json
{
  "workspace_root": "C:\\src\\my-app",
  "mode": "DEVELOPMENT",
  "allowed_read_roots": ["C:\\src\\my-app"],
  "allowed_write_roots": ["C:\\src\\my-app"],
  "denied_paths": ["C:\\src\\my-app\\.git\\hooks", "%USERPROFILE%\\.ssh"]
}
```

Several repositories under one parent: use `C:\src` for `workspace_root` and both root lists. `localforge.example.json` lists the other common keys with their defaults. An unknown key fails at startup with `unknown config key: X`. Paths in roots and `denied_paths` expand `~` and `%VAR%`; relative ones resolve against `workspace_root`; roots must exist.

### Modes and paths

| Key | Default | Meaning |
|---|---|---|
| `workspace_root` | required | Starting cwd; base for relative paths. |
| `mode` | `DEVELOPMENT` | `READ_ONLY` (read, list, stat only), `WORKSPACE` (file changes, no process execution), `DEVELOPMENT` (files and processes), `FULL_ACCESS` (dedicated tools may leave the roots; `denied_paths` still apply). |
| `allowed_read_roots` | `[workspace_root]` | Where the dedicated tools may read. |
| `allowed_write_roots` | `[workspace_root]` | Where they may write. |
| `denied_paths` | `[]` | Always refused, in every mode (`path_denied`). |
| `network` | `unrestricted` | `disabled` blocks recognised network commands (`network_denied`). |
| `state_file` | `.localforge-state.json` next to the config | Persists the `todo` list. |

### Commands and environment

| Key | Default | Meaning |
|---|---|---|
| `default_shell` | `auto` | Shell for string commands: `auto`, `pwsh`, `powershell`, `cmd`, `sh`. |
| `allow_shell_commands` | `true` | `false` refuses string commands (`shell_denied`); argv arrays still run. |
| `default_timeout_seconds` | `600` | Wait for `kill_on_timeout` calls; a longer explicit `timeout` gets a `warning`. |
| `execute_background_after_seconds` | `120` | Foreground wait of an `execute` without `timeout`; also the time budget of a sequential `batch`. |
| `max_processes` | `50` | Processes running or still holding output. |
| `process_ttl_seconds` | `3600` | An exited process stays listed and readable until this long after its start. |
| `kill_children_on_exit` | `true` | Put children in a kill-on-close Windows job, so they end when the server does. |
| `inherit_environment` | `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `COMSPEC`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `ProgramFiles`, `ProgramFiles(x86)`, `ProgramData`, `SystemDrive` | Variables children inherit. A list of your own replaces this one, so keep `SystemDrive` and `ProgramData` (ssh needs it). |
| `deny_environment` | `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*KEY*`, `AWS_*`, `AZURE_*`, `GITHUB_*`, `NPM_TOKEN` | Name globs never passed on; a call's `env` naming one fails with `environment_denied`. |
| `extra_environment` | `{}` | Variables added for every child. |

`default_shell: "auto"` picks `pwsh` when PowerShell 7 is installed (on Windows also found in its MSI or Store/winget folder when not on `PATH`), else `powershell` (Windows PowerShell 5.1) on Windows and `sh` elsewhere. It is resolved once at startup; restart after installing PowerShell 7. Under `pwsh`, shell commands get `NO_COLOR=1`. Windows PowerShell 5.1 has no `&&`/`||` (a failed one gets a `hint`); under `powershell`, a command that mentions `curl` or `wget` drops those aliases so the real `curl.exe` runs.

`network: "disabled"` is command classification, not packet filtering: it blocks `curl`, `wget`, `git`, `npm`, `npx`, `pnpm`, `yarn`, `pip`, `python`, `py` and `docker` with `install`, `fetch`, `pull`, `push`, `clone` or a URL, and nearly every `gh` command. Use Windows Firewall, a proxy, a container or a VM for real restrictions.

### Limits and output

| Key | Default | Meaning |
|---|---|---|
| `max_file_read_bytes` | `1000000` | Byte-read cap; `replace_text`/`multi_edit` refuse bigger files (`file_too_large`). |
| `max_read_chars` | `60000` | Line-mode `read` budget (minimum 64). |
| `max_inline_output_bytes` | `30000` | Per-stream `execute` output in the reply; default `process read` page. |
| `max_capture_bytes` | `1000000` | Output saved to the log file when cut. |
| `process_buffer_bytes` | `4000000` | Kept output per process; the oldest part is dropped past it. |
| `max_result_bytes` | `200000` | Any tool result above this is saved to a file (minimum 1024). |
| `spill_dir` | per-config folder under `%TEMP%\localforge-results` | Where saved output goes; added to the read roots. |
| `max_concurrency` | `8` | Tool calls run in parallel. |
| `edit_diff_max_bytes` | `4000` | Diff returned by edits; `0` turns it off. |
| `edit_checks` | `{}` | File glob to argv run after each edit of a matching file. |
| `edit_check_timeout_seconds` | `30` | Timeout of an edit check. |
| `load_project_instructions` | `true` | `false`: `workspace` reads no instruction files. |
| `project_instruction_files` | `["AGENTS.md", "CLAUDE.md"]` | Instruction file names. |
| `max_project_instruction_bytes` | `8000` | Total budget for them. |
| `log_to_client` | `false` | MCP log notification per tool call (`LOCALFORGE_LOG=1` writes it to stderr). |
| `structured_content` | `false` | Add `structuredContent` to tool results. |
| `advanced_features` | `false` | See [Advanced features](#advanced-features). |
| `require_absolute_paths` | `true` | File changes, `git`, `execute` and `process start` need an absolute `path`/`destination`/`cwd` (`relative_path` otherwise), since the runtime cwd is shared by every chat and task of the client. `false` lets them use the cwd. |

`edit_checks` example (keep checks as argv arrays: wrapping one in `cmd /c` or `powershell -Command` lets a crafted file name inject shell syntax):

```json
"edit_checks": {
  "*.py": ["python", "-m", "py_compile", "{path}"],
  "*.js": ["node", "--check", "{path}"]
}
```

The first matching glob wins and `{path}` becomes the edited file. The result carries `check` (`{"ok": true}` or the failing command, exit code and output); a failing check does not undo the edit. Checks need a mode that runs processes.

## Tools

Argument names are forgiving (`old`/`find` for `old_text`, `new`/`replacement` for `new_text`, `text` for `content`, `file`/`target` for `path`, `start`/`end` for `line_start`/`line_end`, `dest` for `destination`, `pattern` for `query`, `cmd` for `command`, `id` for `process_id`, `dir` for the `workspace` path); a canonical name wins. Values are fixed to the schema type when unambiguous (`"true"`, `"42"`, a bare string for a list, a JSON string for an array or object); anything else fails with one `invalid_arguments` listing every bad argument.

### `workspace`

- `get`: workspace root, cwd, Git root, and `project_instructions`: the instruction files in every folder from the Git root (or `workspace_root`) down to the cwd, outermost first, within `max_project_instruction_bytes` (a file past it is listed as `{"path", "skipped": "budget"}`). In a Git repo also `branch` (`(detached at <sha>)` on a detached HEAD), `dirty` (`git status --porcelain` lines, at most 50; `dirty_truncated` and `dirty_count` when cut) and `recent_commits` (the last 5 as `<sha> <subject>`); a field whose git call fails or takes over 5 s is left out. Git state needs a mode that allows running git (`DEVELOPMENT`, `FULL_ACCESS`); `READ_ONLY` and `WORKSPACE` leave it out, since `git status` runs programs named in `.git/config`. Call it once at the start of a task.
- `set_cwd`: change the cwd inside the read roots; returns the same fields. Use it to switch projects; for a one-off command pass `cwd` instead. `cd` inside a command does not carry over. In `batch` it needs `sequential: true`. The cwd is shared by every chat and task, so with `require_absolute_paths` (default) it only serves reads and searches; changes and commands name their own absolute location.

### `filesystem`

Actions: `read`, `list`, `stat`, `write`, `replace_text`, `apply_patch`, `mkdir`, `delete`, `move`, `copy` (and `multi_edit`, see [Advanced features](#advanced-features)).

- `read`: byte mode by default (`offset`, `max_bytes`). Pass `line_start`/`line_end` for line mode: numbered `N<TAB>line` text, up to 2000 lines or `max_read_chars`, each line cut to 2000 characters (`...[line cut: N chars]`); continue from `next_line`; `total_lines` when the read reaches the end. A negative `line_start` (`-50`) reads the last lines; one past the end returns the last lines with a `note`. `read` on a directory lists it, with a `note`.
- `list`: paginated (`limit` default 200, max 1000; `offset`), with `glob` and `include_hidden`.
- `write`: atomic full replacement; `mkdir` with `recursive` creates parents.
- `replace_text`: exactly `expected_occurrences` (default 1) matches of `old_text`, or every match with `replace_all: true` (at least one, exact only, not with `expected_occurrences`). With no exact match it retries ignoring typographic quotes, dashes and spaces, then trailing whitespace per line, then `old_text` copied with line-mode `N<TAB>` prefixes; a fallback applies only with exactly the expected count, and the result names it (`"match": "typography"`, `"trailing_whitespace"`, `"line_numbers"`). A miss returns the closest lines, numbered, to retry from. An empty `old_text` is refused.
- `apply_patch`: unified diff (git headers fine), modify or create, all-or-nothing. Hunks are placed by content: their context and removed lines must match exactly, at the stated line or the nearest match after the previous hunk; header line counts are not checked. Hunks placed elsewhere are listed in `hunks: [{"stated", "placed"}]`. Renames, copies, binary patches and a file named in two sections are refused.
- `move`/`copy`: create missing parents; an existing destination file needs `overwrite: true`, an existing directory is never replaced; `copy` of a directory needs `recursive`.
- `delete`: `recursive` for non-empty folders. Read-only files are deleted too (so a folder holding a Git repository goes). Links are removed, never followed; a read-only file with other hard links is refused.
- Edits return a short `diff` (capped at `edit_diff_max_bytes`, `diff_skipped` for huge or binary files) and `check` when `edit_checks` match. Line endings and a UTF-8 BOM are kept: reads hide the BOM, edits match without it and write it back.
- A missing path gets similar names in the error. A relative path missing from the cwd but present from the Git root is read from there (`read`, `stat`, `list`, `search`) with a `note`; writes are not retried and name the Git-root path instead (`./name` creates a file in the cwd).

### `search`

Text or filename search with `glob`/`exclude` (ripgrep rules; `.git`, `.venv`, `node_modules` and similar are always excluded), `case_sensitive`, `fixed_string` (default true; false for regex), `context_lines` (0-5), `files_only`, `include_hidden`, `max_file_size_bytes`. Results are grouped by file: `[{path, matches: [{line, text, before?, after?}]}]` with `match_count`. `max_results` (default 200) counts matches; a cut page is spread round-robin across files (`"spread": true`) and gives `total_matches` (or `total_matches_at_least` from the Python engine). Long lines are cut to 2000 characters. A regex ripgrep's default engine rejects (look-around, backreferences) is retried with `--pcre2` when available, with a `note`. A ripgrep run over 60 s fails with `timeout`.

### `git`

Presets `status`, `diff`, `log`, `show`, `branch`, `root`, plus `run` with any argument array:

```json
{"action": "run", "args": ["commit", "-m", "fix parser", "--", "src/parser.py"]}
```

- git never prompts: `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_EDITOR=true`, `GIT_SEQUENCE_EDITOR=true` (so `commit` without `-m` fails at once). Presets also get `GIT_OPTIONAL_LOCKS=0`. Values the config sets or denies are left alone.
- `diff` with `include_untracked: true` also shows untracked, non-ignored files as added, through a throwaway index (the real one is untouched). Each new file is cut to 8000 bytes; `untracked_files` counts them, `untracked_skipped` lists nested repositories and files over `max_file_read_bytes`. Working tree only: `--cached`, `--staged`, a revision or colour output fails with `invalid_arguments`.
- `git add -A` / `--all` / `.` and staging private keys (`*.pfx`, `*.p12`, `*.pem`, `*.key`) are refused; stage explicit files.
- There is no GitHub API tool: run `gh` through `execute` after a one-time `gh auth login`.

### `execute`

Runs a command and waits. Prefer argv arrays (`{"command": ["npm", "test"], "cwd": "C:\\src\\app"}`); a string runs in `default_shell`. Arguments: `cwd`, `timeout` (seconds), `kill_on_timeout`, `shell`, `env` (per call; nothing carries over), `input` (stdin, max 65536 chars; empty stdin otherwise).

- Each stream returns at most `max_inline_output_bytes`, head and tail; cut output is saved as `stdout_path`/`stderr_path` for `filesystem read` or `search`.
- A command still running at its timeout (default `execute_background_after_seconds`) keeps running as a process entry: `success: false`, `error_type: "backgrounded"`, `process_id` (`exec-...`), `next_after`. Follow it with `process wait`/`read`, end it with `process stop`. `kill_on_timeout: true` stops it instead (`error_type: "timeout"`). So does a full `max_processes`. A command that exits while something it started holds its output is kept the same way.
- An argv command whose program cannot be found fails with `command_not_found` (on Windows with a PATH / `shell: true` hint for `.cmd`, `.bat` and `.ps1`).
- Output is UTF-8: on Windows the server switches its console to code page 65001 and children get `PYTHONUTF8=1` unless set (pass `env: {"PYTHONUTF8": "0"}` for legacy programs). Children also get the two git no-prompt variables.
- Complex quoting (multi-line PowerShell, nested `python -c`) is easier as a script written with `filesystem write` and run by path. For ssh, use key auth with `-o BatchMode=yes` so prompts never hang.

### `process`

Actions `start` (`command`, optional `process_id`, `cwd`, `env`, `shell`), `read` (`after` cursor from `next_after`, `limit_bytes`, `wait_ms`, `format` `text` or `chunks`), `wait`, `write` (stdin `text`), `status`, `list`, `restart`, `stop` (stops the whole tree; `force` kills it).

`wait` blocks until the process exits, a finished output line matches the regex `pattern`, or `wait_ms` passes (default and max 60000), and returns `reason` (`matched`, `exited`, `timeout`), `matched`, `tail` (`tail_lines`, default 40) and `next_after`; a `wait` from that cursor goes on after the matched line. Use it instead of polling `read` during builds and tests. `cursor_lost: true` means older output was dropped from the buffer. Process state is in memory only.

### `todo`

Persistent task list: `get`, `add` (`title`), `update` (`id`, `title`, `status`: `pending`, `in_progress`, `completed`), `delete`, `clear`. Stored in `state_file`, so it survives restarts.

## Advanced features

`batch`, `multi_search` and `filesystem` `multi_edit` need `"advanced_features": true` in `localforge.json` (default false). Off, they are not listed, and a call to them fails like any unknown tool, action or argument.

```json
{
  "workspace_root": "C:\\src\\my-app",
  "advanced_features": true
}
```

- `batch`: up to 16 calls (`{"tool", "arguments", "label"}`) in one request, in parallel; distinct paths run concurrently and edits to one file serialize. Results come back with `index` and `label`; a failing call is reported on its own. With `"sequential": true` calls run in order and stop at the first failure (an error or `success: false`); the rest come back `skipped`, with `completed` and `stopped_at`. A sequential run that outlasts `execute_background_after_seconds` skips the calls left. Entries may name `bash`, `shell`, `sh`, `powershell`, `pwsh`, `cmd` (run as `execute`, in `default_shell`) or `grep`, `rg`, `ripgrep` (run as `search`), with a `note`. Nested `batch` is refused.
- `multi_search`: up to 16 `search` entries (with optional `label`) in parallel; a bad entry fails only itself.
- `multi_edit`: several exact replacements (`edits`, up to 32, each with `old_text`, `new_text`, optional `expected_occurrences` or `replace_all`) to one file, applied in order, validated end-to-end and written once; one failing edit leaves the file untouched.

**Warning:** These features can cause Quick agents to have issues: responses often get interrupted for various reasons, and sub-agents especially can get stuck. The cause hasn't been found and it happens often. Use at your own risk.

## Large results

Any tool result over `max_result_bytes` is saved whole as JSON in `spill_dir` and the reply keeps its fields with long strings and lists cut (to fit a quarter of `max_result_bytes`), plus `result_too_large: true`, `bytes`, `result_path` and a `hint`. Paths and ids are never cut; cursors that described the cut value are dropped, so continue from `result_path`. Only `lf-*` files in `spill_dir` are pruned (newest 100 kept). Saved files can hold anything a tool returned: do not share one `spill_dir` between configs with different policies.

## Safety notes

- Not a sandbox: allowed commands run with your Windows permissions, and build scripts, package hooks, Git hooks and interpreters can reach anything you can. For untrusted agents or repositories use a low-privilege account, VM or container. See [`SECURITY.md`](SECURITY.md).
- The dedicated tools resolve links before checking roots and `denied_paths`; a check-to-use race with a swapped reparse point remains possible.
- `READ_ONLY` refuses every write; `READ_ONLY` and `WORKSPACE` refuse every command (`execution_denied`).
- Secret-looking variables are filtered from children, and output gets basic secret redaction. Neither is complete.
- `AGENTS.md`/`CLAUDE.md` from a repository steer the agent; set `project_instruction_files` to `[]` (or `load_project_instructions` to `false`) for untrusted code.
- There is no approval tool: approving calls is the MCP host's job.

## Limitations

- Started programs end when the server exits, however it exits, through a kill-on-close Windows job (`kill_children_on_exit`). Exception: Microsoft Store (MSIX) apps, including a Store Python and venvs made from it, start in their package's own job and can outlive the server and a job-based stop. Make the venv from a python.org install.
- On POSIX, children end only on `process stop`/`restart` or a clean server exit, and Windows-only behaviour (job objects, code page, read-only delete) does not apply.
- Process state is in memory and does not survive a restart; the `todo` list does.
- Network detection is conservative and cannot see inside scripts.
- Writes are atomic at replacement but may not keep every filesystem metadata attribute.
- Transport is stdio only.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q agent_runtime
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT, see [`LICENSE`](LICENSE).
