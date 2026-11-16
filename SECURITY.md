# Security

## Boundary

LocalForge MCP applies application-level policy before its dedicated tools touch files or launch processes. It is not an operating-system sandbox. Any allowed command runs with the Windows permissions of the user running LocalForge MCP and may access resources outside configured roots through child processes, scripts, interpreters, package hooks, Git hooks, or native APIs.

Use a dedicated low-privilege Windows account, VM, container, AppContainer, or restricted token if untrusted agent output or repository content is in scope. Use Windows Firewall or a controlled proxy for enforceable network restrictions.

Existing symlinks and junctions are resolved before policy checks. A check-to-use race remains possible if another process can replace a path with a reparse point between validation and access.

Saved output: oversized `execute` output and tool results are written to `spill_dir` (default: a per-config folder under `%TEMP%\localforge-results`), even in `READ_ONLY` mode, and that folder is added to the read roots. Saved files can hold anything a tool returned, so do not point two configs with different policies at the same `spill_dir`. `denied_paths` still apply inside it.

Edit checks: `edit_checks` commands run after every edit of a matching file, with the agent-chosen path substituted for `{path}`. They need a mode that allows process execution and run like any `execute` call. Keep them as argv arrays; routing one through a shell lets a crafted file name inject commands.

Project instructions: `workspace get` returns `AGENTS.md`/`CLAUDE.md` from the repository, and the tool description tells the agent to follow them. An untrusted repository can use these files to steer the agent. Set `project_instruction_files` to `[]` when working on untrusted code.

Git-root retry: a relative path missing from the runtime cwd can be resolved from the Git root instead, for reads (`read`, `stat`, `list`, `search`). The path found there goes through the same read policy (roots, `denied_paths`) as any other; writes are never retried there.

Untracked diff: `git` `diff` with `include_untracked` runs `git ls-files`, `git rev-parse` and `git add --intent-to-add` itself, outside `execute`, under the same command policy as the plain diff. The add goes to a fresh throwaway index (`GIT_INDEX_FILE` in the temp folder, deleted afterwards); the real index is never touched.

## Reporting

Do not open a public issue for an exploitable vulnerability. Use the repository security-advisory channel configured by the maintainer.
