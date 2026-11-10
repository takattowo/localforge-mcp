from __future__ import annotations
import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from .errors import RuntimeFault
from .job import adopt as job_adopt
from .patch import build_new as build_patched
from .patch import parse as parse_diff
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


def _lf(text: str):
    return text.replace("\r\n", "\n")


def _detect_eol(text: str):
    """Return the dominant line ending of decoded text; LF on a tie.

    None when the text has no line break, so it is written as sent.
    Runs on decoded text because in UTF-16 a CRLF is not b"\\r\\n".
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if not crlf and not lf:
        return None
    return "\r\n" if crlf > lf else "\n"


def _clip(data: str, cap: int):
    raw = data.encode("utf-8")
    if len(raw) <= cap:
        return data, False
    cut = raw[:cap].decode("utf-8", "replace")
    while len(cut.encode("utf-8")) > cap:
        cut = cut[:-1]
    return cut, True


def _clip_ends(data: str, cap: int):
    """Clip data to cap UTF-8 bytes, keeping the head and the tail.

    Builds and test runs usually print the decisive lines last, so a
    head-only cut hides them. Tiny caps fall back to head-only.
    """
    raw = data.encode("utf-8")
    if len(raw) <= cap:
        return data, False
    if cap < 400:
        return _clip(data, cap)
    head, _ = _clip(data, cap // 4)
    head_len = len(head.encode("utf-8"))
    # 64 bytes is room for the cut marker below.
    tail = raw[len(raw) - (cap - head_len - 64):].decode("utf-8", "ignore")
    cut = len(raw) - head_len - len(tail.encode("utf-8"))
    return f"{head}\n...[{cut} bytes cut]...\n{tail}", True

# **/ makes each one apply at any depth: ripgrep reads a glob with a
# slash as anchored to the search root, like a .gitignore line. The
# trailing slash means a folder, so ripgrep skips the whole folder
# instead of walking it, and a file named like one is kept.
DEFAULT_EXCLUDES = ["**/.git/", "**/.venv/", "**/__pycache__/", "**/node_modules/", "**/.hg/",
                    "**/target/", "**/dist/", "**/build/", "**/*.egg-info/"]

# Brace alternatives multiply; patterns come from the agent.
MAX_GLOB_EXPANSIONS = 64
_NEVER = re.compile(r"(?!)")


def _expand_braces(pattern):
    """Expand {a,b} alternatives into plain globs, at most MAX_GLOB_EXPANSIONS."""
    done, todo = [], [pattern]
    while todo:
        current = todo.pop()
        match = re.search(r"\{([^{}]*)\}", current)
        if not match:
            done.append(current)
        else:
            head, tail = current[:match.start()], current[match.end():]
            todo.extend(head + alt + tail for alt in match.group(1).split(","))
        if len(done) + len(todo) > MAX_GLOB_EXPANSIONS:
            raise RuntimeFault("invalid_arguments",
                               f"Glob {pattern!r} expands to more than {MAX_GLOB_EXPANSIONS} patterns")
    return done


def _glob_tokens(glob):
    """Split one brace-free glob into (kind, regex) tokens.

    kind is "star" (*), "dstar" (**), "sep" (anything that can match
    "/") or "lit" (anything matching one non-"/" character).
    """
    tokens, i = [], 0
    while i < len(glob):
        c = glob[i]
        if (glob.startswith("**", i) and (i == 0 or glob[i - 1] == "/")
                and (i + 2 == len(glob) or glob[i + 2] == "/")):
            last = i + 2 == len(glob)
            tokens.append(("dstar", ".*" if last else "(?:.*/)?"))
            i += 2 if last else 3
            continue
        if c == "*":
            # A run of stars inside a folder name is one star.
            while i + 1 < len(glob) and glob[i + 1] == "*":
                i += 1
            tokens.append(("star", "[^/]*"))
        elif c == "?":
            tokens.append(("lit", "[^/]"))
        elif c == "\\" and i + 1 < len(glob):
            i += 1
            tokens.append(("sep" if glob[i] == "/" else "lit", re.escape(glob[i])))
        elif c == "[" and (end := glob.find("]", i + (3 if glob[i + 1:i + 2] in ("!", "^") else 2))) != -1:
            # A ] right after [ or [! is part of the class.
            body = glob[i + 1:end].replace("\\", "\\\\").replace("[", "\\[")
            if body[0] in "!^":
                body = "^" + body[1:]
            # A negated class can match "/" too.
            tokens.append(("sep" if "/" in body or body[0] == "^" else "lit", "[" + body + "]"))
            i = end
        else:
            tokens.append(("sep" if c == "/" else "lit", re.escape(c)))
        i += 1
    return tokens


def _glob_body(glob):
    """Translate one brace-free glob to a regex that cannot blow up.

    A star followed by fixed text and then another star in the same
    folder name, or a ** followed by fixed folders and then another **,
    only needs the earliest match of that text: the next star can absorb
    anything after it. Committing to it with an atomic group keeps the
    match time linear instead of exponential in the number of stars.
    """
    tokens = _glob_tokens(glob)
    out, k = [], 0
    while k < len(tokens):
        kind, rx = tokens[k]
        middle_dstar = kind == "dstar" and rx != ".*"
        if kind == "star" or middle_dstar:
            fixed = ("lit",) if kind == "star" else ("lit", "sep")
            j = k + 1
            while j < len(tokens) and tokens[j][0] in fixed:
                j += 1
            if j > k + 1 and j < len(tokens) and tokens[j][0] == kind:
                lead = "[^/]*?" if kind == "star" else "(?:.*?/)??"
                out.append("(?>" + lead + "".join(r for _, r in tokens[k + 1:j]) + ")")
                k = j
                continue
        out.append(rx)
        k += 1
    return "".join(out)


def _glob_regex(pattern, folders=True):
    """Compile a glob the way ripgrep -g reads it, for root-relative posix paths.

    Like a .gitignore line: a glob with no slash matches at any depth, a
    leading or inner slash anchors it to the search root, a trailing slash
    means a folder, ** spans any number of folders, {a,b} alternates, and
    matching is case-sensitive. Anchoring and the trailing slash are read
    from the whole pattern before braces expand, as ripgrep does. Returns
    None for an empty pattern, which ripgrep ignores; a folder-only
    pattern never matches when folders is false (includes match files).
    """
    core = pattern.rstrip("/")
    if not core.strip("/"):
        return None
    anchored = "/" in core
    core = core.lstrip("/")
    if pattern.endswith("/"):
        if not folders:
            return _NEVER
        core += "/**"
    prefix = "" if anchored else "(?:.*/)?"
    parts = [prefix + _glob_body(glob) for glob in _expand_braces(core)]
    try:
        return re.compile("(?:" + "|".join(parts) + ")")
    except re.error as e:
        raise RuntimeFault("invalid_arguments", f"Invalid glob {pattern!r}: {e}") from e

MAX_BATCH_SEARCHES = 16
MAX_READ_LINES = 2000
LINE_CUT = " ...[line cut]"
BATCH_SEARCH_WORKERS = 8
GIT_ROOT_TTL_SECONDS = 30
PATH_LOCK_STRIPES = 256

class Capabilities:
    def __init__(self, cfg, paths, policy, processes, store=None):
        self.cfg, self.paths, self.policy, self.processes = cfg, paths, policy, processes
        self._cwd_lock = threading.Lock()
        self._cwd = paths.workspace
        # Striped path locks: a fixed-size table (bounded memory
        # over long sessions) where each path maps to one stripe
        # by hash. The same path always maps to the same stripe,
        # so same-file mutations still serialize; paths that share
        # a stripe serialize as a tradeoff, which is rare at the
        # default concurrency.
        self._path_locks = [threading.Lock() for _ in range(PATH_LOCK_STRIPES)]
        self._git_lock = threading.Lock()
        self._git_root_cache = (None, None, 0.0)
        self.store = store
        if self.store is not None:
            saved = self.store.load().get("cwd")
            if isinstance(saved, str) and saved:
                try:
                    self._cwd = self.paths.resolve(saved, access="read", must_exist=True)
                except RuntimeFault:
                    self._cwd = paths.workspace

    @property
    def cwd(self):
        with self._cwd_lock:
            return self._cwd

    def _path_lock(self, target):
        return self._path_locks[hash(str(target)) % PATH_LOCK_STRIPES]

    @contextlib.contextmanager
    def _locked_paths(self, targets):
        """Serialize mutations on the same path; distinct paths stay parallel.

        Mirrors how CLI agents serialize edits to one file while editing
        several files at once. Stripes are acquired in a process-stable
        order (sorted by object identity) so concurrent mutations of
        overlapping path sets cannot deadlock.
        """
        locks = sorted({self._path_lock(t) for t in targets}, key=id)
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def workspace(self, action, path=None):
        if action == "get":
            return self._workspace_info()
        if action == "set_cwd":
            resolved = self.paths.resolve(path, access="read", must_exist=True)
            if not resolved.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {resolved}")
            with self._cwd_lock:
                self._cwd = resolved
            if self.store is not None:
                self.store.save_cwd(str(resolved))
            return self._workspace_info()
        raise RuntimeFault("invalid_action", "workspace action must be get or set_cwd")

    def _workspace_info(self):
        return {"workspace_root": str(self.paths.workspace), "cwd": str(self.cwd),
                "repository_root": self._git_root(self.cwd)}

    def _git_root(self, cwd):
        # rev-parse costs a process spawn; cache the answer per
        # directory for a short TTL so repeated workspace calls in
        # the same directory do not pay it every time.
        key = str(cwd)
        now = time.monotonic()
        with self._git_lock:
            cached_cwd, cached_root, checked_at = self._git_root_cache
            if cached_cwd == key and now - checked_at < GIT_ROOT_TTL_SECONDS:
                return cached_root
        root = None
        try:
            result = subprocess.run(
                ["git", "-C", key, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
                env=safe_environment(self.cfg),
            )
            if result.returncode == 0:
                root = result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
        with self._git_lock:
            self._git_root_cache = (key, root, time.monotonic())
        return root

    def _guard_file_size(self, action, target):
        # replace_text and multi_edit read the whole file into
        # memory; refuse anything beyond the configured read cap
        # instead of an unbounded read.
        try:
            size = target.stat().st_size
        except OSError as e:
            _fs_error(action, target, e)
        if size > int(self.cfg.max_file_read_bytes):
            raise RuntimeFault("file_too_large",
                               f"{target} is {size} bytes; {action} is capped at "
                               f"max_file_read_bytes ({self.cfg.max_file_read_bytes}). "
                               "Raise the limit in the config for large files.")

    def filesystem(self, action, path=".", content=None, destination=None, recursive=False,
                   encoding="utf-8", offset=0, max_bytes=None, expected_occurrences=None,
                   old_text=None, new_text=None, edits=None, line_start=None, line_end=None,
                   limit=200, glob=None, include_hidden=False, patch=None):
        if action == "copy":
            if not destination:
                raise RuntimeFault("invalid_arguments", "copy requires destination")
            src = self.paths.resolve(path, cwd=self.cwd, access="read", must_exist=True)
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("copy")
            with self._locked_paths([src, dest]):
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
                return self._read_lines(target, encoding, line_start, line_end)
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
            with self._locked_paths([target]):
                try:
                    # An existing file keeps its line endings; a new
                    # file is written exactly as sent.
                    eol = None
                    try:
                        with target.open("r", encoding=encoding, errors="replace", newline="") as handle:
                            eol = _detect_eol(handle.read(65536))
                    except FileNotFoundError:
                        pass
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = self._atomic_write(target, data, encoding, eol)
                except OSError as e:
                    _fs_error("write", target, e)
            result = {"path": str(target), "bytes": written}
            if eol is not None:
                # Tells the agent its line endings were kept; delete
                # the file first to change them.
                result["eol"] = "CRLF" if eol == "\r\n" else "LF"
            return result
        if action == "replace_text":
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if old_text is None or new_text is None:
                raise RuntimeFault("invalid_arguments", "replace_text requires old_text and new_text")
            old_text, new_text = _lf(old_text), _lf(new_text)
            if old_text == new_text:
                raise RuntimeFault("invalid_arguments", "old_text and new_text are identical; nothing would change")
            self._guard_file_size("replace_text", target)
            expected = 1 if expected_occurrences is None else int(expected_occurrences)
            with self._locked_paths([target]):
                try:
                    original, eol = self._read_text(target, encoding)
                except OSError as e:
                    _fs_error("replace_text", target, e)
                updated, counts, write_eol = self._apply_edits(
                    original, eol, [(old_text, new_text, expected)],
                    lambda index, want, found: RuntimeFault(
                        "content_mismatch", f"Expected {want} occurrence(s), found {found}. "
                        "Pass expected_occurrences to replace all of them at once."))
                try:
                    written = self._atomic_write(target, updated, encoding, write_eol)
                except OSError as e:
                    _fs_error("replace_text", target, e)
            return {"path": str(target), "replacements": counts[0], "bytes": written}
        if action == "multi_edit":
            if not isinstance(edits, list) or not edits:
                raise RuntimeFault("invalid_arguments",
                                   "multi_edit requires a non-empty edits array, "
                                   'e.g. {"action": "multi_edit", "path": "src/app.py", '
                                   '"edits": [{"old_text": "foo", "new_text": "bar"}]}')
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            self._guard_file_size("multi_edit", target)
            for index, edit in enumerate(edits):
                if (not isinstance(edit, dict)
                        or not isinstance(edit.get("old_text"), str)
                        or not isinstance(edit.get("new_text"), str)):
                    raise RuntimeFault("invalid_arguments",
                                       f"edits[{index}] must be an object with "
                                       "old_text and new_text strings")
            specs = []
            for index, edit in enumerate(edits):
                old, new = _lf(edit["old_text"]), _lf(edit["new_text"])
                if old == new:
                    raise RuntimeFault("invalid_arguments",
                                       f"edits[{index}] old_text and new_text "
                                       "are identical; nothing would change")
                expected = edit.get("expected_occurrences")
                specs.append((old, new, 1 if expected is None else int(expected)))
            with self._locked_paths([target]):
                try:
                    original, eol = self._read_text(target, encoding)
                except OSError as e:
                    _fs_error("multi_edit", target, e)
                # Edits apply in order against the evolving content and
                # are all validated before anything is written: a
                # failing edit leaves the file untouched, so a
                # coherent multi-part change never lands half-applied.
                content, counts, write_eol = self._apply_edits(
                    original, eol, specs,
                    lambda index, want, found: RuntimeFault(
                        "content_mismatch", f"edits[{index}]: expected {want} "
                        f"occurrence(s), found {found}. Edits apply in order, so "
                        "earlier edits may have changed the text; pass "
                        "expected_occurrences when it repeats."))
                try:
                    written = self._atomic_write(target, content, encoding, write_eol)
                except OSError as e:
                    _fs_error("multi_edit", target, e)
            applied = [{"index": index, "replacements": count} for index, count in enumerate(counts)]
            return {"path": str(target), "edits": applied, "bytes": written}
        if action == "apply_patch":
            if patch is None:
                raise RuntimeFault("invalid_arguments", "apply_patch requires patch")
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Patch base is not a directory: {target}")
            self.policy.authorize_path("apply_patch")
            entries = list(parse_diff(patch))
            planned_paths = []
            for fp in entries:
                rel = fp.new_path if fp.new_path != "/dev/null" else fp.old_path
                planned_paths.append((fp, rel, self.paths.resolve(
                    rel, cwd=target, access="write", must_exist=False)))
            with self._locked_paths([dest for _, _, dest in planned_paths]):
                planned = []
                for fp, rel, dest in planned_paths:
                    creating = fp.old_path == "/dev/null"
                    if creating:
                        if dest.exists():
                            raise RuntimeFault("invalid_arguments", f"Patch creates existing file: {dest}")
                        original, eol = "", None
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
                        eol = _detect_eol(original)
                    planned.append((dest, build_patched(original, fp, rel, eol), creating))
                results = []
                for dest, updated, creating in planned:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # build_new keeps untouched lines byte-exact,
                        # so the result is written verbatim.
                        written = self._atomic_write(dest, updated, encoding)
                    except OSError as e:
                        _fs_error("apply_patch", dest, e)
                    results.append({"path": str(dest), "created": creating, "bytes": written})
            return {"base": str(target), "files": results}
        if action == "mkdir":
            with self._locked_paths([target]):
                try:
                    target.mkdir(parents=recursive, exist_ok=True)
                except OSError as e:
                    _fs_error("mkdir", target, e)
            return {"path": str(target)}
        if action == "delete":
            with self._locked_paths([target]):
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
            with self._locked_paths([target, dest]):
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    target.replace(dest)
                except OSError as e:
                    _fs_error("move", target, e)
            return {"source": str(target), "destination": str(dest)}
        raise RuntimeFault("invalid_action", f"Unknown filesystem action: {action}")

    def _read_lines(self, target, encoding, line_start, line_end):
        """Line-mode read as numbered text, one "N<TAB>line" per line.

        Reads in bounded pieces, so memory stays bounded however long
        a line or the file is, and stops at MAX_READ_LINES lines or
        max_read_chars. next_line says where to continue; total_lines
        appears only when the read reaches the end of the file.
        """
        start = max(1, int(line_start or 1))
        last = int(line_end) if line_end is not None else None
        if last is not None and last < start:
            raise RuntimeFault("invalid_arguments", "line_end must be >= line_start")
        budget = int(self.cfg.max_read_chars)
        try:
            with target.open("rb") as handle:
                if b"\x00" in handle.read(8192):
                    raise RuntimeFault("binary_file", "Binary file read is not supported")
            out, used, n, end, more = [], 0, 0, start - 1, False
            # newline="\n": only LF ends a line, like ripgrep, so a
            # lone CR stays inside its line.
            with target.open("r", encoding=encoding, errors="replace", newline="\n") as handle:
                while True:
                    line = handle.readline(budget + 1)
                    if not line:
                        break
                    n += 1
                    if not line.endswith("\n"):
                        # Over-long line: skip the rest without
                        # holding it in memory.
                        while (rest := handle.readline(65536)) and not rest.endswith("\n"):
                            pass
                    if n < start:
                        continue
                    text = f"{n}\t" + line.rstrip("\r\n")
                    if len(text) > budget:
                        text = text[:max(0, budget - len(LINE_CUT))] + LINE_CUT
                    if out and (len(out) >= MAX_READ_LINES or used + len(text) + 1 > budget):
                        more = True
                        break
                    out.append(text)
                    used += len(text) + 1
                    end = n
                    if last is not None and n >= last:
                        more = bool(handle.read(1))
                        break
        except OSError as e:
            _fs_error("read", target, e)
        if not more and start > max(n, 1):
            raise RuntimeFault("invalid_arguments",
                               f"line_start {start} is past the end of the file ({n} lines)")
        result = {"path": str(target), "content": "\n".join(out), "line_start": start,
                  "line_end": end, "truncated": more}
        if more:
            result["next_line"] = end + 1
        else:
            result["total_lines"] = n
        return result

    @staticmethod
    def _read_text(target, encoding):
        """Read a file as text, line endings untouched, plus its dominant ending."""
        text = target.read_bytes().decode(encoding)
        return text, _detect_eol(text)

    @staticmethod
    def _apply_edits(original, eol, edits, mismatch):
        """Apply (old, new, expected) edits in order, all-or-nothing.

        Edit strings arrive with LF line breaks. The LF-normalized view
        is what the agent reads, so it decides match counts and errors.
        The same edits also run on the file text as-is, converted to
        its dominant line ending; that result is used, and written
        verbatim, only when it makes the same change, so untouched
        lines keep their exact bytes (mixed endings, stray CRs) but it
        never produces a different result in the LF view or splits a
        CRLF pair.
        Otherwise the normalized result is written back with the
        dominant ending.
        Returns (content, counts, eol_for_write); mismatch(index,
        expected, found) builds the error when the LF view does not match.
        """
        def run(text, edit_eol):
            counts = []
            for index, (old, new, expected) in enumerate(edits):
                if edit_eol == "\r\n":
                    old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
                found = text.count(old)
                if found != expected:
                    return None, (index, expected, found)
                text = text.replace(old, new)
                counts.append(found)
            return text, counts

        normalized, counts = run(_lf(original), None)
        if normalized is None:
            raise mismatch(*counts)
        exact, _ = run(original, eol)
        if exact is not None and _lf(exact) == normalized:
            return exact, counts, None
        return normalized, counts, eol

    @staticmethod
    def _atomic_write(target, data, encoding, eol=None):
        """Write data atomically and return the bytes written.

        eol "\n" or "\r\n" normalizes every line ending to it; None
        writes data verbatim. Bytes are written directly, so Python
        never translates LF to the platform line ending.
        """
        if eol is not None:
            data = _lf(data)
            if eol == "\r\n":
                data = data.replace("\n", "\r\n")
        raw = data.encode(encoding)
        fd, temp = tempfile.mkstemp(prefix=".localforge-", dir=target.parent)
        os.close(fd)
        try:
            Path(temp).write_bytes(raw)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return len(raw)

    def search(self, query=None, path=".", glob=None, exclude=None, case_sensitive=False,
               max_results=200, files_only=False, fixed_string=True, context_lines=0,
               include_hidden=False, max_file_size_bytes=1_000_000):
        if int(max_file_size_bytes) <= 0:
            raise RuntimeFault("invalid_arguments", "max_file_size_bytes must be positive")
        context = max(0, min(int(context_lines or 0), 5))
        if files_only and query is not None:
            raise RuntimeFault("invalid_arguments", "files_only search takes no query")
        applied = list(dict.fromkeys([*(exclude or []), *DEFAULT_EXCLUDES]))
        # Read the cwd once: a parallel set_cwd must not change the
        # base between resolving the root and shortening result paths.
        base = self.cwd
        root = self.paths.resolve(path, cwd=base, access="read", must_exist=True)
        maximum = max(1, min(int(max_results), 5000))
        rg = shutil.which("rg")
        if root.is_file():
            if rg:
                return self._short_paths(base, self._search_rg_file(rg, root, query, case_sensitive, maximum, files_only, fixed_string, context, applied))
            return self._short_paths(base, self._search_python_file(root, query, case_sensitive, maximum, files_only, fixed_string, context, int(max_file_size_bytes), applied))
        if not root.is_dir():
            raise RuntimeFault("not_directory", f"Not a directory: {root}")
        if rg:
            return self._short_paths(base, self._search_rg(rg, root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, bool(include_hidden)))
        return self._short_paths(base, self._search_python(root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, int(max_file_size_bytes), bool(include_hidden)))

    @staticmethod
    def _short_paths(base, out):
        """Show result paths relative to the runtime cwd when under it.

        Filesystem tools resolve relative paths against the same cwd,
        so the short form stays usable and saves tokens per match.
        """
        for entry in out.get("results", []):
            try:
                entry["path"] = str(Path(entry["path"]).relative_to(base))
            except (KeyError, TypeError, ValueError):
                pass
        return out

    def multi_search(self, searches):
        """Run several independent searches in parallel within one tool call.

        Clients that issue one tool call per model round-trip use this to
        fan out multiple queries without paying a round-trip per search.
        Each entry takes the same arguments as the search tool plus an
        optional label; failures are isolated per entry.
        """
        if not isinstance(searches, list) or not searches:
            raise RuntimeFault("invalid_arguments",
                               "multi_search requires a non-empty searches array, "
                               "e.g. {\"searches\": [{\"query\": \"todo\"}]}")
        if len(searches) > MAX_BATCH_SEARCHES:
            raise RuntimeFault("invalid_arguments",
                               f"multi_search accepts at most {MAX_BATCH_SEARCHES} searches")
        specs = []
        for index, spec in enumerate(searches):
            if not isinstance(spec, dict):
                raise RuntimeFault("invalid_arguments",
                                   f"searches[{index}] must be an object with "
                                   "the search tool's arguments")
            specs.append(dict(spec))
        results: list[dict] = [None] * len(specs)

        def run(index, spec):
            label = spec.pop("label", None)
            try:
                results[index] = {"index": index, "label": label, **self.search(**spec)}
            except Exception as e:
                payload = e.as_dict() if isinstance(e, RuntimeFault) else {
                    "code": "invalid_arguments", "message": str(e)}
                results[index] = {"index": index, "label": label, "error": payload}

        workers = min(len(specs), BATCH_SEARCH_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, index, spec) for index, spec in enumerate(specs)]
            for future in futures:
                future.result()
        return {"results": results}

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
            # cwd=root: ripgrep matches globs with a folder against the
            # path relative to its working directory, not the search root.
            result = subprocess.run(command, cwd=str(root), capture_output=True, text=True, errors="replace", timeout=60,
                                    stdin=subprocess.DEVNULL,
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
        result = subprocess.run(command, cwd=str(root), capture_output=True, text=True, errors="replace", timeout=60,
                                stdin=subprocess.DEVNULL,
                                env=safe_environment(self.cfg))
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        # One extra match tells a full page from a cut one.
        matches = self._rg_matches(events, maximum + 1, context)
        out = {"engine": "ripgrep", "results": matches[:maximum], "exit_code": result.returncode,
                "truncated": len(matches) > maximum, "applied_excludes": excludes}
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
                entry["before"] = Capabilities._rg_context(events, i, -1, context)
                entry["after"] = Capabilities._rg_context(events, i, 1, context)
            matches.append(entry)
            if len(matches) >= maximum:
                break
        return matches

    @staticmethod
    def _rg_context(events, i, step, limit):
        # Walk neighboring context and match lines, like the Python
        # engine; begin/end events mark a file boundary, so context
        # never bleeds into another file.
        lines, j = [], i + step
        while 0 <= j < len(events) and len(lines) < limit and events[j].get("type") in ("context", "match"):
            data = events[j]["data"]
            lines.append(f"{data['line_number']}\t" + data["lines"].get("text", "").rstrip("\r\n"))
            j += step
        return lines[::-1] if step < 0 else lines

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
                                stdin=subprocess.DEVNULL,
                                env=safe_environment(self.cfg))
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        matches = self._rg_matches(events, maximum + 1, context)
        out = {"engine": "ripgrep", "results": matches[:maximum], "exit_code": result.returncode,
                "truncated": len(matches) > maximum, "applied_excludes": applied}
        err = self._rg_error(result)
        if err is not None:
            out["error"] = err
        return out

    def _search_python_file(self, target, query, case_sensitive, maximum, files_only, fixed_string, context, max_size, applied):
        if files_only:
            return {"engine": "python", "results": [{"path": str(target)}], "truncated": False, "applied_excludes": applied}
        if query is None:
            raise RuntimeFault("invalid_arguments", "Content search requires query")
        try:
            if target.stat().st_size > max_size:
                return {"engine": "python", "results": [], "truncated": False, "applied_excludes": applied}
        except OSError:
            return {"engine": "python", "results": [], "truncated": False, "applied_excludes": applied}
        return self._search_python(target, query, [], applied, case_sensitive, maximum, False, fixed_string, context, max_size, True)

    @staticmethod
    def _compile_fallback(query, fixed_string, case_sensitive):
        if fixed_string:
            return None
        try:
            return re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise RuntimeFault("invalid_arguments", f"Invalid regex: {e}") from e

    @staticmethod
    def _search_python(root, query, includes, excludes, case_sensitive, maximum, files_only, fixed_string, context, max_size, include_hidden):
        results = []
        pattern = Capabilities._compile_fallback(query, fixed_string, case_sensitive) if query is not None else None
        include_res = [r for r in (_glob_regex(p, folders=False) for p in includes) if r is not None]
        excluded = [r for r in (_glob_regex(p) for p in excludes) if r is not None]
        # One combined regex: one match per path instead of one per exclude.
        exclude_re = re.compile("|".join(f"(?:{r.pattern})" for r in excluded)) if excluded else None
        # root is a file for a single-file search: read only that file.
        for item in ([root] if root.is_file() else root.rglob("*")):
            if not item.is_file():
                continue
            rel = item.name if item == root else item.relative_to(root).as_posix()
            if not include_hidden and any(part.startswith(".") for part in rel.split("/")):
                continue
            try:
                if item.stat().st_size > max_size:
                    continue
            except OSError:
                continue
            # Excludes follow .gitignore: a matching folder hides all it
            # holds. An explicit file path ignores them, as in ripgrep.
            if exclude_re is not None and item != root:
                segments = rel.split("/")
                ancestors = ("/".join(segments[:n]) for n in range(1, len(segments) + 1))
                if any(exclude_re.fullmatch(path) for path in ancestors):
                    continue
            if include_res and not any(r.fullmatch(rel) for r in include_res):
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
                    text = data.decode("utf-8", "replace")
                    # Split on LF only, like ripgrep, so a lone CR or a
                    # form feed does not shift line numbers.
                    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
                    if not text or text.endswith("\n"):
                        lines.pop()
                    for number, line in enumerate(lines, 1):
                        if pattern is not None:
                            matched = pattern.search(line) is not None
                        elif case_sensitive:
                            matched = query in line
                        else:
                            matched = query.lower() in line.lower()
                        if matched:
                            entry = {"path": str(item), "line": number, "text": line}
                            if context > 0:
                                entry["before"] = [f"{n}\t{lines[n - 1]}" for n in range(max(1, number - context), number)]
                                entry["after"] = [f"{n}\t{lines[n - 1]}" for n in range(number + 1, min(len(lines), number + context) + 1)]
                            results.append(entry)
                            if len(results) > maximum:
                                break
                except OSError:
                    pass
            if len(results) > maximum:
                break
        # Collected up to one extra result: it marks a cut page.
        return {"engine": "python", "results": results[:maximum], "truncated": len(results) > maximum, "applied_excludes": excludes}

    def git(self, action, args=None, cwd=None):
        work = self.paths.resolve(cwd or ".", cwd=self.cwd, access="read", must_exist=True)
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
        work = self.paths.resolve(cwd or ".", cwd=self.cwd, access="read", must_exist=True)
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
        effective_timeout = float(timeout or self.cfg.default_timeout_seconds)
        try:
            proc = subprocess.Popen(command_for_spawn(self.cfg, command, shell), cwd=work, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                                    env=safe_environment(self.cfg, env), **self._creation())
            if self.cfg.kill_children_on_exit:
                # Kernel-side cleanup: the child dies with
                # this process however it dies, not just on
                # a clean exit (see agent_runtime/job.py).
                job_adopt(proc.pid)
            stdout, stderr = proc.communicate(input=stdin_bytes, timeout=effective_timeout)
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
        out, out_cut = _clip_ends(out, cap)
        err, err_cut = _clip_ends(err, cap)
        result = {"success": proc.returncode == 0 and error_type is None, "command": command, "cwd": str(work),
                "exit_code": proc.returncode, "stdout": out, "stderr": err,
                "stdout_truncated": out_cut, "stderr_truncated": err_cut,
                "duration_ms": int((time.monotonic() - started) * 1000), "error_type": error_type}
        if timeout is not None and effective_timeout > float(self.cfg.default_timeout_seconds):
            result["warning"] = (
                "timeout exceeds the configured default "
                f"({self.cfg.default_timeout_seconds} s); long foreground waits risk "
                "gateway/outpost timeouts and block the server — use the process tool for waits and polling.")
        return result
