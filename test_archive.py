import json
import os
import shutil
import tempfile
import unittest
import wave

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


if __name__ == "__main__":
    unittest.main()
