"""#43 A7/A8/A8b/A9: prompt-echo guard around model.transcribe."""

import unittest

from transcribe_guard import transcribe_guarded

TERMS = ["Zima", "DeepSeek", "GitHub", "Kimi"]
ECHO = "、".join(TERMS * 19)


class FakeModel:
    """Records every call; returns queued texts (or raises queued errors)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def transcribe(self, wav, language="zh", initial_prompt=None, **kw):
        self.calls.append({"wav": wav, "language": language,
                           "initial_prompt": initial_prompt, **kw})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        segment = type("Seg", (), {"text": result})()
        return [segment], type("Info", (), {"language": language})()


class TestTranscribeGuard(unittest.TestCase):
    def setUp(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def run_guard(self, model, prompt="词表：Zima、DeepSeek。"):
        return transcribe_guarded(model, "/tmp/a.wav", prompt=prompt, extra_kw={},
                                  terms=TERMS, log=self.log)

    def test_clean_transcript_calls_model_once(self):
        model = FakeModel("帮我看一下这个 PR")
        self.assertEqual(self.run_guard(model), ("帮我看一下这个 PR", "none"))
        self.assertEqual(len(model.calls), 1)

    def test_echo_retries_without_prompt(self):
        model = FakeModel(ECHO, "帮我看一下这个 PR")
        text, mode = self.run_guard(model)
        self.assertEqual((text, mode), ("帮我看一下这个 PR", "retried"))
        self.assertEqual(len(model.calls), 2)
        self.assertIsNone(model.calls[1]["initial_prompt"])
        self.assertEqual(model.calls[1]["language"], "zh")
        self.assertTrue(any("echo guard" in line for line in self.logs))

    def test_echo_persisting_without_prompt_is_dropped(self):
        model = FakeModel(ECHO, ECHO)
        self.assertEqual(self.run_guard(model), ("", "dropped"))
        self.assertEqual(len(model.calls), 2)

    def test_echo_like_but_not_dominated_keeps_text(self):
        # A real sentence mentioning hotwords repeatedly must survive the retry.
        sentence = ("DeepSeek 的 API 多少钱，DeepSeek 和 Kimi 比怎么样，"
                    "DeepSeek 的模型多大，DeepSeek 上下文多长")
        model = FakeModel(sentence, sentence)
        self.assertEqual(self.run_guard(model), (sentence, "retried"))
        self.assertEqual(len(model.calls), 2)

    def test_retry_drops_hotwords_kwarg(self):
        model = FakeModel(ECHO, "正常结果")
        transcribe_guarded(model, "/tmp/a.wav", prompt="词表", extra_kw={"hotwords": "Zima"},
                           terms=TERMS, log=self.log)
        self.assertNotIn("hotwords", model.calls[1])

    def test_retry_exception_propagates(self):
        model = FakeModel(ECHO, RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.run_guard(model)

    def test_no_prompt_skips_the_guard(self):
        model = FakeModel(ECHO)
        self.assertEqual(self.run_guard(model, prompt=None), (ECHO, "none"))
        self.assertEqual(len(model.calls), 1)

    def test_no_terms_skips_the_guard(self):
        model = FakeModel(ECHO)
        text, mode = transcribe_guarded(model, "/tmp/a.wav", prompt="词表", extra_kw={},
                                        terms=[], log=self.log)
        self.assertEqual((text, mode), (ECHO, "none"))
        self.assertEqual(len(model.calls), 1)


if __name__ == "__main__":
    unittest.main()
