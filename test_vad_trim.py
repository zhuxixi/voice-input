"""A1-A10 (#32): silence trimming for transcription."""

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
        self.assertEqual(self.logs, [])

    def test_env_disable_passthrough(self):
        with mock.patch.dict(os.environ, {vad_trim.ENV_VAD: "0"}):
            with vad_trim.trimmed_wav(self.wav,
                                      detector=self.detector([(4000, 12000)]),
                                      log=self.log) as src:
                self.assertEqual(src, self.wav)
        self.assertEqual(self.logs, [])

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
        self.assertIn("vad: 1.00s -> 0.50s (1 span)", "\n".join(self.logs))

    def test_caller_exception_propagates(self):
        spans = [(4000, 12000)]
        seen = []
        with self.assertRaises(ValueError):
            with vad_trim.trimmed_wav(self.wav, detector=self.detector(spans),
                                      log=self.log) as src:
                seen.append(src)
                raise ValueError("caller boom")
        self.assertFalse(os.path.exists(seen[0]))

    def test_near_full_length_passthrough(self):
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 16000)]),
                                  log=self.log) as src:
            self.assertEqual(src, self.wav)
        self.assertEqual([p for p in os.listdir(self.dir)
                          if p.startswith("voice-input-trim-")], [])
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 15856)]),
                                  log=self.log) as src:
            self.assertEqual(src, self.wav)
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 15680)]),
                                  log=self.log) as src:
            self.assertNotEqual(src, self.wav)
        with vad_trim.trimmed_wav(self.wav, detector=self.detector([(0, 15000)]),
                                  log=self.log) as src:
            self.assertNotEqual(src, self.wav)

    def test_write_failure_falls_back(self):
        with mock.patch.object(vad_trim, "_write_trimmed",
                               side_effect=OSError("disk full")):
            with vad_trim.trimmed_wav(self.wav,
                                      detector=self.detector([(4000, 12000)]),
                                      log=self.log) as src:
                self.assertEqual(src, self.wav)
        self.assertIn("vad: error", "\n".join(self.logs))

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
        self.assertEqual(options.speech_pad_ms, 400)
        self.assertEqual(options.min_speech_duration_ms, 0)


class TestWiring(unittest.TestCase):
    def test_cli_call_sites_use_trimmed_wav(self):
        root = os.path.dirname(os.path.abspath(__file__))
        for name in ("transcribe_once.py", "voice-ptt.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn("vad_trim.trimmed_wav(", source, name)
            self.assertNotIn("transcribe(wav", source, name)


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
        # #44: transcribe_once now archives; keep the E2E run out of the real
        # recordings directory (and off the 2 MB repro wav).
        env = dict(os.environ, VOICE_INPUT_ARCHIVE="0")
        result = subprocess.run(
            [sys.executable, os.path.join(root, "transcribe_once.py"), REPRO_WAV],
            capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.strip()
        for marker in ("词曲", "字幕", "Zither"):
            self.assertNotIn(marker, text)
        self.assertLessEqual(text.count("这个"), 8)


class TestVadSummary(unittest.TestCase):
    """#44 A1: pure coverage summary (no faster-whisper import needed)."""

    def test_zero_spans(self):
        s = vad_trim.vad_summary([], SR * 2, SR)
        self.assertEqual(s["spans"], 0)
        self.assertEqual(s["total_s"], 2.0)
        self.assertEqual(s["kept_s"], 0.0)
        self.assertEqual(s["kept_ratio"], 0.0)
        self.assertEqual(s["threshold"], vad_trim.DEFAULT_THRESHOLD)
        self.assertEqual(s["speech_pad_ms"], vad_trim.DEFAULT_PAD_MS)

    def test_full_coverage(self):
        s = vad_trim.vad_summary([(0, SR)], SR, SR)
        self.assertEqual((s["spans"], s["kept_s"], s["kept_ratio"]), (1, 1.0, 1.0))

    def test_partial_coverage(self):
        s = vad_trim.vad_summary([(0, SR // 2)], SR, SR)
        self.assertEqual(s["kept_s"], 0.5)
        self.assertEqual(s["kept_ratio"], 0.5)

    def test_options_are_duck_typed(self):
        class Options:
            threshold = 0.35
            speech_pad_ms = 600
        s = vad_trim.vad_summary([(0, 100)], 100, SR, Options())
        self.assertEqual((s["threshold"], s["speech_pad_ms"]), (0.35, 600))

    def test_bad_sample_rate_does_not_divide_by_zero(self):
        s = vad_trim.vad_summary([(0, 10)], 10, 0)
        self.assertEqual((s["total_s"], s["kept_s"], s["kept_ratio"]),
                         (0.0, 0.0, 1.0))


class TestStats(unittest.TestCase):
    """#44 A2: trimmed_wav fills the stats dict for all four states."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vad-stats-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.wav = os.path.join(self.dir, "in.wav")
        make_wav(self.wav, seconds=1.0)
        self.src = "unset"

    def detector(self, spans=None, error=None):
        def _detect(path):
            if error is not None:
                raise error
            return read_frames(path), spans
        return _detect

    def collect(self, **kw):
        stats = {}
        with vad_trim.trimmed_wav(self.wav, stats=stats, log=lambda m: None, **kw) as src:
            self.src = src
        return stats

    def test_disabled(self):
        stats = self.collect(enabled=False, detector=self.detector([(0, SR)]))
        self.assertEqual(stats, {"status": "disabled"})

    def test_kept_trimmed(self):
        stats = self.collect(detector=self.detector([(0, SR // 2)]))
        self.assertEqual(stats["status"], "kept")
        self.assertEqual((stats["spans"], stats["kept_s"], stats["kept_ratio"]),
                         (1, 0.5, 0.5))
        self.assertTrue(self.src.endswith(".wav"))

    def test_kept_passthrough_keeps_original(self):
        stats = self.collect(detector=self.detector([(0, SR)]))
        self.assertEqual(stats["status"], "kept")
        self.assertEqual(stats["kept_ratio"], 1.0)
        self.assertEqual(self.src, self.wav)

    def test_no_speech(self):
        stats = self.collect(detector=self.detector([]))
        self.assertIsNone(self.src)
        self.assertEqual(stats["status"], "no_speech")
        self.assertEqual((stats["spans"], stats["kept_s"]), (0, 0.0))

    def test_degraded_unavailable(self):
        stats = self.collect(detector=self.detector(
            error=vad_trim.TrimUnavailable("no onnxruntime")))
        self.assertEqual((stats["status"], stats["reason"]), ("degraded", "unavailable"))
        self.assertEqual(self.src, self.wav)

    def test_degraded_error(self):
        stats = self.collect(detector=self.detector(error=ValueError("boom")))
        self.assertEqual((stats["status"], stats["reason"]), ("degraded", "error"))
