# Changelog

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
