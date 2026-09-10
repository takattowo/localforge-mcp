from __future__ import annotations
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from .errors import RuntimeFault
from .security import command_for_spawn, redact, safe_environment

class Capabilities:
    def __init__(self, cfg, paths, policy, processes):
        self.cfg, self.paths, self.policy, self.processes = cfg, paths, policy, processes
        self.cwd = paths.workspace

    def workspace(self, action, path=None):
        if action == "get":
            return self._workspace_info()
        if action == "set_cwd":
            self.cwd = self.paths.resolve(path, access="read", must_exist=True)
            if not self.cwd.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {self.cwd}")
            return self._workspace_info()
        raise RuntimeFault("invalid_action", "workspace action must be get or set_cwd")

    def _workspace_info(self):
        git_root = None
        try:
            result = subprocess.run(
                ["git", "-C", str(self.cwd), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
                env=safe_environment(self.cfg),
            )
            if result.returncode == 0:
                git_root = result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {"workspace_root": str(self.paths.workspace), "cwd": str(self.cwd), "repository_root": git_root}

    def filesystem(self, action, path=".", content=None, destination=None, recursive=False,
                   encoding="utf-8", offset=0, max_bytes=None, expected_occurrences=None,
                   old_text=None, new_text=None):
        access = "read" if action in {"read", "list", "stat"} else "write"
        must_exist = action not in {"write", "mkdir"}
        target = self.paths.resolve(path, cwd=self.cwd, access=access, must_exist=must_exist)
        self.policy.authorize_path(action)
        if action == "read":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            cap = min(int(max_bytes or self.cfg.max_file_read_bytes), self.cfg.max_file_read_bytes)
            offset = max(0, int(offset))
            with target.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(cap + 1)
            truncated = len(data) > cap
            data = data[:cap]
            if b"\x00" in data:
                raise RuntimeFault("binary_file", "Binary file read is not supported")
            return {"path": str(target), "content": data.decode(encoding, "replace"), "offset": offset,
                    "bytes_returned": len(data), "size": target.stat().st_size, "truncated": truncated}
        if action == "list":
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {target}")
            entries = []
            for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                try:
                    stat = item.stat()
                    entries.append({"name": item.name, "path": str(item), "type": "dir" if item.is_dir() else "file",
                                    "size": stat.st_size if item.is_file() else None, "modified": stat.st_mtime})
                except OSError as e:
                    entries.append({"name": item.name, "path": str(item), "type": "unavailable", "error": str(e)})
            return {"path": str(target), "entries": entries}
        if action == "stat":
            stat = target.stat()
            return {"path": str(target), "type": "dir" if target.is_dir() else "file",
                    "size": stat.st_size, "modified": stat.st_mtime, "created": stat.st_ctime}
        if action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content or ""
            self._atomic_write(target, data, encoding)
            return {"path": str(target), "bytes": len(data.encode(encoding))}
        if action == "replace_text":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if old_text is None or new_text is None:
                raise RuntimeFault("invalid_arguments", "replace_text requires old_text and new_text")
            original = target.read_text(encoding=encoding)
            count = original.count(old_text)
            expected = 1 if expected_occurrences is None else int(expected_occurrences)
            if count != expected:
                raise RuntimeFault("content_mismatch", f"Expected {expected} occurrence(s), found {count}")
            updated = original.replace(old_text, new_text)
            self._atomic_write(target, updated, encoding)
            return {"path": str(target), "replacements": count, "bytes": len(updated.encode(encoding))}
        if action == "mkdir":
            target.mkdir(parents=recursive, exist_ok=True)
            return {"path": str(target)}
        if action == "delete":
            count = sum(1 for _ in target.rglob("*")) + 1 if target.is_dir() else 1
            if target.is_dir():
                shutil.rmtree(target) if recursive else target.rmdir()
            else:
                target.unlink()
            return {"path": str(target), "deleted_items": count}
        if action == "move":
            if not destination:
                raise RuntimeFault("invalid_arguments", "move requires destination")
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("move")
            target.replace(dest)
            return {"source": str(target), "destination": str(dest)}
        raise RuntimeFault("invalid_action", f"Unknown filesystem action: {action}")

    @staticmethod
    def _atomic_write(target, data, encoding):
        fd, temp = tempfile.mkstemp(prefix=".localforge-", dir=target.parent)
        os.close(fd)
        try:
            Path(temp).write_text(data, encoding=encoding)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def search(self, query=None, path=".", glob=None, exclude=None, case_sensitive=False,
               max_results=200, files_only=False, fixed_string=True):
        root = self.paths.resolve(path, cwd=self.cwd, access="read", must_exist=True)
        if not root.is_dir():
            raise RuntimeFault("not_directory", f"Not a directory: {root}")
        maximum = max(1, min(int(max_results), 5000))
        rg = shutil.which("rg")
        if rg:
            return self._search_rg(rg, root, query, glob or [], exclude or [], case_sensitive, maximum, files_only, fixed_string)
        return self._search_python(root, query, glob or [], exclude or [], case_sensitive, maximum, files_only)

    def _search_rg(self, rg, root, query, includes, excludes, case_sensitive, maximum, files_only, fixed_string):
        command = [rg, "--hidden"]
        for pattern in includes:
            command += ["-g", pattern]
        for pattern in excludes:
            command += ["-g", "!" + pattern]
        if files_only:
            command += ["--files", str(root)]
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                    env=safe_environment(self.cfg))
            paths = result.stdout.splitlines()
            return {"engine": "ripgrep", "results": [{"path": p} for p in paths[:maximum]],
                    "exit_code": result.returncode, "truncated": len(paths) > maximum}
        if query is None:
            raise RuntimeFault("invalid_arguments", "Content search requires query")
        command += ["--json"]
        if fixed_string:
            command.append("-F")
        if not case_sensitive:
            command.append("-i")
        command += [query, str(root)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                env=safe_environment(self.cfg))
        matches = []
        for line in result.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            matches.append({"path": data["path"].get("text"), "line": data["line_number"],
                            "text": data["lines"].get("text", "").rstrip("\r\n")})
            if len(matches) >= maximum:
                break
        return {"engine": "ripgrep", "results": matches, "exit_code": result.returncode,
                "truncated": len(matches) >= maximum}

    @staticmethod
    def _search_python(root, query, includes, excludes, case_sensitive, maximum, files_only):
        results = []
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(root).as_posix()
            if excludes and any(item.match(pattern) or Path(rel).match(pattern) for pattern in excludes):
                continue
            if includes and not any(item.match(pattern) or Path(rel).match(pattern) for pattern in includes):
                continue
            if files_only:
                results.append({"path": str(item)})
            elif query is not None:
                try:
                    for number, line in enumerate(item.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        haystack, needle = (line, query) if case_sensitive else (line.lower(), query.lower())
                        if needle in haystack:
                            results.append({"path": str(item), "line": number, "text": line})
                            if len(results) >= maximum:
                                break
                except OSError:
                    pass
            if len(results) >= maximum:
                break
        return {"engine": "python", "results": results, "truncated": len(results) >= maximum}

    def git(self, action, args=None, cwd=None):
        work = self.paths.resolve(cwd or self.cwd, access="read", must_exist=True)
        presets = {
            "status": ["status", "--porcelain=v2", "--branch"], "diff": ["diff"],
            "log": ["log", "--oneline", "-n", "20"], "show": ["show"],
            "branch": ["branch"], "root": ["rev-parse", "--show-toplevel"],
        }
        subcommand = list(args or []) if action == "run" else presets.get(action)
        if subcommand is None:
            raise RuntimeFault("invalid_action", f"Unknown git action: {action}")
        return self.execute(["git", "-C", str(work)] + subcommand + ([] if action == "run" else list(args or [])), cwd=str(work))

    @staticmethod
    def _creation():
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}

    @staticmethod
    def _kill_tree(proc):
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def execute(self, command, cwd=None, timeout=None, shell=False, env=None):
        work = self.paths.resolve(cwd or self.cwd, access="read", must_exist=True)
        self.policy.authorize_command(command, work, shell)
        started = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(command_for_spawn(self.cfg, command, shell), cwd=work, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=safe_environment(self.cfg, env), **self._creation())
            stdout, stderr = proc.communicate(timeout=float(timeout or self.cfg.default_timeout_seconds))
            error_type = None if proc.returncode == 0 else "process_exit"
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            stdout, stderr = proc.communicate()
            error_type = "timeout"
        except OSError as e:
            return {"success": False, "command": command, "cwd": str(work), "exit_code": None,
                    "stdout": "", "stderr": str(e), "duration_ms": int((time.monotonic() - started) * 1000),
                    "error_type": "process_start"}
        out, err = redact(stdout.decode("utf-8", "replace")), redact(stderr.decode("utf-8", "replace"))
        cap = self.cfg.max_capture_bytes
        return {"success": proc.returncode == 0 and error_type is None, "command": command, "cwd": str(work),
                "exit_code": proc.returncode, "stdout": out[:cap], "stderr": err[:cap],
                "stdout_truncated": len(out.encode("utf-8")) > cap, "stderr_truncated": len(err.encode("utf-8")) > cap,
                "duration_ms": int((time.monotonic() - started) * 1000), "error_type": error_type}
