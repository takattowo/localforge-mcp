# Security

## Boundary

LocalForge MCP applies application-level policy before its dedicated tools touch files or launch processes. It is not an operating-system sandbox. Any allowed command runs with the Windows permissions of the user running LocalForge MCP and may access resources outside configured roots through child processes, scripts, interpreters, package hooks, Git hooks, or native APIs.

Use a dedicated low-privilege Windows account, VM, container, AppContainer, or restricted token if untrusted agent output or repository content is in scope. Use Windows Firewall or a controlled proxy for enforceable network restrictions.

Existing symlinks and junctions are resolved before policy checks. A check-to-use race remains possible if another process can replace a path with a reparse point between validation and access.

## Reporting

Do not open a public issue for an exploitable vulnerability. Use the repository security-advisory channel configured by the maintainer.
