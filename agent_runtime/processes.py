from __future__ import annotations
import codecs
from collections import deque
from dataclasses import dataclass, field
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from .errors import RuntimeFault
from .job import adopt as job_adopt
from .job import track as job_track
from .security import coerce_command, command_for_spawn, redact, spawn_environment

STDIN_WRITE_TIMEOUT_SECONDS = 30
# Output lines process wait returns by default, and at most.
WAIT_TAIL_LINES = 40
MAX_WAIT_TAIL_LINES = 1000
# wait match marks kept per process (see Managed.marks).
MAX_WAIT_MARKS = 32
# How long wait gives output still held after the process exits.
EXIT_GRACE_SECONDS = 1.0

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
    pumps: list = field(default_factory=list)
    # Windows: a job holding the process tree (see job.track).
    job: object = None
    # Streams that lost output to the process_buffer_bytes cap.
    dropped: set = field(default_factory=set)
    # wait matches: chunk seq -> (offset in its text just past the
    # matched line, so a wait from that cursor goes on from there;
    # seqs of the last chunks whose unfinished line it matched at exit).
    marks: dict = field(default_factory=dict)

    def __post_init__(self):
        self.chunk_cond = threading.Condition(self.lock)


def held(item):
    """True while something it started still holds its output pipes.

    Such an entry can still be read and stopped even after its own
    process exited, so it is kept and counted like a running one.
    """
    return any(p.is_alive() for p in item.pumps)


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

    def _popen(self, command, cwd, shell, env, stdin=subprocess.PIPE):
        return subprocess.Popen(
            command_for_spawn(self.cfg, command, shell), cwd=cwd, shell=False, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
            env=spawn_environment(self.cfg, shell, env), **self._creation(),
        )

    def _gc(self):
        now = time.time()
        ttl = int(self.cfg.process_ttl_seconds)
        with self.lock:
            live = {k: v for k, v in self.items.items()
                    if v.proc.poll() is None or held(v) or (now - v.started) < ttl}
            if len(live) > int(self.cfg.max_processes):
                exited = sorted(((k, v) for k, v in live.items() if v.proc.poll() is not None and not held(v)),
                                key=lambda kv: kv[1].started)
                drop = len(live) - int(self.cfg.max_processes)
                for key, _ in exited[:drop]:
                    live.pop(key, None)
            self.items = live
            active = sum(1 for v in self.items.values() if v.proc.poll() is None or held(v))
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
                        item.dropped.add(old.stream)
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

    def start(self, command, cwd=None, shell=False, env=None, process_id=None, base_cwd=None):
        return self.status(self.spawn(command, cwd, shell, env, process_id, base_cwd).id)

    def spawn(self, command, cwd=None, shell=False, env=None, process_id=None, base_cwd=None,
              enforce_limit=True, stdin=subprocess.PIPE):
        """Start a managed process and return its entry.

        enforce_limit=False skips the max_processes check; execute
        uses it and checks the limit itself before keeping a command.
        stdin is passed to Popen; execute gives DEVNULL when it has no
        input, so the child sees an empty stdin, not a closed pipe.
        """
        # base_cwd is the runtime cwd; a relative or missing cwd
        # resolves against it, like execute.
        work = self.paths.resolve(cwd or ".", cwd=base_cwd, access="read", must_exist=True)
        command = coerce_command(command)
        shell = bool(shell or isinstance(command, str))
        self.policy.authorize_command(command, work, shell)
        active = self._gc()
        if active >= int(self.cfg.max_processes) and enforce_limit:
            raise RuntimeFault("process_limit",
                               f"Too many processes: {active} running or still holding their output "
                               f"(max {self.cfg.max_processes}). Stop one with process stop; "
                               "process list shows them, including exited ones whose output is still held.")
        pid = process_id or f"proc-{uuid.uuid4().hex[:10]}"
        with self.lock:
            if pid in self.items and self.items[pid].proc.poll() is None:
                raise RuntimeFault("process_exists", f"Process id already active: {pid}")
            try:
                proc = self._popen(command, str(work), shell, env, stdin)
            except FileNotFoundError as e:
                hint = (" Check PATH, or pass the full path; a .cmd, .bat or .ps1 script needs shell: true."
                        if os.name == "nt" else "")
                program = command_for_spawn(self.cfg, command, shell)[0]
                raise RuntimeFault("command_not_found", f"Program not found: {program}.{hint}") from e
            if self.cfg.kill_children_on_exit:
                # Kernel-side cleanup: the child dies with
                # this process however it dies, not just on
                # a clean exit (see agent_runtime/job.py).
                job_adopt(proc.pid)
            # After job_adopt, so this job nests inside that one.
            item = Managed(pid, proc, command, str(work), shell, env, time.time(), job=job_track(proc.pid))
            self.items[pid] = item
        for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            pump = threading.Thread(target=self._pump, args=(item, pipe, stream), daemon=True)
            item.pumps.append(pump)
            pump.start()
        return item

    def active(self):
        """Running processes plus exited ones whose output is still held,
        after pruning old exited ones."""
        return self._gc()

    def discard(self, item):
        """Forget an entry, unless its id now names another process."""
        with self.lock:
            if self.items.get(item.id) is item:
                del self.items[item.id]

    def read(self, process_id, after=0, limit_bytes=None, wait_ms=0, fmt="text"):
        if fmt not in {"text", "chunks"}:
            raise RuntimeFault("invalid_arguments", "format must be 'text' or 'chunks'")
        item = self._get(process_id)
        if limit_bytes is None:
            # Small pages by default: the agent keeps reading from
            # next_after, and each page stays cheap to relay.
            limit_bytes = self.cfg.max_inline_output_bytes
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
                # The agent has seen the whole chunk: a wait from this
                # cursor goes on after it, not inside it past an old match.
                item.marks.pop(chunk.seq, None)
        result = {
            **self.status(process_id), "oldest_sequence": oldest,
            "cursor_lost": lost, "truncated": truncated,
            "next_after": output[-1]["seq"] if output else after,
        }
        if fmt == "chunks":
            result["chunks"] = output
        else:
            # One string per stream: far fewer tokens than a JSON
            # object per 4 KB pipe read.
            for stream in ("stdout", "stderr"):
                result[stream] = "".join(c["text"] for c in output if c["stream"] == stream)
        return result

    def wait(self, process_id, pattern=None, wait_ms=60_000, after=0, tail_lines=WAIT_TAIL_LINES):
        """Block until the process exits, an output line matches pattern,
        or wait_ms (max 60000) passes; saves polling read during long
        builds and test runs.

        pattern is a regex searched in each output line (either stream)
        that ends after the after cursor; a line the cursor cuts is
        matched whole. While the process runs, only finished lines are
        matched, so a line still being written cannot match half-way.
        Returns the status plus reason (matched, exited, or timeout),
        the matched line (None without one), tail (the last tail_lines
        lines of the kept output, both streams in arrival order, within
        max_inline_output_bytes), and next_after: after a match, the
        chunk holding the matched line (the newest chunk for an
        unfinished last line at exit), and a wait from it goes on with
        the line after the match (a read from it skips the rest of that
        chunk, which tail shows, and a wait from a cursor a read returned
        goes on after the whole chunk); otherwise the newest chunk, and a
        wait from it still looks at unfinished last lines. A
        process whose pipes something it started still holds counts as
        exited EXIT_GRACE_SECONDS after it exits, with output_held true;
        with a pattern it is watched until wait_ms passes instead, so a
        daemon it started can still print the line waited for.
        """
        try:
            regex = re.compile(pattern) if pattern is not None else None
        except re.error as e:
            raise RuntimeFault("invalid_arguments", f"Invalid pattern: {e}") from e
        tail_lines = max(1, min(int(tail_lines), MAX_WAIT_TAIL_LINES))
        item = self._get(process_id)
        after = int(after)
        deadline = time.monotonic() + min(max(int(wait_ms), 0), 60_000) / 1000
        matched, reason, hit = None, "timeout", None

        def first_match(lines):
            return next((index for index, line in enumerate(lines) if regex.search(line.rstrip("\r"))), None)

        with item.lock:
            # A cursor from a match goes on in its chunk, past the matched line.
            mark = item.marks.get(after)
            skip = {after: mark[0]} if mark is not None else {}
            # From any other cursor (a read's, or 0), each stream's output at or
            # before it was already shown, unfinished last lines included: start
            # with those last chunks done, so a wait mark saved below keeps them.
            tails = mark[1] if mark is not None else frozenset(
                {c.stream: c.seq for c in item.chunks if c.seq <= after}.values())
            scanned = after - 1 if skip else after
            partial = self._line_heads(item, scanned) if regex is not None and scanned > 0 else {}
            for seq, offset in skip.items():
                # The text before the mark ends with the matched line (a mark
                # at 0, from an unfinished line, ends no line in this chunk).
                if offset:
                    partial.pop(next((c.stream for c in item.chunks if c.seq == seq), None), None)
            exit_seen = None
            while True:
                code = item.proc.poll()
                if code is not None and exit_seen is None:
                    exit_seen = time.monotonic()
                # Something it started may hold its output pipes for long:
                # past a short grace for the rest of its own output, it
                # counts as exited all the same (output_held says so).
                # A pattern may still come from what it started: keep
                # watching until the deadline.
                exited = code is not None and (not held(item) or (
                    regex is None and time.monotonic() - exit_seen >= EXIT_GRACE_SECONDS))
                if regex is not None:
                    for chunk in item.chunks:
                        if chunk.seq <= scanned:
                            continue
                        scanned = chunk.seq
                        start = skip.get(chunk.seq, 0)
                        head = partial.get(chunk.stream, "")
                        *done, partial[chunk.stream] = (head + chunk.text[start:]).split("\n")
                        index = first_match(done)
                        if index is not None:
                            matched = done[index].rstrip("\r")
                            # Just past the matched line's "\n", in chunk.text.
                            end = sum(len(line) + 1 for line in done[:index + 1]) - len(head) + start
                            hit = (chunk.seq, end, tails)
                            break
                    if matched is None and exited and not held(item):
                        # Nothing more is coming: unfinished last lines count too,
                        # oldest stream first. A stream whose last chunk is at or
                        # before the cursor has nothing new, unless the cursor is
                        # a wait mark whose waits have not matched that line yet.
                        finals = {c.stream: c for c in item.chunks if c.seq <= scanned}
                        for final in sorted(finals.values(), key=lambda c: c.seq):
                            line = partial.get(final.stream)
                            new = final.seq > after or (mark is not None and final.seq not in tails)
                            if new and line and regex.search(line.rstrip("\r")):
                                matched = line.rstrip("\r")
                                # Marked at the newest chunk, past its last newline, so
                                # the cursor never moves back and a wait from it still
                                # sees every unfinished line; tails keeps this one
                                # from matching again.
                                newest = item.chunks[-1]
                                hit = (newest.seq, newest.text.rfind("\n") + 1, tails | {final.seq})
                                break
                    if matched is not None:
                        reason = "matched"
                        if hit is not None:
                            self._mark(item, *hit)
                        break
                if exited:
                    reason = "exited"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Pumps notify on new output and when a stream closes; the
                # short slice catches the exit itself, which nothing notifies.
                item.chunk_cond.wait(min(remaining, 0.25))
            if matched is None and item.chunks:
                # A wait from this cursor goes on past the finished lines but
                # still looks at unfinished last lines; read() clears the mark.
                # An existing mark (a match's) keeps its offset; the last lines
                # this wait already counted as done are added to its tails.
                newest = item.chunks[-1]
                old = item.marks.get(newest.seq)
                if old is None:
                    self._mark(item, newest.seq, newest.text.rfind("\n") + 1, tails)
                else:
                    item.marks[newest.seq] = (old[0], old[1] | tails)
            chunks, last = list(item.chunks), item.next_seq - 1
        text = "".join(c.text for c in chunks).replace("\r\n", "\n").rstrip("\n")
        tail = "\n".join(text.split("\n")[-tail_lines:]) if text else ""
        raw = tail.encode("utf-8", "replace")
        cap = int(self.cfg.max_inline_output_bytes)
        result = {**self.status(process_id), "reason": reason, "matched": matched,
                  "next_after": hit[0] if hit is not None else last if reason != "matched" else scanned}
        if len(raw) > cap:
            tail = raw[len(raw) - cap:].decode("utf-8", "ignore")
            result["tail_truncated"] = True
        if item.proc.poll() is not None and held(item):
            result["output_held"] = True
        result["tail"] = tail
        return result

    @staticmethod
    def _line_heads(item, upto):
        """Per stream, the start of the line still open at chunk upto: the
        text after its last newline in the kept chunks up to upto."""
        heads, closed = {}, set()
        for chunk in reversed(item.chunks):
            if chunk.seq > upto or chunk.stream in closed:
                continue
            cut = chunk.text.rfind("\n")
            heads.setdefault(chunk.stream, []).append(chunk.text[cut + 1:])
            if cut >= 0:
                closed.add(chunk.stream)
                if len(closed) == 2:
                    break
        return {stream: "".join(reversed(parts)) for stream, parts in heads.items()}

    @staticmethod
    def _mark(item, seq, offset, tails):
        # Only marks for kept chunks, and only the newest few.
        oldest = item.chunks[0].seq if item.chunks else item.next_seq
        for key in [k for k in item.marks if k < oldest]:
            del item.marks[key]
        item.marks[seq] = (offset, frozenset(tails))
        while len(item.marks) > MAX_WAIT_MARKS:
            del item.marks[min(item.marks)]

    def write(self, process_id, text, append_newline=True):
        item = self._get(process_id)
        if item.proc.poll() is not None:
            raise RuntimeFault("process_exited", "Process already exited")
        if item.proc.stdin is None:
            # Started without a stdin pipe (execute with no input).
            raise RuntimeFault("stdin_closed", "Process stdin is closed")
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
                except (BrokenPipeError, OSError, ValueError) as e:
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
        # An exited process may have left children running (holding its
        # output pipes, say); they are stopped too.
        if os.name == "nt":
            if item.proc.poll() is None:
                command = ["taskkill", "/PID", str(item.proc.pid), "/T"]
                if force:
                    command.append("/F")
                subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=15)
            # Exited before or during taskkill: /T cannot reach what it
            # started through a dead PID, the job can.
            if item.job is not None and item.proc.poll() is not None:
                item.job.terminate()
        else:
            if item.proc.poll() is not None and not held(item):
                # The leader is gone and nothing holds its output, so the
                # group is most likely empty, and its id may already
                # belong to an unrelated group. (If what holds the output
                # left the group with setsid, the group can be empty and
                # its id reused all the same; that is not detected.)
                return
            # The process group outlives its leader while members remain.
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
        if item.job is not None:
            # The leader may have exited only after _terminate_tree looked
            # (a graceful close, or taskkill /F finishing late), so end what
            # it left running through its job now that it is gone.
            item.job.terminate()
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
