from __future__ import annotations
import atexit
import difflib
import json
import os
import re
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from . import __version__
from .config import Config
from .errors import RuntimeFault
from .security import PathPolicy, Policy
from .processes import ProcessManager
from .capabilities import Capabilities, _clip
from .state import StateStore
from .todos import TodoStore

PROTOCOL = "2025-06-18"
MAX_BATCH_CALLS = 16
# Tool names models know from other agents, for batch entries (the host
# checks top-level names itself); the result says which tool ran.
TOOL_ALIASES = {"bash": "execute", "shell": "execute", "sh": "execute", "powershell": "execute",
                "pwsh": "execute", "cmd": "execute", "grep": "search", "rg": "search", "ripgrep": "search"}
STDOUT_WRITE_TIMEOUT_SECONDS = 30
MAX_PENDING_REQUESTS = 128
# Sent in the initialize result; MCP clients may show it to the model.
INSTRUCTIONS = (
    "LocalForge rules: "
    "Put independent reads/searches in ONE batch call (max 16, match results by index/label); "
    "fan out searches with multi_search. "
    "Edit files only with filesystem write/replace_text/multi_edit/apply_patch, "
    "never via execute, process, or any script. "
    "Long runs (builds, tests): process start + read with wait_ms, not a long execute timeout. "
    "Stage explicit files only; git add -A/./--all is blocked."
)

def _guarded_write(emit, timeout):
    """Run emit() on a helper thread with a timeout.

    Returns True when emit() completed, False when it failed
    or is still blocked. A blocked helper stays blocked only
    until the pipe drains or breaks, so a client that stops
    reading cannot pin a worker thread forever.
    """
    done = threading.Event()
    ok = False

    def _run():
        nonlocal ok
        try:
            emit()
            ok = True
        except (BrokenPipeError, OSError, ValueError):
            ok = False
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout):
        return False
    return ok

def _bare_tool_name(name):
    """Strip a host prefix from a tool name.

    Hosts may expose tools under prefixed names such as
    "MyHost_localforge_mcp__search". Local tool names never
    contain "__", so the segment after the last "__" is the
    tool this server knows.
    """
    return name.split("__")[-1] if "__" in name else name

# Smallest per-string cut tried before long lists are trimmed instead.
PREVIEW_MIN_CUT = 256
PREVIEW_CUT_NOTE = "; full result in result_path]"
# A string only this much over the cut stays whole: the marker would
# make it longer, not shorter.
PREVIEW_MARKER_ROOM = 80
# Cursors and counts that describe the uncut result: following them
# from a cut preview would skip what was cut, so they are dropped.
PREVIEW_STALE_KEYS = ("next_line", "next_offset", "next_after", "line_end", "bytes_returned")

def _safe_text(text):
    # Lone surrogates (Windows file names) cannot be sent as UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")

def _cut_text(text, limit):
    """Head of text up to limit characters, on a line boundary when possible."""
    text = _safe_text(text)
    if len(text) <= limit + PREVIEW_MARKER_ROOM:
        return text
    head = text[:limit]
    newline = head.rfind("\n")
    # Snap to a line end only when that keeps most of the head, so a
    # short first line cannot swallow a long one.
    if newline >= limit // 2:
        head = head[:newline + 1]
        total = text.count("\n") + (not text.endswith("\n"))
        shown = f"lines 1-{head.count(chr(10))} of {total}"
    else:
        shown = f"{len(head.encode('utf-8'))} of {len(text.encode('utf-8'))} bytes"
    return f"{head}...[cut: showing {shown}{PREVIEW_CUT_NOTE}"

def _keeps_whole(key):
    # Paths and ids are handles the agent passes back: never cut them.
    return isinstance(key, str) and (key == "cwd" or key.endswith("path") or key.endswith("_id"))

def _shrink(value, limit, items=None, dropped=None):
    """Copy of a JSON value with strings cut to limit and lists to items.

    Returns (copy, cut), cut telling whether anything inside was cut.
    In every dict whose subtree lost part of a string or list, the
    cursors that described the uncut value are dropped (their names
    added to the dropped set, when given), and truncated (or the
    child's own <key>_truncated) is set to true where the dict already
    has that key; no such key is added.
    """
    if isinstance(value, str):
        out = _cut_text(value, limit)
        return out, len(out) != len(_safe_text(value))
    if isinstance(value, dict):
        out, cut = {}, []
        for key, item in value.items():
            name = _safe_text(key) if isinstance(key, str) else key
            if isinstance(item, str) and _keeps_whole(key):
                out[name] = _safe_text(item)
                continue
            out[name], child_cut = _shrink(item, limit, items, dropped)
            if child_cut:
                cut.append(name)
        if cut:
            for key in PREVIEW_STALE_KEYS:
                if key in out:
                    del out[key]
                    if dropped is not None:
                        dropped.add(key)
            if "truncated" in out:
                out["truncated"] = True
            for key in cut:
                if f"{key}_truncated" in out:
                    out[f"{key}_truncated"] = True
        return out, bool(cut)
    if isinstance(value, list):
        kept, cut = [], False
        for item in value[:items]:
            shrunk, child_cut = _shrink(item, limit, items, dropped)
            kept.append(shrunk)
            cut = cut or child_cut
        if len(kept) < len(value):
            kept.append(f"...[cut: showing {len(kept)} of {len(value)} items{PREVIEW_CUT_NOTE}")
            cut = True
        return kept, cut
    return value, False

_INTEGER = re.compile(r"[+-]?\d+")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

def _shown(value):
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."

def _coerce(schema, value, where, errors):
    """value fixed to its schema type, where the form is unambiguous.

    Models often send "false" or "5" as strings, a bare string for a
    list of strings, or an array or object as a JSON string. Only those
    forms are fixed: "true"/"false" (any case) for booleans, digit
    strings for integers and numbers, a string that is not a JSON list
    as a one-item list of strings (also one that is JSON but not a
    list of strings, like the glob "[1]"), and a JSON string holding the
    expected array or object. Any other string for a boolean, number,
    array or object adds "<where>: expected ..., got ..." to errors, as
    does a non-boolean (other than null) for a boolean. Other values
    pass through unchanged.
    """
    kind = schema.get("type")
    if isinstance(value, str):
        text = value.strip()
        if kind == "boolean":
            if text.lower() in ("true", "false"):
                return text.lower() == "true"
            errors.append(f"{where}: expected true or false, got {_shown(value)}")
            return value
        if kind in ("integer", "number"):
            if _INTEGER.fullmatch(text):
                try:
                    return int(text)
                except ValueError:
                    # Past Python's int-from-string digit limit (4300).
                    errors.append(f"{where}: expected {'an integer' if kind == 'integer' else 'a number'}, "
                                  f"got a {len(text)}-digit number")
                    return value
            if kind == "number" and _NUMBER.fullmatch(text):
                return float(text)
            errors.append(f"{where}: expected {'an integer' if kind == 'integer' else 'a number'}, got {_shown(value)}")
            return value
        if kind in ("array", "object"):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            strings = kind == "array" and schema.get("items", {}).get("type") == "string"
            # "[1]" is JSON, but for a list of strings it is a one-item
            # list (a character-class glob), not [1].
            if strings and isinstance(parsed, list) and not all(isinstance(item, str) for item in parsed):
                return [value]
            if isinstance(parsed, list if kind == "array" else dict):
                value = parsed
            elif strings:
                return [value]
            else:
                wanted = "an array (a JSON list)" if kind == "array" else "an object"
                errors.append(f"{where}: expected {wanted}, got {_shown(value)}")
                return value
    if kind == "boolean" and value is not None and not isinstance(value, bool):
        # 1 or [] would pass a truthiness test, e.g. delete's recursive.
        errors.append(f"{where}: expected true or false, got {_shown(value)}")
        return value
    if kind == "array" and isinstance(value, list) and "items" in schema:
        return [_coerce(schema["items"], item, f"{where}[{index}]", errors) for index, item in enumerate(value)]
    if kind == "object" and isinstance(value, dict) and "properties" in schema:
        return _coerce_fields(schema["properties"], value, f"{where}.", errors)
    return value

def _coerce_fields(properties, args, prefix, errors):
    return {key: _coerce(properties[key], item, prefix + key, errors) if key in properties else item
            for key, item in args.items()}

SCHEMAS = {
    "workspace": {"type": "object", "properties": {
        "action": {"enum": ["get", "set_cwd"], "description": "get returns roots and Git root; set_cwd changes runtime directory."},
        "path": {"type": "string", "description": "Directory for set_cwd, relative to cwd or absolute, must resolve inside read roots."}}, "required": ["action"]},
    "filesystem": {"type": "object", "properties": {
        "action": {"enum": ["read", "list", "stat", "write", "replace_text", "multi_edit", "apply_patch", "mkdir", "delete", "move", "copy"], "description": "read defaults to byte mode; pass line_start/line_end for line mode, which returns numbered text (one 'N<TAB>line' per line, up to 2000 lines, each cut to 2000 characters; next_line continues). read on a directory lists it like list, with a note. list is paginated. multi_edit applies several exact replacements to one file, validated end-to-end and written once. apply_patch takes a unified diff string. copy duplicates a file (or a directory with recursive=true) without shell quoting issues."},
        "path": {"type": "string", "description": "Target path, relative to cwd or absolute. Reads need read roots, writes need write roots."},
        "content": {"type": "string", "description": "Full replacement content for write; overwrites the file."},
        "destination": {"type": "string", "description": "Destination path for move/copy; move and copy create missing parent dirs. An existing file there is refused unless overwrite is true; an existing directory is never replaced. A folder cannot go into itself, nor a file be copied onto itself."},
        "overwrite": {"type": "boolean", "description": "Let move/copy replace an existing destination file, default false."},
        "recursive": {"type": "boolean", "description": "true creates parent dirs for mkdir, deletes non-empty dirs for delete, copies directories for copy."},
        "encoding": {"type": "string", "description": "Text encoding, default utf-8."},
        "offset": {"type": "integer", "description": "Byte offset for legacy read mode; list page offset when action is list. Ignored in line mode."},
        "max_bytes": {"type": "integer", "description": "Byte cap for legacy read mode."},
        "old_text": {"type": "string", "description": "Text to find for replace_text; exact, else retried ignoring typographic quotes/dashes/spaces, then trailing whitespace per line, then without read's N<TAB> line-number prefixes (the result then has match)."},
        "new_text": {"type": "string", "description": "Replacement text for replace_text."},
        "expected_occurrences": {"type": "integer", "description": "Required match count for replace_text, default 1."},
        "replace_all": {"type": "boolean", "description": "replace_text: replace every exact match of old_text (at least one) without giving the count; not with expected_occurrences, no typography/whitespace fallback."},
        "edits": {"type": "array", "minItems": 1, "maxItems": 32,
            "description": "Exact replacements for multi_edit, applied in order: [{\"old_text\": \"foo\", \"new_text\": \"bar\"}]. The whole set is validated before anything is written; a failing edit leaves the file untouched. Pass expected_occurrences inside an entry when the text repeats.",
            "items": {"type": "object", "properties": {
                "old_text": {"type": "string", "description": "Text to find; exact, else retried ignoring typographic quotes/dashes/spaces, then trailing whitespace per line, then without read's N<TAB> line-number prefixes (the edit then has match)."},
                "new_text": {"type": "string", "description": "Replacement text."},
                "expected_occurrences": {"type": "integer", "description": "Required match count, default 1."},
                "replace_all": {"type": "boolean", "description": "Replace every exact match (at least one); not with expected_occurrences."}},
                "required": ["old_text", "new_text"]}},
        "line_start": {"type": "integer", "description": "1-based first line for line-mode read, default 1. Past the end of the file, the last lines come back with a note. Negative -K reads the last K lines, numbered as in the file (no line_end)."},
        "line_end": {"type": "integer", "description": "Inclusive last line for line-mode read, default end of file."},
        "limit": {"type": "integer", "description": "Max list entries returned, default 200, clamped 1..1000."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Fnmatch filters on entry name for list."},
        "include_hidden": {"type": "boolean", "description": "Include dotfiles in list, default false."},
        "patch": {"type": "string", "description": "Unified diff for apply_patch (git diff headers are fine): modify or create files, all-or-nothing. Each hunk goes where its context and removed lines match exactly, nearest the stated line; header line counts are not checked. Each file in the reply has diff, added/removed, and hunks [{stated, placed}] for hunks placed elsewhere. Renames and copies are refused (move first)."}}, "required": ["action"]},
    "search": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Text or regex to find; omit only with files_only."},
        "path": {"type": "string", "description": "File or directory to search, relative to cwd or absolute."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Include patterns, relative to the search path, with ripgrep (.gitignore-style) rules: no slash matches at any depth, a slash anchors to the search path, ** spans folders, case-sensitive."},
        "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra excludes, relative to the search path; built-ins (.git, .venv, node_modules, etc.) always apply."},
        "case_sensitive": {"type": "boolean", "description": "Case-sensitive match, default false."},
        "max_results": {"type": "integer", "description": "Match cap 1..5000, default 200; counts matches, not files (files with files_only)."},
        "files_only": {"type": "boolean", "description": "List matching filenames; takes no query."},
        "fixed_string": {"type": "boolean", "description": "Literal match when true, regex when false, default true."},
        "context_lines": {"type": "integer", "description": "Surrounding lines per match, clamped 0..5, ignored with files_only. Returned as before/after lists of 'N<TAB>line'."},
        "include_hidden": {"type": "boolean", "description": "Search hidden files, default false."},
        "max_file_size_bytes": {"type": "integer", "description": "Skip larger files, default 1000000, must be positive."}}},
    "git": {"type": "object", "properties": {
        "action": {"enum": ["status", "diff", "log", "show", "branch", "root", "run"], "description": "Preset operation, or run for an arbitrary git argument array."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Extra args appended to presets, or the full git args for run."},
        "include_untracked": {"type": "boolean", "description": "diff only: also show untracked files that .gitignore does not hide, as added (diffed through a fresh throwaway index after the plain diff; the real index is untouched). Each new file is cut to 8000 bytes; nested repos and files over max_file_read_bytes are listed in untracked_skipped instead. Working tree only: not with --cached/--staged or a revision (invalid_arguments); no colour output (--color, --color=always, --color-words, --word-diff=color are refused); option values must be attached (-U1), except after -S, -G, -O, --diff-filter."},
        "cwd": {"type": "string", "description": "Repository directory, defaults to runtime cwd."}}, "required": ["action"]},
    "execute": {"type": "object", "properties": {
        "command": {"description": "Command to run. Prefer an argv array: no shell parsing, no quoting bugs. A string runs through the configured default_shell, so match its syntax.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "timeout": {"type": "number", "description": "Seconds to wait, not milliseconds; default 120 (execute_background_after_seconds). A command still running then keeps running in the background: error_type backgrounded plus a process_id to follow with process read/stop. Long foreground waits risk gateway/outpost timeouts; use the process tool for waits and polling."},
        "kill_on_timeout": {"type": "boolean", "description": "Stop the command at its timeout (error_type timeout) instead of moving it to the background, default false."},
        "shell": {"type": "boolean", "description": "Rarely needed. Strings auto-enable the shell; set true only to force an argv array through the shell for pipes and operators."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "input": {"type": "string", "description": "Optional stdin text, max 65536 chars."}}, "required": ["command"]},
    "process": {"type": "object", "properties": {
        "action": {"enum": ["start", "read", "wait", "write", "status", "list", "restart", "stop"], "description": "start, read, wait, write, status, list, restart, or stop. start needs command; the others except list need process_id. wait blocks until the process exits, an output line after `after` (default 0: all kept output) matches pattern, or wait_ms passes (default and max 60000), and returns reason (matched, exited, timeout), matched, tail (last tail_lines lines), and next_after (after a match, a wait from it goes on with the line after the matched one; output_held true when it exited but something it started still holds its output; without a pattern that ends the wait after ~1 s): use it instead of polling read during builds and tests."},
        "process_id": {"type": "string", "description": "Stable id; auto-generated when start omits it."},
        "command": {"description": "Command to start. Prefer an argv array: no shell parsing, no quoting bugs. A string runs through the configured default_shell, so match its syntax.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "shell": {"type": "boolean", "description": "Rarely needed. Strings auto-enable the shell; set true only to force an argv array through the shell for pipes and operators."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "after": {"type": "integer", "description": "Log cursor from previous next_after for incremental reads; for wait, pattern only looks at lines that end after it, a line the cursor cuts matched whole (default 0: all kept output)."},
        "limit_bytes": {"type": "integer", "description": "Max log bytes per read."},
        "wait_ms": {"type": "integer", "description": "Wait in milliseconds, max 60000: for read, for new output (default 0); for wait, the most it blocks (default 60000)."},
        "pattern": {"type": "string", "description": "wait: regex searched in each finished output line (stdout or stderr); the first match ends the wait."},
        "tail_lines": {"type": "integer", "description": "wait: how many last output lines to return, default 40, max 1000."},
        "format": {"enum": ["text", "chunks"], "description": "read output shape: text (default) returns stdout and stderr strings; chunks returns per-read chunks with seq and timestamps."},
        "text": {"type": "string", "description": "Stdin text for write."},
        "append_newline": {"type": "boolean", "description": "Append newline to stdin write, default true."},
        "force": {"type": "boolean", "description": "Force-kill the process tree on stop."}}, "required": ["action"]},
    "todo": {"type": "object", "properties": {
        "action": {"enum": ["get", "add", "update", "delete", "clear"], "description": "get lists todos; add appends one; update changes title or status; delete removes one; clear removes all."},
        "id": {"type": "integer", "description": "Todo id from get or add; required for update and delete."},
        "title": {"type": "string", "description": "Task text; required for add, optional for update."},
        "status": {"enum": ["pending", "in_progress", "completed"], "description": "Task state for update."}}, "required": ["action"]},
    "multi_search": {"type": "object", "properties": {
        "searches": {"type": "array", "minItems": 1, "maxItems": 16,
            "description": "Up to 16 independent searches run in parallel, e.g. [{\"query\": \"UserService\", \"glob\": [\"*.py\"]}, {\"query\": \"TODO\", \"files_only\": true}]. Each entry takes the same arguments as the search tool, plus an optional label echoed back with its results.",
            "items": {"type": "object", "properties": {
                "label": {"type": "string", "description": "Optional label echoed back to identify this search in the results."},
                "query": {"type": "string", "description": "Text or regex to find; omit only with files_only."},
                "path": {"type": "string", "description": "File or directory to search, relative to cwd or absolute."},
                "glob": {"type": "array", "items": {"type": "string"}, "description": "Include patterns, relative to the search path, with ripgrep (.gitignore-style) rules: no slash matches at any depth, a slash anchors to the search path, ** spans folders, case-sensitive."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra excludes, relative to the search path; built-ins (.git, .venv, node_modules, etc.) always apply."},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive match, default false."},
                "max_results": {"type": "integer", "description": "Match cap 1..5000, default 200; counts matches, not files (files with files_only)."},
                "files_only": {"type": "boolean", "description": "List matching filenames; takes no query."},
                "fixed_string": {"type": "boolean", "description": "Literal match when true, regex when false, default true."},
                "context_lines": {"type": "integer", "description": "Surrounding lines per match, clamped 0..5, ignored with files_only."},
                "include_hidden": {"type": "boolean", "description": "Search hidden files, default false."},
                "max_file_size_bytes": {"type": "integer", "description": "Skip larger files, default 1000000, must be positive."}}}},
        }, "required": ["searches"]},
    "batch": {"type": "object", "properties": {
        "calls": {"type": "array", "minItems": 1, "maxItems": 16,
            "description": "Tool calls to run in parallel (or in order, with sequential), e.g. [{\"tool\": \"search\", \"arguments\": {\"query\": \"todo\"}}, {\"tool\": \"git\", \"arguments\": {\"action\": \"status\"}}].",
            "items": {"type": "object", "properties": {
                "tool": {"type": "string", "description": "Tool to run: workspace, filesystem, search, multi_search, git, execute, or process. bash/shell/powershell/cmd run as execute (string commands in the configured default_shell, not that shell) and grep/rg as search, with a note."},
                "arguments": {"type": "object", "description": "Arguments for the tool; same shape as a tools/call request."},
                "label": {"type": "string", "description": "Optional label echoed back with this call's result."}},
                "required": ["tool"]}},
        "sequential": {"type": "boolean", "description": "Run the calls one at a time in order and stop at the first failed call (an error, or a result with success false); later calls are reported as skipped, as are calls left once the run has taken longer than execute_background_after_seconds. Default false: run in parallel."},
        }, "required": ["calls"]},
}
DESCRIPTIONS = {
    "workspace": "Inspect workspace and Git root, or change runtime current directory. Call get once at the start of a task: it also returns the project's AGENTS.md/CLAUDE.md rules (project_instructions, from the Git root down to the cwd, outermost first), which you must follow; set_cwd returns them for the new folder too.",
    "filesystem": "Policy-checked real filesystem operations: line or byte reads, paginated lists, atomic writes, guarded text replacement, multi_edit for several exact replacements per file (all-or-nothing), policy-checked copy. Edits return a short diff (and a check result when edit_checks is configured), so no re-read is needed to confirm them; a missed old_text gets the closest lines in the error, to retry from, and a missing path gets similar names (a relative one that exists from the Git root is read from there, with a note). Never edit files via execute, process, or scripts; use write, replace_text, multi_edit, and apply_patch.",
    "search": "Repository text or filename search with glob filters, default ignores, and context lines. Content matches come grouped by file: results [{path, matches: [{line, text, before?, after?}]}] plus match_count; max_results counts matches; a cut page is spread round-robin across files (spread, total_matches). Result paths under the runtime cwd are relative to it. Several searches: use multi_search (or batch) in one call, not one search per turn.",
    "git": "Common structured Git operations plus a generic argument-array action. Anything beyond the presets goes through run, e.g. {\"action\": \"run\", \"args\": [\"commit\", \"-m\", \"fix parser\", \"--\", \"src/parser.py\"]}. Stage explicit files only; git add -A / --all / . is blocked. git never prompts: GIT_EDITOR is true, so commit needs -m.",
    "execute": "Run a command and wait for it, with structured output; argv arrays preferred. {shell_note}Long output is cut to head and tail and saved at stdout_path/stderr_path (up to max_capture_bytes; past process_buffer_bytes the oldest part is dropped and marked). A command still running at its timeout keeps running in the background instead of being killed (so does one that exited while a process it started still holds its output); the result has the output so far, error_type backgrounded, and a process_id for the process tool (read, stop). For long waits or polling, use the process tool instead of a long-timeout execute. Do not edit files with it; prefer the filesystem write, replace_text, or apply_patch actions.",
    "process": "Manage long-running processes with stable IDs, split streams, cursors, stdin, restart, and tree stop. wait blocks until a process exits or prints a line matching pattern (up to 60 s per call), so a build or test run needs no read polling. execute commands that outlive their timeout appear here with exec-* ids. For file edits, prefer the filesystem write, replace_text, or apply_patch actions.",
    "multi_search": "Run up to 16 independent searches in one call, e.g. {\"searches\": [{\"query\": \"UserService\", \"glob\": [\"*.py\"], \"label\": \"users\"}, {\"query\": \"TODO\", \"files_only\": true}]}. Each entry takes the same arguments as the search tool and returns the same grouped-by-file results. A failing entry is reported per-entry without failing the batch.",
    "batch": "Run up to 16 tool calls in one call, in parallel, e.g. {\"calls\": [{\"tool\": \"search\", \"arguments\": {\"query\": \"todo\"}}, {\"tool\": \"git\", \"arguments\": {\"action\": \"status\"}}]}. Each entry names a tool and its arguments; tool names may carry the host prefix (MyHost_localforge_mcp__search). Distinct paths run concurrently while edits to the same file serialize. Results come back with index and label; match on those. A failing call is reported per-call without failing the batch. For dependent steps (edit, then run tests, then git commit) pass sequential: true: calls run one at a time in order, and after the first failed call (an error, or a result with success false, such as a failing or backgrounded execute or git command) the rest come back as skipped, so the commit never runs when the tests fail. The reply adds completed (calls run without failure) and stopped_at (index of the failed call, or of the first call skipped because the run already took longer than execute_background_after_seconds, or null). Other results never stop the run: an edit whose check has ok false still counts as done, so check it yourself.",
    "todo": "Persistent task list (TodoWrite-style) with get, add, update, delete, and clear. Keep it current across turns: add before starting work, mark in_progress while working, completed when done. State survives server restarts.",
}

# Filled into the execute description from default_shell.
SHELL_NOTES = {
    "powershell": "String commands run in Windows PowerShell 5.1: no && or ||, chain with ; or make separate calls. ",
    "pwsh": "String commands run in PowerShell 7 (pwsh): && and || work. ",
    "cmd": "String commands run in cmd.exe. ",
    "sh": "String commands run in /bin/sh. ",
}

class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.paths = PathPolicy(cfg)
        self.policy = Policy(cfg, self.paths)
        self.processes = ProcessManager(cfg, self.paths, self.policy)
        self.state = StateStore(cfg.state_file) if cfg.state_file else None
        self.cap = Capabilities(cfg, self.paths, self.policy, self.processes, self.state)
        self.todos = TodoStore(self.state)
        self._client_log = None
        self.pool = ThreadPoolExecutor(max_workers=int(cfg.max_concurrency),
                                       thread_name_prefix="localforge")
        atexit.register(self.cleanup)

    def _log_to_client(self, level, message):
        # Plumbed in by run_stdio; direct callers (tests,
        # embedding) have no client channel to notify.
        if self._client_log is not None:
            self._client_log(level, message)

    def cleanup(self):
        self.processes.cleanup()
        self.pool.shutdown(wait=False)

    def tools(self):
        shell_note = SHELL_NOTES.get(str(self.cfg.default_shell).lower(), "")
        return [{"name": name, "description": DESCRIPTIONS[name].replace("{shell_note}", shell_note),
                 "inputSchema": SCHEMAS[name]} for name in DESCRIPTIONS]

    ARG_KEYS = {
        "workspace": ("action", "path", "dir"),
        "filesystem": ("action", "path", "file", "target", "content", "text", "destination", "dest", "recursive", "encoding", "offset",
                        "max_bytes", "old_text", "old", "find", "new_text", "new", "replacement", "expected_occurrences", "edits",
                        "line_start", "start", "line_end", "end", "limit", "glob", "include_hidden", "patch", "overwrite", "replace_all"),
        "search": ("query", "pattern", "path", "glob", "exclude", "case_sensitive", "max_results", "files_only",
                   "fixed_string", "context_lines", "include_hidden", "max_file_size_bytes"),
        "git": ("action", "args", "cwd", "include_untracked"),
        "execute": ("command", "cmd", "cwd", "timeout", "shell", "env", "input", "kill_on_timeout"),
        "process": ("action", "process_id", "id", "command", "cmd", "cwd", "shell", "env", "after", "limit_bytes",
                    "wait_ms", "text", "append_newline", "force", "format", "pattern", "tail_lines"),
        "multi_search": ("searches",),
        "batch": ("calls", "sequential"),
        "todo": ("action", "id", "title", "status"),
    }

    # Model-facing aliases: Quick's model is not trained on these
    # schemas, so it guesses nearby names (old/replacement instead
    # of old_text/new_text, as seen in dogfooding). Rename before
    # dispatch; a canonical key always wins over its alias.
    ALIASES = {
        "workspace": {"dir": "path"},
        "filesystem": {
            "file": "path", "target": "path",
            "old": "old_text", "find": "old_text",
            "new": "new_text", "replacement": "new_text",
            "text": "content", "dest": "destination",
            "start": "line_start", "end": "line_end",
        },
        "search": {"pattern": "query"},
        "execute": {"cmd": "command"},
        "process": {"cmd": "command", "id": "process_id"},
    }

    @classmethod
    def _apply_aliases(cls, name, arguments):
        aliases = cls.ALIASES.get(name)
        if not aliases:
            return arguments
        renamed = dict(arguments)
        for alias, canonical in aliases.items():
            if alias not in renamed:
                continue
            if canonical in renamed:
                del renamed[alias]
            else:
                renamed[canonical] = renamed.pop(alias)
        return renamed

    @classmethod
    def _unknown_arg_fault(cls, name, exc):
        match = re.search(r"unexpected keyword argument '([^']+)'", str(exc))
        if not match or name not in cls.ARG_KEYS:
            raise exc
        unknown = match.group(1)
        valid = cls.ARG_KEYS[name]
        hints = difflib.get_close_matches(unknown, valid, n=2, cutoff=0.6)
        message = f"Unknown argument '{unknown}'. Valid arguments: {', '.join(valid)}."
        if hints:
            message += f" Did you mean '{hints[0]}'?"
        return RuntimeFault("invalid_arguments", message)

    def call(self, name, arguments):
        start = time.monotonic()
        fault = "ok"
        try:
            return self._dispatch(name, arguments)
        except RuntimeFault as e:
            fault = e.code
            raise
        except TypeError as e:
            enriched = self._unknown_arg_fault(name, e)
            fault = enriched.code
            raise enriched from e
        finally:
            elapsed = int((time.monotonic() - start) * 1000)
            if os.environ.get("LOCALFORGE_LOG") == "1":
                print(f"localforge tool={name} ms={elapsed} {fault}", file=sys.stderr)
            if self.cfg.log_to_client:
                self._log_to_client("info", f"tool={name} ms={elapsed} {fault}")

    def _dispatch(self, name, arguments):
        if name not in SCHEMAS:
            raise RuntimeFault("tool_not_found", f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise RuntimeFault("invalid_arguments", "Tool arguments must be an object")
        args = self._apply_aliases(name, arguments)
        missing = [key for key in SCHEMAS[name].get("required", []) if key not in args]
        if missing:
            raise RuntimeFault("invalid_arguments", f"Missing required argument(s): {', '.join(missing)}")
        errors = []
        properties = SCHEMAS[name]["properties"]
        if name == "multi_search":
            # Entries are fixed one by one below, so a bad one fails only itself.
            properties = {**properties, "searches": {key: item for key, item in properties["searches"].items()
                                                     if key != "items"}}
        args = _coerce_fields(properties, args, "", errors)
        if errors:
            raise RuntimeFault("invalid_arguments", "Invalid argument value(s): " + "; ".join(errors) + ".")
        if name == "workspace": return self.cap.workspace(**args)
        if name == "filesystem": return self.cap.filesystem(**args)
        if name == "search": return self.cap.search(**args)
        if name == "multi_search":
            searches, invalid = args.get("searches"), {}
            if isinstance(searches, list):
                entry = SCHEMAS["multi_search"]["properties"]["searches"]["items"]["properties"]
                fixed = []
                for index, spec in enumerate(searches):
                    if isinstance(spec, dict):
                        problems = []
                        spec = _coerce_fields(entry, self._apply_aliases("search", dict(spec)),
                                              f"searches[{index}].", problems)
                        if problems:
                            invalid[index] = "Invalid argument value(s): " + "; ".join(problems) + "."
                    fixed.append(spec)
                args["searches"] = fixed
            return self.cap.multi_search(**args, invalid=invalid)
        if name == "batch": return self.batch(**args)
        if name == "git": return self.cap.git(**args)
        if name == "execute": return self.cap.execute(**args)
        if name == "todo": return self._todo(args)
        action = args.pop("action")
        required_by_action = {
            "start": ["command"], "read": ["process_id"], "wait": ["process_id"], "write": ["process_id", "text"],
            "status": ["process_id"], "restart": ["process_id"], "stop": ["process_id"], "list": [],
        }
        if action not in required_by_action:
            raise RuntimeFault("invalid_action", f"Unknown process action: {action}")
        missing = [key for key in required_by_action[action] if key not in args]
        if missing:
            raise RuntimeFault("invalid_arguments", f"Process {action} requires: {', '.join(missing)}")
        if action == "start": return self.processes.start(**args, base_cwd=self.cap.cwd)
        if action == "read": return self.processes.read(args.pop("process_id"), fmt=args.pop("format", "text"), **args)
        if action == "wait": return self.processes.wait(args.pop("process_id"), **args)
        if action == "write": return self.processes.write(args.pop("process_id"), args.pop("text"), **args)
        if action == "status": return self.processes.status(args["process_id"])
        if action == "list": return self.processes.list()
        if action == "restart": return self.processes.restart(args["process_id"])
        if action == "stop": return self.processes.stop(args.pop("process_id"), **args)
        raise RuntimeFault("invalid_action", f"Unknown process action: {action}")

    def _todo(self, args):
        action = args.pop("action")
        if action == "get":
            return {"todos": self.todos.get()}
        if action == "add":
            title = args.get("title")
            if not isinstance(title, str) or not title.strip():
                raise RuntimeFault("invalid_arguments",
                                   "todo add requires a non-empty title, "
                                   'e.g. {"action": "add", "title": "Fix login bug"}')
            return self.todos.add(title.strip())
        if action in {"update", "delete"}:
            try:
                todo_id = int(args.get("id"))
            except (TypeError, ValueError):
                raise RuntimeFault("invalid_arguments",
                                   f"todo {action} requires an integer id "
                                   "from get or add")
            title = args.get("title")
            if title is not None and not isinstance(title, str):
                raise RuntimeFault("invalid_arguments", "todo title must be a string")
            if action == "delete":
                return self.todos.delete(todo_id)
            return self.todos.update(todo_id, title=title, status=args.get("status"))
        if action == "clear":
            return self.todos.clear()
        raise RuntimeFault("invalid_action", f"Unknown todo action: {action}")

    def batch(self, calls, sequential=False):
        """Run several tool calls in parallel within one tool call.

        Mirrors how CLI agents dispatch multiple tools per turn:
        the client spends one round-trip and gets every result back.
        Distinct paths run concurrently; edits to the same file
        serialize on that path. A failing call is reported per-call
        without failing the batch.

        With sequential, calls run one at a time in order and the run
        stops at the first call that raised or returned success false;
        the rest are reported as skipped, so dependent steps (edit,
        test, commit) fit in one round-trip.
        """
        if not isinstance(calls, list) or not calls:
            raise RuntimeFault("invalid_arguments",
                               "batch requires a non-empty calls array, "
                               "e.g. {\"calls\": [{\"tool\": \"search\", "
                               "\"arguments\": {\"query\": \"todo\"}}]}")
        if len(calls) > MAX_BATCH_CALLS:
            raise RuntimeFault("invalid_arguments",
                               f"batch accepts at most {MAX_BATCH_CALLS} calls")
        if not isinstance(sequential, bool):
            raise RuntimeFault("invalid_arguments", "batch sequential must be true or false")
        specs, notes = [], {}
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                raise RuntimeFault("invalid_arguments",
                                   f"calls[{index}] must be an object with a "
                                   "'tool' name and an optional 'arguments' object")
            arguments = call.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RuntimeFault("invalid_arguments",
                                   f"calls[{index}] arguments must be an object")
            name = _bare_tool_name(call["tool"])
            if name not in SCHEMAS and name.lower() in TOOL_ALIASES:
                alias = TOOL_ALIASES[name.lower()]
                notes[index] = f'There is no "{name}" tool, so this ran as "{alias}". Call "{alias}" directly.'
                if alias == "execute" and name.lower() != str(self.cfg.default_shell).lower():
                    notes[index] += (f" A string command runs in the configured default_shell "
                                     f"({self.cfg.default_shell}), not in {name}.")
                name = alias
            specs.append((index, name, dict(arguments), call.get("label")))
        results: list[dict] = [None] * len(specs)

        def finish(reply):
            for index, note in notes.items():
                results[index]["note"] = note
            return reply

        def run(index, name, arguments, label):
            if name == "batch":
                results[index] = {"index": index, "tool": name, "label": label,
                                  "error": {"code": "invalid_arguments",
                                            "message": "batch cannot nest batch calls"}}
                return
            try:
                results[index] = {"index": index, "tool": name, "label": label,
                                  "result": self.call(name, arguments)}
            except Exception as e:
                payload = e.as_dict() if isinstance(e, RuntimeFault) else {
                    "code": "internal_error", "message": str(e)}
                results[index] = {"index": index, "tool": name, "label": label,
                                  "error": payload}

        if sequential:
            stopped_at, reason = None, None
            # One worker runs the whole batch: past this budget the client
            # may have given up, so later calls (a commit, say) are skipped.
            budget = float(self.cfg.execute_background_after_seconds)
            started = time.monotonic()
            for index, name, arguments, label in specs:
                if stopped_at is None and index and time.monotonic() - started > budget:
                    stopped_at = index
                    reason = (f"the batch's time budget ({budget:g} s, execute_background_after_seconds) "
                              "was used up")
                if stopped_at is not None:
                    results[index] = {"index": index, "tool": name, "label": label, "skipped": True,
                                      "reason": reason}
                    continue
                run(index, name, arguments, label)
                result = results[index].get("result")
                if "error" in results[index] or (isinstance(result, dict) and result.get("success") is False):
                    stopped_at, reason = index, f"an earlier call failed (index {index})"
            completed = len(specs) if stopped_at is None else stopped_at
            return finish({"results": results, "completed": completed, "stopped_at": stopped_at})

        workers = min(len(specs), max(1, int(self.cfg.max_concurrency)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, *spec) for spec in specs]
            for future in futures:
                future.result()
        return finish({"results": results})

    def _fit_result(self, name, value, text):
        """Keep a tool result under max_result_bytes.

        Hosts relay results through size-limited channels (Quick's
        device relay rejects roughly 300 KB). An oversized result is
        saved whole, indented so line-mode read can page through it.
        A dict result keeps its shape with long strings (and, if
        needed, long lists) cut until the reply fits a quarter of the
        limit; anything else is replaced by a short raw-JSON preview.
        Both carry the saved path. Returns (value, text).
        """
        limit = int(self.cfg.max_result_bytes)
        # surrogatepass: results can hold lone surrogates (Windows
        # file names), which plain UTF-8 encoding rejects.
        size = len(text.encode("utf-8", "surrogatepass"))
        if size <= limit:
            return value, text
        try:
            path = self.cap.spill(f"{_bare_tool_name(name)}-result",
                                  json.dumps(value, ensure_ascii=False, indent=1), ".json")
        except OSError:
            path = None
        narrow = "narrow the call (max_results, line ranges, fewer batch entries)."
        meta = {"result_too_large": True, "bytes": size, "result_path": path,
                "hint": ("Read result_path with filesystem read line_start/line_end or search it, or " + narrow)
                        if path else "Saving the full result failed; " + narrow}
        if isinstance(value, dict):
            budget = limit // 4
            steps, cut, items = [], budget, 1024
            while cut >= PREVIEW_MIN_CUT:
                steps.append((cut, None))
                cut //= 2
            while items:
                steps.append((PREVIEW_MIN_CUT, items))
                items //= 2
            for cut, items in steps:
                # The meta keys come first and win over same-named result
                # keys, except a hint of the result's own, which is kept.
                preview, dropped, own = dict(meta), set(), ""
                shrunk, _ = _shrink(value, cut, items, dropped)
                for key, item in shrunk.items():
                    if key == "hint" and isinstance(item, str):
                        own = item
                    else:
                        preview.setdefault(key, item)
                if dropped and path:
                    names = ", ".join(key for key in PREVIEW_STALE_KEYS if key in dropped)
                    preview["hint"] += (f" The cursors cut from this preview ({names}) are in result_path; "
                                        "or pass a smaller limit_bytes or line range.")
                if own:
                    preview["hint"] += " " + own
                out = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
                if len(out.encode("utf-8")) <= budget:
                    return preview, out
        value = meta
        safe = _safe_text(text)
        # JSON escaping can grow the preview, so shrink it until the reply fits.
        cap = min(2000, limit // 2)
        while True:
            value["preview"], _ = _clip(safe, cap)
            out = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(out.encode("utf-8")) <= limit or cap == 0:
                return value, out
            cap //= 2

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise ValueError("Invalid Request")
        method, request_id = request["method"], request.get("id")
        is_notification = "id" not in request
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {}, "logging": {}},
                      "serverInfo": {"name": "localforge-mcp", "version": __version__},
                      "instructions": INSTRUCTIONS}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise RuntimeFault("invalid_arguments", "tools/call requires params.name")
            value = self.call(params["name"], params.get("arguments", {}))
            # Compact separators: the model reads this text, so every
            # byte costs tokens. structuredContent repeats the whole
            # result; Quick showed both copies on failed calls, so it
            # is opt-in.
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            value, text = self._fit_result(params["name"], value, text)
            result = {"content": [{"type": "text", "text": text}]}
            if self.cfg.structured_content:
                result["structuredContent"] = value
        elif method == "ping":
            result = {}
        elif method.startswith("notifications/") or method == "initialized":
            return None
        else:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def run_stdio(self):
        """Serve newline-delimited JSON-RPC on stdio with concurrent tool calls.

        Each request is handled on the shared thread pool, so pipelined
        tools/call requests run in parallel instead of queueing behind the
        slowest call. Responses are matched to requests by id, so completion
        order does not matter; stdout writes are serialized per response.

        Durability: stdout writes run on a helper thread with a timeout, so
        a client that stops reading cannot pin a worker forever, and the
        pending-request count is bounded so a request flood cannot grow
        memory without limit. Once stdout is stuck or broken, the server
        stops writing and exits when stdin closes.
        """
        write_lock = threading.Lock()
        stdout_dead = False

        def _write(response):
            nonlocal stdout_dead
            if stdout_dead:
                return
            payload = json.dumps(response, separators=(",", ":")) + "\n"

            def _emit():
                with write_lock:
                    sys.stdout.write(payload)
                    sys.stdout.flush()

            if not _guarded_write(_emit, STDOUT_WRITE_TIMEOUT_SECONDS):
                # The client is not draining stdout (or the pipe is
                # broken): further writes would only pile up blocked
                # helper threads, so stop writing for good.
                stdout_dead = True

        def _notify(level, message):
            # MCP logging capability: surfaces tool-call
            # traces to the client when log_to_client is on.
            _write({"jsonrpc": "2.0", "method": "notifications/message",
                    "params": {"level": level, "logger": "localforge",
                               "data": message}})

        self._client_log = _notify

        pending_lock = threading.Lock()
        pending = 0

        def _handle(raw):
            nonlocal pending
            request_id = None
            try:
                request = json.loads(raw)
                request_id = request.get("id") if isinstance(request, dict) else None
                response = self.handle(request)
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            except ValueError as e:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": str(e)}}
            except RuntimeFault as e:
                response = {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(e.as_dict())}], "isError": True}}
            except TypeError as e:
                response = {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({"code": "invalid_arguments", "message": str(e)})}], "isError": True}}
            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error"}}
            if response is not None:
                _write(response)
            with pending_lock:
                pending -= 1

        def _submit(raw):
            # Bound the backlog: the pool queue is unbounded, so
            # a client that pipelines faster than execution drains
            # would grow memory without limit. Overloaded requests
            # get an immediate error; notifications are dropped,
            # as they expect no response anyway.
            nonlocal pending
            request_id = None
            is_notification = False
            try:
                request = json.loads(raw)
                if isinstance(request, dict):
                    request_id = request.get("id")
                    is_notification = "id" not in request
            except ValueError:
                pass
            with pending_lock:
                pending += 1
                overloaded = pending > MAX_PENDING_REQUESTS
            if overloaded:
                with pending_lock:
                    pending -= 1
                if not is_notification:
                    _write({"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32603, "message":
                                      f"Server overloaded: more than {MAX_PENDING_REQUESTS} "
                                      "requests pending. Slow down or raise max_concurrency."}})
                return
            try:
                self.pool.submit(_handle, raw)
            except RuntimeError:
                with pending_lock:
                    pending -= 1

        try:
            for raw in sys.stdin.buffer:
                _submit(raw)
        except BaseException:
            # Abnormal exit (e.g. Ctrl+C): stop taking work and exit
            # without waiting for in-flight tool calls.
            self.pool.shutdown(wait=False, cancel_futures=True)
            raise
        # Clean EOF: let in-flight calls finish so their responses
        # are written before the server exits. The backlog is
        # bounded by MAX_PENDING_REQUESTS, so this cannot wait
        # forever on a flooded queue.
        self.pool.shutdown(wait=True)


def _utf8_console():
    """Switch the server's console to UTF-8 (code page 65001).

    Children share the server's console (hidden when a host such as
    Quick starts it), and PowerShell and cmd encode piped output in its
    code page: the OEM code page by default (437 on US English), which
    turns é into garbage and 日本 into ??. Output is decoded as UTF-8,
    so the console has to match.
    The old code pages come back at a clean exit, for a server run in
    the user's own terminal.
    """
    if os.name != "nt":
        return
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    out_cp, in_cp = kernel32.GetConsoleOutputCP(), kernel32.GetConsoleCP()
    if not out_cp:
        print("localforge: no console, so child output keeps the system code page", file=sys.stderr)
        return
    if not (kernel32.SetConsoleOutputCP(65001) and kernel32.SetConsoleCP(65001)):
        print(f"localforge: could not switch the console to UTF-8 (error {ctypes.get_last_error()})",
              file=sys.stderr)
    atexit.register(lambda: (kernel32.SetConsoleOutputCP(out_cp), kernel32.SetConsoleCP(in_cp)))


def main():
    # First, so its atexit restore runs last: atexit is last-in,
    # first-out, and process cleanup must still see UTF-8.
    _utf8_console()
    try:
        server = Server(Config.load())
    except Exception as e:
        print(f"configuration error: {e}", file=sys.stderr)
        raise SystemExit(2)
    server.run_stdio()


if __name__ == "__main__":
    main()
