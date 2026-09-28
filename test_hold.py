import os
import subprocess
import sys
import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
