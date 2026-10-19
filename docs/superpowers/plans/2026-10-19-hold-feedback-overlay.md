# Hold Feedback Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give hold-to-talk dictation a visible REC → 转写中… → DONE state machine whose layer-shell overlay stays visible over full-screen windows, without ever blocking recording/transcription.

**Architecture:** A pure `feedback_transition()` maps state+event to a new state+action. The blocking `dev.read_loop()` becomes a 0.2s `select` tick that pumps GTK and applies the state machine; a single FIFO `queue.SimpleQueue` carries `start`/`stop` from the key handler and `ok`/`fail` from the delivery worker. The overlay gets a gtk-layer-shell OVERLAY branch with keyboard and pointer pass-through, falling back to the existing top-level window on any failure.

**Tech Stack:** Python 3 stdlib, python-evdev, PyGObject GTK3, gtk-layer-shell 0.1 (optional at runtime), unittest.

**Spec:** `docs/superpowers/specs/2026-10-19-hold-feedback-overlay-design.md`

## Global Constraints

- Module top level stays stdlib-only (pinned by `test_hold.py:TestModulePurity`); `gi`/`evdev`/`media_pause` remain lazy imports.
- All GTK calls stay on the main thread; `_deliver` never touches GTK.
- Recording/transcription must never depend on the overlay: every overlay failure path is non-fatal.
- Deployment interpreter is `/usr/bin/python3`; `python-cairo` is NOT available — do not import `cairo`.
- Single FIFO: `start`/`stop`/`ok`/`fail` share one `queue.SimpleQueue`.
- `stop` is enqueued before `_deliver`'s worker thread starts.
- DONE is visible 0.4 s (`FEEDBACK_DONE_S`), then hidden.
- Preserve busy-drop semantics of `transition()` and OSError propagation from evdev (systemd restart).

## Review Focus

1. Key events arriving while `busy` must be dropped with no ghost recording and no queue growth (Task 4).
2. GTK missing, non-Wayland compositor, or layer-shell init failure must fall back to a normal window, never break record/transcribe (Task 2).
3. An exception from `overlay.show()/hide()` must disable only the overlay and keep the tick loop alive (Task 4).
4. No speech, short recording, transcribe failure, paste failure/timeout must never show DONE (Task 4).
5. `stop` must be consumed before the same dictation's `ok`, even if the 0.3 s flush sleep is removed later (Task 4).

---

### Task 1: Pure feedback state machine (A1)

**Files:**
- Modify: `voice_hold.py` (add constants + `feedback_transition` near `transition`)
- Test: `test_hold.py` (new `TestFeedbackTransition`)

**Interfaces:**
- Produces: `feedback_transition(state: str, event: str) -> tuple[str, str | None]`; actions `"show_rec" | "show_transcribing" | "show_done" | "hide" | None`; module dict `FEEDBACK_TRANSITIONS`.

- [ ] **Step 1: Write the failing test**

```python
class TestFeedbackTransition(unittest.TestCase):
    """A1: pure state machine — six explicit transitions, everything else unchanged."""

    def test_explicit_transitions(self):
        cases = [
            ("idle", "start", "recording", "show_rec"),
            ("recording", "stop", "transcribing", "show_transcribing"),
            ("transcribing", "ok", "done", "show_done"),
            ("transcribing", "fail", "idle", "hide"),
            ("done", "done_timeout", "idle", "hide"),
            ("done", "start", "recording", "show_rec"),
        ]
        for state, event, want_state, want_action in cases:
            with self.subTest(state=state, event=event):
                self.assertEqual(
                    voice_hold.feedback_transition(state, event),
                    (want_state, want_action))

    def test_everything_else_is_a_noop(self):
        cases = [
            ("idle", "ok"), ("idle", "fail"), ("idle", "done_timeout"),
            ("recording", "start"), ("recording", "ok"), ("recording", "fail"),
            ("transcribing", "start"), ("transcribing", "stop"),
            ("done", "stop"), ("done", "ok"),
            ("idle", "bogus"), ("recording", "bogus"),
        ]
        for state, event in cases:
            with self.subTest(state=state, event=event):
                self.assertEqual(
                    voice_hold.feedback_transition(state, event), (state, None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_hold.TestFeedbackTransition -v`
Expected: FAIL with `AttributeError: module 'voice_hold' has no attribute 'feedback_transition'`

- [ ] **Step 3: Write minimal implementation**

Add after `transition()`:

```python
# Feedback state machine (#24): states idle/recording/transcribing/done.
# Actions drive the overlay from the main-thread tick only.
FEEDBACK_TRANSITIONS = {
    ("idle", "start"): ("recording", "show_rec"),
    ("recording", "stop"): ("transcribing", "show_transcribing"),
    ("transcribing", "ok"): ("done", "show_done"),
    ("transcribing", "fail"): ("idle", "hide"),
    ("done", "done_timeout"): ("idle", "hide"),
    ("done", "start"): ("recording", "show_rec"),
}


def feedback_transition(state: str, event: str):
    """Pure: (state, event) -> (new_state, action or None)."""
    return FEEDBACK_TRANSITIONS.get((state, event), (state, None))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_hold.TestFeedbackTransition -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): add pure feedback state machine (#24)"
```

---

### Task 2: Layer-shell overlay + realize-safe pointer passthrough + README deps (A2, A6)

**Files:**
- Modify: `voice_hold.py` (`_Overlay.__init__`, `_try_init`, new `_setup_layer_shell`, `_apply_ptr_passthrough`, `show`)
- Modify: `README.md`, `README.zh-CN.md` (dependency note)
- Test: `test_hold.py` (new `TestOverlayLayerShell`)

**Interfaces:**
- Consumes: `feedback_transition` is unrelated; no dependency.
- Produces: `_Overlay(gtk_module=None, layer_shell_module=None)`; `_Overlay._layer_shell_active: bool`; `_Overlay._apply_ptr_passthrough()`; constants `OVERLAY_W = 160`, `OVERLAY_H = 48`.

- [ ] **Step 1: Write the failing test**

```python
class _FakeGeom:
    def __init__(self, width=1920, height=1080):
        self.width, self.height = width, height


class _FakeMonitor:
    def get_geometry(self):
        return _FakeGeom()


class _FakeDisplay:
    def get_primary_monitor(self):
        return _FakeMonitor()


class _FakeGdkWindow:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def set_pass_through(self, value):
        self.calls.append(value)
        if self.fail:
            raise RuntimeError("pass-through unsupported")


class _FakeWin:
    def __init__(self):
        self.calls = []
        self.realized = False
        self.gdk = _FakeGdkWindow()
        self.destroyed = False

    def set_decorated(self, v): self.calls.append(("set_decorated", v))
    def set_keep_above(self, v): self.calls.append(("set_keep_above", v))
    def set_skip_taskbar_hint(self, v): self.calls.append(("set_skip_taskbar_hint", v))
    def set_default_size(self, w, h): self.calls.append(("set_default_size", w, h))
    def add(self, w): self.calls.append(("add", w))
    def show_all(self):
        self.calls.append(("show_all",))
        self.realized = True
    def hide(self): self.calls.append(("hide",))
    def destroy(self): self.destroyed = True
    def get_window(self): return self.gdk if self.realized else None
    def get_display(self): return _FakeDisplay()


class _FakeGtk:
    class WindowType:
        TOPLEVEL = 0

    class Window:
        instances = []
        def __new__(cls, **kwargs):
            win = _FakeWin()
            win.kwargs = kwargs
            cls.instances.append(win)
            return win

    class Label:
        def set_markup(self, markup): self.markup = markup

    def __init__(self):
        self.pending = False

    def events_pending(self): return self.pending
    def main_iteration_do(self, blocking): return False


class _FakeLayerShell:
    class Layer:
        OVERLAY = 2

    class KeyboardMode:
        NONE = 0

    class Edge:
        LEFT = 0
        TOP = 1

    def __init__(self, supported=True, fail_init=False):
        self.supported = supported
        self.fail_init = fail_init
        self.calls = []

    def is_supported(self): return self.supported
    def init_for_window(self, win):
        self.calls.append(("init_for_window", win))
        if self.fail_init:
            raise RuntimeError("init failed")
    def set_layer(self, win, layer): self.calls.append(("set_layer", layer))
    def set_keyboard_mode(self, win, mode): self.calls.append(("set_keyboard_mode", mode))
    def set_namespace(self, win, ns): self.calls.append(("set_namespace", ns))
    def set_anchor(self, win, edge, on): self.calls.append(("set_anchor", edge, on))
    def set_margin(self, win, edge, margin): self.calls.append(("set_margin", edge, margin))
    def set_exclusive_zone(self, win, zone): self.calls.append(("set_exclusive_zone", zone))


class TestOverlayLayerShell(unittest.TestCase):
    """A2: layer-shell init params, fallback, realize-safe pointer passthrough."""

    def _overlay(self, ls):
        return voice_hold._Overlay(gtk_module=_FakeGtk, layer_shell_module=ls)

    def test_init_params_and_centering(self):
        ls = _FakeLayerShell()
        overlay = self._overlay(ls)
        overlay._try_init()
        win = _FakeGtk.Window.instances[-1]
        self.assertTrue(overlay._layer_shell_active)
        self.assertEqual(ls.calls[0][0], "init_for_window")
        self.assertIn(("set_layer", 2), ls.calls)
        self.assertIn(("set_keyboard_mode", 0), ls.calls)
        self.assertIn(("set_namespace", "voice-hold-overlay"), ls.calls)
        self.assertIn(("set_anchor", 0, True), ls.calls)
        self.assertIn(("set_anchor", 1, True), ls.calls)
        self.assertIn(("set_margin", 0, (1920 - 160) // 2), ls.calls)
        self.assertIn(("set_margin", 1, (1080 - 48) // 2), ls.calls)
        self.assertIn(("set_exclusive_zone", 0), ls.calls)

    def test_passthrough_runs_after_realize_and_retries(self):
        ls = _FakeLayerShell()
        overlay = self._overlay(ls)
        overlay.show("● REC", "#ff5555")
        win = _FakeGtk.Window.instances[-1]
        self.assertTrue(win.realized)
        self.assertEqual(win.gdk.calls, [True])
        overlay.show("转写中…", "#ffaa00")
        self.assertEqual(win.gdk.calls, [True, True])  # idempotent retry

    def test_unsupported_compositor_falls_back(self):
        ls = _FakeLayerShell(supported=False)
        overlay = self._overlay(ls)
        overlay._try_init()
        self.assertTrue(overlay._inited)
        self.assertFalse(overlay._layer_shell_active)
        self.assertNotIn("init_for_window", [c[0] for c in ls.calls])

    def test_passthrough_failure_keeps_overlay_alive(self):
        ls = _FakeLayerShell()
        overlay = self._overlay(ls)
        overlay._try_init()
        win = _FakeGtk.Window.instances[-1]
        win.gdk.fail = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            overlay.show("● REC", "#ff5555")
            overlay.show("● REC", "#ff5555")
        self.assertFalse(overlay._dead)
        self.assertEqual(err.getvalue().count("pointer passthrough unavailable"), 2)
        self.assertEqual(win.calls.count(("show_all",)), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_hold.TestOverlayLayerShell -v`
Expected: FAIL — `_Overlay` has no `layer_shell_module` kwarg / no `_layer_shell_active`.

- [ ] **Step 3: Implement overlay changes**

In `voice_hold.py` add module constants near `PASTE_TIMEOUT`:

```python
# Overlay geometry (#24): fixed 160x48, centered on the primary monitor.
OVERLAY_W, OVERLAY_H = 160, 48
LAYER_SHELL_NAMESPACE = "voice-hold-overlay"
```

Replace `_Overlay.__init__` and `_try_init` with:

```python
    def __init__(self, gtk_module=None, layer_shell_module=None):
        # 惰性初始化(Zima CR round-3 发现 3 + systemd 排序):GTK 延后到首次
        # show()——daemon 在 graphical-session 环境就绪前启动时,监听照常工作,
        # 浮层等会话环境到位后的第一次录音再初始化;初始化失败每次录音重试
        self._inited = False
        self._dead = False
        self._win = None
        self._label = None
        self._Gtk = None
        self._gtk_module = gtk_module
        self._layer_shell_module = layer_shell_module
        self._layer_shell_active = False

    def _try_init(self):
        if self._inited or self._dead:
            return
        try:
            if self._gtk_module is None:
                import gi
                gi.require_version("Gtk", "3.0")
                from gi.repository import Gtk as gtk_module
            else:
                gtk_module = self._gtk_module
            self._Gtk = gtk_module
            win = gtk_module.Window(type=gtk_module.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_keep_above(True)
            win.set_skip_taskbar_hint(True)
            win.set_default_size(OVERLAY_W, OVERLAY_H)
            self._label = gtk_module.Label()
            win.add(self._label)
            self._win = win
            self._setup_layer_shell(win)
            self._inited = True
        except Exception as e:
            print(f"[voice-hold] overlay unavailable (retry next recording): {e}",
                  file=sys.stderr)

    def _setup_layer_shell(self, win):
        """OVERLAY layer + keyboard/pointer pass-through; never fatal."""
        try:
            if self._layer_shell_module is None:
                import gi
                gi.require_version("GtkLayerShell", "0.1")
                from gi.repository import GtkLayerShell as ls
            else:
                ls = self._layer_shell_module
            if not ls.is_supported():
                raise RuntimeError("layer-shell not supported by this compositor")
            ls.init_for_window(win)
            ls.set_layer(win, ls.Layer.OVERLAY)
            ls.set_keyboard_mode(win, ls.KeyboardMode.NONE)
            ls.set_namespace(win, LAYER_SHELL_NAMESPACE)
            display = win.get_display()
            if display is None:
                from gi.repository import Gdk
                display = Gdk.Display.get_default()
            geom = display.get_primary_monitor().get_geometry()
            ls.set_anchor(win, ls.Edge.LEFT, True)
            ls.set_anchor(win, ls.Edge.TOP, True)
            ls.set_margin(win, ls.Edge.LEFT, (geom.width - OVERLAY_W) // 2)
            ls.set_margin(win, ls.Edge.TOP, (geom.height - OVERLAY_H) // 2)
            ls.set_exclusive_zone(win, 0)
            self._layer_shell_active = True
        except Exception:
            self._layer_shell_active = False
```

Add the passthrough helper:

```python
    def _apply_ptr_passthrough(self):
        """Pass clicks through to the window below; needs a realized window."""
        if not self._layer_shell_active:
            return
        gdk_win = self._win.get_window()
        if gdk_win is None:          # not realized yet: retried on the next show()
            return
        try:
            gdk_win.set_pass_through(True)   # GDK 3.24.52 typelib
        except Exception as e:       # lose passthrough only; keep the overlay alive
            print(f"[voice-hold] pointer passthrough unavailable: {e}",
                  file=sys.stderr)
```

In `show()`, insert after `self._win.show_all()`:

```python
            self._win.show_all()
            self._apply_ptr_passthrough()
            self._pump()
```

- [ ] **Step 4: Add the README dependency notes**

`README.md` (English, in the requirements/dependency section) and `README.zh-CN.md` (same section, Chinese) each get one bullet:

```markdown
- `gtk-layer-shell` (Arch `extra`; 0.10.1 tested KDE Plasma 6.7.5) — keeps the
  hold overlay visible over full-screen windows. Missing package or non-Wayland
  session silently falls back to the plain top-level window.
```

Chinese version:

```markdown
- `gtk-layer-shell`（Arch `extra`，0.10.1 在 KDE Plasma 6.7.5 实测）——让 hold
  浮层盖住全屏窗口。未安装或非 Wayland 会话会静默回退普通窗口。
```

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest test_hold.TestOverlayLayerShell test_hold.TestOverlayLazyInit -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add voice_hold.py test_hold.py README.md README.zh-CN.md
git commit -m "feat(hold): layer-shell overlay with pointer pass-through (#24)"
```

---

### Task 3: `iter_events` select seam (A3)

**Files:**
- Modify: `voice_hold.py` (add `import select`, `TICK_S`, `iter_events`)
- Test: `test_hold.py` (new `TestIterEvents`)

**Interfaces:**
- Produces: `iter_events(dev, timeout, select_fn) -> list`; `TICK_S = 0.2`. The run-loop wiring that consumes this seam lands in Task 4 together with `_tick`.

- [ ] **Step 1: Write the failing test**

```python
class TestIterEvents(unittest.TestCase):
    """A3: select seam — timeout, ready drain, OSError passthrough."""

    class _Dev:
        fd = 9
        def __init__(self, result):
            self.result = result
            self.reads = 0
        def read(self):
            self.reads += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def test_timeout_returns_empty(self):
        dev = self._Dev([object()])
        self.assertEqual(
            voice_hold.iter_events(dev, 0.2, lambda r, w, x, t: ([], [], [])), [])
        self.assertEqual(dev.reads, 0)

    def test_ready_returns_all_pending(self):
        events = [object(), object()]
        dev = self._Dev(events)
        seen = {}
        def fake_select(r, w, x, t):
            seen["args"] = (r, t)
            return (r, [], [])
        self.assertEqual(voice_hold.iter_events(dev, 0.2, fake_select), events)
        self.assertEqual(seen["args"], ([9], 0.2))

    def test_blocking_io_returns_empty(self):
        dev = self._Dev(BlockingIOError())
        self.assertEqual(
            voice_hold.iter_events(dev, 0.2, lambda r, w, x, t: (r, [], [])), [])

    def test_oserror_propagates(self):
        dev = self._Dev(OSError("unplugged"))
        with self.assertRaises(OSError):
            voice_hold.iter_events(dev, 0.2, lambda r, w, x, t: (r, [], []))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_hold.TestIterEvents -v`
Expected: FAIL with `AttributeError: module 'voice_hold' has no attribute 'iter_events'`

- [ ] **Step 3: Implement `iter_events`**

Add `import select` to the top-level stdlib imports, then after `is_dropped_press`:

```python
TICK_S = 0.2   # main-loop select timeout; upper bound on feedback latency


def iter_events(dev, timeout, select_fn):
    """Non-blocking drain seam (#24): one select + one read per ready tick.

    BlockingIOError (spurious wake / drained fd) yields []; OSError (device
    unplug) propagates so systemd restarts the daemon, unchanged semantics.
    """
    ready, _, _ = select_fn([dev.fd], [], [], timeout)
    if not ready:
        return []
    try:
        return list(dev.read())
    except BlockingIOError:
        return []
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest test_hold.TestIterEvents test_hold.TestModulePurity -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): select-based event tick seam (#24)"
```

---

### Task 4: Delivery channel, tick wiring, key dispatch (A4)

**Files:**
- Modify: `voice_hold.py` (`__init__`, `handle_key_value`, `start_recording`, `stop_recording`, `_deliver`, `_paste_and_report`, replace `_tick`)
- Test: `test_hold.py` (new `TestDeliverTick`)

**Interfaces:**
- Consumes: `feedback_transition` (Task 1), `_apply_ptr_passthrough` (Task 2), `iter_events`/`TICK_S` (Task 3).
- Produces: `HoldDaemon._ui_queue: queue.SimpleQueue`; `HoldDaemon.feedback_state: str`; `_apply_feedback(action)`; `_tick()`; `_paste_and_report(text) -> bool`; constants `FEEDBACK_DONE_S = 0.4`, `FEEDBACK_TEXTS`; the wired `run()` loop.

- [ ] **Step 1: Write the failing test**

```python
class TestDeliverTick(unittest.TestCase):
    """A4: single FIFO, paste result mapping, tick application, key dispatch."""

    def _daemon(self):
        return voice_hold.HoldDaemon(env={})

    def test_paste_and_report_returns_bool(self):
        d = self._daemon()
        d._spawn_paste = lambda text: (0, False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(d._paste_and_report("hi"))
        d._spawn_paste = lambda text: (3, False)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(d._paste_and_report("hi"))
        d._spawn_paste = lambda text: (None, True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(d._paste_and_report("hi"))

    def test_deliver_posts_ok_only_after_successful_paste(self):
        d = self._daemon()
        with tempfile.TemporaryDirectory() as tmp:
            wav = voice_hold.new_recording_path(tmp)
            with open(wav, "wb") as f:
                f.write(b"\0" * 2048)
            d.wav_path = wav
            d._transcribe_legacy = lambda path: "hello"
            d.resident = False
            with mock.patch.object(d, "_paste_and_report", return_value=True):
                d._deliver()
        self.assertEqual(d._ui_queue.get_nowait(), "ok")
        self.assertFalse(d.busy)

    def test_deliver_posts_fail_when_paste_fails(self):
        d = self._daemon()
        with tempfile.TemporaryDirectory() as tmp:
            wav = voice_hold.new_recording_path(tmp)
            with open(wav, "wb") as f:
                f.write(b"\0" * 2048)
            d.wav_path = wav
            d._transcribe_legacy = lambda path: "hello"
            d.resident = False
            with mock.patch.object(d, "_paste_and_report", return_value=False):
                d._deliver()
        self.assertEqual(d._ui_queue.get_nowait(), "fail")
        self.assertFalse(d.busy)

    def test_deliver_posts_fail_for_no_speech_and_short_recording(self):
        d = self._daemon()
        with tempfile.TemporaryDirectory() as tmp:
            short = voice_hold.new_recording_path(tmp)
            d.wav_path = short
            with contextlib.redirect_stderr(io.StringIO()):
                d._deliver()
            self.assertEqual(d._ui_queue.get_nowait(), "fail")
            wav = voice_hold.new_recording_path(tmp)
            with open(wav, "wb") as f:
                f.write(b"\0" * 2048)
            d.wav_path = wav
            d._transcribe_legacy = lambda path: ""
            d.resident = False
            with contextlib.redirect_stdout(io.StringIO()):
                d._deliver()
            self.assertEqual(d._ui_queue.get_nowait(), "fail")

    def test_tick_applies_queue_and_done_timeout(self):
        d = self._daemon()
        d.overlay = mock.Mock()
        d._ui_queue.put("start")
        d._tick()
        self.assertEqual(d.feedback_state, "recording")
        d.overlay.show.assert_called_with("● REC", "#ff5555")
        d._ui_queue.put("stop")
        d._tick()
        self.assertEqual(d.feedback_state, "transcribing")
        d.overlay.show.assert_called_with("转写中…", "#ffaa00")
        d._ui_queue.put("ok")
        d._tick()
        self.assertEqual(d.feedback_state, "done")
        d.overlay.show.assert_called_with("DONE", "#55ff55")
        d._done_deadline = time.monotonic() - 1
        d._tick()
        self.assertEqual(d.feedback_state, "idle")
        d.overlay.hide.assert_called()

    def test_tick_survives_overlay_exception(self):
        d = self._daemon()
        d.overlay = mock.Mock()
        d.overlay.show.side_effect = RuntimeError("dead GTK")
        d._ui_queue.put("start")
        d._tick()  # must not raise
        d.overlay._disable.assert_called_once()
        self.assertEqual(d.feedback_state, "recording")

    def test_handle_key_value_enqueues_before_starting_worker(self):
        d = self._daemon()
        d.overlay = mock.Mock()
        with mock.patch.object(d, "start_recording") as start:
            d.handle_key_value(1)
            start.assert_called_once()
        self.assertEqual(d._ui_queue.get_nowait(), "start")
        with mock.patch.object(d, "stop_recording"):
            d.handle_key_value(0)
        self.assertEqual(d._ui_queue.get_nowait(), "stop")

    def test_stop_enqueues_stop_before_worker_thread_starts(self):
        d = self._daemon()
        d.recording = True
        seen = []
        class FakeThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon
            def start(self):
                seen.append(d._ui_queue.get_nowait())
        with mock.patch.object(voice_hold.threading, "Thread", FakeThread):
            d.handle_key_value(0)
        self.assertEqual(seen, ["stop"])

    def test_stop_recording_never_touches_overlay(self):
        d = self._daemon()
        d.overlay = mock.Mock()
        with mock.patch.object(voice_hold.threading, "Thread"):
            d.stop_recording()
        self.assertEqual(d.overlay.mock_calls, [])

    def test_busy_key_is_dropped_without_events(self):
        d = self._daemon()
        d.busy = True
        with mock.patch.object(d, "start_recording") as start:
            d.handle_key_value(1)
            d.handle_key_value(0)
            start.assert_not_called()
        with self.assertRaises(queue.Empty):
            d._ui_queue.get_nowait()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_hold.TestDeliverTick -v`
Expected: FAIL — `_ui_queue` attribute missing.

- [ ] **Step 3: Implement the channel and wiring**

Add `import queue` to the top-level stdlib imports. Add constants near `PASTE_TIMEOUT`:

```python
FEEDBACK_DONE_S = 0.4   # DONE visibility before auto-hide
FEEDBACK_TEXTS = {
    "show_rec": ("● REC", "#ff5555"),
    "show_transcribing": ("转写中…", "#ffaa00"),
    "show_done": ("DONE", "#55ff55"),
}
```

In `HoldDaemon.__init__` add:

```python
        self.feedback_state = "idle"
        self._ui_queue = queue.SimpleQueue()
        self._done_deadline = None
```

Replace the `dev.read_loop()` block in `run()` with:

```python
            while True:
                for event in iter_events(dev, TICK_S, select.select):
                    if event.type == EV_KEY and event.code == KEY_RIGHTALT:
                        self.handle_key_value(event.value)
                self._tick()
```

Replace `handle_key_value` action dispatch:

```python
        if action == "start":
            self._ui_queue.put("start")
            self.start_recording()
        elif action == "stop":
            self._ui_queue.put("stop")   # before stop_recording() starts _deliver
            self.stop_recording()
```

Remove `self.overlay.show("● REC", "#ff5555")` from `start_recording()` and
`self.overlay.hide()` from `stop_recording()`.

Change `_paste_and_report` to return bool (`return False` on timeout/rc!=0, `return True` after the `delivered:` print). Keep all log lines unchanged.

Wrap `_deliver` so every exit posts one event:

```python
    def _deliver(self):
        ok = False
        try:
            ...  # existing body unchanged except the final call:
            ok = self._paste_and_report(text)
        finally:
            self._ui_queue.put("ok" if ok else "fail")
            self.busy = False
```

Replace the Task-3 `_tick` placeholder with:

```python
    def _apply_feedback(self, action):
        if action == "hide":
            try:
                self.overlay.hide()
            except Exception as e:
                self.overlay._disable("tick", e)
            return
        text, color = FEEDBACK_TEXTS[action]
        try:
            self.overlay.show(text, color)
        except Exception as e:
            self.overlay._disable("tick", e)

    def _tick(self):
        while True:
            try:
                event = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            new_state, action = feedback_transition(self.feedback_state, event)
            self.feedback_state = new_state
            self._done_deadline = (
                time.monotonic() + FEEDBACK_DONE_S if new_state == "done" else None)
            if action:
                self._apply_feedback(action)
        if (self.feedback_state == "done" and self._done_deadline is not None
                and time.monotonic() >= self._done_deadline):
            self.feedback_state, action = feedback_transition("done", "done_timeout")
            self._done_deadline = None
            if action:
                self._apply_feedback(action)
```

- [ ] **Step 4: Run the new and existing hold tests**

Run: `python3 -m unittest test_hold.TestDeliverTick test_hold.TestPasteSpawnContract test_hold.TestBusyDropLogging -v`
Expected: PASS. Fix any existing assertion that assumed `_paste_and_report` returns `None` (there is none: #29 tests only inspect logs).

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): single-FIFO feedback wiring and paste result mapping (#24)"
```

---

### Task 5: Full regression + acceptance matrix audit (A5, A6)

**Files:**
- Modify: none unless a regression fails (fix in place, same commit).
- Test: existing suite.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: recorded A5/A6 evidence in the ledger.

- [ ] **Step 1: Run the README-pinned full suite (A5)**

Run: `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v`
Expected: PASS, 0 failures/errors.

- [ ] **Step 2: Run the A6 static checks**

Run: `rg -q gtk-layer-shell README.md && rg -q gtk-layer-shell README.zh-CN.md && echo A6-OK`
Expected: prints `A6-OK`.

- [ ] **Step 3: Walk the acceptance matrix**

For each of A1–A6, record in the ledger: command run + result. For U1–U3, confirm they are listed in Task 6 (not yet run) and do not mark them complete.

- [ ] **Step 4: Commit only if a fix was needed**

```bash
git add -u
git commit -m "fix(hold): regression fixes from full suite (#24)"
```

---

### Task 6: Manual verification on the real device (U1, U2, U3)

**Files:** none (user-executed).

- [ ] **Step 1: Deploy the PR branch to the main checkout**

```bash
git -C ~/work/git-repo/voice-input fetch origin
git -C ~/work/git-repo/voice-input checkout issue-24-hold-feedback-overlay
systemctl --user restart voice-hold
journalctl --user -u voice-hold -n 20
```

- [ ] **Step 2: U1 — full-screen chain**

In a full-screen wezterm, hold Right Alt and speak, then release.
Expected: centered `● REC` visible → `转写中…` → text pasted → green `DONE` for ~0.4 s → hidden; no lost keys.

- [ ] **Step 3: U2 — no focus steal, no click swallow**

While the overlay is visible, type in the terminal, then click where the overlay sits.
Expected: typed characters arrive, no focus jump; the click reaches the window below.

- [ ] **Step 4: U3 — fallback (optional, may be post-merge)**

With the PR branch still checked out: `sudo pacman -R gtk-layer-shell`, `systemctl --user restart voice-hold`, verify record/transcribe still work; then `sudo pacman -S gtk-layer-shell` and restart, verify the overlay covers full screen again.

- [ ] **Step 5: Return the checkout to main**

```bash
git -C ~/work/git-repo/voice-input checkout main
```

- [ ] **Step 6: Record results**

Append U1/U2/U3 observations to the ledger and the issue comment. A `pending` U item must be reported as pending, never as passed.
