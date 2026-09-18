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
    cfg = Config(str(tmp), mode=mode, allowed_read_roots=[str(tmp)], allowed_write_roots=[str(tmp)],
                 network=network, inherit_environment=["PATH"], default_shell="cmd" if os.name == "nt" else "sh", **kwargs)
    return Server(cfg)

def test_mcp_protocol(tmp_path):
    server = make(tmp_path)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "localforge-mcp"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {item["name"] for item in tools} == {"workspace", "filesystem", "search", "git", "execute", "process"}
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
    server.cap.filesystem("replace_text", "a.txt", old_text="world", new_text="agent")
    assert (tmp_path / "a.txt").read_text() == "hello agent"
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("replace_text", "a.txt", old_text="missing", new_text="x")
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
    timeout = server.cap.execute([sys.executable, "-c", "import time;time.sleep(2)"], timeout=.05)
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
        assert "ready" in "".join(chunk["text"] for chunk in output["chunks"])
    restarted = server.processes.restart("worker-0")
    assert restarted["process_id"] == "worker-0"
    for process_id in ids: server.processes.stop(process_id, True)

def test_search_fallback_and_git(tmp_path, monkeypatch):
    server = make(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("# TODO UserService\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = server.cap.search("UserService", glob=["*.py"])
    assert result["results"][0]["line"] == 1
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
    with pytest.raises(RuntimeFault) as bad_dest:
        server.cap.filesystem("write", "ok.txt", "x")
        server.cap.filesystem("move", "ok.txt", destination="no-such-dir/moved.txt")
    assert bad_dest.value.code == "path_not_found"

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
    assert any(str(tmp_path / "src" / "hit.py") in p for p in paths)
    assert not any(".venv" in p for p in paths)
    assert not any("big.py" in p for p in paths)
    assert ".git/**" in content["applied_excludes"]
    ctx = server.cap.search("UserService", path="src/hit.py", context_lines=1)
    assert ctx["results"][0]["line"] == 2
    assert "line one" in ctx["results"][0]["context"]
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
    assert [l["no"] for l in lines["lines"]] == [2, 3]
    assert lines["total_lines"] == 3 and lines["truncated"] is False
    tail = server.cap.filesystem("read", "a.txt", line_start=3)
    assert tail["line_end"] == 3 and tail["truncated"] is False
    with pytest.raises(RuntimeFault):
        server.cap.filesystem("read", "a.txt", line_start=3, line_end=2)


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
def test_process_string_auto_enables_shell(tmp_path): server = make(tmp_path); p = server.processes.start("echo proc-shell-ok")["process_id"]; out = server.processes.read(p, wait_ms=5000); assert "proc-shell-ok" in "".join(c["text"] for c in out["chunks"]); server.processes.stop(p, True)
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
        server.call("filesystem", {"action": "read", "path": "x", "old": "y"})
    assert exc.value.code == "invalid_arguments" and "old_text" in exc.value.message
    with pytest.raises(RuntimeFault) as exc2:
        server.call("execute", {"command": ["x"], "bogus": 1})
    assert "Valid arguments" in exc2.value.message and "command" in exc2.value.message

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


def test_empty_output_failure_explains_itself(tmp_path):
    server = make(tmp_path)
    bad = server.cap.execute([sys.executable, "-c", "raise SystemExit(7)"])
    assert bad["exit_code"] == 7 and "no output captured" in bad["stderr"]
    ok = server.cap.execute([sys.executable, "-c", "print('ok')"])
    assert "no output captured" not in ok["stderr"]
