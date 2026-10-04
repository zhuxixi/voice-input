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
