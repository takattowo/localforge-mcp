from __future__ import annotations
import atexit
import json
import sys
import traceback
from . import __version__
from .config import Config
from .errors import RuntimeFault
from .security import PathPolicy, Policy
from .processes import ProcessManager
from .capabilities import Capabilities

PROTOCOL = "2024-11-05"
SCHEMAS = {
    "workspace": {"type": "object", "properties": {"action": {"enum": ["get", "set_cwd"]}, "path": {"type": "string"}}, "required": ["action"]},
    "filesystem": {"type": "object", "properties": {
        "action": {"enum": ["read", "list", "stat", "write", "replace_text", "mkdir", "delete", "move"]},
        "path": {"type": "string"}, "content": {"type": "string"}, "destination": {"type": "string"},
        "recursive": {"type": "boolean"}, "encoding": {"type": "string"}, "offset": {"type": "integer"},
        "max_bytes": {"type": "integer"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        "expected_occurrences": {"type": "integer"}}, "required": ["action"]},
    "search": {"type": "object", "properties": {
        "query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "array", "items": {"type": "string"}},
        "exclude": {"type": "array", "items": {"type": "string"}}, "case_sensitive": {"type": "boolean"},
        "max_results": {"type": "integer"}, "files_only": {"type": "boolean"}, "fixed_string": {"type": "boolean"}}},
    "git": {"type": "object", "properties": {"action": {"enum": ["status", "diff", "log", "show", "branch", "root", "run"]},
        "args": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}}, "required": ["action"]},
    "execute": {"type": "object", "properties": {"command": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string"}, "timeout": {"type": "number"}, "shell": {"type": "boolean"}, "env": {"type": "object"}}, "required": ["command"]},
    "process": {"type": "object", "properties": {"action": {"enum": ["start", "read", "write", "status", "list", "restart", "stop"]},
        "process_id": {"type": "string"}, "command": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string"}, "shell": {"type": "boolean"}, "env": {"type": "object"}, "after": {"type": "integer"},
        "limit_bytes": {"type": "integer"}, "wait_ms": {"type": "integer"}, "text": {"type": "string"},
        "append_newline": {"type": "boolean"}, "force": {"type": "boolean"}}, "required": ["action"]},
}
DESCRIPTIONS = {
    "workspace": "Inspect workspace and Git root, or change runtime current directory.",
    "filesystem": "Policy-checked real filesystem operations including bounded reads and guarded text replacement.",
    "search": "Repository text or filename search with glob filters; ripgrep preferred.",
    "git": "Common structured Git operations plus a generic argument-array action.",
    "execute": "Run a bounded foreground process with structured output; argv arrays preferred.",
    "process": "Manage long-running processes with stable IDs, split streams, cursors, stdin, restart, and tree stop.",
}

class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.paths = PathPolicy(cfg)
        self.policy = Policy(cfg, self.paths)
        self.processes = ProcessManager(cfg, self.paths, self.policy)
        self.cap = Capabilities(cfg, self.paths, self.policy, self.processes)
        atexit.register(self.processes.cleanup)

    def tools(self):
        return [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": SCHEMAS[name]} for name in DESCRIPTIONS]

    def call(self, name, arguments):
        if name not in SCHEMAS:
            raise RuntimeFault("tool_not_found", f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise RuntimeFault("invalid_arguments", "Tool arguments must be an object")
        missing = [key for key in SCHEMAS[name].get("required", []) if key not in arguments]
        if missing:
            raise RuntimeFault("invalid_arguments", f"Missing required argument(s): {', '.join(missing)}")
        args = dict(arguments)
        if name == "workspace": return self.cap.workspace(**args)
        if name == "filesystem": return self.cap.filesystem(**args)
        if name == "search": return self.cap.search(**args)
        if name == "git": return self.cap.git(**args)
        if name == "execute": return self.cap.execute(**args)
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

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise ValueError("Invalid Request")
        method, request_id = request["method"], request.get("id")
        is_notification = "id" not in request
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "localforge-mcp", "version": __version__}}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise RuntimeFault("invalid_arguments", "tools/call requires params.name")
            value = self.call(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
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

def main():
    try:
        server = Server(Config.load())
    except Exception as e:
        print(f"configuration error: {e}", file=sys.stderr)
        raise SystemExit(2)
    for raw in sys.stdin.buffer:
        request_id = None
        try:
            request = json.loads(raw)
            request_id = request.get("id") if isinstance(request, dict) else None
            response = server.handle(request)
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
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
