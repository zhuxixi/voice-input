# Terms Capacity + Deterministic Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 30-term cap with a real-token budget (200) and add a deterministic post-transcription corrections layer (`"corrections": {"sql": "skill"}`), wired into all three transcription entry points.

**Architecture:** All logic lands in `terms.py` (pure functions, stdlib at module top, lazy `engine`/`tokenizers` imports). Entry points call `apply_corrections` after building text; `voice-ptt.py` additionally separates raw/final for archiving. `engine.py`, `vad_trim.py`, `archive.py` untouched.

**Tech Stack:** Python 3.12, stdlib (`re`, `json`, `os`), `tokenizers` (venv, lazy) for real counting.

**Spec:** `docs/superpowers/specs/2026-10-23-terms-capacity-corrections-design.md` (issue #33, cross-reviewed v3)

## Global Constraints

- Corrections never raise and never block transcription (spec D1/D5).
- Single-pass no-chaining replacement via ONE combined alternation regex, longest-key-first (spec D1, pinned by A3b).
- ASCII word keys (only `[A-Za-z0-9_-]`) get word boundaries + case-insensitive; keys containing anything else (CJK, `c++`, `foo.bar`) match as bare literals (spec D1).
- Empty value = deletion; empty key or non-str key/value = skip + one stderr warning (spec D1).
- `MAX_PROMPT_TOKENS = 200`, `FALLBACK_TERM_CAP = 30`, counted as `len(ids)` of `" " + prompt` (spec D2).
- Probe tokenizer via `engine.ov_snapshots_base`/`engine.snapshots_base` with model resolved as `os.environ.get(engine.ENV_MODEL) or engine.default_model(engine.engine_name(os.environ))`; engine branch picks which base; probe result cached per process including failures (spec D2).
- `terms.py` stays stdlib at module top; `engine`/`tokenizers` imported lazily inside functions.
- Test classes `Test*`; system-python tests need no venv except `TestRealTokenizer` (skipUnless probe hits a tokenizer.json).
- Ruling (post-review): add pure helper `apply_corrections_counted(text, corrections) -> tuple[str, int]`; `apply_corrections(text, corrections) -> str` delegates — workers log the count. Signature in spec unchanged.

## Review Focus

- **Chained replacement**: `{"a":"b","b":"c"}` must map `a`→`b`, never `c` — single `re.sub`, not sequential replaces. Covered by A3b.
- **Case-fold key conflicts**: `SQL` and `sql` keys — last one in dict order wins. Covered by A3.
- **Regression of existing 30-cap behavior**: `token_count=None` fallback must equal legacy `[:30]` byte-for-byte. Covered by A6 + updated legacy test.
- **Worker degradation**: terms assemble failure must still transcribe (no prompt, no corrections). Existing worker test covers prompt degrade; add corrections to same pattern.
- **Archive raw/final**: voice-ptt must archive the pre-correction text as raw. Covered by A9 static assertion.

---

### Task 1: `terms.py` — corrections + token budget (A1–A7, A10)

**Files:**
- Modify: `terms.py`
- Modify: `test_terms.py` (new classes; update 4 existing equality assertions; pin legacy test to `token_count=None`)

**Interfaces:**
- Produces: `apply_corrections(text, corrections) -> str`, `apply_corrections_counted(text, corrections) -> tuple[str, int]`, `MAX_PROMPT_TOKENS=200`, `FALLBACK_TERM_CAP=30`, `_token_count(text) -> int | None` (probe+cache), `build_prompt(terms, token_count=_token_count) -> str | None`, `load_terms` now defaults `"corrections": {}` in ALL return paths.

- [ ] **Step 1: Write the failing tests**

Append to `test_terms.py` (and update the four `assertEqual(cfg, {"terms": [], "hotwords": None})` in `TestLoadTermsRobustness` / missing-file / invalid-json tests to include `"corrections": {}`; update `test_truncates_to_30_terms` to call `build_prompt(terms, token_count=None)`):

```python
import unittest.mock as _mock


class TestApplyCorrections(unittest.TestCase):
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

    def test_no_chaining(self):  # A3b
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
        with _mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            out = terms.apply_corrections("ok", {"": "x", 1: "y", "k": 2})
        self.assertEqual(out, "ok")
        self.assertIn("corrections", err.getvalue())


class TestTokenBudget(unittest.TestCase):
    WORDS = [f"词{i}" for i in range(60)]

    def test_budget_keeps_all_when_fits(self):
        fake = lambda s: 100  # noqa: E731
        p = terms.build_prompt(self.WORDS[:10], token_count=fake)
        for w in self.WORDS[:10]:
            self.assertIn(w, p)

    def test_budget_stops_at_word_boundary(self):
        counts = {}
        def fake(s):
            return 150 if counts.setdefault("n", len(counts)) < 12 else 999
        p = terms.build_prompt(self.WORDS, token_count=fake)
        kept = [w for w in self.WORDS if w in p]
        self.assertEqual(len(kept), 12)
        self.assertEqual(kept, self.WORDS[:12])

    def test_min_one_term_even_if_over_budget(self):
        p = terms.build_prompt(["超长词"], token_count=lambda s: 9999)
        self.assertIn("超长词", p)

    def test_none_counter_falls_back_to_30(self):
        p = terms.build_prompt(self.WORDS, token_count=None)
        legacy = "以下是本次内容可能涉及的术语:" + "、".join(self.WORDS[:30]) + "。"
        self.assertEqual(p, legacy)

    def test_empty_terms_still_none(self):
        self.assertIsNone(terms.build_prompt([], token_count=lambda s: 5))


class TestProbeCache(unittest.TestCase):
    def test_probe_cached_including_failure(self):
        calls = {"n": 0}
        def fake_probe():
            calls["n"] += 1
            return None
        with _mock.patch.object(terms, "_probe_counter", fake_probe):
            self.assertIsNone(terms._token_count("x"))
            self.assertIsNone(terms._token_count("y"))
        self.assertEqual(calls["n"], 1)


class TestLoadTermsCorrections(unittest.TestCase):
    def test_missing_key_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            open(p, "w").write('{"terms": ["a"]}')
            cfg = terms.load_terms(p)
        self.assertEqual(cfg["corrections"], {})

    def test_corrections_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            open(p, "w").write('{"corrections": {"sql": "skill"}}')
            self.assertEqual(terms.load_terms(p)["corrections"], {"sql": "skill"})
```

Adjust imports at top of `test_terms.py` as needed (`tempfile`, `os`).

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest test_terms -v`
Expected: FAIL/ERROR on the new classes (`AttributeError: module 'terms' has no attribute 'apply_corrections'`).

- [ ] **Step 3: Implement in `terms.py`**

```python
import re  # add to imports

MAX_PROMPT_TOKENS = 200   # Whisper initial_prompt hard cap 224 minus safety margin
FALLBACK_TERM_CAP = 30    # legacy behavior when no tokenizer is available
_ASCII_WORD_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def apply_corrections_counted(text, corrections):
    """Single-pass deterministic replacement. Returns (new_text, n_replaced).

    One combined alternation regex (longest key first) + one re.sub => no
    chaining: a replacement result is never re-matched by another key.
    """
    if not text or not isinstance(corrections, dict):
        return text, 0
    table = {}
    keys = []
    for key, value in corrections.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            print(f"[voice-input] corrections: skip invalid entry {key!r}", file=sys.stderr)
            continue
        table[key.casefold()] = value          # casefold conflict: last wins
        keys.append(key)
    if not keys:
        return text, 0

    def piece(key):
        if _ASCII_WORD_KEY.match(key):
            return r"(?<![A-Za-z0-9_-])" + re.escape(key) + r"(?![A-Za-z0-9_-])"
        return re.escape(key)                  # CJK / c++ / foo.bar: bare literal

    pattern = re.compile("|".join(piece(k) for k in sorted(keys, key=len, reverse=True)),
                         re.IGNORECASE)
    count = {"n": 0}
    def sub(match):
        count["n"] += 1
        return table.get(match.group(0).casefold(), match.group(0))
    return pattern.sub(sub, text), count["n"]


def apply_corrections(text, corrections):
    """Spec D1 public API: corrected text (drop the count)."""
    return apply_corrections_counted(text, corrections)[0]


_TOKENIZER_STATE = {"probed": False, "counter": None, "warned": False}


def _probe_counter():
    """Find a whisper tokenizer.json via engine helpers; None if unavailable.

    Model resolution mirrors _model_label: env override beats default. Result
    cached per process (including failure) — resident workers must not re-probe
    on every dictation.
    """
    try:
        import engine
    except Exception:
        return None
    try:
        env = dict(os.environ)
        name = engine.engine_name(env)
        model = os.environ.get(engine.ENV_MODEL) or engine.default_model(name)
        base = engine.ov_snapshots_base(model) if name == "npu" else engine.snapshots_base(model)
        import glob as _glob
        candidates = sorted(_glob.glob(os.path.join(base, "*", "tokenizer.json")))
        if not candidates:
            return None
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(candidates[-1])
        return lambda text: len(tok.encode(" " + text.strip()).ids)
    except Exception:
        return None


def _token_count(text):
    if not _TOKENIZER_STATE["probed"]:
        _TOKENIZER_STATE["probed"] = True
        _TOKENIZER_STATE["counter"] = _probe_counter()
    counter = _TOKENIZER_STATE["counter"]
    return counter(text) if counter else None


def build_prompt(terms, token_count=_token_count):
    """Greedy word-granular truncation under MAX_PROMPT_TOKENS (spec D2).

    token_count=None (or a probe miss) falls back to legacy terms[:30] — byte
    equal to the previous behavior. Always keeps at least one term.
    """
    if not isinstance(terms, list) or not terms:
        return None

    def count(prompt):
        if token_count is None:
            return None
        return token_count(prompt)

    intro = "以下是本次内容可能涉及的术语:"
    kept = []
    for term in terms:
        candidate = "、".join([*[str(t) for t in kept], str(term)])
        prompt = intro + candidate + "。"
        n = count(prompt)
        if n is None:
            if not _TOKENIZER_STATE["warned"]:
                _TOKENIZER_STATE["warned"] = True
                print("[voice-input] terms: tokenizer unavailable, "
                      f"falling back to {FALLBACK_TERM_CAP}-word cap", file=sys.stderr)
            kept = [str(t) for t in terms[:FALLBACK_TERM_CAP]]
            return intro + "、".join(kept) + "。"
        if n <= MAX_PROMPT_TOKENS or not kept:
            kept.append(str(term))
            if n > MAX_PROMPT_TOKENS:   # first term alone over budget: keep it, stop
                break
            continue
        print(f"[voice-input] terms: kept {len(kept)}/{len(terms)} words "
              f"(token budget {MAX_PROMPT_TOKENS})", file=sys.stderr)
        break
    return intro + "、".join(kept) + "。"
```

And in `load_terms`: change both degrade literals to `{"terms": [], "hotwords": None, "corrections": {}}` and add `cfg.setdefault("corrections", {})` after the existing setdefaults.

- [ ] **Step 4: Run module tests**

Run: `python3 -m unittest test_terms -v`
Expected: PASS (all old + new). Then `venv/bin/python3 -m unittest test_terms -v` — must also PASS (existing truncation test now pinned to `token_count=None`, so no env dependence).

- [ ] **Step 5: Commit**

```bash
git add terms.py test_terms.py
git commit -m "feat(terms): deterministic corrections + token-budget prompt (#33)"
```

---

### Task 2: real-tokenizer tests (A10)

**Files:**
- Modify: `test_terms.py` (TestRealTokenizer)

- [ ] **Step 1: Add the class**

```python
def _probe_available():
    try:
        import sys as _s; _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import terms as _t
        with _mock.patch.object(_t, "_TOKENIZER_STATE", {"probed": False, "counter": None, "warned": False}):
            return _t._probe_counter() is not None
    except Exception:
        return False


@unittest.skipUnless(_probe_available(), "no tokenizer.json reachable via probe paths")
class TestRealTokenizer(unittest.TestCase):
    FIXTURE = ["zima", "jfox", "Claude", "Claude Code", "Kimi", "DeepSeek",
               "GitHub", "GitHub issue", "issue", "Boktionary", "Wiktionary",
               "vocabo", "pjob", "pi-coding-agent", "pi", "pi-agent board",
               "nvim", "skill", "spec", "review", "session", "智谱", "飞书",
               "永久笔记", "根因", "待确认项", "clear report", "workflow",
               "闪屏", "OpenVINO"]  # replica of the 2026-10-21 live list (~147 tokens)

    def test_real_fixture_within_budget_no_truncation(self):
        with _mock.patch.object(terms, "_TOKENIZER_STATE",
                                {"probed": False, "counter": None, "warned": False}):
            p = terms.build_prompt(self.FIXTURE)
        for w in self.FIXTURE:
            self.assertIn(w, p)

    def test_real_60_cjk_words_truncated(self):
        words = [f"词汇编号{i:02d}" for i in range(60)]
        with _mock.patch.object(terms, "_TOKENIZER_STATE",
                                {"probed": False, "counter": None, "warned": False}):
            with _mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
                p = terms.build_prompt(words)
        kept = [w for w in words if w in p]
        self.assertLess(len(kept), 60)
        self.assertGreater(len(kept), 30)   # the whole point of the budget
        self.assertEqual(kept, words[:len(kept)])
```

- [ ] **Step 2: Run under venv**

Run: `venv/bin/python3 -m unittest test_terms.TestRealTokenizer -v`
Expected: PASS (2 tests). Under system python the class skips.

- [ ] **Step 3: Commit**

```bash
git add test_terms.py
git commit -m "test(terms): real-tokenizer budget tests (#33)"
```

---

### Task 3: entry-point wiring (A8, A9)

**Files:**
- Modify: `transcribe_worker.py` (terms try-block ~L99; after text join ~L110)
- Modify: `test_transcribe_worker.py` (new `TestCorrectionsWiring`)
- Modify: `transcribe_once.py` (terms try-block ~L82; after text join ~L98)
- Modify: `voice-ptt.py` (terms try-block ~L241; text join ~L261; archive call ~L271)
- Modify: `test_engine.py` (`TestTranscribeOnceTerms` new case)
- Modify: `test_terms.py` (`TestCorrectionsWiring` static class)

**Interfaces:**
- Consumes: `apply_corrections_counted` (worker logs count), `apply_corrections` (CLIs).
- Produces: worker responses carry corrected text; `transcribe_once` stdout corrected; `voice-ptt` archives `(raw, final)` and pastes `final`.

- [ ] **Step 1: Failing tests**

`test_transcribe_worker.py` — inside `TestVadWiring`-style harness add:

```python
class TestCorrectionsWiring(WorkerHarness):
    TERMS = '{"terms": [], "corrections": {"批诏": "pjob", "sql": "skill"}}'

    def test_corrections_applied_to_response(self):
        self.start()
        self.read()
        self.parent.write(wp.encode_request(1, "/tmp/a.wav"))
        self.parent.flush()
        msg = self.read()
        self.assertEqual(msg, {"id": 1, "text": "你好"})
```

(with `FakeModel` returning `"批诏 sql"` for non-silence: extend FakeModel's text to `"批诏 sql"` — simpler: point the request at `/tmp/a.wav`; FakeModel returns `"你好"` unless wav endswith silence. Change FakeModel text to `"批诏 sql"` would break other tests' assertions (`你好`). Instead write a dedicated fake: override `terms_loader` file content is not enough — the model text is fixed. Cleanest: add `self.model.text = "批诏 sql"` attribute support:

In `FakeModel.transcribe`, use `text = "" if wav.endswith("silence.wav") else getattr(self, "text", "你好")`.)

Then assert `{"id": 1, "text": "pjob skill"}`.

`test_engine.py` — add to `TestTranscribeOnceTerms`:

```python
    def test_corrections_applied_to_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            tp = os.path.join(d, "t.json")
            open(tp, "w", encoding="utf-8").write(
                '{"corrections": {"你好": "nihao"}}')
            p, markers = self._run_main(tp)
        self.assertEqual(markers["RC"], "0")
        self.assertEqual(markers["OUT"], "nihao")
```

(FakeModel returns `' 你好 '` → after strip/join + corrections → `nihao`.)

`test_terms.py` — static class:

```python
class TestCorrectionsWiring(unittest.TestCase):
    def test_voice_ptt_uses_corrections_and_raw_final_archive(self):
        root = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(root, "voice-ptt.py"), encoding="utf-8").read()
        self.assertIn("apply_corrections(", src)
        self.assertIn("archive_recording(wav, raw_text, final_text)", src)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest test_transcribe_worker.TestCorrectionsWiring test_terms.TestCorrectionsWiring -v` and `venv/bin/python3 -m unittest test_engine.TestTranscribeOnceTerms -v`
Expected: FAIL (worker returns uncorrected text; static marker missing; OUT == `你好`).

- [ ] **Step 3: Wire worker**

In `transcribe_worker.py` try-block add `corrections = cfg.get("corrections")` (init `corrections = None` beside `prompt, extra_kw`); after the `with vad_trim...` block, before `conn.write`:

```python
            text, n_fixed = terms.apply_corrections_counted(text, corrections)
            if n_fixed:
                log(f"[voice-input] corrections: {n_fixed} replaced")
```

Add `apply_corrections_counted` to the existing `from terms import (...)` list.

- [ ] **Step 4: Wire transcribe_once**

In the terms try-block add `corrections = cfg.get("corrections", {})` (init `corrections = {}` in the except); after `text = "".join(...).strip()` (inside the with-block) add:

```python
        text = terms.apply_corrections(text, corrections)
```

(or import the name directly alongside the existing terms imports).

- [ ] **Step 5: Wire voice-ptt**

In the terms try-block capture `corrections = cfg.get("corrections", {})` (except → `corrections = {}`); rename the post-join variable flow:

```python
                raw_text = "".join(s.text for s in segments).strip()
                text = apply_corrections(raw_text, corrections)
```

(no-speech branch: `raw_text = text = ""`). Archive call becomes `archive_recording(wav, raw_text, text)`; the pre-declared `text = ""` init stays. Import `apply_corrections` with the other terms imports.

- [ ] **Step 6: Run all wiring tests + suites**

```bash
python3 -m unittest test_transcribe_worker test_terms -v
venv/bin/python3 -m unittest test_engine -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add transcribe_worker.py transcribe_once.py voice-ptt.py test_transcribe_worker.py test_engine.py test_terms.py
git commit -m "feat(wiring): corrections at all three transcription entry points (#33)"
```

---

### Task 4: README sync + full regression (A11, U-prep)

**Files:**
- Modify: `README.md`, `README.zh-CN.md` (Configuration row, Custom vocabulary section, testing unchanged)

- [ ] **Step 1: README.md**

Configuration table row after `VOICE_INPUT_VAD`:

```markdown
| `~/.config/voice-input/terms.json` → `"corrections"` | (none) | Post-transcription deterministic replacements, e.g. `{"sql": "skill", "批诏": "pjob"}`; ASCII keys match whole words case-insensitively, other keys as literals; wrong→right only, applied in one pass (no chaining) | Edit the file |
```

Custom vocabulary section append:

```markdown
- **Corrections** (`"corrections"` key): deterministic wrong→right replacements applied
  after transcription — the safety net hotwords can't provide for stable mishearings
  (`skill`→`sql`, `pjob`→`批诏`). One pass, longest key first; put only wrong forms as
  keys. The prompt now fits up to `MAX_PROMPT_TOKENS = 200` tokens (measured with the
  bundled tokenizer; falls back to the legacy 30-word cap when no tokenizer is found).
```

- [ ] **Step 2: Mirror both blocks in `README.zh-CN.md`**（配置表同一行中文 + 自定义词汇小节补「纠错」与「token 预算」说明）。

- [ ] **Step 3: Full regression**

```bash
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim -v
venv/bin/python3 -m unittest test_engine test_terms -v
```
Expected: both PASS (A11a + A11b; `TestRealTokenizer` runs under venv, skips under system python).

- [ ] **Step 4: Commit**

```bash
git add README.md README.zh-CN.md
git commit -m "docs: corrections layer and token-budget terms (#33)"
```
