from __future__ import annotations
from pathlib import Path
import fnmatch
import functools
import json
import os
import re
import shutil
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


# Windows PowerShell 5.1 aliases curl and wget to Invoke-WebRequest,
# which takes none of their flags. Drop the aliases so the real
# curl.exe runs. pwsh 6+ has no such aliases.
# Same line as the command, so error line numbers stay as written.
POWERSHELL_PRELUDE = "Remove-Item Alias:curl, Alias:wget -Force -ErrorAction SilentlyContinue; "
# Statements PowerShell accepts only at the very start of a script
# (after blank lines and comments); such commands run without the
# prelude. #requires lines stay visible to the start check.
_PS_LEADING = re.compile(r"\A(?:\s|#(?!requires)[^\n]*|<#.*?#>)*", re.S | re.I)
_PS_SCRIPT_START = re.compile(r"using\s|#requires|param\s*\(|\[cmdletbinding", re.I)
# Only commands that name curl or wget need the prelude; every other
# command runs as written, so its errors show its own text and columns.
_PS_NEEDS_PRELUDE = re.compile(r"\b(?:curl|wget)\b", re.I)

def _powershell_script(command):
    if not _PS_NEEDS_PRELUDE.search(command):
        return command
    body = command[_PS_LEADING.match(command).end():]
    if _PS_SCRIPT_START.match(body):
        return command
    return POWERSHELL_PRELUDE + command

@functools.lru_cache(maxsize=None)
def find_pwsh():
    """Path to PowerShell 7+ (pwsh.exe), or None when it is not installed.

    Checks PATH, then the MSI and Store/winget install locations: a
    host started before pwsh was installed still has its old PATH.
    Cached; restart the server to pick up a new install.
    """
    found = shutil.which("pwsh")
    # On Windows, which also matches pwsh.cmd/.bat through PATHEXT
    # (and looks in the current folder); only a real pwsh.exe counts.
    if found and (os.name != "nt" or found.lower().endswith(".exe")):
        return found
    if os.name != "nt":
        return None
    candidates = [Path(base) / "PowerShell" / "7" / "pwsh.exe"
                  for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")) if base]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps" / "pwsh.exe")
    return next((str(c) for c in candidates if c.exists()), None)


def resolve_shell(name):
    """Turn default_shell "auto" into a concrete shell; other names pass through.

    auto picks pwsh when installed (it has && and ||, and no curl/wget
    aliases), else Windows PowerShell 5.1, or sh off Windows.
    """
    if str(name).lower() != "auto":
        return name
    if find_pwsh():
        return "pwsh"
    return "powershell" if os.name == "nt" else "sh"


def command_for_spawn(cfg, command, shell):
    if not shell:
        return command
    kind = resolve_shell(cfg.default_shell).lower()
    if kind == "powershell":
        executable = "powershell.exe" if os.name == "nt" else "pwsh"
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _powershell_script(command)]
    if kind == "pwsh":
        return [find_pwsh() or "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if kind == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/D", "/S", "/C", command]
    if kind == "sh":
        return ["/bin/sh", "-c", command]
    raise RuntimeFault("invalid_shell", f"Unsupported default_shell: {cfg.default_shell}")

def spawn_environment(cfg, shell, overrides=None):
    """safe_environment, plus defaults that keep child output readable.

    PYTHONUTF8=1 (Python 3.7+ UTF-8 mode): piped Python children
    otherwise write in the ANSI code page (cp1252 on US English), which
    crashes on characters like 日本; output is decoded as UTF-8. UTF-8
    mode, unlike PYTHONIOENCODING, also makes a child's own subprocess
    pipes UTF-8, so the setting is safe to inherit. Python 2 and 3.6
    ignore it. NO_COLOR=1 for pwsh shell
    commands: pwsh colours its errors with ANSI codes, which reach the
    agent as noise. Values from the config or the caller win.
    """
    env = safe_environment(cfg, overrides)
    env.setdefault("PYTHONUTF8", "1")
    if shell and resolve_shell(cfg.default_shell).lower() == "pwsh":
        env.setdefault("NO_COLOR", "1")
    return env

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
