from __future__ import annotations
from pathlib import Path
import fnmatch
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from .errors import RuntimeFault
from .patch import build_new as build_patched
from .patch import parse as parse_diff
from .state import StateStore
from .security import coerce_command, command_for_spawn, redact, safe_environment

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


def _clip(data: str, cap: int):
    raw = data.encode("utf-8")
    if len(raw) <= cap:
        return data, False
    cut = raw[:cap].decode("utf-8", "replace")
    while len(cut.encode("utf-8")) > cap:
        cut = cut[:-1]
    return cut, True

DEFAULT_EXCLUDES = [".git/**", ".venv/**", "__pycache__/**", "node_modules/**", ".hg/**",
                    "target/**", "dist/**", "build/**", "*.egg-info/**"]

class Capabilities:
    def __init__(self, cfg, paths, policy, processes):
        self.cfg, self.paths, self.policy, self.processes = cfg, paths, policy, processes
        self.cwd = paths.workspace
        self.store = StateStore(cfg.state_file) if cfg.state_file else None
        if self.store is not None:
            saved = self.store.load().get("cwd")
            if isinstance(saved, str) and saved:
                try:
                    self.cwd = self.paths.resolve(saved, access="read", must_exist=True)
                except RuntimeFault:
                    self.cwd = paths.workspace

    def workspace(self, action, path=None):
        if action == "get":
            return self._workspace_info()
        if action == "set_cwd":
            self.cwd = self.paths.resolve(path, access="read", must_exist=True)
            if not self.cwd.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {self.cwd}")
            if self.store is not None:
                self.store.save_cwd(str(self.cwd))
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
                   old_text=None, new_text=None, line_start=None, line_end=None,
                   limit=200, glob=None, include_hidden=False, patch=None):
        if action == "copy":
            if not destination:
                raise RuntimeFault("invalid_arguments", "copy requires destination")
            src = self.paths.resolve(path, cwd=self.cwd, access="read", must_exist=True)
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("copy")
            try:
                if src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    return {"source": str(src), "destination": str(dest), "bytes": dest.stat().st_size}
                if src.is_dir():
                    if not recursive:
                        raise RuntimeFault("invalid_arguments", "copying a directory requires recursive=true")
                    if dest.exists():
                        raise RuntimeFault("invalid_arguments", f"Copy destination already exists: {dest}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src, dest)
                    count = sum(1 for _ in dest.rglob("*")) + 1
                    return {"source": str(src), "destination": str(dest), "copied_items": count}
            except OSError as e:
                _fs_error("copy", src, e)
            raise RuntimeFault("not_file", f"Not a file or directory: {src}")
        access = "read" if action in {"read", "list", "stat"} else "write"
        must_exist = action not in {"write", "mkdir"}
        target = self.paths.resolve(path, cwd=self.cwd, access=access, must_exist=must_exist)
        self.policy.authorize_path(action)
        if action == "read":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if line_start is not None or line_end is not None:
                start = max(1, int(line_start or 1))
                with target.open("rb") as handle:
                    head = handle.read(8192)
                    if b"\x00" in head:
                        raise RuntimeFault("binary_file", "Binary file read is not supported")
                try:
                    text = target.read_text(encoding=encoding, errors="replace")
                except OSError as e:
                    _fs_error("read", target, e)
                split = text.splitlines()
                total = len(split)
                end = min(int(line_end) if line_end is not None else total, total)
                if end < start:
                    raise RuntimeFault("invalid_arguments", "line_end must be >= line_start")
                picked = [{"no": n, "text": line} for n, line in enumerate(split, 1) if start <= n <= end]
                return {"path": str(target), "lines": picked, "line_start": start,
                        "line_end": end, "total_lines": total, "truncated": total > end}
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
        if action == "stat":
            try:
                stat = target.stat()
            except OSError as e:
                _fs_error("stat", target, e)
            return {"path": str(target), "type": "dir" if target.is_dir() else "file",
                    "size": stat.st_size, "modified": stat.st_mtime, "created": stat.st_ctime}
        if action == "write":
            data = content or ""
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(target, data, encoding)
            except OSError as e:
                _fs_error("write", target, e)
            return {"path": str(target), "bytes": len(data.encode(encoding))}
        if action == "replace_text":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if old_text is None or new_text is None:
                raise RuntimeFault("invalid_arguments", "replace_text requires old_text and new_text")
            if old_text == new_text:
                raise RuntimeFault("invalid_arguments", "old_text and new_text are identical; nothing would change")
            try:
                original = target.read_text(encoding=encoding)
            except OSError as e:
                _fs_error("replace_text", target, e)
            count = original.count(old_text)
            expected = 1 if expected_occurrences is None else int(expected_occurrences)
            if count != expected:
                raise RuntimeFault("content_mismatch", f"Expected {expected} occurrence(s), found {count}. "
                    "Pass expected_occurrences to replace all of them at once.")
            updated = original.replace(old_text, new_text)
            try:
                self._atomic_write(target, updated, encoding)
            except OSError as e:
                _fs_error("replace_text", target, e)
            return {"path": str(target), "replacements": count, "bytes": len(updated.encode(encoding))}
        if action == "apply_patch":
            if patch is None:
                raise RuntimeFault("invalid_arguments", "apply_patch requires patch")
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Patch base is not a directory: {target}")
            self.policy.authorize_path("apply_patch")
            planned = []
            for fp in parse_diff(patch):
                rel = fp.new_path if fp.new_path != "/dev/null" else fp.old_path
                dest = self.paths.resolve(rel, cwd=target, access="write", must_exist=False)
                creating = fp.old_path == "/dev/null"
                if creating:
                    if dest.exists():
                        raise RuntimeFault("invalid_arguments", f"Patch creates existing file: {dest}")
                    original = ""
                else:
                    if not dest.exists():
                        raise RuntimeFault("path_not_found", f"Path not found: {dest}")
                    if not dest.is_file():
                        raise RuntimeFault("not_file", f"Not a file: {dest}")
                    try:
                        raw = dest.read_bytes()
                    except OSError as e:
                        _fs_error("apply_patch", dest, e)
                    if b"\x00" in raw[:8192]:
                        raise RuntimeFault("binary_file", "Binary file patch is not supported")
                    original = raw.decode(encoding, "replace")
                planned.append((dest, build_patched(original, fp, rel), creating))
            results = []
            for dest, updated, creating in planned:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self._atomic_write(dest, updated, encoding)
                except OSError as e:
                    _fs_error("apply_patch", dest, e)
                results.append({"path": str(dest), "created": creating,
                                "bytes": len(updated.encode(encoding))})
            return {"base": str(target), "files": results}
        if action == "mkdir":
            try:
                target.mkdir(parents=recursive, exist_ok=True)
            except OSError as e:
                _fs_error("mkdir", target, e)
            return {"path": str(target)}
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
                return self._search_rg_file(rg, root, query, case_sensitive, maximum, files_only, fixed_string, context, applied)
            return self._search_python_file(root, query, case_sensitive, maximum, files_only, context, int(max_file_size_bytes), applied)
        if not root.is_dir():
            raise RuntimeFault("not_directory", f"Not a directory: {root}")
        if rg:
            return self._search_rg(rg, root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, bool(include_hidden))
        return self._search_python(root, query, glob or [], applied, case_sensitive, maximum, files_only, context, int(max_file_size_bytes), bool(include_hidden))

    @staticmethod
    def _rg_error(result, cap=500):
        if result.returncode in (0, 1):
            return None
        text = (result.stderr or "").strip()
        return text[:cap] if text else f"ripgrep exited with code {result.returncode}"

    def _search_rg(self, rg, root, query, includes, excludes, case_sensitive, maximum, files_only, fixed_string, context, include_hidden):
        command = [rg]
        if include_hidden:
            command.append("--hidden")
        for pattern in includes:
            command += ["-g", pattern]
        for pattern in excludes:
            command += ["-g", "!" + pattern]
        if files_only:
            command += ["--files", str(root)]
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                    env=safe_environment(self.cfg))
            paths = result.stdout.splitlines()
            out = {"engine": "ripgrep", "results": [{"path": p} for p in paths[:maximum]],
                    "exit_code": result.returncode, "truncated": len(paths) > maximum,
                    "applied_excludes": excludes}
            err = self._rg_error(result)
            if err is not None:
                out["error"] = err
            return out
        if query is None:
            raise RuntimeFault("invalid_arguments", "Content search requires query")
        command += ["--json"]
        if fixed_string:
            command.append("-F")
        if not case_sensitive:
            command.append("-i")
        if context > 0:
            command += ["-C", str(context)]
        command += [query, str(root)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                env=safe_environment(self.cfg))
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        matches = self._rg_matches(events, maximum, context)
        out = {"engine": "ripgrep", "results": matches, "exit_code": result.returncode,
                "truncated": len(matches) >= maximum, "applied_excludes": excludes}
        err = self._rg_error(result)
        if err is not None:
            out["error"] = err
        return out

    @staticmethod
    def _rg_matches(events, maximum, context):
        matches = []
        for i, event in enumerate(events):
            if event.get("type") != "match":
                continue
            data = event["data"]
            entry = {"path": data["path"].get("text"), "line": data["line_number"],
                     "text": data["lines"].get("text", "").rstrip("\r\n")}
            if context > 0:
                before = [e["data"]["lines"].get("text", "").rstrip("\r\n") for e in events[max(0, i - context):i] if e.get("type") == "context"]
                after = [e["data"]["lines"].get("text", "").rstrip("\r\n") for e in events[i + 1:i + 1 + context] if e.get("type") == "context"]
                entry["context"] = before + after
            matches.append(entry)
            if len(matches) >= maximum:
                break
        return matches

    def _search_rg_file(self, rg, target, query, case_sensitive, maximum, files_only, fixed_string, context, applied):
        if files_only:
            return {"engine": "ripgrep", "results": [{"path": str(target)}], "exit_code": 0, "truncated": False, "applied_excludes": applied}
        if query is None:
            raise RuntimeFault("invalid_arguments", "Content search requires query")
        command = [rg, "--json"]
        if fixed_string:
            command.append("-F")
        if not case_sensitive:
            command.append("-i")
        if context > 0:
            command += ["-C", str(context)]
        command += [query, str(target)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                env=safe_environment(self.cfg))
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        matches = self._rg_matches(events, maximum, context)
        out = {"engine": "ripgrep", "results": matches, "exit_code": result.returncode,
                "truncated": len(matches) >= maximum, "applied_excludes": excludes}
        err = self._rg_error(result)
        if err is not None:
            out["error"] = err
        return out

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

    @staticmethod
    def _search_python(root, query, includes, excludes, case_sensitive, maximum, files_only, context, max_size, include_hidden):
        results = []
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(root).as_posix()
            if not include_hidden and any(part.startswith(".") for part in rel.split("/")):
                continue
            try:
                if item.stat().st_size > max_size:
                    continue
            except OSError:
                continue
            if excludes and any(item.match(pattern) or Path(rel).match(pattern) for pattern in excludes):
                continue
            if includes and not any(item.match(pattern) or Path(rel).match(pattern) for pattern in includes):
                continue
            if files_only:
                results.append({"path": str(item)})
            elif query is not None:
                try:
                    with item.open("rb") as handle:
                        head = handle.read(8192)
                        if b"\x00" in head:
                            continue
                        handle.seek(0)
                        data = handle.read(max_size + 1)
                    lines = data.decode("utf-8", "replace").splitlines()
                    for number, line in enumerate(lines, 1):
                        haystack, needle = (line, query) if case_sensitive else (line.lower(), query.lower())
                        if needle in haystack:
                            entry = {"path": str(item), "line": number, "text": line}
                            if context > 0:
                                entry["context"] = lines[max(0, number - 1 - context):number - 1] + lines[number:min(len(lines), number + context)]
                            results.append(entry)
                            if len(results) >= maximum:
                                break
                except OSError:
                    pass
            if len(results) >= maximum:
                break
        return {"engine": "python", "results": results, "truncated": len(results) >= maximum, "applied_excludes": excludes}

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
        if action == "run" and subcommand and subcommand[0] == "add":
            pathspec = [a for a in subcommand[1:] if not a.startswith("-") or a in {"-A", "."}]
            if any(a in {"-A", "--all", "."} for a in subcommand[1:]):
                raise RuntimeFault("invalid_arguments",
                    "Broad git add blocked (add -A / --all / . stages secrets and scratch dirs). "
                    "Stage explicit files instead, e.g. git add src/app.py.")
            for spec in pathspec:
                lowered = spec.lower()
                if lowered.endswith((".pfx", ".p12", ".pem", ".key")) or Path(spec).name.lower() in {"sects.txt"}:
                    raise RuntimeFault("secret_file",
                        f"Refusing to stage a likely secret: {spec}. Private keys must never enter git history.")
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

    def execute(self, command, cwd=None, timeout=None, shell=False, env=None, input=None):
        work = self.paths.resolve(cwd or self.cwd, access="read", must_exist=True)
        command = coerce_command(command)
        stdin_bytes = None
        if input is not None:
            if len(input) > 65536:
                raise RuntimeFault("invalid_arguments", "input exceeds 65536 chars")
            stdin_bytes = input.encode("utf-8")
        shell = bool(shell or isinstance(command, str))
        self.policy.authorize_command(command, work, shell)
        started = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(command_for_spawn(self.cfg, command, shell), cwd=work, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.PIPE if stdin_bytes is not None else None,
                                    env=safe_environment(self.cfg, env), **self._creation())
            stdout, stderr = proc.communicate(input=stdin_bytes, timeout=float(timeout or self.cfg.default_timeout_seconds))
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
        if not out and not err and (proc.returncode != 0 or error_type is not None):
            err = ("[no output captured on stdout/stderr; the child may have written directly "
                   "to the console or died during startup (e.g. missing environment)]")
        cap = self.cfg.max_capture_bytes
        out, out_cut = _clip(out, cap)
        err, err_cut = _clip(err, cap)
        return {"success": proc.returncode == 0 and error_type is None, "command": command, "cwd": str(work),
                "exit_code": proc.returncode, "stdout": out, "stderr": err,
                "stdout_truncated": out_cut, "stderr_truncated": err_cut,
                "duration_ms": int((time.monotonic() - started) * 1000), "error_type": error_type}
