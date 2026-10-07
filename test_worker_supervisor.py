"""A2–A6/A15 (#25): supervisor behaviour against a real stub-worker subprocess.

The stub is a real child process speaking the real protocol — no fake sockets, so
these tests exercise the production spawn/handshake/kill path without any model.
"""

import errno
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

    def test_budget_exhausted_before_send_fails_without_kill(self):
        """Controller addendum (#25 §4.3 (b)): a ready wait that consumed the
        whole budget must fail the request without killing the worker — no
        request was ever sent. The clock jumps between the deadline captured in
        request() and the pre-send budget check, mimicking an over-long ready
        wait on an already-live worker."""
        self.env["STUB_MODE"] = "ok"
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/first.wav", timeout=10.0)[0], "stub-text")
        proc = live_worker(sup)
        # request() reads the clock twice for an already-ready worker (deadline,
        # then the pre-send check): report the second read far past the deadline.
        base = self.clock.now
        reads = []

        def jumping_clock():
            reads.append(base)
            return base if len(reads) == 1 else base + 10_000.0

        sup._clock = jumping_clock
        with mock.patch.object(sup, "_fail") as fail:
            self.assertEqual(sup.request("/tmp/second.wav", timeout=1.0),
                             (None, "timeout"))
        fail.assert_not_called()
        self.assertIsNone(proc.poll())


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
        self.assertTrue(any("protocol error" in m for m in self.logs), self.logs)

    def test_single_request_spawns_at_most_once(self):
        self.env["STUB_MODE"] = "die"
        sup = self.make_supervisor()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            sup.request("/tmp/x.wav", timeout=5.0)
            self.assertLessEqual(spy.call_count, 1)

    def test_makefile_failure_after_spawn_contains_and_reaps(self):
        """A4/A5 (#25): a failure between Popen and channel creation (e.g.
        makefile EMFILE) must stay inside the (None, reason) contract and reap
        the already-spawned worker instead of leaking it."""
        parent = mock.Mock()
        parent.makefile.side_effect = OSError(errno.EMFILE, "too many open files")
        child = mock.Mock()
        spawned = []

        def spawn(command, env, fd):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL)
            spawned.append(proc)
            return proc

        sup = self.make_supervisor()
        sup._spawn_fn = spawn
        with mock.patch.object(ws.socket, "socketpair", return_value=(parent, child)):
            self.assertEqual(sup.request("/tmp/x.wav", timeout=1.0),
                             (None, "worker-exit"))
        self.assertIsNone(sup._startup)
        self.assertIsNotNone(spawned[0].poll())   # killed + reaped, not orphaned
        parent.close.assert_called()

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

    def test_joiner_request_during_slow_start_succeeds_after_ready(self):
        """Deterministic joiner-success pin (#25 D15): a request arriving while
        a slow start is in flight waits on the shared ready event and is then
        served by the ready worker. Concurrent in-flight requests are out of
        contract (spec NG7): one handshake owner + one joiner is the
        production shape (prewarm + dictation)."""
        self.env["STUB_DELAY"] = "1"
        sup = self.make_supervisor()
        results = []

        def owner_handshake():            # prewarm's shape: handshake, no request
            results.append((sup.prewarm(), None))

        starter = threading.Thread(target=owner_handshake)
        starter.start()
        time.sleep(0.2)                   # owner is mid-handshake now
        results.append(sup.request("/tmp/joiner.wav", timeout=5.0))
        starter.join(timeout=10.0)

        self.assertIn((True, None), results)
        self.assertIn(("stub-text", None), results)
        self.assertTrue(sup.ready)
        self.assertIsNone(live_worker(sup).poll())


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

    def test_request_after_shutdown_degrades_without_respawn(self):
        """CR r1 #1 (#25): a straggler request racing teardown must degrade to
        ("worker-exit") — never crash with AttributeError, never respawn a
        worker while the daemon is exiting."""
        sup = self.make_supervisor()
        self.assertEqual(sup.request("/tmp/a.wav", timeout=10.0)[0], "stub-text")
        sup.shutdown()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            self.assertEqual(sup.request("/tmp/b.wav", timeout=10.0),
                             (None, "worker-exit"))
            spy.assert_not_called()
        self.assertIsNone(live_worker(sup))

    def test_shutdown_is_idempotent_and_kills_the_worker(self):
        sup = self.make_supervisor()
        self.assertTrue(sup.prewarm())
        proc = live_worker(sup)
        sup.shutdown()
        sup.shutdown()
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll())
