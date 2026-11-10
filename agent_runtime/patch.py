from __future__ import annotations
from dataclasses import dataclass, field
import re
from .errors import RuntimeFault

MAX_DIFF_CHARS = 262144
MAX_FILES = 10

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"


@dataclass
class Hunk:
    header: str
    old_start: int
    old_len: int
    ops: list[str] = field(default_factory=list)  # ' ', '-', '+'
    texts: list[str] = field(default_factory=list)
    newlines: list[bool] = field(default_factory=list)  # trailing "\n" present per line


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk]


def _strip_prefix(path):
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


def _check_rel(path):
    if not path or path == "/dev/null":
        return
    if "\x00" in path:
        raise RuntimeFault("invalid_arguments", "Patch path contains NUL")
    p = path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise RuntimeFault("invalid_arguments", f"Patch path must be relative: {path}")
    if ".." in p.split("/"):
        raise RuntimeFault("invalid_arguments", f"Patch path escapes base: {path}")


def parse(text):
    if not isinstance(text, str) or not text.strip():
        raise RuntimeFault("invalid_arguments", "patch must be a non-empty unified diff string")
    if len(text) > MAX_DIFF_CHARS:
        raise RuntimeFault("invalid_arguments", f"patch exceeds {MAX_DIFF_CHARS} chars")
    lines = text.replace("\r\n", "\n").split("\n")
    files = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i] == "" and i == n - 1:
            break
        if not lines[i].startswith("--- "):
            raise RuntimeFault("invalid_arguments", f"Expected '--- ' header at diff line {i + 1}")
        old_raw = lines[i][4:].split("\t")[0].strip()
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise RuntimeFault("invalid_arguments", f"Expected '+++ ' header at diff line {i + 1}")
        new_raw = lines[i][4:].split("\t")[0].strip()
        i += 1
        old_path, new_path = _strip_prefix(old_raw), _strip_prefix(new_raw)
        _check_rel(old_path)
        _check_rel(new_path)
        if old_path != "/dev/null" and new_path == "/dev/null":
            raise RuntimeFault("invalid_arguments",
                               f"Patch deletes {old_path}; use the delete action instead")
        hunks = []
        while i < n and lines[i].startswith("@@"):
            match = _HUNK.match(lines[i])
            if not match:
                raise RuntimeFault("invalid_arguments", f"Bad hunk header at diff line {i + 1}: {lines[i]}")
            header = lines[i]
            old_start = int(match.group(1))
            old_len = int(match.group(2)) if match.group(2) is not None else 1
            i += 1
            hunk = Hunk(header=header, old_start=old_start, old_len=old_len)
            seen_old = 0
            while i < n and not lines[i].startswith(("--- ", "+++ ", "@@", "diff --git")) \
                    and (lines[i][:1] in (" ", "-", "+") or lines[i] == _NO_NEWLINE):
                if lines[i] == _NO_NEWLINE:
                    if not hunk.ops:
                        raise RuntimeFault("invalid_arguments",
                                           f"Stray no-newline marker at diff line {i + 1}")
                    hunk.newlines[-1] = False
                    i += 1
                    continue
                op, content = lines[i][0], lines[i][1:]
                if op not in (" ", "-", "+"):
                    break
                if op in (" ", "-"):
                    seen_old += 1
                hunk.ops.append(op)
                hunk.texts.append(content)
                hunk.newlines.append(True)
                i += 1
            if seen_old != old_len:
                raise RuntimeFault("invalid_arguments",
                                   f"Hunk expects {old_len} old lines, found {seen_old}: {header}")
            hunks.append(hunk)
        if not hunks:
            raise RuntimeFault("invalid_arguments", f"No hunks for {new_raw}")
        files.append(FilePatch(old_path=old_path, new_path=new_path, hunks=hunks))
    if not files:
        raise RuntimeFault("invalid_arguments", "patch contains no files")
    if len(files) > MAX_FILES:
        raise RuntimeFault("invalid_arguments", f"patch touches {len(files)} files, max {MAX_FILES}")
    return files


def _split_lines(text):
    """Split on LF only, keeping each line's ending.

    str.splitlines also breaks on CR, form feed, and other separators,
    which would misnumber lines and rewrite their endings.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _body(line):
    body = line[:-1] if line.endswith("\n") else line
    return body[:-1] if body.endswith("\r") else body


def build_new(original, filepatch, rel, eol=None):
    # Untouched lines keep their exact bytes; added lines take the
    # file's dominant line ending (eol), LF when it has none. parse
    # LF-normalizes patch text, so lines compare without endings.
    newline = eol or "\n"
    lines = _split_lines(original)
    out = []

    def emit(line):
        # A line after one that lacks a newline (the old last line)
        # needs a break between them.
        if out and not out[-1].endswith("\n"):
            out[-1] += newline
        out.append(line)

    cursor = 0
    for hunk in filepatch.hunks:
        start = hunk.old_start - 1 if hunk.old_start > 0 else 0
        if start < cursor or start > len(lines):
            raise RuntimeFault("content_mismatch",
                               f"Patch hunk out of order in {rel}: {hunk.header}")
        for line in lines[cursor:start]:
            emit(line)
        cursor = start
        for op, want, nl in zip(hunk.ops, hunk.texts, hunk.newlines):
            if op in (" ", "-"):
                if cursor >= len(lines) or _body(lines[cursor]) != want:
                    found = _body(lines[cursor]) if cursor < len(lines) else "<end of file>"
                    raise RuntimeFault("content_mismatch",
                                       f"Patch hunk failed in {rel}: {hunk.header} "
                                       f"(expected {want!r}, found {found!r})")
                if op == " ":
                    emit(lines[cursor])
                cursor += 1
            else:
                emit(want + (newline if nl else ""))
    for line in lines[cursor:]:
        emit(line)
    return "".join(out)
