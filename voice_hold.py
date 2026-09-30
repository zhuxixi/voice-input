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

import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time

import engine  # 顶层纯标准库(#16 同款);npu 子进程 env 注入的唯一来源(#19)

REPO_DIR = os.path.dirname(os.path.realpath(__file__))
VENV_PY = os.path.join(REPO_DIR, "venv", "bin", "python3")
WAVFILE = "/tmp/voice-input-hold.wav"
ENV_DEVICE = "VOICE_INPUT_DEVICE"

LOCK_STEM = "voice-input-hold"          # lock filename stem (#28)
EXIT_DUPLICATE_INSTANCE = 4             # D3: refused start, never restart-loop
RECORDING_PREFIX = "voice-input-hold-"  # per-recording wav prefix (#28)
RECORDING_SUFFIX = ".wav"

# KEY_RIGHTALT 单源在 paste.py(evdev KEY_RIGHTALT, linux/input-event-codes.h
# 稳定 ABI);paste 顶层纯 stdlib,顶层 import 不破纯净性契约
from paste import KEY_RIGHTALT  # noqa: F401  (re-exported for tests/callers)
EV_KEY = 1          # event type for keys

# Paste delivery timeout (#29): the real paste takes <0.2s (wl-copy sets the
# clipboard, ydotool injects the combo) — 10s is a 50x margin. The old 30s
# only amplified false timeouts; see the spec for the wl-copy daemonization
# mechanism that made PIPE waits hang. No env knob on purpose: the timeout
# is not a user-tunable semantic.
PASTE_TIMEOUT = 10

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


def transition(value: int, recording: bool, busy: bool = False):
    """State machine (pure): evdev key value + current state ->
    (new_recording, new_busy, action) with action in {None, "start", "stop"}.

    busy = 转写/上屏管线仍在工作线程里跑(Zima CR round-3 发现 1):期间到达的
    按键一律丢弃——单线程 read_loop 会把它们缓冲到管线返回后才处理,导致
    「迟到开始录音 + 语音静默丢失」;忙态显式丢弃则事件被即时消费,不残留。
    Autorepeat (value 2) and redundant press/release are ignored."""
    if busy:
        return recording, True, None
    if value == _REPEAT:
        return recording, False, None
    if value == _PRESS and not recording:
        return True, False, "start"
    if value == _RELEASE and recording:
        return False, False, "stop"
    return recording, False, None


def is_dropped_press(value: int, was_busy: bool) -> bool:
    """True when a key event will be silently dropped because the pipeline
    is busy (#29 D5): only presses are worth logging — autorepeat fires
    dozens of times per second while the key is held, release carries no
    user intent of its own."""
    return was_busy and value == _PRESS


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


def runtime_tmpdir(env) -> str:
    """Lock/recording directory (#28 D9): $TMPDIR only when set to a
    non-empty ABSOLUTE path, else tempfile.gettempdir(). Refusing relative
    paths matches gettempdir semantics — a relative lock path would mean
    one lock per cwd and the mutex would silently vanish."""
    cand = (env or {}).get("TMPDIR", "")
    if cand and os.path.isabs(cand):
        return cand
    return tempfile.gettempdir()


def singleton_lock_path(uid: int, tmpdir: str) -> str:
    """Lock file path for uid (pure, no I/O)."""
    return os.path.join(tmpdir, f"{LOCK_STEM}-{uid}.lock")


def acquire_singleton_lock(path: str):
    """Single-instance gate — the only place flock lives (#28).

    Returns (fd, holder_pid, error); NEVER raises (spec D3/B1):
      success        -> (fd, None, None) — caller keeps fd open for the
                        process lifetime (close releases the lock); fd is
                        non-inheritable (PEP 446) so arecord/transcribe
                        children never hold the lock (spec M3).
      already locked -> (None, <pid read from the file, best effort>, None)
      cannot open    -> (None, None, "<strerror>")  # EACCES/ELOOP/… incl.
                                                              O_NOFOLLOW
    The pid inside the file is diagnostic only ("last holder" — may be a
    dead predecessor); the mutex decision is always the flock itself.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        return None, None, (os.strerror(e.errno) if e.errno else str(e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 32).decode(errors="replace").strip()
            # isascii guard: str.isdigit() accepts Unicode digits that
            # int() rejects (e.g. "²"), which would escape the contract
            if data.isascii() and data.isdigit():
                holder = int(data)
        except OSError:
            pass
        os.close(fd)
        return None, holder, None
    except OSError as e:
        os.close(fd)
        return None, None, (os.strerror(e.errno) if e.errno else str(e))
    try:  # record our pid for the next failed acquirer (diagnostic only)
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd, None, None


def new_recording_path(tmpdir: str) -> str:
    """Fresh unique recording slot (#28 D4): mkstemp gives O_EXCL creation
    (no TOCTOU with another instance); arecord opens/truncates the path."""
    fd, path = tempfile.mkstemp(
        prefix=RECORDING_PREFIX, suffix=RECORDING_SUFFIX, dir=tmpdir)
    os.close(fd)
    return path


class _Overlay:
    """Minimal GTK floating indicator (undecorated TOPLEVEL kept above).

    gtk_window_move is a no-op under Wayland (clients cannot self-position) —
    KWin places the window; README notes this. Any GTK failure disables the
    overlay for the session: recording/transcription must never depend on it.
    """

    def __init__(self):
        # 惰性初始化(Zima CR round-3 发现 3 + systemd 排序):GTK 延后到首次
        # show()——daemon 在 graphical-session 环境就绪前启动时,监听照常工作,
        # 浮层等会话环境到位后的第一次录音再初始化;初始化失败每次录音重试
        self._inited = False
        self._dead = False
        self._win = None
        self._label = None
        self._Gtk = None

    def _try_init(self):
        if self._inited or self._dead:
            return
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
            self._inited = True
        except Exception as e:
            print(f"[voice-hold] overlay unavailable (retry next recording): {e}",
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
        # 本地 CR 发现 2:静默禁用会让用户失去录音指示且查不到原因;hide 失败
        # 还要兜底销毁,否则浮层永久留在屏幕上
        print(f"[voice-hold] overlay disabled at {where}: {e}", file=sys.stderr)
        self._dead = True
        try:
            self._win.destroy()
        except Exception:
            pass

    def show(self, text, color):
        if self._dead:
            return
        if not self._inited:
            self._try_init()
        if not self._inited:
            return
        try:
            self._label.set_markup(
                f"<span font='16' foreground='{color}'>{text}</span>")
            self._win.show_all()
            self._pump()
        except Exception as e:
            self._disable("show", e)

    def hide(self, settle_s=0.0):
        if self._dead or not self._inited:
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
        self.busy = False
        self.rec_proc = None
        self.wav_path = None    # per-recording path, set by start_recording (#28)
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
        self.wav_path = new_recording_path(runtime_tmpdir(self.env))
        if self._media_pause and self._media_pause.PAUSE_MEDIA_ENABLED:
            try:
                self._paused = self._media_pause.pause_playing()
            except Exception as e:
                print(f"[voice-hold] pause_media failed: {e}", file=sys.stderr)
        try:
            self.rec_proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
                 "-D", "default", self.wav_path],
                stdout=subprocess.DEVNULL, stderr=sys.stderr)
        except Exception:
            # M1 (#28): leave no empty slot behind; daemon crash/restart
            # semantics unchanged (the exception still propagates)
            try:
                os.unlink(self.wav_path)
            except OSError:
                pass
            raise
        self.overlay.show("● REC", "#ff5555")
        print(f"[voice-hold] recording -> {self.wav_path}", flush=True)

    def stop_recording(self):
        """同步快停(毫秒~2.5s 级):杀录音、恢复媒体、藏浮层;转写+上屏交给
        工作线程(Zima CR round-3 发现 1),read_loop 继续消费事件——忙态期间
        的新按键由 transition 丢弃,不会被 evdev 缓冲成迟到的幽灵录音。"""
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
        self.busy = True
        threading.Thread(target=self._deliver, daemon=True).start()

    def _deliver(self):
        """工作线程:转写 + 上屏(带超时,Zima CR round-3 发现 2——挂死的子进程
        必须被杀掉,busy 必须总能回到 False,否则热键一次性永久失效)。
        注意本函数不触碰 GTK(工作线程非 GTK 主线程)。"""
        try:
            time.sleep(0.3)  # let arecord finish flushing the wav (mirrors voice-ptt)
            if not os.path.exists(WAVFILE) or os.path.getsize(WAVFILE) < 1000:
                print("[voice-hold] recording empty/too short (<1KB)",
                      file=sys.stderr)
                return
            # Same-class risk (#29): check_output waits for the stdout pipe
            # EOF — if a future engine path ever daemonizes a child that
            # inherits stdout, transcribe will false-timeout exactly like
            # paste did. Today's faster-whisper / OpenVINO paths do not
            # fork; keep it that way or drop the pipe here too.
            try:
                text = subprocess.check_output(
                    [VENV_PY, os.path.join(REPO_DIR, "transcribe_once.py"),
                     WAVFILE],
                    # npu 时注入 NPU 库目录(ld.so 只在子进程启动读一次);
                    # 其他引擎原样拷贝,维持现状(#19)
                    env=engine.child_env(self.env),
                    stderr=sys.stderr, text=True,
                    timeout=self._transcribe_timeout()).strip()
            except subprocess.TimeoutExpired:
                print("[voice-hold] transcribe timed out (hung child killed) — "
                      "raise VOICE_INPUT_TRANSCRIBE_TIMEOUT if the model "
                      "legitimately needs longer", file=sys.stderr)
                text = ""
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"[voice-hold] transcribe failed: {e}", file=sys.stderr)
                text = ""
            finally:
                if os.path.exists(WAVFILE):
                    os.unlink(WAVFILE)
            if not text:
                print("[voice-hold] no speech recognized", flush=True)
                return
            self._paste_and_report(text)
        finally:
            self.busy = False

    def _spawn_paste(self, text: str):
        """Paste delivery subprocess (#29): the ONLY place paste.py is spawned.

        Never pass stdout/stderr pipes here: wl-copy daemonizes a clipboard
        server that inherits the fds, and communicate() would then wait for
        a pipe EOF that never comes (the 30s false timeout, #29). stderr
        inherits into the journal instead — paste.py prefixes its own
        messages. Returns (returncode, timed_out); on timeout subprocess.run
        has already killed the child ("hung child killed").
        """
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(REPO_DIR, "paste.py")],
                input=text.encode(), timeout=PASTE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, True
        return proc.returncode, False

    def _paste_and_report(self, text: str):
        """Deliver `text` and log the outcome (#29 test seam: depends only
        on _spawn_paste so the result branches are unit-testable without
        faking the transcribe pipeline — driving _deliver would need a real
        WAVFILE at a fixed /tmp path)."""
        rc, timed_out = self._spawn_paste(text)
        if timed_out:
            print("[voice-hold] paste timed out after "
                  f"{PASTE_TIMEOUT}s (hung child killed) — paste.py output "
                  "should be right above; if it isn't, check ydotoold "
                  "liveness (#29)", file=sys.stderr)
            return
        if rc != 0:
            print(f"[voice-hold] paste failed: paste.py exit {rc}",
                  file=sys.stderr)
            return
        print(f"[voice-hold] delivered: {text}", flush=True)

    def _transcribe_timeout(self) -> float:
        raw = self.env.get("VOICE_INPUT_TRANSCRIBE_TIMEOUT", "120")
        try:
            return max(1.0, float(raw))
        except ValueError:
            print(f"[voice-hold] bad VOICE_INPUT_TRANSCRIBE_TIMEOUT {raw!r}, "
                  "using 120", file=sys.stderr)
            return 120.0

    def handle_key_value(self, value: int):
        was_busy = self.busy
        self.recording, self.busy, action = transition(
            value, self.recording, self.busy)
        if is_dropped_press(value, was_busy):
            print("[voice-hold] key press dropped while busy "
                  "(transcribe/paste in flight)", file=sys.stderr)
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
