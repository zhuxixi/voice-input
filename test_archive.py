import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from datetime import datetime
from unittest import mock

import archive
import transcribe_once as to
from archive import archive_recording, archive_result, prune_recordings


def _make_wav(path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 100)


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.archive_dir = os.path.join(self.tmp, "recordings")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wav(self, name="in.wav"):
        p = os.path.join(self.tmp, name)
        _make_wav(p)
        return p

    def test_basic_archive(self):
        wav = self._wav()
        rec_dir = archive_recording(wav, "你好 zima", "你好 zima", archive_dir=self.archive_dir)
        self.assertTrue(os.path.isfile(os.path.join(rec_dir, "audio.wav")))
        self.assertEqual(
            open(os.path.join(rec_dir, "raw.txt"), encoding="utf-8").read(), "你好 zima"
        )
        self.assertEqual(
            open(os.path.join(rec_dir, "final.txt"), encoding="utf-8").read(), "你好 zima"
        )
        self.assertFalse(os.path.exists(wav))  # wav 被 move 走

    def test_index_jsonl_two_records(self):
        archive_recording(self._wav("a.wav"), "a", "a", archive_dir=self.archive_dir)
        archive_recording(self._wav("b.wav"), "b", "b", archive_dir=self.archive_dir)
        lines = (
            open(os.path.join(self.archive_dir, "index.jsonl"), encoding="utf-8")
            .read()
            .strip()
            .split("\n")
        )
        self.assertEqual(len(lines), 2)
        r0, r1 = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual(r0["raw"], "a")
        self.assertEqual(r1["raw"], "b")
        for r in (r0, r1):
            for k in ("ts", "dir", "audio", "final", "enhanced"):
                self.assertIn(k, r)

    def test_enhanced_flag_when_raw_ne_final(self):
        archive_recording(self._wav(), "z码", "zima", archive_dir=self.archive_dir)
        rec = json.loads(
            open(os.path.join(self.archive_dir, "index.jsonl"), encoding="utf-8").readline()
        )
        self.assertTrue(rec["enhanced"])
        self.assertEqual(rec["final"], "zima")

    def test_dir_unique_across_calls(self):
        for i in range(3):
            archive_recording(self._wav(f"{i}.wav"), "x", "x", archive_dir=self.archive_dir)
        dirs = [d for d in os.listdir(self.archive_dir) if not d.endswith(".jsonl")]
        self.assertEqual(len(dirs), 3)

    def test_unwritable_dir_raises(self):
        with self.assertRaises(Exception):
            archive_recording(self._wav(), "x", "x", archive_dir="/proc/cannot-create-xxx")

    def test_audio_not_lost_if_index_write_fails_after_move(self):
        wav = self._wav()
        # 让 index.jsonl 的 open("a") 抛异常:raw.txt/final.txt("w") 正常 → move 成功 →
        # 写 index 时失败 → audio_moved=True → except 不 rmtree → audio.wav 留在 rec_dir。
        real_open = open

        def flaky_open(path, mode="r", *a, **kw):
            if mode == "a":
                raise OSError("simulated index write failure")
            return real_open(path, mode, *a, **kw)

        with mock.patch("builtins.open", side_effect=flaky_open):
            with self.assertRaises(OSError):
                archive_recording(wav, "x", "x", archive_dir=self.archive_dir)
        # 关键断言:wav 被 move 进了某个 rec_dir(音频没丢,没留在 /tmp 也没被删)
        rec_dirs = (
            [d for d in os.listdir(self.archive_dir) if not d.endswith(".jsonl")]
            if os.path.isdir(self.archive_dir)
            else []
        )
        found_audio = any(
            os.path.isfile(os.path.join(self.archive_dir, d, "audio.wav")) for d in rec_dirs
        )
        self.assertTrue(found_audio, "audio.wav must survive index-write failure (no data loss)")
        self.assertFalse(os.path.exists(wav), "original wav should have been moved out of /tmp")

    def test_audio_not_lost_on_copy_truncation(self):
        """cc#9/kimi#9: copyfile 中途失败(ENOSPC)留截断 audio → 必须 rmtree 清掉,源 wav 保留。"""
        wav = self._wav()
        def truncating_copyfile(src, dst):
            # 写一小部分(dst 截断)后抛,模拟 ENOSPC(源 ~244 bytes,这里只写 10)
            with open(dst, "wb") as f:
                f.write(b"\x00" * 10)  # 截断
            raise OSError("simulated ENOSPC during copyfile")
        with mock.patch("archive.shutil.copyfile", side_effect=truncating_copyfile):
            with self.assertRaises(OSError):
                archive_recording(wav, "x", "x", archive_dir=self.archive_dir)
        # rec_dir 应被 rmtree 清掉(不留截断孤儿)
        if os.path.isdir(self.archive_dir):
            leftover = [d for d in os.listdir(self.archive_dir) if not d.endswith(".jsonl")]
            self.assertEqual(leftover, [], "truncated rec_dir must be cleaned up")
        # 源 wav 还在(没丢,交 finally)
        self.assertTrue(os.path.exists(wav), "source wav must survive copy failure")

    def test_audio_survives_copystat_failure_after_copy(self):
        """cc#8: copyfile 成功但 copystat 失败 → audio 数据完整,必须保留(不 rmtree)。"""
        wav = self._wav()
        expected_size = os.path.getsize(wav)  # 完整大小(WAV header + frames)
        with mock.patch("archive.shutil.copystat", side_effect=OSError("copystat failed")):
            rec_dir = archive_recording(wav, "x", "x", archive_dir=self.archive_dir)
        self.assertTrue(os.path.isfile(os.path.join(rec_dir, "audio.wav")))
        # audio 数据完整(大小与源一致)
        self.assertEqual(os.path.getsize(os.path.join(rec_dir, "audio.wav")), expected_size)
        self.assertFalse(os.path.exists(wav))  # 源已删



class TestMeta(unittest.TestCase):
    """#44 A3: optional provenance/VAD fields in the index record."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive_dir = os.path.join(self.tmp, "recordings")

    def _wav(self, name="in.wav"):
        p = os.path.join(self.tmp, name)
        _make_wav(p)
        return p

    def _first_record(self):
        line = open(os.path.join(self.archive_dir, "index.jsonl"),
                    encoding="utf-8").readline()
        return json.loads(line)

    def test_fields_absent_by_default(self):
        archive_recording(self._wav(), "x", "x", archive_dir=self.archive_dir)
        record = self._first_record()
        self.assertEqual(
            set(record),
            {"ts", "dir", "audio", "raw", "final", "enhanced"},
            "voice-ptt path must keep its original key set")

    def test_keep_source_copies_without_deleting(self):
        wav = self._wav()
        rec_dir = archive_recording(wav, "x", "x", archive_dir=self.archive_dir,
                                    keep_source=True)
        self.assertTrue(os.path.isfile(os.path.join(rec_dir, "audio.wav")))
        self.assertTrue(os.path.exists(wav), "keep_source must not delete the input")

    def test_fields_written_when_given(self):
        vad = {"status": "kept", "threshold": 0.5, "speech_pad_ms": 400,
               "total_s": 1.2, "kept_s": 1.0, "kept_ratio": 0.833, "spans": 1}
        archive_recording(self._wav(), "x", "y", archive_dir=self.archive_dir,
                          engine="npu", model="small-int8-ov", vad=vad)
        record = self._first_record()
        self.assertEqual(record["engine"], "npu")
        self.assertEqual(record["model"], "small-int8-ov")
        self.assertEqual(record["vad"], vad)
        self.assertTrue(record["enhanced"])


class TestArchiveResult(unittest.TestCase):
    """#44 A7/A8/D9: the gate + exception guard used by both transcribers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive_dir = os.path.join(self.tmp, "recordings")
        self.wav = os.path.join(self.tmp, "in.wav")
        _make_wav(self.wav)
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def test_disabled_writes_nothing(self):
        with mock.patch.object(archive, "ARCHIVE_ENABLED", False):
            ok = archive_result(self.wav, "x", "x", archive_dir=self.archive_dir,
                                log=self.log)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.archive_dir))
        self.assertTrue(os.path.exists(self.wav))

    def test_none_archive_dir_falls_back_to_module_default(self):
        with mock.patch.object(archive, "ARCHIVE_DIR", self.archive_dir):
            ok = archive_result(self.wav, "x", "x", archive_dir=None, log=self.log)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(os.path.join(self.archive_dir, "index.jsonl")))

    def test_missing_source_raises_before_creating_root(self):
        missing = os.path.join(self.tmp, "nope.wav")
        with self.assertRaises(FileNotFoundError):
            archive_recording(missing, "x", "x", archive_dir=self.archive_dir)
        self.assertFalse(os.path.exists(self.archive_dir),
                         "a bogus path must not create an empty archive root")

    def test_failure_never_raises(self):
        with mock.patch.object(archive, "archive_recording",
                               side_effect=OSError("disk full")):
            ok = archive_result(self.wav, "x", "x", archive_dir=self.archive_dir,
                                log=self.log)
        self.assertFalse(ok)
        self.assertTrue(any("disk full" in line for line in self.logs))


class TestPrune(unittest.TestCase):
    """#44 A4: retention is opt-in and never touches index.jsonl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive_dir = os.path.join(self.tmp, "recordings")
        os.makedirs(self.archive_dir)

    def _mk(self, name):
        os.makedirs(os.path.join(self.archive_dir, name), exist_ok=True)

    def test_zero_keep_days_is_a_noop(self):
        self._mk("2020-01-01_000000")
        self.assertEqual(prune_recordings(self.archive_dir, keep_days=0), [])
        self.assertTrue(os.path.isdir(os.path.join(self.archive_dir, "2020-01-01_000000")))

    def test_negative_keep_days_is_a_noop(self):
        self._mk("2020-01-01_000000")
        self.assertEqual(prune_recordings(self.archive_dir, keep_days=-1), [])

    def test_removes_only_expired_timestamped_dirs(self):
        self._mk("2020-01-01_000000")
        self._mk("2020-01-01_000000_2")          # collision suffix form
        self._mk("2020-01-09_000000")            # fresh
        self._mk("notes")                        # unrecognised -> untouched
        with open(os.path.join(self.archive_dir, "index.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"ts": "2020-01-01T00:00:00"}\n')
        removed = prune_recordings(self.archive_dir, keep_days=5,
                                   now=datetime(2020, 1, 10, 0, 0, 0))
        self.assertEqual(sorted(removed),
                         ["2020-01-01_000000", "2020-01-01_000000_2"])
        self.assertTrue(os.path.isdir(os.path.join(self.archive_dir, "2020-01-09_000000")))
        self.assertTrue(os.path.isdir(os.path.join(self.archive_dir, "notes")))
        self.assertTrue(os.path.isfile(os.path.join(self.archive_dir, "index.jsonl")))

    def test_missing_dir_is_not_an_error(self):
        self.assertEqual(prune_recordings(os.path.join(self.tmp, "nope"), keep_days=1), [])


class FakeOnceModel:
    def __init__(self, text="你好"):
        self.text = text

    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        seg = type("Seg", (), {"text": self.text})()
        return [seg], type("Info", (), {"language": language})()


class TestTranscribeOnceHook(unittest.TestCase):
    """#44 A9b/A10: legacy CLI archives (including the no-speech case)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive_dir = os.path.join(self.tmp, "recordings")
        self.wav = os.path.join(self.tmp, "in.wav")
        _make_wav(self.wav)
        self.terms_path = os.path.join(self.tmp, "missing-terms.json")

    def fake_trim(self, src):
        @contextlib.contextmanager
        def _ctx(path, stats=None, **_kw):
            if stats is not None:
                stats.update({"status": "no_speech" if src is None else "kept",
                              "threshold": 0.5, "speech_pad_ms": 400,
                              "total_s": 0.01, "kept_s": 0.01,
                              "kept_ratio": 1.0, "spans": 0 if src is None else 1})
            yield src
        return _ctx

    def run_main(self, src, text="你好"):
        patches = [
            mock.patch.object(to, "_preamble", lambda: 0),
            mock.patch.object(to.engine, "build_model", lambda: FakeOnceModel(text)),
            mock.patch.object(to.vad_trim, "trimmed_wav", self.fake_trim(src)),
            mock.patch.object(archive, "ARCHIVE_DIR", self.archive_dir),
            mock.patch.object(archive, "ARCHIVE_ENABLED", True),
            mock.patch.object(archive, "ARCHIVE_KEEP_DAYS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return to.main([self.wav], terms_path=self.terms_path)

    def records(self):
        path = os.path.join(self.archive_dir, "index.jsonl")
        if not os.path.isfile(path):
            return []
        return [json.loads(line) for line in
                open(path, encoding="utf-8").read().strip().splitlines()]

    def test_speech_is_archived_before_stdout(self):
        self.assertEqual(self.run_main(self.wav, "你好呀"), 0)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["raw"], "你好呀")
        self.assertEqual(records[0]["vad"]["status"], "kept")
        self.assertEqual(records[0]["engine"] is not None, True)
        self.assertTrue(os.path.exists(self.wav),
                        "the CLI must never delete the file it was given")

    def test_no_speech_is_archived(self):
        self.assertEqual(self.run_main(None), 0)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["raw"], "")
        self.assertEqual(records[0]["vad"]["status"], "no_speech")
        self.assertTrue(os.path.exists(self.wav))


class TestTranscribeOnceImport(unittest.TestCase):
    """#44 A13/D10: importing the CLI must not fork or mutate the environment."""

    def test_import_has_no_side_effects(self):
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        result = subprocess.run(
            [sys.executable, "-c",
             "import os, transcribe_once; print('ok', 'LD_LIBRARY_PATH' in os.environ)"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok False")
