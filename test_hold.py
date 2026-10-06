import contextlib
import engine
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import voice_hold

REPO = os.path.dirname(os.path.abspath(__file__))


class FakeDevice:
    def __init__(self, path, name, key_codes):
        self.path = path
        self.name = name
        self._key_codes = key_codes

    def capabilities(self):
        return {voice_hold.EV_KEY: list(self._key_codes)}


class TestStateMachine(unittest.TestCase):
    """A7: press/release/repeat transitions."""

    def test_press_starts_recording(self):
        self.assertEqual(voice_hold.transition(1, False), (True, False, "start"))

    def test_release_stops_recording(self):
        self.assertEqual(voice_hold.transition(0, True), (False, False, "stop"))

    def test_repeat_ignored_while_holding(self):
        self.assertEqual(voice_hold.transition(2, True), (True, False, None))
        self.assertEqual(voice_hold.transition(2, False), (False, False, None))

    def test_redundant_press_while_recording_ignored(self):
        self.assertEqual(voice_hold.transition(1, True), (True, False, None))

    def test_release_without_recording_ignored(self):
        self.assertEqual(voice_hold.transition(0, False), (False, False, None))

    def test_press_while_busy_dropped(self):
        # Zima round-3 发现 1:转写管线期间到达的按键必须被即时消费丢弃,
        # 否则被单线程 read_loop 缓冲成迟到的幽灵录音、语音静默丢失
        for value in (1, 0, 2):
            with self.subTest(value=value):
                self.assertEqual(
                    voice_hold.transition(value, False, busy=True),
                    (False, True, None))
                self.assertEqual(
                    voice_hold.transition(value, True, busy=True),
                    (True, True, None))


class TestDevicePredicate(unittest.TestCase):
    """A8: keyboard-class detection and ydotool exclusion."""

    def test_internal_keyboard_matches(self):
        self.assertTrue(
            voice_hold.is_keyboard_device(
                "AT Translated Set 2 keyboard", {voice_hold.KEY_RIGHTALT, 28}))

    def test_ydotool_virtual_device_excluded(self):
        # the injector's own uinput device must never be listened on
        self.assertFalse(
            voice_hold.is_keyboard_device(
                "ydotoold virtual device", {voice_hold.KEY_RIGHTALT}))

    def test_mouse_without_rightalt_rejected(self):
        self.assertFalse(
            voice_hold.is_keyboard_device("Logitech Mouse", {272, 273}))

    def test_none_name_safe(self):
        self.assertFalse(voice_hold.is_keyboard_device(None, set()))


class TestPickDevice(unittest.TestCase):
    """A8: discovery order and the VOICE_INPUT_DEVICE override."""

    def _fake_evdev(self, devices):
        return types.SimpleNamespace(
            list_devices=lambda: [d.path for d in devices],
            InputDevice=lambda p: next(d for d in devices if d.path == p),
        )

    def test_first_keyboard_wins(self):
        devices = [
            FakeDevice("/dev/input/event3", "Logitech Mouse", {272}),
            FakeDevice("/dev/input/event4", "AT Translated Set 2 keyboard",
                       {voice_hold.KEY_RIGHTALT}),
        ]
        dev = voice_hold.pick_device(self._fake_evdev(devices))
        self.assertEqual(dev.path, "/dev/input/event4")

    def test_no_keyboard_returns_none(self):
        devices = [FakeDevice("/dev/input/event3", "Logitech Mouse", {272})]
        self.assertIsNone(voice_hold.pick_device(self._fake_evdev(devices)))

    def test_override_by_path(self):
        devices = [
            FakeDevice("/dev/input/event4", "AT Translated Set 2 keyboard",
                       {voice_hold.KEY_RIGHTALT}),
            FakeDevice("/dev/input/event9", "USB Keyboard", {voice_hold.KEY_RIGHTALT}),
        ]
        dev = voice_hold.pick_device(self._fake_evdev(devices),
                                     wanted="/dev/input/event9")
        self.assertEqual(dev.path, "/dev/input/event9")

    def test_override_by_name_substring_case_insensitive(self):
        devices = [
            FakeDevice("/dev/input/event4", "AT Translated Set 2 keyboard",
                       {voice_hold.KEY_RIGHTALT}),
            FakeDevice("/dev/input/event9", "USB Keyboard", {voice_hold.KEY_RIGHTALT}),
        ]
        dev = voice_hold.pick_device(self._fake_evdev(devices), wanted="usb")
        self.assertEqual(dev.path, "/dev/input/event9")


class TestOverlayLazyInit(unittest.TestCase):
    """Zima round-3 发现 3:浮层 GTK 延迟到首次 show;构造必须零 GTK 副作用。"""

    def test_construction_touches_no_gtk(self):
        code = ("import sys, voice_hold; "
                "o = voice_hold._Overlay(); "
                "print(o._win, o._inited, 'gi' in sys.modules)")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO, capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(proc.stdout.decode().strip(), "None False False")


class TestModulePurity(unittest.TestCase):
    """A9: import must stay stdlib-only (no evdev/gi needed at import time)."""

    def test_import_in_clean_subprocess(self):
        code = ("import voice_hold; "
                "print(voice_hold.KEY_RIGHTALT, voice_hold.transition(1, False))")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO, capture_output=True,
            env={"PATH": "/usr/bin:/bin"},  # no evdev/gi on path isolation
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("100 (True, False, 'start')", proc.stdout.decode())


class TestPasteSpawnContract(unittest.TestCase):
    """#29 A1/A4/A5: paste subprocess contract — no stdio pipes (root cause
    of the 30s false timeout: wl-copy's daemonized clipboard server inherits
    the pipe and EOF never arrives), pinned timeout/argv/input, and the
    result branches of _paste_and_report."""

    def _daemon(self):
        return voice_hold.HoldDaemon(env={})

    def test_no_pipe_timeout_argv_input(self):
        d = self._daemon()
        completed = types.SimpleNamespace(returncode=0)
        with mock.patch.object(voice_hold.subprocess, "run",
                               return_value=completed) as m:
            d._spawn_paste("测试文本")
        kwargs = m.call_args.kwargs
        self.assertNotIn("stdout", kwargs)   # the #29 root cause — never again
        self.assertNotIn("stderr", kwargs)
        self.assertEqual(kwargs.get("timeout"), voice_hold.PASTE_TIMEOUT)
        self.assertEqual(kwargs["input"], "测试文本".encode())
        argv = m.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("paste.py"))

    def test_timeout_expired_swallowed(self):
        d = self._daemon()
        def raise_timeout(*a, **k):
            raise voice_hold.subprocess.TimeoutExpired("paste.py", 10)
        with mock.patch.object(voice_hold.subprocess, "run",
                               side_effect=raise_timeout):
            self.assertEqual(d._spawn_paste("x"), (None, True))

    def test_timeout_bound_and_message(self):
        self.assertLessEqual(voice_hold.PASTE_TIMEOUT, 10)
        d = self._daemon()
        d._spawn_paste = lambda text: (None, True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d._paste_and_report("x")
        out = err.getvalue()
        self.assertIn("timed out after", out)
        self.assertNotIn("ydotoold responsive", out)

    def test_result_branches(self):
        d = self._daemon()
        for rc, expect in ((3, "exit 3"), (0, "delivered:")):
            with self.subTest(rc=rc):
                d._spawn_paste = lambda text, rc=rc: (rc, False)
                err = io.StringIO()
                # capture both streams: the success branch prints to stdout
                # (flush=True, unchanged pre-#29), the fail/timeout branches
                # to stderr (spec D3/D4)
                with contextlib.redirect_stderr(err), \
                        contextlib.redirect_stdout(err):
                    d._paste_and_report("hi")
                out = err.getvalue()
                self.assertIn(expect, out)
                self.assertEqual(len(out.strip().splitlines()), 1)
                if rc != 0:
                    self.assertNotIn("delivered:", out)


class TestBusyDropLogging(unittest.TestCase):
    """#29 A2: busy-drop observability — a dropped press logs exactly one
    line; repeat/release stay silent (autorepeat fires dozens per second);
    drop semantics themselves are unchanged."""

    def _daemon(self):
        return voice_hold.HoldDaemon(env={})

    def test_is_dropped_press_pure(self):
        self.assertTrue(voice_hold.is_dropped_press(1, True))
        self.assertFalse(voice_hold.is_dropped_press(1, False))
        self.assertFalse(voice_hold.is_dropped_press(2, True))
        self.assertFalse(voice_hold.is_dropped_press(0, True))

    def test_press_while_busy_logs_one_line(self):
        d = self._daemon()
        d.busy = True
        d.recording = False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.handle_key_value(1)
        out = err.getvalue()
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("dropped while busy", out)
        self.assertFalse(d.recording)   # semantics unchanged: no ghost start

    def test_repeat_and_release_silent(self):
        d = self._daemon()
        d.busy = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.handle_key_value(2)
            d.handle_key_value(0)
        self.assertEqual(err.getvalue(), "")

    def test_normal_press_not_logged(self):
        d = self._daemon()
        d.busy = False
        d.recording = False
        d.start_recording = lambda: None   # stub: must not spawn arecord
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.handle_key_value(1)
        self.assertEqual(err.getvalue(), "")


class TestSingletonLock(unittest.TestCase):
    """A1-A6 (#28): lock path derivation + flock semantics. All fs work is
    isolated under a per-test tmpdir; nothing here touches arecord/evdev."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-lock-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _path(self):
        return voice_hold.singleton_lock_path(os.getuid(), self._tmp)

    # A1
    def test_runtime_tmpdir_env_variants(self):
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": "/var/tmp"}),
                         "/var/tmp")
        self.assertEqual(voice_hold.runtime_tmpdir({}),
                         tempfile.gettempdir())
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": ""}),
                         tempfile.gettempdir())
        # D9: relative TMPDIR must be refused — a relative lock path would
        # mean one lock per cwd and the mutex would silently vanish
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": "rel/dir"}),
                         tempfile.gettempdir())

    # A1
    def test_singleton_lock_path_contract(self):
        p = voice_hold.singleton_lock_path(1000, "/var/tmp")
        self.assertEqual(p, "/var/tmp/voice-input-hold-1000.lock")
        self.assertEqual(voice_hold.singleton_lock_path(0, "/x"),
                         "/x/voice-input-hold-0.lock")

    # A2 + A6
    def test_second_acquire_blocked_pid_reported_fd_not_inheritable(self):
        path = self._path()
        fd, holder, err = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd)
        self.assertIsNone(holder)
        self.assertIsNone(err)
        # M3: children (arecord/transcribe) must never hold the lock — a
        # hung child keeping the flock past parent death would make every
        # new instance refuse to start (permanent deafness)
        self.assertFalse(os.get_inheritable(fd))
        self.addCleanup(os.close, fd)
        fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNone(fd2)
        self.assertIsNone(err2)
        self.assertEqual(holder2, os.getpid())  # we are the holder
        with open(path) as f:  # file carries our pid for the next acquirer
            self.assertEqual(f.read(), str(os.getpid()))

    # Review Focus 3 + review round 1: garbage in the pid slot (crash
    # mid-write) degrades to holder=None instead of crashing the failed
    # acquirer. b"\xc2\xb2" decodes to U+00B2 "²": str.isdigit() is true
    # for it but int() raises ValueError — the isascii guard must catch it.
    def test_garbage_lock_file_content_degrades_to_no_holder(self):
        path = self._path()
        fd, _, _ = voice_hold.acquire_singleton_lock(path)
        self.addCleanup(os.close, fd)
        for garbage in (b"not-a-pid", b"\xc2\xb2"):
            with self.subTest(garbage=garbage):
                os.lseek(fd, 0, os.SEEK_SET)
                os.truncate(fd, 0)
                os.write(fd, garbage)
                fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
                self.assertIsNone(fd2)
                self.assertIsNone(err2)
                self.assertIsNone(holder2)

    # A3
    def test_release_then_reacquire_no_stale_lock(self):
        path = self._path()
        fd, _, _ = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd)
        os.close(fd)  # process-death analogue: kernel releases the flock
        fd2, _, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd2)
        self.assertIsNone(err2)
        os.close(fd2)

    # A5 / Review Focus 2: un-openable lock file must not raise
    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_open_failure_never_raises(self):
        # occupied-by-another semantics: a file we lack permission to open
        p = os.path.join(self._tmp, "blocked.lock")
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o600)
        res = voice_hold.acquire_singleton_lock(p)
        self.assertIsNone(res[0])
        self.assertIsNone(res[1])
        self.assertTrue(res[2])  # readable errno string, no exception
        # symlink must be rejected by O_NOFOLLOW (ELOOP)
        target = os.path.join(self._tmp, "target")
        with open(target, "w"):
            pass
        link = os.path.join(self._tmp, "link.lock")
        os.symlink(target, link)
        res2 = voice_hold.acquire_singleton_lock(link)
        self.assertIsNone(res2[0])
        self.assertIsNone(res2[1])
        self.assertTrue(res2[2])

    # A4 — real second process, with the B2 ready-line handshake: asserting
    # before the child provably holds the lock would race its flock and
    # flip the assertion (chronic-flaky classic, cf. pi-agent-board #95)
    def test_cross_process_mutex(self):
        path = self._path()
        code = (
            "import sys, time; sys.path.insert(0, %r); import voice_hold;"
            "fd, _, _ = voice_hold.acquire_singleton_lock(%r);"
            "assert fd is not None, 'child failed to acquire';"
            "print('ready', flush=True); time.sleep(30)"
        ) % (REPO, path)
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, text=True, cwd=REPO)
        try:
            # bounded handshake read: a bare readline() on a child that
            # never prints would wedge the suite until the 30s sleep ends
            line = []
            reader = threading.Thread(
                target=lambda: line.append(proc.stdout.readline()),
                daemon=True)
            reader.start()
            reader.join(5)
            self.assertFalse(reader.is_alive(), "handshake did not arrive in 5s")
            self.assertEqual(line[0].strip(), "ready")
            fd, holder, err = voice_hold.acquire_singleton_lock(path)
            self.assertIsNone(fd)
            self.assertIsNone(err)
            self.assertEqual(holder, proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=5)  # the flock dies with the child
        fd2, _, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd2)  # A3 semantics across processes
        self.assertIsNone(err2)
        os.close(fd2)


class TestRecordingPath(unittest.TestCase):
    """A7/A8/A10 (#28): per-recording unique wav path through
    start_recording/_deliver, with arecord/paste/transcribe mocked."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-wav-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _daemon(self, resident=True):
        env = {"TMPDIR": self._tmp, "VOICE_INPUT_ENGINE": "cpu"}
        if not resident:  # #25 D8: exercise the legacy CLI branch
            env["VOICE_INPUT_RESIDENT"] = "0"
        d = voice_hold.HoldDaemon(env=env)
        d.overlay = mock.Mock()   # never touch GTK from tests
        d._media_pause = None     # never touch D-Bus from tests
        return d

    # A7
    def test_new_recording_path_unique_in_dir(self):
        p1 = voice_hold.new_recording_path(self._tmp)
        p2 = voice_hold.new_recording_path(self._tmp)
        self.assertNotEqual(p1, p2)
        for p in (p1, p2):
            self.assertEqual(os.path.dirname(p), self._tmp)
            self.assertTrue(os.path.exists(p))  # slot created
            self.assertEqual(os.path.getsize(p), 0)

    # A7 + A10
    def test_start_recording_uses_fresh_path_and_logs_it(self):
        d = self._daemon()
        with mock.patch.object(voice_hold.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            d.start_recording()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "arecord")
        self.assertEqual(argv[-1], d.wav_path)          # A7: unique path
        self.assertTrue(os.path.exists(d.wav_path))     # slot exists
        self.assertIn("recording -> ", out.getvalue())  # A10: log carries it
        self.assertIn(d.wav_path, out.getvalue())

    # A7/M1/Review Focus 5: arecord spawn failure leaves no empty slot
    def test_start_recording_popen_failure_leaves_no_file(self):
        d = self._daemon()

        def boom(*a, **k):
            raise OSError("arecord missing")

        with mock.patch.object(voice_hold.subprocess, "Popen",
                               side_effect=boom):
            with self.assertRaises(OSError):
                d.start_recording()
        self.assertFalse(os.path.exists(d.wav_path))

    # A8 (legacy escape hatch, #25 ruling): resident=0 exercises the CLI spawn
    def test_deliver_transcribes_instance_path_and_cleans_up(self):
        d = self._daemon(resident=False)
        wav = voice_hold.new_recording_path(self._tmp)
        with open(wav, "wb") as f:
            f.write(b"\0" * 2048)   # pass the <1KB guard without arecord
        d.wav_path = wav
        d._spawn_paste = mock.Mock(return_value=(0, False))  # #29 seam
        with mock.patch.object(voice_hold.subprocess, "check_output",
                               return_value="你好\n") as co:
            with contextlib.redirect_stdout(io.StringIO()):
                d._deliver()
        argv = co.call_args.args[0]
        self.assertTrue(argv[1].endswith("transcribe_once.py"))
        self.assertEqual(argv[2], wav)              # A8: argv carries it
        self.assertFalse(os.path.exists(wav))       # A8: cleaned up
        d._spawn_paste.assert_called_once_with("你好")
        self.assertFalse(d.busy)                    # always un-busied

    # Review Focus 6: worker survives a missing wav_path (snapshot guard)
    def test_deliver_without_path_reports_and_returns(self):
        d = self._daemon()
        d.wav_path = None
        with mock.patch.object(voice_hold.subprocess, "check_output") as co:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                d._deliver()
        co.assert_not_called()
        self.assertIn("no recording path", err.getvalue())
        self.assertFalse(d.busy)


class TestDuplicateInstance(unittest.TestCase):
    """A9 (#28): a failed lock acquisition exits 4 before any listening."""

    def _daemon(self):
        return voice_hold.HoldDaemon(env={})

    def test_busy_lock_exits_4_without_listening(self):
        d = self._daemon()
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(None, 4242, None)), \
             mock.patch.object(
                 voice_hold.HoldDaemon, "_preflight",
                 return_value=[]) as pre, \
             mock.patch.object(voice_hold, "pick_device") as pick:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = d.run()
        self.assertEqual(rc, 4)
        self.assertEqual(rc, voice_hold.EXIT_DUPLICATE_INSTANCE)
        pre.assert_not_called()   # D5: lock gate comes before preflight
        pick.assert_not_called()
        out = err.getvalue()
        self.assertIn("another instance already running", out)
        self.assertIn("last holder pid 4242", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    # Review Focus 2: un-openable lock maps to exit 4 too (never a
    # traceback exit code that would defeat RestartPreventExitStatus)
    def test_open_error_exits_4_with_errno_message(self):
        d = self._daemon()
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(None, None, "Permission denied")), \
             mock.patch.object(voice_hold, "pick_device") as pick:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = d.run()
        self.assertEqual(rc, 4)
        pick.assert_not_called()
        out = err.getvalue()
        self.assertIn("cannot open lock file", out)
        self.assertIn("Permission denied", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_success_keeps_lock_fd_for_process_lifetime(self):
        d = self._daemon()
        fd = os.open(os.devnull, os.O_RDONLY)  # stand-in for a real lock fd
        self.addCleanup(os.close, fd)
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(fd, None, None)), \
             mock.patch.object(
                 voice_hold.HoldDaemon, "_preflight",
                 return_value=["fake missing item"]):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = d.run()   # preflight failure -> 3 (existing semantics)
        self.assertEqual(rc, 3)
        self.assertEqual(d._lock_fd, fd)   # held, not closed


class TestPreflightWorkerScript(unittest.TestCase):
    """CR r1 #2 (#25): the resident worker script is checked at startup —
    a half-deployed checkout fails preflight, not the first dictation."""

    def test_missing_worker_script_flagged_when_resident(self):
        with mock.patch.object(voice_hold, "WorkerSupervisor"):
            d = voice_hold.HoldDaemon({"VOICE_INPUT_ENGINE": "cpu"})
        with mock.patch.object(voice_hold.os.path, "isfile", return_value=False):
            missing = " ".join(d._preflight())
        self.assertIn("transcribe_worker.py missing", missing)

    def test_legacy_mode_does_not_require_worker_script(self):
        with mock.patch.object(voice_hold, "WorkerSupervisor"):
            d = voice_hold.HoldDaemon({"VOICE_INPUT_ENGINE": "cpu",
                                       "VOICE_INPUT_RESIDENT": "0"})
        with mock.patch.object(voice_hold.os.path, "isfile", return_value=False):
            missing = " ".join(d._preflight())
        self.assertNotIn("transcribe_worker.py missing", missing)


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
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                 contextlib.redirect_stdout(io.StringIO()):
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
                               return_value="你好\n") as co, \
             mock.patch.object(d, "_spawn_paste") as paste:
            d.wav_path = self._wav(tmp)
            paste.return_value = (0, False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                d._deliver()
        req.assert_not_called()
        argv = co.call_args[0][0]
        self.assertEqual(argv[1], os.path.join(voice_hold.REPO_DIR, "transcribe_once.py"))
        self.assertIn("(spawn)", out.getvalue())

    def test_stop_recording_logs_release_marker(self):
        d = self._daemon()
        d.recording = True
        with mock.patch.object(voice_hold.threading, "Thread"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            d.stop_recording()
        self.assertIn("released -> transcribing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
