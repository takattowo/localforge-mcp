from __future__ import annotations
from dataclasses import dataclass, field, fields
from pathlib import Path
import json
import os
from .security import resolve_shell

MODES = {"READ_ONLY", "WORKSPACE", "DEVELOPMENT", "FULL_ACCESS"}
# The repo folder: localforge.json there is used when no variable names a config.
SERVER_DIR = Path(__file__).resolve().parent.parent
NETWORK_MODES = {"disabled", "unrestricted"}

@dataclass
class Config:
    workspace_root: str
    mode: str = "DEVELOPMENT"
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    network: str = "unrestricted"
    # "auto" becomes pwsh when PowerShell 7 is installed, else
    # powershell (Windows PowerShell 5.1), or sh off Windows.
    default_shell: str = "auto"
    default_timeout_seconds: int = 600
    # execute with no timeout waits this long in the foreground, then
    # leaves the command running in the background as a process entry.
    execute_background_after_seconds: int = 120
    max_capture_bytes: int = 1_000_000
    max_file_read_bytes: int = 1_000_000
    max_read_chars: int = 60_000
    process_buffer_bytes: int = 4_000_000
    process_ttl_seconds: int = 3600
    max_processes: int = 50
    max_concurrency: int = 8
    state_file: str | None = None
    config_path: str | None = None
    inherit_environment: list[str] = field(default_factory=lambda: [
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
        "ProgramData", "SystemDrive",
    ])
    deny_environment: list[str] = field(default_factory=lambda: [
        "*TOKEN*", "*SECRET*", "*PASSWORD*", "*KEY*", "AWS_*", "AZURE_*",
        "GITHUB_*", "NPM_TOKEN",
    ])
    extra_environment: dict[str, str] = field(default_factory=dict)
    allow_shell_commands: bool = True
    log_to_client: bool = False
    # File that gets one JSON line per tool call (tool, outcome, time, size, never arguments); unset logs nothing.
    call_log: str | None = None
    structured_content: bool = False
    kill_children_on_exit: bool = True
    max_inline_output_bytes: int = 30_000
    max_result_bytes: int = 200_000
    spill_dir: str | None = None
    edit_diff_max_bytes: int = 4_000
    # Glob on the file name -> argv run after each edit of a
    # matching file; "{path}" becomes the edited file's path.
    edit_checks: dict[str, list[str]] = field(default_factory=dict)
    edit_check_timeout_seconds: int = 30
    # Agent rule files workspace get returns, from every folder between
    # the Git root (or workspace_root) and the cwd; false reads none.
    load_project_instructions: bool = True
    project_instruction_files: list[str] = field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md"])
    max_project_instruction_bytes: int = 8_000
    # Turns on the batch and multi_search tools and filesystem multi_edit.
    advanced_features: bool = False
    # One server serves every chat and task, and a set_cwd anywhere moves its cwd: edits, git, execute and
    # process start must then name an absolute path or cwd, so one chat can't change another's repo.
    require_absolute_paths: bool = True

    def __post_init__(self):
        # Resolved once, so the rest of the server only sees real shells.
        self.default_shell = resolve_shell(self.default_shell)
        if int(self.max_result_bytes) < 1024:
            # Room for the oversized-result reply's own keys and path.
            raise ValueError("max_result_bytes must be at least 1024")

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("LOCALFORGE_CONFIG") or os.environ.get("WIN_AGENT_RUNTIME_CONFIG")
        if not path:
            path = "localforge.json"
            if not Path(path).is_file() and (SERVER_DIR / path).is_file():
                path = SERVER_DIR / path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Compatibility with 1.0 configuration.
        if "shell" in data and "default_shell" not in data:
            data["default_shell"] = data.pop("shell")
        data.pop("network_hosts", None)
        data.pop("approval_ttl_seconds", None)
        data.pop("config_path", None)
        # Checked here, so a typo gets a plain message, not Python's own hint.
        unknown = [key for key in data if key not in {item.name for item in fields(cls)}]
        if unknown:
            raise ValueError(("unknown config key: " if len(unknown) == 1 else "unknown config keys: ") + ", ".join(unknown))
        cfg = cls(**data)
        cfg.config_path = str(Path(path).expanduser())
        cfg.workspace_root = str(Path(cfg.workspace_root).expanduser())
        if not cfg.allowed_read_roots:
            cfg.allowed_read_roots = [cfg.workspace_root]
        if not cfg.allowed_write_roots:
            cfg.allowed_write_roots = [cfg.workspace_root]
        if cfg.mode not in MODES:
            raise ValueError(f"invalid mode: {cfg.mode}")
        if cfg.network not in NETWORK_MODES:
            raise ValueError("network must be 'disabled' or 'unrestricted'")
        for name in ("default_timeout_seconds", "execute_background_after_seconds", "max_capture_bytes", "max_file_read_bytes", "max_read_chars", "process_buffer_bytes", "process_ttl_seconds", "max_processes", "max_concurrency", "max_inline_output_bytes", "max_result_bytes", "edit_check_timeout_seconds", "max_project_instruction_bytes"):
            if int(getattr(cfg, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if int(cfg.max_read_chars) < 64:
            # Room for a line number plus the line-cut marker.
            raise ValueError("max_read_chars must be at least 64")
        if int(cfg.edit_diff_max_bytes) < 0:
            raise ValueError("edit_diff_max_bytes must be 0 (off) or positive")
        if not isinstance(cfg.load_project_instructions, bool):
            raise ValueError("load_project_instructions must be true or false")
        if not isinstance(cfg.advanced_features, bool):
            raise ValueError("advanced_features must be true or false")
        if not isinstance(cfg.require_absolute_paths, bool):
            raise ValueError("require_absolute_paths must be true or false")
        if not isinstance(cfg.edit_checks, dict) or not all(
                isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv)
                for argv in cfg.edit_checks.values()):
            raise ValueError('edit_checks must map a file glob to an argv array, e.g. {"*.py": ["python", "-m", "py_compile", "{path}"]}')
        if not cfg.state_file:
            cfg.state_file = str(Path(cfg.config_path).parent / ".localforge-state.json")
        if cfg.call_log is not None and not isinstance(cfg.call_log, str):
            raise ValueError("call_log must be a file path or null")
        if cfg.call_log:
            expanded = os.path.expandvars(cfg.call_log)
            # expandvars leaves an unset variable as written, which would become a folder named after it.
            if "$" in expanded or "%" in expanded:
                raise ValueError(f"call_log names an unset environment variable: {cfg.call_log}")
            log = Path(expanded).expanduser()
            cfg.call_log = str(log if log.is_absolute() else Path(cfg.config_path).parent / log)
        return cfg
