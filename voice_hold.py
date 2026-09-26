#!/usr/bin/env python3
"""Hold-to-talk dictation daemon: hold Right Alt to record, release to transcribe & paste.

#18 v3 (A+): evdev listener at the kernel level (compositor-independent —
replaces pynput, which is X11-only) + arecord + transcribe_once.py + paste.py
(ydotool injection). Works on Wayland and X11 alike.

Design: docs/superpowers/specs/2026-09-20-wayland-paste-hotkey-design.md (#18 v3)

Purity contract (pinned by test_hold.py): the module top level is stdlib-only;
evdev / GTK / media_pause are lazy-imported so the module imports cleanly on
machines without them. Run under the SYSTEM python3 (python-evdev and
python-gobject come from pacman); transcription shells out to the repo venv.

Device permission: reading /dev/input/event* requires the `input` group
(udev rule from the ydotool package covers /dev/uinput; event nodes are
root:input by default). Needs a re-login after `usermod -aG input $USER`.
"""

import os
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.realpath(__file__))
VENV_PY = os.path.join(REPO_DIR, "venv", "bin", "python3")
WAVFILE = "/tmp/voice-input-hold.wav"
ENV_DEVICE = "VOICE_INPUT_DEVICE"

# KEY_RIGHTALT 单源在 paste.py(evdev KEY_RIGHTALT, linux/input-event-codes.h
# 稳定 ABI);paste 顶层纯 stdlib,顶层 import 不破纯净性契约
from paste import KEY_RIGHTALT  # noqa: F401  (re-exported for tests/callers)
EV_KEY = 1          # event type for keys

# Key event values (linux/input.h)
_PRESS, _RELEASE, _REPEAT = 1, 0, 2


def is_keyboard_device(name: str, key_codes) -> bool:
    """Device-class predicate (pure, testable): has KEY_RIGHTALT and is NOT
    ydotool's own uinput device — listening to our injector would see nothing
    real and risks confusion."""
    lowered = (name or "").lower()
    if "ydotool" in lowered:
        return False
    return KEY_RIGHTALT in key_codes


def transition(value: int, recording: bool):
    """State machine (pure): evdev key value + current state ->
    (new_state, action) with action in {None, "start", "stop"}.
    Autorepeat (value 2) and redundant press/release are ignored."""
    if value == _REPEAT:
        return recording, None
    if value == _PRESS and not recording:
        return True, "start"
    if value == _RELEASE and recording:
        return False, "stop"
    return recording, None


def pick_device(evdev, wanted: str = None):
    """Choose the input device to listen on. `wanted` (VOICE_INPUT_DEVICE) may
    be a device path (/dev/input/eventN) or a case-insensitive name substring;
    without it, the first keyboard-class device wins. Pure-ish: all evdev API
    use goes through the passed module, so tests inject a fake."""
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if wanted:
            if wanted == path or wanted.lower() in (dev.name or "").lower():
                return dev
            continue
        caps = dev.capabilities()
        key_codes = set(caps.get(EV_KEY, ()))
        if is_keyboard_device(dev.name, key_codes):
            return dev
    return None


class _Overlay:
    """Minimal GTK floating indicator (undecorated TOPLEVEL kept above).

    gtk_window_move is a no-op under Wayland (clients cannot self-position) —
    KWin places the window; README notes this. Any GTK failure disables the
    overlay for the session: recording/transcription must never depend on it.
    """

    def __init__(self):
        self._ok = False
        self._win = None
        self._label = None
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
            self._Gtk = Gtk
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_keep_above(True)
            win.set_skip_taskbar_hint(True)
            win.set_default_size(160, 48)
            self._label = Gtk.Label()
            win.add(self._label)
            self._win = win
            self._ok = True
        except Exception as e:
            print(f"[voice-hold] overlay disabled (GTK unavailable): {e}",
                  file=sys.stderr)

    def _pump(self, seconds=0.0):
        end = time.time() + seconds
        while True:
            while self._Gtk.events_pending():
                self._Gtk.main_iteration_do(False)
            if time.time() >= end:
                break
            time.sleep(0.05)

    def _disable(self, where, e):
        # CR 发现 2:静默禁用会让用户失去录音指示且查不到原因;hide 失败还要
        # 兜底销毁,否则浮层永久留在屏幕上
        print(f"[voice-hold] overlay disabled at {where}: {e}", file=sys.stderr)
        self._ok = False
        try:
            self._win.destroy()
        except Exception:
            pass

    def show(self, text, color):
        if not self._ok:
            return
        try:
            self._label.set_markup(
                f"<span font='16' foreground='{color}'>{text}</span>")
            self._win.show_all()
            self._pump()
        except Exception as e:
            self._disable("show", e)

    def hide(self, settle_s=0.0):
        if not self._ok:
            return
        try:
            self._pump(settle_s)
            self._win.hide()
            self._pump()
        except Exception as e:
            self._disable("hide", e)


class HoldDaemon:
    """Side-effect shell around the pure state machine: arecord, transcribe
    (venv), paste (paste.py), media pause (media_pause), overlay."""

    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.recording = False
        self.rec_proc = None
        self.overlay = _Overlay()
        self._paused = []
        self._media_pause = None
        try:
            sys.path.insert(0, REPO_DIR)
            import media_pause
            self._media_pause = media_pause
        except Exception as e:
            print(f"[voice-hold] media_pause unavailable: {e}", file=sys.stderr)

    def start_recording(self):
        if os.path.exists(WAVFILE):
            os.unlink(WAVFILE)
        if self._media_pause and self._media_pause.PAUSE_MEDIA_ENABLED:
            try:
                self._paused = self._media_pause.pause_playing()
            except Exception as e:
                print(f"[voice-hold] pause_media failed: {e}", file=sys.stderr)
        self.rec_proc = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
             "-D", "default", WAVFILE],
            stdout=subprocess.DEVNULL, stderr=sys.stderr)
        self.overlay.show("● REC", "#ff5555")
        print("[voice-hold] recording...", flush=True)

    def stop_recording(self):
        rec, self.rec_proc = self.rec_proc, None
        if rec is not None:
            # 与 voice-ptt.py 同款分支日志(CR 发现 1):kill 后仍不退出的卡死
            # 与普通失败必须可区分——静默吞掉会被 systemd Restart 放大成无诊断 crash-loop
            try:
                rec.terminate()
                rec.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    rec.kill()
                    rec.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    print("[voice-hold] arecord 在 kill 后仍未退出(可能残留占用音频设备)",
                          file=sys.stderr)
                except Exception as ke:
                    print(f"[voice-hold] arecord kill failed: {ke}", file=sys.stderr)
            except Exception as e:
                print(f"[voice-hold] arecord stop failed: {e}", file=sys.stderr)
        if self._media_pause and self._paused:
            try:
                self._media_pause.resume(self._paused)
            except Exception as e:
                print(f"[voice-hold] resume_media failed: {e}", file=sys.stderr)
            self._paused = []
        self.overlay.hide()
        time.sleep(0.3)  # let arecord finish flushing the wav (mirrors voice-ptt)
        if not os.path.exists(WAVFILE) or os.path.getsize(WAVFILE) < 1000:
            print("[voice-hold] recording empty/too short (<1KB)", file=sys.stderr)
            return
        try:
            text = subprocess.check_output(
                [VENV_PY, os.path.join(REPO_DIR, "transcribe_once.py"), WAVFILE],
                stderr=sys.stderr, text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[voice-hold] transcribe failed: {e}", file=sys.stderr)
            text = ""
        finally:
            if os.path.exists(WAVFILE):
                os.unlink(WAVFILE)
        if not text:
            print("[voice-hold] no speech recognized", flush=True)
            return
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_DIR, "paste.py")],
            input=text.encode(), stderr=subprocess.PIPE)
        if proc.returncode != 0:
            print(f"[voice-hold] paste failed: "
                  f"{proc.stderr.decode(errors='replace')}", file=sys.stderr)
            return
        print(f"[voice-hold] delivered: {text}", flush=True)
        self.overlay.show("DONE", "#4fc3f7")
        self.overlay.hide(settle_s=1.2)

    def handle_key_value(self, value: int):
        self.recording, action = transition(value, self.recording)
        if action == "start":
            self.start_recording()
        elif action == "stop":
            self.stop_recording()

    def _preflight(self) -> list:
        """启动预检(CR 发现 4/5):把所有「首次按键才会炸」的缺失提前到启动时报清。
        返回缺失项描述列表,空 = 全部就绪。"""
        import shutil
        missing = []
        if not os.path.isfile(VENV_PY):
            missing.append(f"venv interpreter {VENV_PY} — see README Installation")
        if shutil.which("arecord") is None:
            missing.append("arecord — pacman -S alsa-utils")
        for f in ("transcribe_once.py", "paste.py"):
            if not os.path.isfile(os.path.join(REPO_DIR, f)):
                missing.append(f"{f} missing under {REPO_DIR} — repo checkout broken?")
        return missing

    def run(self) -> int:
        missing = self._preflight()
        if missing:
            for m in missing:
                print(f"[voice-hold] preflight: {m}", file=sys.stderr)
            return 3
        try:
            import evdev
        except ImportError:
            print("[voice-hold] python-evdev missing — pacman -S python-evdev",
                  file=sys.stderr)
            return 3
        dev = pick_device(evdev, self.env.get(ENV_DEVICE))
        if dev is None:
            print(
                "[voice-hold] no keyboard device with KEY_RIGHTALT found — "
                "check `input` group membership (needs re-login) or set "
                f"{ENV_DEVICE}", file=sys.stderr)
            return 3
        print(f"[voice-hold] listening on {dev.path} ({dev.name})", flush=True)
        # read_loop blocks; unplug raises OSError -> crash -> systemd restarts
        for event in dev.read_loop():
            if event.type == EV_KEY and event.code == KEY_RIGHTALT:
                self.handle_key_value(event.value)
        return 0


def main() -> int:
    return HoldDaemon().run()


if __name__ == "__main__":
    sys.exit(main())
