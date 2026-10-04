"""A7 (#25): worker loop — ready line, per-request terms reload, error survival, EOF."""

import argparse
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
        text = "" if wav.endswith("silence.wav") else "你好"
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

    def start(self):
        def run():
            self.done = tw.worker_main(self.child_file, model_factory=lambda: self.model,
                                       terms_loader=lambda path=None: tw.load_terms(
                                           self.terms_path))
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
