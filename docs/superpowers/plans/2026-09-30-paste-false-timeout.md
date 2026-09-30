# #29 Paste False-Timeout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the 30s false paste timeout in `voice_hold.py` (wl-copy's daemonized clipboard server inheriting the stderr pipe) and make busy-dropped key presses observable.

**Architecture:** Three-layer paste seam (`_spawn_paste` → `_paste_and_report` → `_deliver`) so subprocess kwargs and result branches are unit-testable without faking the transcribe pipeline; a module-level pure predicate `is_dropped_press` gates one-line drop logging in `handle_key_value`. No behavior change to the busy-drop semantics themselves.

**Tech Stack:** Python 3 stdlib only; `unittest` (repo convention, no pytest); `unittest.mock` / `contextlib` in tests only.

**Spec:** `docs/superpowers/specs/2026-09-30-paste-false-timeout-design.md` (approved)

**Work from:** `/home/elling/work/git-repo/voice-input/.pi/worktrees/issue-29-paste-false-timeout` (all paths below are relative to this worktree root)

## Global Constraints

- Module top level stays stdlib-only (pinned by existing `TestModulePurity`); `mock`/`contextlib`/`io` are imported **only** in `test_hold.py`.
- Do NOT touch `transition()` semantics, `paste.py`, or anything scoped to #28 (flock) / device-selection.
- `PASTE_TIMEOUT = 10`, module-level; **no env knob** (spec D2).
- Three log messages are pinned verbatim in spec D3/D4/D5 — copy them exactly.
- Test runner is `python3 -m unittest` (README:363 full suite: `test_terms test_archive test_media_pause test_bench test_paste test_hold`).
- `mock.patch` must be used as a context manager (patches the shared `subprocess` module attribute; must restore).
- Test `HoldDaemon` instances must not spawn `arecord` or touch GTK: construct with `env={}` and stub `start_recording` wherever a non-busy press is simulated.
- Commit messages: English, conventional commits.

## Review Focus

1. **TimeoutExpired double-handling** — if `_deliver` kept a `try/except TimeoutExpired` around the new seam it would be dead code; `_spawn_paste` must swallow it and return `(None, True)`. Pinned by `test_timeout_expired_swallowed` (Task 1).
2. **Normal presses must not log** — a non-busy press going through `handle_key_value` produces zero stderr output, else every dictation spams the journal. Pinned by `test_normal_press_not_logged` (Task 2).
3. **Autorepeat must stay silent** — holding Right Alt during the busy window fires dozens of value-2 events per second; only presses (value 1) log. Pinned by `test_repeat_and_release_silent` (Task 2).
4. **`busy` must still always return to False** — the refactor must not disturb `_deliver`'s `finally: self.busy = False` (the #18 round-3 "hotkey dies once" guarantee). Pinned by the full-suite regression incl. existing `TestStateMachine` (Task 3).
5. **Test side-effect leakage** — constructing `HoldDaemon` in tests must not import GTK or record audio; `env={}` + `start_recording` stubs pin this (Tasks 1–2); module purity pinned by existing `TestModulePurity` (Task 3).

---

### Task 1: Paste seam — `_spawn_paste` / `_paste_and_report` / `PASTE_TIMEOUT` (spec D1, D2, D3, D4, D6 → A1, A4, A5)

**Files:**
- Modify: `voice_hold.py` (add `PASTE_TIMEOUT` after the `KEY_RIGHTALT` import block; add two methods to `HoldDaemon`; rewire `_deliver`; comment on the transcribe `check_output`)
- Test: `test_hold.py` (add `TestPasteSpawnContract`; add imports `contextlib`, `io`, `from unittest import mock`)

**Interfaces:**
- Consumes: none (first task).
- Produces: `PASTE_TIMEOUT: int` (module constant, value 10); `HoldDaemon._spawn_paste(self, text: str) -> tuple[int | None, bool]` returning `(returncode, timed_out)`; `HoldDaemon._paste_and_report(self, text: str) -> None`. Task 2 does not consume these.

- [ ] **Step 1: Write the failing tests**

Append to `test_hold.py` (and extend the import block at the top with `import contextlib`, `import io`, `from unittest import mock`):

```python
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
                with contextlib.redirect_stderr(err):
                    d._paste_and_report("hi")
                out = err.getvalue()
                self.assertIn(expect, out)
                if rc != 0:
                    self.assertNotIn("delivered:", out)
                    self.assertEqual(len(out.strip().splitlines()), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && python3 -m unittest test_hold.TestPasteSpawnContract -v`
Expected: FAIL — `AttributeError: 'HoldDaemon' object has no attribute '_spawn_paste'` (and `PASTE_TIMEOUT` missing for the bound test).

- [ ] **Step 3: Write the implementation**

In `voice_hold.py`, add after the `from paste import KEY_RIGHTALT` block (module level):

```python
# Paste delivery timeout (#29): the real paste takes <0.2s (wl-copy sets the
# clipboard, ydotool injects the combo) — 10s is a 50x margin. The old 30s
# only amplified false timeouts; see the spec for the wl-copy daemonization
# mechanism that made PIPE waits hang. No env knob on purpose: the timeout
# is not a user-tunable semantic.
PASTE_TIMEOUT = 10
```

Add two methods to `HoldDaemon` (place before `_transcribe_timeout`):

```python
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
```

In `_deliver`, replace the whole block from `try:` (the `proc = subprocess.run(...)` one) through `print(f"[voice-hold] delivered: {text}", flush=True)` with:

```python
            self._paste_and_report(text)
```

Above the transcribe `check_output` call, add this comment (spec D6, comment-only):

```python
            # Same-class risk (#29): check_output waits for the stdout pipe
            # EOF — if a future engine path ever daemonizes a child that
            # inherits stdout, transcribe will false-timeout exactly like
            # paste did. Today's faster-whisper / OpenVINO paths do not
            # fork; keep it that way or drop the pipe here too.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && python3 -m unittest test_hold.TestPasteSpawnContract -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "fix(hold): spawn paste without stdio pipes, killing the 30s false timeout (#29)"
```

### Task 2: Busy-drop observability — `is_dropped_press` + one-line log (spec D5 → A2)

**Files:**
- Modify: `voice_hold.py` (module-level `is_dropped_press` after `transition()`; `handle_key_value` logging)
- Test: `test_hold.py` (add `TestBusyDropLogging`)

**Interfaces:**
- Consumes: none from Task 1 (independent).
- Produces: `is_dropped_press(value: int, was_busy: bool) -> bool` (module-level pure function).

- [ ] **Step 1: Write the failing tests**

Append to `test_hold.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && python3 -m unittest test_hold.TestBusyDropLogging -v`
Expected: FAIL — `AttributeError: module 'voice_hold' has no attribute 'is_dropped_press'`.

- [ ] **Step 3: Write the implementation**

In `voice_hold.py`, after the `transition()` function (module level):

```python
def is_dropped_press(value: int, was_busy: bool) -> bool:
    """True when a key event will be silently dropped because the pipeline
    is busy (#29 D5): only presses are worth logging — autorepeat fires
    dozens of times per second while the key is held, release carries no
    user intent of its own."""
    return was_busy and value == _PRESS
```

Replace `handle_key_value` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && python3 -m unittest test_hold.TestBusyDropLogging -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): log key presses dropped while busy (#29)"
```

### Task 3: Full-suite regression + module purity (spec A3)

**Files:**
- No code changes expected; this task runs the README:363 full suite and fixes anything the refactor broke.

**Interfaces:**
- Consumes: Tasks 1–2 changes.
- Produces: verification record (paste command + result into the task report).

- [ ] **Step 1: Run the full suite**

Run: `cd <worktree> && python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v`
Expected: all PASS, including existing `TestStateMachine.test_press_while_busy_dropped` (drop semantics unchanged) and `TestModulePurity` (stdlib-only import despite the new code).

- [ ] **Step 2: If anything fails, fix minimally and re-run; commit only if a fix was needed**

```bash
git add -u && git commit -m "fix(hold): regression fixups from full-suite run (#29)"
```
(Skip when everything passes on the first run.)

### Task 4: U1 user acceptance — post-merge manual verification (spec U1; not a code task)

**Files:** none (procedural checklist; executed by the user after merge).

- [ ] **Step 1: Deploy the merged code**

```bash
git -C /home/elling/work/git-repo/voice-input pull   # systemd unit points at the main checkout
systemctl --user restart voice-hold
```

- [ ] **Step 2: Dictate ≥10 times; watch the journal**

```bash
journalctl --user -u voice-hold -f
```
Expected per dictation: one `delivered:` line, **zero** `paste timed out` lines; pressing Right Alt while transcription is running shows `key press dropped while busy (transcribe/paste in flight)`.

- [ ] **Step 3: Record the outcome** on issue #29 (pass / fail + counts). Failure with the new actionable message means a second cause exists → new investigation, per spec risk section.
