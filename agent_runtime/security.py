from __future__ import annotations
from pathlib import Path
import fnmatch
import json
import os
import re
from .errors import RuntimeFault

def _looks_like_argv(text):
    stripped = text.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return False
    try:
        value = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) for x in value)


def coerce_command(command):
    """Parse a stringified JSON argv array back into a real argv list.

    Some MCP hosts serialize the command array into a JSON string before
    sending. Passing that string to a shell breaks quoting, so coerce it
    back to a list. Returns the original command when it is not a
    stringified argv array.
    """
    if isinstance(command, str) and _looks_like_argv(command):
        try:
            value = json.loads(command.strip())
        except ValueError:
            return command
        if isinstance(value, list) and bool(value) and all(isinstance(x, str) for x in value):
            return value
    return command


class PathPolicy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.workspace = self._canonical(cfg.workspace_root, must_exist=True)
        self.read_roots = [self._canonical(p, must_exist=True) for p in cfg.allowed_read_roots]
        self.write_roots = [self._canonical(p, must_exist=True) for p in cfg.allowed_write_roots]
        self.denied = [self._canonical(p, must_exist=False) for p in cfg.denied_paths]

    def _canonical(self, value, base=None, must_exist=False):
        raw = os.path.expandvars(os.path.expanduser(str(value)))
        p = Path(raw)
        if not p.is_absolute():
            p = Path(base or getattr(self, "workspace", Path.cwd())) / p
        try:
            return p.resolve(strict=must_exist)
        except (FileNotFoundError, OSError) as e:
            raise RuntimeFault("path_not_found", f"Path cannot be resolved: {p}") from e

    @staticmethod
    def _within(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def resolve(self, value, *, cwd=None, access="read", must_exist=False):
        p = self._canonical(value or ".", cwd or self.workspace, must_exist)
        if self._within(p, self.denied):
            raise RuntimeFault("path_denied", f"Denied path: {p}")
        roots = self.read_roots if access == "read" else self.write_roots
        if self.cfg.mode != "FULL_ACCESS" and not self._within(p, roots):
            cfg_path = getattr(self.cfg, "config_path", None) or "localforge.json"
            raise RuntimeFault("path_outside_roots",
                f"Path outside {access} roots: {p}. "
                f"Add it to allowed_{access}_roots in {cfg_path} then restart MCP (toggle connection off/on).")
        return p

class Policy:
    NETWORK_TOOLS = {"curl", "curl.exe", "wget", "git", "npm", "npm.cmd", "npx", "npx.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd", "pip", "pip3", "python", "python.exe", "py", "docker", "docker.exe", "gh", "gh.exe"}
    NETWORK_MARKERS = ("install", "fetch", "pull", "push", "clone", "http://", "https://")
    # gh subcommands almost always hit the GitHub API; only local-only
    # invocations are exempt from network classification.
    GH_LOCAL_ONLY = ("--version", "--help", "auth status")

    def __init__(self, cfg, paths):
        self.cfg, self.paths = cfg, paths

    def authorize_path(self, action):
        if self.cfg.mode == "READ_ONLY" and action not in {"read", "list", "stat"}:
            raise RuntimeFault("permission_denied", "Active mode is read-only")

    def authorize_command(self, command, cwd, shell=False):
        if self.cfg.mode in {"READ_ONLY", "WORKSPACE"}:
            raise RuntimeFault("execution_denied", f"Mode {self.cfg.mode} does not allow process execution")
        command = coerce_command(command)
        if isinstance(command, str):
            if not shell:
                raise RuntimeFault("invalid_command", "String commands require shell=true; use an argv array otherwise")
            if not self.cfg.allow_shell_commands:
                raise RuntimeFault("shell_denied", "Shell commands are disabled")
            head = command.strip().split(maxsplit=1)[0] if command.strip() else ""
            display = command
        elif isinstance(command, list) and command and all(isinstance(x, str) for x in command):
            head, display = command[0], " ".join(command)
        else:
            raise RuntimeFault("invalid_command", "Command must be a non-empty string or string array")
        exe = Path(head.strip('"')).name.lower()
        lowered = display.lower()
        if exe in {"gh", "gh.exe"}:
            network = not any(marker in lowered for marker in self.GH_LOCAL_ONLY)
        else:
            network = exe in self.NETWORK_TOOLS and any(x in lowered for x in self.NETWORK_MARKERS)
        if network and self.cfg.network == "disabled":
            raise RuntimeFault("network_denied", "Recognized network command blocked by configuration")
        return {"command": display, "cwd": str(cwd), "shell": shell, "network_classified": network}


def command_for_spawn(cfg, command, shell):
    if not shell:
        return command
    kind = cfg.default_shell.lower()
    if kind == "powershell":
        executable = "powershell.exe" if os.name == "nt" else "pwsh"
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if kind == "pwsh":
        return ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if kind == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/D", "/S", "/C", command]
    if kind == "sh":
        return ["/bin/sh", "-c", command]
    raise RuntimeFault("invalid_shell", f"Unsupported default_shell: {cfg.default_shell}")

def safe_environment(cfg, overrides=None):
    source, result = os.environ, {}
    def denied(key):
        return any(fnmatch.fnmatch(key.upper(), pattern.upper()) for pattern in cfg.deny_environment)
    for key in cfg.inherit_environment:
        if key in source and not denied(key):
            result[key] = source[key]
    for key, value in cfg.extra_environment.items():
        if not denied(key):
            result[key] = str(value)
    for key, value in (overrides or {}).items():
        if denied(key):
            raise RuntimeFault("environment_denied", f"Environment variable denied: {key}")
        result[key] = str(value)
    return result

_SECRET = re.compile(r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)")

_CODE_REF = re.compile(r"^[A-Za-z_]\w*(\.\w+)+$")

def _looks_like_code(value):
    """True when a redaction candidate is code, not a credential.

    execute output often contains source listings (e.g. Get-Content of a
    config class). Redacting `Password = OverrideSecret(` or
    `secret = cfg.Secret` destroys the listing while hiding nothing.
    Skip call expressions, dotted code references, and common literals.
    Long blobs (tokens, base64 certs) still redact.
    """
    if "(" in value or ")" in value:
        return True
    if _CODE_REF.match(value):
        return True
    if value.strip("\"'").lower() in {"null", "true", "false", "none", ""}:
        return True
    return len(value.strip("\"'")) < 4

def redact(text):
    def _sub(m):
        if _looks_like_code(m.group(3)):
            return m.group(0)
        return m.group(1) + m.group(2) + "[REDACTED]"
    return _SECRET.sub(_sub, text)
