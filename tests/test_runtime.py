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
