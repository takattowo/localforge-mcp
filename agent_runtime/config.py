from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
import os

MODES = {"READ_ONLY", "WORKSPACE", "DEVELOPMENT", "FULL_ACCESS"}
NETWORK_MODES = {"disabled", "unrestricted"}

@dataclass
class Config:
    workspace_root: str
    mode: str = "DEVELOPMENT"
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    network: str = "unrestricted"
    default_shell: str = "powershell"
    default_timeout_seconds: int = 600
    max_capture_bytes: int = 1_000_000
    max_file_read_bytes: int = 1_000_000
    process_buffer_bytes: int = 4_000_000
    inherit_environment: list[str] = field(default_factory=lambda: [
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
    ])
    deny_environment: list[str] = field(default_factory=lambda: [
        "*TOKEN*", "*SECRET*", "*PASSWORD*", "*KEY*", "AWS_*", "AZURE_*",
        "GITHUB_*", "NPM_TOKEN",
    ])
    extra_environment: dict[str, str] = field(default_factory=dict)
    allow_shell_commands: bool = True

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("LOCALFORGE_CONFIG") or os.environ.get("WIN_AGENT_RUNTIME_CONFIG") or "localforge.json"
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Compatibility with 1.0 configuration.
        if "shell" in data and "default_shell" not in data:
            data["default_shell"] = data.pop("shell")
        data.pop("network_hosts", None)
        data.pop("approval_ttl_seconds", None)
        cfg = cls(**data)
        cfg.workspace_root = str(Path(cfg.workspace_root).expanduser())
        if not cfg.allowed_read_roots:
            cfg.allowed_read_roots = [cfg.workspace_root]
        if not cfg.allowed_write_roots:
            cfg.allowed_write_roots = [cfg.workspace_root]
        if cfg.mode not in MODES:
            raise ValueError(f"invalid mode: {cfg.mode}")
        if cfg.network not in NETWORK_MODES:
            raise ValueError("network must be 'disabled' or 'unrestricted'")
        for name in ("default_timeout_seconds", "max_capture_bytes", "max_file_read_bytes", "process_buffer_bytes"):
            if int(getattr(cfg, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        return cfg
