from __future__ import annotations
from dataclasses import dataclass, field
import re
from .errors import RuntimeFault

MAX_DIFF_CHARS = 262144
MAX_FILES = 10

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"
# Lines git writes before a file's "---" header. They carry nothing a
# content patch needs and are skipped; a rename or copy is refused.
_GIT_HEADERS = ("diff --git ", "index ", "similarity index ", "dissimilarity index ",
                "new file mode ", "deleted file mode ", "old mode ", "new mode ",
                "rename from ", "rename to ", "copy from ", "copy to ")


@dataclass
class Hunk:
    header: str
    old_start: int
    # As stated in the header: only 0 matters (an insert after
    # old_start); the real count comes from the body.
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


def _refuse_move(old, new, verb="renames"):
    raise RuntimeFault("invalid_arguments",
                       f"Patch {verb} {old} to {new}; apply_patch does not rename or copy files. "
                       "Move or copy the file with filesystem move/copy first, then patch it under its new path.")


def _in_body(lines, i):
    """True when lines[i] continues a hunk body.

    A "--- " line ends the body only when a "+++ " line follows (the
    next file's header): alone, it is a removed line starting "-- ".
    """
    line = lines[i]
    if line == _NO_NEWLINE:
        return True
    if line[:1] not in (" ", "-", "+"):
        return False
    return not (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "))


def parse(text):
    if not isinstance(text, str) or not text.strip():
        raise RuntimeFault("invalid_arguments", "patch must be a non-empty unified diff string")
    if len(text) > MAX_DIFF_CHARS:
        raise RuntimeFault("invalid_arguments", f"patch exceeds {MAX_DIFF_CHARS} chars")
    lines = text.replace("\r\n", "\n").split("\n")
    files, moved_from = [], {}
    i, n = 0, len(lines)
    while i < n:
        if lines[i] == "" and i == n - 1:
            break
        if lines[i].startswith(_GIT_HEADERS):
            for kind, verb in (("rename", "renames"), ("copy", "copies")):
                if lines[i].startswith(f"{kind} from "):
                    moved_from[kind] = lines[i][len(kind) + 6:]
                elif lines[i].startswith(f"{kind} to "):
                    _refuse_move(moved_from.get(kind, "a file"), lines[i][len(kind) + 4:], verb)
            i += 1
            continue
        if lines[i].startswith(("Binary files ", "GIT binary patch")):
            raise RuntimeFault("binary_file", "Binary file patch is not supported")
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
        # git's a/ and b/ naming two files is a rename; a plain
        # "diff -u x.orig x" patches the new name, as before.
        if old_raw.startswith("a/") and new_raw.startswith("b/") and old_path != new_path:
            _refuse_move(old_path, new_path)
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
            # The header's counts are not checked: models often get them
            # wrong, and the body says how many lines there are.
            while i < n and _in_body(lines, i):
                if lines[i] == _NO_NEWLINE:
                    if not hunk.ops:
                        raise RuntimeFault("invalid_arguments",
                                           f"Stray no-newline marker at diff line {i + 1}")
                    hunk.newlines[-1] = False
                    i += 1
                    continue
                op, content = lines[i][0], lines[i][1:]
                hunk.ops.append(op)
                hunk.texts.append(content)
                hunk.newlines.append(True)
                i += 1
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


def _place(lines, hunk, cursor, rel):
    """Index where a hunk's old lines start, found by content.

    Tries the line the header states first, then searches outward for
    the nearest index, at or after cursor (the end of the previous
    hunk), where the old lines match exactly. Two equally near matches
    are refused, naming both lines.
    """
    old = [text for op, text in zip(hunk.ops, hunk.texts) if op != "+"]
    # A zero-length old range (-N,0) inserts after line N, so it
    # starts at index N; any other range starts at line N.
    stated = hunk.old_start if hunk.old_len == 0 else max(hunk.old_start - 1, 0)
    if not old:
        if stated < cursor or stated > len(lines):
            raise RuntimeFault("content_mismatch",
                               f"Patch hunk out of order in {rel}: {hunk.header}")
        return stated
    last = len(lines) - len(old)

    def fits(at):
        return cursor <= at <= last and all(_body(lines[at + k]) == want for k, want in enumerate(old))

    for distance in range(max(abs(stated - cursor), abs(last - stated)) + 1):
        found = [at for at in dict.fromkeys((stated - distance, stated + distance)) if fits(at)]
        if len(found) > 1:
            raise RuntimeFault("content_mismatch",
                               f"Patch hunk is ambiguous in {rel}: {hunk.header} matches equally well "
                               f"at lines {found[0] + 1} and {found[1] + 1}; add context lines to pick one")
        if found:
            return found[0]
    if stated < cursor:
        raise RuntimeFault("content_mismatch",
                           f"Patch hunk out of order in {rel}: {hunk.header} (its lines are not found "
                           "after the previous hunk)")
    at = stated
    for want in old:
        if at >= len(lines) or _body(lines[at]) != want:
            break
        at += 1
    found = _body(lines[at]) if at < len(lines) else "<end of file>"
    raise RuntimeFault("content_mismatch",
                       f"Patch hunk failed in {rel}: {hunk.header} (expected {old[at - stated]!r}, found {found!r})")


def _drop_bom(hunk):
    """Strip the byte order mark a git diff shows on line 1.

    The file is read without its BOM (it is written back), so the first
    old and new line of a hunk at line 1 compare without it.
    """
    if hunk.old_start > 1:
        return
    for side in (" -", " +"):
        first = next((k for k, op in enumerate(hunk.ops) if op in side), None)
        if first is not None and hunk.texts[first].startswith("\ufeff"):
            hunk.texts[first] = hunk.texts[first][1:]


def build_new(original, filepatch, rel, eol=None, bom=False):
    # Untouched lines keep their exact bytes; added lines take the
    # file's dominant line ending (eol), LF when it has none. parse
    # LF-normalizes patch text, so lines compare without endings.
    # bom: the file had a byte order mark, removed from original.
    newline = eol or "\n"
    lines = _split_lines(original)
    out = []
    if bom:
        for hunk in filepatch.hunks:
            _drop_bom(hunk)

    def emit(line):
        # A line after one that lacks a newline (the old last line)
        # needs a break between them.
        if out and not out[-1].endswith("\n"):
            out[-1] += newline
        out.append(line)

    cursor = 0
    for hunk in filepatch.hunks:
        start = _place(lines, hunk, cursor, rel)
        for line in lines[cursor:start]:
            emit(line)
        cursor = start
        for op, text, nl in zip(hunk.ops, hunk.texts, hunk.newlines):
            if op in (" ", "-"):
                if op == " ":
                    emit(lines[cursor])
                cursor += 1
            else:
                emit(text + (newline if nl else ""))
    for line in lines[cursor:]:
        emit(line)
    return "".join(out)
