"""A1-A9 (#32): silence trimming for transcription."""

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
        with self.assertRaises(ValueError):
            with vad_trim.trimmed_wav(self.wav, detector=self.detector(spans),
                                      log=self.log) as src:
                raise ValueError("caller boom")

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
        result = subprocess.run(
            [sys.executable, os.path.join(root, "transcribe_once.py"), REPRO_WAV],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.strip()
        for marker in ("词曲", "字幕", "Zither"):
            self.assertNotIn(marker, text)
        self.assertLessEqual(text.count("这个"), 8)
