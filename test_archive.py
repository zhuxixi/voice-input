import json
import os
import shutil
import tempfile
import unittest
import wave
from unittest import mock

from archive import archive_recording


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


if __name__ == "__main__":
    unittest.main()
