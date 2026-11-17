# LocalForge MCP

Local Windows coding runtime for AWS Quick and other MCP clients. It gives an agent a real workspace: files, code search, Git, commands, and long-running processes, run under your own Windows account. Policy checks (roots, denied paths, modes) apply before the tools touch anything; it is not an OS sandbox.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- Git, for the `git` tool
- ripgrep (optional, recommended): faster `search`

## Install

```powershell
cd C:\path\to\localforge-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item localforge.example.json localforge.json
```

Then edit `localforge.json` (see Config).

## Add to Quick

Create a Local MCP connection:

- Command: `<repo>\.venv\Scripts\python.exe`
- Arguments: `-m agent_runtime.server`
- Environment: `LOCALFORGE_CONFIG=<path to localforge.json>`

Toggle the connection off and on (or restart Quick) after changing the config. Other MCP clients use the same command, arguments, and environment. [`QUICK.md`](QUICK.md) has optional agent rules to paste into the connection's prompt.

## Config

Minimal `localforge.json`:

```json
{
  "workspace_root": "C:\\src\\my-app",
  "mode": "DEVELOPMENT",
  "allowed_read_roots": ["C:\\src\\my-app"],
  "allowed_write_roots": ["C:\\src\\my-app"],
  "denied_paths": ["C:\\Users\\me\\.ssh"]
}
```

`mode` is `READ_ONLY`, `WORKSPACE`, `DEVELOPMENT` (default), or `FULL_ACCESS`. `localforge.example.json` shows the other common options.

## Tools

- `workspace`: roots, cwd, and Git root; `get` also returns the project's AGENTS.md/CLAUDE.md.
- `filesystem`: read, list, stat, write, replace_text, apply_patch, mkdir, delete, move, copy.
- `search`: text or filename search (ripgrep, with a Python fallback).
- `git`: status, diff, log, show, branch, root, or any git arguments via `run`.
- `execute`: run a command and get structured output; long runs move to the background.
- `process`: start, read, write, status, list, restart, and stop long-running processes.
- `todo`: persistent task list.

## Safety notes

- Not a sandbox: allowed commands run with your Windows permissions and can reach anything you can. See [`SECURITY.md`](SECURITY.md).
- The dedicated tools only read inside `allowed_read_roots` and write inside `allowed_write_roots` (`FULL_ACCESS` skips these checks). `denied_paths` always apply. `READ_ONLY` refuses every write.
- `git add -A` / `--all` / `.` is refused; git never prompts.
- Started programs run in a kill-on-close Windows job and end when the server exits (`kill_children_on_exit`, default true).
- Microsoft Store (MSIX) apps, including a Store Python and venvs made from it, start outside that job and can outlive the server. Make the venv from a python.org install.

## License

MIT, see [`LICENSE`](LICENSE).
