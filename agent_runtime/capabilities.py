from __future__ import annotations
import codecs
from collections import deque
import contextlib
import base64
import bisect
import difflib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from .errors import RuntimeFault
from .patch import build_new as build_patched
from .patch import parse as parse_diff
from .processes import held
from .security import coerce_command, git_environment, redact, safe_environment

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

def _remove_read_only(func, path, exc):
    """Delete error hook (rmtree onexc/onerror): Windows refuses to delete
    read-only entries, such as Git object files. Clear the attribute and
    retry once. Links are never touched; other systems are unchanged."""
    exc = exc[1] if isinstance(exc, tuple) else exc
    try:
        read_only = (os.name == "nt" and isinstance(exc, PermissionError) and not os.path.islink(path)
                     and not getattr(os.path, "isjunction", lambda _: False)(path)
                     and not os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        read_only = False
    if not read_only:
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)

_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _lf(text: str):
    return text.replace("\r\n", "\n")


BOM = "\ufeff"


def _split_bom(text: str):
    """(text without a leading byte order mark, whether it had one).

    Reads hide the mark and edits write it back, as they keep line
    endings, so it never shows in returned text or breaks a match on
    line 1.
    """
    return (text[1:], True) if text.startswith(BOM) else (text, False)


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


def _rg_text(field):
    """Text of a ripgrep --json field.

    ripgrep sends {"text": ...} for UTF-8 data and {"bytes": <base64>}
    otherwise (e.g. a line in a cp1252 file), which used to come back
    empty. Decoded as UTF-8 with replacement, like the Python engine.
    """
    if not isinstance(field, dict):
        return None
    if "text" in field:
        return field["text"]
    if "bytes" in field:
        return base64.b64decode(field["bytes"]).decode("utf-8", "replace")
    return None


def _edit_diff(before: str, after: str, cap: int):
    """Unified diff of an edit in the LF view, capped at cap bytes.

    Lets the agent confirm an edit without reading the file again.
    Returns (diff, truncated); diff is None when cap is 0 or nothing
    changed.
    """
    if cap <= 0:
        return None, False
    lines = list(difflib.unified_diff(_lf(before).splitlines(keepends=True),
                                      _lf(after).splitlines(keepends=True), n=2))
    # Drop the ---/+++ header: the result already names the file.
    body = "".join(line if line.endswith("\n") else line + "\n" for line in lines[2:])
    if not body:
        return None, False
    return _clip(body, cap)


# A capped content search keeps up to max_results matches per file and
# this many times max_results in all, to spread the page across files.
SEARCH_SPREAD_FACTOR = 5


def _spread_page(matches, maximum, total, exact=True):
    """A search page of at most maximum of matches (grouped by file, in
    order). With more, they are taken round-robin by file, so every file
    gets a share; files keep their order and matches their line order.
    total counts every match found, a lower bound unless exact."""
    page = {"results": matches, "truncated": total > maximum}
    if len(matches) > maximum:
        files = {}
        for match in matches:
            files.setdefault(match["path"], []).append(match)
        files = list(files.values())
        taken, left, active = [0] * len(files), maximum, list(range(len(files)))
        while left:
            for i in active[:left]:
                taken[i] += 1
            left -= min(left, len(active))
            active = [i for i in active if taken[i] < len(files[i])]
        page["results"] = [m for kept, n in zip(files, taken) for m in kept[:n]]
        if len(files) > 1:
            page["spread"] = True
    if page["truncated"]:
        page["total_matches" if exact else "total_matches_at_least"] = total
    return page


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
            if tokens and tokens[-1] == ("dstar", "(?:.*/)?"):
                # **/** is **: a run of them would backtrack exponentially.
                tokens.pop()
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
            body = glob[i + 1:end]
            negate = body[0] in "!^"
            if negate:
                body = body[1:]
            # Rebuild with every character escaped, so Python reads none of
            # them as nesting, a set operation or an escape; only a bare "-"
            # between two characters makes a range. Like ripgrep, a range
            # followed by -X has its end replaced by X ([a-c-e] is a to e,
            # [a-c-b] is a to b); a reversed step stops the chain, so the
            # reversed range fails to compile ([b-a-c] is an error).
            items, j = [], 0
            while j < len(body):
                if j + 2 < len(body) and body[j + 1] == "-":
                    last = j + 2
                    while body[last] >= body[j] and last + 2 < len(body) and body[last + 1] == "-":
                        last += 2
                    items.append(re.escape(body[j]) + "-" + re.escape(body[last]))
                    j = last + 1
                else:
                    items.append(re.escape(body[j]))
                    j += 1
            cls = "[" + ("^" if negate else "") + "".join(items) + "]"
            try:
                # A range or negation can cover "/" too ([ -~], [!a]).
                spans_sep = re.fullmatch(cls, "/") is not None
            except re.error:
                spans_sep = False  # reported when the whole glob compiles
            tokens.append(("sep" if spans_sep else "lit", cls))
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
    from the whole pattern before braces expand, as ripgrep does. A
    folder-only pattern matches whatever lies inside a matching folder,
    and never matches when folders is false (includes match files).
    Returns None for a pattern ripgrep ignores: an empty one, or slashes
    alone as an exclude; slashes alone as an include match nothing.
    """
    core = pattern.rstrip("/")
    if not pattern:
        return None
    if not core.strip("/"):
        return None if folders else _NEVER
    anchored = "/" in core
    core = core.lstrip("/")
    suffix = ""
    if pattern.endswith("/"):
        if not folders:
            return _NEVER
        # Regex, not "/**": a**/ must not also match a's own files.
        suffix = "/.*"
    prefix = "" if anchored else "(?:.*/)?"
    parts = [prefix + _glob_body(glob) + suffix for glob in _expand_braces(core)]
    try:
        return re.compile("(?:" + "|".join(parts) + ")")
    except re.error as e:
        raise RuntimeFault("invalid_arguments", f"Invalid glob {pattern!r}: {e}") from e

MAX_BATCH_SEARCHES = 16
MAX_READ_LINES = 2000
# Last lines a line-mode read past the end of the file shows.
PAST_END_LINES = 100
LINE_CUT = " ...[line cut]"
# Longest line read and search return; a minified line cannot use the whole budget.
MAX_LINE_CHARS = 2000
RG_TIMEOUT_SECONDS = 60
# Most of one new file a git diff with include_untracked shows.
UNTRACKED_DIFF_BYTES = 8000
# Most untracked files a git diff with include_untracked lists as skipped.
UNTRACKED_SKIPPED_SHOWN = 50
BATCH_SEARCH_WORKERS = 8
GIT_ROOT_TTL_SECONDS = 30
PATH_LOCK_STRIPES = 256
# Saved outputs kept in the spill folder; older ones are pruned.
SPILL_KEEP = 100
# Every spill file starts with this; pruning touches nothing else.
SPILL_PREFIX = "lf-"
# difflib grows worse than quadratically on big repetitive files,
# so edits above this many characters (before + after) skip the diff.
EDIT_DIFF_MAX_INPUT = 128_000
# Failure output an edit check returns inline.
EDIT_CHECK_OUTPUT_BYTES = 4000
# Hint for an edit that finds old_text the wrong number of times.
HINT_LINES = 12
HINT_LINE_CHARS = 200
HINT_MATCH_LINES = 10
# A line found more often than this in the file (a lone brace, "end")
# says nothing about where old_text is, and costs a vote per copy.
HINT_COMMON_LINE = 100
# How the hint names each fallback tier of _find_spans.
FALLBACK_LABELS = {"typography": "ignoring typography",
                   "trailing_whitespace": "ignoring trailing whitespace"}


# Typographic variants, each mapped to one ASCII character so offsets
# into the normalized text stay valid for the original.
_TYPOGRAPHY = str.maketrans({**dict.fromkeys("\u2018\u2019\u201a\u201b", "'"),
                             **dict.fromkeys("\u201c\u201d\u201e\u201f", '"'),
                             **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
                             **dict.fromkeys("\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
                                             "\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " ")})


def _typography(text):
    return text.translate(_TYPOGRAPHY)


def _occurrences(text, needle, step=None):
    """(start, end) spans of needle in text; step=1 also finds overlapping ones."""
    if not needle:
        # Like str.count and str.replace: once at every position.
        return [(i, i) for i in range(len(text) + 1)]
    spans, at = [], text.find(needle)
    while at != -1:
        spans.append((at, at + len(needle)))
        at = text.find(needle, at + (step or len(needle)))
    return spans


def _trim_end(line):
    # Trailing whitespace, but not a CR: a stray CR is content.
    end = len(line)
    while end and line[end - 1].isspace() and line[end - 1] != "\r":
        end -= 1
    return line[:end]


def _line_occurrences(view, old):
    """Spans of runs of whole lines equal to old's lines, ignoring
    trailing whitespace and typographic variants."""
    def key(line):
        return _typography(_trim_end(line))

    trailing = old.endswith("\n")
    wanted = old.split("\n")
    if trailing:
        wanted.pop()
    wanted = [key(line) for line in wanted]
    parts = view.split("\n")
    if view.endswith("\n"):
        parts.pop()
    lines, pos = [], 0
    for part in parts:
        end = pos + len(part)
        lines.append((pos, end, key(part), end < len(view)))
        pos = end + 1
    spans = []
    for first in range(len(lines) - len(wanted) + 1):
        if all(lines[first + k][2] == want for k, want in enumerate(wanted)):
            last = lines[first + len(wanted) - 1]
            if trailing and not last[3]:
                continue
            spans.append((lines[first][0], last[1] + 1 if trailing else last[1]))
    return spans


def _find_spans(view, old, new, expected):
    """Matches of old in the LF view, the tier that found them, and the
    fallback tiers tried.

    Exact matches win. Only when there are none, and the edit has no
    lone CR (one asks for exact bytes, as when old and new differ only
    in line endings), retry ignoring typographic variants, then per
    line ignoring trailing whitespace too, then (for one expected match)
    old copied with read's 'N<TAB>' line-number prefixes. A fallback
    tier counts only
    with exactly expected matches, none overlapping; otherwise the next
    one is tried. Returns (spans, tier, tried); tier None is a mismatch,
    spans then being the exact matches. tried lists (tier, spans) for
    each fallback tier that did not count.
    """
    exact = _occurrences(view, old)
    if expected is None:
        # replace_all: exact matches only. A fallback match that is
        # wrong would be repeated at every match, unseen.
        return exact, "exact" if exact else None, []
    if exact or "\r" in old or "\r" in new or not old.strip():
        return exact, "exact" if len(exact) == expected else None, []
    tried = []
    for tier, find in (("typography", lambda: _occurrences(_typography(view), _typography(old), step=1)),
                       ("trailing_whitespace", lambda: _line_occurrences(view, old))):
        spans = find()
        if spans and _apart(spans) and len(spans) == expected:
            return spans, tier, tried
        tried.append((tier, spans))
    span = _numbered_span(view, old) if expected == 1 else None
    if span:
        return [span], "line_numbers", tried
    return [], None, tried


_LINE_NUMBER = re.compile(r"(\d{1,9})\t")
NUMBERED_HINT = (" old_text lines start with line numbers (N<TAB>) that do not match the file there;"
                 " leave the line-number prefixes out.")
NUMBERED_SHIFT_HINT = (" old_text lines start with line numbers (N<TAB>), but an earlier edit in this call"
                       " changed the line count, so they may point at other lines than the ones read;"
                       " put line-numbered edits first, or leave the prefixes out.")


def _split_numbered(text, consecutive=True):
    """(first number, lines without prefix, ends with a line break) when
    every line of text starts with an 'N<TAB>' prefix, as line-mode read
    prints them, numbered on from 1 or more if consecutive; else None."""
    trailing = text.endswith("\n")
    parts = text.split("\n")
    if trailing:
        parts.pop()
    numbers, lines = [], []
    for part in parts:
        m = _LINE_NUMBER.match(part)
        if not m:
            return None
        numbers.append(int(m.group(1)))
        lines.append(part[m.end():])
    if not numbers or consecutive and (numbers[0] < 1 or numbers != list(range(numbers[0], numbers[0] + len(numbers)))):
        return None
    return numbers[0], lines, trailing


def _numbered_span(view, old):
    """Span of old copied with line-number prefixes, when its lines are
    exactly the view's lines at those numbers; else None."""
    numbered = _split_numbered(old)
    if numbered is None:
        return None
    first, wanted, trailing = numbered
    lines = view.split("\n")
    real = len(lines) - 1 if view.endswith("\n") else len(lines)
    last = first + len(wanted) - 1
    if last > real or lines[first - 1:last] != wanted:
        return None
    start = sum(len(line) + 1 for line in lines[:first - 1])
    end = start + sum(len(line) + 1 for line in wanted) - 1
    if trailing:
        if end >= len(view):
            return None
        end += 1
    return start, end


def _strip_numbers(new):
    """new without 'N<TAB>' prefixes when every line has one."""
    numbered = _split_numbered(new, consecutive=False)
    if numbered is None:
        return new
    _, lines, trailing = numbered
    return "\n".join(lines) + ("\n" if trailing else "")


def _apart(spans):
    return all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def _match_lines(view, starts):
    """' start on lines 1, 3.' for match offsets in view, capped."""
    lines = list(dict.fromkeys(view.count("\n", 0, at) + 1 for at in starts))
    more = ", ..." if len(lines) > HINT_MATCH_LINES else ""
    return f" start on lines {', '.join(map(str, lines[:HINT_MATCH_LINES]))}{more}."


def _fallback_miss(view, exact, tried):
    """(found, hint) for an edit whose fallback tiers matched, but not
    the expected number of times: each tier's count, and the lines its
    matches start on."""
    counts, hint = [f"{exact} exact"], ""
    for tier, spans in tried:
        label = FALLBACK_LABELS[tier]
        counts.append(f"{len(spans)} {label}" + ("" if _apart(spans) else " (overlapping)"))
        if spans:
            hint += f" Matches {label}" + _match_lines(view, (start for start, _ in spans[:HINT_MATCH_LINES * 2]))
    return ", ".join(counts), hint


def _replace_spans(text, spans, new):
    out, at = [], 0
    for start, end in spans:
        out += [text[at:start], new]
        at = end
    out.append(text[at:])
    return "".join(out)


def _source_spans(source, spans):
    """Map spans in the LF view of source back to source itself."""
    # View offset of the LF of each CRLF; a span boundary moves right by
    # the CRs removed before it, so a CRLF pair is never split.
    removed = [m.start() - n for n, m in enumerate(re.finditer("\r\n", source))]
    return [(start + bisect.bisect_left(removed, start), end + bisect.bisect_left(removed, end))
            for start, end in spans]


def _miss_hint(view, old, new, found, expected):
    """Hint for an edit whose old_text matched found times, not expected.

    Lets the agent retry without reading the file again. For a miss:
    new_text is already there (the edit may already be applied), or the
    lines closest to old_text, numbered like line-mode read. Lines vote
    for where old_text starts when they match ignoring whitespace; only
    lines with a letter or digit vote, and at least half of them must
    agree. Only the span from the first to the last matched line is
    shown, so padding old_text reveals nothing past it. For too many
    matches, the lines they start on. view is the LF view; returns ""
    when there is nothing useful to say.
    """
    if found > 1 and expected == 1:
        starts, at = [], view.find(old)
        while at != -1 and len(starts) <= HINT_MATCH_LINES:
            starts.append(view.count("\n", 0, at) + 1)
            at = view.find(old, at + len(old))
        more = ", ..." if len(starts) > HINT_MATCH_LINES else ""
        return f" Matches start on lines {', '.join(map(str, starts[:HINT_MATCH_LINES]))}{more}."
    if found:
        return ""
    stripped = new.strip()
    if len(stripped) >= 20 and _typography(stripped) in _typography(view):
        return " new_text is already in the file, so this edit may already be applied."

    def key(line):
        return re.sub(r"\s+", "", _typography(line))

    def votes_for(text):
        return any(c.isalnum() for c in text)

    lines = view.split("\n")
    positions = {}
    for number, line in enumerate(lines):
        if votes_for(k := key(line)):
            positions.setdefault(k, []).append(number)
    search = old.split("\n")
    votes, wanted = {}, 0
    for offset, line in enumerate(search):
        if not votes_for(k := key(line)) or len(positions.get(k, ())) > HINT_COMMON_LINE:
            continue
        wanted += 1
        for number in positions.get(k, ()):
            votes[number - offset] = votes.get(number - offset, 0) + 1
    if not votes:
        return ""
    start, count = max(votes.items(), key=lambda vote: (vote[1], -vote[0]))
    if count * 2 < wanted:
        return ""
    matched = [start + offset for offset, line in enumerate(search)
               if key(line) and 0 <= start + offset < len(lines) and key(lines[start + offset]) == key(line)]
    first = matched[0]
    shown = []
    for number in range(first, min(matched[-1] + 1, first + HINT_LINES)):
        line = lines[number]
        if len(line) > HINT_LINE_CHARS:
            line = line[:HINT_LINE_CHARS] + LINE_CUT
        shown.append(f"{number + 1}\t{line}")
    return "\nClosest lines in the file (N<TAB>line):\n" + "\n".join(shown)


# Entries of one folder read for path suggestions; folders can be huge.
SUGGEST_SCAN_LIMIT = 10_000
SUGGEST_MAX = 3


def _stem(name):
    return os.path.splitext(name)[0] or name


def _edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _similar(names, requested):
    """Names in a folder that look like a missing one, best first.

    Ranked like CLI agents do: the same name ignoring case, then the
    same stem with another extension, then one name containing the
    other (3+ characters), then a typo in the stem (edit distance 1 for
    stems under 5 characters, 2 above, none under 3). A generator: a
    rank is sorted (by edit distance) only when the caller gets to it.
    """
    name = requested.lower()
    wanted = _stem(name)
    typos = 0 if len(wanted) < 3 else 1 if len(wanted) < 5 else 2
    ranked = {1: [], 2: [], 3: [], 4: []}
    for entry in names:
        if entry == requested:
            continue
        candidate = entry.lower()
        found = _stem(candidate)
        if candidate == name:
            rank = 1
        elif found == wanted:
            rank = 2
        elif min(len(found), len(wanted)) >= 3 and (name in candidate or candidate in name
                                                    or wanted in found or found in wanted):
            rank = 3
        elif typos and abs(len(found) - len(wanted)) <= typos and _edit_distance(found, wanted) <= typos:
            rank = 4
        else:
            continue
        ranked[rank].append(entry)
    for entries in ranked.values():
        if len(entries) > 1:
            entries.sort(key=lambda entry: (_edit_distance(entry.lower(), name), entry))
        yield from entries


def _did_you_mean(paths):
    return "\nDid you mean one of these?\n" + "\n".join(paths) if paths else ""


def _cap_line(text, length=None):
    """text cut to MAX_LINE_CHARS, marked with the line's full length."""
    length = len(text) if length is None else length
    if length <= MAX_LINE_CHARS:
        return text
    return f"{text[:MAX_LINE_CHARS]}...[line cut: {length} chars]"


def _cap_new_files(diff, cap=UNTRACKED_DIFF_BYTES):
    """Each new file's part of a git diff cut to cap bytes, on a line end."""
    out = []
    for part in re.split(r"(?m)^(?=diff --git )", diff):
        size = len(part.encode("utf-8"))
        if size <= cap or "\nnew file mode " not in part.split("\n@@", 1)[0]:
            out.append(part)
            continue
        kept, _ = _clip(part, cap)
        kept = kept[:kept.rfind("\n") + 1] or kept
        out.append(f"{kept}...[new file cut: showing {len(kept.encode('utf-8'))} of {size} bytes; "
                   "read the file for the rest]\n")
    return "".join(out)


def _numbered(number, body, length, budget):
    # One "N<TAB>line" of a line-mode read, within the read budget too.
    text = f"{number}\t" + _cap_line(body, length)
    if len(text) > budget:
        text = text[:max(0, budget - len(LINE_CUT))] + LINE_CUT
    return text

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
        # Oversized outputs are saved here so the reply can stay
        # small. The folder is added to the read roots so the agent
        # can read a saved output back with filesystem read or search.
        self.spill_dir = None
        try:
            if cfg.spill_dir:
                spill = paths._canonical(cfg.spill_dir)
            else:
                # One folder per config, so instances with different
                # policies never read or prune each other's output.
                key = os.path.normcase(str(Path(cfg.config_path or cfg.workspace_root).resolve()))
                digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
                spill = Path(tempfile.gettempdir()) / "localforge-results" / digest
            spill.mkdir(parents=True, exist_ok=True)
            self.spill_dir = spill.resolve()
        except (OSError, RuntimeFault) as e:
            print(f"localforge: spill_dir unusable, large output will only be clipped: {e}", file=sys.stderr)
        if self.spill_dir is not None and self.spill_dir not in paths.read_roots:
            paths.read_roots.append(self.spill_dir)
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
        info = {"workspace_root": str(self.paths.workspace), "cwd": str(self.cwd),
                "repository_root": self._git_root(self.cwd)}
        if not self.cfg.load_project_instructions:
            return info
        instructions = self._project_instructions(info["repository_root"], self.cwd)
        if instructions:
            info["project_instructions"] = instructions
        return info

    def _project_instructions(self, repo_root, cwd):
        """Read agent rule files (AGENTS.md, CLAUDE.md) like CLI agents do.

        Looks in every folder from the Git root down to the cwd, so a
        subfolder's rules apply when working in it; with no Git root,
        from workspace_root down to the cwd (just the cwd when it is
        outside workspace_root). Outermost first. All files share one
        max_project_instruction_bytes budget; a file the policy denies
        or that cannot be read is skipped.
        """
        cwd = Path(cwd).resolve()
        top = Path(repo_root).resolve() if repo_root else Path(self.paths.workspace).resolve()
        try:
            parts = cwd.relative_to(top).parts
        except ValueError:
            top, parts = cwd, ()
        folders = [top]
        for part in parts:
            folders.append(folders[-1] / part)
        budget = int(self.cfg.max_project_instruction_bytes)
        found = []
        for folder in folders:
            for name in self.cfg.project_instruction_files:
                try:
                    target = self.paths.resolve(folder / name, access="read", must_exist=True)
                    if not target.is_file():
                        continue
                    if budget <= 0:
                        found.append({"path": str(target), "skipped": "budget"})
                        continue
                    with target.open("rb") as handle:
                        raw = handle.read(budget + 1)
                except (RuntimeFault, OSError):
                    continue
                cut = len(raw) > budget
                # On a cut read, final=False holds back only a partial
                # character at the end; bad bytes elsewhere become U+FFFD.
                text = codecs.getincrementaldecoder("utf-8")("replace").decode(raw[:budget], final=not cut)
                text, _ = _split_bom(text)
                content, extra = _clip(text, budget)
                cut = cut or extra
                budget -= len(content.encode("utf-8"))
                entry = {"path": str(target), "content": content}
                if cut:
                    entry["truncated"] = True
                found.append(entry)
        return found

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
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
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

    def _resolve_existing(self, path, access, retry=False, cwd=None):
        """Resolve a path that must exist; a miss suggests similar names.

        Returns (target, note). A relative path missing from the cwd
        that exists from the Git root (copied from git status after a
        set_cwd into a subfolder, say) is used from there when retry is
        set, for read-only actions, with a note saying so; otherwise
        the error names it. Either way it passes the same path policy.
        """
        cwd = cwd or self.cwd
        try:
            return self.paths.resolve(path, cwd=cwd, access=access, must_exist=True), None
        except RuntimeFault as e:
            if e.code != "path_not_found":
                raise
            found = self._from_git_root(path, access, cwd)
            if found is not None and retry:
                return found, f"{path} is not in the cwd ({cwd}); used {found}, relative to the Git root."
            if found is not None:
                e.message += (f"\nRelative to the Git root it is {found}; writes, deletes and moves are not "
                              "retried there, so check it is the file you mean before passing that path.")
            else:
                e.message += self._missing_hint(path)
            e.args = (e.message,)
            raise

    def _from_git_root(self, path, access, cwd):
        """path resolved from the Git root of cwd, when it is relative,
        the root is not cwd itself, and it exists there under the path
        policy; else None."""
        raw = Path(os.path.expandvars(os.path.expanduser(str(path or "."))))
        if raw.is_absolute() or raw.drive or raw.root:
            return None
        root = self._git_root(cwd)
        if not root or Path(root).resolve() == Path(cwd).resolve():
            return None
        try:
            return self.paths.resolve(str(path), cwd=Path(root), access=access, must_exist=True)
        except RuntimeFault:
            return None

    @staticmethod
    def _noted(result, note):
        if note:
            result["note"] = f"{result['note']} {note}" if result.get("note") else note
        return result

    def _missing_hint(self, path):
        """"Did you mean" text for a missing path, or "".

        When the parent folder exists, up to SUGGEST_MAX similar entries
        in it; otherwise the first missing folder, the nearest existing
        parent, and similar names there. Only folders and entries the
        read policy allows are listed or named.
        """
        def readable(candidate):
            try:
                self.paths.resolve(candidate, access="read", must_exist=True)
                return True
            except RuntimeFault:
                return False

        def similar(folder, name, show):
            try:
                with os.scandir(folder) as entries:
                    names = [entry.name for _, entry in zip(range(SUGGEST_SCAN_LIMIT), entries)]
            except OSError:
                return []
            # Policy checks only until SUGGEST_MAX names pass.
            found = []
            for n in _similar(names, name):
                if readable(folder / n):
                    found.append(show(n))
                    if len(found) == SUGGEST_MAX:
                        break
            return found

        try:
            target = self.paths._canonical(path or ".", self.cwd)
        except RuntimeFault:
            return ""
        child, parent = target, target.parent
        while not parent.exists():
            if parent.parent == parent:
                return ""
            child, parent = parent, parent.parent
        if not parent.is_dir() or not readable(parent):
            return ""
        if child == target:
            base = os.path.dirname(str(path))
            return _did_you_mean(similar(parent, target.name, lambda n: os.path.join(base, n)))
        return (f"\nThe folder {child} does not exist; the nearest folder that does is {parent}."
                + _did_you_mean(similar(parent, child.name, lambda n: str(parent / n))))

    def filesystem(self, action, path=".", content=None, destination=None, recursive=False,
                   encoding="utf-8", offset=0, max_bytes=None, expected_occurrences=None,
                   old_text=None, new_text=None, edits=None, line_start=None, line_end=None,
                   limit=200, glob=None, include_hidden=False, patch=None, overwrite=False, replace_all=False):
        if action == "copy":
            if not destination:
                raise RuntimeFault("invalid_arguments", "copy requires destination")
            src, _ = self._resolve_existing(path, "read")
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("copy")
            with self._locked_paths([src, dest]):
                try:
                    if src.is_file():
                        self._check_destination("copy", src, dest, overwrite)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        return {"source": str(src), "destination": str(dest), "bytes": dest.stat().st_size}
                    if src.is_dir():
                        if not recursive:
                            raise RuntimeFault("invalid_arguments", "copying a directory requires recursive=true")
                        self._check_destination("copy", src, dest, overwrite)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(src, dest)
                        count = sum(1 for _ in dest.rglob("*")) + 1
                        return {"source": str(src), "destination": str(dest), "copied_items": count}
                except OSError as e:
                    _fs_error("copy", src, e)
            raise RuntimeFault("not_file", f"Not a file or directory: {src}")
        access = "read" if action in {"read", "list", "stat"} else "write"
        must_exist = action not in {"write", "mkdir"}
        note = None
        if must_exist:
            target, note = self._resolve_existing(path, access, retry=access == "read")
        else:
            target = self.paths.resolve(path, cwd=self.cwd, access=access, must_exist=False)
            if action == "write" and not target.exists() and not str(path).startswith(("./", ".\\")):
                # read retries such a path from the Git root; writing it here would
                # make a second file instead of changing the one read.
                found = self._from_git_root(path, "read", self.cwd)
                if found is not None:
                    raise RuntimeFault("invalid_arguments",
                                       f"{path} is not in the cwd ({self.cwd}) but exists relative to the Git "
                                       f"root: {found}. Writes are not retried there: pass {found} to "
                                       f"overwrite that file, or ./{path} to create a new file in the cwd.")
        self.policy.authorize_path(action)
        if action == "read":
            if target.is_dir():
                # Like CLI agents: reading a folder lists it, as list does.
                return self._noted({**self._list_dir(target, limit, offset, glob, include_hidden),
                                    "note": f"{target} is a directory, so its entries are listed, as by action list."},
                                   note)
            if not target.is_file():
                raise RuntimeFault("not_file", f"Not a file: {target}")
            if line_start is not None or line_end is not None:
                return self._noted(self._read_lines(target, encoding, line_start, line_end), note)
            cap = min(int(max_bytes or self.cfg.max_file_read_bytes), self.cfg.max_file_read_bytes)
            offset = max(0, int(offset))
            with target.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(cap + 1)
            truncated = len(data) > cap
            data = data[:cap]
            if b"\x00" in data:
                raise RuntimeFault("binary_file", "Binary file read is not supported")
            content = data.decode(encoding, "replace")
            if offset == 0:
                content, _ = _split_bom(content)
            return self._noted({"path": str(target), "content": content, "offset": offset,
                                "bytes_returned": len(data), "size": target.stat().st_size,
                                "truncated": truncated}, note)
        if action == "list":
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Not a directory: {target}")
            return self._noted(self._list_dir(target, limit, offset, glob, include_hidden), note)
        if action == "stat":
            try:
                stat = target.stat()
            except OSError as e:
                _fs_error("stat", target, e)
            return self._noted({"path": str(target), "type": "dir" if target.is_dir() else "file",
                                "size": stat.st_size, "modified": stat.st_mtime, "created": stat.st_ctime}, note)
        if action == "write":
            data = content or ""
            with self._locked_paths([target]):
                try:
                    # An existing file keeps its line endings; a new
                    # file is written exactly as sent.
                    eol = None
                    before = None
                    existed = False
                    bom = False
                    try:
                        with target.open("r", encoding=encoding, errors="replace", newline="") as handle:
                            existed = True
                            head, bom = _split_bom(handle.read(65536))
                            eol = _detect_eol(head)
                            if target.stat().st_size <= EDIT_DIFF_MAX_INPUT:
                                before = head + handle.read()
                    except FileNotFoundError:
                        pass
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # A file that starts with a byte order mark keeps it, like
                    # its line endings, whatever the content starts with; a
                    # file without one gets it only from the content.
                    data, own_bom = _split_bom(data)
                    written = self._atomic_write(target, data, encoding, eol, bom or own_bom)
                except OSError as e:
                    _fs_error("write", target, e)
            result = {"path": str(target), "bytes": written}
            if eol is not None:
                # Tells the agent its line endings were kept; delete
                # the file first to change them.
                result["eol"] = "CRLF" if eol == "\r\n" else "LF"
            if before is not None:
                self._add_diff(result, before, data)
            elif existed and int(self.cfg.edit_diff_max_bytes) > 0:
                result["diff_skipped"] = "too_large"
            self._add_check(result, target)
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
            # None: replace_all, every exact match (at least one).
            expected = self._expected(replace_all, expected_occurrences, old_text, "")
            with self._locked_paths([target]):
                try:
                    original, eol, bom = self._read_text(target, encoding)
                except OSError as e:
                    _fs_error("replace_text", target, e)
                updated, counts, tiers, write_eol = self._apply_edits(
                    original, eol, [(old_text, new_text, expected)],
                    lambda index, want, found, hint: RuntimeFault(
                        "content_mismatch", ("replace_all found no exact occurrence of old_text." if want is None else
                                             f"Expected {want} occurrence(s), found {found}. Pass expected_occurrences "
                                             "(or replace_all: true) to replace all of them at once.") + hint))
                try:
                    written = self._atomic_write(target, updated, encoding, write_eol, bom)
                except OSError as e:
                    _fs_error("replace_text", target, e)
            result = {"path": str(target), "replacements": counts[0], "bytes": written}
            if tiers[0] != "exact":
                # Not an exact match: say which fallback found it.
                result["match"] = tiers[0]
            self._add_diff(result, original, updated)
            self._add_check(result, target)
            return result
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
                specs.append((old, new, self._expected(edit.get("replace_all"), edit.get("expected_occurrences"),
                                                       old, f"edits[{index}]: ")))
            with self._locked_paths([target]):
                try:
                    original, eol, bom = self._read_text(target, encoding)
                except OSError as e:
                    _fs_error("multi_edit", target, e)
                # Edits apply in order against the evolving content and
                # are all validated before anything is written: a
                # failing edit leaves the file untouched, so a
                # coherent multi-part change never lands half-applied.
                content, counts, tiers, write_eol = self._apply_edits(
                    original, eol, specs,
                    lambda index, want, found, hint: RuntimeFault(
                        "content_mismatch", (f"edits[{index}]: replace_all found no exact occurrence of old_text. "
                                             "Edits apply in order, so earlier edits may have changed the text."
                                             if want is None else
                                             f"edits[{index}]: expected {want} occurrence(s), found {found}. Edits "
                                             "apply in order, so earlier edits may have changed the text; pass "
                                             "expected_occurrences (or replace_all) when it repeats.") + hint))
                try:
                    written = self._atomic_write(target, content, encoding, write_eol, bom)
                except OSError as e:
                    _fs_error("multi_edit", target, e)
            applied = [{"index": index, "replacements": count} for index, count in enumerate(counts)]
            for entry, tier in zip(applied, tiers):
                if tier != "exact":
                    entry["match"] = tier
            result = {"path": str(target), "edits": applied, "bytes": written}
            self._add_diff(result, original, content)
            self._add_check(result, target)
            return result
        if action == "apply_patch":
            if patch is None:
                raise RuntimeFault("invalid_arguments", "apply_patch requires patch")
            if not target.is_dir():
                raise RuntimeFault("not_directory", f"Patch base is not a directory: {target}")
            self.policy.authorize_path("apply_patch")
            entries = list(parse_diff(patch))
            planned_paths, seen = [], {}
            for fp in entries:
                rel = fp.new_path if fp.new_path != "/dev/null" else fp.old_path
                dest = self.paths.resolve(rel, cwd=target, access="write", must_exist=False)
                # Each section is built from the file on disk, so a second
                # one for the same file would drop the first one's change.
                key = os.path.normcase(str(dest))
                if key in seen:
                    raise RuntimeFault("invalid_arguments",
                                       f"Patch names {dest} in two sections ({seen[key]} and {rel}); "
                                       "merge its hunks into one section")
                seen[key] = rel
                planned_paths.append((fp, rel, dest))
            with self._locked_paths([dest for _, _, dest in planned_paths]):
                planned = []
                for fp, rel, dest in planned_paths:
                    creating = fp.old_path == "/dev/null"
                    if creating:
                        if dest.exists():
                            raise RuntimeFault("invalid_arguments", f"Patch creates existing file: {dest}")
                        original, eol, bom = "", None, False
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
                        original, bom = _split_bom(raw.decode(encoding, "replace"))
                        eol = _detect_eol(original)
                    moved = []
                    updated = build_patched(original, fp, rel, eol, bom, moved)
                    ops = [op for hunk in fp.hunks for op in hunk.ops]
                    info = {"added": ops.count("+"), "removed": ops.count("-")}
                    if moved:
                        # Only hunks placed away from their stated line.
                        info["hunks"] = moved
                    planned.append((dest, updated, creating, bom, original, info))
                results, diffs = [], []
                for dest, updated, creating, bom, original, info in planned:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # build_new keeps untouched lines byte-exact,
                        # so the result is written verbatim.
                        written = self._atomic_write(dest, updated, encoding, bom=bom)
                    except OSError as e:
                        _fs_error("apply_patch", dest, e)
                    results.append({"path": str(dest), "created": creating, "bytes": written, **info})
                    # Like write: a new file's diff would only repeat it.
                    diffs.append(None if creating else (original, updated))
            for entry, change in zip(results, diffs):
                if change:
                    self._add_diff(entry, *change)
                self._add_check(entry, Path(entry["path"]))
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
                    if target.is_dir() and recursive:
                        shutil.rmtree(target, **{_RMTREE_HOOK: _remove_read_only})
                    else:
                        remove = os.rmdir if target.is_dir() else os.unlink
                        try:
                            remove(target)
                        except OSError as e:
                            _remove_read_only(remove, target, e)
                except OSError as e:
                    _fs_error("delete", target, e)
            return {"path": str(target), "deleted_items": count}
        if action == "move":
            if not destination:
                raise RuntimeFault("invalid_arguments", "move requires destination")
            dest = self.paths.resolve(destination, cwd=self.cwd, access="write", must_exist=False)
            self.policy.authorize_path("move")
            with self._locked_paths([target, dest]):
                self._check_destination("move", target, dest, overwrite)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # The resolved dest has the on-disk case; the name asked for may not.
                    wanted = Path(str(destination).rstrip("/\\")).name
                    if (wanted and wanted != target.name and dest.exists() and os.path.samefile(target, dest)
                            and wanted.lower() == target.name.lower()):
                        # Case-only rename on a case-insensitive file system. Windows
                        # renames in place; where a plain replace leaves the name as
                        # it was, go through a temporary name.
                        renamed = dest.with_name(wanted)
                        target.replace(renamed)
                        names = os.listdir(target.parent)
                        if wanted in names and target.name in names:
                            # Hard links to one file on a case-sensitive file system:
                            # rename leaves both names, so drop the old one.
                            target.unlink()
                        elif wanted not in names:
                            temp = target.with_name(f".localforge-{uuid.uuid4().hex[:8]}-{target.name}")
                            target.replace(temp)
                            try:
                                temp.replace(renamed)
                            except OSError:
                                try:
                                    temp.replace(target)
                                except OSError as e:
                                    raise RuntimeFault("io_error", f"Case-only rename failed; the file is left at {temp}: {e}") from e
                                raise
                        dest = renamed
                    else:
                        target.replace(dest)
                except OSError as e:
                    _fs_error("move", target, e)
            return {"source": str(target), "destination": str(dest)}
        raise RuntimeFault("invalid_action", f"Unknown filesystem action: {action}")

    @staticmethod
    def _check_destination(action, src, dest, overwrite):
        """Refuse an existing move/copy destination, unless overwrite
        replaces a file with a file.

        An existing directory is never replaced or merged. A move onto
        the source itself (another spelling of its path) is not an
        overwrite; a copy onto it is refused, as is a folder moved or
        copied into itself (copytree would recurse into its own copy).
        """
        name = action.capitalize()
        same = False
        with contextlib.suppress(OSError):
            same = dest.exists() and os.path.samefile(src, dest)
        if same and action == "copy":
            raise RuntimeFault("invalid_arguments", f"Copy destination {dest} is the source itself.")
        if src.is_dir() and not same:
            inner, outer = (os.path.normcase(os.path.realpath(p)) for p in (dest, src))
            if inner.startswith(outer.rstrip(os.sep) + os.sep):
                raise RuntimeFault("invalid_arguments",
                                   f"{name} destination {dest} is inside the source folder {src}; "
                                   "a folder cannot go into itself.")
        if not dest.exists():
            return
        if same:
            return
        if dest.is_dir():
            raise RuntimeFault("invalid_arguments",
                               f"{name} destination is an existing directory: {dest}. A directory is never "
                               "replaced or merged: give the full destination path, or delete it first.")
        if src.is_dir():
            raise RuntimeFault("invalid_arguments",
                               f"{name} destination is an existing file: {dest}; a directory never replaces a file.")
        if not overwrite:
            raise RuntimeFault("invalid_arguments",
                               f"{name} destination already exists: {dest}. Pass overwrite: true to replace it.")

    @staticmethod
    def _expected(replace_all, expected_occurrences, old, where):
        """Match count an edit needs: None for replace_all (every exact
        match, at least one), else expected_occurrences or 1."""
        if not old:
            raise RuntimeFault("invalid_arguments", f"{where}old_text is empty; pass the text to replace "
                                                    "(write replaces a whole file)")
        if not replace_all:
            return 1 if expected_occurrences is None else int(expected_occurrences)
        if expected_occurrences is not None:
            raise RuntimeFault("invalid_arguments",
                               f"{where}replace_all replaces every occurrence; do not pass expected_occurrences with it")
        return None

    @staticmethod
    def _list_dir(target, limit, offset, glob, include_hidden):
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

    def _add_diff(self, result, before, after):
        if int(self.cfg.edit_diff_max_bytes) <= 0:
            return
        if len(before) + len(after) > EDIT_DIFF_MAX_INPUT:
            result["diff_skipped"] = "too_large"
            return
        if "\x00" in before or "\x00" in after:
            result["diff_skipped"] = "binary"
            return
        diff, cut = _edit_diff(before, after, int(self.cfg.edit_diff_max_bytes))
        if diff is not None:
            result["diff"] = diff
            if cut:
                result["diff_truncated"] = True

    def _add_check(self, result, target):
        """Run the edit_checks command whose glob matches the edited file.

        Runs after the write, outside the path lock; the edit stays
        written either way. A pass is just {"ok": true}; a failure
        carries the command, exit code, and clipped output.
        """
        argv = next((cmd for pattern, cmd in self.cfg.edit_checks.items()
                     if fnmatch.fnmatch(target.name, pattern)), None)
        if argv is None or not target.is_file():
            return
        command = [part.replace("{path}", str(target)) for part in argv]
        try:
            # kill_on_timeout: a check must never outlive its timeout.
            run = self.execute(command, timeout=self.cfg.edit_check_timeout_seconds, kill_on_timeout=True)
        except RuntimeFault as e:
            result["check"] = {"ok": False, "command": command, "error": e.message}
            return
        if run["success"]:
            result["check"] = {"ok": True}
            return
        output, _ = _clip_ends((run["stdout"] + run["stderr"]).strip(), EDIT_CHECK_OUTPUT_BYTES)
        result["check"] = {"ok": False, "command": command, "exit_code": run["exit_code"],
                           "error_type": run["error_type"], "output": output}
        for key in ("stdout_path", "stderr_path"):
            if key in run:
                result["check"][key] = run[key]

    def _read_lines(self, target, encoding, line_start, line_end):
        """Line-mode read as numbered text, one "N<TAB>line" per line.

        Reads in bounded pieces, so memory stays bounded however long
        a line or the file is, and stops at MAX_READ_LINES lines or
        max_read_chars. next_line says where to continue; total_lines
        appears only when the read reaches the end of the file. A
        line_start past the end returns the file's last lines (up to
        PAST_END_LINES) and a note, instead of nothing. A negative
        line_start -K returns the last K lines (up to MAX_READ_LINES),
        numbered as they are in the file.
        """
        tail = None
        if line_start is not None and int(line_start) < 0:
            if line_end is not None:
                raise RuntimeFault("invalid_arguments",
                                   "line_end cannot be combined with a negative line_start, which reads the last lines")
            tail = min(-int(line_start), MAX_READ_LINES)
        # A tail read keeps its lines in skipped, as start is past any file.
        start = sys.maxsize if tail else max(1, int(line_start or 1))
        last = int(line_end) if line_end is not None else None
        if last is not None and last < start:
            raise RuntimeFault("invalid_arguments", "line_end must be >= line_start")
        budget = int(self.cfg.max_read_chars)
        try:
            with target.open("rb") as handle:
                if b"\x00" in handle.read(8192):
                    raise RuntimeFault("binary_file", "Binary file read is not supported")
            out, used, n, end, more = [], 0, 0, start - 1, False
            # The lines skipped before start, in case it is past the end.
            skipped = deque(maxlen=tail or PAST_END_LINES)
            # newline="\n": only LF ends a line, like ripgrep, so a
            # lone CR stays inside its line.
            with target.open("r", encoding=encoding, errors="replace", newline="\n") as handle:
                while True:
                    line = handle.readline(budget + 1)
                    if not line:
                        break
                    n += 1
                    if n == 1:
                        line, _ = _split_bom(line)
                        if not line:
                            # Only a byte order mark: no lines at all.
                            n = 0
                            break
                    length, piece = len(line), line
                    if not line.endswith("\n"):
                        # Over-long line: skip the rest without
                        # holding it in memory, counting its length.
                        while rest := handle.readline(65536):
                            length, piece = length + len(rest), rest
                            if rest.endswith("\n"):
                                break
                    length -= len(piece) - len(piece.rstrip("\r\n"))
                    if n < start:
                        # Only what a numbered line shows: a tail read keeps
                        # up to MAX_READ_LINES of these.
                        skipped.append((n, line.rstrip("\r\n")[:MAX_LINE_CHARS], length))
                        continue
                    text = _numbered(n, line.rstrip("\r\n"), length, budget)
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
            # Show where the file ends, newest lines kept within budget.
            for number, line, length in reversed(skipped):
                text = _numbered(number, line.rstrip("\r\n"), length, budget)
                if out and used + len(text) + 1 > budget:
                    break
                out.insert(0, text)
                used += len(text) + 1
            if tail:
                wanted = min(tail, n)
                result = {"path": str(target), "content": "\n".join(out), "line_start": n - len(out) + 1 if n else 0,
                          "line_end": n, "truncated": len(out) < wanted, "total_lines": n}
                if result["truncated"]:
                    result["note"] = f"Only the last {len(out)} of the {wanted} lines asked for fit in max_read_chars."
                return result
            return {"path": str(target), "content": "\n".join(out),
                    "line_start": n - len(out) + 1 if n else 0, "line_end": n, "truncated": False, "total_lines": n,
                    "note": f"line_start {start} is past the end; the file has {n} lines."
                            + (f" Showing its last {len(out)}." if out else "")}
        result = {"path": str(target), "content": "\n".join(out), "line_start": start,
                  "line_end": end, "truncated": more}
        if more:
            result["next_line"] = end + 1
        else:
            result["total_lines"] = n
        return result

    @staticmethod
    def _read_text(target, encoding):
        """Read a file as text, line endings untouched, plus its dominant
        ending and whether it starts with a byte order mark (removed)."""
        text, bom = _split_bom(target.read_bytes().decode(encoding))
        return text, _detect_eol(text), bom

    @staticmethod
    def _apply_edits(original, eol, edits, mismatch):
        """Apply (old, new, expected) edits in order, all-or-nothing.

        Edit strings arrive with LF line breaks. The LF-normalized view
        is what the agent reads, so matches are found there (exactly, or
        through the fallback tiers of _find_spans) and decide counts and
        errors. Each match is mapped back to the file text as-is and
        replaced there with new_text in the dominant line ending, so
        untouched bytes (mixed endings, stray CRs) stay exact and a
        match never splits a CRLF pair. When that would give a different
        result in the LF view, the normalized result is written back
        with the dominant ending instead.
        Returns (content, counts, tiers, eol_for_write), tiers naming how
        each edit matched; mismatch(index, expected, found, hint) builds
        the error when the LF view does not match. found is a count, or
        the per-tier summary ("0 exact, 2 ignoring typography, ...") when
        a fallback tier found the wrong number; hint is _miss_hint's
        closest lines or _fallback_miss's match lines.
        """
        view, exact = _lf(original), original
        counts, tiers = [], []
        # Line numbers in old_text come from a read taken before the
        # call: once an edit changes the line count, they are refused.
        shifted = False
        for index, (old, new, expected) in enumerate(edits):
            spans, tier, tried = _find_spans(view, old, new, expected)
            if tier == "line_numbers" and shifted:
                raise mismatch(index, expected, 0, NUMBERED_SHIFT_HINT)
            if tier is None:
                found = len(spans)
                if any(matches for _, matches in tried):
                    found, hint = _fallback_miss(view, found, tried)
                else:
                    hint = _miss_hint(view, old, new, found, expected)
                if not found and _split_numbered(old, consecutive=False) is not None:
                    hint = NUMBERED_HINT + hint
                raise mismatch(index, expected, found, hint)
            if tier == "line_numbers":
                new = _strip_numbers(new)
            if exact is not None:
                written = new.replace("\n", "\r\n") if eol == "\r\n" else new
                exact = _replace_spans(exact, _source_spans(exact, spans), written)
            lines = view.count("\n")
            view = _replace_spans(view, spans, new)
            shifted = shifted or view.count("\n") != lines
            if exact is not None and _lf(exact) != view:
                exact = None
            counts.append(len(spans))
            tiers.append(tier)
        if exact is not None:
            return exact, counts, tiers, None
        return view, counts, tiers, eol

    @staticmethod
    def _atomic_write(target, data, encoding, eol=None, bom=False):
        """Write data atomically and return the bytes written.

        eol "\n" or "\r\n" normalizes every line ending to it; None
        writes data verbatim. bom puts a byte order mark first. Bytes
        are written directly, so Python never translates LF to the
        platform line ending.
        """
        if eol is not None:
            data = _lf(data)
            if eol == "\r\n":
                data = data.replace("\n", "\r\n")
        if bom:
            data = BOM + data
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
        root, note = self._resolve_existing(path, "read", retry=True, cwd=base)
        maximum = max(1, min(int(max_results), 5000))
        rg = shutil.which("rg")
        if root.is_file():
            if rg:
                out = self._search_rg_file(rg, root, query, case_sensitive, maximum, files_only, fixed_string, context, applied)
            else:
                out = self._search_python_file(root, query, case_sensitive, maximum, files_only, fixed_string, context, int(max_file_size_bytes), applied)
        elif not root.is_dir():
            raise RuntimeFault("not_directory", f"Not a directory: {root}")
        elif rg:
            out = self._search_rg(rg, root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, bool(include_hidden))
        else:
            out = self._search_python(root, query, glob or [], applied, case_sensitive, maximum, files_only, fixed_string, context, int(max_file_size_bytes), bool(include_hidden))
        if not files_only:
            self._group_by_file(out)
        return self._noted(self._short_paths(base, out), note)

    @staticmethod
    def _group_by_file(out):
        """One entry per file holding its matches, not the path on every match.

        Saves tokens on files with many hits; match_count is the total.
        """
        grouped = {}
        for entry in out["results"]:
            grouped.setdefault(entry.pop("path"), []).append(entry)
        out["results"] = [{"path": path, "matches": matches} for path, matches in grouped.items()]
        out["match_count"] = sum(len(matches) for matches in grouped.values())
        return out

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

    def multi_search(self, searches, invalid=None):
        """Run several independent searches in parallel within one tool call.

        Clients that issue one tool call per model round-trip use this to
        fan out multiple queries without paying a round-trip per search.
        Each entry takes the same arguments as the search tool plus an
        optional label; failures are isolated per entry. invalid maps an
        entry index to its argument error (found by the server), which
        that entry reports instead of running.
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
            if index in (invalid or {}):
                results[index] = {"index": index, "label": label,
                                  "error": {"code": "invalid_arguments", "message": invalid[index]}}
                return
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

    def _run_rg(self, command, cwd=None):
        try:
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=RG_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                                  env=safe_environment(self.cfg))
        except subprocess.TimeoutExpired:
            raise RuntimeFault("timeout", f"Search timed out after {RG_TIMEOUT_SECONDS} s. Narrow it: a smaller "
                               "path, a glob such as [\"*.py\"], or excludes for big folders.") from None

    def _run_rg_regex(self, command, out_note, cwd=None):
        """Run a content search; a regex ripgrep's default engine rejects
        but PCRE2 handles (look-around, backreferences: ripgrep then
        suggests --pcre2) is run again with --pcre2, and out_note gets
        a note saying so. Returns the result to report."""
        result = self._run_rg(command, cwd=cwd)
        if result.returncode == 2 and "--pcre2" in (result.stderr or "") and "-F" not in command:
            retry = self._run_rg([command[0], "--pcre2", *command[1:]], cwd=cwd)
            err = retry.stderr or ""
            if "pcre2 is not available" in err.lower():
                out_note.append("The regex needs look-around or backreferences, but this ripgrep build has no "
                                "PCRE2 (PCRE2 unavailable); rewrite it without them.")
            # Exit 2 without a compile error: the search ran, some files could not be read or matched.
            elif retry.returncode in (0, 1) or not any(mark in err for mark in ("regex parse error", "error compiling pattern")):
                out_note.append("The regex needs look-around or backreferences, so it ran with ripgrep's --pcre2.")
                return retry
        return result

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
            result = self._run_rg(command, cwd=str(root))
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
        # -e: a query starting with - (--verbose, -1) is a pattern, not an option.
        command += ["-e", query, str(root)]
        notes = []
        result = self._run_rg_regex(command, notes, cwd=str(root))
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        # Up to max_results per file and SEARCH_SPREAD_FACTOR times that in
        # all, so a cut page can be spread across files; ripgrep's output
        # is read whole, so the total is exact.
        matches = self._rg_matches(events, maximum * SEARCH_SPREAD_FACTOR, context, per_file=maximum)
        total = sum(1 for event in events if event.get("type") == "match")
        out = {"engine": "ripgrep", **_spread_page(matches, maximum, total), "exit_code": result.returncode,
               "applied_excludes": excludes}
        if notes:
            out["note"] = notes[0]
        err = self._rg_error(result)
        if err is not None:
            out["error"] = err
        return out

    @staticmethod
    def _rg_matches(events, maximum, context, per_file=None):
        matches, seen = [], {}
        for i, event in enumerate(events):
            if event.get("type") != "match":
                continue
            data = event["data"]
            path = _rg_text(data["path"])
            seen[path] = seen.get(path, 0) + 1
            if per_file is not None and seen[path] > per_file:
                continue
            entry = {"path": path, "line": data["line_number"],
                     "text": _cap_line((_rg_text(data["lines"]) or "").rstrip("\r\n"))}
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
            lines.append(f"{data['line_number']}\t" + _cap_line((_rg_text(data["lines"]) or "").rstrip("\r\n")))
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
        # -e: a query starting with - (--verbose, -1) is a pattern, not an option.
        command += ["-e", query, str(target)]
        notes = []
        result = self._run_rg_regex(command, notes)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        matches = self._rg_matches(events, maximum, context)
        total = sum(1 for event in events if event.get("type") == "match")
        out = {"engine": "ripgrep", **_spread_page(matches, maximum, total), "exit_code": result.returncode,
               "applied_excludes": applied}
        if notes:
            out["note"] = notes[0]
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
        # Content: up to maximum per file, SEARCH_SPREAD_FACTOR times that
        # in all (see _spread_page), every match counted. Files: one extra
        # marks a cut page.
        limit = maximum + 1 if files_only else maximum * SEARCH_SPREAD_FACTOR
        counted, stopped = 0, False
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
                    kept = 0
                    for number, line in enumerate(lines, 1):
                        if pattern is not None:
                            matched = pattern.search(line) is not None
                        elif case_sensitive:
                            matched = query in line
                        else:
                            matched = query.lower() in line.lower()
                        if matched:
                            counted += 1
                            if kept >= maximum:
                                continue
                            kept += 1
                            entry = {"path": str(item), "line": number, "text": _cap_line(line)}
                            if context > 0:
                                entry["before"] = [f"{n}\t{_cap_line(lines[n - 1])}" for n in range(max(1, number - context), number)]
                                entry["after"] = [f"{n}\t{_cap_line(lines[n - 1])}" for n in range(number + 1, min(len(lines), number + context) + 1)]
                            results.append(entry)
                except OSError:
                    pass
            if len(results) >= limit:
                stopped = True
                break
        if not files_only:
            return {"engine": "python", **_spread_page(results, maximum, counted, not stopped),
                    "applied_excludes": excludes}
        # Collected up to one extra result: it marks a cut page.
        return {"engine": "python", "results": results[:maximum], "truncated": len(results) > maximum, "applied_excludes": excludes}

    def git(self, action, args=None, cwd=None, include_untracked=False):
        work = self.paths.resolve(cwd or ".", cwd=self.cwd, access="read", must_exist=True)
        if include_untracked:
            if action != "diff":
                raise RuntimeFault("invalid_arguments", "include_untracked works with action diff only")
            return self._diff_untracked(work, list(args or []))
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
                if lowered.endswith((".pfx", ".p12", ".pem", ".key")):
                    raise RuntimeFault("secret_file",
                        f"Refusing to stage a likely secret: {spec}. Private keys must never enter git history.")
        # core.quotepath=false: show non-ASCII paths as themselves, not
        # octal escapes like "f\303\251.txt".
        # kill_on_timeout: a git command must not linger in the background.
        # The presets only read: no optional locks for them.
        return self.execute(["git", "-c", "core.quotepath=false", "-C", str(work)] + subcommand + ([] if action == "run" else list(args or [])), cwd=str(work),
                            env=git_environment(self.cfg, action != "run"), kill_on_timeout=True)

    def _diff_untracked(self, work, args):
        """git diff that also shows untracked files as added.

        The plain diff runs against the real index, which is never
        touched. The untracked files .gitignore does not hide are then
        added as intent-to-add to a fresh throwaway index
        (GIT_INDEX_FILE) that holds nothing else, and diffed against it
        with the same args, so only they show; the two parts are joined.
        (A copy of the real index would not do: its new mtime makes git
        trust stale stat data and miss a change made in the same second
        as the last index write.) Each new file's part is cut to
        UNTRACKED_DIFF_BYTES, so one huge file cannot flood the reply.
        Only the untracked files under the pathspecs after "--" in args
        are listed (all of them without "--"); nested repositories and
        files over max_file_read_bytes are left out and listed in
        untracked_skipped. untracked_files counts the new files the diff
        shows (its "diff --git" headers, so 0 with --stat and the like);
        when adding them fails, the plain diff comes back with a note.
        """
        # color.diff=never (it beats color.ui): colour from git config would
        # hide the "diff --git" headers new files are counted and cut at. A
        # --color flag beats it, so that is refused below.
        git = ["git", "-c", "core.quotepath=false", "-c", "color.diff=never", "-C", str(work)]
        # Every git command here, both diffs included, runs through run()
        # below, outside execute, under the command policy a plain diff gets.
        self.policy.authorize_command(git + ["diff", *args], work, False)
        env = {**safe_environment(self.cfg), **git_environment(self.cfg, True)}
        started = time.monotonic()

        def run(argv, extra=None):
            try:
                return subprocess.run(git + argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
                                      env={**env, **(extra or {})})
            except subprocess.TimeoutExpired:
                raise RuntimeFault("timeout", f"git {argv[0]} timed out after 60 s") from None

        listed = run(["ls-files", "-z", "--others", "--exclude-standard", "--",
                      *((args[args.index("--") + 1:] if "--" in args else []) or [":/"])])
        if listed.returncode:
            # Not a repository, say: the plain diff says why.
            return self.git("diff", args, str(work))
        new, skipped = [], []
        for name in listed.stdout.decode("utf-8", "replace").split("\0"):
            if not name:
                continue
            if name.endswith("/"):
                # An untracked nested repository: intent-to-add fails on it.
                skipped.append({"path": name, "reason": "nested_repository"})
                continue
            try:
                size = os.lstat(work / name).st_size
            except OSError:
                size = 0
            # git reads each new file whole, and its diff is redacted whole.
            if size > int(self.cfg.max_file_read_bytes):
                skipped.append({"path": name, "reason": "too_large", "bytes": size})
            else:
                new.append(name)
        # The new files are diffed against the working tree: with a staged
        # view or a revision the throwaway index would show every tracked
        # file as deleted.
        # The value of -S, -G, -O or --diff-filter may come as its own
        # argument (-S HEAD); it is not a revision.
        values = ("-S", "-G", "-O", "--diff-filter")
        # An option given as another's value (-S in -O -S HEAD) takes no value itself.
        kept, skip_next = [], False
        for a in args:
            if not skip_next:
                kept.append(a)
            skip_next = a in values and not skip_next
        # Only options count for --cached and colour: not an option's value
        # (-S --cached) or a pathspec after --.
        flags = kept[:kept.index("--")] if "--" in kept else kept
        staged = next((a for a in flags if a in ("--cached", "--staged") or a.startswith("--merge-base")), None)
        revs = run(["rev-parse", "--revs-only", "--no-flags", *kept])
        # Colour codes hide the "diff --git" headers new files are counted and cut at.
        # The last --color/--no-color wins; --color=auto never colours a pipe.
        color = next((a for a in flags if a.startswith("--color-words") or a == "--word-diff=color"), None)
        last = next((a for a in reversed(flags) if a in ("--color", "--no-color") or a.startswith("--color=")), None)
        if color is None and last not in (None, "--no-color") and last.partition("=")[2].lower() not in ("never", "auto"):
            color = last
        if color:
            raise RuntimeFault("invalid_arguments",
                               f"include_untracked counts and cuts new files in plain diff output, so it cannot be "
                               f"combined with {color}; drop it (git diff without include_untracked takes it).")
        if staged or (revs.returncode == 0 and revs.stdout.strip()):
            raise RuntimeFault("invalid_arguments",
                               f"include_untracked diffs new files against the working tree, so it cannot be "
                               f"combined with {staged or 'a revision'}; drop one of them (git diff without "
                               "include_untracked takes any args).")
        command = git + ["diff", *args]
        result = run(["diff", *args])
        out, err = result.stdout, result.stderr
        note, included = None, 0
        fd, index = tempfile.mkstemp(prefix=f"{SPILL_PREFIX}index-")
        os.close(fd)
        names = index + ".paths"
        try:
            # git makes the index itself; an empty file is not one.
            os.unlink(index)
            extra = {"GIT_INDEX_FILE": index}
            # --exit-code and --quiet make git exit 1 when something differs.
            differs = 1 if any(a in ("--exit-code", "--quiet") for a in flags) else 0
            if new and result.returncode in (0, differs):
                first = result.returncode
                Path(names).write_bytes("\0".join(new).encode("utf-8"))
                # Literal: a file named like a glob stages only itself.
                adding = ["add", "--intent-to-add", f"--pathspec-from-file={names}", "--pathspec-file-nul"]
                added = run(adding, {**extra, "GIT_LITERAL_PATHSPECS": "1"})
                if added.returncode:
                    # Keep the plain diff; say why the new files are missing.
                    reason = redact(added.stderr.decode("utf-8", "replace")).strip()[:500]
                    note = f"git add --intent-to-add failed, so only tracked changes are shown: {reason}"
                else:
                    result = run(["diff", *args], extra)
                    if first and not result.returncode:
                        # Tracked files differ even if no new file is shown.
                        result.returncode = first
                    # What the diff shows: a pathspec without "--" or a filter can leave some out.
                    included = sum(1 for line in result.stdout.split(b"\n") if line.startswith(b"diff --git "))
                    out, err = out + result.stdout, err + result.stderr
        finally:
            for leftover in (index, names, index + ".lock"):
                with contextlib.suppress(OSError):
                    os.unlink(leftover)
        out, out_cut, out_path = self._inline_output(
            "stdout", _cap_new_files(redact(out.decode("utf-8", "replace"))))
        err, err_cut, err_path = self._inline_output("stderr", redact(err.decode("utf-8", "replace")))
        reply = {"success": result.returncode == 0, "command": command, "cwd": str(work),
                 "exit_code": result.returncode, "stdout": out, "stderr": err,
                 "stdout_truncated": out_cut, "stderr_truncated": err_cut,
                 "duration_ms": int((time.monotonic() - started) * 1000),
                 "error_type": None if result.returncode == 0 else "process_exit", "untracked_files": included}
        if skipped:
            reply["untracked_skipped"] = skipped[:UNTRACKED_SKIPPED_SHOWN]
        if note:
            reply["note"] = note
        if out_path:
            reply["stdout_path"] = out_path
        if err_path:
            reply["stderr_path"] = err_path
        return reply

    @staticmethod
    def _feed_stdin(proc, data):
        # On a thread: a child that never reads stdin must not block the call.
        try:
            proc.stdin.write(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    def spill(self, prefix, text, suffix=".log"):
        """Save text to the spill folder and return the file path.

        Only the newest SPILL_KEEP spill files are kept, so the folder
        stays bounded over long sessions. Raises OSError when there is
        no usable spill folder.
        """
        if self.spill_dir is None:
            raise OSError("no usable spill_dir")
        fd, name = tempfile.mkstemp(prefix=f"{SPILL_PREFIX}{prefix}-", suffix=suffix, dir=self.spill_dir)
        with os.fdopen(fd, "wb") as handle:
            # "replace": a lone surrogate (possible in Windows file
            # names) has no UTF-8 encoding.
            handle.write(text.encode("utf-8", "replace"))
        # Prune only our own files, and skip any that fail (locked by
        # a reader, or already pruned by a concurrent call).
        saved = []
        for item in self.spill_dir.glob(f"{SPILL_PREFIX}*"):
            try:
                if item.is_file():
                    saved.append((item.stat().st_mtime, str(item)))
            except OSError:
                continue
        saved.sort(reverse=True)
        for _, old in saved[SPILL_KEEP:]:
            try:
                os.unlink(old)
            except OSError:
                continue
        return name

    def _inline_output(self, stream, text):
        """Clip command output for the reply, saving it whole when cut.

        The reply keeps head and tail (max_inline_output_bytes); the
        saved log keeps up to max_capture_bytes. Returns (text,
        truncated, saved_path_or_None).
        """
        full, full_cut = _clip_ends(text, int(self.cfg.max_capture_bytes))
        shown, shown_cut = _clip_ends(full, min(int(self.cfg.max_inline_output_bytes),
                                                int(self.cfg.max_capture_bytes)))
        path = None
        if shown_cut:
            try:
                path = self.spill(f"execute-{stream}", full)
            except OSError:
                pass
        return shown, full_cut or shown_cut, path

    def execute(self, command, cwd=None, timeout=None, shell=False, env=None, input=None,
                kill_on_timeout=False):
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
        # With no timeout given, a command that may move to the background
        # waits execute_background_after_seconds, well inside host and
        # gateway timeouts; one that must stay bounded keeps the old default.
        fallback = self.cfg.default_timeout_seconds if kill_on_timeout else self.cfg.execute_background_after_seconds
        effective_timeout = float(timeout or fallback)
        deadline = started + effective_timeout
        try:
            # A process entry, so a command still running at its timeout
            # can keep running and be followed with the process tool.
            # No input: an empty stdin, not a closed pipe, which tools
            # such as rg read as piped input instead of searching the cwd.
            item = self.processes.spawn(command, str(work), shell, env, f"exec-{uuid.uuid4().hex[:10]}",
                                        enforce_limit=False,
                                        stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL)
        except OSError as e:
            return {"success": False, "command": command, "cwd": str(work), "exit_code": None,
                    "stdout": "", "stderr": str(e), "duration_ms": int((time.monotonic() - started) * 1000),
                    "error_type": "process_start"}
        proc = item.proc
        if stdin_bytes is not None:
            threading.Thread(target=self._feed_stdin, args=(proc, stdin_bytes), daemon=True).start()
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass

        def drain():
            # Let the readers drain the pipes, both by one deadline (with a
            # second of grace past it, for a command that ended or was
            # stopped right at it): a grandchild that holds them open must
            # not hold up the reply.
            until = max(deadline, time.monotonic() + 1.0)
            for pump in item.pumps:
                pump.join(max(0.0, until - time.monotonic()))

        if proc.poll() is not None:
            drain()
        error_type, hint = None, None
        running = proc.poll() is None
        if running or held(item):
            # Still running, or exited while a process it started holds
            # its output open: either way it can move to the background.
            # active() counts this command either way, so <= leaves it room.
            if not kill_on_timeout and self.processes.active() <= int(self.cfg.max_processes):
                error_type = "backgrounded"
            else:
                try:
                    self.processes.stop(item.id, True)
                except subprocess.TimeoutExpired:
                    # Still not gone after the forced stop's waits; its
                    # entry is dropped below all the same.
                    pass
                error_type = "timeout"
                drain()
                limit = f"max_processes ({self.cfg.max_processes}) is reached"
                if running:
                    if not kill_on_timeout:
                        hint = f"The command was stopped at its timeout instead of moving to the background, as {limit}."
                else:
                    hint = (f"The command exited with code {proc.returncode}, but a process it started "
                            "still held its output open at the timeout")
                    if not kill_on_timeout:
                        hint += f"; as {limit}, it was not moved to the background"
                    hint += ("; stopping what it left running was tried, but something still holds its output."
                             if held(item) else ", so what it left running was stopped.")
        elif proc.returncode != 0:
            error_type = "process_exit"
        if error_type != "backgrounded":
            self.processes.discard(item)
        with item.lock:
            chunks, last, dropped = list(item.chunks), item.next_seq - 1, set(item.dropped)
        # Chunks are redacted one read at a time; redacting the whole
        # again also catches a secret split across two reads.
        out, err = (redact("".join(c.text for c in chunks if c.stream == stream)) for stream in ("stdout", "stderr"))
        # Over process_buffer_bytes the oldest output is gone, from
        # either stream or both.
        marker = "...[earlier output dropped: over process_buffer_bytes]...\n"
        if "stdout" in dropped:
            out = marker + out
        if "stderr" in dropped:
            err = marker + err
        # PowerShell 5.1 rejects && and || with this (unlocalized) error id.
        and_or_failed = (shell and isinstance(command, str) and ("&&" in command or "||" in command)
                         and "InvalidEndOfLine" in err)
        if not out and not err and error_type not in (None, "backgrounded"):
            err = ("[no output captured on stdout/stderr; the child may have written directly "
                   "to the console or died during startup (e.g. missing environment)]")
        out, out_cut, out_path = self._inline_output("stdout", out)
        err, err_cut, err_path = self._inline_output("stderr", err)
        out_cut = out_cut or "stdout" in dropped
        err_cut = err_cut or "stderr" in dropped
        result = {"success": proc.returncode == 0 and error_type is None, "command": command, "cwd": str(work),
                "exit_code": proc.returncode, "stdout": out, "stderr": err,
                "stdout_truncated": out_cut, "stderr_truncated": err_cut,
                "duration_ms": int((time.monotonic() - started) * 1000), "error_type": error_type}
        if out_path:
            result["stdout_path"] = out_path
        if err_path:
            result["stderr_path"] = err_path
        if error_type == "backgrounded":
            result["process_id"] = item.id
            result["next_after"] = last
            if running:
                hint = (f"Still running after {effective_timeout:g} s, so it keeps running in the background "
                        f"as process {item.id}: process read with that process_id (after={last}, wait_ms) "
                        "shows new output, process stop ends it. Do not start another copy while it runs.")
            else:
                hint = (f"The command exited, but a process it started still held its output open after "
                        f"{effective_timeout:g} s, so it is kept as process {item.id}: process read with that "
                        f"process_id (after={last}, wait_ms) shows new output, process stop ends it.")
        if and_or_failed:
            hint = ("Windows PowerShell 5.1 has no && or ||. Chain with ; "
                    "(or 'if ($?) { ... }'), or make separate calls.")
        if hint:
            result["hint"] = hint
        if timeout is not None and effective_timeout > float(self.cfg.default_timeout_seconds):
            result["warning"] = (
                "timeout exceeds the configured default "
                f"({self.cfg.default_timeout_seconds} s); long foreground waits risk "
                "gateway/outpost timeouts and block the server — use the process tool for waits and polling.")
        return result
