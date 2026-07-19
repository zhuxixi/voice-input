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
    global recording, rec_proc, active_window
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
    rec_proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-D", "hw:3", WAVFILE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    GLib.idle_add(show_overlay, "● REC", "#ff5555")
    print("[voice-input] Recording...", flush=True)


def stop_recording():
    global recording, rec_proc
    if not recording:
        return
    recording = False
    if rec_proc:
        rec_proc.terminate()
        rec_proc.wait(timeout=2)
        rec_proc = None

    GLib.idle_add(hide_overlay)
    time.sleep(0.3)

    if not os.path.exists(WAVFILE) or os.path.getsize(WAVFILE) < 1000:
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
