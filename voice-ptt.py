#!/usr/bin/env python3
"""按住右 Command 键录音，松开转写并输入到当前窗口。"""

import os
import sys

VENV = "/home/elling/.local/share/voice-input/venv"
os.environ["LD_LIBRARY_PATH"] = (
    f"{VENV}/lib/python3.12/site-packages/nvidia/cublas/lib:"
    f"{VENV}/lib/python3.12/site-packages/nvidia/cudnn/lib:"
    f"{VENV}/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib"
    + (f':{os.environ.get("LD_LIBRARY_PATH", "")}')
)

import subprocess
import threading
import time
import signal
from archive import archive_recording, ARCHIVE_ENABLED
from media_pause import pause_playing, resume, PAUSE_MEDIA_ENABLED
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

from pynput import keyboard

WAVFILE = "/tmp/voice-input-recording.wav"
MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3"
    "/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478"
)

recording = False
rec_proc = None
active_window = None
overlay_window = None
_paused_players = []  # 录音开始时被暂停的 MPRIS 播放器 bus name,stop 时 resume
_paused_lock = threading.Lock()  # 保护 _paused_players 跨 start/stop 线程读写(cc#1)

def _make_css(color_hex):
    css = f"label {{ color: {color_hex}; font: bold 13px Sans; }}"
    p = Gtk.CssProvider()
    p.load_from_data(css.encode())
    return p


def show_overlay(text, css_hex):
    global overlay_window
    hide_overlay()

    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_decorated(False)
    win.set_keep_above(True)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_accept_focus(False)
    win.set_app_paintable(True)
    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual:
        win.set_visual(visual)

    label = Gtk.Label(label=text)
    label.get_style_context().add_provider(
        _make_css(css_hex), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    win.add(label)
    win.show_all()
    overlay_window = win


def hide_overlay():
    global overlay_window
    if overlay_window:
        overlay_window.destroy()
        overlay_window = None


model = None


def load_model():
    global model
    if model is None:
        from faster_whisper import WhisperModel
        model = WhisperModel(MODEL_PATH, device="cuda", compute_type="float16")
    return model


def start_recording():
    global recording, rec_proc, active_window, _paused_players
    if recording:
        return
    recording = True
    try:
        active_window = subprocess.check_output(
            ["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        active_window = None
    if os.path.exists(WAVFILE):
        os.unlink(WAVFILE)
    if PAUSE_MEDIA_ENABLED:
        try:
            paused = pause_playing()  # D-Bus 在锁外执行,不阻塞其它线程
        except Exception as pe:
            print(f"[voice-input] pause_media failed: {pe}", file=sys.stderr)
            paused = []
        with _paused_lock:
            # preserve: 新录音继承上一周期尚未 resume 的暂停责任。快速重录时
            # pause_playing 返回 [](媒体已被我们暂停),不清空旧值,留给 stop resume。
            # 去重:外部恢复后又重新暂停的同一播放器不重复累加,免 resume 重复 Play(cc#9)。
            _paused_players = list(dict.fromkeys(_paused_players + paused))
    # 走 PipeWire "default" 后端（默认 source=PD200X），由 PW 重采样共享，
    # 避免与 GNOME 等电平表监听抢占 ALSA 硬件节点(hw:3)导致 EBUSY 静默失败（#6）
    rec_proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-D", "default", WAVFILE],
        stdout=subprocess.DEVNULL, stderr=sys.stderr
    )
    GLib.idle_add(show_overlay, "● REC", "#ff5555")
    print("[voice-input] Recording...", flush=True)


def stop_recording():
    global recording, rec_proc, _paused_players
    if not recording:
        return
    recording = False
    # 局部快照 rec_proc:terminate 目标用局部 rec,避免 clobber 快速重录时 press2
    # 写入的新 arecord(cc#7)。不置 rec_proc=None(同因);全局由下次 start 覆盖。
    rec = rec_proc
    if rec is not None:
        # terminate/wait 包 try/except:arecord 卡死/设备占用抛 TimeoutExpired 不得
        # 跳过后续 resume + 转写(cc#2)。超时则 kill 兜底,仍失败也继续往下。
        try:
            rec.terminate()
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                rec.kill()
                rec.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # kill 已发但进程仍未退出(可能残留占用音频)——区别于 kill 本身失败(cc#8)
                print(f"[voice-input] arecord kill 后仍未退出(可能残留占用音频设备)", file=sys.stderr)
            except Exception as ke:
                print(f"[voice-input] arecord kill failed: {ke}", file=sys.stderr)
        except Exception as e:
            print(f"[voice-input] arecord stop failed: {e}", file=sys.stderr)

    if PAUSE_MEDIA_ENABLED:
        # _paused_lock 保护 _paused_players 列表的快照+清空原子性(cc#1)。recording
        # 是 GIL 下原子布尔,锁内读是 best-effort 快照:新录音已开始则不 resume、把
        # 恢复责任交给新周期(其 start 已 preserve)。resume 的 D-Bus 在锁外执行避免
        # 阻塞;D-Bus 操作排序的残留窗口见 media_pause 文档(kimi#7 接受)。
        with _paused_lock:
            to_resume = _paused_players
            if recording:
                to_resume = []
            else:
                _paused_players = []
        if to_resume:
            try:
                resume(to_resume)
            except Exception as re:
                print(f"[voice-input] resume_media failed: {re}", file=sys.stderr)

    GLib.idle_add(hide_overlay)
    time.sleep(0.3)

    if not os.path.exists(WAVFILE) or os.path.getsize(WAVFILE) < 1000:
        print("[voice-input] recording empty/too short (<1KB); arecord may have failed (EBUSY/device busy?)", file=sys.stderr)
        return

    text = ""  # 预置:转写若抛 BaseException(如 KeyboardInterrupt)不致 finally NameError
    try:
        m = load_model()
        segments, info = m.transcribe(WAVFILE, language="zh")
        text = "".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[voice-input] Error: {e}", file=sys.stderr)
        text = ""
    finally:
        # 归档(独立 try,失败不影响转写/粘贴)
        if ARCHIVE_ENABLED and text and os.path.exists(WAVFILE):
            try:
                archive_recording(WAVFILE, text, text)
            except Exception as ae:
                print(f"[voice-input] archive failed: {ae}", file=sys.stderr)
        if os.path.exists(WAVFILE):  # 异常/归档失败 → wav 还在 → 清理
            os.unlink(WAVFILE)

    if text:
        if active_window:
            subprocess.run(["xdotool", "windowactivate", "--sync", active_window],
                           stderr=subprocess.DEVNULL)
            time.sleep(0.1)
        subprocess.run(["xsel", "--clipboard", "--input"], input=text.encode(),
                       stderr=subprocess.DEVNULL)
        time.sleep(0.05)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"])
        GLib.idle_add(show_overlay, "DONE", "#4fc3f7")
        threading.Timer(1.5, lambda: GLib.idle_add(hide_overlay)).start()
        print(f"[voice-input] Typed: {text}", flush=True)
    else:
        print("[voice-input] No text detected", flush=True)


def on_press(key):
    if key == keyboard.Key.alt_r:
        start_recording()


def on_release(key):
    if key == keyboard.Key.alt_r:
        threading.Thread(target=stop_recording, daemon=True).start()


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print("[voice-input] Preloading model...", flush=True)
    load_model()
    print("[voice-input] Ready! 按住右 Command 键录音，松开转写", flush=True)
    print("[voice-input] Ctrl+C 退出", flush=True)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    listener.stop()


if __name__ == "__main__":
    main()
