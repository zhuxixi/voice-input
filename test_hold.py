import contextlib
import io
import os
import subprocess
import sys
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
                if rc != 0:
                    self.assertNotIn("delivered:", out)
                    self.assertEqual(len(out.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
