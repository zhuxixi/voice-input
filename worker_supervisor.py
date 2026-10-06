"""Supervisor for the resident transcription worker (#25).

Stdlib-only at the top level: `voice_hold.py` (pinned stdlib-only) imports this
module. The supervisor owns one worker child process: spawn, ready handshake,
request/response, timeouts, kill and throttled restart. Blocking reads happen
outside `self._lock` — the lock only guards short state updates (spec D15).

Design: docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md §4.3
"""

import socket
import subprocess
import sys
import threading
import time

from worker_protocol import EVENT_READY, ProtocolError, encode_request, parse_line

ENV_RESIDENT = "VOICE_INPUT_RESIDENT"
WORKER_RESTART_MIN_INTERVAL = 1.0   # min seconds between two spawn attempts (D5)
PREWARM_READY_TIMEOUT = 600.0       # ready-wait ceiling; older startup = failed (§4.3)
KILL_WAIT_TIMEOUT = 5.0             # how long _kill() waits for the child to die


def resident_enabled(env: dict) -> bool:
    """Pure: the escape hatch. Exactly "0" disables (#25 D8)."""
    return env.get(ENV_RESIDENT, "1") != "0"


class _Startup:
    """One in-flight worker start (spec D15).

    `ready` is set once the handshake resolved (success or failure); `reading` marks
    that some thread is inside readline() for the ready line — only one reader may
    touch the socket at a time.
    """

    def __init__(self, proc, conn, sock, started_at):
        self.proc = proc
        self.conn = conn            # buffered file object ("rwb")
        self.sock = sock            # the raw socket (settimeout lives here)
        self.started_at = started_at
        self.ready = threading.Event()
        self.reading = False
        self.error = None           # None | "timeout" | "worker-exit" | "protocol"
        self.load_s = None
        self.pid = None


class WorkerSupervisor:
    """The only owner of the resident worker's lifecycle (spec §4.3)."""

    def __init__(self, command: list, env: dict, *, spawn=None,
                 clock=time.monotonic, log=None):
        self.command = list(command)
        self.env = dict(env)
        self._spawn_fn = spawn or self._default_spawn
        self._clock = clock
        self._log = log if log is not None else (
            lambda msg: print(msg, file=sys.stderr))
        self._lock = threading.Lock()
        self._startup = None
        self._ready = False
        self._last_attempt = None
        self._last_error = None
        self._rid = 0
        self._shutdown_done = False

    # -- spawning ---------------------------------------------------------
    @staticmethod
    def _default_spawn(command, env, fd):
        """Popen with the protocol fd handed over; stdout/stderr stay inherited.

        Never use PIPE here: #29 showed a forking child can hold a pipe open
        forever, and the worker's logs belong in the journal anyway.
        """
        return subprocess.Popen(
            list(command) + ["--protocol-fd", str(fd)],
            env=env, pass_fds=(fd,), stdin=subprocess.DEVNULL,
        )

    def _spawn_locked(self):
        """Create and publish a fresh startup. Caller holds self._lock."""
        now = self._clock()
        if (self._last_attempt is not None
                and now - self._last_attempt < WORKER_RESTART_MIN_INTERVAL):
            self._last_error = "throttled"
            return None
        self._last_attempt = now
        parent = child = proc = None
        try:
            parent, child = socket.socketpair()
            proc = self._spawn_fn(self.command, self.env, child.fileno())
            child.close()                  # the child owns its end now
            conn = parent.makefile("rwb")
        except OSError as e:
            for sock in (child, parent):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=KILL_WAIT_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._last_error = "worker-exit"
            self._log(f"[voice-hold] worker spawn failed: {e}")
            return None
        self._startup = _Startup(proc, conn, parent, now)
        self._ready = False
        return self._startup

    # -- readiness --------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._ready

    def _ensure_ready(self, deadline: float):
        """-> (ok, reason). Waits for the handshake within the remaining budget.

        Owner reads the ready line; joiners wait on the event (same socket must
        never have two readers). A ready-wait timeout does NOT kill the worker
        (spec §4.3 case (b)); only an over-age startup or a read failure does.
        """
        while True:
            with self._lock:
                if self._shutdown_done:
                    # teardown already ran: never respawn during exit — a
                    # straggler request degrades cleanly instead (CR r1 #1)
                    return False, "worker-exit"
                if self._ready and self._startup is not None:
                    return True, None
                startup = self._startup
                if startup is None or startup.error is not None:
                    if startup is not None and startup.error is not None:
                        self._startup = None
                    startup = self._spawn_locked()
                    if startup is None:
                        return False, self._last_error or "worker-exit"
                    owner = True
                else:
                    # Atomic owner/joiner decision: a live, unresolved startup is
                    # owned by exactly the thread that marked it `reading`. Every
                    # other caller joins by waiting on `ready` — never a second
                    # reader on the same socket (spec D15).
                    owner = startup.reading is False and not startup.ready.is_set()
                if owner:
                    startup.reading = True

            if not owner:
                remaining = deadline - self._clock()
                if remaining <= 0 or not startup.ready.wait(timeout=remaining):
                    return False, "timeout"          # joiner: worker stays alive
                if startup.error is not None:
                    return False, startup.error
                continue

            # owner path: read the ready line outside the lock
            remaining = deadline - self._clock()
            try:
                startup.sock.settimeout(max(0.05, remaining))
                line = startup.conn.readline()
            except (socket.timeout, TimeoutError):
                with self._lock:
                    startup.reading = False
                    if self._clock() - startup.started_at >= PREWARM_READY_TIMEOUT:
                        self._log("[voice-hold] worker never became ready within "
                                  f"{PREWARM_READY_TIMEOUT:.0f}s — killing it")
                        self._kill_locked()
                    return False, "timeout"
            except OSError as e:
                with self._lock:
                    startup.error = "worker-exit"
                    self._log(f"[voice-hold] worker channel failed: {e}")
                    self._kill_locked()
                    startup.ready.set()
                return False, "worker-exit"

            if not line:
                with self._lock:
                    startup.error = "worker-exit"
                    self._log("[voice-hold] worker exited before ready "
                              "(its stderr is above)")
                    self._kill_locked()
                    startup.ready.set()
                return False, "worker-exit"
            try:
                msg = parse_line(line)
            except ProtocolError as e:
                with self._lock:
                    startup.error = "protocol"
                    self._log(f"[voice-hold] worker protocol error before ready: {e}")
                    self._kill_locked()
                    startup.ready.set()
                return False, "protocol"
            if msg.get("event") != EVENT_READY:
                with self._lock:
                    startup.error = "protocol"
                    self._log(f"[voice-hold] unexpected first line from worker: {msg!r}")
                    self._kill_locked()
                    startup.ready.set()
                return False, "protocol"
            with self._lock:
                startup.pid = msg.get("pid")
                startup.load_s = msg.get("load_s")
                startup.reading = False
                self._ready = True
                startup.ready.set()
            return True, None

    # -- requests ---------------------------------------------------------
    def request(self, wav: str, timeout: float):
        """One dictation -> (text, error). See spec §4.3 for the reason values."""
        deadline = self._clock() + timeout
        ok, reason = self._ensure_ready(deadline)
        if not ok:
            return None, reason
        with self._lock:
            startup = self._startup
            self._rid += 1
            rid = self._rid
        if startup is None:
            # shutdown() raced us between _ensure_ready and this read
            # (run()'s finally on the exit path): degrade, never crash (CR r1 #1)
            return None, "worker-exit"
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                # budget consumed by the ready wait: no kill (§4.3 (b))
                return None, "timeout"
            startup.sock.settimeout(remaining)
            startup.conn.write(encode_request(rid, wav))
            startup.conn.flush()
            while True:
                line = startup.conn.readline()
                if not line:
                    self._fail("worker-exit", "[voice-hold] worker exited mid-request")
                    return None, "worker-exit"
                msg = parse_line(line)
                if msg.get("id") != rid:
                    continue                      # stale line: ignore, keep reading
                if "error" in msg:
                    return None, f"transcribe:{msg['error']}"
                return msg.get("text", ""), None
        except (socket.timeout, TimeoutError):
            self._fail("timeout", "[voice-hold] worker read timed out — killed, "
                                  "will restart on next dictation")
            return None, "timeout"
        except ProtocolError as e:
            self._fail("protocol", f"[voice-hold] worker protocol error: {e}")
            return None, "protocol"
        except OSError as e:
            self._fail("worker-exit", f"[voice-hold] worker channel failed: {e}")
            return None, "worker-exit"

    def _fail(self, reason, message):
        with self._lock:
            self._last_error = reason
            self._log(message)
            self._kill_locked()

    def shutdown(self) -> None:
        """kill worker + close channel; idempotent (spec §4.3, pinned by A15)."""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._kill_locked()

    def prewarm(self) -> bool:
        """Start the worker now (startup thread entry). Never raises.

        Success logs `worker ready in X.XXs (pid N, engine=…)` (pinned by A15);
        failure logs an actionable line and leaves the supervisor not-ready so the
        next request retries under the throttle (spec §4.3).
        """
        deadline = self._clock() + PREWARM_READY_TIMEOUT
        ok, reason = self._ensure_ready(deadline)
        if not ok:
            self._log(f"[voice-hold] worker prewarm failed ({reason}) — next dictation "
                      "will retry; see the worker's own stderr above")
            return False
        startup = self._startup
        load_s = startup.load_s if startup and startup.load_s is not None else 0.0
        pid = startup.pid if startup else "?"
        self._log(f"[voice-hold] worker ready in {load_s:.2f}s (pid {pid}, "
                  f"engine={self.env.get('VOICE_INPUT_ENGINE', 'cuda')})")
        return True

    def _kill_locked(self):
        """Caller holds self._lock. Never blocks longer than KILL_WAIT_TIMEOUT."""
        startup, self._startup = self._startup, None
        self._ready = False
        if startup is None:
            return
        try:
            startup.conn.close()
        except OSError:
            pass
        try:
            startup.sock.close()
        except OSError:
            pass
        try:
            startup.proc.kill()
            startup.proc.wait(timeout=KILL_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._log("[voice-hold] worker survived SIGKILL (zombie?)")
        except OSError as e:
            self._log(f"[voice-hold] worker kill failed: {e}")
