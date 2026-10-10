from __future__ import annotations
import codecs
from collections import deque
from dataclasses import dataclass, field
import os
import signal
import subprocess
import threading
import time
import uuid
from .errors import RuntimeFault
from .job import adopt as job_adopt
from .security import coerce_command, command_for_spawn, redact, safe_environment

STDIN_WRITE_TIMEOUT_SECONDS = 30

@dataclass
class Chunk:
    seq: int
    stream: str
    text: str
    at: float
    raw_len: int = 0

@dataclass
class Managed:
    id: str
    proc: subprocess.Popen
    command: str | list[str]
    cwd: str
    shell: bool
    env: dict | None
    started: float
    chunks: deque = field(default_factory=deque)
    bytes: int = 0
    next_seq: int = 1
    lock: threading.RLock = field(default_factory=threading.RLock)
    stdin_lock: threading.Lock = field(default_factory=threading.Lock)
    chunk_cond: threading.Condition = field(init=False)

    def __post_init__(self):
        self.chunk_cond = threading.Condition(self.lock)

class ProcessManager:
    def __init__(self, cfg, paths, policy):
        self.cfg, self.paths, self.policy = cfg, paths, policy
        self.items = {}
        self.lock = threading.RLock()

    @staticmethod
    def _creation():
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def _popen(self, command, cwd, shell, env):
        return subprocess.Popen(
            command_for_spawn(self.cfg, command, shell), cwd=cwd, shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
            env=safe_environment(self.cfg, env), **self._creation(),
        )

    def _gc(self):
        now = time.time()
        ttl = int(self.cfg.process_ttl_seconds)
        with self.lock:
            live = {k: v for k, v in self.items.items()
                    if v.proc.poll() is None or (now - v.started) < ttl}
            if len(live) > int(self.cfg.max_processes):
                exited = sorted(((k, v) for k, v in live.items() if v.proc.poll() is not None),
                                key=lambda kv: kv[1].started)
                drop = len(live) - int(self.cfg.max_processes)
                for key, _ in exited[:drop]:
                    live.pop(key, None)
            self.items = live
            active = sum(1 for v in self.items.values() if v.proc.poll() is None)
            return active

    def _pump(self, item, pipe, stream):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = os.read(pipe.fileno(), 4096)
                if not data:
                    break
                text = decoder.decode(data)
                if not text:
                    continue
                text = redact(text)
                with item.lock:
                    item.chunks.append(Chunk(item.next_seq, stream, text, time.time(), len(data)))
                    item.next_seq += 1
                    item.bytes += len(data)
                    while item.bytes > self.cfg.process_buffer_bytes and item.chunks:
                        old = item.chunks.popleft()
                        item.bytes -= old.raw_len
                    item.chunk_cond.notify_all()
        finally:
            tail = decoder.decode(b"", final=True)
            with item.lock:
                if tail:
                    item.chunks.append(Chunk(item.next_seq, stream, redact(tail), time.time(), 0))
                    item.next_seq += 1
                # Wake readers so a wait ends promptly when
                # the stream closes, even without new chunks.
                item.chunk_cond.notify_all()
            pipe.close()

    def start(self, command, cwd=None, shell=False, env=None, process_id=None):
        work = self.paths.resolve(cwd or ".", access="read", must_exist=True)
        command = coerce_command(command)
        shell = bool(shell or isinstance(command, str))
        self.policy.authorize_command(command, work, shell)
        if self._gc() >= int(self.cfg.max_processes):
            raise RuntimeFault("process_limit", f"Too many processes (max {self.cfg.max_processes})")
        pid = process_id or f"proc-{uuid.uuid4().hex[:10]}"
        with self.lock:
            if pid in self.items and self.items[pid].proc.poll() is None:
                raise RuntimeFault("process_exists", f"Process id already active: {pid}")
            proc = self._popen(command, str(work), shell, env)
            if self.cfg.kill_children_on_exit:
                # Kernel-side cleanup: the child dies with
                # this process however it dies, not just on
                # a clean exit (see agent_runtime/job.py).
                job_adopt(proc.pid)
            item = Managed(pid, proc, command, str(work), shell, env, time.time())
            self.items[pid] = item
        for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(target=self._pump, args=(item, pipe, stream), daemon=True).start()
        return self.status(pid)

    def read(self, process_id, after=0, limit_bytes=200_000, wait_ms=0):
        item = self._get(process_id)
        limit_bytes = max(1, min(int(limit_bytes), self.cfg.max_capture_bytes))
        deadline = time.monotonic() + min(max(int(wait_ms), 0), 60_000) / 1000
        # Wait on a condition notified by the pump threads
        # instead of polling: readers wake the moment new
        # chunks land, and idle waits cost no CPU.
        with item.lock:
            while time.monotonic() < deadline:
                if any(c.seq > after for c in item.chunks):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                item.chunk_cond.wait(remaining)
            output, used, truncated = [], 0, False
            oldest = item.chunks[0].seq if item.chunks else item.next_seq
            lost = bool(after and after < oldest - 1)
            for chunk in item.chunks:
                if chunk.seq <= after:
                    continue
                size = len(chunk.text.encode("utf-8", "replace"))
                if output and used + size > limit_bytes:
                    truncated = True
                    break
                output.append({"seq": chunk.seq, "stream": chunk.stream, "text": chunk.text, "at": chunk.at})
                used += size
        return {
            **self.status(process_id), "chunks": output, "oldest_sequence": oldest,
            "cursor_lost": lost, "truncated": truncated,
            "next_after": output[-1]["seq"] if output else after,
        }

    def write(self, process_id, text, append_newline=True):
        item = self._get(process_id)
        if item.proc.poll() is not None:
            raise RuntimeFault("process_exited", "Process already exited")
        data = (text + ("\n" if append_newline else "")).encode("utf-8")
        # stdin is a pipe: a child that never drains it fills
        # the buffer and blocks the write forever, pinning a
        # worker. Write on a helper thread with a timeout; the
        # helper stays blocked only until the child exits and
        # the pipe breaks. Writes serialize per process.
        with item.stdin_lock:
            outcome: dict = {}

            def _do_write():
                try:
                    item.proc.stdin.write(data)
                    item.proc.stdin.flush()
                    outcome["ok"] = True
                except (BrokenPipeError, OSError) as e:
                    outcome["error"] = e

            writer = threading.Thread(target=_do_write, daemon=True)
            writer.start()
            writer.join(STDIN_WRITE_TIMEOUT_SECONDS)
        if writer.is_alive():
            raise RuntimeFault("stdin_blocked",
                               f"Process did not drain stdin within "
                               f"{STDIN_WRITE_TIMEOUT_SECONDS}s; the write "
                               "may complete later or fail when the process exits.")
        if "error" in outcome:
            raise RuntimeFault("stdin_closed", "Process stdin is closed") from outcome["error"]
        if "ok" not in outcome:
            raise RuntimeFault("stdin_closed", "Process stdin is closed")
        return {"process_id": process_id, "written_bytes": len(data)}

    def _terminate_tree(self, item, force=False):
        if item.proc.poll() is not None:
            return
        if os.name == "nt":
            command = ["taskkill", "/PID", str(item.proc.pid), "/T"]
            if force:
                command.append("/F")
            subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(item.proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    def stop(self, process_id, force=False):
        item = self._get(process_id)
        self._terminate_tree(item, force)
        try:
            item.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate_tree(item, True)
            item.proc.wait(timeout=10)
        return self.status(process_id)

    def restart(self, process_id):
        old = self._get(process_id)
        command, cwd, shell, env = old.command, old.cwd, old.shell, old.env
        self.stop(process_id, True)
        with self.lock:
            self.items.pop(process_id, None)
        return self.start(command, cwd, shell, env, process_id)

    def status(self, process_id):
        item = self._get(process_id)
        code = item.proc.poll()
        return {
            "process_id": process_id, "pid": item.proc.pid,
            "state": "running" if code is None else "exited", "exit_code": code,
            "command": item.command, "cwd": item.cwd, "started_at": item.started,
        }

    def list(self):
        self._gc()
        return [self.status(key) for key in list(self.items)]

    def _get(self, process_id):
        with self.lock:
            if process_id not in self.items:
                raise RuntimeFault("process_not_found", f"Unknown process: {process_id}")
            return self.items[process_id]

    def cleanup(self):
        for process_id in list(self.items):
            try:
                self.stop(process_id, True)
            except Exception:
                pass
