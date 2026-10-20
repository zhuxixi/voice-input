# Silence VAD Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trim leading/trailing and long internal silence before transcription so Whisper never hallucinates on silence or amplifies repeated tokens, on all three transcription entry points.

**Architecture:** New stdlib-only module `vad_trim.py` wraps faster-whisper's bundled Silero VAD and yields either a trimmed temp wav, the original path (passthrough/degrade), or `None` (no speech). The three entry points wrap `model.transcribe()` in `with vad_trim.trimmed_wav(wav) as src:`; engine adapters stay untouched.

**Tech Stack:** Python 3.12, stdlib (`wave`, `tempfile`, `contextlib`), numpy + faster-whisper (already in venv; lazily imported), Silero VAD model shipped inside faster-whisper 1.2.1.

**Spec:** `docs/superpowers/specs/2026-10-20-vad-silence-trim-design.md` (issue #32)

## Global Constraints

- `VOICE_INPUT_VAD=0` disables trimming completely (spec D6).
- Never raise out of `trimmed_wav` for VAD/format/IO problems: log and fall back to the original audio (spec D6).
- No speech (`spans == []`) yields `None`; callers print `vad: no speech` and return empty text without calling the model (spec D5/D7).
- ≥99% kept → return the original path, no temp file (spec D2).
- Engine adapters (`engine.py`) and hold recording/overlay code (`voice_hold.py`) are not modified (spec D4/D8).
- Every new test in `test_vad_trim.py` must run under the **system** python3 except those guarded by `skipUnless` (A6b, A10).
- Use Python's `unittest` (repo convention), test classes prefixed `Test`.

## Review Focus

- **Caller exception inside the context**: an exception raised in the `with` body must propagate; the context manager must not swallow it while yielding a fallback path. Covered by `TestTrimmedWav.test_caller_exception_propagates` (A3).
- **Silence-only recording**: must return empty text, no model call, no hallucination. Covered by A4 (module) and A7 (worker wiring).
- **Non-16 kHz / non-mono / non-16-bit wav**: must fall back to the original file instead of crashing. Covered by A6.
- **Missing VAD dependencies / VAD runtime error**: must fall back to original audio and still transcribe. Covered by A5.
- **A short but real word**: must not be trimmed away. `min_speech_duration_ms=0` + 400 ms padding; covered by U1 (manual).

---

### Task 1: `vad_trim.py` core + module tests (A1–A6b)

**Files:**
- Create: `vad_trim.py`
- Create: `test_vad_trim.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `DEFAULT_PAD_MS: int = 400`
  - `ENV_VAD: str = "VOICE_INPUT_VAD"`
  - `class TrimUnavailable(Exception)`
  - `span_bounds(spans, total_samples) -> list[tuple[int, int]]`
  - `speech_spans(frames: bytes, sample_rate: int = 16000, vad_options=None) -> list[tuple[int, int]]`
  - `spans_from_wav(path: str) -> tuple[bytes, list[tuple[int, int]]]`
  - `trimmed_wav(path, *, detector=None, enabled=None, log=None)` context manager yielding `str | None`

- [ ] **Step 1: Write the failing module tests**

Create `test_vad_trim.py`:

```python
"""A1-A6b (#32): silence trimming for transcription."""

import os
import shutil
import struct
import tempfile
import unittest
import wave
from unittest import mock

import vad_trim

SR = 16000


def make_wav(path, seconds=1.0, sample_rate=SR, channels=1, width=2):
    frames = struct.pack("<h", 1200) * int(seconds * sample_rate) * channels
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(sample_rate)
        w.writeframes(frames)


def read_frames(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


def deps_available():
    try:
        import numpy  # noqa: F401
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


HAS_VAD = deps_available()


class TestSpanBounds(unittest.TestCase):
    def test_clamps_to_range(self):
        self.assertEqual(vad_trim.span_bounds([(-5, 10), (100, 50)], 40), [(0, 10)])

    def test_merges_overlaps_and_touching(self):
        self.assertEqual(vad_trim.span_bounds([(0, 10), (10, 20)], 40), [(0, 20)])
        self.assertEqual(vad_trim.span_bounds([(0, 10), (5, 20), (30, 35)], 40),
                         [(0, 20), (30, 35)])

    def test_drops_empty_and_keeps_order(self):
        self.assertEqual(vad_trim.span_bounds([(30, 40), (5, 5), (0, 10)], 40),
                         [(0, 10), (30, 40)])


class TestTrimmedWav(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vad-trim-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.wav = os.path.join(self.dir, "in.wav")
        make_wav(self.wav, seconds=1.0)
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def detector(self, spans):
        def _detect(path):
            return read_frames(path), spans
        return _detect

    def test_disabled_passthrough(self):
        def explode(_path):
            raise AssertionError("detector must not run when disabled")
        with vad_trim.trimmed_wav(self.wav, detector=explode, enabled=False,
                                  log=self.log) as src:
            self.assertEqual(src, self.wav)

    def test_env_disable_passthrough(self):
        with mock.patch.dict(os.environ, {vad_trim.ENV_VAD: "0"}):
            with vad_trim.trimmed_wav(self.wav, log=self.log) as src:
                self.assertEqual(src, self.wav)

    def test_trim_creates_and_removes_temp(self):
        before = read_frames(self.wav)
        spans = [(4000, 12000)]
        with vad_trim.trimmed_wav(self.wav, detector=self.detector(spans),
                                  log=self.log) as src:
            self.assertNotEqual(src, self.wav)
            self.assertTrue(os.path.exists(src))
            with wave.open(src, "rb") as w:
                self.assertEqual(w.getnframes(), 8000)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(read_frames(self.wav), before)

    def test_caller_exception_propagates(self):
        spans = [(4000, 12000)]
        with self.assertRaises(ValueError):
            with vad_trim.trimmed_wav(self.wav, detector=self.detector(spans),
                                      log=self.log) as src:
                raise ValueError("caller boom")

    def test_near_full_length_passthrough(self):
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 16000)]),
                                  log=self.log) as src:
            self.assertEqual(src, self.wav)
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 15000)]),
                                  log=self.log) as src:
            self.assertNotEqual(src, self.wav)

    def test_no_speech_yields_none(self):
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([]),
                                  log=self.log) as src:
            self.assertIsNone(src)
        self.assertEqual([p for p in os.listdir(self.dir) if p.startswith("voice-input-trim-")], [])

    def test_detector_error_falls_back(self):
        def unavailable(_path):
            raise vad_trim.TrimUnavailable("no onnxruntime")
        with vad_trim.trimmed_wav(self.wav, detector=unavailable, log=self.log) as src:
            self.assertEqual(src, self.wav)
        self.assertIn("vad: unavailable", "\n".join(self.logs))

        def broken(_path):
            raise RuntimeError("boom")
        with vad_trim.trimmed_wav(self.wav, detector=broken, log=self.log) as src:
            self.assertEqual(src, self.wav)
        self.assertIn("vad: error", "\n".join(self.logs))

    def test_rejects_non_16k(self):
        path8k = os.path.join(self.dir, "in8k.wav")
        make_wav(path8k, seconds=1.0, sample_rate=8000)
        with vad_trim.trimmed_wav(path8k, log=self.log) as src:
            self.assertEqual(src, path8k)
        self.assertIn("vad: unavailable", "\n".join(self.logs))


@unittest.skipUnless(HAS_VAD, "numpy/faster_whisper not importable")
class TestSpeechSpans(unittest.TestCase):
    def test_converts_float32_and_samples(self):
        import numpy as np
        frames = struct.pack("<h", 1000) * 32000
        with mock.patch("faster_whisper.vad.get_speech_timestamps",
                        return_value=[{"start": 100, "end": 2000}]) as fake:
            spans = vad_trim.speech_spans(frames, SR)
        self.assertEqual(spans, [(100, 2000)])
        audio = fake.call_args.args[0]
        options = fake.call_args.args[1]
        self.assertEqual(audio.dtype, np.float32)
        self.assertAlmostEqual(float(audio[0]), 1000 / 32768.0, places=6)
        self.assertEqual(fake.call_args.kwargs["sampling_rate"], SR)
        self.assertEqual(options.speech_pad_ms, vad_trim.DEFAULT_PAD_MS)
        self.assertEqual(options.min_speech_duration_ms, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/elling/work/git-repo/voice-input/.pi/worktrees/issue-32-vad-silence-trim && python3 -m unittest test_vad_trim -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vad_trim'`.

- [ ] **Step 3: Write `vad_trim.py`**

```python
"""Silence trimming for transcription (#32).

Whisper hallucinates on silence and amplifies repeated tokens across long
silences; feeding it only VAD-detected speech fixes both. Silero VAD ships with
faster-whisper (assets/silero_vad_v6.onnx), so this adds no dependency; numpy and
faster_whisper are imported lazily so the rest of the module stays stdlib-only.
"""

import contextlib
import os
import sys
import tempfile
import wave

DEFAULT_PAD_MS = 400
ENV_VAD = "VOICE_INPUT_VAD"
SAMPLE_RATE = 16000


class TrimUnavailable(Exception):
    """VAD cannot run on this input (unsupported format / missing runtime)."""


def span_bounds(spans, total_samples):
    """Clamp spans into [0, total_samples], merge overlaps, drop empties."""
    total = max(0, int(total_samples))
    bounds = []
    for start, end in spans:
        begin = max(0, min(int(start), total))
        stop = max(0, min(int(end), total))
        if stop <= begin:
            continue
        if bounds and begin <= bounds[-1][1]:
            bounds[-1] = (bounds[-1][0], max(bounds[-1][1], stop))
        else:
            bounds.append((begin, stop))
    return bounds


def speech_spans(frames, sample_rate=SAMPLE_RATE, vad_options=None):
    """Silero VAD spans (sample indices) for 16-bit mono PCM frames."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    options = vad_options or VadOptions(speech_pad_ms=DEFAULT_PAD_MS,
                                        min_speech_duration_ms=0)
    timestamps = get_speech_timestamps(samples, options, sampling_rate=sample_rate)
    return [(int(ts["start"]), int(ts["end"])) for ts in timestamps]


def spans_from_wav(path):
    """Return (pcm_frames, spans) for a 16 kHz mono 16-bit wav."""
    with wave.open(path, "rb") as wav:
        if (wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1
                or wav.getsampwidth() != 2):
            raise TrimUnavailable(
                "expected 16000Hz mono 16-bit, got "
                f"{wav.getframerate()}Hz {wav.getnchannels()}ch "
                f"{wav.getsampwidth() * 8}-bit")
        frames = wav.readframes(wav.getnframes())
    return frames, speech_spans(frames, SAMPLE_RATE)


def _write_trimmed(frames, bounds, sample_rate):
    fd, tmp_path = tempfile.mkstemp(prefix="voice-input-trim-", suffix=".wav")
    try:
        os.close(fd)
        with wave.open(tmp_path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(sample_rate)
            out.writeframes(b"".join(frames[start * 2:stop * 2]
                                     for start, stop in bounds))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


@contextlib.contextmanager
def trimmed_wav(path, *, detector=None, enabled=None, log=None):
    """Yield a speech-only wav path, the original path, or None (no speech).

    Degrades to the original path on any VAD/format/IO problem; never raises.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if enabled is None:
        enabled = os.environ.get(ENV_VAD, "1").strip().lower() not in ("0", "false", "no")
    if not enabled:
        yield path
        return

    detector = detector or spans_from_wav
    try:
        frames, spans = detector(path)
        with wave.open(path, "rb") as wav:
            sample_rate = wav.getframerate()
    except TrimUnavailable as exc:
        log(f"[voice-input] vad: unavailable ({exc}), using original audio")
        yield path
        return
    except Exception as exc:
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    total = len(frames) // 2
    bounds = span_bounds(spans, total)
    if not bounds:
        yield None
        return
    kept = sum(stop - start for start, stop in bounds)
    if kept >= total * 0.99:
        yield path
        return

    try:
        tmp_path = _write_trimmed(frames, bounds, sample_rate)
    except Exception as exc:
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    log(f"[voice-input] vad: {total / sample_rate:.2f}s -> "
        f"{kept / sample_rate:.2f}s ({len(bounds)} span)")
    try:
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_vad_trim -v`
Expected: PASS, `TestSpeechSpans` skipped under system python (no numpy).
Then run under venv: `venv/bin/python3 -m unittest test_vad_trim -v`
Expected: PASS including `TestSpeechSpans`.

- [ ] **Step 5: Commit**

```bash
git add vad_trim.py test_vad_trim.py
git commit -m "feat(vad): silence trim module for transcription (#32)"
```

---

### Task 2: Worker wiring (A7, A8)

**Files:**
- Modify: `transcribe_worker.py` (import block near line 21; request handling at lines 96–112)
- Modify: `test_transcribe_worker.py` (imports; `WorkerHarness.start`; new `TestVadWiring`)

**Interfaces:**
- Consumes: `vad_trim.trimmed_wav(path) -> context manager yielding str | None`.
- Produces: worker returns `{"id": N, "text": ""}` when `trimmed_wav` yields `None`; otherwise `FakeModel.transcribe` receives the yielded path. `worker_main(..., log=None)` now accepts a log callable (already existed; tests now pass it).

- [ ] **Step 1: Write the failing worker tests**

Append to `test_transcribe_worker.py` (and add `import contextlib` next to the other imports):

```python
class TestVadWiring(WorkerHarness):
    def _patch_trim(self, value):
        @contextlib.contextmanager
        def fake(path, **kwargs):
            yield value
        return mock.patch.object(tw.vad_trim, "trimmed_wav", fake)

    def test_no_speech_returns_empty_text(self):
        logs = []
        self.start(log=logs.append)
        self.read()
        with self._patch_trim(None):
            self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
            self.parent.flush()
            self.assertEqual(self.read(), {"id": 1, "text": ""})
        self.assertEqual(self.model.calls, [])
        self.assertIn("vad: no speech", "\n".join(logs))

    def test_trimmed_path_reaches_model(self):
        self.start()
        self.read()
        with self._patch_trim("/tmp/trimmed.wav"):
            self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
            self.parent.flush()
            self.assertEqual(self.read(), {"id": 1, "text": "你好"})
        self.assertEqual(self.model.calls[0]["wav"], "/tmp/trimmed.wav")
```

Change `WorkerHarness.start` to accept and forward a log callable:

```python
    def start(self, log=None):
        def run():
            self.done = tw.worker_main(self.child_file, model_factory=lambda: self.model,
                                       terms_loader=lambda path=None: tw.load_terms(
                                           self.terms_path),
                                       log=log)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5.0)
        return t
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m unittest test_transcribe_worker.TestVadWiring -v`
Expected: FAIL with `AttributeError: module 'transcribe_worker' has no attribute 'vad_trim'`.

- [ ] **Step 3: Wire `vad_trim` into the worker**

In `transcribe_worker.py`, next to `from terms import ...`, add:

```python
import vad_trim
```

Replace the transcription block (`segments, _info = model.transcribe(wav, ...)` through `text = ...`) with:

```python
            with vad_trim.trimmed_wav(wav) as src:
                if src is None:
                    log("[voice-input] vad: no speech, empty transcript")
                    text = ""
                else:
                    segments, _info = model.transcribe(
                        src, language="zh", initial_prompt=prompt, **extra_kw)
                    text = "".join(s.text for s in segments).strip()
            conn.write(encode_text(rid, text))
```

- [ ] **Step 4: Run the worker tests**

Run: `python3 -m unittest test_transcribe_worker -v`
Expected: PASS (all existing cases plus the two new ones).

- [ ] **Step 5: Commit**

```bash
git add transcribe_worker.py test_transcribe_worker.py
git commit -m "feat(worker): trim silence before transcription (#32)"
```

---

### Task 3: `transcribe_once.py` + `voice-ptt.py` wiring (A9)

**Files:**
- Modify: `transcribe_once.py` (imports near line 45; transcribe block at lines 90–96)
- Modify: `voice-ptt.py` (imports near line 53; transcribe block at lines 249–258)
- Modify: `test_vad_trim.py` (new `TestWiring`)

**Interfaces:**
- Consumes: `vad_trim.trimmed_wav`.
- Produces: both CLIs print `[voice-input] vad: no speech, empty transcript` to stderr and emit empty text (`transcribe_once` prints an empty stdout line) when `src is None`.

- [ ] **Step 1: Write the failing static wiring test**

Append to `test_vad_trim.py`:

```python
class TestWiring(unittest.TestCase):
    def test_cli_call_sites_use_trimmed_wav(self):
        root = os.path.dirname(os.path.abspath(__file__))
        for name in ("transcribe_once.py", "voice-ptt.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn("vad_trim.trimmed_wav(", source, name)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest test_vad_trim.TestWiring -v`
Expected: FAIL with `AssertionError: 'vad_trim.trimmed_wav(' not found in '...' : transcribe_once.py`.

- [ ] **Step 3: Wire `transcribe_once.py`**

Add `import vad_trim` next to `from terms import ...`, then replace the transcribe/join/print block with:

```python
    with vad_trim.trimmed_wav(wav) as src:
        if src is None:
            print("[voice-input] vad: no speech, empty transcript", file=sys.stderr)
            print("")
            return 0
        segments, info = model.transcribe(src, language="zh",
                                          initial_prompt=prompt, **extra_kw)
        text = "".join(s.text for s in segments).strip()
    print(text)
    return 0
```

- [ ] **Step 4: Wire `voice-ptt.py`**

Add `import vad_trim` next to `from terms import ...`, then replace the `segments, info = m.transcribe(...)` / `text = ...` block with:

```python
        with vad_trim.trimmed_wav(wav) as src:
            if src is None:
                print("[voice-input] vad: no speech, empty transcript",
                      file=sys.stderr)
                text = ""
            else:
                segments, info = m.transcribe(
                    src,
                    language="zh",
                    initial_prompt=prompt,
                    **extra_kw,
                )
                text = "".join(s.text for s in segments).strip()
```

- [ ] **Step 5: Run the wiring test and the existing suites**

Run:
```bash
python3 -m unittest test_vad_trim.TestWiring -v
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add transcribe_once.py voice-ptt.py test_vad_trim.py
git commit -m "feat(cli): trim silence on x11 and toggle transcription paths (#32)"
```

---

### Task 4: Real-audio E2E test (A10)

**Files:**
- Modify: `test_vad_trim.py` (new `TestEndToEnd`)

**Interfaces:**
- Consumes: `transcribe_once.py` CLI, the repro wav at `~/.local/share/voice-input/debug/repro-loop-20260930.wav`, NPU engine env.
- Produces: `TestEndToEnd`, skipped unless all three conditions hold.

- [ ] **Step 1: Write the test**

Append to `test_vad_trim.py`:

```python
REPRO_WAV = os.path.expanduser(
    "~/.local/share/voice-input/debug/repro-loop-20260930.wav")


def e2e_ready():
    if not os.path.exists(REPRO_WAV):
        return False
    if os.environ.get("VOICE_INPUT_ENGINE") != "npu":
        return False
    try:
        import numpy  # noqa: F401
        import faster_whisper  # noqa: F401
        import openvino_genai  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(e2e_ready(), "needs repro wav + npu deps + VOICE_INPUT_ENGINE=npu")
class TestEndToEnd(unittest.TestCase):
    def test_repro_has_no_silence_hallucination(self):
        import subprocess
        import sys
        root = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(
            [sys.executable, os.path.join(root, "transcribe_once.py"), REPRO_WAV],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.strip()
        for marker in ("词曲", "字幕", "Zither"):
            self.assertNotIn(marker, text)
        self.assertLessEqual(text.count("这个"), 8)
```

- [ ] **Step 2: Run it under the venv + NPU**

Run:
```bash
LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu VOICE_INPUT_ENGINE=npu \
  venv/bin/python3 -m unittest test_vad_trim.TestEndToEnd -v
```
Expected: PASS (runtime ~10–20 s incl. model load). If the repro wav or NPU is missing, the test is skipped — that is not a failure.

- [ ] **Step 3: Commit**

```bash
git add test_vad_trim.py
git commit -m "test(vad): real-audio e2e for silence hallucination (#32)"
```

---

### Task 5: README sync + full regression (A11)

**Files:**
- Modify: `README.md` (Configuration table, NPU/Wayland notes, Testing command)
- Modify: `README.zh-CN.md` (same three places)
- Modify: `test_vad_trim.py` (nothing new; A11 runs the full suite)

**Interfaces:**
- Consumes: `VOICE_INPUT_VAD` from `vad_trim.ENV_VAD`.
- Produces: docs mention the env knob and the new test module.

- [ ] **Step 1: Update `README.md`**

Add a Configuration row after `VOICE_INPUT_RESIDENT`:

```markdown
| `VOICE_INPUT_VAD` | `1` (on) | Trim leading/trailing and long internal silence (Silero VAD bundled with faster-whisper) before transcription, so Whisper never decodes silence. `0` = legacy behavior | `systemctl --user edit voice-hold` drop-in, e.g. `Environment=VOICE_INPUT_VAD=0` |
```

Append one sentence to the Wayland section's hold bullet and to the NPU engine notes:

```markdown
Silence is trimmed before transcription (#32): only VAD-detected speech reaches Whisper, which removes the "词曲 李宗盛" / "字幕by索兰娅" silence hallucinations. `VOICE_INPUT_VAD=0` disables it.
```

Replace the Testing command with:

```bash
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim test_engine -v
```

- [ ] **Step 2: Mirror all three edits in `README.zh-CN.md`**

配置表新增同一行（中文）：

```markdown
| `VOICE_INPUT_VAD` | `1`（开） | 转写前用 Silero VAD 裁掉首尾与段内长静音（模型随 faster-whisper 分发），避免 Whisper 解码静音。`0` = 旧行为 | `systemctl --user edit voice-hold` 加 drop-in，如 `Environment=VOICE_INPUT_VAD=0` |
```

段末中文说明：

```markdown
转写前会裁掉静音（#32）：只有 VAD 判定为语音的部分进入 Whisper，消除「词曲 李宗盛」「字幕by索兰娅」这类静音幻觉。`VOICE_INPUT_VAD=0` 可关闭。
```

Testing 命令与英文版同一行（追加 `test_vad_trim test_engine`）。

- [ ] **Step 3: Run the full regression suite (system python)**

Run:
```bash
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim test_engine -v
```
Expected: PASS; `TestSpeechSpans` and `TestEndToEnd` are skipped (no numpy in system python).

- [ ] **Step 4: Verify the two language versions stay in sync**

Run:
```bash
rg -n 'VOICE_INPUT_VAD' README.md README.zh-CN.md
rg -n 'test_vad_trim test_engine' README.md README.zh-CN.md
```
Expected: each file returns the new config row and the updated testing command.

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md
git commit -m "docs: VOICE_INPUT_VAD knob and updated test suite (#32)"
```
