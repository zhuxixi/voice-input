import json
import os
import tempfile
import unittest

from terms import DEFAULT_TERMS_PATH, load_terms, build_prompt


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


if __name__ == "__main__":
    unittest.main()
