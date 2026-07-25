import json
import os
import tempfile
import unittest

from terms import DEFAULT_TERMS_PATH, load_terms, build_prompt, build_transcribe_kwargs


class TestLoadTerms(unittest.TestCase):
    def test_missing_file_returns_empty_no_raise(self):
        # 文件不存在 -> 空配置,不抛
        cfg = load_terms("/nonexistent/path/terms.json")
        self.assertEqual(cfg, {"terms": [], "hotwords": None})

    def test_invalid_json_returns_empty_no_raise(self):
        # JSON 错 -> 空配置,不抛
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            path = f.name
        try:
            cfg = load_terms(path)
            self.assertEqual(cfg, {"terms": [], "hotwords": None})
        finally:
            os.unlink(path)

    def test_valid_json_sets_defaults(self):
        # 只给 terms,缺 hotwords -> setdefault 补 None
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"terms": ["zima", "jfox"]}, f)
            path = f.name
        try:
            cfg = load_terms(path)
            self.assertEqual(cfg, {"terms": ["zima", "jfox"], "hotwords": None})
        finally:
            os.unlink(path)

    def test_default_path_points_to_xdg_config(self):
        self.assertEqual(
            DEFAULT_TERMS_PATH,
            os.path.expanduser("~/.config/voice-input/terms.json"),
        )


class TestBuildPrompt(unittest.TestCase):
    def test_empty_terms_returns_none(self):
        self.assertIsNone(build_prompt([]))

    def test_normal_terms_embeds_in_chinese_sentence(self):
        prompt = build_prompt(["zima", "jfox"])
        self.assertIn("zima", prompt)
        self.assertIn("jfox", prompt)
        self.assertIn("术语", prompt)  # 中文包装句

    def test_truncates_to_30_terms(self):
        many = [f"term{i}" for i in range(100)]
        prompt = build_prompt(many)
        self.assertIn("term29", prompt)
        self.assertNotIn("term30", prompt)


class TestBuildTranscribeKwargs(unittest.TestCase):
    def test_hotwords_none_returns_empty(self):
        # hotwords=None/缺失 -> 空 dict(不传 hotwords,等价现状)
        self.assertEqual(build_transcribe_kwargs({"hotwords": None}), {})
        self.assertEqual(build_transcribe_kwargs({}), {})

    def test_hotwords_empty_list_returns_empty(self):
        self.assertEqual(build_transcribe_kwargs({"hotwords": []}), {})

    def test_hotwords_present_returns_kwarg(self):
        # faster-whisper hotwords 是 Optional[str],list 须 join 成空格分隔字符串
        kw = build_transcribe_kwargs({"hotwords": ["zima", "jfox"]})
        self.assertEqual(kw, {"hotwords": "zima jfox"})


class TestLoadTermsRobustness(unittest.TestCase):
    """malformed config 安全降级(spec §3.5:任何配置问题不得阻断转写)。"""

    def _with_content(self, content_bytes):
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
            f.write(content_bytes)
            return f.name

    def test_non_dict_json_degrades(self):
        # JSON 顶层非 dict(裸数组) -> load_terms 降级空配置
        path = self._with_content(b"[1,2,3]")
        try:
            self.assertEqual(load_terms(path), {"terms": [], "hotwords": None})
        finally:
            os.unlink(path)

    def test_non_utf8_degrades(self):
        # 非 UTF-8 字节 -> UnicodeDecodeError 被兜底 -> 降级
        path = self._with_content(b"\xff\xfe\x00{")
        try:
            self.assertEqual(load_terms(path), {"terms": [], "hotwords": None})
        finally:
            os.unlink(path)

    def test_terms_field_non_list_build_prompt_none(self):
        # terms 字段非 list(string/int/dict) -> build_prompt 守卫返回 None
        self.assertIsNone(build_prompt("zima"))
        self.assertIsNone(build_prompt(123))
        self.assertIsNone(build_prompt({"a": 1}))

    def test_terms_list_of_nonstr_does_not_crash(self):
        # terms=[1,2](int 元素) -> 不崩,isinstance(list) 通过后 join 成 "1、2"
        result = build_prompt([1, 2])
        self.assertIsNotNone(result)
        self.assertIn("1", result)


if __name__ == "__main__":
    unittest.main()
