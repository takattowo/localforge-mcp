from __future__ import annotations
import json
import os
import sys
import threading
from pathlib import Path

VERSION = 1

class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"localforge: ignoring unreadable state file {self.path}: {e}", file=sys.stderr)
            return {}
        if not isinstance(data, dict) or data.get("version") != VERSION:
            print(f"localforge: ignoring unknown state version in {self.path}", file=sys.stderr)
            return {}
        return data

    def save_cwd(self, cwd):
        self._save({"cwd": str(cwd)})

    def save_todos(self, todos):
        self._save({"todos": todos})

    def _save(self, updates):
        # One lock for every writer (cwd, todos): a plain
        # read-modify-write from two threads could otherwise drop
        # the other key on the floor.
        with self._lock:
            data = self.load()
            data.update(updates)
            payload = {"version": VERSION, **data}
            tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as e:
                print(f"localforge: state write failed: {e}", file=sys.stderr)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
