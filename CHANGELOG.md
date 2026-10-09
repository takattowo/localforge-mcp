# Changelog

## 1.6.0

- New `todo` tool: persistent task list (get, add, update, delete, clear) backed by the state file, so agent task tracking survives server restarts; in-memory only when no state file is configured.
- New `filesystem` action `multi_edit`: several exact replacements in one file, applied in order and validated end-to-end before a single atomic write. A failing edit leaves the file untouched — a coherent multi-part change never lands half-applied.
- Tolerant argument aliases: the dispatch layer renames common model guesses before dispatch — `old`/`find`→`old_text`, `new`/`replacement`→`new_text`, `text`→`content`, `file`/`target`→`path`, `start`/`end`→`line_start`/`line_end`, `dest`→`destination`, `pattern`→`query` (search), `cmd`→`command` (execute/process), `id`→`process_id`, `dir`→`path` (workspace). A canonical name always wins when both are sent.
- `multi_search` entries accept the search tool's aliases.
- State-file writes are now serialized and merge per key: a concurrent `set_cwd` and todo update can no longer drop each other's state.
- New `QUICK.md`: copy-paste agent operating rules for Amazon Quick — batch-first dispatch, filesystem-first edits, an exhaustive no-shell-editing prohibition naming every channel (`execute`, `process`, `python`, scripts, `sed`, redirection), `process` for long waits, and `todo` for task tracking.
- `agent_runtime.__version__` now matches `pyproject.toml` (it had drifted to 1.5.0).

## 1.5.1

- `batch` accepts host-prefixed tool names (e.g. `Birb_localforge_mcp__search`) in call entries; the prefix is stripped before dispatch, so batches no longer fail with tool_not_found when the model uses the names it was given.
- `batch` and `multi_search` validation errors now carry a concrete JSON example of the expected shape, so a model that probes them with an empty array learns the correct call in one round-trip instead of abandoning the tool.
- `batch`/`multi_search` schemas and descriptions include inline examples; batch call entries require `tool` in the JSON schema.

## 1.5.0

- Concurrent tool execution: JSON-RPC requests are handled on a bounded thread pool (`max_concurrency`, default 8), so pipelined tool calls run in parallel instead of queueing behind the slowest search or execute.
- New `batch` tool: run up to 16 tool calls of any type in one round-trip, with per-call error isolation, labels, and same-path write serialization.
- New `multi_search` tool: run up to 16 independent searches in one round-trip, with per-entry error isolation and optional labels.
- Filesystem mutations (write, replace_text, apply_patch, copy, move, mkdir, delete) now lock the target path, so concurrent edits to one file serialize like a CLI agent instead of silently overwriting each other.
- execute children no longer inherit the MCP control channel: stdin is /dev/null unless input is passed. An inherited control pipe could hang the child on Windows, and a child reading stdin would consume the JSON-RPC request stream. Same for the ripgrep, git rev-parse, and taskkill helpers.
- Runtime cwd is now lock-guarded for concurrent `set_cwd`; state-file temp names are unique per writer thread.
- Tool descriptions steer file edits to the filesystem write/replace_text/apply_patch actions instead of shelling out to python or shell commands.

## 1.4.2

- gh CLI counts as network activity (except local-only invocations), closing the network-disabled bypass.
- Python search fallback honors regex when fixed_string is false, with invalid-pattern errors.
- filesystem move creates missing parent dirs like copy and write.
- README documents the GitHub-via-gh pattern: one-time gh auth login, --json output.

- replace_text rejects identical old/new text and points at expected_occurrences on count mismatch.
- Search surfaces ripgrep's stderr when it exits with an error instead of returning bare empty results.
- README documents the write-script-then-run pattern for complex shell quoting.

- New filesystem copy action for policy-checked file and directory copies without shell quoting issues.
- Stringified JSON argv arrays are now coerced back to arrays instead of rejected.
- path_outside_roots errors name the config key and the restart step.
- Secret redaction no longer mangles code listings (call expressions, dotted references, literals).
- git run blocks broad `add -A` / `--all` / `.` and refuses to stage private keys.
- Inherit `%ProgramData%` by default; without it OpenSSH for Windows dies instantly with exit 255 and no output.
- Empty-output failures now say so instead of returning blank streams.
- SSH usage notes: fail-fast flags and the Downloads key-permission fix.

- New filesystem apply_patch action for unified-diff edits with dry-run validation.
- Patch application normalizes line endings to LF.

## 1.2.1

- Stringified JSON arrays are rejected with guidance instead of failing in PowerShell.
- Unknown tool arguments suggest close matches and list valid arguments.

## 1.2.0

- Documented every tool schema field; fixed shell auto-enable and timeout-unit docs with per-tool examples.
- Filesystem errors now return structured codes (path_not_found, permission_denied, io_error) instead of Internal error.
- Search adds default excludes, context lines, hidden and size guards, single-file paths, and files_only validation.
- Filesystem list is paginated and filterable; read supports line ranges with line numbers.
- Execute truncation is byte-correct and accepts stdin input.
- Process streaming handles split multibyte output; exited entries are garbage-collected with a process_limit.
- Runtime cwd persists across server restarts via a best-effort sidecar file.
- Added LOCALFORGE_LOG=1 stderr request logging.

## 1.1.1

- String commands automatically use configured shell when clients omit shell=true.
- Tool schema clarifies timeout values are seconds, not milliseconds.
- Added client guidance for command and shell parameters.

## 1.1.0

- Renamed project to LocalForge MCP.
- Removed runtime approval tools because host clients already approve tasks.
- Fixed read-only list and stat operations.
- Fixed duplicate process IDs spawning orphan processes.
- Preserved shell and environment settings across process restart.
- Added explicit PowerShell, pwsh, cmd, and sh backends.
- Added process-tree termination for foreground timeouts.
- Fixed ripgrep filename-search invocation and normalized search results.
- Added bounded file reads, binary-file rejection, and guarded text replacement.
- Added JSON-RPC notification suppression and stronger argument errors.
- Added cursor-loss reporting for bounded process logs.
- Added public-repository metadata, license, security notes, and expanded tests.
