# #25 Resident Transcribe Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Wayland hold-to-talk path (`voice_hold.py`) a resident transcription worker process, so each dictation stops paying the utterance-independent startup cost (probe + `import openvino_genai` + NPU pipeline construction ≈ 1.1–1.45s).

**Architecture:** `voice_hold` becomes the supervisor; a new `transcribe_worker.py` (venv python) loads the model once and serves newline-delimited JSON requests over a dedicated `socketpair` fd (`--protocol-fd N`). Protocol framing lives in `worker_protocol.py` (pure), lifecycle in `worker_supervisor.py` (stdlib-only, `_Startup` owner/joiner coordination per spec D15). Startup prewarms in a background thread; a hung worker is SIGKILLed at request timeout and rebuilt on the next dictation; `VOICE_INPUT_RESIDENT=0` restores today's per-dictation spawn.

**Tech Stack:** Python 3 stdlib only (`socket`, `subprocess`, `json`, `threading`, `select`), `unittest` + `mock`, existing `engine.py` / `terms.py`.

**Spec:** `docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md` (converged after 3 cross-review rounds; §6 has the A1–A15 / U1–U5 acceptance matrix this plan traces to)

## Global Constraints

- `voice_hold.py` top level stays **stdlib-only** (pinned by `test_hold.TestModulePurity`); `worker_protocol.py` and `worker_supervisor.py` are stdlib-only too. No new dependencies. — spec NG6
- Model/engine selection goes through `engine.py` only: worker uses `engine.build_model()`; supervisor spawns with `engine.child_env(env)`. — spec §4.5
- Timeout knob: reuse `VOICE_INPUT_TRANSCRIBE_TIMEOUT`, parsed by the existing `HoldDaemon._transcribe_timeout()`. No new env knob except `VOICE_INPUT_RESIDENT` (default `"1"`, disabled **only** by exactly `"0"`). — spec D6/D8
- Worker stdout/stderr are **inherited** (→ journal); the protocol channel is the dedicated socket fd and carries nothing else. — spec D10
- Timeout semantics (spec §4.3, the normative rule): (a) read timeout **after** a request was sent → SIGKILL worker; (b) timeout while **waiting for ready** (owner or joiner) → do NOT kill, fail this dictation only; (c) a startup older than `PREWARM_READY_TIMEOUT` (600s) is declared failed → kill, so the next request respawns. — spec D15 + §4.3 + §7.4 (see Task 13 for the §7.4 wording fix)
- Worker exit codes: `0` = protocol EOF/normal, `1` = startup failure, `2` = bad argv, `3` = protocol violation. — spec §4.4
- Every blocked read happens **outside** the supervisor lock; the lock only guards short state updates. — spec D15
- `transcribe_once.py` is **not touched** (`git diff main -- transcribe_once.py` must stay empty); it stays the CLI for `test-mic.sh` / `voice-toggle.sh`. — spec NG2/A11
- Do NOT touch: `voice-ptt.py`, the paste seam (`_spawn_paste` / `paste.py`), `transition()`/busy-drop semantics, device selection, the `_deliver` `time.sleep(0.3)` settle. — spec NG2/NG4
- Tests: `unittest` + `mock`, no model/NPU/evdev/GTK; stub worker = a real subprocess speaking the protocol; suite command after every task:
  `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v`
- Work only in this worktree; commit per task with conventional-commit English messages; `git add <file>` per file (never `git add -A`).

## Review Focus

Inputs/conditions the spec implies but whose happy-path tests would otherwise miss them; each line is pinned by the named test in its owning task:

1. **Response arriving after the dictation gave up** — the worker must never deliver stale text into the next dictation; a killed worker's socket is closed and the next request spawns fresh, so no stale line can be read. → Task 5 `test_kill_closes_channel_and_next_request_uses_new_worker`.
2. **Non-ASCII / space-bearing wav paths** — `/tmp/voice-input-hold-*.wav` is ASCII, but a user-set `$TMPDIR` may not be; JSON round-trip must not mangle it. → Task 1 `test_encode_request_round_trips_unicode_path`, Task 7 `test_worker_handles_unicode_wav_path`.
3. **Silence / empty transcription** — worker must answer `{"id":N,"text":""}` (not an error), and `_deliver` must treat it as "no speech" (no paste). → Task 7 `test_empty_text_is_a_normal_response`, Task 10 `test_deliver_empty_text_does_not_paste`.
4. **Garbage bytes on the protocol channel** (library noise written to the wrong fd) — must be a `ProtocolError` → kill + respawn, never a crash loop or a silent wrong answer. → Task 4 `test_bad_line_kills_worker_and_reports_protocol`.
5. **Terms file becomes unreadable mid-session** (`terms.json` deleted/corrupt while the daemon runs) — per-request reload must degrade to no-prompt and keep the worker alive. → Task 7 `test_terms_failure_degrades_and_worker_survives`.
6. **Two rapid dictations while the previous worker is dead** — the throttle must fail the second with an actionable reason instead of hanging or spawning a storm. → Task 5 `test_throttle_blocks_second_spawn_within_interval`.
7. **Parent killed with `SIGKILL` mid-decode** — the worker must exit on protocol EOF; no 1.1GB orphan may keep the NPU. → Task 8 `test_worker_exits_on_parent_socket_close`.

---

### Task 0: Baseline the worktree

**Files:** none (verification only)

- [ ] **Step 1: Confirm the worktree is clean and on the right branch**

Run: `cd <worktree> && git status --short --branch && git log --oneline -1`
Expected: clean; branch `issue-25-resident-transcribe-worker`; top commit is the spec commit.

- [ ] **Step 2: Run the full suite before touching anything**

Run: `cd <worktree> && python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 3: If (and only if) Step 2 fails on venv paths, link the main repo's venv**

Run: `ln -s /home/elling/work/git-repo/voice-input/venv <worktree>/venv`
Do not commit the symlink (gitignored via `.git/info/exclude`'s `venv` entry). Re-run Step 2.

---

### Task 1: `worker_protocol.py` — framing (A1)

**Files:**
- Create: `worker_protocol.py`
- Test: `test_worker_protocol.py`

**Interfaces:**
- Consumes: nothing.
- Produces (later tasks rely on these exact names): `PROTOCOL_VERSION: int`, `EVENT_READY: str`, `ProtocolError`, `encode(msg: dict) -> bytes`, `encode_ready(pid, engine, model, load_s) -> bytes`, `encode_request(rid: int, wav: str) -> bytes`, `encode_text(rid: int, text: str) -> bytes`, `encode_error(rid, error: str) -> bytes`, `parse_line(line: bytes) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `test_worker_protocol.py`:

```python
"""A1 (#25): protocol framing is pure — round-trip, junk rejection, forward compat."""

import json
import unittest

import worker_protocol as wp


class TestEncodeDecode(unittest.TestCase):
    def test_encode_is_one_utf8_json_line(self):
        raw = wp.encode({"a": 1})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw.decode("utf-8")), {"a": 1})

    def test_encode_keeps_non_ascii_readable(self):
        # ensure_ascii=False: Chinese text stays UTF-8 in the frame (#25 §4.2)
        self.assertIn("语音".encode("utf-8"), wp.encode_text(1, "语音"))

    def test_ready_line_shape(self):
        msg = wp.parse_line(wp.encode_ready(123, "npu", "small-int8-ov", 0.71))
        self.assertEqual(msg["event"], "ready")
        self.assertEqual(msg["protocol"], wp.PROTOCOL_VERSION)
        self.assertEqual(msg["pid"], 123)
        self.assertEqual(msg["engine"], "npu")
        self.assertEqual(msg["model"], "small-int8-ov")
        self.assertEqual(msg["load_s"], 0.71)
        self.assertNotIn("id", msg)

    def test_request_round_trip(self):
        self.assertEqual(wp.parse_line(wp.encode_request(7, "/tmp/a.wav")),
                         {"id": 7, "wav": "/tmp/a.wav"})

    def test_encode_request_round_trips_unicode_path(self):
        path = "/tmp/语音 目录/voice-input-hold-x.wav"
        self.assertEqual(wp.parse_line(wp.encode_request(1, path))["wav"], path)

    def test_text_and_error_round_trip(self):
        self.assertEqual(wp.parse_line(wp.encode_text(2, "你好")), {"id": 2, "text": "你好"})
        self.assertEqual(wp.parse_line(wp.encode_error(2, "ValueError: boom")),
                         {"id": 2, "error": "ValueError: boom"})
        self.assertIsNone(wp.parse_line(wp.encode_error(None, "bad request"))["id"])

    def test_newline_in_error_is_escaped_not_split(self):
        raw = wp.encode_error(1, "line1\nline2")
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(wp.parse_line(raw)["error"], "line1\nline2")


class TestParseRejects(unittest.TestCase):
    def test_bad_json(self):
        with self.assertRaises(wp.ProtocolError):
            wp.parse_line(b"{not json}\n")

    def test_non_object(self):
        for raw in (b"[1, 2]\n", b"\"text\"\n", b"42\n", b"null\n"):
            with self.subTest(raw=raw), self.assertRaises(wp.ProtocolError):
                wp.parse_line(raw)

    def test_non_utf8_bytes(self):
        with self.assertRaises(wp.ProtocolError):
            wp.parse_line(b"\xff\xfe\x00\n")

    def test_id_must_be_int_when_present(self):
        for raw in (b'{"id": "1"}\n', b'{"id": 1.5}\n', b'{"id": null}\n', b'{"id": true}\n'):
            with self.subTest(raw=raw), self.assertRaises(wp.ProtocolError):
                wp.parse_line(raw)

    def test_event_line_without_id_is_valid(self):
        self.assertEqual(wp.parse_line(b'{"event":"ready"}\n'), {"event": "ready"})

    def test_unknown_fields_tolerated(self):
        msg = wp.parse_line(b'{"id":1,"text":"x","future":true}\n')
        self.assertEqual(msg["text"], "x")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_worker_protocol -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker_protocol'`.

- [ ] **Step 3: Write minimal implementation**

Create `worker_protocol.py`:

```python
"""Line-delimited JSON protocol between the hold daemon and its resident worker (#25).

Stdlib-only by contract: `voice_hold.py` (pinned stdlib-only by TestModulePurity)
imports this module transitively. One message = one UTF-8 JSON line, `\\n`-terminated,
carried on a dedicated socketpair fd (never on the worker's stdout — see spec D2/D10).

Design: docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md §4.2
"""

import json

PROTOCOL_VERSION = 1
EVENT_READY = "ready"


class ProtocolError(Exception):
    """Peer sent a line that is not a valid protocol message."""


def encode(msg: dict) -> bytes:
    """The single serialization exit (adds the newline)."""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def encode_ready(pid: int, engine: str, model: str, load_s: float) -> bytes:
    return encode({
        "event": EVENT_READY, "protocol": PROTOCOL_VERSION,
        "pid": pid, "engine": engine, "model": model, "load_s": load_s,
    })


def encode_request(rid: int, wav: str) -> bytes:
    return encode({"id": rid, "wav": wav})


def encode_text(rid: int, text: str) -> bytes:
    return encode({"id": rid, "text": text})


def encode_error(rid, error: str) -> bytes:
    return encode({"id": rid, "error": error})


def parse_line(line: bytes) -> dict:
    """bytes -> dict. Bad JSON / non-object / non-int `id` -> ProtocolError.

    Event lines (`event`) carry no `id` and are valid (#25 §4.2 rule 3); a present
    `id` must be a true int (bool is rejected — it is an int subclass).
    """
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(f"bad JSON: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError(f"not a JSON object: {type(msg).__name__}")
    if "id" in msg and (isinstance(msg["id"], bool) or not isinstance(msg["id"], int)):
        raise ProtocolError(f"id must be int, got {type(msg['id']).__name__}")
    return msg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_worker_protocol -v`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add worker_protocol.py test_worker_protocol.py
git commit -m "feat(worker): line-delimited JSON protocol framing (#25 A1)"
```

---

### Task 2: `worker_supervisor.py` skeleton — constants, `resident_enabled`, purity (A2, A11)

**Files:**
- Create: `worker_supervisor.py`
- Test: `test_worker_supervisor.py`

**Interfaces:**
- Consumes: `worker_protocol.PROTOCOL_VERSION`, `worker_protocol.EVENT_READY`, `worker_protocol.ProtocolError`, `worker_protocol.encode_request`, `worker_protocol.parse_line`.
- Produces: `ENV_RESIDENT = "VOICE_INPUT_RESIDENT"`, `WORKER_RESTART_MIN_INTERVAL = 1.0`, `PREWARM_READY_TIMEOUT = 600.0`, `resident_enabled(env: dict) -> bool`, `class WorkerSupervisor` (filled in Tasks 3–6), `class _Startup`.

- [ ] **Step 1: Write the failing tests**

Create `test_worker_supervisor.py` (the stub-worker helper is used by later tasks too — write it now, in full):

```python
"""A2–A6/A15 (#25): supervisor behaviour against a real stub-worker subprocess.

The stub is a real child process speaking the real protocol — no fake sockets, so
these tests exercise the production spawn/handshake/kill path without any model.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import worker_supervisor as ws

REPO = os.path.dirname(os.path.abspath(__file__))

# Stub worker: `--protocol-fd N` + STUB_MODE in {ok, slow-ready, never, die, bad-line}.
# `ok` answers each request with "stub-text"; `slow-ready` sleeps STUB_DELAY before ready.
STUB_WORKER = textwrap.dedent(
    '''
    import json, os, socket, sys, time

    fd = int(sys.argv[sys.argv.index("--protocol-fd") + 1])
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=fd)
    f = conn.makefile("rwb")
    mode = os.environ.get("STUB_MODE", "ok")

    if mode == "die":
        sys.stderr.write("[stub] cannot start\\n")
        sys.exit(1)
    if mode == "slow-ready":
        time.sleep(float(os.environ.get("STUB_DELAY", "2")))
    if mode == "bad-line":
        f.write(b"{not json}\\n"); f.flush()
        time.sleep(30); sys.exit(0)

    f.write((json.dumps({"event": "ready", "protocol": 1, "pid": os.getpid(),
                         "engine": "stub", "model": "stub", "load_s": 0.01}) + "\\n").encode())
    f.flush()

    while True:
        line = f.readline()
        if not line:
            break
        req = json.loads(line)
        if mode == "never":
            time.sleep(60)
            continue
        f.write((json.dumps({"id": req["id"], "text": "stub-text"}) + "\\n").encode())
        f.flush()
    '''
)


class SupervisorTestCase(unittest.TestCase):
    """Shared fixture: a stub worker script plus a supervisor pointed at it."""

    mode = "ok"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-sup-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.stub_path = os.path.join(self._tmp, "stub_worker.py")
        with open(self.stub_path, "w", encoding="utf-8") as fh:
            fh.write(STUB_WORKER)
        self.logs = []
        self.env = dict(os.environ, STUB_MODE=self.mode)
        self.clock = FakeClock()
        self.supervisors = []

    def make_supervisor(self, **kw):
        sup = ws.WorkerSupervisor(
            [sys.executable, self.stub_path], self.env,
            clock=kw.pop("clock", self.clock),
            log=kw.pop("log", self.logs.append), **kw)
        self.supervisors.append(sup)
        self.addCleanup(sup.shutdown)
        return sup


class FakeClock:
    """Injectable clock: tests advance it instead of sleeping (#25 防 flaky)."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
```

Then append the purity + env tests:

```python
class TestModulePurity(unittest.TestCase):
    """A11 (#25): both new stdlib-only modules import in a clean subprocess."""

    def test_import_in_clean_subprocess(self):
        code = ("import worker_protocol, worker_supervisor; "
                "print(worker_protocol.parse_line(b'{\"id\":1}')['id'], "
                "worker_supervisor.resident_enabled({}))")
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO, capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("1 True", proc.stdout.decode())


class TestResidentEnabled(unittest.TestCase):
    """A2 (#25): `!= "0"` contract, mirroring VOICE_INPUT_ARCHIVE/PAUSE_MEDIA."""

    def test_default_is_enabled(self):
        self.assertTrue(ws.resident_enabled({}))

    def test_exact_zero_disables(self):
        self.assertFalse(ws.resident_enabled({ws.ENV_RESIDENT: "0"}))

    def test_other_values_stay_enabled(self):
        for value in ("1", "false", "no", "", " 0", "00"):
            with self.subTest(value=value):
                self.assertTrue(ws.resident_enabled({ws.ENV_RESIDENT: value}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker_supervisor'`.

- [ ] **Step 3: Write minimal implementation**

Create `worker_supervisor.py`:

```python
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

from worker_protocol import ProtocolError, encode_request, parse_line

ENV_RESIDENT = "VOICE_INPUT_RESIDENT"
WORKER_RESTART_MIN_INTERVAL = 1.0   # min seconds between two spawn attempts (D5)
PREWARM_READY_TIMEOUT = 600.0       # ready-wait ceiling; older startup = failed (§4.3)
KILL_WAIT_TIMEOUT = 5.0             # how long _kill() waits for the child to die


def resident_enabled(env: dict) -> bool:
    """Pure: the escape hatch. Exactly "0" disables (#25 D8)."""
    return env.get(ENV_RESIDENT, "1") != "0"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: PASS (`TestModulePurity`, `TestResidentEnabled`).

- [ ] **Step 5: Commit**

```bash
git add worker_supervisor.py test_worker_supervisor.py
git commit -m "feat(worker): supervisor constants + resident switch + purity (#25 A2)"
```

---

### Task 3: Spawn, ready handshake, first successful request (A3)

**Files:**
- Modify: `worker_supervisor.py` (add `_Startup`, `WorkerSupervisor` with `__init__`, `_default_spawn`, `spawn`, `_ensure_ready`, `request`, `ready`)
- Test: `test_worker_supervisor.py`

**Interfaces:**
- Consumes: Task 1 framing, Task 2 constants.
- Produces: `WorkerSupervisor(command: list, env: dict, *, spawn=None, clock=time.monotonic, log=None)`; `WorkerSupervisor.ready -> bool`; `.request(wav: str, timeout: float) -> tuple[str | None, str | None]` (returns `(text, None)` on success, `(None, reason)` otherwise); `.shutdown() -> None` (Task 6 fills it, stub it now by calling `self._kill()`). `_Startup` attributes used by Task 5: `proc`, `conn`, `sock`, `ready` (Event), `error`, `load_s`, `pid`, `started_at`, `reading`.

- [ ] **Step 1: Write the failing test**

Append to `test_worker_supervisor.py`:

```python
class TestSpawnAndRequest(SupervisorTestCase):
    """A3 (#25): happy path — ready handshake, request/response, spawn contract."""

    def test_request_returns_text_after_ready_handshake(self):
        sup = self.make_supervisor()
        text, err = sup.request("/tmp/x.wav", timeout=10.0)
        self.assertEqual((text, err), ("stub-text", None))
        self.assertTrue(sup.ready)

    def test_ready_line_payload_is_parsed(self):
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/x.wav", timeout=10.0)[0], "stub-text")
        startup = sup._startup
        self.assertEqual(startup.error, None)
        self.assertGreater(startup.pid, 0)
        self.assertAlmostEqual(startup.load_s, 0.01, places=3)

    def test_spawn_receives_protocol_fd_and_env_verbatim(self):
        sup = self.make_supervisor()
        seen = {}

        def fake_spawn(command, env, fd):
            seen["command"] = list(command)
            seen["env_is_verbatim"] = env == self.env
            seen["fd_arg"] = ["--protocol-fd", str(fd)]
            return subprocess.Popen(
                list(command) + ["--protocol-fd", str(fd)], env=env,
                pass_fds=(fd,), stdin=subprocess.DEVNULL)

        sup._spawn_fn = fake_spawn
        self.assertEqual(sup.request("/tmp/x.wav", timeout=10.0)[0], "stub-text")
        self.assertEqual(seen["fd_arg"][0], "--protocol-fd")
        self.assertTrue(seen["fd_arg"][1].isdigit())
        self.assertTrue(seen["env_is_verbatim"])
        self.assertEqual(seen["command"][1], self.stub_path)

    def test_second_request_reuses_the_same_worker(self):
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/a.wav", timeout=10.0)[0], "stub-text")
        pid = sup._startup.proc.pid
        self.assertEqual(sup.request("/tmp/b.wav", timeout=10.0)[0], "stub-text")
        self.assertEqual(sup._startup.proc.pid, pid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_worker_supervisor.TestSpawnAndRequest -v`
Expected: FAIL with `AttributeError: 'WorkerSupervisor' object has no attribute 'request'` (or `'module' has no attribute 'WorkerSupervisor'`).

- [ ] **Step 3: Write the implementation**

Append to `worker_supervisor.py`:

```python
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
        parent, child = socket.socketpair()
        try:
            proc = self._spawn_fn(self.command, self.env, child.fileno())
        except OSError as e:
            child.close()
            parent.close()
            self._last_error = "worker-exit"
            self._log(f"[voice-hold] worker spawn failed: {e}")
            return None
        child.close()                      # the child owns its end now
        conn = parent.makefile("rwb")
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
                if self._ready and self._startup is not None:
                    return True, None
                startup = self._startup
                if startup is None:
                    startup = self._spawn_locked()
                    if startup is None:
                        return False, self._last_error or "worker-exit"
                    owner = True
                elif startup.error is not None:
                    self._startup = None
                    continue
                elif startup.reading:
                    owner = False
                else:
                    owner = True
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
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise socket.timeout()
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
        """kill worker + close channel (Task 6 makes this fully idempotent)."""
        with self._lock:
            self._kill_locked()

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
```

Also import the protocol constants at the top: change the import line to
`from worker_protocol import EVENT_READY, ProtocolError, encode_request, parse_line`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: PASS (Task 2 classes + `TestSpawnAndRequest`).

- [ ] **Step 5: Commit**

```bash
git add worker_supervisor.py test_worker_supervisor.py
git commit -m "feat(worker): supervisor spawn + ready handshake + request (#25 A3)"
```

---

### Task 4: Failure paths — timeout kill, startup death, protocol junk (A4, A5)

**Files:**
- Modify: `worker_supervisor.py` (only if a gap shows up; `_fail`/`_ensure_ready` already cover these)
- Test: `test_worker_supervisor.py`

**Interfaces:**
- Consumes: Task 3's `request`/`_ensure_ready`/`_kill_locked`.
- Produces: nothing new — this task proves the failure branches and their exact reason strings.

- [ ] **Step 1: Write the failing tests**

Append to `test_worker_supervisor.py`:

```python
def live_worker(sup):
    """Test helper: the current worker Popen, or None (kept out of the module)."""
    return None if sup._startup is None else sup._startup.proc


class TestFailurePaths(SupervisorTestCase):
    """A4/A5 (#25): hung request, dead startup, protocol junk, and respawn.

    Sockets always time out in real time (settimeout ignores the fake clock), so
    a frozen FakeClock keeps the throttle deterministic while the 0.4s budget
    still elapses for real.
    """

    mode = "never"

    def test_hung_request_times_out_and_kills_worker(self):
        sup = self.make_supervisor()
        proc = None
        t0 = time.monotonic()
        text, reason = sup.request("/tmp/x.wav", timeout=0.4)
        self.assertEqual((text, reason), (None, "timeout"))
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertIsNone(live_worker(sup))          # killed and forgotten
        self.assertTrue(any("worker read timed out" in m for m in self.logs))

    def test_next_request_after_timeout_respawns(self):
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/x.wav", timeout=0.4)[1], "timeout")
        self.clock.advance(ws.WORKER_RESTART_MIN_INTERVAL + 0.01)
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            self.assertEqual(sup.request("/tmp/y.wav", timeout=0.4)[1], "timeout")
            self.assertEqual(spy.call_count, 1)      # exactly one fresh spawn


class TestStartupFailure(SupervisorTestCase):
    """A5 (#25): a worker that dies before ready / speaks junk must fail cleanly."""

    def test_worker_exit_before_ready(self):
        sup = self.make_supervisor()
        sup.env = dict(self.env, STUB_MODE="die")
        text, reason = sup.request("/tmp/x.wav", timeout=5.0)
        self.assertEqual((text, reason), (None, "worker-exit"))
        self.assertTrue(any("exited before ready" in m for m in self.logs))
        self.assertFalse(sup.ready)

    def test_bad_line_kills_worker_and_reports_protocol(self):
        self.env["STUB_MODE"] = "bad-line"
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/x.wav", timeout=5.0)[1], "protocol")
        self.assertFalse(sup.ready)

    def test_single_request_spawns_at_most_once(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            sup.request("/tmp/x.wav", timeout=5.0)
            self.assertLessEqual(spy.call_count, 1)

    def test_default_spawn_wires_fd_and_inherits_stdio(self):
        """A5/D10 (#25): worker stdout/stderr must reach the journal — never PIPE."""
        with mock.patch.object(ws.subprocess, "Popen") as popen:
            ws.WorkerSupervisor._default_spawn(["py", "worker.py"], {"A": "1"}, 7)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["py", "worker.py", "--protocol-fd", "7"])
        self.assertEqual(kwargs["pass_fds"], (7,))
        self.assertEqual(kwargs["stdin"], ws.subprocess.DEVNULL)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_worker_supervisor.TestFailurePaths test_worker_supervisor.TestStartupFailure -v`
Expected: FAIL on the first run because `TestFailurePaths` leaves a live worker (`live_worker(sup)` is not None) or because the kill/log wiring is missing.

- [ ] **Step 3: Implement**

Add the module-level `live_worker(sup)` helper from Step 1 to `test_worker_supervisor.py` (test code only — do **not** add test-only helpers to `worker_supervisor.py`). Then make `_ensure_ready` / `request` / `_fail` log through `self._log` with the exact phrasings the tests assert (`exited before ready`, `worker read timed out`, `protocol error`). No production behaviour change beyond log text is expected — if a branch differs, fix the branch, not the test.

Note for the implementer: the timeout budget is computed from the injected `clock`, so a frozen `FakeClock` keeps it constant; `socket.settimeout()` still elapses in **real** time. That is why these tests can assert both "fails within 0.9s" and "throttle deterministic".

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: PASS (all classes so far).

- [ ] **Step 5: Commit**

```bash
git add worker_supervisor.py test_worker_supervisor.py
git commit -m "test(worker): pin timeout/crash/protocol failure paths (#25 A4/A5)"
```

---

### Task 5: Throttle + startup coordination (A4 joiner case, A6)

**Files:**
- Modify: `worker_supervisor.py` (throttle already in `_spawn_locked`; verify + expose `last_error` reason)
- Test: `test_worker_supervisor.py`

**Interfaces:**
- Consumes: Task 3/4 seams.
- Produces: the `"throttled"` reason and the joiner semantics (`ready_event` wait, no kill on timeout) that Task 6's prewarm relies on.

- [ ] **Step 1: Write the failing tests**

Append to `test_worker_supervisor.py`:

```python
class TestThrottle(SupervisorTestCase):
    """A6 (#25): at most one spawn per interval; the blocked call says so."""

    def test_throttle_blocks_second_spawn_within_interval(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            self.assertEqual(sup.request("/tmp/a.wav", timeout=5.0)[1], "worker-exit")
            self.assertEqual(sup.request("/tmp/b.wav", timeout=5.0)[1], "throttled")
            self.assertEqual(spy.call_count, 1)

    def test_spawn_allowed_again_after_interval(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            sup.request("/tmp/a.wav", timeout=5.0)
            self.clock.advance(ws.WORKER_RESTART_MIN_INTERVAL + 0.01)
            sup.request("/tmp/b.wav", timeout=5.0)
            self.assertEqual(spy.call_count, 2)


class TestStartupCoordination(SupervisorTestCase):
    """A4/D15 (#25): a request that joins an in-flight slow start must not kill it."""

    mode = "slow-ready"

    def test_joiner_times_out_without_killing_the_worker(self):
        self.env["STUB_DELAY"] = "3"
        sup = self.make_supervisor()
        starter = threading.Thread(
            target=lambda: sup.request("/tmp/owner.wav", timeout=8.0))
        starter.start()
        time.sleep(0.3)                       # let the owner thread enter the handshake
        text, reason = sup.request("/tmp/joiner.wav", timeout=0.2)
        self.assertEqual((text, reason), (None, "timeout"))
        proc = live_worker(sup)
        self.assertIsNotNone(proc)            # worker still alive: compile protected
        self.assertIsNone(proc.poll())
        starter.join(timeout=10.0)
        self.assertTrue(sup.ready)            # owner completed the handshake

    def test_joiners_do_not_read_the_socket_concurrently(self):
        self.env["STUB_DELAY"] = "1"
        sup = self.make_supervisor()
        results = []
        starter = threading.Thread(
            target=lambda: results.append(sup.request("/tmp/owner.wav", timeout=8.0)))
        starter.start()
        time.sleep(0.2)
        joiners = [threading.Thread(target=lambda: results.append(
            sup.request("/tmp/joiner.wav", timeout=5.0))) for _ in range(3)]
        for t in joiners:
            t.start()
        for t in joiners:
            t.join(timeout=8.0)
        starter.join(timeout=8.0)
        self.assertEqual([r[0] for r in results], ["stub-text"] * 4)


class TestKillSemantics(SupervisorTestCase):
    """Review Focus 1 (#25): a killed worker leaves no stale channel behind."""

    mode = "never"

    def test_kill_closes_channel_and_next_request_uses_new_worker(self):
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/a.wav", timeout=0.3)[1], "timeout")
        self.assertIsNone(live_worker(sup))
        self.clock.advance(ws.WORKER_RESTART_MIN_INTERVAL + 0.01)
        sup.env = dict(self.env, STUB_MODE="ok")
        self.assertEqual(sup.request("/tmp/b.wav", timeout=5.0), ("stub-text", None))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_worker_supervisor.TestThrottle test_worker_supervisor.TestStartupCoordination test_worker_supervisor.TestKillSemantics -v`
Expected: FAIL — `test_joiners_do_not_read_the_socket_concurrently` will block or return `timeout` because the current `_ensure_ready` lets every joiner fall through to the owner path, and `"throttled"` is not yet reported for the second call.

- [ ] **Step 3: Implement the coordination fix**

In `_ensure_ready`, the owner/joiner decision must be taken **atomically under the lock** and every non-owner must wait on `startup.ready` (never take the reader role). Replace the `while True:` body's locking block with:

```python
            with self._lock:
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
                    owner = startup.reading is False and not startup.ready.is_set()
                if owner:
                    startup.reading = True
```

…and make the joiner branch wait with `startup.ready.wait(timeout=remaining)` (already in Task 3's code) **and** re-check the deadline after the wait; a joiner that times out must return `"timeout"` **without** touching `startup.error` (the owner may still succeed).

Also make `_spawn_locked` the only place that sets `self._last_attempt`, and have `request`/`_ensure_ready` surface `"throttled"` verbatim (no rewording) so A6 can assert it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: PASS. If `TestStartupCoordination` is flaky, do **not** add sleeps: the owner/joiner handshake is protocol-driven — a joiner is only created while `startup.ready` is unset.

- [ ] **Step 5: Commit**

```bash
git add worker_supervisor.py test_worker_supervisor.py
git commit -m "feat(worker): single-flight startup, joiner wait, throttle reason (#25 A4/A6/D15)"
```

---

### Task 6: `prewarm()` + idempotent `shutdown()` (A15)

**Files:**
- Modify: `worker_supervisor.py`
- Test: `test_worker_supervisor.py`

**Interfaces:**
- Consumes: `_ensure_ready` (Task 3/5).
- Produces: `prewarm() -> bool` (logs `[voice-hold] worker ready in X.XXs (pid N, engine=…)` on success; returns False and logs on failure, never raises) and an idempotent `shutdown()`.

- [ ] **Step 1: Write the failing tests**

Append to `test_worker_supervisor.py`:

```python
class TestPrewarmAndShutdown(SupervisorTestCase):
    """A15 (#25): prewarm log contract, failure return, idempotent shutdown."""

    def test_prewarm_success_logs_the_pinned_format(self):
        sup = self.make_supervisor()
        self.assertTrue(sup.prewarm())
        self.assertTrue(any("worker ready in " in m and "(pid " in m for m in self.logs),
                        self.logs)

    def test_prewarm_failure_returns_false_and_logs(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        self.assertFalse(sup.prewarm())
        self.assertTrue(any("exited before ready" in m for m in self.logs), self.logs)

    def test_request_retries_after_prewarm_failure(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        self.assertFalse(sup.prewarm())
        self.clock.advance(ws.WORKER_RESTART_MIN_INTERVAL + 0.01)
        sup.env = dict(self.env, STUB_MODE="ok")
        self.assertEqual(sup.request("/tmp/x.wav", timeout=5.0), ("stub-text", None))

    def test_shutdown_is_idempotent_and_kills_the_worker(self):
        sup = self.make_supervisor()
        self.assertTrue(sup.prewarm())
        proc = live_worker(sup)
        sup.shutdown()
        sup.shutdown()
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_worker_supervisor.TestPrewarmAndShutdown -v`
Expected: FAIL with `AttributeError: 'WorkerSupervisor' object has no attribute 'prewarm'`.

- [ ] **Step 3: Implement**

Add to `WorkerSupervisor`:

```python
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
```

and make `shutdown()` idempotent (it already calls `_kill_locked`, which is a no-op when `self._startup is None`; add a `self._shutdown_done` guard so the second call skips logging and cannot raise):

```python
    def shutdown(self) -> None:
        """kill worker + close channel; idempotent (spec §4.3, pinned by A15)."""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._kill_locked()
```

Initialize `self._shutdown_done = False` in `__init__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: PASS (all supervisor classes).

- [ ] **Step 5: Commit**

```bash
git add worker_supervisor.py test_worker_supervisor.py
git commit -m "feat(worker): prewarm log contract + idempotent shutdown (#25 A15)"
```

---

### Task 7: `transcribe_worker.py` — worker loop (A7)

**Files:**
- Create: `transcribe_worker.py`
- Test: `test_transcribe_worker.py`

**Interfaces:**
- Consumes: `worker_protocol.*`, `engine.engine_name/required_lib_paths/prepend_library_path/has_library_paths/build_model/NPU_LIB_DIR`, `terms.DEFAULT_TERMS_PATH/load_terms/build_prompt/build_transcribe_kwargs`.
- Produces: `parse_args(argv) -> argparse.Namespace` (`protocol_fd: int`), `worker_main(conn, *, model_factory=None, terms_loader=load_terms, clock=time.monotonic, log=None) -> int` (0 on EOF, 3 on `ProtocolError`), `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `test_transcribe_worker.py`:

```python
"""A7 (#25): worker loop — ready line, per-request terms reload, error survival, EOF."""

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

import transcribe_worker as tw
import worker_protocol as wp

REPO = os.path.dirname(os.path.abspath(__file__))
TERMS = '{"terms": ["Z码"]}'


class FakeModel:
    """faster-whisper-shaped stub; records the kwargs it was called with."""

    def __init__(self):
        self.calls = []
        self.raise_next = False

    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        self.calls.append({"wav": wav, "language": language,
                           "initial_prompt": initial_prompt, **kw})
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("boom")
        text = "" if wav.endswith("silence.wav") else "你好"
        seg = type("Seg", (), {"text": text})()
        return [seg], type("Info", (), {"language": language})()


class WorkerHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-worker-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.terms_path = os.path.join(self._tmp, "terms.json")
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write(TERMS)
        self.model = FakeModel()
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        self.parent = parent.makefile("rwb")
        self.child = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                                   fileno=child.detach())
        self.child_file = self.child.makefile("rwb")
        self.done = None

    def start(self):
        def run():
            self.done = tw.worker_main(self.child_file, model_factory=lambda: self.model,
                                       terms_loader=lambda path=None: tw.load_terms(
                                           self.terms_path))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5.0)
        return t

    def read(self):
        return wp.parse_line(self.parent.readline())

    def send(self, msg):
        self.parent.write(msg)
        self.parent.flush()
```

Then the test classes:

```python
class TestWorkerLoop(WorkerHarness):
    def test_ready_then_text_response(self):
        self.start()
        ready = self.read()
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(ready["engine"], os.environ.get("VOICE_INPUT_ENGINE", "cpu"))
        self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
        self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "你好"})

    def test_empty_text_is_a_normal_response(self):
        self.start(); self.read()
        self.parent.write(wp.encode_request(1, "/tmp/silence.wav")); self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": ""})

    def test_terms_are_reloaded_per_request(self):
        self.start(); self.read()
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[0]["initial_prompt"], "Z码")
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write('{"terms": ["新词"]}')
        self.parent.write(wp.encode_request(2, "/tmp/a.wav")); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[1]["initial_prompt"], "新词")

    def test_terms_failure_degrades_and_worker_survives(self):
        self.start(); self.read()
        os.unlink(self.terms_path)
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "你好"})
        self.assertIsNone(self.model.calls[0]["initial_prompt"])

    def test_transcribe_exception_answers_error_and_survives(self):
        self.start(); self.read()
        self.model.raise_next = True
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        msg = self.read()
        self.assertIn("RuntimeError", msg["error"])
        self.parent.write(wp.encode_request(2, "/tmp/a.wav")); self.parent.flush()
        self.assertEqual(self.read()["text"], "你好")

    def test_worker_handles_unicode_wav_path(self):
        self.start(); self.read()
        path = "/tmp/语音 目录/a.wav"
        self.parent.write(wp.encode_request(1, path)); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[0]["wav"], path)

    def test_eof_returns_zero(self):
        self.start(); self.read()
        self.parent.close()
        for _ in range(50):
            if self.done is not None:
                break
            import time; time.sleep(0.05)
        self.assertEqual(self.done, 0)

    def test_bad_line_returns_three(self):
        self.start(); self.read()
        self.parent.write(b"{not json}\n"); self.parent.flush()
        for _ in range(50):
            if self.done is not None:
                break
            import time; time.sleep(0.05)
        self.assertEqual(self.done, 3)


class TestArgv(unittest.TestCase):
    def test_protocol_fd_is_required(self):
        with self.assertRaises(SystemExit):
            tw.parse_args([])

    def test_protocol_fd_parsed(self):
        self.assertEqual(tw.parse_args(["--protocol-fd", "7"]).protocol_fd, 7)

    def test_non_int_fd_rejected(self):
        with self.assertRaises(SystemExit):
            tw.parse_args(["--protocol-fd", "abc"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_transcribe_worker -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'transcribe_worker'`.

- [ ] **Step 3: Write the implementation**

Create `transcribe_worker.py`:

```python
#!/usr/bin/env python3
"""Resident transcription worker for the hold-to-talk daemon (#25).

Spawned by `worker_supervisor` with `--protocol-fd N` and serves newline-delimited
JSON requests (`{"id": N, "wav": path}` -> `{"id": N, "text": "…"}`) on that socket.
The model is built once; `terms.json` is re-read per request so hotword edits keep
taking effect without a restart (spec D9). stdout/stderr stay inherited (journal);
the protocol never shares them.

Exit codes (spec §4.4): 0 EOF/normal, 1 startup failure, 2 bad argv, 3 protocol junk.
"""

import argparse
import os
import socket
import sys
import time

_REPO_DIR = os.path.dirname(os.path.realpath(__file__))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from terms import (DEFAULT_TERMS_PATH, build_prompt, build_transcribe_kwargs,
                   load_terms)
from worker_protocol import (ProtocolError, encode_error, encode_ready, encode_text,
                             parse_line)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="transcribe_worker.py",
                                description="resident worker for voice_hold (#25)")
    p.add_argument("--protocol-fd", type=int, required=True,
                   help="inherited socket fd carrying the line protocol")
    return p.parse_args(argv)


def _preamble(log):
    """Mirror `transcribe_once.py`'s process preamble for **every** engine.

    The site-packages dir comes from this process (the worker *is* the venv
    interpreter), so no extra subprocess is needed. `required_lib_paths` needs it
    for cuda/auto (engine.py:106-116 raises without it); npu/cpu ignore it.
    """
    import sysconfig
    import engine
    site = sysconfig.get_paths()["purelib"]
    eng = engine.engine_name(dict(os.environ))          # ValueError -> caller exits 1
    paths = engine.required_lib_paths(eng, site)
    if eng == "npu" and not engine.has_library_paths(dict(os.environ), paths):
        log("[voice-input] NPU engine requires LD_LIBRARY_PATH to contain "
            f"{engine.NPU_LIB_DIR} at process start (ld.so reads it once).\n"
            f"  export LD_LIBRARY_PATH={engine.NPU_LIB_DIR}"
            "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n"
            "  or launch via voice_hold (it injects it).")
        return 3
    os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(dict(os.environ), paths)
    return 0


def worker_main(conn, *, model_factory=None, terms_loader=load_terms,
                clock=time.monotonic, log=None):
    """Serving loop. Returns 0 on protocol EOF, 3 on protocol junk (spec §4.4)."""
    import engine
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if model_factory is None:
        model_factory = engine.build_model
    t0 = clock()
    model = model_factory()
    eng = _engine_label(engine)
    log(f"[worker] model ready in {clock() - t0:.2f}s (engine={eng})")
    conn.write(encode_ready(os.getpid(), eng, _model_label(engine), clock() - t0))
    conn.flush()

    while True:
        line = conn.readline()
        if not line:                       # parent gone -> exit (no orphan, D11)
            log("[worker] protocol EOF, exiting")
            return 0
        try:
            req = parse_line(line)
        except ProtocolError as e:
            log(f"[worker] protocol error, exiting: {e}")
            return 3
        try:
            rid = req["id"]
            wav = req["wav"]
        except (KeyError, TypeError) as e:
            conn.write(encode_error(req.get("id") if isinstance(req, dict) else None,
                                    f"bad request: {e}"))
            conn.flush()
            continue
        try:
            prompt, extra_kw = None, {}
            try:
                cfg = terms_loader(DEFAULT_TERMS_PATH)
                prompt = build_prompt(cfg.get("terms", []))
                extra_kw = build_transcribe_kwargs(cfg)
            except Exception as te:        # degrade, never block transcription
                log(f"[worker] terms assemble failed, degrade to no-prompt: {te}")
            segments, _info = model.transcribe(wav, language="zh",
                                               initial_prompt=prompt, **extra_kw)
            text = "".join(s.text for s in segments).strip()
            conn.write(encode_text(rid, text))
        except Exception as e:             # worker stays alive (spec §4.4)
            conn.write(encode_error(rid, f"{type(e).__name__}: {e}"))
        conn.flush()


def _engine_label(engine) -> str:
    return os.environ.get(engine.ENV_ENGINE, engine.DEFAULT_ENGINE)


def _model_label(engine) -> str:
    return os.environ.get(engine.ENV_MODEL) or engine.default_model(_engine_label(engine))


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    log = lambda msg: print(msg, file=sys.stderr)      # noqa: E731
    rc = _preamble(log)
    if rc:
        return rc
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                             fileno=args.protocol_fd)
    except OSError as e:
        log(f"[worker] --protocol-fd {args.protocol_fd} is not a socket: {e}")
        return 1
    try:
        return worker_main(conn.makefile("rwb"), log=log)
    except Exception as e:
        log(f"[worker] startup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_transcribe_worker -v`
Expected: PASS. (`worker_main` must not import `engine` when `model_factory` is injected —
that is what keeps these tests model-free.)

- [ ] **Step 5: Commit**

```bash
git add transcribe_worker.py test_transcribe_worker.py
git commit -m "feat(worker): resident worker loop with per-request terms (#25 A7)"
```

---

### Task 8: Worker EOF self-exit as a real process (A8)

**Files:**
- Modify: `test_transcribe_worker.py` (driver script + subprocess test)
- `transcribe_worker.py` unchanged (the behavior already exists)

**Interfaces:**
- Consumes: Task 7's `worker_main`.
- Produces: proof that the last line of orphan protection works in a real process.

- [ ] **Step 1: Write the failing test**

Append to `test_transcribe_worker.py`:

```python
DRIVER = """
import os, socket, sys
sys.path.insert(0, {repo!r})
import transcribe_worker as tw

class FakeModel:
    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        return [type("S", (), {{"text": "driver-text"}})()], object()

fd = int(sys.argv[sys.argv.index("--protocol-fd") + 1])
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=fd)
sys.exit(tw.worker_main(conn.makefile("rwb"), model_factory=FakeModel,
                        log=lambda m: print(m, file=sys.stderr)))
"""


class TestRealProcessEof(unittest.TestCase):
    def test_worker_exits_on_parent_socket_close(self):
        import subprocess, textwrap
        tmp = tempfile.mkdtemp(prefix="vh-eof-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        driver = os.path.join(tmp, "driver.py")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(DRIVER.format(repo=REPO))
        parent, child = socket.socketpair()
        proc = subprocess.Popen(
            [sys.executable, driver, "--protocol-fd", str(child.fileno())],
            pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL)
        child.close()
        with parent.makefile("rwb") as f:
            self.assertEqual(wp.parse_line(f.readline())["event"], "ready")
            parent.close()                      # simulate the daemon vanishing
            self.assertEqual(proc.wait(timeout=5.0), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_transcribe_worker.TestRealProcessEof -v`
Expected: FAIL only if `worker_main` misses the EOF return (Task 7 makes it pass; this task exists to pin the real-process behavior).

- [ ] **Step 3: Implement**

No production change. If the test fails, fix `worker_main`'s EOF branch — never widen the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_transcribe_worker -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add test_transcribe_worker.py
git commit -m "test(worker): pin real-process EOF self-exit (#25 A8)"
```

---

### Task 9: `voice_hold.py` wiring — supervisor, prewarm thread, shutdown (A9 part)

**Files:**
- Modify: `voice_hold.py` (`__init__`, `run()`)
- Test: `test_hold.py`

**Interfaces:**
- Consumes: `worker_supervisor.WorkerSupervisor`, `worker_supervisor.resident_enabled`, existing `engine.child_env`, `VENV_PY`, `REPO_DIR`.
- Produces: `HoldDaemon.supervisor` (constructed with `[VENV_PY, <repo>/transcribe_worker.py]` and `engine.child_env(self.env)`), `HoldDaemon.resident` (bool), `HoldDaemon._prewarm()` (thread entry).

- [ ] **Step 1: Write the failing tests**

Append to `test_hold.py`:

```python
class TestSupervisorWiring(unittest.TestCase):
    """A9 (#25): construction contract, background prewarm, finally-shutdown."""

    def test_supervisor_constructed_with_venv_worker_and_child_env(self):
        env = {"VOICE_INPUT_ENGINE": "npu", "LD_LIBRARY_PATH": ""}
        with mock.patch.object(voice_hold, "WorkerSupervisor") as Sup:
            d = voice_hold.HoldDaemon(env)
        args, kwargs = Sup.call_args
        self.assertEqual(args[0], [voice_hold.VENV_PY,
                                   os.path.join(voice_hold.REPO_DIR, "transcribe_worker.py")])
        self.assertEqual(args[1], engine.child_env(env))
        self.assertIs(d.supervisor, Sup.return_value)

    def test_resident_flag_follows_env(self):
        with mock.patch.object(voice_hold, "WorkerSupervisor"):
            self.assertTrue(voice_hold.HoldDaemon({"VOICE_INPUT_ENGINE": "cpu"}).resident)
            self.assertFalse(voice_hold.HoldDaemon(
                {"VOICE_INPUT_ENGINE": "cpu", "VOICE_INPUT_RESIDENT": "0"}).resident)

    def test_prewarm_runs_in_background_and_never_raises(self):
        with mock.patch.object(voice_hold, "WorkerSupervisor") as Sup:
            Sup.return_value.prewarm.side_effect = RuntimeError("boom")
            d = voice_hold.HoldDaemon({"VOICE_INPUT_ENGINE": "cpu"})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                d._prewarm()
        self.assertIn("prewarm failed", err.getvalue())

    def test_run_shuts_the_supervisor_down_even_on_crash(self):
        with mock.patch.object(voice_hold, "WorkerSupervisor") as Sup, \
             mock.patch.object(voice_hold, "acquire_singleton_lock",
                               return_value=(3, None, None)), \
             mock.patch.object(voice_hold.HoldDaemon, "_preflight", return_value=[]), \
             mock.patch.object(voice_hold, "pick_device", side_effect=RuntimeError("boom")):
            d = voice_hold.HoldDaemon({"VOICE_INPUT_ENGINE": "cpu"})
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    d.run()
        self.assertEqual(Sup.return_value.shutdown.call_count, 1)
```

Add `import contextlib`, `import io`, `import engine` to `test_hold.py`'s import block if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestSupervisorWiring -v`
Expected: FAIL with `AttributeError: module 'voice_hold' has no attribute 'WorkerSupervisor'`.

- [ ] **Step 3: Implement**

In `voice_hold.py`:

1. Add to the import block (top level, stdlib-only):

```python
from worker_supervisor import WorkerSupervisor, resident_enabled
```

(and re-export nothing else; `test_hold.TestModulePurity` must keep passing).

2. Add to `HoldDaemon.__init__` (after `self._lock_fd = None`):

```python
        self.resident = resident_enabled(self.env)
        self.supervisor = WorkerSupervisor(
            [VENV_PY, os.path.join(REPO_DIR, "transcribe_worker.py")],
            engine.child_env(self.env),
        )
```

3. Add the prewarm entry:

```python
    def _prewarm(self):
        """Background prewarm thread: never let a failure reach the read loop."""
        try:
            self.supervisor.prewarm()
        except Exception as e:
            print(f"[voice-hold] prewarm failed: {e}", file=sys.stderr)
```

4. In `run()`, start it after the device check and wrap the loop:

```python
        print(f"[voice-hold] listening on {dev.path} ({dev.name})", flush=True)
        if self.resident:
            threading.Thread(target=self._prewarm, daemon=True).start()
        try:
            for event in dev.read_loop():
                if event.type == EV_KEY and event.code == KEY_RIGHTALT:
                    self.handle_key_value(event.value)
        finally:
            self.supervisor.shutdown()
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold -v`
Expected: PASS (existing classes + `TestSupervisorWiring`).

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): wire resident supervisor + prewarm + shutdown (#25 A9)"
```

---

### Task 10: `_deliver` dual branch + release/duration logs (A9, A10, D13)

**Files:**
- Modify: `voice_hold.py` (`stop_recording()`, `_deliver()`, new `_transcribe_resident()` / `_transcribe_legacy()` / `_describe_reason()`)
- Test: `test_hold.py`

**Interfaces:**
- Consumes: `self.supervisor`, `self.resident`, `self._transcribe_timeout()`, `engine.child_env`.
- Produces: the two journal lines A9/A10/U1/U4 rely on: `[voice-hold] released -> transcribing` and `[voice-hold] transcribed in X.XXs (worker|spawn)`.

- [ ] **Step 1: Write the failing tests**

Append to `test_hold.py`:

```python
class TestDeliverBranches(unittest.TestCase):
    """A9/A10 + D13 (#25): resident vs legacy transcribe, and the timing log lines."""

    def _daemon(self, env=None):
        with mock.patch.object(voice_hold, "WorkerSupervisor"):
            d = voice_hold.HoldDaemon(env or {"VOICE_INPUT_ENGINE": "cpu"})
        return d

    def _wav(self, tmpdir):
        path = os.path.join(tmpdir, "probe.wav")
        with open(path, "wb") as fh:
            fh.write(b"0" * 2000)
        return path

    def test_resident_success_pastes_and_logs_duration(self):
        d = self._daemon()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(d.supervisor, "request", return_value=("你好", None)) as req, \
             mock.patch.object(d, "_spawn_paste") as paste:
            d.wav_path = self._wav(tmp)
            paste.return_value = (0, False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                d._deliver()
        req.assert_called_once()
        self.assertEqual(req.call_args[0][0], d.wav_path)
        self.assertEqual(req.call_args[0][1], d._transcribe_timeout())
        self.assertEqual(paste.call_args[0][0], "你好")
        self.assertIn("(worker)", out.getvalue())
        self.assertFalse(os.path.exists(d.wav_path))

    def test_resident_error_does_not_paste_and_resets_busy(self):
        d = self._daemon()
        d.busy = True
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(d.supervisor, "request", return_value=(None, "throttled")), \
             mock.patch.object(d, "_spawn_paste") as paste:
            d.wav_path = self._wav(tmp)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                d._deliver()
        paste.assert_not_called()
        self.assertIn("throttled", err.getvalue())
        self.assertFalse(d.busy)

    def test_deliver_empty_text_does_not_paste(self):
        d = self._daemon()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(d.supervisor, "request", return_value=("", None)), \
             mock.patch.object(d, "_spawn_paste") as paste:
            d.wav_path = self._wav(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                d._deliver()
        paste.assert_not_called()
        self.assertIn("no speech", out.getvalue())   # "no speech recognized" goes to stdout

    def test_legacy_branch_uses_cli_and_logs_spawn(self):
        d = self._daemon({"VOICE_INPUT_ENGINE": "cpu", "VOICE_INPUT_RESIDENT": "0"})
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(d.supervisor, "request") as req, \
             mock.patch.object(voice_hold.subprocess, "check_output",
                               return_value="你好\n") as out, \
             mock.patch.object(d, "_spawn_paste") as paste:
            d.wav_path = self._wav(tmp)
            paste.return_value = (0, False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                d._deliver()
        req.assert_not_called()
        argv = out.call_args[0][0]
        self.assertEqual(argv[1], os.path.join(voice_hold.REPO_DIR, "transcribe_once.py"))
        self.assertIn("(spawn)", out.getvalue())

    def test_stop_recording_logs_release_marker(self):
        d = self._daemon()
        d.recording = True
        with mock.patch.object(voice_hold.threading, "Thread"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            d.stop_recording()
        self.assertIn("released -> transcribing", out.getvalue())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestDeliverBranches -v`
Expected: FAIL — `_deliver` still shells out unconditionally and no release marker exists.

- [ ] **Step 3: Implement**

In `voice_hold.py`, replace `_deliver`'s transcribe block with the two branches (keep the existing size check, the 0.3s settle, the `finally` unlink, and `_paste_and_report` untouched):

```python
    def _transcribe_resident(self, wav):
        """Resident branch: one round trip to the prewarmed worker."""
        t0 = time.monotonic()
        text, reason = self.supervisor.request(wav, self._transcribe_timeout())
        if reason is not None:
            print(f"[voice-hold] {self._describe_reason(reason)}", file=sys.stderr)
            return ""
        print(f"[voice-hold] transcribed in {time.monotonic() - t0:.2f}s (worker)",
              flush=True)   # stdout, same channel as "recording ->" / "delivered:"
        return text or ""

    def _transcribe_legacy(self, wav):
        """Legacy branch (#25 D8 escape hatch): spawn the CLI like before."""
        t0 = time.monotonic()
        try:
            text = subprocess.check_output(
                [VENV_PY, os.path.join(REPO_DIR, "transcribe_once.py"), wav],
                env=engine.child_env(self.env),
                stderr=sys.stderr, text=True,
                timeout=self._transcribe_timeout()).strip()
        except subprocess.TimeoutExpired:
            print("[voice-hold] transcribe timed out (hung child killed) — "
                  "raise VOICE_INPUT_TRANSCRIBE_TIMEOUT if the model "
                  "legitimately needs longer", file=sys.stderr)
            return ""
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[voice-hold] transcribe failed: {e}", file=sys.stderr)
            return ""
        print(f"[voice-hold] transcribed in {time.monotonic() - t0:.2f}s (spawn)",
              flush=True)
        return text

    @staticmethod
    def _describe_reason(reason: str) -> str:
        """Reason -> actionable log line (spec §4.3 reason values)."""
        if reason == "timeout":
            return ("transcribe timed out — worker not ready in time, this dictation "
                    "dropped (raise VOICE_INPUT_TRANSCRIBE_TIMEOUT if the model "
                    "legitimately needs longer)")
        if reason == "throttled":
            return ("transcribe skipped: worker restart throttled after a recent "
                    "failure — next dictation retries")
        if reason == "worker-exit":
            return ("transcribe worker unavailable (it exited) — next dictation "
                    "will restart it; see its stderr above")
        if reason == "protocol":
            return ("transcribe worker protocol error — worker killed, next "
                    "dictation will restart it")
        return f"transcribe failed: {reason}"
```

and in `_deliver`, the transcribe section becomes:

```python
            if self.resident:
                text = self._transcribe_resident(wav)
            else:
                text = self._transcribe_legacy(wav)
```

Add the release marker at the top of `stop_recording()` (after the snapshot of `rec`):

```python
        print("[voice-hold] released -> transcribing", flush=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold -v`
Expected: PASS. If an existing test asserted the old inline `check_output` call from `_deliver`, update **that** assertion to the new seam — the observable behaviour (argv, timeout, cleanup) is unchanged.

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): resident/legacy transcribe branches + timing logs (#25 A9/A10/D13)"
```

---

### Task 11: Static + purity gates (A11)

**Files:**
- Modify: `test_hold.py` (extend `TestModulePurity` to the new modules if Task 2's test was not enough), `README.md`, `README.zh-CN.md` (Testing suite)
- Test: commands below

- [ ] **Step 1: Run the static gates**

```bash
python3 -m py_compile voice_hold.py worker_protocol.py worker_supervisor.py transcribe_worker.py
python3 -m unittest test_hold.TestModulePurity test_worker_supervisor.TestModulePurity -v
rg -n "transcribe_once" voice_hold.py            # expect: only the legacy branch
git diff --name-only main -- transcribe_once.py  # expect: empty
```

- [ ] **Step 2: Fix anything the gates flag**

Expected failures at first run: none. If `rg` shows a second `transcribe_once` occurrence outside `_transcribe_legacy`, remove the stray reference (usually an unused import).

- [ ] **Step 3: Update the README Testing sections (EN + zh-CN)**

Replace the suite line in both files with:

```bash
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v
```

- [ ] **Step 4: Re-run the suite**

Run: `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md
git commit -m "docs: include worker test modules in the suite (#25 A11)"
```

---

### Task 12: `bench/hold-latency.py` (A13, A14 harness)

**Files:**
- Create: `bench/hold-latency.py`
- Test: `test_bench.py` (the existing bench-test home — already in the README suite)

**Interfaces:**
- Consumes: `worker_protocol`/`worker_supervisor` (resident path), `venv/bin/python3 transcribe_once.py` (spawn path).
- Produces: CLI `--wav PATH --runs N [--dry-run] [--resident-only|--spawn-only]` printing `spawn_ready_s`, per-path `min/median/max`.

- [ ] **Step 1: Write the failing test** (append to the existing `test_bench.py`, which is already in the README suite)

```python
class TestHoldLatencyDryRun(unittest.TestCase):
    """A13 (#25): argument surface only — never loads a model."""

    def test_dry_run_prints_both_paths(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "bench", "hold-latency.py"),
             "--dry-run", "--wav", "/tmp/does-not-exist.wav", "--runs", "3"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("transcribe_once.py", proc.stdout)
        self.assertIn("transcribe_worker.py", proc.stdout)
        self.assertIn("/tmp/does-not-exist.wav", proc.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_bench.TestHoldLatencyDryRun -v`
Expected: FAIL — script missing.

- [ ] **Step 3: Implement `bench/hold-latency.py`**

Structure (keep it small; reuse `worker_supervisor` for the resident half):

```python
#!/usr/bin/env python3
"""A/B latency bench for #25: per-dictation spawn vs resident worker round trip.

    venv/bin/python3 bench/hold-latency.py --wav /tmp/eight-seconds.wav --runs 5

Human-measured stage only (no hotkey, no arecord): the spawn path runs the real
`transcribe_once.py`, the resident path spawns `transcribe_worker.py` through
`WorkerSupervisor` and sends N requests. Run it while the daemon is idle; the wav
is user-supplied (never committed — the repo is public).
"""

import argparse
import os
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="spawn vs resident transcribe latency (#25)")
    p.add_argument("--wav", required=True)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--dry-run", action="store_true",
                   help="print what would run; do not touch the model")
    p.add_argument("--resident-only", action="store_true")
    p.add_argument("--spawn-only", action="store_true")
    return p.parse_args(argv)


def spawn_path(wav, runs, env_extra=None):
    import subprocess
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    script = os.path.join(REPO, "transcribe_once.py")
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    times = []
    for _ in range(runs):
        t0 = time.monotonic()
        subprocess.run([venv_py, script, wav], env=env, check=False,
                       stdout=subprocess.DEVNULL)
        times.append(time.monotonic() - t0)
    return times


def resident_path(wav, runs):
    import engine
    from worker_supervisor import WorkerSupervisor
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    sup = WorkerSupervisor([venv_py, os.path.join(REPO, "transcribe_worker.py")],
                           engine.child_env(dict(os.environ)))
    t0 = time.monotonic()
    ready = sup.prewarm()
    ready_s = time.monotonic() - t0
    times = []
    try:
        if not ready:
            raise RuntimeError("worker failed to become ready — see stderr above")
        for _ in range(runs):
            t0 = time.monotonic()
            text, reason = sup.request(wav, timeout=240.0)
            if reason:
                raise RuntimeError(f"request failed: {reason}")
            times.append(time.monotonic() - t0)
    finally:
        sup.shutdown()
    return ready_s, times


def report(label, times):
    print(f"{label}: min={min(times):.2f}s median={statistics.median(times):.2f}s "
          f"max={max(times):.2f}s runs={['%.2f' % t for t in times]}")


def main(argv=None) -> int:
    args = parse_args(argv)
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    if args.dry_run:
        print("spawn path  :", venv_py, os.path.join(REPO, "transcribe_once.py"),
              args.wav, f"x{args.runs}")
        print("resident path:", venv_py, os.path.join(REPO, "transcribe_worker.py"),
              args.wav, f"x{args.runs} (+ 1 prewarm)")
        return 0
    if not os.path.isfile(args.wav):
        print(f"wav not found: {args.wav}", file=sys.stderr)
        return 2
    if not args.resident_only:
        report("spawn   ", spawn_path(args.wav, args.runs))
    if not args.spawn_only:
        ready_s, times = resident_path(args.wav, args.runs)
        print(f"resident: spawn->ready={ready_s:.2f}s")
        report("resident", times)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_bench -v && python3 bench/hold-latency.py --dry-run --wav /tmp/x.wav --runs 3`
Expected: PASS and a two-line plan.

- [ ] **Step 5: Commit**

```bash
git add bench/hold-latency.py test_bench.py
git commit -m "feat(bench): spawn vs resident latency harness (#25 A13/A14)"
```

---

### Task 13: Docs + the §7.4 consistency fix (spec traceability)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md` (§7.4), `README.md`, `README.zh-CN.md`, `contrib/voice-hold.service`

- [ ] **Step 1: Fix the stale §7.4 sentence**

Replace in the spec's §7 item 4:

> **预热被请求超时打断**：冷编译期间到达的听写若超出 `VOICE_INPUT_TRANSCRIBE_TIMEOUT`，会 SIGKILL 正在编译的 worker（缓存多半已写入，代价是一次重建）。与今天「那一次听写本身超时」等价。

with:

> **预热被请求超时打断**：冷编译期间到达的听写若超出 `VOICE_INPUT_TRANSCRIBE_TIMEOUT`，**本次听写失败但不杀 worker**（保住进行中的编译，§4.3 情形 (b)）；只有等 ready 超过 `PREWARM_READY_TIMEOUT`(600s) 才判定启动失败并 kill，下一次听写重建。

(This aligns the risk note with the normative §4.3 rule fixed in review round 2; it is a wording-only change, no design decision is altered.)

- [ ] **Step 2: Update the READMEs**

- NPU section: replace the "Each dictation still pays ~1.4s model load (subprocess-per-dictation design, #18); a resident transcription worker is tracked as #25." sentence with the resident behaviour (prewarm at start, ~1.0–1.4s saved per dictation, first dictation after a cache wipe no longer pays the compile inside a dictation).
- Configuration table: add `VOICE_INPUT_RESIDENT` (default `1`; set `0` for the legacy per-dictation spawn; `systemctl --user edit voice-hold` example).
- Known limitations / resources: worker holds ~1.1GB RSS and keeps the NPU model resident; hung worker → killed at `VOICE_INPUT_TRANSCRIBE_TIMEOUT`, rebuilt on the next dictation.
- zh-CN: mirror all of the above.

- [ ] **Step 3: Update the service comments**

In `contrib/voice-hold.service`, replace the "常驻 worker 免每次加载见 #25" comment with the current behaviour (worker prewarms at start; timeout still bounds each dictation), and note `Environment=VOICE_INPUT_RESIDENT=0` as the escape hatch. Do not change `ExecStart`/timeouts.

- [ ] **Step 4: Verify docs consistency**

Run: `rg -n "tracked as #25|仍付 ~1.4s|免每次加载" README.md README.zh-CN.md contrib/voice-hold.service docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md`
Expected: no matches (every stale claim is gone).

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md contrib/voice-hold.service docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md
git commit -m "docs: resident worker behaviour + §7.4 consistency fix (#25)"
```

---

### Task 14: Full regression + A14 measurement + U checklist handoff (A12, A14)

**Files:** none (verification) — plus the issue comment.

- [ ] **Step 1: Full suite (A12)**

Run: `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 2: A14 measurement on this machine**

Prepare a real 8s Chinese wav outside the repo:

```bash
arecord -q -f S16_LE -r 16000 -c 1 -D default -d 8 /tmp/issue25-8s.wav   # speak during it
venv/bin/python3 bench/hold-latency.py --wav /tmp/issue25-8s.wav --runs 5
```

Expected: `resident` median < `spawn` median by ≥0.8s; record both numbers plus `spawn->ready`.
Paste the numbers into the issue comment in Step 4. Do **not** commit the wav.

- [ ] **Step 3: Emit the U checklist for the user**

Print the U1–U5 checklist from spec §6 (deployment prerequisites first: merge → `git pull` → unit diff → restart → record the commit) with a journal command per item:

```bash
journalctl --user -u voice-hold -n 40 --no-pager
```

- [ ] **Step 4: Comment on the issue**

Post one comment with: branch/commit, A1–A15 results, the A14 numbers, and the U1–U5 checklist marked `pending` until run.

- [ ] **Step 5: Hand off to CR (no push yet)**

Do **not** push or open a PR until the user explicitly approves (step 9 gate). Report the local diff summary: `git log --oneline main..HEAD && git diff --stat main`.
