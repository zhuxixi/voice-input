import io
import json
import os
import tempfile
import unittest
import unittest.mock as _mock

import terms
from terms import DEFAULT_TERMS_PATH, load_terms, build_prompt, build_transcribe_kwargs


class TestLoadTerms(unittest.TestCase):
    def test_missing_file_returns_empty_no_raise(self):
        # 文件不存在 -> 空配置,不抛
        cfg = load_terms("/nonexistent/path/terms.json")
        self.assertEqual(cfg, {"terms": [], "hotwords": None, "corrections": {}})

    def test_invalid_json_returns_empty_no_raise(self):
        # JSON 错 -> 空配置,不抛
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            path = f.name
        try:
            cfg = load_terms(path)
            self.assertEqual(cfg, {"terms": [], "hotwords": None, "corrections": {}})
        finally:
            os.unlink(path)

    def test_valid_json_sets_defaults(self):
        # 只给 terms,缺 hotwords -> setdefault 补 None
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"terms": ["zima", "jfox"]}, f)
            path = f.name
        try:
            cfg = load_terms(path)
            self.assertEqual(cfg, {"terms": ["zima", "jfox"], "hotwords": None, "corrections": {}})
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
        # 无 tokenizer 时回退 terms[:30](#33 A6:显式注入 None,不依赖运行环境)
        many = [f"term{i}" for i in range(100)]
        prompt = build_prompt(many, token_count=None)
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

    def test_hotwords_str_passed_through(self):
        self.assertEqual(build_transcribe_kwargs({"hotwords": "zima jfox"}), {"hotwords": "zima jfox"})

    def test_hotwords_int_not_passed(self):
        # 非 list/str 真值(int)不传,避免 faster-whisper .strip() 崩
        self.assertEqual(build_transcribe_kwargs({"hotwords": 123}), {})

    def test_hotwords_dict_not_passed(self):
        self.assertEqual(build_transcribe_kwargs({"hotwords": {"a": "b"}}), {})

    def test_hotwords_bool_not_passed(self):
        self.assertEqual(build_transcribe_kwargs({"hotwords": True}), {})

    def test_hotwords_list_nonstr_joined(self):
        # list 非 str 元素 join 成 str(不崩)
        self.assertEqual(build_transcribe_kwargs({"hotwords": [1, 2]}), {"hotwords": "1 2"})

    def test_hotwords_all_blank_list_not_passed(self):
        # 全空白元素 list(["",""] / [" "," "])join 得空格串,strip 后不传
        self.assertEqual(build_transcribe_kwargs({"hotwords": ["", ""]}), {})
        self.assertEqual(build_transcribe_kwargs({"hotwords": [" ", " "]}), {})


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
            self.assertEqual(load_terms(path), {"terms": [], "hotwords": None, "corrections": {}})
        finally:
            os.unlink(path)

    def test_non_utf8_degrades(self):
        # 非 UTF-8 字节 -> UnicodeDecodeError 被兜底 -> 降级
        path = self._with_content(b"\xff\xfe\x00{")
        try:
            self.assertEqual(load_terms(path), {"terms": [], "hotwords": None, "corrections": {}})
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



class TestApplyCorrections(unittest.TestCase):
    """#33 A1-A4:确定性替换(re.sub 单趟,stdout 无依赖)。"""

    def test_chinese_key_substring(self):
        self.assertEqual(terms.apply_corrections("我看一下批诏的状态", {"批诏": "pjob"}),
                         "我看一下pjob的状态")

    def test_missing_or_none_corrections_identity(self):
        self.assertEqual(terms.apply_corrections("abc", None), "abc")
        self.assertEqual(terms.apply_corrections("abc", {}), "abc")
        self.assertEqual(terms.apply_corrections("", {"a": "b"}), "")

    def test_ascii_word_case_insensitive_no_substring_damage(self):
        out = terms.apply_corrections("跑一下SQL和sqlite查询", {"sql": "skill"})
        self.assertEqual(out, "跑一下skill和sqlite查询")

    def test_longest_key_first(self):
        out = terms.apply_corrections("GitHub issue", {"GitHub": "GH", "GitHub issue": "GI"})
        self.assertEqual(out, "GI")

    def test_no_chaining(self):  # A3b:替换结果不被另一键二次命中
        out = terms.apply_corrections("a b", {"a": "b", "b": "c"})
        self.assertEqual(out, "b c")

    def test_empty_value_deletes(self):
        self.assertEqual(terms.apply_corrections("x字幕by索兰娅y", {"字幕by索兰娅": ""}), "xy")

    def test_ascii_with_punct_matches_literal(self):
        self.assertEqual(terms.apply_corrections("写c++代码", {"c++": "C++"}), "写C++代码")

    def test_casefold_conflict_last_wins(self):
        out = terms.apply_corrections("SQL", {"sql": "a", "SQL": "b"})
        self.assertEqual(out, "b")

    def test_invalid_entries_skipped_with_warning(self):
        with _mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = terms.apply_corrections("ok", {"": "x", 1: "y", "k": 2})
        self.assertEqual(out, "ok")
        self.assertIn("corrections", err.getvalue())


class TestTokenBudget(unittest.TestCase):
    """#33 A5-A6:词粒度预算截断(fake counter,不依赖 tokenizer)。"""

    WORDS = [f"词{i}" for i in range(60)]

    def test_budget_keeps_all_when_fits(self):
        p = terms.build_prompt(self.WORDS[:10], token_count=lambda s: 100)
        for w in self.WORDS[:10]:
            self.assertIn(w, p)

    def test_budget_stops_at_word_boundary(self):
        calls = {"n": 0}

        def fake(s):
            calls["n"] += 1
            return 150 if calls["n"] <= 12 else 999

        p = terms.build_prompt(self.WORDS, token_count=fake)
        kept = [w for w in self.WORDS if w in p]
        self.assertEqual(len(kept), 12)
        self.assertEqual(kept, self.WORDS[:12])

    def test_min_one_term_even_if_over_budget(self):
        p = terms.build_prompt(["超长词"], token_count=lambda s: 9999)
        self.assertIn("超长词", p)

    def test_none_counter_falls_back_to_30(self):
        with _mock.patch("sys.stderr", new_callable=io.StringIO):
            p = terms.build_prompt(self.WORDS, token_count=None)
        legacy = "以下是本次内容可能涉及的术语:" + "、".join(self.WORDS[:30]) + "。"
        self.assertEqual(p, legacy)

    def test_empty_terms_still_none(self):
        self.assertIsNone(terms.build_prompt([], token_count=lambda s: 5))


class TestProbeCache(unittest.TestCase):
    """#33 A6:探测结果进程内缓存(含失败),二次调用不重探。"""

    def test_probe_cached_including_failure(self):
        calls = {"n": 0}

        def fake_probe():
            calls["n"] += 1
            return None

        fresh = {"probed": False, "counter": None, "warned": False}
        with _mock.patch.object(terms, "_TOKENIZER_STATE", fresh), \
                _mock.patch.object(terms, "_probe_counter", fake_probe):
            self.assertIsNone(terms._token_count("x"))
            self.assertIsNone(terms._token_count("y"))
        self.assertEqual(calls["n"], 1)


class TestLoadTermsCorrections(unittest.TestCase):
    """#33 A7:corrections 键解析(缺失 -> {},合法 -> 原样)。"""

    def test_missing_key_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"terms": ["a"]}')
            cfg = terms.load_terms(p)
        self.assertEqual(cfg["corrections"], {})

    def test_corrections_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"corrections": {"sql": "skill"}}')
            cfg = terms.load_terms(p)
        self.assertEqual(cfg["corrections"], {"sql": "skill"})



def _fresh_state():
    return {"probed": False, "counter": None, "warned": False}


def _probe_available():
    """venv-only:任一探测路径命中 tokenizer.json 且 tokenizers 可导入。"""
    try:
        with _mock.patch.object(terms, "_TOKENIZER_STATE", _fresh_state()):
            return terms._probe_counter() is not None
    except Exception:
        return False


@unittest.skipUnless(_probe_available(), "no tokenizer.json reachable via probe paths")
class TestRealTokenizer(unittest.TestCase):
    """#33 A10:真实 tokenizer 计数(system python 下无 tokenizers -> skip)。"""

    # 复刻 2026-10-21 实测现役词表构成(30 词,~147 token)
    FIXTURE = ["zima", "jfox", "Claude", "Claude Code", "Kimi", "DeepSeek",
               "GitHub", "GitHub issue", "issue", "Boktionary", "Wiktionary",
               "vocabo", "pjob", "pi-coding-agent", "pi", "pi-agent board",
               "nvim", "skill", "spec", "review", "session", "智谱", "飞书",
               "永久笔记", "根因", "待确认项", "clear report", "workflow",
               "闪屏", "OpenVINO"]

    def test_real_fixture_within_budget_no_truncation(self):
        with _mock.patch.object(terms, "_TOKENIZER_STATE", _fresh_state()):
            p = terms.build_prompt(self.FIXTURE)
            n = terms._token_count(p)
        for w in self.FIXTURE:
            self.assertIn(w, p)
        self.assertGreater(n, 100)                  # 区间合理性,不逐字对齐 og.Tokenizer
        self.assertLessEqual(n, terms.MAX_PROMPT_TOKENS)

    def test_real_60_cjk_words_truncated(self):
        words = [f"词汇编号{i:02d}" for i in range(60)]
        with _mock.patch.object(terms, "_TOKENIZER_STATE", _fresh_state()):
            with _mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                p = terms.build_prompt(words)
            n = terms._token_count(p)
        kept = [w for w in words if w in p]
        self.assertLess(len(kept), 60)
        self.assertGreater(len(kept), 0)
        self.assertEqual(kept, words[:len(kept)])   # 词边界 + 保持顺序
        self.assertLessEqual(n, terms.MAX_PROMPT_TOKENS)
        self.assertIn("token budget", err.getvalue())



class TestCorrectionsWiring(unittest.TestCase):
    """#33 A9: voice-ptt 静态接线(GTK 依赖,不做端到端;功能接线由 test_engine 覆盖)。"""

    def test_voice_ptt_uses_corrections_and_raw_final_archive(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "voice-ptt.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("apply_corrections(", src)
        self.assertIn("archive_recording(wav, raw_text, final_text)", src)




class TestReviewHardening(unittest.TestCase):
    """CR nits #5/#6: boundary, warn-once, worker log string, probe path resolution."""

    def test_boundary_200_inclusive(self):
        # fake counter that reports exactly 200 for any prompt -> all kept (<=)
        p = terms.build_prompt(["甲", "乙"], token_count=lambda s: terms.MAX_PROMPT_TOKENS)
        self.assertIn("甲", p); self.assertIn("乙", p)

    def test_warn_once_on_fallback(self):
        import io as _io
        state = {"probed": True, "counter": None, "warned": False}
        with _mock.patch.object(terms, "_TOKENIZER_STATE", state):
            with _mock.patch("sys.stderr", new_callable=_io.StringIO) as err:
                terms.build_prompt(["a"], token_count=None)
                terms.build_prompt(["b"], token_count=None)
        self.assertEqual(err.getvalue().count("tokenizer unavailable"), 1)

    def test_truncation_log_line(self):
        import io as _io
        calls = {"n": 0}
        def fake(s):
            calls["n"] += 1
            return 100 if calls["n"] <= 3 else 999
        with _mock.patch("sys.stderr", new_callable=_io.StringIO) as err:
            terms.build_prompt(["w%d" % i for i in range(10)], token_count=fake)
        self.assertIn("terms: kept", err.getvalue())

    def test_worker_log_string(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "transcribe_worker.py"), encoding="utf-8").read()
        self.assertIn("corrections: {n_fixed} replaced", src)

    def test_probe_path_resolution(self):
        import sys as _sys, types
        fake_engine = types.ModuleType("engine")
        fake_engine.ENV_MODEL = "VOICE_INPUT_MODEL"
        fake_engine.engine_name = lambda env: env.get("VOICE_INPUT_ENGINE", "cuda")
        fake_engine.default_model = lambda name: "small-int8-ov" if name == "npu" else "large-v3"
        fake_engine.ov_snapshots_base = lambda m: f"/fake/ov/{m}"
        fake_engine.snapshots_base = lambda m: f"/fake/ct2/{m}"
        fake_tokenizers = types.ModuleType("tokenizers")
        class _Tok:
            @staticmethod
            def from_file(path):
                return types.SimpleNamespace(
                    encode=lambda t: types.SimpleNamespace(ids=[0] * 5))
        fake_tokenizers.Tokenizer = _Tok
        seen = {}
        def fake_glob(pat):
            seen["pat"] = pat
            return [pat.replace("*", "rev1")]
        with _mock.patch.dict(_sys.modules, {"engine": fake_engine,
                                             "tokenizers": fake_tokenizers}), \
             _mock.patch.object(terms.glob, "glob", fake_glob), \
             _mock.patch.dict(os.environ, {"VOICE_INPUT_ENGINE": "cuda"}, clear=False):
            counter = terms._probe_counter()
        self.assertIn("/fake/ct2/large-v3", seen["pat"])
        self.assertIsNotNone(counter)
        with _mock.patch.dict(_sys.modules, {"engine": fake_engine,
                                             "tokenizers": fake_tokenizers}), \
             _mock.patch.object(terms.glob, "glob", fake_glob), \
             _mock.patch.dict(os.environ, {"VOICE_INPUT_ENGINE": "cuda",
                                           "VOICE_INPUT_MODEL": "small"}, clear=False):
            terms._probe_counter()
        self.assertIn("/fake/ct2/small", seen["pat"])
        with _mock.patch.dict(_sys.modules, {"engine": fake_engine,
                                             "tokenizers": fake_tokenizers}), \
             _mock.patch.object(terms.glob, "glob", fake_glob), \
             _mock.patch.dict(os.environ, {"VOICE_INPUT_ENGINE": "npu"}, clear=False):
            terms._probe_counter()
        self.assertIn("/fake/ov/small-int8-ov", seen["pat"])


class TestPromptEcho(unittest.TestCase):
    """#43 A6: hotword prompt-echo detection (pure text, no audio)."""

    TERMS = ["Zima", "DeepSeek", "GitHub", "Kimi"]

    def test_loop_of_nineteen(self):
        text = "、".join(self.TERMS * 19)
        self.assertTrue(terms.looks_like_prompt_echo(text, self.TERMS))

    def test_genuine_enumeration_once(self):
        self.assertFalse(terms.looks_like_prompt_echo(
            "Zima、DeepSeek、GitHub、Kimi", self.TERMS))

    def test_normal_sentence_with_hotwords(self):
        self.assertFalse(terms.looks_like_prompt_echo(
            "帮我看一下 GitHub 上那个 issue 的根因，还有 Zima 的 PR", self.TERMS))

    def test_single_term_repeated_twice_is_not_echo(self):
        self.assertFalse(terms.looks_like_prompt_echo(
            "GitHub 上的 GitHub 仓库", self.TERMS))

    def test_single_term_repeated_four_times(self):
        self.assertTrue(terms.looks_like_prompt_echo(
            "GitHub GitHub GitHub GitHub 看一下", self.TERMS))

    def test_two_terms_twice_in_short_text(self):
        self.assertTrue(terms.looks_like_prompt_echo(
            "Zima Kimi Zima Kimi", self.TERMS))

    def test_periodic_sequence(self):
        self.assertTrue(terms.looks_like_prompt_echo(
            "、".join(self.TERMS * 2), self.TERMS))

    def test_ascii_terms_respect_word_boundaries(self):
        # "AI" must not match email/said/detail/again (#43 review P1-1)
        self.assertFalse(terms.looks_like_prompt_echo(
            "我发了 email，said 一句，detail 很长，again", ["AI"]))
        self.assertTrue(terms.looks_like_prompt_echo(
            "AI AI AI AI 又是 AI", ["AI"]))

    def test_long_periodic_sequence_only_rule_c(self):
        # 3 rounds of 4 terms: max count 3 (<4) and len > 60 -> only rule (c) fires
        text = "、".join(self.TERMS * 3) + "，" + "这是一段正常长度的中文补充说明用来超过长度上限"
        self.assertTrue(terms.looks_like_prompt_echo(text, self.TERMS))

    def test_hotword_dominated(self):
        self.assertTrue(terms.hotword_dominated("、".join(self.TERMS * 19), self.TERMS))
        self.assertFalse(terms.hotword_dominated(
            "DeepSeek 的 API 多少钱，DeepSeek 和 Kimi 比怎么样，"
            "DeepSeek 的模型多大，DeepSeek 上下文多长", self.TERMS))

    def test_empty_inputs(self):
        self.assertFalse(terms.looks_like_prompt_echo("", self.TERMS))
        self.assertFalse(terms.looks_like_prompt_echo("Zima Zima Zima Zima", []))
        self.assertFalse(terms.looks_like_prompt_echo("随便说点什么", self.TERMS))


if __name__ == "__main__":
    unittest.main()
