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
from .capabilities import Capabilities
from .state import StateStore
from .todos import TodoStore

PROTOCOL = "2025-06-18"
MAX_BATCH_CALLS = 16
STDOUT_WRITE_TIMEOUT_SECONDS = 30
MAX_PENDING_REQUESTS = 128

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
    "Birb_localforge_mcp__search". Local tool names never
    contain "__", so the segment after the last "__" is the
    tool this server knows.
    """
    return name.split("__")[-1] if "__" in name else name

SCHEMAS = {
    "workspace": {"type": "object", "properties": {
        "action": {"enum": ["get", "set_cwd"], "description": "get returns roots and Git root; set_cwd changes runtime directory."},
        "path": {"type": "string", "description": "Directory for set_cwd, relative to cwd or absolute, must resolve inside read roots."}}, "required": ["action"]},
    "filesystem": {"type": "object", "properties": {
        "action": {"enum": ["read", "list", "stat", "write", "replace_text", "multi_edit", "apply_patch", "mkdir", "delete", "move", "copy"], "description": "read defaults to byte mode; pass line_start/line_end for line mode. list is paginated. multi_edit applies several exact replacements to one file, validated end-to-end and written once. apply_patch takes a unified diff string. copy duplicates a file (or a directory with recursive=true) without shell quoting issues."},
        "path": {"type": "string", "description": "Target path, relative to cwd or absolute. Reads need read roots, writes need write roots."},
        "content": {"type": "string", "description": "Full replacement content for write; overwrites the file."},
        "destination": {"type": "string", "description": "Destination path for move/copy; copy creates missing parent dirs, move requires the parent to exist."},
        "recursive": {"type": "boolean", "description": "true creates parent dirs for mkdir, deletes non-empty dirs for delete, copies directories for copy."},
        "encoding": {"type": "string", "description": "Text encoding, default utf-8."},
        "offset": {"type": "integer", "description": "Byte offset for legacy read mode; list page offset when action is list. Ignored in line mode."},
        "max_bytes": {"type": "integer", "description": "Byte cap for legacy read mode."},
        "old_text": {"type": "string", "description": "Exact text to find for replace_text."},
        "new_text": {"type": "string", "description": "Replacement text for replace_text."},
        "expected_occurrences": {"type": "integer", "description": "Required match count for replace_text, default 1."},
        "edits": {"type": "array", "minItems": 1, "maxItems": 32,
            "description": "Exact replacements for multi_edit, applied in order: [{\"old_text\": \"foo\", \"new_text\": \"bar\"}]. The whole set is validated before anything is written; a failing edit leaves the file untouched. Pass expected_occurrences inside an entry when the text repeats.",
            "items": {"type": "object", "properties": {
                "old_text": {"type": "string", "description": "Exact text to find."},
                "new_text": {"type": "string", "description": "Replacement text."},
                "expected_occurrences": {"type": "integer", "description": "Required match count, default 1."}},
                "required": ["old_text", "new_text"]}},
        "line_start": {"type": "integer", "description": "1-based first line for line-mode read, default 1."},
        "line_end": {"type": "integer", "description": "Inclusive last line for line-mode read, default end of file."},
        "limit": {"type": "integer", "description": "Max list entries returned, default 200, clamped 1..1000."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Fnmatch filters on entry name for list."},
        "include_hidden": {"type": "boolean", "description": "Include dotfiles in list, default false."},
        "patch": {"type": "string", "description": "Unified diff for apply_patch: modify or create files, exact context, all-or-nothing."}}, "required": ["action"]},
    "search": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Text or regex to find; omit only with files_only."},
        "path": {"type": "string", "description": "File or directory to search, relative to cwd or absolute."},
        "glob": {"type": "array", "items": {"type": "string"}, "description": "Include patterns, fnmatch on repo-relative posix path."},
        "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra excludes; built-ins (.git, .venv, node_modules, etc.) always apply."},
        "case_sensitive": {"type": "boolean", "description": "Case-sensitive match, default false."},
        "max_results": {"type": "integer", "description": "Result cap 1..5000, default 200."},
        "files_only": {"type": "boolean", "description": "List matching filenames; takes no query."},
        "fixed_string": {"type": "boolean", "description": "Literal match when true, regex when false, default true."},
        "context_lines": {"type": "integer", "description": "Surrounding lines per match, clamped 0..5, ignored with files_only."},
        "include_hidden": {"type": "boolean", "description": "Search hidden files, default false."},
        "max_file_size_bytes": {"type": "integer", "description": "Skip larger files, default 1000000, must be positive."}}},
    "git": {"type": "object", "properties": {
        "action": {"enum": ["status", "diff", "log", "show", "branch", "root", "run"], "description": "Preset operation, or run for an arbitrary git argument array."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Extra args appended to presets, or the full git args for run."},
        "cwd": {"type": "string", "description": "Repository directory, defaults to runtime cwd."}}, "required": ["action"]},
    "execute": {"type": "object", "properties": {
        "command": {"description": "Command to run. Prefer an argv array: no shell parsing, no quoting bugs. A string runs through the configured default_shell, so match its syntax.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "timeout": {"type": "number", "description": "Timeout in seconds, not milliseconds. Long foreground waits risk gateway/outpost timeouts and block the server; use the process tool for waits and polling."},
        "shell": {"type": "boolean", "description": "Rarely needed. Strings auto-enable the shell; set true only to force an argv array through the shell for pipes and operators."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "input": {"type": "string", "description": "Optional stdin text, max 65536 chars."}}, "required": ["command"]},
    "process": {"type": "object", "properties": {
        "action": {"enum": ["start", "read", "write", "status", "list", "restart", "stop"], "description": "start, read, write, status, list, restart, or stop. start needs command; read, write, status, restart, and stop need process_id."},
        "process_id": {"type": "string", "description": "Stable id; auto-generated when start omits it."},
        "command": {"description": "Command to start. Prefer an argv array: no shell parsing, no quoting bugs. A string runs through the configured default_shell, so match its syntax.", "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string", "description": "Working directory, defaults to runtime cwd."},
        "shell": {"type": "boolean", "description": "Rarely needed. Strings auto-enable the shell; set true only to force an argv array through the shell for pipes and operators."},
        "env": {"type": "object", "description": "Extra environment variables; denied names raise environment_denied."},
        "after": {"type": "integer", "description": "Log cursor from previous next_after for incremental reads."},
        "limit_bytes": {"type": "integer", "description": "Max log bytes per read."},
        "wait_ms": {"type": "integer", "description": "Wait in milliseconds for new output, max 60000."},
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
                "glob": {"type": "array", "items": {"type": "string"}, "description": "Include patterns, fnmatch on repo-relative posix path."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra excludes; built-ins (.git, .venv, node_modules, etc.) always apply."},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive match, default false."},
                "max_results": {"type": "integer", "description": "Result cap 1..5000, default 200."},
                "files_only": {"type": "boolean", "description": "List matching filenames; takes no query."},
                "fixed_string": {"type": "boolean", "description": "Literal match when true, regex when false, default true."},
                "context_lines": {"type": "integer", "description": "Surrounding lines per match, clamped 0..5, ignored with files_only."},
                "include_hidden": {"type": "boolean", "description": "Search hidden files, default false."},
                "max_file_size_bytes": {"type": "integer", "description": "Skip larger files, default 1000000, must be positive."}}}},
        }, "required": ["searches"]},
    "batch": {"type": "object", "properties": {
        "calls": {"type": "array", "minItems": 1, "maxItems": 16,
            "description": "Tool calls to run in parallel, e.g. [{\"tool\": \"search\", \"arguments\": {\"query\": \"todo\"}}, {\"tool\": \"git\", \"arguments\": {\"action\": \"status\"}}].",
            "items": {"type": "object", "properties": {
                "tool": {"type": "string", "description": "Tool to run: workspace, filesystem, search, multi_search, git, execute, or process."},
                "arguments": {"type": "object", "description": "Arguments for the tool; same shape as a tools/call request."},
                "label": {"type": "string", "description": "Optional label echoed back with this call's result."}},
                "required": ["tool"]}},
        }, "required": ["calls"]},
}
DESCRIPTIONS = {
    "workspace": "Inspect workspace and Git root, or change runtime current directory.",
    "filesystem": "Policy-checked real filesystem operations: line or byte reads, paginated lists, atomic writes, guarded text replacement, multi_edit for several exact replacements per file (all-or-nothing), policy-checked copy. Prefer write, replace_text, multi_edit, and apply_patch for file edits instead of shelling out to python or shell commands.",
    "search": "Repository text or filename search with glob filters, default ignores, and context lines.",
    "git": "Common structured Git operations plus a generic argument-array action.",
    "execute": "Run a bounded foreground process with structured output; argv arrays preferred. For long waits or polling, use the process tool instead of a long-timeout execute. Do not edit files with it; prefer the filesystem write, replace_text, or apply_patch actions.",
    "process": "Manage long-running processes with stable IDs, split streams, cursors, stdin, restart, and tree stop. For file edits, prefer the filesystem write, replace_text, or apply_patch actions.",
    "multi_search": "Run up to 16 independent searches in one call, e.g. {\"searches\": [{\"query\": \"UserService\", \"glob\": [\"*.py\"], \"label\": \"users\"}, {\"query\": \"TODO\", \"files_only\": true}]}. Each entry takes the same arguments as the search tool. A failing entry is reported per-entry without failing the batch.",
    "batch": "Run up to 16 tool calls in one call, in parallel, e.g. {\"calls\": [{\"tool\": \"search\", \"arguments\": {\"query\": \"todo\"}}, {\"tool\": \"git\", \"arguments\": {\"action\": \"status\"}}]}. Each entry names a tool and its arguments; tool names may carry the host prefix (Birb_localforge_mcp__search). Distinct paths run concurrently while edits to the same file serialize. A failing call is reported per-call without failing the batch.",
    "todo": "Persistent task list (TodoWrite-style) with get, add, update, delete, and clear. Keep it current across turns: add before starting work, mark in_progress while working, completed when done. State survives server restarts.",
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
        return [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": SCHEMAS[name]} for name in DESCRIPTIONS]

    ARG_KEYS = {
        "workspace": ("action", "path", "dir"),
        "filesystem": ("action", "path", "file", "target", "content", "text", "destination", "dest", "recursive", "encoding", "offset",
                        "max_bytes", "old_text", "old", "find", "new_text", "new", "replacement", "expected_occurrences", "edits",
                        "line_start", "start", "line_end", "end", "limit", "glob", "include_hidden", "patch"),
        "search": ("query", "pattern", "path", "glob", "exclude", "case_sensitive", "max_results", "files_only",
                   "fixed_string", "context_lines", "include_hidden", "max_file_size_bytes"),
        "git": ("action", "args", "cwd"),
        "execute": ("command", "cmd", "cwd", "timeout", "shell", "env", "input"),
        "process": ("action", "process_id", "id", "command", "cmd", "cwd", "shell", "env", "after", "limit_bytes",
                    "wait_ms", "text", "append_newline", "force"),
        "multi_search": ("searches",),
        "batch": ("calls",),
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
        if name == "workspace": return self.cap.workspace(**args)
        if name == "filesystem": return self.cap.filesystem(**args)
        if name == "search": return self.cap.search(**args)
        if name == "multi_search":
            searches = args.get("searches")
            if isinstance(searches, list):
                args["searches"] = [self._apply_aliases("search", dict(s))
                                    if isinstance(s, dict) else s for s in searches]
            return self.cap.multi_search(**args)
        if name == "batch": return self.batch(**args)
        if name == "git": return self.cap.git(**args)
        if name == "execute": return self.cap.execute(**args)
        if name == "todo": return self._todo(args)
        action = args.pop("action")
        required_by_action = {
            "start": ["command"], "read": ["process_id"], "write": ["process_id", "text"],
            "status": ["process_id"], "restart": ["process_id"], "stop": ["process_id"], "list": [],
        }
        if action not in required_by_action:
            raise RuntimeFault("invalid_action", f"Unknown process action: {action}")
        missing = [key for key in required_by_action[action] if key not in args]
        if missing:
            raise RuntimeFault("invalid_arguments", f"Process {action} requires: {', '.join(missing)}")
        if action == "start": return self.processes.start(**args)
        if action == "read": return self.processes.read(args.pop("process_id"), **args)
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

    def batch(self, calls):
        """Run several tool calls in parallel within one tool call.

        Mirrors how CLI agents dispatch multiple tools per turn:
        the client spends one round-trip and gets every result back.
        Distinct paths run concurrently; edits to the same file
        serialize on that path. A failing call is reported per-call
        without failing the batch.
        """
        if not isinstance(calls, list) or not calls:
            raise RuntimeFault("invalid_arguments",
                               "batch requires a non-empty calls array, "
                               "e.g. {\"calls\": [{\"tool\": \"search\", "
                               "\"arguments\": {\"query\": \"todo\"}}]}")
        if len(calls) > MAX_BATCH_CALLS:
            raise RuntimeFault("invalid_arguments",
                               f"batch accepts at most {MAX_BATCH_CALLS} calls")
        specs = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                raise RuntimeFault("invalid_arguments",
                                   f"calls[{index}] must be an object with a "
                                   "'tool' name and an optional 'arguments' object")
            arguments = call.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RuntimeFault("invalid_arguments",
                                   f"calls[{index}] arguments must be an object")
            specs.append((index, _bare_tool_name(call["tool"]), dict(arguments),
                          call.get("label")))
        results: list[dict] = [None] * len(specs)

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

        workers = min(len(specs), max(1, int(self.cfg.max_concurrency)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, *spec) for spec in specs]
            for future in futures:
                future.result()
        return {"results": results}

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise ValueError("Invalid Request")
        method, request_id = request["method"], request.get("id")
        is_notification = "id" not in request
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {}, "logging": {}},
                      "serverInfo": {"name": "localforge-mcp", "version": __version__}}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise RuntimeFault("invalid_arguments", "tools/call requires params.name")
            value = self.call(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                      "structuredContent": value}
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


def main():
    try:
        server = Server(Config.load())
    except Exception as e:
        print(f"configuration error: {e}", file=sys.stderr)
        raise SystemExit(2)
    server.run_stdio()


if __name__ == "__main__":
    main()
