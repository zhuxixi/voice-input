"""A7 (#25): worker loop — ready line, per-request terms reload, error survival, EOF."""

import argparse
import contextlib
import wave
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import engine
import terms
import transcribe_worker as tw
import worker_protocol as wp

REPO = os.path.dirname(os.path.abspath(__file__))
TERMS = '{"terms": ["Z码"]}'


class FakeModel:
    """faster-whisper-shaped stub; records the kwargs it was called with."""

    def __init__(self):
        self.calls = []
        self.raise_next = False

    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        self.calls.append({"wav": wav, "language": language,
                           "initial_prompt": initial_prompt, **kw})
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("boom")
        text = "" if wav.endswith("silence.wav") else getattr(self, "text", "你好")
        seg = type("Seg", (), {"text": text})()
        return [seg], type("Info", (), {"language": language})()


class WorkerHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-worker-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.terms_path = os.path.join(self._tmp, "terms.json")
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write(TERMS)
        self.model = FakeModel()
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        self.parent = parent.makefile("rwb")
        self.parent_sock = parent
        self.child = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                                   fileno=child.detach())
        self.addCleanup(self.child.close)
        self.child_file = self.child.makefile("rwb")
        self.done = None
        # #44: every worker started here archives into a throwaway dir, so tests
        # can never write to (or prune!) the real ~/.local/share/voice-input/recordings.
        self.default_archive_dir = os.path.join(self._tmp, "recordings")

    def start(self, log=None, archive_dir=None, conn=None):
        archive_dir = archive_dir or self.default_archive_dir
        def run():
            self.done = tw.worker_main(conn or self.child_file,
                                       model_factory=lambda: self.model,
                                       terms_loader=lambda path=None: tw.load_terms(
                                           self.terms_path),
                                       log=log, archive_dir=archive_dir)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5.0)
        return t

    def read(self):
        return wp.parse_line(self.parent.readline())

    def send(self, msg):
        self.parent.write(msg)
        self.parent.flush()


class TestWorkerLoop(WorkerHarness):
    def test_ready_then_text_response(self):
        self.start()
        ready = self.read()
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(ready["engine"],
                         os.environ.get(engine.ENV_ENGINE, engine.DEFAULT_ENGINE))
        self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
        self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "你好"})

    def test_empty_text_is_a_normal_response(self):
        self.start(); self.read()
        self.parent.write(wp.encode_request(1, "/tmp/silence.wav")); self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": ""})

    def test_terms_are_reloaded_per_request(self):
        self.start(); self.read()
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[0]["initial_prompt"],
                         terms.build_prompt(["Z码"]))
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write('{"terms": ["新词"]}')
        self.parent.write(wp.encode_request(2, "/tmp/a.wav")); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[1]["initial_prompt"],
                         terms.build_prompt(["新词"]))

    def test_terms_failure_degrades_and_worker_survives(self):
        self.start(); self.read()
        os.unlink(self.terms_path)
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "你好"})
        self.assertIsNone(self.model.calls[0]["initial_prompt"])

    def test_transcribe_exception_answers_error_and_survives(self):
        self.start(); self.read()
        self.model.raise_next = True
        self.parent.write(wp.encode_request(1, "/tmp/a.wav")); self.parent.flush()
        msg = self.read()
        self.assertIn("RuntimeError", msg["error"])
        self.parent.write(wp.encode_request(2, "/tmp/a.wav")); self.parent.flush()
        self.assertEqual(self.read()["text"], "你好")

    def test_worker_handles_unicode_wav_path(self):
        self.start(); self.read()
        path = "/tmp/语音 目录/a.wav"
        self.parent.write(wp.encode_request(1, path)); self.parent.flush()
        self.read()
        self.assertEqual(self.model.calls[0]["wav"], path)

    def test_eof_returns_zero(self):
        self.start(); self.read()
        self.parent.close()
        self.parent_sock.close()
        for _ in range(50):
            if self.done is not None:
                break
            import time; time.sleep(0.05)
        self.assertEqual(self.done, 0)

    def test_bad_line_returns_three(self):
        self.start(); self.read()
        self.parent.write(b"{not json}\n"); self.parent.flush()
        for _ in range(50):
            if self.done is not None:
                break
            import time; time.sleep(0.05)
        self.assertEqual(self.done, 3)


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


class TestCorrectionsWiring(WorkerHarness):
    """#33 A8: worker 对响应文本应用 corrections(与 terms 同一次加载,热加载生效)。"""

    CORRECTIONS = '{"terms": [], "corrections": {"批诏": "pjob", "sql": "skill"}}'

    def setUp(self):
        super().setUp()
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write(self.CORRECTIONS)
        self.model.text = "批诏 sql"

    def test_corrections_applied_to_response(self):
        self.start()
        self.read()
        self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
        self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "pjob skill"})

    def test_corrections_reloaded_per_request(self):
        self.start()
        self.read()
        self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
        self.parent.flush()
        self.assertEqual(self.read(), {"id": 1, "text": "pjob skill"})
        with open(self.terms_path, "w", encoding="utf-8") as fh:
            fh.write('{"terms": [], "corrections": {"批诏": "pijob"}}')
        self.parent.write(wp.encode_request(2, "/tmp/a.wav"))
        self.parent.flush()
        self.assertEqual(self.read(), {"id": 2, "text": "pijob sql"})


class TestPreambleInvalidEngine(unittest.TestCase):
    """Spec §4.4 (#25): invalid engine -> clean exit 1, one actionable log
    line, no bare traceback (mirrors transcribe_once.py:47-53)."""

    def test_invalid_engine_exits_clean(self):
        logs = []
        with mock.patch.dict(os.environ, {"VOICE_INPUT_ENGINE": "bogus"}):
            rc = tw._preamble(logs.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any("unsupported engine" in m for m in logs), logs)


class TestArgv(unittest.TestCase):
    def test_protocol_fd_is_required(self):
        with self.assertRaises(SystemExit):
            tw.parse_args([])

    def test_protocol_fd_parsed(self):
        self.assertEqual(tw.parse_args(["--protocol-fd", "7"]).protocol_fd, 7)

    def test_non_int_fd_rejected(self):
        with self.assertRaises(SystemExit):
            tw.parse_args(["--protocol-fd", "abc"])


DRIVER = """
import os, socket, sys
sys.path.insert(0, {repo!r})
import transcribe_worker as tw

class FakeModel:
    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        return [type("S", (), {{"text": "driver-text"}})()], object()

fd = int(sys.argv[sys.argv.index("--protocol-fd") + 1])
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=fd)
sys.exit(tw.worker_main(conn.makefile("rwb"), model_factory=FakeModel,
                        log=lambda m: print(m, file=sys.stderr)))
"""


class TestRealProcessEof(unittest.TestCase):
    def test_worker_exits_on_parent_socket_close(self):
        import subprocess, textwrap
        tmp = tempfile.mkdtemp(prefix="vh-eof-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        driver = os.path.join(tmp, "driver.py")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(DRIVER.format(repo=REPO))
        parent, child = socket.socketpair()
        proc = subprocess.Popen(
            [sys.executable, driver, "--protocol-fd", str(child.fileno())],
            pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
            env=dict(os.environ, VOICE_INPUT_ARCHIVE="0"))   # #44: no real writes
        child.close()
        with parent.makefile("rwb") as f:
            self.assertEqual(wp.parse_line(f.readline())["event"], "ready")
        parent.close()                          # simulate the daemon vanishing
        self.assertEqual(proc.wait(timeout=5.0), 0)


class RecordingConn:
    """File-like proxy that logs write() calls (for the archive/reply ordering)."""

    def __init__(self, inner, events):
        self._inner = inner
        self._events = events

    def write(self, data):
        self._events.append("write")
        return self._inner.write(data)

    def flush(self):
        return self._inner.flush()

    def readline(self):
        return self._inner.readline()


def _write_wav(path, seconds=0.2, sample_rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * seconds))


class TestArchiveHook(WorkerHarness):
    """#44 A5/A6/A7/A8/A9/A12: worker-side archiving contract."""

    def setUp(self):
        super().setUp()
        self.archive_dir = os.path.join(self._tmp, "recordings")
        self.wav = os.path.join(self._tmp, "dictation.wav")
        _write_wav(self.wav)
        self.events = []

    def trim_to(self, src, status="kept"):
        @contextlib.contextmanager
        def fake(path, stats=None, **kwargs):
            if stats is not None:
                stats.update({"status": status, "threshold": 0.5,
                              "speech_pad_ms": 400, "total_s": 0.2,
                              "kept_s": 0.2 if status == "kept" else 0.0,
                              "kept_ratio": 1.0 if status == "kept" else 0.0,
                              "spans": 1 if status == "kept" else 0})
            yield src
        return mock.patch.object(tw.vad_trim, "trimmed_wav", fake)

    def send(self, wav=None):
        self.parent.write(wp.encode_request(1, wav or self.wav))
        self.parent.flush()
        return self.read()

    def records(self):
        path = os.path.join(self.archive_dir, "index.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh.read().strip().splitlines()]

    def test_archives_original_audio_with_vad_metadata(self):
        source_size = os.path.getsize(self.wav)
        self.start(archive_dir=self.archive_dir)
        self.read()
        with self.trim_to(self.wav):
            self.assertEqual(self.send(), {"id": 1, "text": "你好"})
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["vad"]["status"], "kept")
        self.assertEqual(records[0]["vad"]["spans"], 1)
        self.assertEqual(records[0]["raw"], "你好")
        self.assertIn("engine", records[0])
        rec_dir = os.path.join(self.archive_dir, records[0]["dir"])
        archived = os.path.join(rec_dir, "audio.wav")
        self.assertEqual(os.path.getsize(archived), source_size,
                         "archived audio must be the original, untrimmed wav")

    def test_archive_precedes_reply(self):
        self.start(archive_dir=self.archive_dir, conn=RecordingConn(self.child_file,
                                                                    self.events))
        self.read()
        self.events.clear()
        with self.trim_to(self.wav):
            with mock.patch.object(tw.archive, "archive_result",
                                   lambda *a, **k: self.events.append("archive")):
                self.assertEqual(self.send(), {"id": 1, "text": "你好"})
        self.assertEqual(self.events, ["archive", "write"])

    def test_archive_failure_still_replies(self):
        self.start(archive_dir=self.archive_dir)
        self.read()
        with self.trim_to(self.wav):
            with mock.patch.object(tw.archive, "archive_recording",
                                   side_effect=OSError("read-only fs")):
                self.assertEqual(self.send(), {"id": 1, "text": "你好"})
        self.assertEqual(self.records(), [])

    def test_disabled_archiving_writes_nothing(self):
        with mock.patch.object(tw.archive, "ARCHIVE_ENABLED", False):
            self.start(archive_dir=self.archive_dir)
            self.read()
            with self.trim_to(self.wav):
                self.assertEqual(self.send(), {"id": 1, "text": "你好"})
        self.assertFalse(os.path.exists(self.archive_dir))
        self.assertTrue(os.path.exists(self.wav))

    def test_no_speech_is_archived(self):
        self.start(archive_dir=self.archive_dir)
        self.read()
        with self.trim_to(None, status="no_speech"):
            self.assertEqual(self.send(), {"id": 1, "text": ""})
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["raw"], "")
        self.assertEqual(records[0]["vad"]["status"], "no_speech")

    def test_prune_runs_once_on_start(self):
        with mock.patch.object(tw.archive, "prune_recordings",
                               return_value=[]) as prune:
            self.start(archive_dir=self.archive_dir)
            self.read()
        prune.assert_called_once()
        self.assertEqual(prune.call_args[0][0], self.archive_dir)

    def test_prune_skipped_when_disabled(self):
        with mock.patch.object(tw.archive, "ARCHIVE_ENABLED", False):
            with mock.patch.object(tw.archive, "prune_recordings") as prune:
                self.start(archive_dir=self.archive_dir)
                self.read()
        prune.assert_not_called()
