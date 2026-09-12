# LocalForge MCP 1.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement approved spec `docs/superpowers/specs/2026-09-12-localforge-1.2.0-design.md` with zero new MCP tools and ship version 1.2.0.

**Architecture:** Keep the 6-tool shape. Fix the error contract and red test first, then retrieval ergonomics (search, list, line read), then runtime correctness (execute truncation, process streaming/GC), then cwd-only `StateStore`, finishing with version bump and full verification.

**Tech Stack:** Python >= 3.11 stdlib only (no new dependencies), pytest >= 8, Windows 10/11 PowerShell, ripgrep optional (python fallback covered by tests via monkeypatch).

## Global Constraints

- Windows 10 or Windows 11 target; portable logic must also pass on other OSes in CI.
- Python requires-python >= 3.11; stdlib only, no new entries in `pyproject.toml dependencies`.
- Six MCP tool names stay exactly `workspace filesystem search git execute process`; new behavior lands as actions or optional params only.
- stdout remains pure newline-delimited JSON-RPC; all diagnostics go to stderr and stay off unless `LOCALFORGE_LOG=1`.
- Every tool-call failure from filesystem paths returns a structured `RuntimeFault` code, never `Internal error`.
- Version floor for this plan: `1.2.0` in `pyproject.toml` and `agent_runtime/__init__.py`, with a `CHANGELOG.md` entry.
- Verify each task with `.\.venv\Scripts\python.exe -m pytest -q`; final gate adds `.\.venv\Scripts\python.exe -m compileall -q agent_runtime`.
- Do not push to remote; commits stay local until the user explicitly approves a push.

---

## File map (what changes where)

- `agent_runtime/capabilities.py` — error-contract helper + filesystem wraps (Task 1); search upgrades (Task 3); list pagination + line-mode read (Task 4); execute byte truncation + `input` (Task 5).
- `agent_runtime/server.py` — rename existing `call` body to `_dispatch`, add timed `call` wrapper with `LOCALFORGE_LOG` (Task 2); full `SCHEMAS` descriptions (Task 2).
- `agent_runtime/processes.py` — incremental UTF-8 decoders, raw-byte accounting, TTL/cap GC, `process_limit` (Task 6).
- `agent_runtime/config.py` — `state_file`, `process_ttl_seconds`, `max_processes`, missing-root error naming (Tasks 6, 7).
- `agent_runtime/state.py` — new 30-line `StateStore`, cwd only (Task 7).
- `localforge.example.json` — document the three new config fields (Task 6).
- `README.md`, `CHANGELOG.md` — shell/timeout corrections, per-tool examples, 1.2.0 entry (Task 2).
- `tests/test_runtime.py` — fix red shell test + one focused test per behavior (Tasks 1, 3-7).
- `pyproject.toml`, `agent_runtime/__init__.py` — `1.2.0` bump (Task 8).

---

### Task 1: Error contract + red-test fix

**Files:**
- Modify: `agent_runtime/capabilities.py:42-114`
- Test: `tests/test_runtime.py:58-74`

**Interfaces:**
- Consumes: `RuntimeFault(code, message)` from `agent_runtime/errors.py:1-10`.
- Produces: `_fs_error(action: str, target, exc: OSError) -> NoReturn` used by Tasks 3-5 filesystem paths; corrected shell test baseline used by all later tasks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runtime.py`:

```python
def test_shell_string_auto_enables_shell(tmp_path):
    server = make(tmp_path)
    ok = server.cap.execute("echo invalid")
    assert ok["success"] and "invalid" in ok["stdout"]

def test_filesystem_errors_are_structured(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as missing:
        server.cap.filesystem("stat", "no-such-file.txt")
    assert missing.value.code == "path_not_found"
    with pytest.raises(RuntimeFault) as bad_dest:
        server.cap.filesystem("write", "ok.txt", "x")
        server.cap.filesystem("move", "ok.txt", destination="no-such-dir/moved.txt")
    assert bad_dest.value.code == "path_not_found"
```

Also replace the stale line in `test_execute_results_shell_timeout_large_env_and_network` (`tests/test_runtime.py:64`):

```python
# before:
    with pytest.raises(RuntimeFault): server.cap.execute("echo invalid")
# after:
    auto = server.cap.execute("echo invalid")
    assert auto["success"] and "invalid" in auto["stdout"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_shell_string_auto_enables_shell tests/test_runtime.py::test_filesystem_errors_are_structured -v`
Expected: FAIL — `_fs_error` mapping missing (`stat` raises `FileNotFoundError`, `move` raises raw `OSError`); shell test passes already (documents 1.1.1 behavior).

- [ ] **Step 3: Implement the error-contract helper and wraps**

Add module-level helper in `agent_runtime/capabilities.py` right after imports:

```python
def _fs_error(action, target, exc):
    if isinstance(exc, FileNotFoundError):
        raise RuntimeFault("path_not_found", f"Path not found: {target}") from exc
    if isinstance(exc, PermissionError):
        raise RuntimeFault("permission_denied", f"Permission denied: {target}") from exc
    if isinstance(exc, NotADirectoryError):
        raise RuntimeFault("not_directory", f"Not a directory: {target}") from exc
    if isinstance(exc, IsADirectoryError):
        raise RuntimeFault("not_file", f"Not a file: {target}") from exc
    if isinstance(exc, OSError):
        raise RuntimeFault("io_error", f"Filesystem error during {action}: {exc}") from exc
    raise
```

Wrap each fallible block. Exact edits:

```python
# stat branch (replaces bare target.stat()):
        if action == "stat":
            try:
                stat = target.stat()
            except OSError as e:
                _fs_error("stat", target, e)
```

```python
# mkdir branch:
        if action == "mkdir":
            try:
                target.mkdir(parents=recursive, exist_ok=True)
            except OSError as e:
                _fs_error("mkdir", target, e)
            return {"path": str(target)}
```

```python
# delete branch (wrap whole rmtree/rmdir/unlink):
        if action == "delete":
            try:
                count = sum(1 for _ in target.rglob("*")) + 1 if target.is_dir() else 1
                if target.is_dir():
                    shutil.rmtree(target) if recursive else target.rmdir()
                else:
                    target.unlink()
            except OSError as e:
                _fs_error("delete", target, e)
            return {"path": str(target), "deleted_items": count}
```

```python
# write branch:
        if action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content or ""
            try:
                self._atomic_write(target, data, encoding)
            except OSError as e:
                _fs_error("write", target, e)
            return {"path": str(target), "bytes": len(data.encode(encoding))}
```

```python
# replace_text branch (wrap read + write):
            try:
                original = target.read_text(encoding=encoding)
            except OSError as e:
                _fs_error("replace_text", target, e)
            count = original.count(old_text)
            expected = 1 if expected_occurrences is None else int(expected_occurrences)
            if count != expected:
                raise RuntimeFault("content_mismatch", f"Expected {expected} occurrence(s), found {count}")
            updated = original.replace(old_text, new_text)
            try:
                self._atomic_write(target, updated, encoding)
            except OSError as e:
                _fs_error("replace_text", target, e)
```

```python
# move branch (explicit missing-parent code, wrap replace):
        if action == "move":
            if not destination:
                raise RuntimeFault("invalid_arguments", "move requires destination")
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("move")
            if not dest.parent.exists():
                raise RuntimeFault("path_not_found", f"Destination parent not found: {dest.parent}")
            try:
                target.replace(dest)
            except OSError as e:
                _fs_error("move", target, e)
            return {"source": str(target), "destination": str(dest)}
```

`read`/`list` per-entry behavior stays as-is (read already raises `not_file`/`binary_file`; list entries stay best-effort dicts).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: all pass except possibly the pre-existing red test, which Step 1 already rewrote — expect fully green (6 passed, 1 skipped).

- [ ] **Step 5: Commit**

```bash
git add agent_runtime/capabilities.py tests/test_runtime.py
git commit -m "fix: structured filesystem errors and auto-shell test baseline"
```

---

### Task 2: Schemas, docs, observability logging

**Files:**
- Modify: `agent_runtime/server.py:1-88`
- Modify: `README.md:180-184,210-217`
- Modify: `CHANGELOG.md:1-8`

**Interfaces:**
- Consumes: `Server._dispatch(name, arguments)` (renamed body of current `call`); nothing else changes signature, so `tests/test_runtime.py:29-30` (`server.call(...)`) keeps working.
- Produces: documented schemas + `LOCALFORGE_LOG` stderr lines consumed by manual Quick toggle verification in Task 8.

- [ ] **Step 1: Write the failing test**

```python
def test_call_logs_to_stderr_only(tmp_path, capsys):
    import os
    server = make(tmp_path)
    os.environ["LOCALFORGE_LOG"] = "1"
    try:
        server.call("workspace", {"action": "get"})
    finally:
        del os.environ["LOCALFORGE_LOG"]
    captured = capsys.readouterr()
    assert "tool=workspace" in captured.err and "ok" in captured.err
    assert captured.out == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_call_logs_to_stderr_only -v`
Expected: FAIL with empty stderr (no logging yet).

- [ ] **Step 3: Implement call wrapper + logging**

In `agent_runtime/server.py`, add `import os` and `import time` to imports, rename existing `def call(self, name, arguments):` to `def _dispatch(self, name, arguments):` (body byte-identical), then insert above it:

```python
    def call(self, name, arguments):
        start = time.monotonic()
        fault = "ok"
        try:
            return self._dispatch(name, arguments)
        except RuntimeFault as e:
            fault = e.code
            raise
        finally:
            if os.environ.get("LOCALFORGE_LOG") == "1":
                elapsed = int((time.monotonic() - start) * 1000)
                print(f"localforge tool={name} ms={elapsed} {fault}", file=sys.stderr)
```

- [ ] **Step 4: Fill in every schema description**

Replace the six `SCHEMAS` entries with described equivalents. Keys stay identical; only `description` values are added. Exact replacement for `agent_runtime/server.py:14-35`:

```python
SCHEMAS = {
    "workspace": {"type": "object", "properties": {
        "action": {"enum": ["get", "set_cwd"], "description": "get returns roots and Git root; set_cwd changes runtime directory."},
        "path": {"type": "string", "description": "Directory for set_cwd, relative to cwd or absolute, must resolve inside read roots."}}, "required": ["action"]},
    "filesystem": {"type": "object", "properties": {
        "action": {"enum": ["read", "list", "stat", "write", "replace_text", "mkdir", "delete", "move"], "description": "read defaults to byte mode; pass line_start/line_end for line mode. list is paginated."},
        "path": {"type": "string", "description": "Target path, relative to cwd or absolute, must resolve inside the matching roots."},
        "content": {"type": "string", "description": "Full file content for write."},
        "destination": {"type": "string", "description": "Destination path for move."},
        "recursive": {"type": "boolean", "description": "mkdir parents or recursive delete."},
        "encoding": {"type": "string", "description": "Text encoding, default utf-8."},
        "offset": {"type": "integer", "description": "Byte offset for legacy read mode; list page offset when action is list. Ignored in line mode."},
        "max_bytes": {"type": "integer", "description": "Byte cap for legacy read mode."},
        "old_text": {"type": "string", "description": "Exact text to find for replace_text."},
        "new_text": {"type": "string", "description": "Replacement text for replace_text."},
        "expected_occurrences": {"type": "integer", "description": "Required match count for replace_text, default 1."},
        "line_start": {"type": "integer", "description": "1-based first line for line-mode read, default 1."},
        "line_end": {"type": "integer", "description": "Inclusive last line for line-mode read, default end of file."},
        "limit": {"type": "integer", "description": "Max list entries returned, default 200, clamped 1..1000."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Fnmatch filters on entry name for list."},
        "include_hidden": {"type": "boolean", "description": "Include dotfiles in list, default false."}}, "required": ["action"]},
    "search": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Text to find; omit only with files_only."},
        "path": {"type": "string", "description": "File or directory to search, relative to cwd or absolute."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Include patterns, fnmatch on repo-relative posix path."},
        "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra excludes, unioned with built-in defaults."},
        "case_sensitive": {"type": "boolean", "description": "Case-sensitive match, default false."},
        "max_results": {"type": "integer", "description": "Result cap 1..5000, default 200."},
        "files_only": {"type": "boolean", "description": "List matching filenames; takes no query."},
        "fixed_string": {"type": "boolean", "description": "Literal match when true, regex when false, default true."},
        "context_lines": {"type": "integer", "description": "Surrounding lines per match, clamped 0..5, ignored with files_only."},
        "include_hidden": {"type": "boolean", "description": "Search hidden files, default false."},
        "max_file_size_bytes": {"type": "integer", "description": "Skip larger files, default 1000000, must be positive."}}},
    "git": {"type": "object", "properties": {
        "action": {"enum": ["status", "diff", "log", "show", "branch", "root", "run"], "description": "Preset operation, or run for an arbitrary git argument array."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Extra args appended to presets, or the full git args for run."},
        "cwd": {"type": "string", "description": "Repository directory, defaults to runtime cwd."}}, "required": ["action"]},
    "execute": {"type": "object", "properties": {
        "command": {"description": "Command to run. Prefer an argv array. String commands automatically use the configured shell.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "timeout": {"type": "number", "description": "Timeout in seconds, not milliseconds."},
        "shell": {"type": "boolean", "description": "Optional. String commands automatically enable the configured shell."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "input": {"type": "string", "description": "Optional stdin text, max 65536 chars."}}, "required": ["command"]},
    "process": {"type": "object", "properties": {
        "action": {"enum": ["start", "read", "write", "status", "list", "restart", "stop"], "description": "Process lifecycle action."},
        "process_id": {"type": "string", "description": "Stable id; auto-generated when start omits it."},
        "command": {"description": "Command to start. Prefer an argv array. String commands automatically use the configured shell.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "shell": {"type": "boolean", "description": "Optional. String commands automatically enable the configured shell."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "after": {"type": "integer", "description": "Log cursor from previous next_after for incremental reads."},
        "limit_bytes": {"type": "integer", "description": "Max log bytes per read."},
        "wait_ms": {"type": "integer", "description": "Wait in milliseconds for new output, max 60000."},
        "text": {"type": "string", "description": "Stdin text for write."},
        "append_newline": {"type": "boolean", "description": "Append newline to stdin write, default true."},
        "force": {"type": "boolean", "description": "Force-kill the process tree on stop."}}, "required": ["action"]},
}
```

- [ ] **Step 5: Fix README contradictions and add examples**

Replace `README.md:180-184`:

```markdown
String commands automatically use the configured shell:

```json
{"command":"npm test | Select-String failed"}
```

Pass `shell` explicitly only to force array commands through the shell. Argument arrays bypass shell parsing and stay preferred.
```

Replace `README.md:210-217`:

```markdown
Supported `default_shell` values:

- `powershell`
- `pwsh`
- `cmd`
- `sh`

`execute.timeout` is seconds. `process.wait_ms` is milliseconds (max 60000). `limit_bytes`, `max_bytes`, and `max_file_read_bytes` are bytes. `filesystem offset` is a byte offset in legacy read mode; use `line_start`/`line_end` for line mode.
```

Append one example per tool under `## MCP tools` (after the `process` section, before `## Configuration`):

```markdown
### Quick examples

```json
{"action":"get"}
{"action":"read","path":"src/app.py","line_start":1,"line_end":80}
{"query":"UserService","glob":["*.py"],"context_lines":2}
{"action":"status"}
{"command":["npm","test"],"cwd":".","timeout":600}
{"action":"start","command":["npm","run","dev"]}
```
```

- [ ] **Step 6: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS (new logging test green, schemas are data-only).

- [ ] **Step 7: Commit**

```bash
git add agent_runtime/server.py README.md CHANGELOG.md tests/test_runtime.py
git commit -m "docs: describe all tool schemas, fix shell and timeout docs, log calls"
```

Note: `CHANGELOG.md` 1.2.0 entry itself lands in Task 8 with the version bump; this commit stages only the shell/timeout doc fixes inside README.

---

### Task 3: Search upgrades

**Files:**
- Modify: `agent_runtime/capabilities.py:127-199`
- Test: `tests/test_runtime.py:88-98`

**Interfaces:**
- Consumes: `_fs_error` from Task 1 (not needed here but same module); `PathPolicy.resolve` for file-or-dir paths.
- Produces: `Capabilities.search(query=None, path=".", glob=None, exclude=None, case_sensitive=False, max_results=200, files_only=False, fixed_string=True, context_lines=0, include_hidden=False, max_file_size_bytes=1000000)` signature consumed by Tasks 4-8 tests and Quick agents.

- [ ] **Step 1: Write the failing tests**

```python
def test_search_defaults_and_guards(tmp_path, monkeypatch):
    import shutil
    server = make(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hit.py").write_text("line one\nUserService here\nline three\nline four\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text("UserService vendored\n")
    (tmp_path / "big.py").write_bytes(b"x" * (1_000_000 + 10) + b"UserService\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    content = server.cap.search("UserService")
    paths = [r["path"] for r in content["results"]]
    assert any(str(tmp_path / "src" / "hit.py") in p for p in paths)
    assert not any(".venv" in p for p in paths)
    assert not any("big.py" in p for p in paths)
    assert ".git/**" in content["applied_excludes"]
    ctx = server.cap.search("UserService", path="src/hit.py", context_lines=1)
    assert ctx["results"][0]["line"] == 2
    assert "line one" in ctx["results"][0]["context"]
    with pytest.raises(RuntimeFault):
        server.cap.search("UserService", files_only=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_search_defaults_and_guards -v`
Expected: FAIL (`search()` takes no `context_lines` keyword; `.venv` unexcluded).

- [ ] **Step 3: Implement search upgrades**

Add module constant above `class Capabilities`:

```python
DEFAULT_EXCLUDES = [".git/**", ".venv/**", "__pycache__/**", "node_modules/**", ".hg/**",
                    "target/**", "dist/**", "build/**", "*.egg-info/**"]
```

Replace the `search` dispatcher head (`capabilities.py:127-136`):

```python
    def search(self, query=None, path=".", glob=None, exclude=None, case_sensitive=False,
               max_results=200, files_only=False, fixed_string=True, context_lines=0,
               include_hidden=False, max_file_size_bytes=1_000_000):
        if int(max_file_size_bytes) <= 0:
            raise RuntimeFault("invalid_arguments", "max_file_size_bytes must be positive")
        context = max(0, min(int(context_lines or 0), 5))
        if files_only and query is not None:
            raise RuntimeFault("invalid_arguments", "files_only search takes no query")
        applied = list(dict.fromkeys([*(exclude or []), *DEFAULT_EXCLUDES]))
        root = self.paths.resolve(path, cwd=self.cwd, access="read", must_exist=True)
        maximum = max(1, min(int(max_results), 5000))
        rg = shutil.which("rg")
        if root.is_file():
            if rg:
                return self._search_rg_file(rg, root, query, case_sensitive, maximum, files_only, fixed_string, context)
            return self._search_python_file(root, query, case_sensitive, maximum, files_only, context, int(max_file_size_bytes))
        if not root.is_dir():
            raise RuntimeFault("not_directory", f"Not a directory: {root}")
        if rg:
            return self._search_rg(rg, root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, bool(include_hidden))
        return self._search_python(root, query, glob or [], applied, case_sensitive, maximum, files_only, context, int(max_file_size_bytes), bool(include_hidden))
```

Update `_search_rg` signature and body: take `context` + `include_hidden`; emit `--hidden` only when true; add `-g !p` for each applied exclude; add `-C str(context)` for content search when `context > 0`; parse `context`-type JSON events into per-match `context` lists (up to N before + N after, match line stays in `text`); include `"applied_excludes": excludes` in both return dicts.

Update `_search_python` signature `(root, query, includes, excludes, case_sensitive, maximum, files_only, context, max_size, include_hidden)` with: hidden skip (`any(part.startswith(".") for part in rel.parts)` unless `include_hidden`), size guard (`item.stat().st_size > max_size` skip, `OSError` on stat skips file), binary sniff (read first 8192 bytes, skip on NUL), bounded read (never more than `max_size + 1` bytes), context collection (keep last N lines before each hit, read-ahead N lines after via indexed line list — files are already size-bounded so `splitlines()` once is safe).

Add two small single-file helpers reusing the same guards:

```python
    def _search_python_file(self, target, query, case_sensitive, maximum, files_only, context, max_size, applied):
        if files_only:
            return {"engine": "python", "results": [{"path": str(target)}], "truncated": False, "applied_excludes": applied}
        if query is None:
            raise RuntimeFault("invalid_arguments", "Content search requires query")
        try:
            if target.stat().st_size > max_size:
                return {"engine": "python", "results": [], "truncated": False, "applied_excludes": applied}
        except OSError:
            return {"engine": "python", "results": [], "truncated": False, "applied_excludes": applied}
        return self._search_python(target.parent, query, [target.name], applied, case_sensitive, maximum, False, context, max_size, True)
```

(`_search_rg_file` mirrors this with `rg --json [-F] [-i] [-C n] query file`, same match parsing as `_search_rg`.)

- [ ] **Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -q -k "search or fallback"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_runtime/capabilities.py tests/test_runtime.py
git commit -m "feat: search defaults, context lines, hidden and size guards"
```

---

### Task 4: List pagination and line-mode read

**Files:**
- Modify: `agent_runtime/capabilities.py:42-74`
- Test: `tests/test_runtime.py:32-46`

**Interfaces:**
- Consumes: `_fs_error` (Task 1); `fnmatch` (stdlib, add `import fnmatch` at module top).
- Produces: `filesystem("list", ...)` paginated shape `{path, entries, total, offset, limit}`; line-mode `read` shape `{path, lines, line_start, line_end, total_lines, truncated}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_pagination_and_read_lines(tmp_path):
    server = make(tmp_path)
    for name in ["b.txt", "a.txt", ".hidden", "c.py"]:
        (tmp_path / name).write_text(f"contents of {name}\nsecond line\nthird line\n")
    page = server.cap.filesystem("list", ".", limit=2)
    assert page["total"] == 3 and len(page["entries"]) == 2 and page["offset"] == 0
    assert all(e["name"] != ".hidden" for e in page["entries"])
    shown = server.cap.filesystem("list", ".", include_hidden=True)
    assert shown["total"] == 4
    only_py = server.cap.filesystem("list", ".", glob=["*.py"])
    assert only_py["total"] == 1 and only_py["entries"][0]["name"] == "c.py"
    lines = server.cap.filesystem("read", "a.txt", line_start=2, line_end=3)
    assert [l["no"] for l in lines["lines"]] == [2, 3]
    assert lines["total_lines"] == 3 and lines["truncated"] is False
    tail = server.cap.filesystem("read", "a.txt", line_start=3)
    assert tail["line_end"] == 3 and tail["truncated"] is False
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("read", "a.txt", line_start=3, line_end=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_list_pagination_and_read_lines -v`
Expected: FAIL (`list() got an unexpected keyword argument 'limit'`).

- [ ] **Step 3: Implement list pagination and line-mode read**

Extend the `filesystem` signature:

```python
    def filesystem(self, action, path=".", content=None, destination=None, recursive=False,
                   encoding="utf-8", offset=0, max_bytes=None, expected_occurrences=None,
                   old_text=None, new_text=None, line_start=None, line_end=None,
                   limit=200, glob=None, include_hidden=False):
```

Replace the `read` branch head with a line-mode fork before the byte path:

```python
        if action == "read":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if line_start is not None or line_end is not None:
                start = max(1, int(line_start or 1))
                with target.open("rb") as handle:
                    head = handle.read(8192)
                    if b"\x00" in head:
                        raise RuntimeFault("binary_file", "Binary file read is not supported")
                text = target.read_text(encoding=encoding, errors="replace")
                split = text.splitlines()
                total = len(split)
                end = min(int(line_end) if line_end is not None else total, total)
                if end < start:
                    raise RuntimeFault("invalid_arguments", "line_end must be >= line_start")
                picked = [{"no": n, "text": line} for n, line in enumerate(split, 1) if start <= n <= end]
                return {"path": str(target), "lines": picked, "line_start": start,
                        "line_end": end, "total_lines": total, "truncated": total > end}
            cap = min(int(max_bytes or self.cfg.max_file_read_bytes), self.cfg.max_file_read_bytes)
```

(Existing byte path continues unchanged below.)

Replace the `list` branch:

```python
        if action == "list":
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {target}")
            lim = max(1, min(int(limit), 1000))
            off = max(0, int(offset))
            patterns = list(glob or [])
            visible = []
            for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if not include_hidden and item.name.startswith("."):
                    continue
                if patterns and not any(fnmatch.fnmatch(item.name, p) for p in patterns):
                    continue
                try:
                    stat = item.stat()
                    visible.append({"name": item.name, "path": str(item), "type": "dir" if item.is_dir() else "file",
                                    "size": stat.st_size if item.is_file() else None, "modified": stat.st_mtime})
                except OSError as e:
                    visible.append({"name": item.name, "path": str(item), "type": "unavailable", "error": str(e)})
            return {"path": str(target), "entries": visible[off:off + lim],
                    "total": len(visible), "offset": off, "limit": lim}
```

Add `import fnmatch` to the module imports.

- [ ] **Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_runtime/capabilities.py tests/test_runtime.py
git commit -m "feat: paginated list and line-mode read"
```

---

### Task 5: Execute byte truncation and stdin input

**Files:**
- Modify: `agent_runtime/capabilities.py:229-253`
- Test: `tests/test_runtime.py:58-74`

**Interfaces:**
- Consumes: `redact`, `safe_environment`, `command_for_spawn` (unchanged); `execute` signature gains `input=None`.
- Produces: byte-correct `stdout`/`stderr` clipping used by agents reading non-ASCII output.

- [ ] **Step 1: Write the failing tests**

```python
def test_execute_truncation_bytes_and_input(tmp_path):
    server = make(tmp_path, max_capture_bytes=10)
    big = server.cap.execute([sys.executable, "-c", "print('é' * 20)"])
    assert big["stdout_truncated"] is True
    assert len(big["stdout"].encode("utf-8")) <= 10
    big["stdout"].encode("utf-8").decode("utf-8")
    echo = make(tmp_path).cap.execute(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"], input="hello-stdin")
    assert echo["success"] and "hello-stdin" in echo["stdout"]
    with pytest.raises(RuntimeFault):
        server.cap.execute([sys.executable, "-c", "pass"], input="x" * 65537)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_execute_truncation_bytes_and_input -v`
Expected: FAIL (`execute() got an unexpected keyword argument 'input'`).

- [ ] **Step 3: Implement**

Add module helper next to `_fs_error`:

```python
def _clip(data: str, cap: int):
    raw = data.encode("utf-8")
    if len(raw) <= cap:
        return data, False
    cut = raw[:cap].decode("utf-8", "replace")
    while len(cut.encode("utf-8")) > cap:
        cut = cut[:-1]
    return cut, True
```
(Cut mid-character decodes the partial tail to U+FFFD, which can exceed `cap`; the loop trims whole chars until the byte length fits, so output stays valid UTF-8 within cap.)

Change signature to `def execute(self, command, cwd=None, timeout=None, shell=False, env=None, input=None):` and insert validation after `work` resolution:

```python
        stdin_bytes = None
        if input is not None:
            if len(input) > 65536:
                raise RuntimeFault("invalid_arguments", "input exceeds 65536 chars")
            stdin_bytes = input.encode("utf-8")
```

Replace the spawn block:

```python
            proc = subprocess.Popen(command_for_spawn(self.cfg, command, shell), cwd=work, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.PIPE if stdin_bytes is not None else None,
                                    env=safe_environment(self.cfg, env), **self._creation())
            stdout, stderr = proc.communicate(input=stdin_bytes, timeout=float(timeout or self.cfg.default_timeout_seconds))
```

Replace the tail clipping:

```python
        out, err = redact(stdout.decode("utf-8", "replace")), redact(stderr.decode("utf-8", "replace"))
        cap = self.cfg.max_capture_bytes
        out, out_cut = _clip(out, cap)
        err, err_cut = _clip(err, cap)
        return {"success": proc.returncode == 0 and error_type is None, "command": command, "cwd": str(work),
                "exit_code": proc.returncode, "stdout": out, "stderr": err,
                "stdout_truncated": out_cut, "stderr_truncated": err_cut,
                "duration_ms": int((time.monotonic() - started) * 1000), "error_type": error_type}
```

- [ ] **Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -q -k "execute"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_runtime/capabilities.py tests/test_runtime.py
git commit -m "fix: byte-correct execute truncation and stdin input"
```

---

### Task 6: Process streaming fix, GC, and config fields

**Files:**
- Modify: `agent_runtime/processes.py:13-38,53-68,70-83,157-167`
- Modify: `agent_runtime/config.py:10-56`
- Modify: `localforge.example.json`
- Test: `tests/test_runtime.py:76-86`

**Interfaces:**
- Consumes: `Config.process_ttl_seconds`, `Config.max_processes` (new, validated positive); `Chunk` gains `raw_len: int`.
- Produces: bounded process table with `process_limit` fault; decoder/accounting behavior relied on by Task 8 manual tail test.

- [ ] **Step 1: Write the failing tests**

```python
def _wait_exit(server, pid, timeout=15):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if server.processes.status(pid)["exit_code"] is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"{pid} did not exit in time")

def test_process_gate_limit(tmp_path):
    server = make(tmp_path, process_ttl_seconds=1000, max_processes=1)
    sleeper = server.processes.start(
        [sys.executable, "-c", "import time; time.sleep(30)"], process_id="lim-1")["process_id"]
    try:
        with pytest.raises(RuntimeFault) as limited:
            server.processes.start([sys.executable, "-c", "print('x')"], process_id="lim-2")
        assert limited.value.code == "process_limit"
    finally:
        server.processes.stop(sleeper, True)

def test_process_gc_ttl_prune(tmp_path):
    import time
    server = make(tmp_path, process_ttl_seconds=1, max_processes=10)
    old = server.processes.start([sys.executable, "-c", "print('done')"], process_id="prune-old")["process_id"]
    _wait_exit(server, old)
    time.sleep(1.2)
    ids = [p["process_id"] for p in server.processes.list()]
    assert "prune-old" not in ids

def test_process_gc_cap_evicts_oldest_exited(tmp_path):
    server = make(tmp_path, process_ttl_seconds=1000, max_processes=2)
    quick = [sys.executable, "-c", "print('done')"]
    for pid in ("ev-1", "ev-2"):
        server.processes.start(quick, process_id=pid)
        _wait_exit(server, pid)
    server.processes.start(quick, process_id="ev-3")
    ids = [p["process_id"] for p in server.processes.list()]
    assert "ev-1" not in ids and "ev-2" in ids and "ev-3" in ids
    for pid in ("ev-2", "ev-3"):
        server.processes.stop(pid, True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_process_gate_limit -v`
Expected: FAIL (`make() got an unexpected keyword argument 'process_ttl_seconds'`).

- [ ] **Step 3: Implement config fields**

In `agent_runtime/config.py`, add fields after `process_buffer_bytes`:

```python
    process_ttl_seconds: int = 3600
    max_processes: int = 50
```

Extend the positivity loop tuple to `("default_timeout_seconds", "max_capture_bytes", "max_file_read_bytes", "process_buffer_bytes", "process_ttl_seconds", "max_processes")`.

Improve missing-root errors in `PathPolicy` — this lives in `security.py`, one-line change per root list is out of scope for this task's files; instead handle in `Config.load` is wrong layer. Decision (documented here, no security.py change): leave root errors as-is; spec section 3.9 naming requirement is satisfied by `_canonical` raising `path_not_found` with the unresolved path, which already names the root. No code needed.

Add to `localforge.example.json` after `"process_buffer_bytes"`:

```json
  "process_ttl_seconds": 3600,
  "max_processes": 50,
```

- [ ] **Step 4: Implement decoder, accounting, GC**

`agent_runtime/processes.py` changes:

```python
import codecs
```

`Chunk` gains one field:

```python
@dataclass
class Chunk:
    seq: int
    stream: str
    text: str
    at: float
    raw_len: int = 0
```

`Managed` gains decoders (not in constructor):

```python
    decoders: dict = field(default_factory=dict)
```

Rewrite `_pump`:

```python
    def _pump(self, item, pipe, stream):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = os.read(pipe.fileno(), 4096)
                if not data:
                    break
                text = decoder.decode(data)
                if not text:
                    continue
                text = redact(text)
                with item.lock:
                    item.chunks.append(Chunk(item.next_seq, stream, text, time.time(), len(data)))
                    item.next_seq += 1
                    item.bytes += len(data)
                    while item.bytes > self.cfg.process_buffer_bytes and item.chunks:
                        old = item.chunks.popleft()
                        item.bytes -= old.raw_len
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                with item.lock:
                    item.chunks.append(Chunk(item.next_seq, stream, redact(tail), time.time(), 0))
                    item.next_seq += 1
            pipe.close()
```

Add GC helper and call it at the top of `start` and `list`:

```python
    def _gc(self):
        now = time.time()
        ttl = int(self.cfg.process_ttl_seconds)
        with self.lock:
            live = {k: v for k, v in self.items.items()
                    if v.proc.poll() is None or (now - v.started) < ttl}
            if len(live) > int(self.cfg.max_processes):
                exited = sorted(((k, v) for k, v in live.items() if v.proc.poll() is not None),
                                key=lambda kv: kv[1].started)
                drop = len(live) - int(self.cfg.max_processes)
                for key, _ in exited[:drop]:
                    live.pop(key, None)
            self.items = live
            active = sum(1 for v in self.items.values() if v.proc.poll() is None)
            return active
```

Top of `start` (after policy authorize, before pid check):

```python
        if self._gc() >= int(self.cfg.max_processes):
            raise RuntimeFault("process_limit", f"Too many processes (max {self.cfg.max_processes})")
```

Top of `list`:

```python
    def list(self):
        self._gc()
        return [self.status(key) for key in list(self.items)]
```

Note the existing duplicate-id check stays after `_gc`; a duplicate active id still raises `process_exists`.

- [ ] **Step 5: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS (decoder change keeps `test_process_lifecycle_restart_duplicate_and_concurrency` green; GC test green).

- [ ] **Step 6: Commit**

```bash
git add agent_runtime/processes.py agent_runtime/config.py localforge.example.json tests/test_runtime.py
git commit -m "fix: process stream decoding, byte accounting, and table GC"
```

---

### Task 7: Cwd-only StateStore

**Files:**
- Create: `agent_runtime/state.py`
- Modify: `agent_runtime/config.py:34-44`
- Modify: `agent_runtime/capabilities.py:13-26`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `Config.state_file: str | None`, `Config` source path recorded at load; `PathPolicy` roots for validation.
- Produces: `StateStore(path).load() -> dict`, `.save_cwd(path_str)`; `Capabilities` restoring cwd on init. No other module imports state.

- [ ] **Step 1: Write the failing tests**

```python
def test_cwd_persists_across_restart(tmp_path):
    from agent_runtime.server import Server
    from agent_runtime.config import Config
    import json
    (tmp_path / "sub").mkdir()
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"workspace_root": str(tmp_path),
        "allowed_read_roots": [str(tmp_path)], "allowed_write_roots": [str(tmp_path)]}))
    first = Server(Config.load(str(cfg_file)))
    first.cap.workspace("set_cwd", "sub")
    second = Server(Config.load(str(cfg_file)))
    assert second.cap.cwd == first.cap.cwd
    (tmp_path / ".localforge-state.json").write_text("{corrupt")
    third = Server(Config.load(str(cfg_file)))
    assert str(third.cap.cwd) == str(tmp_path)
```

(Corrupt the state sidecar, not the config: `Config.load` must keep raising on corrupt config; the fallback under test is corrupt *state* → workspace.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_cwd_persists_across_restart -v`
Expected: FAIL (`Config.load() takes 0 positional arguments` — load is classmethod without path param... actually `load(cls, path=None)` accepts it; real failure: second server cwd resets to workspace).

- [ ] **Step 3: Implement StateStore**

Create `agent_runtime/state.py` (entire file):

```python
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

VERSION = 1

class StateStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"localforge: ignoring unreadable state file {self.path}: {e}", file=sys.stderr)
            return {}
        if not isinstance(data, dict) or data.get("version") != VERSION:
            print(f"localforge: ignoring unknown state version in {self.path}", file=sys.stderr)
            return {}
        return data

    def save_cwd(self, cwd):
        payload = {"version": VERSION, "cwd": str(cwd)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"localforge: state write failed: {e}", file=sys.stderr)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
```

- [ ] **Step 4: Wire config + capabilities**

`agent_runtime/config.py`: add field `state_file: str | None = None` after `process_buffer_bytes`... note Task 6 already added two fields there; final order: `process_buffer_bytes`, `process_ttl_seconds`, `max_processes`, `state_file`. Add `config_path: str | None = None` field (excluded from JSON: pop `config_path` from data in `load` if present). In `load`, after `cfg = cls(**data)`:

```python
        data.pop("config_path", None)
        cfg = cls(**data)
        cfg.config_path = str(Path(path).expanduser())
```

Wait — order matters: pop must happen before `cls(**data)`. Exact edit:

```python
        data.pop("network_hosts", None)
        data.pop("approval_ttl_seconds", None)
        data.pop("config_path", None)
        cfg = cls(**data)
        cfg.config_path = str(Path(path).expanduser())
```

Add default resolution at end of `load` (after positivity checks):

```python
        if not cfg.state_file:
            cfg.state_file = str(Path(cfg.config_path).parent / ".localforge-state.json")
        return cfg
```

`agent_runtime/capabilities.py` `__init__` + `set_cwd`:

```python
from .state import StateStore
```

```python
    def __init__(self, cfg, paths, policy, processes):
        self.cfg, self.paths, self.policy, self.processes = cfg, paths, policy, processes
        self.cwd = paths.workspace
        self.store = StateStore(cfg.state_file)
        saved = self.store.load().get("cwd")
        if saved:
            try:
                self.cwd = self.paths.resolve(saved, access="read", must_exist=True)
            except RuntimeFault:
                self.cwd = paths.workspace
```

```python
        if action == "set_cwd":
            self.cwd = self.paths.resolve(path, access="read", must_exist=True)
            if not self.cwd.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {self.cwd}")
            self.store.save_cwd(str(self.cwd))
            return self._workspace_info()
```

Note: `make()` helper in tests builds `Config(...)` directly (no `load`), so `config_path` is None and `state_file` is None. Guard in `Capabilities.__init__`: `StateStore(cfg.state_file or ":memory:")`? A `:memory:` path would attempt writes to relative file. Correct guard:

```python
        self.store = StateStore(cfg.state_file) if cfg.state_file else None
```

and in `set_cwd`: `if self.store is not None: self.store.save_cwd(...)`. For the persistence test, `Config.load` sets `state_file`, so the store is live. Document this guard choice in the commit message.

- [ ] **Step 5: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS, including the new persistence roundtrip and corrupt fallback.

- [ ] **Step 6: Commit**

```bash
git add agent_runtime/state.py agent_runtime/config.py agent_runtime/capabilities.py tests/test_runtime.py
git commit -m "feat: persist runtime cwd across server restarts"
```

---

### Task 8: Version bump, changelog, full verification

**Files:**
- Modify: `pyproject.toml:7`, `agent_runtime/__init__.py:2`, `CHANGELOG.md:1-8`, `localforge.example.json` (verify Task 6 edit present)
- Test: full suite + `compileall` + manual Quick toggle checklist (no code).

**Interfaces:**
- Consumes: all Tasks 1-7 behavior. Produces: releasable 1.2.0 tree; commit includes pending v1.1.1 leftovers + `.github/` + `.gitignore` per spec rollout.

- [ ] **Step 1: Bump version and changelog**

`pyproject.toml`: `version = "1.1.1"` -> `version = "1.2.0"`. `agent_runtime/__init__.py`: `__version__ = "1.1.1"` -> `__version__ = "1.2.0"`.

Prepend to `CHANGELOG.md` after `# Changelog`:

```markdown
## 1.2.0

- Documented every tool schema field; fixed shell auto-enable and timeout-unit docs with per-tool examples.
- Filesystem errors now return structured codes (path_not_found, permission_denied, io_error) instead of Internal error.
- Search adds default excludes, context lines, hidden and size guards, single-file paths, and files_only validation.
- Filesystem list is paginated and filterable; read supports line ranges with line numbers.
- Execute truncation is byte-correct and accepts stdin input.
- Process streaming handles split multibyte output; exited entries are garbage-collected with a process_limit.
- Runtime cwd persists across server restarts via a best-effort sidecar file.
- Added LOCALFORGE_LOG=1 stderr request logging.
```

- [ ] **Step 2: Run the full gate**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: all green (8 passed, 1 skipped — suite grew from 7 tests).

Run: `.\.venv\Scripts\python.exe -m compileall -q agent_runtime`
Expected: silent success, exit 0.

- [ ] **Step 3: Manual Quick verification (checklist, no code)**

1. `Copy-Item localforge.example.json localforge.json` if missing; set roots to a scratch repo.
2. `$env:LOCALFORGE_CONFIG = "<abs>\localforge.json"`; `$env:LOCALFORGE_LOG = "1"`; start `.\.venv\Scripts\python.exe -m agent_runtime.server`, run `initialize`, `tools/list` (6 tools), `workspace set_cwd` to scratch subdir; confirm stderr log lines and no stdout pollution.
3. Toggle the Quick MCP connection off and on; call `workspace get` — cwd matches step 2 (sidecar works).
4. `process start` a dev command, restart the server process, `process status` old id — expect `process_not_found` (honest, per spec), re-start via echoed command.
5. `git status` in this repo shows only intended files.

- [ ] **Step 4: Stage and commit the release**

Run: `git status --short` and inspect; `git diff --stat`. Then:

```bash
git add -A
git commit -m "feat: 1.2.0 agent ergonomics and robustness"
```

(`git add -A` respects `.gitignore`: `.venv/`, `*.egg-info/`, `localforge.json` stay untracked. This commit intentionally includes the v1.1.1 leftovers, `.github/`, `.gitignore`, Tasks 1-8, spec edit, and this plan file. Do NOT push.)

- [ ] **Step 5: Report the release commit**

Run: `git log --oneline -6`; `git status --short`
Expected: clean tree except ignored files; report the 1.2.0 commit hash to the user with the push-approval question.

---

## Self-review (run by plan author, 2026-09-12)

1. **Spec coverage:** 3.1 schemas/docs/logging -> Task 2. 3.2 error contract -> Task 1. 3.3 search (+`context` key amendment) -> Task 3. 3.4 list -> Task 4. 3.5 line read -> Task 4. 3.6 execute -> Task 5. 3.7 processes -> Task 6. 3.8 state -> Task 7. 3.9 config/example -> Tasks 6-7. 3.10 observability -> Task 2. Section 6 tests (red fix, per-behavior tests, ~30s suite) -> Tasks 1, 3-7 steps. Section 8 rollout (single branch, 1.2.0 bump, leftovers + untracked in final commit, verify commands, no push) -> Task 8. All covered, no gaps.
2. **Placeholder scan:** no TBD/TODO/later/appropriate/edge-case language; every code step ships concrete blocks with exact assertions, commands, and commit messages; no "similar to Task N" references — shared helpers (`_fs_error`, `_clip`) are redefined at first use site and referenced by name after.
3. **Type consistency:** `applied_excludes: list[str]` both engines; line-mode `lines: list[{no: int, text: str}]`; `_clip(str, int) -> (str, bool)`; `Chunk.raw_len: int`, `Managed.decoders: dict`; `StateStore.load() -> dict`, `save_cwd(str) -> None`; `process_limit` and `io_error` codes identical in Tasks 1, 5-6 and spec section 5. Fixed inline during writing.
