# Changelog

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
