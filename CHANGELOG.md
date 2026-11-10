# Changelog

## 1.9.2

- The default excludes (`.git`, `.venv`, `__pycache__`, `node_modules`, `.hg`, `target`, `dist`, `build`, `*.egg-info`) now apply at any depth under ripgrep, e.g. `packages/web/node_modules`. ripgrep reads a glob with a slash as anchored to the search root, so they only covered the top level. They are now reported as `**/node_modules/` etc. in `applied_excludes`; the trailing slash makes ripgrep skip those folders instead of walking them, and keeps files with those names.
- The Python search fallback now matches globs like ripgrep `-g` instead of `Path.match`: a glob with a folder is anchored to the search root (`src/*.py` no longer matches `a/src/x.py`), `**` spans any number of folders on every Python version, `{a,b}` alternates (a slash anywhere in the pattern anchors every alternative), matching is case-sensitive on Windows too, a bare-name exclude hides matching folders at any depth, a folder-only include (`src/`) matches no file, an empty glob is ignored, and `[!]x]` is a class. Each verified against ripgrep 15.1.0.
- The Python fallback's glob matching no longer backtracks exponentially on star and `**` chunks: they are matched atomically and runs of `**/` collapse, so patterns like `*a*a*a*a*a*a*a*a*a*a*b` or `**/**/**/…/y` no longer take seconds per path. A few contrived shapes (many negated classes between stars, or repeated `**/*/` on very deep paths) can still be slow. A glob that expands to more than 64 brace alternatives is rejected with `invalid_arguments`.
- Python fallback, more ripgrep parity (each checked live): a folder-only exclude ending in `**` (`src/**/`, `**/`) hides only folders below it and keeps the files beside it; a pattern of slashes alone matches nothing as an include and is ignored as an exclude; a class whose range covers `/` (e.g. `[ -~]`) can match it; `&`, `~`, `|` and `[` in a class are literal, and a doubled `-` no longer raises a FutureWarning; ranges such as `[+--]` and `[--0]` keep their meaning. Like ripgrep, a range followed by `-X` is extended (`[a-c-e]` is a to e) and `[a-b--c]` is rejected as an invalid range.
- A single-file search with the Python fallback reads only that file, and ignores excludes like ripgrep does for an explicit path; it used to also search same-named files in subfolders.

## 1.9.1

- `search` globs with a folder (e.g. `src/*.py`, `src/**/*.cs`) now match with ripgrep. ripgrep matches such globs against the path relative to its working directory, and it ran from the server's directory instead of the search root, so they silently returned nothing. Include and exclude globs (including the built-in excludes such as `node_modules/**`) are now relative to the search `path`; before, ripgrep ignored excludes unless the server happened to run from the search root.
- `search` `truncated` is true only when results were actually cut, not whenever the result count equals `max_results`.

## 1.9.0

- Breaking response shapes: line-mode `read` returns `content` instead of `lines` and omits `total_lines` when truncated; search context returns `before`/`after` instead of `context`; `process read` returns `stdout`/`stderr` instead of `chunks` unless `format: "chunks"`; `structuredContent` is opt-in. Clients that parse the old fields must update.
- Edits keep line endings: `write` (existing file), `replace_text`, `multi_edit`, and `apply_patch` preserve the file's dominant line ending instead of converting LF files to CRLF on Windows. New files, and files with no line break yet, are written exactly as sent. Detection runs on decoded text, so `write`, `replace_text`, and `multi_edit` work on UTF-16 files (line-mode `read` and `apply_patch` still reject them as binary). `write` reports the kept ending as `eol`; delete the file first to change it. `old_text`/`new_text` copied from a CRLF byte read still match. `replace_text` and `multi_edit` count matches in the LF view the agent reads and leave untouched lines byte-exact (mixed endings, stray CRs) when that gives the same change; otherwise the edit is applied to normalized text. `apply_patch` keeps each untouched line's ending, splits lines on LF only (a form feed no longer breaks a patch), and adds a line break when lines are added after a last line that had none. A zero-context insert hunk (`@@ -N,0 ... @@`) now inserts after line N instead of before it; one past the end of the file is rejected. Reported `bytes` are now the bytes actually written.
- `execute`, `git`, and `process start` resolve a missing or relative `cwd` against the runtime cwd (`set_cwd`). `process start` used to run in the workspace root.
- Line-mode `read` reads in bounded pieces (no whole-file load, no `max_file_read_bytes` limit, over-long lines skipped without loading) and returns numbered text in `content` (one `N<TAB>line` per line) instead of a `lines` array of objects. Output stops at 2000 lines or `max_read_chars` (default 60000, minimum 64) instead of scanning to the end; `next_line` says where to continue, and `total_lines` appears only when the read reaches the end of the file. Only LF ends a line, as in ripgrep. Over-long lines are cut.
- Search context comes back as `before`/`after` lists of `N<TAB>line` instead of one unlabeled `context` list, and ripgrep context no longer bleeds across file boundaries. Both search engines include neighboring match lines in context and split lines on LF only, so they return the same context and line numbers. Result paths under the runtime cwd are relative to it.
- `execute` output over `max_capture_bytes` keeps the head and the tail (where build and test errors usually are) with a cut marker, instead of the head only.
- `process read` returns `stdout`/`stderr` strings by default; `format: "chunks"` keeps the previous per-chunk shape.
- Tool results use compact JSON. `structuredContent` is opt-in (`structured_content: true`): Quick showed both copies on failed calls.

## 1.8.0

- Durability hardening for long-running sessions:
  - `process write` and stdio responses now write on a helper thread with a 30s timeout (new `stdin_blocked` code); a child that never drains stdin, or a host that stops reading stdout, can no longer pin a worker thread forever.
  - Pending stdio requests are bounded at 128: a request flood gets an immediate per-request error (`-32603`) instead of unbounded memory growth, and a clean EOF no longer waits on an unbounded backlog.
  - Children are adopted into a Windows Job Object with kill-on-close (via `ctypes`, no dependencies): they die with the server however it exits — taskkill, crash, host restart after hibernation — not just on clean exit. Grandchildren join the job automatically. Set `kill_children_on_exit: false` to opt out. Off Windows, adoption is a no-op.
  - `process read` waits on a condition variable instead of polling at 20 Hz; idle waits cost no CPU.
  - Once stdout is stuck or broken, the server stops writing for good instead of silently failing every subsequent response.

## 1.7.0

- MCP protocol bumped to 2025-06-18. `tools/call` results now carry `structuredContent` (the typed result object) alongside the text content, so clients that support it get typed results instead of parsing JSON text; the text form is unchanged for older clients.
- New `logging` capability: with `log_to_client=true` in the config, every tool call emits an MCP logging notification (tool, duration, outcome) visible in the client. `LOCALFORGE_LOG=1` still writes the same line to stderr.
- `workspace get` caches the Git root per directory for 30 seconds instead of spawning `git rev-parse` on every call.
- `replace_text` and `multi_edit` refuse files larger than `max_file_read_bytes` (`file_too_large`) instead of reading unbounded files into memory.
- Path locks are now striped into a fixed table of 256 locks (bounded memory over long sessions); the same path always maps to the same stripe so same-file edits still serialize, and stripes are acquired in a process-stable order so overlapping edits cannot deadlock.

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
