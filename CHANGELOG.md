# Changelog

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
