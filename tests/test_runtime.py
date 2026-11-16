import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import pytest
from agent_runtime.config import Config
from agent_runtime.server import Server
from agent_runtime.errors import RuntimeFault

def make(tmp, mode="DEVELOPMENT", network="unrestricted", **kwargs):
    # Keep saved output out of the real %TEMP% folder and out of tmp.
    kwargs.setdefault("spill_dir", str(Path(tmp).parent / (Path(tmp).name + "-spill")))
    cfg = Config(str(tmp), mode=mode, allowed_read_roots=[str(tmp)], allowed_write_roots=[str(tmp)],
                 network=network, inherit_environment=["PATH"], default_shell="cmd" if os.name == "nt" else "sh", **kwargs)
    return Server(cfg)

def test_mcp_protocol(tmp_path):
    server = make(tmp_path)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "localforge-mcp"
    assert "logging" in init["result"]["capabilities"]
    assert init["result"]["instructions"].startswith("LocalForge rules:")
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {item["name"] for item in tools} == {"workspace", "filesystem", "search", "multi_search", "batch", "git", "execute", "process", "todo"}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "unknown-notification"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 3, "method": "bogus"})["error"]["code"] == -32601
    with pytest.raises(ValueError): server.handle([])
    with pytest.raises(RuntimeFault): server.call("missing", {})
    with pytest.raises(RuntimeFault): server.call("workspace", {})

def test_filesystem_and_modes(tmp_path):
    server = make(tmp_path, max_file_read_bytes=5)
    server.cap.filesystem("write", "a.txt", "hello world")
    result = server.cap.filesystem("read", "a.txt")
    assert result["content"] == "hello" and result["truncated"]
    # replace_text reads the whole file, so it follows the
    # same cap; edit via a server with the default limit.
    full = make(tmp_path)
    full.cap.filesystem("replace_text", "a.txt", old_text="world", new_text="agent")
    assert (tmp_path / "a.txt").read_text() == "hello agent"
    with pytest.raises(RuntimeFault):
        full.cap.filesystem("replace_text", "a.txt", old_text="missing", new_text="x")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("read", "../escape")
    readonly = make(tmp_path, "READ_ONLY")
    assert readonly.cap.filesystem("list", ".")["entries"]
    with pytest.raises(RuntimeFault): readonly.cap.filesystem("write", "b.txt", "x")

def test_symlink_escape_when_supported(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    server = make(tmp_path)
    with pytest.raises(RuntimeFault): server.cap.filesystem("list", "link")

def test_execute_results_shell_timeout_large_env_and_network(tmp_path):
    server = make(tmp_path)
    ok = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert ok["success"] and "ok" in ok["stdout"]
    shell = server.cap.execute("echo shell-ok", shell=True)
    assert shell["success"] and "shell-ok" in shell["stdout"]
    auto = server.cap.execute("echo invalid")
    assert auto["success"] and "invalid" in auto["stdout"]
    bad = server.cap.execute([sys.executable, "-c", "raise SystemExit(7)"])
    assert bad["exit_code"] == 7 and bad["error_type"] == "process_exit"
    timeout = server.cap.execute([sys.executable, "-c", "import time;time.sleep(2)"], timeout=.05, kill_on_timeout=True)
    assert timeout["error_type"] == "timeout"
    capped = make(tmp_path, max_capture_bytes=1000)
    big = capped.cap.execute([sys.executable, "-c", "print('x'*5000)"])
    assert big["stdout_truncated"] and len(big["stdout"]) <= 1000
    with pytest.raises(RuntimeFault): server.cap.execute([sys.executable, "-c", "pass"], env={"API_KEY": "bad"})
    offline = make(tmp_path, network="disabled")
    with pytest.raises(RuntimeFault): offline.cap.execute(["git", "clone", "https://example.test/x"])

def test_process_lifecycle_restart_duplicate_and_concurrency(tmp_path):
    server = make(tmp_path)
    command = ["cmd.exe", "/c", "echo ready & ping -n 2 127.0.0.1 >nul"] if os.name == "nt" else ["/bin/sh", "-c", "echo ready; sleep .3"]
    ids = [server.processes.start(command, process_id=f"worker-{n}")["process_id"] for n in range(2)]
    with pytest.raises(RuntimeFault): server.processes.start(command, process_id="worker-0")
    for process_id in ids:
        output = server.processes.read(process_id, wait_ms=1000)
        assert "ready" in output["stdout"] and "chunks" not in output
    chunked = server.call("process", {"action": "read", "process_id": ids[0], "format": "chunks"})
    assert "ready" in "".join(c["text"] for c in chunked["chunks"]) and "stdout" not in chunked
    with pytest.raises(RuntimeFault):
        server.call("process", {"action": "read", "process_id": ids[0], "format": "bogus"})
    restarted = server.processes.restart("worker-0")
    assert restarted["process_id"] == "worker-0"
    for process_id in ids: server.processes.stop(process_id, True)

def test_process_wait_for_exit_pattern_or_timeout(tmp_path):
    server = make(tmp_path)
    lines = server.call("process", {"action": "start", "command": [
        sys.executable, "-c", "for n in range(100): print('row', n)"]})["process_id"]
    done = server.call("process", {"action": "wait", "process_id": lines, "wait_ms": 20000, "tail_lines": 5})
    assert done["state"] == "exited" and done["exit_code"] == 0 and done["reason"] == "exited"
    assert done["tail"].splitlines() == [f"row {n}" for n in range(95, 100)]
    assert done["next_after"] >= 1 and done["matched"] is None
    serve = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import time; print('starting', flush=True); time.sleep(0.3); "
        "print('READY on 8080', flush=True); time.sleep(30)")]})["process_id"]
    started = time.monotonic()
    ready = server.call("process", {"action": "wait", "process_id": serve, "pattern": r"READY on \d+", "wait_ms": 20000})
    assert ready["reason"] == "matched" and ready["matched"] == "READY on 8080" and ready["state"] == "running"
    assert time.monotonic() - started < 15
    started = time.monotonic()
    idle = server.call("process", {"action": "wait", "process_id": serve, "pattern": "never",
                                   "after": ready["next_after"], "wait_ms": 500})
    assert idle["reason"] == "timeout" and idle["state"] == "running" and time.monotonic() - started < 10
    with pytest.raises(RuntimeFault) as exc:
        server.call("process", {"action": "wait", "process_id": serve, "pattern": "(unclosed"})
    assert exc.value.code == "invalid_arguments"
    server.processes.stop(serve, True)

def test_process_wait_resumes_after_the_matched_line(tmp_path):
    server = make(tmp_path)
    # Both lines in one write, so most likely in one chunk.
    pid = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import sys, time; sys.stdout.write('Compiled\\nTests passed\\n'); sys.stdout.flush(); time.sleep(30)")]})["process_id"]
    try:
        first = server.call("process", {"action": "wait", "process_id": pid, "pattern": "Compiled", "wait_ms": 20000})
        assert first["reason"] == "matched" and first["matched"] == "Compiled", first
        second = server.call("process", {"action": "wait", "process_id": pid, "pattern": "Tests passed",
                                         "after": first["next_after"], "wait_ms": 3000})
        assert second["reason"] == "matched" and second["matched"] == "Tests passed", second
        # A matched line is not matched again from its cursor.
        again = server.call("process", {"action": "wait", "process_id": pid, "pattern": "Compiled|Tests",
                                        "after": second["next_after"], "wait_ms": 500})
        assert again["reason"] == "timeout", again
    finally:
        server.processes.stop(pid, True)

def test_process_wait_matches_a_line_the_cursor_cuts_as_a_whole(tmp_path):
    server = make(tmp_path)
    pid = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import sys, time; sys.stdout.write('abc'); sys.stdout.flush(); time.sleep(1); "
        "sys.stdout.write('ERROR-def\\n'); sys.stdout.flush(); time.sleep(30)")]})["process_id"]
    try:
        read = server.processes.read(pid, wait_ms=10000)
        assert read["stdout"] == "abc", read
        # The cursor sits inside the line "abcERROR-def": it is matched
        # whole, so the fragment after the cursor is not a line start.
        cut = server.call("process", {"action": "wait", "process_id": pid, "pattern": "^ERROR",
                                      "after": read["next_after"], "wait_ms": 3000})
        assert cut["reason"] == "timeout", cut
        whole = server.call("process", {"action": "wait", "process_id": pid, "pattern": "^abcERROR-def$",
                                        "after": read["next_after"], "wait_ms": 3000})
        assert whole["reason"] == "matched" and whole["matched"] == "abcERROR-def", whole
    finally:
        server.processes.stop(pid, True)

def _waits(server, pid, pattern, rounds=4):
    # Waits from each next_after until one does not match.
    seen, after = [], 0
    for _ in range(rounds):
        out = server.call("process", {"action": "wait", "process_id": pid, "pattern": pattern,
                                      "after": after, "wait_ms": 20000})
        if out["reason"] != "matched":
            break
        seen.append(out["matched"])
        after = out["next_after"]
    return seen, out

def test_process_wait_at_exit_matches_each_unfinished_last_line_once(tmp_path):
    server = make(tmp_path)
    # Both streams end without a newline, then the process exits.
    pid = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import sys, time; sys.stdout.write('AAA match'); sys.stdout.flush(); time.sleep(0.3); "
        "sys.stderr.write('BBB match'); sys.stderr.flush()")]})["process_id"]
    try:
        seen, out = _waits(server, pid, "match")
        assert seen == ["AAA match", "BBB match"] and out["reason"] == "exited", (seen, out)
    finally:
        server.processes.stop(pid, True)
    # An unfinished last line after a match in the same chunk is still new.
    pid = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import sys; sys.stdout.write('x match\\ny match'); sys.stdout.flush()")]})["process_id"]
    try:
        seen, out = _waits(server, pid, "match")
        assert seen == ["x match", "y match"] and out["reason"] == "exited", (seen, out)
    finally:
        server.processes.stop(pid, True)

def test_process_wait_from_a_read_cursor_skips_the_chunk_read_returned(tmp_path):
    server = make(tmp_path)
    pid = server.call("process", {"action": "start", "command": [sys.executable, "-c", (
        "import sys, time; sys.stdout.write('err one\\nerr two\\n'); sys.stdout.flush(); time.sleep(30)")]})["process_id"]
    try:
        first = server.call("process", {"action": "wait", "process_id": pid, "pattern": "err", "wait_ms": 20000})
        assert first["matched"] == "err one", first
        read = server.processes.read(pid)
        assert "err two" in read["stdout"], read
        # read showed the whole chunk, so its cursor does not resume inside it.
        again = server.call("process", {"action": "wait", "process_id": pid, "pattern": "err",
                                        "after": read["next_after"], "wait_ms": 500})
        assert again["reason"] == "timeout", again
    finally:
        server.processes.stop(pid, True)

@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_long_lines_are_cut_in_reads_and_search(tmp_path, monkeypatch, engine):
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    elif shutil.which("rg") is None:
        pytest.skip("rg unavailable")
    server = make(tmp_path)
    cut = "x" * 2000 + "...[line cut: 10006 chars]"
    (tmp_path / "min.js").write_text("short needle\n" + "x" * 5000 + "needle" + "y" * 5000 + "\nend\n")
    lines = server.cap.filesystem("read", "min.js", line_start=1)["content"].split("\n")
    assert lines == ["1\tshort needle", "2\t" + cut, "3\tend"]
    for path in ("min.js", "."):
        matches = server.cap.search("needle", path=path, context_lines=1)["results"][0]["matches"]
        assert matches[1]["text"] == cut and matches[0]["after"] == ["2\t" + cut]
    # A tail read keeps only the cut head of each long line.
    assert server.cap.filesystem("read", "min.js", line_start=-2)["content"].split("\n") == ["2\t" + cut, "3\tend"]

def test_search_timeout_says_what_to_do(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("rg unavailable")
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("x\n")

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", slow)
    for kwargs in ({"query": "x"}, {"files_only": True}, {"query": "x", "path": "a.txt"}):
        with pytest.raises(RuntimeFault) as exc:
            server.cap.search(**kwargs)
        assert exc.value.code == "timeout" and "timed out" in exc.value.message and "glob" in exc.value.message

def test_read_lines_negative_start_reads_from_the_end(tmp_path):
    server = make(tmp_path)
    (tmp_path / "f.txt").write_text("".join(f"row {n}\n" for n in range(1, 201)))
    out = server.cap.filesystem("read", "f.txt", line_start=-50)
    lines = out["content"].split("\n")
    assert len(lines) == 50 and lines[0] == "151\trow 151" and lines[-1] == "200\trow 200"
    assert out["line_start"] == 151 and out["line_end"] == 200 and out["total_lines"] == 200 and not out["truncated"]
    (tmp_path / "s.txt").write_text("a\nb\n")
    short = server.cap.filesystem("read", "s.txt", line_start=-10)
    assert short["content"] == "1\ta\n2\tb" and short["line_start"] == 1 and short["total_lines"] == 2
    assert server.call("filesystem", {"action": "read", "path": "s.txt", "line_start": "-1"})["content"] == "2\tb"
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("read", "f.txt", line_start=-5, line_end=10)
    assert exc.value.code == "invalid_arguments"
    small = make(tmp_path, max_read_chars=64).cap.filesystem("read", "f.txt", line_start=-50)
    assert small["truncated"] and small["content"].endswith("200\trow 200") and len(small["content"]) <= 64
    assert small["line_start"] > 151 and "note" in small
    # An empty file has no lines to number, and a file that is only a BOM is empty.
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbf")
    for name in ("empty.txt", "bom.txt"):
        tail = server.cap.filesystem("read", name, line_start=-5)
        assert (tail["content"], tail["line_start"], tail["line_end"], tail["total_lines"]) == ("", 0, 0, 0), tail
        head = server.cap.filesystem("read", name, line_start=1)
        assert head["content"] == "" and head["total_lines"] == 0, head

def test_utf8_bom_is_hidden_and_kept(tmp_path):
    server = make(tmp_path)
    bom = b"\xef\xbb\xbf"
    path = tmp_path / "b.txt"
    path.write_bytes(bom + b"first\nsecond\n")
    assert server.cap.filesystem("read", "b.txt")["content"] == "first\nsecond\n"
    assert server.cap.filesystem("read", "b.txt", line_start=1)["content"] == "1\tfirst\n2\tsecond"
    server.cap.filesystem("apply_patch", ".", patch="--- a/b.txt\n+++ b/b.txt\n@@ -1,2 +1,2 @@\n-first\n+FIRST\n second\n")
    assert path.read_bytes() == bom + b"FIRST\nsecond\n"
    replaced = server.cap.filesystem("replace_text", "b.txt", old_text="FIRST", new_text="one")
    assert "\ufeff" not in replaced["diff"] and path.read_bytes() == bom + b"one\nsecond\n"
    edited = server.cap.filesystem("multi_edit", "b.txt", edits=[{"old_text": "one", "new_text": "uno"}])
    assert "\ufeff" not in edited["diff"] and path.read_bytes() == bom + b"uno\nsecond\n"
    written = server.cap.filesystem("write", "b.txt", "new\n")
    assert path.read_bytes() == bom + b"new\n" and "\ufeff" not in written["diff"]
    server.cap.filesystem("write", "b.txt", "\ufeffown\n")
    assert path.read_bytes() == bom + b"own\n"
    # A file without one never gets one.
    (tmp_path / "plain.txt").write_bytes(b"x\n")
    server.cap.filesystem("write", "plain.txt", "y\n")
    assert (tmp_path / "plain.txt").read_bytes() == b"y\n"

def test_patch_from_git_diff_of_a_bom_file_applies(tmp_path):
    server = make(tmp_path)
    bom = b"\xef\xbb\xbf"
    path = tmp_path / "c.txt"
    path.write_bytes(bom + b"first\nsecond\n")
    # git diff shows the BOM as "\ufeff" on line 1, old and new.
    server.cap.filesystem("apply_patch", ".", patch="--- a/c.txt\n+++ b/c.txt\n@@ -1,2 +1,2 @@\n-\ufefffirst\n+\ufeffFIRST\n second\n")
    assert path.read_bytes() == bom + b"FIRST\nsecond\n"
    server.cap.filesystem("apply_patch", ".", patch="--- a/c.txt\n+++ b/c.txt\n@@ -1,2 +1,2 @@\n \ufeffFIRST\n-second\n+SECOND\n")
    assert path.read_bytes() == bom + b"FIRST\nSECOND\n"

def test_replace_all_replaces_every_exact_match(tmp_path):
    server = make(tmp_path)
    path = tmp_path / "r.txt"
    path.write_text("a = old\nb = old\nc = old()\n")
    out = server.call("filesystem", {"action": "replace_text", "path": "r.txt", "old_text": "old", "new_text": "new",
                                     "replace_all": "true"})
    assert out["replacements"] == 3 and path.read_text() == "a = new\nb = new\nc = new()\n"
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "r.txt", old_text="missing", new_text="x", replace_all=True)
    assert exc.value.code == "content_mismatch" and "replace_all" in exc.value.message
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "r.txt", old_text="new", new_text="x", replace_all=True, expected_occurrences=3)
    assert exc.value.code == "invalid_arguments"
    # Exact matches only: no typography fallback, which every match would repeat.
    path.write_text("say \u201chi\u201d\nsay \u201chi\u201d\n", encoding="utf-8")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("replace_text", "r.txt", old_text='"hi"', new_text="'hi'", replace_all=True)
    out = server.cap.filesystem("multi_edit", "r.txt", edits=[
        {"old_text": "say", "new_text": "said", "replace_all": True},
        {"old_text": "said", "new_text": "told", "expected_occurrences": 2}])
    assert [entry["replacements"] for entry in out["edits"]] == [2, 2]
    assert path.read_text(encoding="utf-8") == "told \u201chi\u201d\ntold \u201chi\u201d\n"
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("multi_edit", "r.txt", edits=[
            {"old_text": "told", "new_text": "x", "replace_all": True, "expected_occurrences": 2}])
    assert exc.value.code == "invalid_arguments" and "edits[0]" in exc.value.message

def test_missing_relative_path_is_retried_from_the_git_root(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    assert server.cap.execute(["git", "init", "-q"])["success"]
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n")
    server.cap.workspace("set_cwd", "docs")
    # A path copied from git status, after set_cwd into a subfolder.
    read = server.cap.filesystem("read", "src/app.py", line_start=1)
    assert read["content"] == "1\tneedle" and "Git root" in read["note"]
    assert "Git root" in server.cap.filesystem("stat", "src/app.py")["note"]
    listed = server.cap.filesystem("list", "src")
    assert [entry["name"] for entry in listed["entries"]] == ["app.py"] and "Git root" in listed["note"]
    found = server.cap.search("needle", path="src")
    assert found["match_count"] == 1 and "Git root" in found["note"]
    # Writes are not retried there; the error names the Git-root path instead.
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "src/app.py", old_text="needle", new_text="pin")
    assert exc.value.code == "path_not_found" and "Git root" in exc.value.message and "app.py" in exc.value.message
    assert "writes, deletes and moves are not retried" in exc.value.message
    assert (tmp_path / "src" / "app.py").read_text() == "needle\n"
    # The same path policy applies from the Git root.
    denied = make(tmp_path, denied_paths=[str(tmp_path / "src")])
    denied.cap.workspace("set_cwd", "docs")
    with pytest.raises(RuntimeFault) as exc:
        denied.cap.filesystem("read", "src/app.py")
    assert exc.value.code == "path_not_found" and "Git root" not in exc.value.message

def test_git_diff_can_include_untracked_files(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    for argv in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        assert server.cap.git("run", argv)["success"]
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "tracked.txt").write_text("one\n")
    assert server.cap.git("run", ["add", "--", ".gitignore", "tracked.txt"])["success"]
    assert server.cap.git("run", ["commit", "-q", "-m", "base"])["success"]
    (tmp_path / "tracked.txt").write_text("two\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("fresh\n")
    (tmp_path / "ignored.txt").write_text("hidden\n")
    (tmp_path / "big.txt").write_text("".join(f"line {n}\n" for n in range(5000)))
    plain = server.cap.git("diff")
    assert "+two" in plain["stdout"] and "new.txt" not in plain["stdout"]
    server.cap.workspace("set_cwd", "sub")
    out = server.call("git", {"action": "diff", "include_untracked": True})
    text = out["stdout"]
    assert out["success"] and "+two" in text and "+fresh" in text and "ignored.txt" not in text
    assert "[new file cut:" in text and "+line 4999" not in text and len(text) < 20000
    # The real index is untouched: the new files are still untracked.
    status = server.cap.git("run", ["status", "--porcelain", "--untracked-files=all"])["stdout"]
    assert "?? sub/new.txt" in status and "?? big.txt" in status
    # The new files are diffed against the working tree only: a staged
    # view or a revision would show every tracked file as deleted.
    for extra in (["--cached"], ["--staged"], ["HEAD"], ["--stat", "HEAD~0..HEAD"]):
        with pytest.raises(RuntimeFault) as exc:
            server.call("git", {"action": "diff", "include_untracked": True, "args": extra})
        assert exc.value.code == "invalid_arguments" and "include_untracked" in exc.value.message, extra
    # Options and pathspecs still work.
    only = server.call("git", {"action": "diff", "include_untracked": True, "args": ["-U1", "--", "new.txt"]})
    assert "+fresh" in only["stdout"] and "+two" not in only["stdout"] and "deleted" not in only["stdout"]
    # An option's value given as its own argument is not taken for a revision.
    pick = server.call("git", {"action": "diff", "include_untracked": True, "args": ["-S", "HEAD", "--", "new.txt"]})
    assert pick["success"] and "deleted" not in pick["stdout"], pick
    with pytest.raises(RuntimeFault):
        server.call("git", {"action": "log", "include_untracked": True})
    # A new repository has no index file yet.
    other = tmp_path.parent / (tmp_path.name + "-fresh")
    other.mkdir()
    fresh = make(other)
    assert fresh.cap.git("run", ["init", "-q"])["success"]
    (other / "only.txt").write_text("first\n")
    assert "+first" in fresh.call("git", {"action": "diff", "include_untracked": True})["stdout"]

def test_git_diff_untracked_skips_what_it_cannot_add(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path, max_file_read_bytes=1000)
    for argv in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        assert server.cap.git("run", argv)["success"]
    (tmp_path / "tracked.txt").write_text("one\n")
    assert server.cap.git("run", ["add", "--", "tracked.txt"])["success"]
    assert server.cap.git("run", ["commit", "-q", "-m", "base"])["success"]
    (tmp_path / "tracked.txt").write_text("two\n")
    (tmp_path / "small.txt").write_text("fresh\n")
    (tmp_path / "huge.txt").write_bytes(b"x" * 1000 + b"\n")
    # An untracked nested repository with no commit: git cannot add it.
    (tmp_path / "nested").mkdir()
    assert server.cap.execute(["git", "init", "-q"], cwd=str(tmp_path / "nested"))["success"]
    (tmp_path / "nested" / "inner.txt").write_text("inner\n")
    out = server.call("git", {"action": "diff", "include_untracked": True})
    text = out["stdout"]
    assert out["success"] and "+two" in text and "+fresh" in text and "huge.txt" not in text, out
    assert out["untracked_files"] == 1
    assert {"path": "huge.txt", "reason": "too_large", "bytes": 1001} in out["untracked_skipped"]
    assert {"path": "nested/", "reason": "nested_repository"} in out["untracked_skipped"]
    # Only the pathspec's files are counted.
    only = server.call("git", {"action": "diff", "include_untracked": True, "args": ["--", "tracked.txt"]})
    assert only["untracked_files"] == 0 and "untracked_skipped" not in only and "+two" in only["stdout"]
    # Counted from the diff itself: a pathspec without "--", or a filter.
    for extra in (["tracked.txt"], ["--diff-filter=M"]):
        bare = server.call("git", {"action": "diff", "include_untracked": True, "args": extra})
        assert bare["untracked_files"] == 0 and "+two" in bare["stdout"] and "+fresh" not in bare["stdout"], (extra, bare)
    # A failed add keeps the plain diff and says why.
    import agent_runtime.capabilities as caps
    real = caps.subprocess.run

    def failing(argv, **kwargs):
        if "--intent-to-add" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: boom\n")
        return real(argv, **kwargs)

    monkeypatch.setattr(caps.subprocess, "run", failing)
    failed = server.call("git", {"action": "diff", "include_untracked": True})
    assert failed["success"] and "+two" in failed["stdout"] and "+fresh" not in failed["stdout"], failed
    assert failed["untracked_files"] == 0 and "boom" in failed["note"]

def test_search_retries_lookaround_with_pcre2(tmp_path):
    rg = shutil.which("rg")
    if rg is None or subprocess.run([rg, "--pcre2-version"], capture_output=True).returncode != 0:
        pytest.skip("rg with PCRE2 unavailable")
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("foobar\nfoobaz\n")
    for path in (".", "a.txt"):
        out = server.cap.search("foo(?=bar)", path=path, fixed_string=False)
        assert out["match_count"] == 1 and "--pcre2" in out["note"] and "error" not in out
    # A regex that is wrong either way still reports ripgrep's error.
    assert "error" in server.cap.search("foo(", fixed_string=False)

def test_pcre2_retry_is_kept_despite_per_file_errors(tmp_path, monkeypatch):
    server = make(tmp_path)
    first = subprocess.CompletedProcess([], 2, "", "regex parse error:\n    foo(?=bar)\nerror: look-around, including look-ahead and look-behind, is not supported\n\nConsider enabling PCRE2 with the --pcre2 flag, which can handle backreferences\nand look-around.\n")
    replies = {}
    monkeypatch.setattr(server.cap, "_run_rg", lambda command, cwd=None: replies["pcre2"] if "--pcre2" in command else first)
    # Exit 2 for a locked file, but the regex ran: keep the retry.
    replies["pcre2"] = subprocess.CompletedProcess([], 2, "{}\n", "rg: ./locked.txt: Access is denied. (os error 5)\n")
    notes = []
    assert server.cap._run_rg_regex(["rg", "-e", "foo(?=bar)", "."], notes) is replies["pcre2"] and "--pcre2" in notes[0]
    # A PCRE2 match error in one file: the regex compiled, keep the retry.
    replies["pcre2"] = subprocess.CompletedProcess([], 2, "{}\n", "rg: ./big.txt: PCRE2: error matching: match limit exceeded\n")
    notes = []
    assert server.cap._run_rg_regex(["rg", "-e", "foo(?=bar)", "."], notes) is replies["pcre2"] and "--pcre2" in notes[0]
    # A pattern PCRE2 cannot compile either: the first error stays.
    replies["pcre2"] = subprocess.CompletedProcess([], 2, "", "rg: PCRE2: error compiling pattern at offset 9: missing closing parenthesis\n")
    assert server.cap._run_rg_regex(["rg", "-e", "foo(?=bar)", "."], []) is first
    # A ripgrep built without PCRE2: the first error stays, with a note.
    replies["pcre2"] = subprocess.CompletedProcess([], 2, "", "PCRE2 is not available in this build of ripgrep.\n")
    notes = []
    assert server.cap._run_rg_regex(["rg", "-e", "foo(?=bar)", "."], notes) is first and "PCRE2 unavailable" in notes[0]

def test_search_fallback_and_git(tmp_path, monkeypatch):
    server = make(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("# TODO UserService\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = server.cap.search("UserService", glob=["*.py"])
    assert result["results"][0]["matches"][0]["line"] == 1
    files = server.cap.search(path=".", glob=["*.py"], files_only=True)
    assert files["results"]
    if shutil.which("git") is None:
        pytest.skip("git unavailable")

def test_git_when_available(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    assert server.cap.execute(["git", "init"])["success"]
    status = server.cap.git("status")
    assert status["success"] and "branch" in status["stdout"].lower()

def test_shell_string_auto_enables_shell(tmp_path):
    server = make(tmp_path)
    ok = server.cap.execute("echo invalid")
    assert ok["success"] and "invalid" in ok["stdout"]

def test_filesystem_errors_are_structured(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as missing:
        server.cap.filesystem("stat", "no-such-file.txt")
    assert missing.value.code == "path_not_found"
    server.cap.filesystem("write", "ok.txt", "x")
    server.cap.filesystem("move", "ok.txt", destination="new-dir/moved.txt")
    assert (tmp_path / "new-dir" / "moved.txt").read_text() == "x"

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
    assert os.path.join("src", "hit.py") in paths
    assert not any(".venv" in p for p in paths)
    assert not any("big.py" in p for p in paths)
    assert "**/.git/" in content["applied_excludes"]
    ctx = server.cap.search("UserService", path="src/hit.py", context_lines=1)
    [hit] = ctx["results"][0]["matches"]
    assert hit["line"] == 2
    assert hit["before"] == ["1\tline one"]
    assert hit["after"] == ["3\tline three"]
    with pytest.raises(RuntimeFault):
        server.cap.search("UserService", files_only=True)


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
    assert lines["content"] == "2\tsecond line\n3\tthird line"
    assert lines["total_lines"] == 3 and lines["truncated"] is False
    assert "next_line" not in lines
    tail = server.cap.filesystem("read", "a.txt", line_start=3)
    assert tail["line_end"] == 3 and tail["truncated"] is False
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("read", "a.txt", line_start=3, line_end=2)
    # Past the end: a note and the last lines instead of an error.
    past = server.cap.filesystem("read", "a.txt", line_start=9)
    assert past["content"].endswith("3\tthird line") and "past the end" in past["note"]


def test_read_on_a_directory_lists_it(tmp_path):
    server = make(tmp_path)
    (tmp_path / "sub").mkdir()
    for name in ("b.txt", "a.py", ".hidden"):
        (tmp_path / "sub" / name).write_text("x")
    # The same entries and paging as list, plus a note.
    out = server.cap.filesystem("read", "sub", limit=1, offset=1)
    listed = server.cap.filesystem("list", "sub", limit=1, offset=1)
    assert "directory" in out.pop("note") and out == listed
    assert server.cap.filesystem("read", "sub", glob=["*.py"], include_hidden=True)["total"] == 1
    # Same policy as list: a denied folder stays denied.
    denied = make(tmp_path, denied_paths=[str(tmp_path / "sub")])
    with pytest.raises(RuntimeFault):
        denied.cap.filesystem("read", "sub")


def test_read_lines_caps_output_and_streams(tmp_path):
    server = make(tmp_path, max_read_chars=200, max_file_read_bytes=10)
    (tmp_path / "long.txt").write_text("".join(f"row {n}\n" for n in range(1, 101)))
    # Line mode streams, so max_file_read_bytes does not apply.
    first = server.cap.filesystem("read", "long.txt", line_start=1)
    assert first["truncated"] is True and len(first["content"]) <= 200
    assert first["content"].startswith("1\trow 1\n2\trow 2")
    assert "total_lines" not in first
    nxt = server.cap.filesystem("read", "long.txt", line_start=first["next_line"])
    assert nxt["content"].startswith(f"{first['next_line']}\trow {first['next_line']}")
    page = nxt
    for _ in range(100):
        if "next_line" not in page:
            break
        page = server.cap.filesystem("read", "long.txt", line_start=page["next_line"])
    assert "next_line" not in page
    assert page["total_lines"] == 100 and page["content"].endswith("100\trow 100")
    (tmp_path / "wide.txt").write_text("x" * 1000 + "\nshort\n")
    wide = server.cap.filesystem("read", "wide.txt", line_start=1)
    assert wide["content"].endswith("...[line cut]") and wide["next_line"] == 2
    (tmp_path / "empty.txt").write_text("")
    empty = server.cap.filesystem("read", "empty.txt", line_start=1)
    assert empty["content"] == "" and empty["total_lines"] == 0


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
def test_process_string_auto_enables_shell(tmp_path): server = make(tmp_path); p = server.processes.start("echo proc-shell-ok")["process_id"]; out = server.processes.read(p, wait_ms=5000); assert "proc-shell-ok" in out["stdout"]; server.processes.stop(p, True)
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

def test_stringified_array_coerced(tmp_path):
    import json
    server = make(tmp_path)
    payload = json.dumps([sys.executable, "-c", "print(1)"])
    ok = server.policy.authorize_command(payload, str(tmp_path), True)
    assert ok["shell"] is True
    run = server.cap.execute(payload)
    assert run["success"] and "1" in run["stdout"]
    proc = server.processes.start(payload)
    assert proc["process_id"]
    server.processes.stop(proc["process_id"], True)
    plain = server.policy.authorize_command("[System.Console]::Beep()", str(tmp_path), True)
    assert plain["shell"] is True

def test_unknown_argument_suggests(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as exc:
        server.call("filesystem", {"action": "read", "path": "x", "bogus": "y"})
    assert exc.value.code == "invalid_arguments" and "Valid arguments" in exc.value.message
    assert "old_text" in exc.value.message
    with pytest.raises(RuntimeFault) as exc2:
        server.call("execute", {"command": ["x"], "bogus": 1})
    assert "Valid arguments" in exc2.value.message and "command" in exc2.value.message

def test_argument_types_follow_the_schema(tmp_path):
    server = make(tmp_path)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "keep.txt").write_text("x")
    # "false" is a non-empty string, so it used to be truthy and ran rmtree.
    with pytest.raises(RuntimeFault):
        server.call("filesystem", {"action": "delete", "path": "d", "recursive": "false"})
    assert (tmp_path / "d" / "keep.txt").exists()
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.md").write_text("needle\n")
    # A bare glob string used to become one glob per character ("*" matched everything).
    found = server.call("search", {"query": "needle", "glob": "*.py"})
    assert [entry["path"] for entry in found["results"]] == ["a.py"]
    listed = server.call("filesystem", {"action": "list", "path": ".", "glob": "*.py"})
    assert [entry["name"] for entry in listed["entries"]] == ["a.py"]
    # fixed_string "false" means a regex.
    assert server.call("search", {"query": "ne+dle", "fixed_string": "FALSE", "glob": "*.py"})["match_count"] == 1
    assert server.call("search", {"query": "needle", "max_results": "1", "glob": '["*.py"]'})["match_count"] == 1
    multi = server.call("multi_search", {"searches": json.dumps([{"query": "needle", "glob": "*.md", "case_sensitive": "true"}])})
    assert [entry["path"] for entry in multi["results"][0]["results"]] == ["b.md"]
    # "[1]" is valid JSON, but not a list of strings: a character-class glob.
    (tmp_path / "1").write_text("needle\n")
    assert [entry["path"] for entry in server.call("search", {"query": "needle", "glob": "[1]"})["results"]] == ["1"]
    assert [e["name"] for e in server.call("filesystem", {"action": "list", "path": ".", "glob": "[1]"})["entries"]] == ["1"]
    (tmp_path / "1").unlink()
    # Stringified arrays and objects.
    edited = server.call("filesystem", {"action": "multi_edit", "path": "a.py", "edits": json.dumps(
        [{"old_text": "needle", "new_text": "pin", "expected_occurrences": "1"}])})
    assert edited["edits"][0]["replacements"] == 1 and (tmp_path / "a.py").read_text() == "pin\n"
    batch = server.call("batch", {"calls": json.dumps(
        [{"tool": "filesystem", "arguments": json.dumps({"action": "stat", "path": "b.md"})}])})
    assert batch["results"][0]["result"]["type"] == "file"
    ran = server.call("execute", {"command": [sys.executable, "-c", "import os; print(os.environ['LF_X'])"],
                                  "env": json.dumps({"LF_X": "hi"})})
    assert ran["success"] and "hi" in ran["stdout"]
    # Calls inside a batch are fixed the same way.
    inner = server.call("batch", {"calls": [{"tool": "filesystem", "arguments": {
        "action": "delete", "path": "d", "recursive": "false"}}]})
    assert "error" in inner["results"][0] and (tmp_path / "d" / "keep.txt").exists()

def test_bad_argument_types_are_reported_together(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as exc:
        server.call("filesystem", {"action": "delete", "path": "x", "recursive": "maybe", "limit": "lots",
                                   "edits": "not json"})
    message = exc.value.message
    assert exc.value.code == "invalid_arguments"
    assert "recursive: expected true or false, got 'maybe'" in message
    assert "limit: expected an integer, got 'lots'" in message and "edits: expected an array" in message
    # One bad multi_search entry fails only itself (this used to fail the whole call).
    (tmp_path / "x.txt").write_text("x\n")
    multi = server.call("multi_search", {"searches": [{"query": "x", "max_results": "lots", "label": "bad"},
                                                      {"query": "x", "max_results": "5"}]})
    bad, good = multi["results"]
    assert bad["label"] == "bad" and bad["error"]["code"] == "invalid_arguments"
    assert "searches[0].max_results: expected an integer" in bad["error"]["message"]
    assert "error" not in good and good["match_count"] == 1
    # A non-boolean for a boolean is refused too: recursive 1 used to run rmtree.
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "keep.txt").write_text("x")
    with pytest.raises(RuntimeFault) as exc:
        server.call("filesystem", {"action": "delete", "path": "d", "recursive": 1, "limit": "9" * 5000})
    assert "recursive: expected true or false, got 1" in exc.value.message
    # Past Python's 4300-digit int limit: an argument error, not internal_error.
    assert exc.value.code == "invalid_arguments" and "limit: expected an integer" in exc.value.message
    assert (tmp_path / "d" / "keep.txt").exists()

def test_patch_placement_stays_fast_on_repetitive_files(tmp_path):
    server = make(tmp_path)
    (tmp_path / "r.txt").write_text("x\n" * 200_000)
    # 299 lines that match everywhere, then one that matches nowhere.
    hunk = " x\n" * 299 + "-z\n+y\n"
    started = time.monotonic()
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("apply_patch", ".", patch="--- a/r.txt\n+++ b/r.txt\n@@ -1,300 +1,300 @@\n" + hunk)
    assert exc.value.code == "content_mismatch" and time.monotonic() - started < 3

def test_apply_patch_modify_and_create(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@ header\n one\n-two\n+TWO\n three\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n")
    out = server.cap.filesystem("apply_patch", ".", patch=diff)
    assert len(out["files"]) == 2
    assert (tmp_path / "a.txt").read_text().splitlines() == ["one", "TWO", "three"]
    assert (tmp_path / "new.txt").read_text().splitlines() == ["hello", "world"]

def test_apply_patch_atomic_on_mismatch(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    (tmp_path / "b.txt").write_text("other\n")
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
            "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-missing\n+hit\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("apply_patch", ".", patch=diff)
    assert exc.value.code == "content_mismatch" and "b.txt" in exc.value.message
    assert (tmp_path / "a.txt").read_text().splitlines() == ["one", "two"]

def test_apply_patch_rejects_escape_delete_and_garbage(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("apply_patch", ".", patch="--- a/../x\n+++ b/../x\n@@ -0,0 +1 @@\n+q\n")
    (tmp_path / "gone.txt").write_text("x\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("apply_patch", ".", patch="--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    assert exc.value.code == "invalid_arguments"
    assert (tmp_path / "gone.txt").exists()
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("apply_patch", ".", patch="garbage")

def test_apply_patch_no_trailing_newline(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"one\ntwo")
    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n\\ No newline at end of file\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert (tmp_path / "a.txt").read_text() == "one\nTWO"


def test_filesystem_copy_file_and_bracket_name(tmp_path):
    server = make(tmp_path)
    (tmp_path / "[KBMISC001] - note.pdf").write_bytes(b"%PDF-1.6 fake")
    out = server.cap.filesystem("copy", "[KBMISC001] - note.pdf", destination="staged/copy.pdf")
    assert (tmp_path / "staged" / "copy.pdf").read_bytes() == b"%PDF-1.6 fake"
    assert out["bytes"] == len(b"%PDF-1.6 fake")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("copy", "[KBMISC001] - note.pdf")
    readonly = make(tmp_path, "READ_ONLY")
    with pytest.raises(RuntimeFault):
        readonly.cap.filesystem("copy", "staged/copy.pdf", destination="elsewhere.pdf")


def test_filesystem_copy_dir_needs_recursive(tmp_path):
    server = make(tmp_path)
    (tmp_path / "srcdir").mkdir()
    (tmp_path / "srcdir" / "f.txt").write_text("data")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("copy", "srcdir", destination="destdir")
    out = server.cap.filesystem("copy", "srcdir", destination="destdir", recursive=True)
    assert (tmp_path / "destdir" / "f.txt").read_text() == "data"
    assert out["copied_items"] >= 2


def test_move_and_copy_refuse_to_overwrite(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    for action in ("move", "copy"):
        with pytest.raises(RuntimeFault) as exc:
            server.call("filesystem", {"action": action, "path": "a.txt", "destination": "b.txt"})
        assert exc.value.code == "invalid_arguments" and "overwrite" in exc.value.message
    assert (tmp_path / "b.txt").read_text() == "b" and (tmp_path / "a.txt").exists()
    server.call("filesystem", {"action": "copy", "path": "a.txt", "destination": "b.txt", "overwrite": True})
    assert (tmp_path / "b.txt").read_text() == "a"
    (tmp_path / "b.txt").write_text("b")
    server.call("filesystem", {"action": "move", "path": "a.txt", "destination": "b.txt", "overwrite": True})
    assert (tmp_path / "b.txt").read_text() == "a" and not (tmp_path / "a.txt").exists()
    # An existing directory is never replaced or merged, even with overwrite.
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    (tmp_path / "d2" / "keep").write_text("k")
    for action in ("move", "copy"):
        for source in ("d1", "b.txt"):
            with pytest.raises(RuntimeFault) as exc:
                server.call("filesystem", {"action": action, "path": source, "destination": "d2",
                                           "recursive": True, "overwrite": True})
            assert "directory" in exc.value.message
    assert (tmp_path / "d2" / "keep").exists() and (tmp_path / "d1").is_dir() and (tmp_path / "b.txt").exists()
    # The source itself under another spelling is not an overwrite.
    server.call("filesystem", {"action": "move", "path": "b.txt", "destination": "./b.txt"})
    assert (tmp_path / "b.txt").read_text() == "a"

def test_move_and_copy_refuse_a_folder_into_itself(tmp_path):
    server = make(tmp_path)
    (tmp_path / "dd" / "sub").mkdir(parents=True)
    (tmp_path / "dd" / "f.txt").write_text("x")
    cases = [("move", "dd/sub/moved"), ("copy", "dd/sub/copy"), ("copy", "dd/sub")]
    if os.name == "nt":
        cases.append(("copy", "DD/sub/upper"))
    for action, dest in cases:
        with pytest.raises(RuntimeFault) as exc:
            server.call("filesystem", {"action": action, "path": "dd", "destination": dest, "recursive": True})
        assert exc.value.code == "invalid_arguments" and "inside the source folder" in exc.value.message, (action, exc.value)
    # Nothing was created.
    assert sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "dd").rglob("*")) == ["dd/f.txt", "dd/sub"]
    # A file copied onto itself, under any spelling, is refused clearly.
    for dest, overwrite in (("dd/f.txt", False), ("./dd/f.txt", True)) + ((("DD/F.TXT", True),) if os.name == "nt" else ()):
        with pytest.raises(RuntimeFault) as exc:
            server.call("filesystem", {"action": "copy", "path": "dd/f.txt", "destination": dest, "overwrite": overwrite})
        assert exc.value.code == "invalid_arguments" and "source itself" in exc.value.message, (dest, exc.value)
    assert (tmp_path / "dd" / "f.txt").read_text() == "x"

def test_move_renames_a_file_whose_name_changes_only_in_case(tmp_path):
    (tmp_path / "Case.txt").write_text("x")
    if not (tmp_path / "case.txt").exists():
        pytest.skip("case-sensitive file system")
    server = make(tmp_path)
    out = server.call("filesystem", {"action": "move", "path": "Case.txt", "destination": "case.txt"})
    assert [p.name for p in tmp_path.iterdir()] == ["case.txt"] and (tmp_path / "case.txt").read_text() == "x", out


def test_redact_keeps_code_redacts_secrets():
    from agent_runtime.security import redact
    code = "SharepointCertificatePassword = OverrideSecret(\n    body, \"sharepoint_certificate_password\", current.SharepointCertificatePassword),"
    assert redact(code) == code
    dotted = "secret = cfg.Secret"
    assert redact(dotted) == dotted
    assert redact("Secret=hunter2-secret-value") == "Secret=[REDACTED]"
    assert redact("password = null") == "password = null"


def test_git_guards_broad_add_and_secrets(tmp_path):
    server = make(tmp_path)
    for spec in (["add", "-A"], ["add", "--all"], ["add", "."]):
        with pytest.raises(RuntimeFault) as exc:
            server.cap.git("run", args=spec)
        assert exc.value.code == "invalid_arguments" and "explicit" in exc.value.message
    with pytest.raises(RuntimeFault) as exc2:
        server.cap.git("run", args=["add", "certs/sp-ingest.pfx"])
    assert exc2.value.code == "secret_file"


def test_path_outside_roots_is_actionable(tmp_path):
    from agent_runtime.config import Config
    from agent_runtime.server import Server
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    cfg = Config(str(tmp_path), mode="DEVELOPMENT", allowed_read_roots=[str(tmp_path)],
                 allowed_write_roots=[str(tmp_path)], inherit_environment=["PATH"],
                 default_shell="cmd" if os.name == "nt" else "sh")
    server = Server(cfg)
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("read", str(outside))
    assert exc.value.code == "path_outside_roots"
    assert "allowed_read_roots" in exc.value.message and "restart" in exc.value.message.lower()


def test_default_env_inherits_programdata(tmp_path):
    from agent_runtime.config import Config
    cfg = Config(str(tmp_path))
    assert "ProgramData" in cfg.inherit_environment


def test_gh_counts_as_network(tmp_path):
    server = make(tmp_path)
    ok = server.policy.authorize_command(["gh", "pr", "list"], str(tmp_path))
    assert ok["network_classified"] is True
    local = server.policy.authorize_command(["gh", "--version"], str(tmp_path))
    assert local["network_classified"] is False
    offline = make(tmp_path, network="disabled")
    with pytest.raises(RuntimeFault) as exc:
        offline.cap.execute(["gh", "pr", "list"])
    assert exc.value.code == "network_denied"


def test_python_fallback_honors_regex(tmp_path, monkeypatch):
    import shutil
    server = make(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hit.py").write_text("line one\nUserService here\nline three\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    found = server.cap.search("User.*here", fixed_string=False)
    assert found["engine"] == "python" and len(found["results"]) == 1
    literal = server.cap.search("User.*here")
    assert literal["results"] == []
    with pytest.raises(RuntimeFault) as exc:
        server.cap.search("(unclosed", fixed_string=False)
    assert exc.value.code == "invalid_arguments"


def test_empty_output_failure_explains_itself(tmp_path):
    server = make(tmp_path)
    bad = server.cap.execute([sys.executable, "-c", "raise SystemExit(7)"])
    assert bad["exit_code"] == 7 and "no output captured" in bad["stderr"]
    ok = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert "no output captured" not in ok["stderr"]


def test_replace_text_rejects_identical_and_hints_count(tmp_path):
    server = make(tmp_path)
    server.cap.filesystem("write", "a.txt", "hello")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "a.txt", old_text="hello", new_text="hello")
    assert exc.value.code == "invalid_arguments" and "identical" in exc.value.message
    (tmp_path / "b.txt").write_text("x x")
    with pytest.raises(RuntimeFault) as exc2:
        server.cap.filesystem("replace_text", "b.txt", old_text="x", new_text="y")
    assert exc2.value.code == "content_mismatch" and "expected_occurrences" in exc2.value.message
    server.cap.filesystem("replace_text", "b.txt", old_text="x", new_text="y", expected_occurrences=2)
    assert (tmp_path / "b.txt").read_text() == "y y"


def test_search_surfaces_rg_errors(tmp_path, monkeypatch):
    import shutil
    import subprocess
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(shutil, "which", lambda name: "rg" if name == "rg" else None)
    real_run = subprocess.run
    def fake_error(*args, **kwargs):
        class R:
            returncode = 2
            stdout = ""
            stderr = "rg: regex parse error (line 1)"
        return R()
    monkeypatch.setattr(subprocess, "run", fake_error)
    try:
        out = server.cap.search("UserService")
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)
    assert out["results"] == [] and "regex parse error" in out.get("error", "")


def test_search_single_file_with_rg(tmp_path):
    import shutil
    if shutil.which("rg") is None:
        pytest.skip("rg unavailable")
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("hello UserService\n")
    out = server.cap.search("UserService", path="a.txt")
    assert out["engine"] == "ripgrep"
    assert len(out["results"]) == 1 and out["results"][0]["matches"][0]["line"] == 1
    assert "**/.git/" in out["applied_excludes"]


def test_execute_timeout_above_default_warns(tmp_path):
    server = make(tmp_path)
    over = float(server.cfg.default_timeout_seconds) + 20
    out = server.cap.execute([sys.executable, "-c", "print('ok')"], timeout=over)
    assert out["success"] and "ok" in out["stdout"]
    assert "process" in out.get("warning", "")


def test_execute_timeout_at_default_has_no_warning(tmp_path):
    server = make(tmp_path)
    out = server.cap.execute([sys.executable, "-c", "print('ok')"],
                             timeout=float(server.cfg.default_timeout_seconds))
    assert out["success"] and "warning" not in out


def test_execute_schema_steers_long_waits_to_process(tmp_path):
    tools = {t["name"]: t for t in make(tmp_path).tools()}
    assert "process" in tools["execute"]["description"]
    props = tools["execute"]["inputSchema"]["properties"]
    assert "process" in props["timeout"]["description"]


def test_multi_search_runs_queries_in_one_call(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("alpha beta\n")
    (tmp_path / "b.txt").write_text("gamma delta\n")
    out = server.call("multi_search", {"searches": [
        {"query": "alpha", "label": "first"},
        {"query": "delta", "glob": ["b.txt"]},
    ]})
    assert len(out["results"]) == 2
    first, second = out["results"]
    assert first["index"] == 0 and first["label"] == "first"
    assert first["results"][0]["matches"][0]["text"] == "alpha beta"
    assert second["index"] == 1 and second["label"] is None
    assert second["results"][0]["path"] == "b.txt"


def test_multi_search_isolates_failures(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\n")
    out = server.call("multi_search", {"searches": [
        {"query": "alpha"},
        {"query": "beta", "max_file_size_bytes": 0},
        {"files_only": True, "glob": ["*.txt"]},
    ]})
    ok, bad, files = out["results"]
    assert ok["results"][0]["matches"][0]["text"] == "alpha"
    assert bad["error"]["code"] == "invalid_arguments"
    assert files["results"]


def test_multi_search_validates_input(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as empty:
        server.call("multi_search", {"searches": []})
    assert empty.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as missing:
        server.call("multi_search", {})
    assert "searches" in missing.value.message
    with pytest.raises(RuntimeFault) as many:
        server.call("multi_search", {"searches": [{"query": "x"}] * 17})
    assert many.value.code == "invalid_arguments" and "16" in many.value.message
    with pytest.raises(RuntimeFault) as shape:
        server.call("multi_search", {"searches": ["oops"]})
    assert shape.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as teach:
        server.call("multi_search", {"searches": []})
    assert '"query"' in teach.value.message


def test_multi_search_entries_take_labels(tmp_path):
    server = make(tmp_path)
    out = server.call("multi_search", {"searches": [
        {"query": "todo", "label": "todos"},
        {"query": "fixme"},
    ]})
    assert out["results"][0]["label"] == "todos"
    assert out["results"][1]["label"] is None


def test_tool_calls_execute_concurrently(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    server = make(tmp_path)
    slow = [sys.executable, "-c", "import time; time.sleep(1); print('done')"]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(server.call, "execute", {"command": slow}) for _ in range(2)]
        results = [future.result() for future in futures]
    elapsed = time.monotonic() - started
    assert all(result["success"] and "done" in result["stdout"] for result in results)
    assert elapsed < 1.9, f"tool calls serialized: {elapsed:.2f}s"


def test_batch_runs_calls_in_one_round_trip(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\n")
    slow = [sys.executable, "-c", "import time; time.sleep(1); print('done')"]
    started = time.monotonic()
    out = server.call("batch", {"calls": [
        {"tool": "execute", "arguments": {"command": slow}, "label": "slow-1"},
        {"tool": "execute", "arguments": {"command": slow}, "label": "slow-2"},
        {"tool": "search", "arguments": {"query": "alpha", "path": str(tmp_path)},
         "label": "find"},
    ]})
    elapsed = time.monotonic() - started
    assert len(out["results"]) == 3
    slow_one, slow_two, find = out["results"]
    assert slow_one["index"] == 0 and slow_one["tool"] == "execute"
    assert slow_one["label"] == "slow-1" and "done" in slow_one["result"]["stdout"]
    assert slow_two["label"] == "slow-2" and "done" in slow_two["result"]["stdout"]
    assert find["result"]["results"][0]["matches"][0]["text"] == "alpha"
    assert elapsed < 1.9, f"batch calls serialized: {elapsed:.2f}s"


def test_batch_isolates_failures(tmp_path):
    server = make(tmp_path)
    out = server.call("batch", {"calls": [
        {"tool": "search", "arguments": {"query": "x"}},
        {"tool": "execute", "arguments": {"command": [sys.executable, "-c", "raise SystemExit(7)"]}},
        {"tool": "bogus", "arguments": {}},
        {"tool": "batch", "arguments": {"calls": []}},
    ]})
    ok, exit7, unknown, nested = out["results"]
    assert "results" in ok["result"]
    assert exit7["result"]["exit_code"] == 7
    assert unknown["error"]["code"] == "tool_not_found"
    assert nested["error"]["code"] == "invalid_arguments"


def test_batch_validates_shape(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as empty:
        server.call("batch", {"calls": []})
    assert empty.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as missing:
        server.call("batch", {})
    assert "calls" in missing.value.message
    with pytest.raises(RuntimeFault) as many:
        server.call("batch", {"calls": [{"tool": "workspace", "arguments": {"action": "get"}}] * 17})
    assert many.value.code == "invalid_arguments" and "16" in many.value.message
    with pytest.raises(RuntimeFault) as shape:
        server.call("batch", {"calls": ["oops"]})
    assert shape.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as args:
        server.call("batch", {"calls": [{"tool": "workspace", "arguments": "get"}]})
    assert args.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as teach:
        server.call("batch", {"calls": []})
    assert '"tool"' in teach.value.message


def test_batch_accepts_host_prefixed_tool_names(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    out = server.call("batch", {"calls": [
        {"tool": "MyHost_localforge_mcp__search",
         "arguments": {"query": "alpha", "path": str(tmp_path)},
         "label": "prefixed"},
    ]})
    entry = out["results"][0]
    assert "result" in entry, entry
    assert entry["label"] == "prefixed"
    assert entry["result"]["results"][0]["matches"][0]["text"] == "alpha"


def test_batch_maps_common_tool_names(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("needle\n")
    out = server.batch([{"tool": "grep", "arguments": {"query": "needle"}},
                        {"tool": "MyHost_localforge_mcp__bash", "arguments": {"command": [sys.executable, "-c", "print(7)"]}},
                        {"tool": "nope", "arguments": {}}])
    first, second, third = out["results"]
    assert first["tool"] == "search" and first["result"]["match_count"] == 1
    assert 'no "grep" tool' in first["note"] and 'Call "search" directly' in first["note"]
    assert second["tool"] == "execute" and second["result"]["success"] and '"bash"' in second["note"]
    assert "default_shell" in second["note"] and "not in bash" in second["note"]
    assert "default_shell" not in first["note"]
    assert third["error"]["code"] == "tool_not_found" and "note" not in third

def test_batch_edits_distinct_files_in_parallel(tmp_path):
    server = make(tmp_path)
    names = ["a.txt", "b.txt", "c.txt"]
    for name in names:
        (tmp_path / name).write_text(f"old {name}\n")
    out = server.call("batch", {"calls": [
        {"tool": "filesystem", "arguments": {"action": "replace_text", "path": name,
            "old_text": f"old {name}", "new_text": f"new {name}"}}
        for name in names
    ]})
    assert all("result" in call and "replacements" in call["result"] for call in out["results"])
    for name in names:
        assert (tmp_path / name).read_text() == f"new {name}\n"


def test_batch_sequential_keeps_order_and_stops_on_success_false(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("old\n")
    out = server.call("batch", {"sequential": True, "calls": [
        {"tool": "filesystem", "arguments": {"action": "replace_text", "path": "a.txt",
            "old_text": "old", "new_text": "new"}, "label": "edit"},
        {"tool": "MyHost_localforge_mcp__filesystem", "arguments": {"action": "read", "path": "a.txt",
            "line_start": 1}, "label": "read"},
        {"tool": "execute", "arguments": {"command": [sys.executable, "-c", "raise SystemExit(3)"]},
         "label": "test"},
        {"tool": "filesystem", "arguments": {"action": "write", "path": "b.txt", "content": "x"},
         "label": "commit"},
    ]})
    edit, read, failed, skipped = out["results"]
    assert "replacements" in edit["result"]
    # The read sees the edit made by the call before it.
    assert read["tool"] == "filesystem" and read["result"]["content"] == "1\tnew"
    assert failed["result"]["success"] is False and failed["result"]["exit_code"] == 3
    assert skipped == {"index": 3, "tool": "filesystem", "label": "commit", "skipped": True,
                       "reason": "an earlier call failed (index 2)"}
    assert not (tmp_path / "b.txt").exists()
    assert out["completed"] == 2 and out["stopped_at"] == 2


def test_batch_sequential_stops_on_a_raised_error(tmp_path):
    server = make(tmp_path)
    out = server.call("batch", {"sequential": True, "calls": [
        {"tool": "workspace", "arguments": {"action": "get"}},
        {"tool": "filesystem", "arguments": {"action": "replace_text", "path": "missing.txt",
            "old_text": "a", "new_text": "b"}},
        {"tool": "filesystem", "arguments": {"action": "write", "path": "b.txt", "content": "x"}},
        {"tool": "batch", "arguments": {"calls": []}},
    ]})
    ok, failed, *skipped = out["results"]
    assert "result" in ok
    assert failed["error"]["code"] == "path_not_found"
    assert [entry["index"] for entry in skipped] == [2, 3]
    assert all(entry["skipped"] and entry["reason"] == "an earlier call failed (index 1)"
               and "result" not in entry and "error" not in entry for entry in skipped)
    assert not (tmp_path / "b.txt").exists()
    assert out["completed"] == 1 and out["stopped_at"] == 1
    # Nested batch still fails, and so stops the run.
    nested = server.call("batch", {"sequential": True, "calls": [
        {"tool": "batch", "arguments": {"calls": []}},
        {"tool": "workspace", "arguments": {"action": "get"}}]})
    assert nested["results"][0]["error"]["code"] == "invalid_arguments"
    assert nested["results"][1]["skipped"] and nested["stopped_at"] == 0


def test_batch_sequential_only_counts_raised_or_success_false(tmp_path):
    server = make(tmp_path, edit_checks={"*.py": [sys.executable, "-m", "py_compile", "{path}"]})
    out = server.call("batch", {"sequential": True, "calls": [
        {"tool": "filesystem", "arguments": {"action": "write", "path": "bad.py", "content": "def (:\n"}},
        {"tool": "execute", "arguments": {"command": [sys.executable, "-c", "print('ran')"]}},
    ]})
    edit, ran = out["results"]
    # A failing edit check is not a failed call; the agent reads check itself.
    assert edit["result"]["check"]["ok"] is False
    assert "ran" in ran["result"]["stdout"]
    assert out["completed"] == 2 and out["stopped_at"] is None
    with pytest.raises(RuntimeFault) as many:
        server.call("batch", {"sequential": True,
                              "calls": [{"tool": "workspace", "arguments": {"action": "get"}}] * 17})
    assert "16" in many.value.message
    with pytest.raises(RuntimeFault) as flag:
        server.call("batch", {"sequential": "yes", "calls": [{"tool": "workspace", "arguments": {"action": "get"}}]})
    assert flag.value.code == "invalid_arguments" and "sequential" in flag.value.message


def test_batch_parallel_default_shape_unchanged(tmp_path):
    server = make(tmp_path)
    calls = [{"tool": "execute", "arguments": {"command": [sys.executable, "-c", "raise SystemExit(3)"]}},
             {"tool": "workspace", "arguments": {"action": "get"}}]
    for out in (server.call("batch", {"calls": calls}),
                server.call("batch", {"calls": calls, "sequential": False})):
        assert list(out) == ["results"]
        failed, ran = out["results"]
        assert failed["result"]["exit_code"] == 3 and "result" in ran and "skipped" not in ran


def test_concurrent_edits_same_file_serialize(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")

    def edit(word):
        return server.call("filesystem", {"action": "replace_text", "path": "a.txt",
            "old_text": "one", "new_text": word})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(edit, "two"), pool.submit(edit, "three")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("ok", future.result()))
            except RuntimeFault as e:
                outcomes.append(("fault", e.code))
    assert (tmp_path / "a.txt").read_text() in ("two\n", "three\n")
    ok = [outcome for outcome in outcomes if outcome[0] == "ok"]
    faults = [outcome for outcome in outcomes if outcome[0] == "fault"]
    assert len(ok) == 1 and len(faults) == 1 and faults[0][1] == "content_mismatch"


def test_tool_descriptions_steer_file_edits_off_execute(tmp_path):
    tools = {t["name"]: t for t in make(tmp_path).tools()}
    for name in ("execute", "process", "filesystem"):
        assert "filesystem" in tools[name]["description"]
        assert "replace_text" in tools[name]["description"]


def test_execute_child_gets_closed_stdin(tmp_path):
    # Children must never inherit the MCP control channel (stdin):
    # a child reading stdin would consume the JSON-RPC request stream,
    # and on Windows an inherited control pipe can hang the child.
    server = make(tmp_path)
    out = server.cap.execute(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
    assert out["success"] and "''" in out["stdout"]


def test_multi_edit_applies_all_edits(tmp_path):
    server = make(tmp_path)
    (tmp_path / "app.py").write_text("def old_one():\n    pass\n\ndef old_two():\n    pass\n")
    out = server.cap.filesystem("multi_edit", "app.py", edits=[
        {"old_text": "old_one", "new_text": "new_one"},
        {"old_text": "old_two", "new_text": "new_two"},
    ])
    assert [e["replacements"] for e in out["edits"]] == [1, 1]
    assert (tmp_path / "app.py").read_text() == \
        "def new_one():\n    pass\n\ndef new_two():\n    pass\n"


def test_multi_edit_all_or_nothing(tmp_path):
    server = make(tmp_path)
    (tmp_path / "app.py").write_text("alpha beta\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("multi_edit", "app.py", edits=[
            {"old_text": "alpha", "new_text": "ALPHA"},
            {"old_text": "missing", "new_text": "x"},
        ])
    assert exc.value.code == "content_mismatch" and "edits[1]" in exc.value.message
    assert (tmp_path / "app.py").read_text() == "alpha beta\n"


def test_multi_edit_validates_shape_counts_and_order(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("x x\n")
    with pytest.raises(RuntimeFault) as empty:
        server.cap.filesystem("multi_edit", "a.txt", edits=[])
    assert empty.value.code == "invalid_arguments" and "edits" in empty.value.message
    with pytest.raises(RuntimeFault) as shape:
        server.cap.filesystem("multi_edit", "a.txt", edits=[{"old_text": "x"}])
    assert shape.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as ident:
        server.cap.filesystem("multi_edit", "a.txt",
                              edits=[{"old_text": "x", "new_text": "x"}])
    assert ident.value.code == "invalid_arguments" and "identical" in ident.value.message
    with pytest.raises(RuntimeFault) as count:
        server.cap.filesystem("multi_edit", "a.txt",
                              edits=[{"old_text": "x", "new_text": "y"}])
    assert count.value.code == "content_mismatch" and "expected_occurrences" in count.value.message
    server.cap.filesystem("multi_edit", "a.txt",
                          edits=[{"old_text": "x", "new_text": "y", "expected_occurrences": 2}])
    assert (tmp_path / "a.txt").read_text() == "y y\n"
    # Edits apply in order against the evolving content.
    (tmp_path / "b.txt").write_text("one two\n")
    server.cap.filesystem("multi_edit", "b.txt", edits=[
        {"old_text": "one", "new_text": "1"},
        {"old_text": "two", "new_text": "2"},
    ])
    assert (tmp_path / "b.txt").read_text() == "1 2\n"


def test_multi_edit_edits_one_file_per_call_in_batch(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("old one and old two\n")
    out = server.call("batch", {"calls": [
        {"tool": "filesystem", "arguments": {"action": "multi_edit", "path": "a.txt",
            "edits": [{"old_text": "old one", "new_text": "new one"},
                      {"old_text": "old two", "new_text": "new two"}]}},
    ]})
    assert "result" in out["results"][0]
    assert (tmp_path / "a.txt").read_text() == "new one and new two\n"


def test_argument_aliases_renamed_before_dispatch(tmp_path):
    server = make(tmp_path)
    server.call("filesystem", {"action": "write", "file": "a.txt", "text": "hello world"})
    assert (tmp_path / "a.txt").read_text() == "hello world"
    server.call("filesystem", {"action": "replace_text", "file": "a.txt",
                               "old": "hello", "replacement": "hi"})
    assert (tmp_path / "a.txt").read_text() == "hi world"
    found = server.call("search", {"pattern": "hi", "path": str(tmp_path)})
    assert found["results"][0]["matches"][0]["text"] == "hi world"
    run = server.call("execute", {"cmd": [sys.executable, "-c", "print('aliased')"]})
    assert run["success"] and "aliased" in run["stdout"]
    info = server.call("workspace", {"action": "get", "dir": str(tmp_path)})
    assert info["cwd"] == str(tmp_path)


def test_aliases_never_override_canonical_names(tmp_path):
    server = make(tmp_path)
    server.cap.filesystem("write", "a.txt", "one two")
    server.call("filesystem", {"action": "replace_text", "path": "a.txt",
                               "file": "b.txt", "old_text": "one", "new_text": "1",
                               "old": "two", "new": "2"})
    # Canonical path/old_text/new_text win; the aliases are ignored.
    assert (tmp_path / "a.txt").read_text() == "1 two"
    assert not (tmp_path / "b.txt").exists()


def test_multi_search_entries_accept_aliases(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\n")
    out = server.call("multi_search", {"searches": [{"pattern": "alpha", "label": "p"}]})
    assert out["results"][0]["results"][0]["matches"][0]["text"] == "alpha"


def test_todo_lifecycle_and_persistence(tmp_path):
    import json
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"workspace_root": str(tmp_path),
        "allowed_read_roots": [str(tmp_path)], "allowed_write_roots": [str(tmp_path)]}))
    first = Server(Config.load(str(cfg_file)))
    assert first.call("todo", {"action": "get"})["todos"] == []
    added = first.call("todo", {"action": "add", "title": "Fix login"})
    assert added["id"] == 1 and added["status"] == "pending"
    first.call("todo", {"action": "add", "title": "Write tests"})
    first.call("todo", {"action": "update", "id": 1, "status": "in_progress"})
    listed = first.call("todo", {"action": "get"})["todos"]
    assert [t["id"] for t in listed] == [1, 2]
    assert listed[0]["status"] == "in_progress"
    second = Server(Config.load(str(cfg_file)))
    restored = second.call("todo", {"action": "get"})["todos"]
    assert [t["title"] for t in restored] == ["Fix login", "Write tests"]
    renamed = second.call("todo", {"action": "update", "id": 2, "title": "More tests"})
    assert renamed["title"] == "More tests"
    deleted = second.call("todo", {"action": "delete", "id": 1})
    assert deleted["deleted"] is True
    cleared = second.call("todo", {"action": "clear"})
    assert cleared["cleared"] == 1
    assert second.call("todo", {"action": "get"})["todos"] == []


def test_todo_validation_errors(tmp_path):
    server = make(tmp_path)
    with pytest.raises(RuntimeFault) as blank:
        server.call("todo", {"action": "add", "title": "   "})
    assert blank.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as missing:
        server.call("todo", {"action": "add"})
    assert missing.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as unknown:
        server.call("todo", {"action": "update", "id": 99, "status": "completed"})
    assert unknown.value.code == "todo_not_found"
    with pytest.raises(RuntimeFault) as bad:
        server.call("todo", {"action": "update", "id": 1, "status": "done"})
    assert bad.value.code == "invalid_arguments"
    with pytest.raises(RuntimeFault) as act:
        server.call("todo", {"action": "explode"})
    assert act.value.code == "invalid_action"


def test_todo_schema_steers_task_tracking(tmp_path):
    tools = {t["name"]: t for t in make(tmp_path).tools()}
    props = tools["todo"]["inputSchema"]["properties"]
    assert set(props["action"]["enum"]) == {"get", "add", "update", "delete", "clear"}
    assert set(props["status"]["enum"]) == {"pending", "in_progress", "completed"}
    assert "restarts" in tools["todo"]["description"]


def test_tools_call_structured_content_is_opt_in(tmp_path):
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "workspace", "arguments": {"action": "get"}}}
    plain = make(tmp_path).handle(call)["result"]
    assert "structuredContent" not in plain
    text = plain["content"][0]["text"]
    assert json.loads(text)["workspace_root"] == str(tmp_path) and ", " not in text
    result = make(tmp_path, structured_content=True).handle(call)["result"]
    assert result["structuredContent"]["workspace_root"] == str(tmp_path)
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_client_logging_notifications(tmp_path):
    server = make(tmp_path, log_to_client=True)
    messages = []
    server._client_log = lambda level, message: messages.append((level, message))
    server.call("workspace", {"action": "get"})
    assert len(messages) == 1
    level, message = messages[0]
    assert level == "info" and "tool=workspace" in message and "ok" in message
    assert make(tmp_path).cfg.log_to_client is False


def test_workspace_get_caches_git_root(tmp_path, monkeypatch):
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    server.cap.execute(["git", "init"])
    calls = []
    real_run = subprocess.run
    def counting(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", counting)
    first = server.cap.workspace("get")
    second = server.cap.workspace("get")
    assert first["repository_root"] == second["repository_root"] is not None
    assert len([c for c in calls if "rev-parse" in c[0]]) == 1


def test_replace_text_and_multi_edit_size_guard(tmp_path):
    server = make(tmp_path, max_file_read_bytes=10)
    (tmp_path / "big.txt").write_text("x" * 100)
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "big.txt", old_text="x", new_text="y")
    assert exc.value.code == "file_too_large" and "max_file_read_bytes" in exc.value.message
    assert (tmp_path / "big.txt").read_text() == "x" * 100
    with pytest.raises(RuntimeFault) as exc2:
        server.cap.filesystem("multi_edit", "big.txt",
                              edits=[{"old_text": "x", "new_text": "y"}])
    assert exc2.value.code == "file_too_large"


def test_path_locks_are_bounded_stripes(tmp_path):
    server = make(tmp_path)
    assert len(server.cap._path_locks) == 256
    for i in range(300):
        with server.cap._locked_paths([tmp_path / f"f{i}.txt"]):
            pass
    assert len(server.cap._path_locks) == 256


def test_guarded_write_completes_fails_and_times_out():
    from agent_runtime.server import _guarded_write
    assert _guarded_write(lambda: None, 5) is True
    def _broken():
        raise BrokenPipeError("pipe gone")
    assert _guarded_write(_broken, 5) is False
    import time
    def _stuck():
        time.sleep(5)
    assert _guarded_write(_stuck, 0.1) is False


def test_run_stdio_bounds_pending_requests(tmp_path, monkeypatch):
    import io
    from agent_runtime import server as server_module
    monkeypatch.setattr(server_module, "MAX_PENDING_REQUESTS", 2)
    server = make(tmp_path)
    slow = [sys.executable, "-c", "import time; time.sleep(1); print('done')"]
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                          "params": {"name": "execute", "arguments": {"command": slow}}})
              for i in range(5)]

    class _FakeStdin:
        def __init__(self, data):
            self.buffer = io.BytesIO(data)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(("\n".join(lines) + "\n").encode()))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    server.run_stdio()
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(responses) == 5
    rejected = [r for r in responses if r.get("error", {}).get("code") == -32603]
    accepted = [r for r in responses if "result" in r]
    assert len(rejected) == 3 and len(accepted) == 2
    assert all("overloaded" in r["error"]["message"] for r in rejected)


def test_process_write_times_out_on_full_stdin(tmp_path, monkeypatch):
    from agent_runtime import processes as processes_module
    monkeypatch.setattr(processes_module, "STDIN_WRITE_TIMEOUT_SECONDS", 0.5)
    server = make(tmp_path)
    child = [sys.executable, "-c", "import time; time.sleep(30)"]
    server.processes.start(child, process_id="stdin-hog")
    try:
        with pytest.raises(RuntimeFault) as exc:
            server.processes.write("stdin-hog", "x" * 1_000_000)
        assert exc.value.code == "stdin_blocked"
    finally:
        server.processes.stop("stdin-hog", True)


def test_job_adopt_is_best_effort():
    import subprocess
    from agent_runtime import job
    if os.name != "nt":
        job.adopt(0)
        return
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    try:
        job.adopt(proc.pid)
        # The job must really be created, not silently given up on.
        assert job._job is not None and not job._job_failed
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_adopted_child_dies_when_server_is_killed():
    import ctypes
    import subprocess
    # A stand-in server: adopts a long sleeper, prints its pid, then waits.
    helper = ("import subprocess, sys, time\n"
              "from agent_runtime import job\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
              "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
              "kill_on_close = job.WindowsJob()\n"
              "print(child.pid, kill_on_close.assign(child.pid), flush=True)\n"
              "time.sleep(60)\n")
    server = subprocess.Popen([sys.executable, "-c", helper], stdout=subprocess.PIPE, text=True,
                              cwd=str(Path(__file__).resolve().parent.parent))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle = None
    try:
        line = server.stdout.readline().split()
        assert len(line) == 2, "stand-in server died before adopting its child"
        pid, assigned = line
        # SYNCHRONIZE | PROCESS_TERMINATE: wait on it, and clean up if the test fails.
        handle = kernel32.OpenProcess(0x00100001, False, int(pid))
        assert handle
        assert assigned == "True", "child was not assigned to the job"
        server.kill()  # hard kill, like taskkill /F or a crash: no atexit
        server.wait()
        assert kernel32.WaitForSingleObject(handle, 10_000) == 0, "adopted child outlived the server"
    finally:
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
        if server.poll() is None:
            server.kill()
            server.wait()
        server.stdout.close()


def test_kill_children_on_exit_gates_adoption(tmp_path):
    server = make(tmp_path, kill_children_on_exit=False)
    assert server.cfg.kill_children_on_exit is False
    out = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert out["success"] and "ok" in out["stdout"]


def test_edits_preserve_line_endings(tmp_path):
    server = make(tmp_path)
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(b"one\ntwo\nthree\n")
    crlf.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = server.cap.filesystem("replace_text", "lf.txt", old_text="two", new_text="TWO")
    assert lf.read_bytes() == b"one\nTWO\nthree\n"
    assert out["bytes"] == len(lf.read_bytes())
    # old_text copied from a CRLF byte read still matches.
    server.cap.filesystem("multi_edit", "crlf.txt",
                          edits=[{"old_text": "one\r\ntwo", "new_text": "1\n2"}])
    assert crlf.read_bytes() == b"1\r\n2\r\nthree\r\n"
    with pytest.raises(RuntimeFault) as same:
        server.cap.filesystem("replace_text", "crlf.txt", old_text="1\r\n", new_text="1\n")
    assert same.value.code == "invalid_arguments"
    out = server.cap.filesystem("write", "crlf.txt", "a\nb\n")
    assert crlf.read_bytes() == b"a\r\nb\r\n" and out["bytes"] == 6
    out = server.cap.filesystem("write", "new.txt", "x\ny\n")
    assert (tmp_path / "new.txt").read_bytes() == b"x\ny\n" and out["bytes"] == 4
    diff = "--- a/lf.txt\n+++ b/lf.txt\n@@ -1,3 +1,3 @@\n one\n-TWO\n+two\n three\n"
    out = server.cap.filesystem("apply_patch", ".", patch=diff)
    assert lf.read_bytes() == b"one\ntwo\nthree\n"
    assert out["files"][0]["bytes"] == len(lf.read_bytes())
    diff = "--- a/crlf.txt\n+++ b/crlf.txt\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert crlf.read_bytes() == b"A\r\nb\r\n"


def test_eol_detection_edge_cases(tmp_path):
    server = make(tmp_path)
    wide = tmp_path / "wide.txt"
    wide.write_bytes("one\r\ntwo\r\n".encode("utf-16"))
    server.cap.filesystem("replace_text", "wide.txt", old_text="two", new_text="TWO", encoding="utf-16")
    assert wide.read_bytes().decode("utf-16") == "one\r\nTWO\r\n"
    out = server.cap.filesystem("write", "wide.txt", "a\nb\n", encoding="utf-16")
    assert wide.read_bytes().decode("utf-16") == "a\r\nb\r\n" and out["eol"] == "CRLF"
    # No line break yet: nothing to preserve, so content is written as sent.
    single = tmp_path / "single.txt"
    single.write_bytes(b"solo")
    out = server.cap.filesystem("write", "single.txt", "a\r\nb")
    assert single.read_bytes() == b"a\r\nb" and "eol" not in out
    (tmp_path / "empty.txt").write_text("")
    assert "the file has 0 lines" in server.cap.filesystem("read", "empty.txt", line_start=2)["note"]


def test_edits_keep_untouched_line_bytes(tmp_path):
    server = make(tmp_path)
    mixed = tmp_path / "mixed.txt"
    mixed.write_bytes(b"one\ntwo\r\nthree\r\nfour\r\n")
    server.cap.filesystem("replace_text", "mixed.txt", old_text="four", new_text="4")
    assert mixed.read_bytes() == b"one\ntwo\r\nthree\r\n4\r\n"
    stray = tmp_path / "stray.txt"
    stray.write_bytes(b"a\r\r\nb\r\n")
    server.cap.filesystem("multi_edit", "stray.txt", edits=[{"old_text": "b", "new_text": "B"}])
    assert stray.read_bytes() == b"a\r\r\nB\r\n"
    # An edit spanning a minority line ending falls back to normalized text.
    mixed.write_bytes(b"a\r\nb\nc\r\nd\r\n")
    server.cap.filesystem("replace_text", "mixed.txt", old_text="b\nc", new_text="B\nC")
    assert mixed.read_bytes() == b"a\r\nB\r\nC\r\nd\r\n"
    mixed.write_bytes(b"one\ntwo\r\nthree\r\nfour\r\n")
    diff = "--- a/mixed.txt\n+++ b/mixed.txt\n@@ -2,3 +2,3 @@\n two\n-three\n+THREE\n four\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert mixed.read_bytes() == b"one\ntwo\r\nTHREE\r\nfour\r\n"
    feed = tmp_path / "feed.txt"
    feed.write_bytes(b"a\x0cb\nc\n")
    diff = "--- a/feed.txt\n+++ b/feed.txt\n@@ -1,2 +1,2 @@\n a\x0cb\n-c\n+C\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert feed.read_bytes() == b"a\x0cb\nC\n"
    tail = tmp_path / "tail.txt"
    tail.write_bytes(b"one\ntwo")
    diff = "--- a/tail.txt\n+++ b/tail.txt\n@@ -1,2 +1,3 @@\n one\n two\n+three\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert tail.read_bytes() == b"one\ntwo\nthree\n"


def test_read_lines_bounded_and_lf_only(tmp_path):
    server = make(tmp_path, max_read_chars=64)
    (tmp_path / "cr.txt").write_bytes(b"a\rb\nc\n")
    out = server.cap.filesystem("read", "cr.txt", line_start=1)
    assert out["content"] == "1\ta\rb\n2\tc" and out["total_lines"] == 2
    (tmp_path / "huge.txt").write_bytes(b"y" * 1_000_000 + b"\nafter\n")
    first = server.cap.filesystem("read", "huge.txt", line_start=1, line_end=1)
    assert first["content"].endswith("...[line cut]") and len(first["content"]) <= 64
    assert first["truncated"] and first["next_line"] == 2 and "total_lines" not in first
    second = server.cap.filesystem("read", "huge.txt", line_start=2)
    assert second["content"] == "2\tafter" and second["total_lines"] == 2


def test_read_lines_past_the_end_shows_the_last_lines(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("".join(f"row {n}\n" for n in range(1, 151)))
    out = server.cap.filesystem("read", "a.txt", line_start=200)
    assert "line_start 200 is past the end; the file has 150 lines" in out["note"]
    shown = out["content"].split("\n")
    assert len(shown) == 100 and shown[0] == "51\trow 51" and shown[-1] == "150\trow 150"
    assert out["line_start"] == 51 and out["line_end"] == 150 and out["total_lines"] == 150
    assert out["truncated"] is False and "next_line" not in out
    # Still within max_read_chars, keeping the last lines.
    small = make(tmp_path, max_read_chars=64)
    tail = small.cap.filesystem("read", "a.txt", line_start=151)
    assert len(tail["content"]) <= 64 and tail["content"].endswith("149\trow 149\n150\trow 150")
    (tmp_path / "empty.txt").write_text("")
    empty = server.cap.filesystem("read", "empty.txt", line_start=5)
    assert empty["content"] == "" and empty["total_lines"] == 0
    assert "line_start 5 is past the end; the file has 0 lines" in empty["note"]


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_context_near_matches_and_lone_cr(tmp_path, monkeypatch, engine):
    if engine == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"x\ry\nhit\nhit\nz\n")
    out = server.cap.search("hit", context_lines=1)
    assert out["engine"] == engine
    [hits] = out["results"]
    first, second = hits["matches"]
    assert first["line"] == 2 and first["before"] == ["1\tx\ry"] and first["after"] == ["3\thit"]
    assert second["line"] == 3 and second["before"] == ["2\thit"] and second["after"] == ["4\tz"]


def test_edits_count_matches_in_lf_view(tmp_path):
    server = make(tmp_path)
    # Two matches in the LF view the agent reads; only one has the
    # dominant ending. Must be a mismatch, not a silent edit of one.
    twice = tmp_path / "twice.txt"
    twice.write_bytes(b"x\r\ny\r\nz\r\nx\ny\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "twice.txt", old_text="x\ny", new_text="X\nY")
    assert exc.value.code == "content_mismatch" and "found 2" in exc.value.as_dict()["message"]
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("multi_edit", "twice.txt", edits=[{"old_text": "x\ny", "new_text": "X\nY"}])
    assert exc.value.code == "content_mismatch"
    assert twice.read_bytes() == b"x\r\ny\r\nz\r\nx\ny\n"
    # Matching half of a CRLF pair must not leave a stray CR.
    split = tmp_path / "split.txt"
    split.write_bytes(b"a\nfoo\r\nbar\nbaz\n")
    server.cap.filesystem("replace_text", "split.txt", old_text="\nbar", new_text=" bar")
    assert split.read_bytes() == b"a\nfoo bar\nbaz\n"
    cr = tmp_path / "cr.txt"
    cr.write_bytes(b"foo\r\nbar\r\n")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("replace_text", "cr.txt", old_text="foo\r", new_text="FOO")
    assert cr.read_bytes() == b"foo\r\nbar\r\n"


def test_apply_patch_zero_context_insert(tmp_path):
    server = make(tmp_path)
    (tmp_path / "f.txt").write_bytes(b"1\n2\n3\n")
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -2,0 +3 @@\n+X\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert (tmp_path / "f.txt").read_bytes() == b"1\n2\nX\n3\n"
    (tmp_path / "g.txt").write_bytes(b"1\n2\n3")
    diff = "--- a/g.txt\n+++ b/g.txt\n@@ -3,0 +4 @@\n+X\n"
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert (tmp_path / "g.txt").read_bytes() == b"1\n2\n3\nX\n"


def test_apply_patch_places_hunks_by_content(tmp_path):
    server = make(tmp_path)
    body = "".join(f"line {n}\n" for n in range(1, 21))
    (tmp_path / "f.txt").write_text(body)
    # git headers, line numbers 2 and 5 lines off, and counts that do not match the bodies.
    diff = ("diff --git a/f.txt b/f.txt\nindex 1111111..2222222 100644\n--- a/f.txt\n+++ b/f.txt\n"
            "@@ -5,9 +5,9 @@\n line 7\n-line 8\n+LINE 8\n line 9\n"
            "@@ -10,1 +10,7 @@\n line 15\n-line 16\n+LINE 16\n line 17\n")
    server.cap.filesystem("apply_patch", ".", patch=diff)
    assert (tmp_path / "f.txt").read_text() == body.replace("line 8\n", "LINE 8\n").replace("line 16\n", "LINE 16\n")
    created = ("diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..3333333\n"
               "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,5 @@\n+hello\n")
    server.cap.filesystem("apply_patch", ".", patch=created)
    assert (tmp_path / "new.txt").read_text() == "hello\n"
    # A removed line starting "-- " is not the next file's header.
    (tmp_path / "sql.txt").write_text("-- note\nkeep\n")
    server.cap.filesystem("apply_patch", ".", patch="--- a/sql.txt\n+++ b/sql.txt\n@@ -1,2 +1,1 @@\n--- note\n keep\n")
    assert (tmp_path / "sql.txt").read_text() == "keep\n"


def test_apply_patch_refuses_ambiguous_places_and_renames(tmp_path):
    server = make(tmp_path)
    (tmp_path / "amb.txt").write_text("x\nb\nm\nm\nx\nb\n")
    # Stated at line 3: the matches at lines 1 and 5 are equally near.
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("apply_patch", ".", patch="--- a/amb.txt\n+++ b/amb.txt\n@@ -3,2 +3,2 @@\n x\n-b\n+B\n")
    assert exc.value.code == "content_mismatch" and "lines 1 and 5" in exc.value.message
    assert (tmp_path / "amb.txt").read_text() == "x\nb\nm\nm\nx\nb\n"
    # Hunks apply in order: the second may not land before the first.
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("apply_patch", ".", patch=(
            "--- a/amb.txt\n+++ b/amb.txt\n@@ -5,2 +5,2 @@\n x\n-b\n+B\n@@ -1,2 +1,2 @@\n m\n-m\n+M\n"))
    (tmp_path / "old.txt").write_text("x\n")
    renames = ["diff --git a/old.txt b/new.txt\nsimilarity index 90%\nrename from old.txt\nrename to new.txt\n"
               "--- a/old.txt\n+++ b/new.txt\n@@ -1 +1 @@\n-x\n+y\n",
               "--- a/old.txt\n+++ b/new.txt\n@@ -1 +1 @@\n-x\n+y\n"]
    for diff in renames:
        with pytest.raises(RuntimeFault) as exc:
            server.cap.filesystem("apply_patch", ".", patch=diff)
        assert exc.value.code == "invalid_arguments" and "renames old.txt to new.txt" in exc.value.message
    assert (tmp_path / "old.txt").read_text() == "x\n" and not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_folder_glob_and_exact_truncation(tmp_path, monkeypatch, engine):
    if engine == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    for folder in ("src", "other"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "a.py").write_text("hit\nhit\n")
    # Run from a different directory than the search root, as the server
    # does, so a glob with a folder must still match the root-relative path.
    monkeypatch.chdir(tmp_path / "other")
    found = server.cap.search("hit", glob=["src/*.py"])
    assert found["engine"] == engine
    assert {r["path"] for r in found["results"]} == {os.path.join("src", "a.py")}
    files = server.cap.search(files_only=True, glob=["src/*.py"])
    assert [r["path"] for r in files["results"]] == [os.path.join("src", "a.py")]
    exact = server.cap.search("hit", path="src", max_results=2)
    assert exact["match_count"] == 2 and exact["truncated"] is False
    over = server.cap.search("hit", path="src", max_results=1)
    assert over["match_count"] == 1 and over["truncated"] is True
    one_file = server.cap.search("hit", path="src/a.py", max_results=2)
    assert one_file["match_count"] == 2 and one_file["truncated"] is False
    one_over = server.cap.search("hit", path="src/a.py", max_results=1)
    assert one_over["match_count"] == 1 and one_over["truncated"] is True
    all_files = server.cap.search(files_only=True, max_results=2)
    assert len(all_files["results"]) == 2 and all_files["truncated"] is False
    cut_files = server.cap.search(files_only=True, max_results=1)
    assert len(cut_files["results"]) == 1 and cut_files["truncated"] is True


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_globs_follow_ripgrep_rules(tmp_path, monkeypatch, engine):
    # Expectations recorded from ripgrep 15.1.0 run from the search root;
    # the Python fallback must give the same answers.
    if engine == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    for rel in ("src/a.py", "src/deep/a.py", "src/deep/er/y.cs", "a/src/x.py", "scripts/build",
                "pkg/node_modules/x.js", "pkg/build/out.txt", "pkg/dist/b.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("hit\n")

    def files(**kwargs):
        out = server.cap.search(files_only=True, **kwargs)
        assert out["engine"] == engine
        return sorted(Path(r["path"]).as_posix() for r in out["results"])

    # Default excludes apply at any depth; a file named like one is kept.
    assert files() == ["a/src/x.py", "scripts/build", "src/a.py", "src/deep/a.py", "src/deep/er/y.cs"]
    # A glob with a folder is anchored to the search root.
    assert files(glob=["src/*.py"]) == ["src/a.py"]
    assert files(glob=["/src/*.py"]) == ["src/a.py"]
    # ** spans any number of folders.
    assert files(glob=["src/**/*.cs"]) == ["src/deep/er/y.cs"]
    assert files(glob=["src/**"]) == ["src/a.py", "src/deep/a.py", "src/deep/er/y.cs"]
    # Case-sensitive, braces alternate, a bare name matches at any depth.
    assert files(glob=["*.PY"]) == []
    assert files(glob=["*.{py,cs}"]) == ["a/src/x.py", "src/a.py", "src/deep/a.py", "src/deep/er/y.cs"]
    assert files(glob=["build"]) == ["scripts/build"]
    # Excludes follow .gitignore: a bare name hides matching folders at any depth.
    assert files(exclude=["src"]) == ["scripts/build"]
    assert files(exclude=["a/**"]) == ["scripts/build", "src/a.py", "src/deep/a.py", "src/deep/er/y.cs"]
    # A single-file search reads only that file, not same-named files below it.
    one = server.cap.search("hit", path="src/a.py")
    assert [Path(r["path"]).as_posix() for r in one["results"]] == ["src/a.py"]
    # An explicit file path ignores excludes, as in ripgrep.
    kept = server.cap.search("hit", path="src/a.py", exclude=["*.py"])
    assert [Path(r["path"]).as_posix() for r in kept["results"]] == ["src/a.py"]
    defaults = ["a/src/x.py", "scripts/build", "src/a.py", "src/deep/a.py", "src/deep/er/y.cs"]
    # A trailing slash means a folder: the file scripts/build stays.
    assert files(exclude=["build/"]) == defaults
    assert files(exclude=["build"]) == [p for p in defaults if p != "scripts/build"]
    # A folder-only include matches no file; an empty glob is ignored.
    assert files(glob=["src/"]) == []
    assert files(glob=[""]) == defaults
    # A slash anywhere in a brace pattern anchors every alternative.
    assert files(glob=["{src/*.py,*.cs}"]) == ["src/a.py"]
    # A backslash escapes the next character; it is not a separator.
    assert files(glob=["src\\*.py"]) == []
    assert files(glob=["*\\.py"]) == ["a/src/x.py", "src/a.py", "src/deep/a.py"]
    # A class starting with ! may hold ]: [!]x] is any character but ] and x.
    assert files(glob=["[!]x]*.py"]) == ["src/a.py", "src/deep/a.py"]
    # A folder-only exclude ending in ** hides only folders below it.
    assert files(exclude=["src/**/"]) == ["a/src/x.py", "scripts/build", "src/a.py"]
    assert files(exclude=["**/"]) == []
    # Slashes alone: as an include nothing matches, as an exclude it is ignored.
    assert files(glob=["/"]) == []
    assert files(exclude=["/"]) == defaults
    # A class can match "/" when its range covers it.
    assert files(glob=["*/*[ -~]*"]) == ["a/src/x.py", "scripts/build", "src/a.py", "src/deep/a.py"]
    # A range followed by -X is extended: [a-c-z] is a to z, so y.cs matches.
    assert files(glob=["[a-c-z]*.cs"]) == ["src/deep/er/y.cs"]


def test_python_glob_fallback_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    (tmp_path / ("a" * 40 + ".txt")).write_text("hit\n")
    deep = tmp_path.joinpath(*["x"] * 30)
    deep.mkdir(parents=True)
    (deep / "z.txt").write_text("hit\n")
    started = time.monotonic()
    for glob in ("*a*a*a*a*a*a*a*a*a*a*b", "**/x/**/x/**/x/**/x/**/x/**/y", "a********b", "**/" * 10 + "y"):
        assert server.cap.search(files_only=True, glob=[glob])["results"] == []
    assert time.monotonic() - started < 2
    with pytest.raises(RuntimeFault) as exc:
        server.cap.search(files_only=True, glob=["{a,b}" * 8])
    assert exc.value.code == "invalid_arguments"
    # Many single-choice groups expand once and must not recurse.
    assert server.cap.search(files_only=True, glob=["{a}" * 2000])["results"] == []


def test_glob_atomic_form_matches_plain_translation():
    import re
    from agent_runtime.capabilities import _glob_body, _glob_tokens
    # Includes a negated class, which can match "/" and so must not be
    # folded into an atomic chunk (found by fuzzing).
    cases = [("a**[!a]b*[!a]", "abbbb/b/"), ("*a*b*", "xaybz"), ("**/x/**/y", "q/x/r/x/y"),
             ("*[ab]?*c", "zbqqc"), ("**/a/b/**/c", "a/b/a/b/c"), ("x/*[ -~]*", "x/a/b"),
             ("*[+-0]*", "-a/b"), ("**/**/b", "a/b"), ("a/**/**", "a/b/c")]
    for glob, path in cases:
        plain = re.compile("".join(rx for _, rx in _glob_tokens(glob)))
        fast = re.compile(_glob_body(glob))
        assert bool(plain.fullmatch(path)) == bool(fast.fullmatch(path)), (glob, path)
        assert plain.fullmatch(path), (glob, path)
    # Set-operation lookalikes in a class are literal, with no FutureWarning.
    import warnings
    from agent_runtime.capabilities import _glob_regex
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _glob_regex("[a&&b|~]").fullmatch("&")
        # A dash next to a dash still reads as a range: [+--] is + to -,
        # [--0] is - to 0 (so it covers "." and "/").
        plus_dash = _glob_regex("[+--]")
        assert plus_dash.fullmatch(",") and not plus_dash.fullmatch(".")
        dash_zero = _glob_regex("[--0]")
        assert dash_zero.fullmatch(".") and not dash_zero.fullmatch("1")
        assert _glob_regex("[a-]").fullmatch("-") and _glob_regex("[-a]").fullmatch("-")
    with pytest.raises(RuntimeFault) as exc:
        _glob_regex("[z-a]")
    assert exc.value.code == "invalid_arguments"


def test_glob_regex_rules():
    from agent_runtime.capabilities import _glob_regex
    # A folder-only exclude ending in ** hides what is below a folder,
    # never a top-level file.
    assert _glob_regex("**/").fullmatch("f.txt") is None
    assert _glob_regex("**/").fullmatch("a/f.txt")
    # ripgrep extends a range by a following -X ([a-c-e] is a to e) and
    # rejects [a-b--c] as the range a to "-" (checked with ripgrep 15.1.0).
    chained = _glob_regex("[a-c-e]")
    assert chained.fullmatch("d") and not chained.fullmatch("-")
    assert chained.fullmatch("e") and not chained.fullmatch("f")
    # Each step replaces the end and is checked against the start, so the
    # range can narrow ([a-c-b] is a to b) and a reversed step is an error
    # even when a later end is valid ([b-a-c]); both checked with ripgrep.
    narrowed = _glob_regex("[a-c-b]")
    assert narrowed.fullmatch("b") and not narrowed.fullmatch("c")
    for bad in ("[b-a-c]", "[z-a-z]", "[.-+-z]"):
        with pytest.raises(RuntimeFault) as exc:
            _glob_regex(bad)
        assert exc.value.code == "invalid_arguments", bad
    with pytest.raises(RuntimeFault) as exc:
        _glob_regex("[a-b--c]")
    assert exc.value.code == "invalid_arguments"
    # A dash with nothing after it stays literal.
    assert _glob_regex("[a-c-]").fullmatch("-")


def test_config_rejects_tiny_max_read_chars(tmp_path):
    path = tmp_path / "lf.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "max_read_chars": 20}))
    with pytest.raises(ValueError, match="at least 64"):
        Config.load(str(path))


def test_execute_clip_keeps_head_and_tail(tmp_path):
    server = make(tmp_path, max_capture_bytes=1000)
    out = server.cap.execute([sys.executable, "-c", "print('HEAD' + 'x' * 5000 + 'TAIL')"])
    assert out["stdout_truncated"] and len(out["stdout"].encode("utf-8")) <= 1000
    assert out["stdout"].startswith("HEAD") and out["stdout"].rstrip().endswith("TAIL")
    assert "bytes cut]" in out["stdout"]


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_context_numbered_and_per_file(tmp_path, monkeypatch, engine):
    if engine == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("a1\na2\nhit\n")
    (tmp_path / "b.txt").write_text("hit\nb2\nb3\n")
    out = server.cap.search("hit", context_lines=4)
    assert out["engine"] == engine
    by_path = {r["path"]: r["matches"][0] for r in out["results"]}
    assert by_path["a.txt"]["before"] == ["1\ta1", "2\ta2"] and by_path["a.txt"]["after"] == []
    assert by_path["b.txt"]["before"] == [] and by_path["b.txt"]["after"] == ["2\tb2", "3\tb3"]
    # Outside the runtime cwd, paths stay absolute.
    (tmp_path / "sub").mkdir()
    server.cap.workspace("set_cwd", "sub")
    assert all(Path(r["path"]).is_absolute() for r in server.cap.search("hit", path="..")["results"])


def test_commands_resolve_cwd_against_runtime_cwd(tmp_path):
    server = make(tmp_path)
    (tmp_path / "sub" / "inner").mkdir(parents=True)
    server.cap.workspace("set_cwd", "sub")
    code = [sys.executable, "-c", "import os; print(os.getcwd())"]
    pid = server.call("process", {"action": "start", "command": code})["process_id"]
    try:
        out = server.processes.read(pid, wait_ms=5000)
        printed = out["stdout"].strip()
        assert printed.endswith("sub") and out["cwd"].endswith("sub")
    finally:
        server.processes.stop(pid, True)
    ran = server.cap.execute(code, cwd="inner")
    assert ran["stdout"].strip().endswith(os.path.join("sub", "inner"))
    assert server.cap.git("root", cwd="inner")["cwd"].endswith(os.path.join("sub", "inner"))


def test_execute_saves_cut_output_to_readable_log(tmp_path):
    # Outside the read roots on purpose: the spill folder is
    # added to them so the agent can read the log back.
    spill = tmp_path.parent / (tmp_path.name + "-spill")
    server = make(tmp_path, max_inline_output_bytes=1000, spill_dir=str(spill))
    big = server.cap.execute([sys.executable, "-c", "print('a' * 3000 + 'END')"])
    assert big["stdout_truncated"] and len(big["stdout"].encode()) <= 1000
    assert big["stdout"].rstrip().endswith("END")
    saved = Path(big["stdout_path"])
    assert saved.parent == spill.resolve()
    assert saved.read_text(encoding="utf-8").strip() == "a" * 3000 + "END"
    assert "END" in server.cap.filesystem("read", str(saved))["content"]
    small = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert not small["stdout_truncated"] and "stdout_path" not in small


def test_spill_folder_is_pruned(tmp_path, monkeypatch):
    import agent_runtime.capabilities as caps
    monkeypatch.setattr(caps, "SPILL_KEEP", 3)
    server = make(tmp_path, spill_dir=str(tmp_path / "spill"))
    (tmp_path / "spill" / "user-file.txt").write_text("keep")
    paths = []
    for n in range(5):
        paths.append(server.cap.spill("t", str(n)))
        os.utime(paths[-1], (n + 1, n + 1))
    assert sorted(os.listdir(tmp_path / "spill")) == sorted(
        ["user-file.txt"] + [os.path.basename(p) for p in paths[2:]])


def test_oversized_result_is_saved_with_preview(tmp_path):
    server = make(tmp_path, max_result_bytes=5000, spill_dir=str(tmp_path / "spill"))
    (tmp_path / "big.txt").write_text("x" * 20000)

    def call(arguments):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "filesystem", "arguments": arguments}})
        return response["result"]["content"][0]["text"]

    text = call({"action": "read", "path": "big.txt"})
    assert len(text.encode()) <= 5000
    value = json.loads(text)
    # Shape change on purpose: a dict result keeps its own fields, cut, instead of a raw preview.
    assert value["result_too_large"] and value["bytes"] > 20000 and "preview" not in value
    assert value["content"].startswith("x" * 100) and "[cut: showing" in value["content"]
    saved = json.loads(Path(value["result_path"]).read_text(encoding="utf-8"))
    assert saved["content"] == "x" * 20000
    assert "result_too_large" not in json.loads(call({"action": "stat", "path": "big.txt"}))
    # A batch is fitted as a whole.
    response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "batch", "arguments": {"calls": [{"tool": "filesystem", "arguments": {"action": "read", "path": "big.txt"}}] * 2}}})
    batch = json.loads(response["result"]["content"][0]["text"])
    assert batch["result_too_large"]
    assert len(json.loads(Path(batch["result_path"]).read_text(encoding="utf-8"))["results"]) == 2


def test_oversized_result_keeps_its_shape(tmp_path):
    server = make(tmp_path, max_result_bytes=8000, spill_dir=str(tmp_path / "spill"))
    body = "".join(f"line {n}\n" for n in range(1, 3001))
    (tmp_path / "big.txt").write_bytes(body.encode("utf-8"))

    def call(name, arguments):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
        return response["result"]["content"][0]["text"]

    text = call("filesystem", {"action": "read", "path": "big.txt"})
    assert len(text.encode("utf-8")) <= 8000 // 4
    value = json.loads(text)
    assert list(value)[:4] == ["result_too_large", "bytes", "result_path", "hint"]
    assert value["result_too_large"] is True and value["bytes"] > len(body)
    assert value["path"].endswith("big.txt") and "preview" not in value
    content = value["content"]
    assert content.startswith("line 1\nline 2\nline 3\n")
    head, marker = content.split("...[cut: ")
    assert head.endswith("\n") and head == body[:len(head)]
    assert marker == f"showing lines 1-{head.count(chr(10))} of 3000; full result in result_path]"
    assert json.loads(Path(value["result_path"]).read_text(encoding="utf-8"))["content"] == body

    text = call("batch", {"calls": [{"tool": "filesystem", "arguments": {"action": "read", "path": "big.txt"},
                                     "label": f"r{n}"} for n in range(2)]})
    assert len(text.encode("utf-8")) <= 8000 // 4
    batch = json.loads(text)
    assert batch["result_too_large"] and [entry["label"] for entry in batch["results"]] == ["r0", "r1"]
    assert all(entry["result"]["content"].startswith("line 1\n") for entry in batch["results"])
    assert len(json.loads(Path(batch["result_path"]).read_text(encoding="utf-8"))["results"]) == 2


def test_oversized_preview_trims_long_lists_and_falls_back(tmp_path):
    server = make(tmp_path, max_result_bytes=40000, spill_dir=str(tmp_path / "spill"))
    value = {"results": [{"path": f"f{n}.py", "line": n} for n in range(3000)], "name": "bad\udc80name"}
    fitted, text = server._fit_result("search", value, json.dumps(value, ensure_ascii=False))
    assert len(text.encode("utf-8")) <= 10000 and json.loads(text) == fitted
    assert fitted["results"][0] == {"path": "f0.py", "line": 0} and fitted["name"] == "bad?name"
    kept = len(fitted["results"]) - 1
    assert fitted["results"][-1] == f"...[cut: showing {kept} of 3000 items; full result in result_path]"
    # A single long line is cut by bytes.
    line = {"stdout": "y" * 50000}
    fitted, _ = server._fit_result("execute", line, json.dumps(line))
    assert fitted["stdout"].startswith("yyy") and fitted["stdout"].endswith(
        " of 50000 bytes; full result in result_path]")
    # Not a dict: today's raw-JSON preview.
    listed = ["z" * 50000]
    fitted, text = server._fit_result("x", listed, json.dumps(listed))
    assert fitted["preview"].startswith('["zzz') and len(text.encode("utf-8")) <= 40000


def test_process_read_pages_default_to_inline_cap(tmp_path):
    server = make(tmp_path, max_inline_output_bytes=1000)
    code = [sys.executable, "-c", "import sys\nfor _ in range(200): print('y' * 99, flush=True)"]
    pid = server.processes.start(code)["process_id"]
    try:
        deadline = time.monotonic() + 10
        while server.processes.status(pid)["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        page = server.processes.read(pid)
        explicit = server.processes.read(pid, limit_bytes=1000)
        whole = server.processes.read(pid, limit_bytes=200_000)
        assert page["truncated"] and page["next_after"] == explicit["next_after"] < whole["next_after"]
    finally:
        server.processes.stop(pid, True)


def test_edits_return_a_capped_diff(tmp_path):
    server = make(tmp_path)
    assert "diff" not in server.cap.filesystem("write", "d.txt", "a\nb\nc\n")
    replaced = server.cap.filesystem("replace_text", "d.txt", old_text="b", new_text="B")
    assert replaced["diff"] == "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    multi = server.cap.filesystem("multi_edit", "d.txt", edits=[{"old_text": "a", "new_text": "A"}])
    assert "-a\n+A\n" in multi["diff"]
    assert "diff" not in server.cap.filesystem("write", "d.txt", "A\nB\nc\n")
    # Only the ---/+++ header is dropped, not removed lines that start with --.
    server.cap.filesystem("write", "d.txt", "-- note\n")
    assert "--- note\n" in server.cap.filesystem("replace_text", "d.txt", old_text="-- note", new_text="x")["diff"]
    (tmp_path / "crlf.txt").write_bytes(b"x\r\ny\r\n")
    crlf = server.cap.filesystem("replace_text", "crlf.txt", old_text="y", new_text="z")
    assert "\r" not in crlf["diff"] and "-y\n+z\n" in crlf["diff"]
    small = make(tmp_path, edit_diff_max_bytes=10)
    cut = small.cap.filesystem("write", "d.txt", "1\n2\n3\n")
    assert cut["diff_truncated"] and len(cut["diff"].encode()) <= 10
    off = make(tmp_path, edit_diff_max_bytes=0)
    assert "diff" not in off.cap.filesystem("write", "d.txt", "q\n")


def test_edit_checks_run_on_matching_files(tmp_path):
    check = [sys.executable, "-c",
             "import sys\nif 'bad' in open(sys.argv[1]).read(): sys.exit('bad: ' + sys.argv[1])", "{path}"]
    server = make(tmp_path, edit_checks={"*.chk": check})
    assert server.cap.filesystem("write", "a.chk", "fine\n")["check"] == {"ok": True}
    bad = server.cap.filesystem("replace_text", "a.chk", old_text="fine", new_text="bad")
    assert bad["check"]["ok"] is False and bad["check"]["exit_code"] == 1
    assert "bad: " + str(tmp_path / "a.chk") in bad["check"]["output"]
    assert (tmp_path / "a.chk").read_text() == "bad\n"
    multi = server.cap.filesystem("multi_edit", "a.chk", edits=[{"old_text": "bad", "new_text": "ok"}])
    assert multi["check"] == {"ok": True}
    patched = server.cap.filesystem("apply_patch", ".", patch="--- /dev/null\n+++ b/new.chk\n@@ -0,0 +1 @@\n+bad\n")
    assert patched["files"][0]["check"]["ok"] is False
    assert "check" not in server.cap.filesystem("write", "plain.txt", "bad")
    # A mode without process execution reports the check as not run.
    no_exec = make(tmp_path, mode="WORKSPACE", edit_checks={"*.chk": check})
    denied = no_exec.cap.filesystem("write", "a.chk", "x")["check"]
    assert denied["ok"] is False and "execution" in denied["error"]


def test_config_rejects_bad_edit_checks(tmp_path):
    path = tmp_path / "lf.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "edit_checks": {"*.py": "python"}}))
    with pytest.raises(ValueError):
        Config.load(str(path))


def test_workspace_get_returns_project_instructions(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    assert "project_instructions" not in server.cap.workspace("get")
    server.cap.execute(["git", "init"])
    (tmp_path / "AGENTS.md").write_text("root rules")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "CLAUDE.md").write_text("sub rules")
    found = server.cap.workspace("set_cwd", "sub")["project_instructions"]
    assert [(Path(f["path"]).name, f["content"]) for f in found] == [("AGENTS.md", "root rules"), ("CLAUDE.md", "sub rules")]
    # One shared byte budget across files.
    small = make(tmp_path, max_project_instruction_bytes=4)
    small.cap.workspace("set_cwd", "sub")
    cut = small.cap.workspace("get")["project_instructions"]
    assert cut == [{"path": str((tmp_path / "AGENTS.md").resolve()), "content": "root", "truncated": True},
                   {"path": str((tmp_path / "sub" / "CLAUDE.md").resolve()), "skipped": "budget"}]
    denied = make(tmp_path, denied_paths=[str(tmp_path / "AGENTS.md")])
    assert "project_instructions" not in denied.cap.workspace("get")
    off = make(tmp_path, project_instruction_files=[])
    assert "project_instructions" not in off.cap.workspace("get")


def test_project_instructions_follow_the_folder_chain_and_switch(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    assert server.cap.execute(["git", "init", "-q"], cwd="repo")["success"]
    (tmp_path / "AGENTS.md").write_text("outside the repo")
    (tmp_path / "repo" / "AGENTS.md").write_text("root rules")
    (tmp_path / "repo" / "a" / "CLAUDE.md").write_text("a rules")
    (tmp_path / "repo" / "a" / "b" / "AGENTS.md").write_text("b rules")
    deep = str(tmp_path / "repo" / "a" / "b")
    # From the Git root down to the cwd, outermost first; nothing above the root.
    found = server.cap.workspace("set_cwd", deep)["project_instructions"]
    assert [entry["content"] for entry in found] == ["root rules", "a rules", "b rules"]
    # No Git root: the cwd and its parents up to workspace_root.
    (tmp_path / "plain" / "deep").mkdir(parents=True)
    (tmp_path / "plain" / "CLAUDE.md").write_text("plain rules")
    found = server.cap.workspace("set_cwd", str(tmp_path / "plain" / "deep"))["project_instructions"]
    assert [entry["content"] for entry in found] == ["outside the repo", "plain rules"]
    # One budget along the whole chain.
    small = make(tmp_path, max_project_instruction_bytes=12)
    cut = small.cap.workspace("set_cwd", deep)["project_instructions"]
    assert [entry.get("content") for entry in cut] == ["root rules", "a ", None]
    assert cut[1]["truncated"] and cut[2]["skipped"] == "budget"
    # Switched off: no instruction file is read at all, even when they exist.
    off = make(tmp_path, load_project_instructions=False)

    def boom(*args, **kwargs):
        raise AssertionError("instruction files were read")

    monkeypatch.setattr(off.cap, "_project_instructions", boom)
    assert "project_instructions" not in off.cap.workspace("set_cwd", deep)
    assert "project_instructions" not in off.cap.workspace("get")
    config = tmp_path / "lf.json"
    config.write_text(json.dumps({"workspace_root": str(tmp_path), "load_project_instructions": "no"}))
    with pytest.raises(ValueError):
        Config.load(str(config))


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell only")
def test_windows_powershell_curl_and_and_hint(tmp_path):
    server = make(tmp_path)
    server.cfg.default_shell = "powershell"
    # make() inherits only PATH; PowerShell needs the default set to start.
    server.cfg.inherit_environment = Config(str(tmp_path)).inherit_environment
    curl = server.cap.execute("(Get-Command curl).CommandType; (Get-Command wget -ErrorAction SilentlyContinue).CommandType")
    assert curl["success"] and curl["stdout"].split() == ["Application"]
    chained = server.cap.execute("echo a && echo b")
    assert not chained["success"] and "&&" in chained["hint"]
    # Commands without curl/wget run as written: no prelude in the error.
    assert "Remove-Item" not in chained["stderr"] and "char:8" in chained["stderr"]
    # A curl command that opens with using still runs (prelude skipped).
    using_curl = server.cap.execute("using namespace System.Text\n[StringBuilder]::new('curl').ToString()")
    assert using_curl["success"] and using_curl["stdout"].strip() == "curl"
    assert "hint" not in server.cap.execute("echo a; echo b")
    # Statements that must open a script still work behind the prelude.
    using = server.cap.execute("using namespace System.Text\n[StringBuilder]::new('ok').ToString()")
    assert using["success"] and using["stdout"].strip() == "ok"
    # Comments and help blocks may come first; so may odd spacing.
    for script in ("# note\n<# help\n#>\nusing namespace System.Text\n[StringBuilder]::new('ok').ToString()",
                   "param\t($v = 'ok')\n$v"):
        ran = server.cap.execute(script)
        assert ran["success"] and ran["stdout"].strip() == "ok", ran["stderr"]
    # A leading comment does not stop the prelude for ordinary commands.
    assert server.cap.execute("# x\n(Get-Command curl).CommandType")["stdout"].strip() == "Application"
    # The prelude shares line 1, so error line numbers stay as written.
    assert "At line:1" in server.cap.execute("throw 'x'")["stderr"]


def test_auto_shell_picks_pwsh_when_installed(tmp_path, monkeypatch):
    import agent_runtime.security as sec
    monkeypatch.setattr(sec, "find_pwsh", lambda: "C:/fake/pwsh.exe")
    cfg = Config(str(tmp_path), spill_dir=str(tmp_path / "spill"))
    assert cfg.default_shell == "pwsh"
    assert sec.command_for_spawn(cfg, "echo a", True)[0] == "C:/fake/pwsh.exe"
    execute = {t["name"]: t["description"] for t in Server(cfg).tools()}["execute"]
    assert "PowerShell 7" in execute and "5.1" not in execute
    # Explicit shells are kept; auto is case-insensitive and also works from a file.
    assert Config(str(tmp_path), default_shell="cmd").default_shell == "cmd"
    path = tmp_path / "lf.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "default_shell": "Auto"}))
    assert Config.load(str(path)).default_shell == "pwsh"
    monkeypatch.setattr(sec, "find_pwsh", lambda: None)
    assert Config(str(tmp_path)).default_shell == ("powershell" if os.name == "nt" else "sh")


@pytest.mark.skipif(os.name != "nt", reason="Windows install locations")
def test_find_pwsh_checks_install_folders(tmp_path, monkeypatch):
    import agent_runtime.security as sec
    monkeypatch.setattr(sec.shutil, "which", lambda name: str(tmp_path / "pwsh.cmd"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    msi = tmp_path / "pf" / "PowerShell" / "7" / "pwsh.exe"
    store = tmp_path / "local" / "Microsoft" / "WindowsApps" / "pwsh.exe"
    try:
        sec.find_pwsh.cache_clear()
        assert sec.find_pwsh() is None
        for exe in (store, msi):
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        sec.find_pwsh.cache_clear()
        assert sec.find_pwsh() == str(msi)
        msi.unlink()
        sec.find_pwsh.cache_clear()
        assert sec.find_pwsh() == str(store)
    finally:
        sec.find_pwsh.cache_clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_pwsh_string_commands_support_and_and(tmp_path):
    import agent_runtime.security as sec
    if not sec.find_pwsh():
        pytest.skip("PowerShell 7 not installed")
    server = make(tmp_path)
    server.cfg.default_shell = "pwsh"
    # make() inherits only PATH; Get-Command curl needs PATHEXT to find curl.exe.
    server.cfg.inherit_environment = Config(str(tmp_path)).inherit_environment
    ran = server.cap.execute("echo a && echo b")
    assert ran["success"] and ran["stdout"].split() == ["a", "b"] and "hint" not in ran
    curl = server.cap.execute("(Get-Command curl).CommandType")
    assert curl["stdout"].strip() == "Application"
    # NO_COLOR keeps pwsh errors free of ANSI colour codes, in both runners.
    failed = server.cap.execute("throw 'boom'")
    assert not failed["success"] and "boom" in failed["stderr"] and "\x1b" not in failed["stderr"]
    pid = server.processes.start("throw 'boom'")["process_id"]
    try:
        deadline = time.monotonic() + 15
        while server.processes.status(pid)["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        err = server.processes.read(pid)["stderr"]
        assert "boom" in err and "\x1b" not in err
    finally:
        server.processes.stop(pid, True)
    # Auto set after construction still spawns a real shell.
    server.cfg.default_shell = "auto"
    assert server.cap.execute("echo a && echo b")["success"]


def test_spawn_environment_adds_no_color_only_for_pwsh_shells(tmp_path, monkeypatch):
    import agent_runtime.security as sec
    monkeypatch.setattr(sec, "find_pwsh", lambda: "C:/fake/pwsh.exe")
    pwsh = Config(str(tmp_path), default_shell="pwsh", inherit_environment=["PATH"])
    assert sec.spawn_environment(pwsh, True)["NO_COLOR"] == "1"
    assert "NO_COLOR" not in sec.spawn_environment(pwsh, False)
    assert sec.spawn_environment(pwsh, True, {"NO_COLOR": ""})["NO_COLOR"] == ""
    assert "NO_COLOR" not in sec.spawn_environment(Config(str(tmp_path), default_shell="cmd"), True)


def test_spawn_environment_sets_python_utf8_mode(tmp_path):
    import agent_runtime.security as sec
    cfg = Config(str(tmp_path), default_shell="cmd", inherit_environment=["PATH"])
    env = sec.spawn_environment(cfg, False)
    assert env["PYTHONUTF8"] == "1" and "PYTHONIOENCODING" not in env
    assert sec.spawn_environment(cfg, True, {"PYTHONUTF8": "0"})["PYTHONUTF8"] == "0"
    cfg.extra_environment = {"PYTHONUTF8": "0"}
    assert sec.spawn_environment(cfg, False)["PYTHONUTF8"] == "0"


@pytest.mark.skipif(os.name != "nt", reason="Windows console code pages")
def test_server_children_print_unicode(tmp_path):
    import queue
    import threading
    text = "x \u00e9 \u65e5\u672c x"
    config = tmp_path / "lf.json"
    config.write_text(json.dumps({"workspace_root": str(tmp_path), "spill_dir": str(tmp_path / "spill")}))
    err_path = tmp_path / "server.err"
    err_file = open(err_path, "wb")
    try:
        # A fresh hidden console with the default OEM code page, like the
        # one Quick gives the server; the live server's console is untouched.
        server = subprocess.Popen([sys.executable, "-m", "agent_runtime.server"],
                                  cwd=str(Path(__file__).resolve().parent.parent),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file,
                                  env={**os.environ, "LOCALFORGE_CONFIG": str(config)},
                                  creationflags=0x08000000)  # CREATE_NO_WINDOW
    except BaseException:
        err_file.close()
        raise
    # Read replies on a thread, so a server that hangs fails the test
    # after a timeout instead of blocking it forever.
    replies = queue.Queue()

    def pump():
        try:
            for line in iter(server.stdout.readline, b""):
                replies.put(line)
        except (OSError, ValueError):
            pass
        replies.put(b"")

    threading.Thread(target=pump, daemon=True).start()

    def server_stderr():
        return err_path.read_bytes().decode("utf-8", "replace")

    def execute(n, command, stdin=None):
        arguments = {"command": command}
        if stdin is not None:
            arguments["input"] = stdin
        request = {"jsonrpc": "2.0", "id": n, "method": "tools/call",
                   "params": {"name": "execute", "arguments": arguments}}
        server.stdin.write((json.dumps(request) + "\n").encode())
        server.stdin.flush()
        try:
            line = replies.get(timeout=60)
        except queue.Empty:
            raise AssertionError(f"no reply in 60 s; server stderr: {server_stderr()}")
        assert line, f"server exited; stderr: {server_stderr()}"
        return json.loads(json.loads(line)["result"]["content"][0]["text"])

    # A Python child whose own subprocess pipe carries non-ASCII text:
    # this breaks if the inherited setting makes the two ends disagree.
    grandchild = ("import subprocess, sys; print(subprocess.run([sys.executable, '-c', "
                  "'print(chr(193), chr(26085))'], capture_output=True, text=True).stdout.strip())")
    try:
        cases = [(f"'{text}'", None, text),  # the default shell: pwsh, or Windows PowerShell 5.1
                 (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"'{text}'"], None, text),
                 (["cmd", "/d", "/c", f"echo {text}"], None, text),
                 ([sys.executable, "-c", f"print({text!r})"], None, text),
                 # Stdin: count characters, since a single-byte code page
                 # round-trips UTF-8 bytes unchanged but sees 13, not 8.
                 ([sys.executable, "-c", "import sys; print(len(sys.stdin.read().strip()))"], text, str(len(text))),
                 ("(@($input) -join '').Length", text, str(len(text))),
                 ([sys.executable, "-c", grandchild], None, "\u00c1 \u65e5")]
        for n, (command, stdin, want) in enumerate(cases, 1):
            ran = execute(n, command, stdin)
            assert ran["success"] and ran["stdout"].strip() == want, (command, ran, server_stderr())
    finally:
        server.stdin.close()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        server.stdout.close()
        err_file.close()


def test_search_reads_lines_that_are_not_utf8(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    server = make(tmp_path)
    (tmp_path / "latin.txt").write_bytes(b"one\ncaf\xe9 needle\nthree\n")
    found = server.cap.search("needle", context_lines=1)
    assert found["engine"] == "ripgrep"
    [hit] = found["results"][0]["matches"]
    assert hit["text"] == "caf\ufffd needle" and hit["before"] == ["1\tone"] and hit["after"] == ["3\tthree"]


def test_search_query_starting_with_dash(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("run --verbose\n-1 is negative\n")
    # ripgrep used to read a leading - as one of its own options.
    found = server.cap.search("--verbose")
    assert found["engine"] == "ripgrep" and [m["text"] for r in found["results"] for m in r["matches"]] == ["run --verbose"], found
    one_file = server.cap.search("-1", path=str(tmp_path / "a.txt"))
    assert [m["text"] for r in one_file["results"] for m in r["matches"]] == ["-1 is negative"], one_file
    regex = server.cap.search("-1 is", fixed_string=False)
    assert [m["text"] for r in regex["results"] for m in r["matches"]] == ["-1 is negative"], regex
    dash = server.cap.search("-")
    assert [m["text"] for r in dash["results"] for m in r["matches"]] == ["run --verbose", "-1 is negative"], dash
    near = server.cap.search("-1", context_lines=1)
    [hit] = near["results"][0]["matches"]
    assert hit["text"] == "-1 is negative" and hit["before"] == ["1\trun --verbose"], near


def test_git_shows_non_ascii_paths(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    server = make(tmp_path)
    server.cap.execute(["git", "init"])
    (tmp_path / "f\u00e9.txt").write_text("x")
    status = server.cap.git("status")
    assert "f\u00e9.txt" in status["stdout"] and "\\303" not in status["stdout"]


def test_cut_preview_drops_stale_cursors_and_keeps_own_hint(tmp_path):
    server = make(tmp_path, max_result_bytes=20000)
    big = "\n".join(f"{n}\tline {n} " + "x" * 50 for n in range(1, 2001))
    value = {"path": str(tmp_path / "f.txt"), "content": big, "line_start": 1, "line_end": 2000,
             "truncated": False, "next_line": 2001, "total_lines": 2000}
    fitted, _ = server._fit_result("filesystem", value, json.dumps(value))
    assert fitted["result_too_large"] and "[cut:" in fitted["content"]
    # These described the uncut content: following next_line would skip lines.
    assert "next_line" not in fitted and "line_end" not in fitted and fitted["truncated"] is True
    assert fitted["total_lines"] == 2000
    ran = {"success": True, "stdout": big, "stdout_truncated": False, "stderr": "", "stderr_truncated": False,
           "hint": "own hint", "process_id": "exec-1"}
    fitted, _ = server._fit_result("execute", ran, json.dumps(ran))
    assert fitted["stdout_truncated"] is True and fitted["stderr_truncated"] is False
    assert "result_path" in fitted["hint"] and fitted["hint"].endswith("own hint") and fitted["process_id"] == "exec-1"


def test_cut_text_snaps_to_a_line_only_near_the_limit():
    from agent_runtime.server import _cut_text
    # A short first line must not swallow a long one.
    cut = _cut_text("hdr\n" + "y" * 100000, 50000)
    assert len(cut) > 40000 and "bytes" in cut
    cut = _cut_text("\n".join("z" * 99 for _ in range(1000)), 50000)
    assert "lines 1-" in cut and len(cut) > 25000
    # Only a little over the limit: the marker would make it longer, so it stays whole.
    assert _cut_text("a" * 300, 256) == "a" * 300


def test_preview_never_cuts_paths_or_ids(tmp_path):
    server = make(tmp_path, max_result_bytes=8000)
    long_path = "C:\\" + "d" * 400 + "\\f.txt"
    value = {"path": long_path, "cwd": long_path, "process_id": "exec-" + "1" * 300, "content": "c" * 50000}
    fitted, _ = server._fit_result("filesystem", value, json.dumps(value))
    assert fitted["result_too_large"] and "[cut:" in fitted["content"]
    assert fitted["path"] == long_path and fitted["cwd"] == long_path and fitted["process_id"] == value["process_id"]


def test_cut_inside_nested_values_drops_parent_cursors(tmp_path):
    # A process read with format chunks: only the chunk texts are cut.
    server = make(tmp_path, max_result_bytes=120000)
    read = {"process_id": "p", "state": "exited", "truncated": False, "next_after": 60,
            "chunks": [{"seq": n, "stream": "stdout", "text": "a" * 4096, "at": 0.0} for n in range(1, 61)]}
    fitted, _ = server._fit_result("process", read, json.dumps(read))
    assert len(fitted["chunks"]) == 60 and "[cut:" in fitted["chunks"][0]["text"]
    assert "next_after" not in fitted and fitted["truncated"] is True
    assert "next_after" in fitted["hint"] and "limit_bytes" in fitted["hint"]
    # A list of long strings next to a cursor.
    small = make(tmp_path, max_result_bytes=40000)
    listed = {"lines": ["x" * 5000] * 10, "next_offset": 99, "truncated": False}
    fitted, _ = small._fit_result("filesystem", listed, json.dumps(listed))
    assert len(fitted["lines"]) == 10 and "[cut:" in fitted["lines"][0]
    assert "next_offset" not in fitted and fitted["truncated"] is True
    # Every dict that holds the cut loses its cursors, not only the innermost.
    nested = {"next_line": 5, "result": {"next_line": 7, "content": "y" * 50000}}
    fitted, _ = small._fit_result("batch", nested, json.dumps(nested))
    assert "next_line" not in fitted and "next_line" not in fitted["result"]


def test_cut_preview_hint_has_no_trailing_space(tmp_path):
    server = make(tmp_path, max_result_bytes=8000)
    value = {"content": "c" * 50000, "hint": ""}
    fitted, _ = server._fit_result("filesystem", value, json.dumps(value))
    assert fitted["result_too_large"] and fitted["hint"] == fitted["hint"].strip()


def test_sequential_batch_stops_once_its_time_budget_is_used(tmp_path):
    server = make(tmp_path, execute_background_after_seconds=1)
    slow = {"tool": "execute", "arguments": {"command": [sys.executable, "-c", "import time; time.sleep(1.5)"],
                                              "timeout": 10}}
    write = {"tool": "filesystem", "arguments": {"action": "write", "path": "after.txt", "content": "x"}}
    out = server.batch([slow, write], sequential=True)
    assert out["results"][0]["result"]["success"], out
    assert out["completed"] == 1 and out["stopped_at"] == 1
    assert out["results"][1]["skipped"] and "time budget" in out["results"][1]["reason"]
    assert not (tmp_path / "after.txt").exists()


def test_config_requires_room_for_the_oversized_reply(tmp_path):
    path = tmp_path / "lf.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path), "max_result_bytes": 500}))
    with pytest.raises(ValueError):
        Config.load(str(path))
    # Checked on construction too, not only when loaded from a file.
    with pytest.raises(ValueError):
        Config(str(tmp_path), max_result_bytes=500)


def test_result_with_lone_surrogate_still_fits(tmp_path):
    server = make(tmp_path, max_result_bytes=1024)
    value = {"name": "bad\udc80name", "pad": "p" * 2000}
    fitted, text = server._fit_result("filesystem", value, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    assert fitted["result_too_large"] and len(text.encode("utf-8")) <= 1024
    assert "bad?name" in Path(fitted["result_path"]).read_text(encoding="utf-8")
    small = {"name": "x\udc80"}
    small_text = json.dumps(small, ensure_ascii=False)
    assert server._fit_result("filesystem", small, small_text) == (small, small_text)


def test_diff_skipped_for_large_or_binary_files(tmp_path):
    server = make(tmp_path)
    big = "line\n" * 30000
    server.cap.filesystem("write", "big.txt", big)
    assert server.cap.filesystem("write", "big.txt", big + "x\n")["diff_skipped"] == "too_large"
    replaced = server.cap.filesystem("replace_text", "big.txt", old_text="line", new_text="LINE", expected_occurrences=30000)
    assert replaced["diff_skipped"] == "too_large" and "diff" not in replaced
    (tmp_path / "bin.dat").write_bytes(b"a\x00b\n")
    assert server.cap.filesystem("write", "bin.dat", "text\n")["diff_skipped"] == "binary"
    off = make(tmp_path, edit_diff_max_bytes=0)
    assert "diff_skipped" not in off.cap.filesystem("write", "big.txt", "small\n")


def test_project_instructions_cut_keeps_bad_bytes_visible(tmp_path):
    server = make(tmp_path, max_project_instruction_bytes=7)
    (tmp_path / "AGENTS.md").write_bytes(b"x\xffyz\xc3\xa9\xc3\xa9")
    [entry] = server.cap._project_instructions(None, tmp_path)
    assert entry["content"] == "x\ufffdyz" and entry["truncated"]


def test_spill_dir_paths_and_failures(tmp_path, monkeypatch):
    import tempfile
    # Default spill folders go under %TEMP%; keep them in tmp_path.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setenv("LF_SPILL_TEST", str(tmp_path))
    expanded = make(tmp_path, spill_dir=os.path.join("$LF_SPILL_TEST", "s"))
    assert expanded.cap.spill_dir == (tmp_path / "s").resolve()
    relative = make(tmp_path, spill_dir="rel")
    assert relative.cap.spill_dir == (tmp_path / "rel").resolve()
    # Each config gets its own default folder.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = make(tmp_path / "a", spill_dir=None), make(tmp_path / "b", spill_dir=None)
    assert first.cap.spill_dir != second.cap.spill_dir
    assert first.cap.spill_dir.parent == (tmp_path / "tmp" / "localforge-results").resolve()
    # An unusable folder turns saving off instead of failing startup.
    (tmp_path / "file").write_text("x")
    broken = make(tmp_path, spill_dir=str(tmp_path / "file"), max_inline_output_bytes=1000)
    assert broken.cap.spill_dir is None
    big = broken.cap.execute([sys.executable, "-c", "print('a' * 3000)"])
    assert big["stdout_truncated"] and "stdout_path" not in big
    fitted, _ = broken._fit_result("filesystem", {"x": "y" * 300000}, json.dumps({"x": "y" * 300000}))
    assert fitted["result_path"] is None and "failed" in fitted["hint"]


def test_edit_miss_shows_closest_lines(tmp_path):
    server = make(tmp_path)
    (tmp_path / "app.py").write_text("import os\n\ndef total(items):\n    return  sum(items)\n\ndef other():\n    pass\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "app.py", old_text="def total(items):\n    return sum( items )",
                              new_text="def total(xs):\n    return sum(xs)")
    message = exc.value.message
    assert exc.value.code == "content_mismatch" and "found 0" in message
    # Numbered like line-mode read, and only the matched span.
    assert "3\tdef total(items):\n4\t    return  sum(items)" in message
    assert "import os" not in message and "other" not in message
    # Fewer than half of old_text's lines agree: no hint.
    with pytest.raises(RuntimeFault) as weak:
        server.cap.filesystem("replace_text", "app.py", old_text="import os\nnothing here\nnor here", new_text="x")
    assert "\t" not in weak.value.message
    # Padding old_text with unmatched lines reveals nothing past the last match.
    (tmp_path / "v.txt").write_text("x = 1\ny = 2\nz = 3\nw = 4\n")
    with pytest.raises(RuntimeFault) as padded:
        server.cap.filesystem("replace_text", "v.txt", old_text="x = 1\ny = 2\nQQQ", new_text="x")
    assert "1\tx = 1\n2\ty = 2" in padded.value.message and "z = 3" not in padded.value.message
    # At most 12 lines, each cut to 200 characters; CRLF files hint from the LF view.
    rows = "".join(f"row {n}\r\n" for n in range(1, 21))
    (tmp_path / "rows.txt").write_bytes(("k" + "x" * 300 + "\r\n" + rows).encode())
    old = "k" + "x" * 300 + "\n" + rows.replace("\r\n", "\n").replace("row 5\n", "row five\n")
    with pytest.raises(RuntimeFault) as wide:
        server.cap.filesystem("replace_text", "rows.txt", old_text=old, new_text="x")
    shown = [line for line in wide.value.message.split("\n") if "\t" in line]
    assert len(shown) == 12 and shown[0].startswith("1\tk") and shown[-1] == "12\trow 11"
    assert len(shown[0]) < 230 and "\r" not in wide.value.message


def test_edit_miss_says_when_already_applied(tmp_path):
    server = make(tmp_path)
    (tmp_path / "a.py").write_text("value = compute_total(alpha, beta)\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "a.py", old_text="value = compute(alpha)",
                              new_text="value = compute_total(alpha, beta)")
    assert "already" in exc.value.message
    # Short new_text is too common to mean anything.
    with pytest.raises(RuntimeFault) as short:
        server.cap.filesystem("replace_text", "a.py", old_text="nope", new_text="alpha")
    assert "already" not in short.value.message


def test_edit_miss_hints_multi_edit_and_repeated_matches(tmp_path):
    server = make(tmp_path)
    (tmp_path / "m.txt").write_text("one\ntwo\nthree\n")
    # The hint for a failing edit reflects the earlier edits.
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("multi_edit", "m.txt", edits=[
            {"old_text": "one", "new_text": "uno"},
            {"old_text": "uno\n two \nfour", "new_text": "x"},
        ])
    assert "edits[1]" in exc.value.message and "1\tuno\n2\ttwo" in exc.value.message
    assert (tmp_path / "m.txt").read_text() == "one\ntwo\nthree\n"
    # Too many matches for the expected one: say where they are.
    (tmp_path / "r.txt").write_text("x\ny\nx\nz\nx\n")
    with pytest.raises(RuntimeFault) as many:
        server.cap.filesystem("replace_text", "r.txt", old_text="x", new_text="X")
    assert "found 3" in many.value.message and "lines 1, 3, 5" in many.value.message
    (tmp_path / "s.txt").write_text("x\n" * 15)
    with pytest.raises(RuntimeFault) as capped:
        server.cap.filesystem("multi_edit", "s.txt", edits=[{"old_text": "x", "new_text": "X"}])
    assert "lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ..." in capped.value.message


def test_missing_path_suggests_similar_names(tmp_path):
    server = make(tmp_path, denied_paths=[str(tmp_path / "src" / "secret.py")])
    (tmp_path / "src").mkdir()
    for name in ("config.py", "Config.json", "settings.py", "app.py", "secret.py", "zz.txt"):
        (tmp_path / "src" / name).write_text("x")
    with pytest.raises(RuntimeFault) as typo:
        server.cap.filesystem("read", "src/confg.py")
    assert typo.value.code == "path_not_found"
    shown = typo.value.message.split("Did you mean one of these?\n")[1].split("\n")
    assert shown == [os.path.join("src", "config.py"), os.path.join("src", "Config.json")]
    # Same stem with another extension; every action that needs the path.
    calls = {"read": {}, "stat": {}, "list": {}, "delete": {}, "move": {"destination": "m.py"},
             "copy": {"destination": "c.py"}, "replace_text": {"old_text": "a", "new_text": "b"},
             "multi_edit": {"edits": [{"old_text": "a", "new_text": "b"}]}}
    for action, extra in calls.items():
        with pytest.raises(RuntimeFault) as exc:
            server.cap.filesystem(action, "src/app.js", **extra)
        assert exc.value.code == "path_not_found" and os.path.join("src", "app.py") in exc.value.message, action
    # Denied paths are never suggested.
    with pytest.raises(RuntimeFault) as denied:
        server.cap.filesystem("read", "src/secrt.py")
    assert "secret.py" not in denied.value.message and "Did you mean" not in denied.value.message
    # A missing folder: name it, its nearest existing parent, and similar names there.
    with pytest.raises(RuntimeFault) as folder:
        server.cap.filesystem("read", "srcx/deep/a.py")
    message = folder.value.message
    root = tmp_path.resolve()
    assert str(root / "srcx") in message and f"nearest folder that does is {root}" in message
    assert str(root / "src") + "\n" in message + "\n"
    # Nothing outside the read roots is listed or named.
    with pytest.raises(RuntimeFault) as outside:
        server.cap.filesystem("read", str(tmp_path.parent / "nope-lf" / "x.txt"))
    assert outside.value.code == "path_not_found" and "nearest" not in outside.value.message
    assert "Did you mean" not in outside.value.message


def _exec_entries(server):
    return [p["process_id"] for p in server.processes.list() if p["process_id"].startswith("exec-")]


def test_execute_moves_to_background_on_timeout(tmp_path):
    server = make(tmp_path)
    code = "import time\nprint('started', flush=True)\ntime.sleep(5)\nprint('later', flush=True)\ntime.sleep(30)"
    out = server.cap.execute([sys.executable, "-c", code], timeout=3)
    pid = out.get("process_id")
    try:
        assert out["success"] is False and out["error_type"] == "backgrounded" and out["exit_code"] is None
        assert "started" in out["stdout"] and "later" not in out["stdout"]
        assert pid and pid in out["hint"] and "process" in out["hint"]
        assert server.processes.status(pid)["state"] == "running"
        # Output keeps flowing; next_after skips what the result already showed.
        page = server.processes.read(pid, after=out["next_after"], wait_ms=10000)
        assert "later" in page["stdout"] and "started" not in page["stdout"]
    finally:
        if pid:
            server.processes.stop(pid, True)


def test_execute_kill_on_timeout_and_cleanup(tmp_path):
    server = make(tmp_path)
    sleeper = [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"]
    killed = server.cap.execute(sleeper, timeout=0.5, kill_on_timeout=True)
    assert killed["error_type"] == "timeout" and "process_id" not in killed
    assert _exec_entries(server) == []
    # A command that ends in time leaves no process entry behind.
    ok = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert ok["success"] and "process_id" not in ok and _exec_entries(server) == []
    # No timeout given: the foreground wait is execute_background_after_seconds.
    short = make(tmp_path, execute_background_after_seconds=1)
    started = time.monotonic()
    out = short.cap.execute(sleeper)
    try:
        assert out["error_type"] == "backgrounded" and time.monotonic() - started < 10
    finally:
        short.processes.stop(out["process_id"], True)


def test_execute_kills_on_timeout_when_process_limit_is_reached(tmp_path):
    server = make(tmp_path, max_processes=1, process_ttl_seconds=1000)
    hog = server.processes.start([sys.executable, "-c", "import time; time.sleep(30)"])["process_id"]
    try:
        out = server.cap.execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        assert out["error_type"] == "timeout" and "max_processes" in out["hint"]
        assert _exec_entries(server) == []
    finally:
        server.processes.stop(hog, True)


def test_edit_checks_stay_bounded(tmp_path):
    slow = [sys.executable, "-c", "import time; time.sleep(30)", "{path}"]
    server = make(tmp_path, edit_checks={"*.chk": slow}, edit_check_timeout_seconds=1)
    check = server.cap.filesystem("write", "a.chk", "x")["check"]
    assert check["ok"] is False and check["error_type"] == "timeout"
    assert _exec_entries(server) == []


def test_execute_marks_output_past_the_process_buffer(tmp_path):
    server = make(tmp_path, process_buffer_bytes=5000)
    out = server.cap.execute([sys.executable, "-c", "import sys\nfor n in range(100): print(str(n) * 100, flush=True)"])
    assert out["success"] and out["stdout_truncated"]
    assert out["stdout"].startswith("...[earlier output dropped") and out["stdout"].rstrip().endswith("99" * 100)


def test_execute_marks_dropped_output_per_stream(tmp_path):
    server = make(tmp_path, process_buffer_bytes=5000)
    code = "import sys\nfor n in range(100): print(str(n) * 100, file=sys.stderr, flush=True)\nprint('done', flush=True)"
    out = server.cap.execute([sys.executable, "-c", code])
    assert out["stderr_truncated"] and out["stderr"].startswith("...[earlier output dropped"), out
    assert not out["stdout_truncated"] and out["stdout"].strip() == "done", out


def test_execute_drops_its_entry_when_the_forced_stop_times_out(tmp_path, monkeypatch):
    server = make(tmp_path)

    def stuck(process_id, force=False):
        raise subprocess.TimeoutExpired("stop", 10)

    monkeypatch.setattr(server.processes, "stop", stuck)
    out = server.cap.execute([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5, kill_on_timeout=True)
    assert out["error_type"] == "timeout" and _exec_entries(server) == [], out


def test_execute_without_input_gives_an_empty_stdin(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("needle\n")
    # With a closed stdin pipe, rg searched that (empty) pipe instead of the cwd.
    out = server.cap.execute(["rg", "-c", "needle"])
    assert out["success"] and "a.txt" in out["stdout"], out
    # A process started without a stdin pipe says its stdin is closed.
    item = server.processes.spawn([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL)
    try:
        with pytest.raises(RuntimeFault) as exc:
            server.processes.write(item.id, "x")
        assert exc.value.code == "stdin_closed"
    finally:
        server.processes.stop(item.id, True)


# The command exits soon; what it starts keeps its output pipes and
# prints every second. The first ping gives cmd time to be put in its
# job before it starts anything. Not Python: a Store Python's children
# are started outside the caller's job.
_GRANDCHILD = ("ping -n 2 127.0.0.1 >nul & echo parent & start /b ping -n 30 127.0.0.1" if os.name == "nt"
               else "echo parent; (while :; do echo tick; sleep 1; done) &")


def _pipes_close(item, seconds=10):
    # The pipes close once whatever held them is gone.
    for pump in item.pumps:
        pump.join(seconds)
    return not any(pump.is_alive() for pump in item.pumps)


def test_execute_backgrounds_a_grandchild_holding_the_pipes(tmp_path):
    server = make(tmp_path)
    started = time.monotonic()
    out = server.cap.execute(_GRANDCHILD, timeout=5)
    elapsed = time.monotonic() - started
    pid = out.get("process_id")
    try:
        assert out["error_type"] == "backgrounded" and out["exit_code"] == 0 and pid, out
        assert "parent" in out["stdout"] and pid in out["hint"]
        # One deadline for both pipes, not a second more per pipe.
        assert elapsed < 5.9, elapsed
        # Output keeps flowing, and process stop ends what holds the pipes.
        assert server.processes.read(pid, after=out["next_after"], wait_ms=5000)["stdout"]
        item = server.processes._get(pid)
    finally:
        if pid:
            server.processes.stop(pid, True)
    assert _pipes_close(item)


def test_process_wait_ends_soon_after_exit_while_output_is_held(tmp_path):
    server = make(tmp_path)
    pid = server.call("process", {"action": "start", "command": _GRANDCHILD})["process_id"]
    try:
        started = time.monotonic()
        out = server.call("process", {"action": "wait", "process_id": pid, "wait_ms": 20000})
        elapsed = time.monotonic() - started
        assert out["reason"] == "exited" and out["state"] == "exited" and out["output_held"] is True, out
        assert elapsed < 10, elapsed
    finally:
        server.processes.stop(pid, True)


# Like _GRANDCHILD, but what it leaves running prints a line a few seconds
# after the command exits.
_LATE_READY = ("ping -n 2 127.0.0.1 >nul & echo parent & start /b cmd /c ping -n 5 127.0.0.1 ^>nul "
               "^& echo daemon ready ^& ping -n 30 127.0.0.1 ^>nul" if os.name == "nt"
               else "echo parent; (sleep 4; echo daemon ready; sleep 30) &")


def test_process_wait_for_a_pattern_outlasts_the_exit_grace_while_output_is_held(tmp_path):
    server = make(tmp_path)
    pid = server.call("process", {"action": "start", "command": _LATE_READY})["process_id"]
    try:
        out = server.call("process", {"action": "wait", "process_id": pid, "pattern": "daemon ready", "wait_ms": 20000})
        assert out["reason"] == "matched" and out["state"] == "exited" and out["output_held"] is True, out
    finally:
        server.processes.stop(pid, True)


def test_execute_kills_a_grandchild_holding_the_pipes_on_kill_on_timeout(tmp_path, monkeypatch):
    server = make(tmp_path)
    dropped = []
    discard = server.processes.discard
    monkeypatch.setattr(server.processes, "discard", lambda item: (dropped.append(item), discard(item)))
    out = server.cap.execute(_GRANDCHILD, timeout=5, kill_on_timeout=True)
    assert out["error_type"] == "timeout" and "process_id" not in out and _exec_entries(server) == [], out
    assert "parent" in out["stdout"] and _pipes_close(dropped[0])
    # It exited 0, so say why the reply is still a timeout.
    assert "exited" in out.get("hint", ""), out
    # The command had already exited: only what it left running was stopped.
    assert "what it left running" in out["hint"] and "the command was stopped" not in out["hint"].lower(), out


def _fake_entry(code, pumps=(), job=None, started=None):
    import types
    return types.SimpleNamespace(proc=types.SimpleNamespace(pid=4242, poll=lambda: code), pumps=list(pumps),
                                 job=job, started=time.time() if started is None else started)


def _held_pump():
    import threading
    hold = threading.Event()
    pump = threading.Thread(target=hold.wait, daemon=True)
    pump.start()
    return hold, pump


def test_posix_stop_never_signals_a_group_that_is_gone(tmp_path, monkeypatch):
    import types
    import agent_runtime.processes as procs
    server = make(tmp_path)
    calls = []
    hold, pump = _held_pump()
    try:
        # Only processes.py sees POSIX: the global os module is untouched.
        with monkeypatch.context() as m:
            m.setattr(procs, "os", types.SimpleNamespace(name="posix", killpg=lambda pid, sig: calls.append(pid)))
            # Exited and nothing holds its pipes: the group is gone, and
            # its id may already belong to an unrelated group.
            server.processes._terminate_tree(_fake_entry(0))
            gone = list(calls)
            server.processes._terminate_tree(_fake_entry(None))
            server.processes._terminate_tree(_fake_entry(0, [pump]))
        assert gone == [] and calls == [4242, 4242]
    finally:
        hold.set()


def test_stop_ends_the_job_after_the_process_exits_on_its_own(tmp_path):
    import types
    server = make(tmp_path)
    ended = []
    entry = _fake_entry(None, job=types.SimpleNamespace(terminate=lambda: ended.append(True)))
    entry.proc.wait = lambda timeout=None: 0
    entry.command, entry.cwd = ["x"], str(tmp_path)
    server.processes.items = {"p": entry}
    # A graceful stop: the leader exits by itself after the stop request,
    # so _terminate_tree saw it running and never ended the job.
    server.processes._terminate_tree = lambda item, force=False: None
    server.processes.stop("p")
    assert ended == [True]


def test_active_counts_entries_whose_pipes_are_still_held(tmp_path):
    server = make(tmp_path, max_processes=5)
    hold, pump = _held_pump()
    try:
        server.processes.items = {"held": _fake_entry(0, [pump]), "done": _fake_entry(0),
                                  "running": _fake_entry(None)}
        # The held one can still be read and stopped, so it takes a slot.
        assert server.processes.active() == 2
    finally:
        hold.set()


@pytest.mark.skipif(os.name != "nt", reason="taskkill and job objects")
def test_stop_ends_the_job_when_the_process_exits_during_taskkill(tmp_path, monkeypatch):
    import types
    import agent_runtime.processes as procs
    server = make(tmp_path)
    state, ended = {"code": None}, []

    def taskkill(*args, **kwargs):
        state["code"] = 0  # exits while taskkill runs, so taskkill finds no PID
        return types.SimpleNamespace(returncode=128)

    entry = _fake_entry(None, job=types.SimpleNamespace(terminate=lambda: ended.append(True)))
    entry.proc.poll = lambda: state["code"]
    monkeypatch.setattr(procs.subprocess, "run", taskkill)
    server.processes._terminate_tree(entry, True)
    assert ended == [True]


def test_gc_keeps_an_exited_entry_whose_pipes_are_still_held(tmp_path):
    server = make(tmp_path, max_processes=1, process_ttl_seconds=1)
    hold, pump = _held_pump()
    old = time.time() - 100
    try:
        server.processes.items = {"held": _fake_entry(0, [pump], started=old), "done": _fake_entry(0, started=old),
                                  "recent1": _fake_entry(0), "recent2": _fake_entry(0)}
        server.processes._gc()
        # Past the TTL and over max_processes, but still stoppable: kept.
        assert set(server.processes.items) == {"held"}
    finally:
        hold.set()


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_groups_matches_by_file(tmp_path, monkeypatch, engine):
    if engine == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    server = make(tmp_path)
    (tmp_path / "a.txt").write_text("hit one\nmiss\nhit two\n")
    (tmp_path / "b.txt").write_text("hit three\n")
    out = server.cap.search("hit", context_lines=1)
    assert out["engine"] == engine and out["match_count"] == 3 and out["truncated"] is False
    # The path once per file, not once per match.
    by_path = {r["path"]: r["matches"] for r in out["results"]}
    assert set(by_path) == {"a.txt", "b.txt"} and len(out["results"]) == 2
    assert [(m["line"], m["text"]) for m in by_path["a.txt"]] == [(1, "hit one"), (3, "hit two")]
    assert by_path["a.txt"][1]["before"] == ["2\tmiss"] and "path" not in by_path["a.txt"][0]
    # max_results still counts matches.
    capped = server.cap.search("hit", max_results=2)
    assert capped["match_count"] == 2 and capped["truncated"] is True
    assert sum(len(r["matches"]) for r in capped["results"]) == 2
    multi = server.cap.multi_search([{"query": "three"}])
    assert multi["results"][0]["results"] == [{"path": "b.txt", "matches": [{"line": 1, "text": "hit three"}]}]
    assert multi["results"][0]["match_count"] == 1
    # files_only keeps its shape.
    assert {"path": "b.txt"} in server.cap.search(files_only=True)["results"]


def test_edit_falls_back_to_typography_and_trailing_whitespace(tmp_path):
    server = make(tmp_path)
    (tmp_path / "t.py").write_text("msg = \u201chello\u201d \u2013 world\u00a0x\nkeep\n", encoding="utf-8")
    out = server.cap.filesystem("replace_text", "t.py", old_text='msg = "hello" - world x', new_text='msg = "bye"')
    assert out["match"] == "typography" and out["replacements"] == 1
    assert (tmp_path / "t.py").read_text(encoding="utf-8") == 'msg = "bye"\nkeep\n'
    (tmp_path / "w.py").write_text("def f():   \n    return \u2018a\u2019  \n\nother\n", encoding="utf-8")
    out = server.cap.filesystem("replace_text", "w.py", old_text="def f():\n    return 'a'\n",
                                new_text="def g():\n    return 'b'\n")
    assert out["match"] == "trailing_whitespace"
    assert (tmp_path / "w.py").read_text(encoding="utf-8") == "def g():\n    return 'b'\n\nother\n"
    # Exact matches win and say nothing extra.
    (tmp_path / "e.py").write_text('a = "x"\na = \u201cx\u201d\n', encoding="utf-8")
    out = server.cap.filesystem("replace_text", "e.py", old_text='a = "x"', new_text='a = "y"')
    assert "match" not in out
    assert (tmp_path / "e.py").read_text(encoding="utf-8") == 'a = "y"\na = \u201cx\u201d\n'
    # expected_occurrences applies to the fallback too.
    (tmp_path / "two.py").write_text("q(\u201cz\u201d)\nq(\u201cz\u201d)\n", encoding="utf-8")
    out = server.cap.filesystem("replace_text", "two.py", old_text='q("z")', new_text="q(1)", expected_occurrences=2)
    assert out["match"] == "typography" and (tmp_path / "two.py").read_text(encoding="utf-8") == "q(1)\nq(1)\n"


def test_edit_fallback_keeps_crlf_bytes(tmp_path):
    server = make(tmp_path)
    path = tmp_path / "c.txt"
    path.write_bytes("a = \u201cx\u201d\r\nkeep  \r\nb = 1   \r\nc\r\n".encode("utf-8"))
    server.cap.filesystem("replace_text", "c.txt", old_text='a = "x"', new_text='a = "y"')
    assert path.read_bytes() == 'a = "y"\r\nkeep  \r\nb = 1   \r\nc\r\n'.encode("utf-8")
    out = server.cap.filesystem("multi_edit", "c.txt", edits=[
        {"old_text": 'a = "y"', "new_text": 'a = "z"'},
        {"old_text": "b = 1\nc\n", "new_text": "b = 2\nc\n"}])
    assert "match" not in out["edits"][0] and out["edits"][1]["match"] == "trailing_whitespace"
    assert path.read_bytes() == b'a = "z"\r\nkeep  \r\nb = 2\r\nc\r\n'
    # An edit over a minority-ending line leaves the other lines' endings alone.
    path.write_bytes(b"keep\nX\r\nb\nc\r\nd\r\n")
    server.cap.filesystem("replace_text", "c.txt", old_text="b\nc", new_text="B\nC")
    assert path.read_bytes() == b"keep\nX\r\nB\r\nC\r\nd\r\n"


def test_edit_fallback_refuses_ambiguous_or_line_ending_edits(tmp_path):
    server = make(tmp_path)
    ambiguous = "x = \u201ca\u201d\ny = 1\nx = \u201ca\u201d\n"
    (tmp_path / "a.py").write_text(ambiguous, encoding="utf-8")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "a.py", old_text='x = "a"', new_text='x = "b"')
    # The fallback counts and where they start, not just "found 0".
    message = exc.value.message
    assert exc.value.code == "content_mismatch" and "found 0 exact, 2 ignoring typography, 2 ignoring" in message
    assert "Matches ignoring typography start on lines 1, 3." in message, message
    (tmp_path / "b.py").write_text("foo  \nfoo \n")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("multi_edit", "b.py", edits=[{"old_text": "foo\n", "new_text": "bar\n"}])
    # Overlapping fallback matches are ambiguous too.
    (tmp_path / "o.txt").write_text("\u2013\u2013\u2013\n", encoding="utf-8")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("replace_text", "o.txt", old_text="--", new_text="+")
    # A lone CR in the edit asks for exact bytes: no fallback.
    (tmp_path / "r.txt").write_text("a  \nb\n")
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("replace_text", "r.txt", old_text="a\r", new_text="a")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == ambiguous
    assert (tmp_path / "b.py").read_text() == "foo  \nfoo \n" and (tmp_path / "r.txt").read_text() == "a  \nb\n"
    assert (tmp_path / "o.txt").read_text(encoding="utf-8") == "\u2013\u2013\u2013\n"


def test_edit_miss_hint_ignores_typography(tmp_path):
    server = make(tmp_path)
    (tmp_path / "h.py").write_text("say(\u201chi\u201d)\nnext\n", encoding="utf-8")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "h.py", old_text='say("hi")\nnope', new_text="x")
    assert "1\tsay(\u201chi\u201d)" in exc.value.message


def test_edit_fallback_tries_the_next_tier_after_a_wrong_count(tmp_path):
    server = make(tmp_path)
    # Ignoring typography finds both lines; whole lines ignoring trailing
    # whitespace find one, so that tier applies.
    (tmp_path / "n.py").write_text("x = \u201ca\u201d  \nx = \u201ca\u201d!\n", encoding="utf-8")
    out = server.cap.filesystem("replace_text", "n.py", old_text='x = "a"', new_text='x = "b"')
    assert out["match"] == "trailing_whitespace"
    assert (tmp_path / "n.py").read_text(encoding="utf-8") == 'x = "b"\nx = \u201ca\u201d!\n'


def test_edit_miss_hint_skips_very_common_lines(tmp_path):
    server = make(tmp_path)
    (tmp_path / "c.txt").write_text("end\n" * 150 + "def target():\n    return 1\nend\n")
    with pytest.raises(RuntimeFault) as exc:
        server.cap.filesystem("replace_text", "c.txt", old_text="def target():\n    return 2\nend", new_text="x")
    assert "151\tdef target():" in exc.value.message
