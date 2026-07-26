# 语音输入专有名词识别增强 (Phase 1 纯 terms) 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 voice-input 的 Whisper 转写接入 `initial_prompt` 软热词,把 13 个高频专有名词(zima/jfox/Claude…)作为解码上文,降低 OOV 误识别。

**Architecture:** 新建独立模块 `terms.py`(3 个纯函数,无 GTK 依赖,可独立单测),`voice-ptt.py` 在 `stop_recording` 的 `transcribe` 调用处加 `initial_prompt` + 可选 `hotwords`。纯 terms 方案,不做 corrections 后处理,`archive.py` 零改动。

**Tech Stack:** Python 3.12, faster-whisper 1.2.1, unittest(标准库), XDG 配置 JSON。

**Spec:** `docs/2026-07-18-term-correction-design.md` (v2)

## Global Constraints

- faster-whisper **1.2.1**,transcribe 的参数名是 **`initial_prompt`**(不是 `prompt`,照搬 spec v1 会 TypeError);`hotwords` 参数同版本可用
- **不重训/不换引擎**;热词是增强,**任何配置问题不得阻断转写**(降级到不传 prompt)
- **纯 terms**: 不实现 corrections/apply_corrections;`raw==final`,归档调用不动
- `~/.config/voice-input/terms.json` 是用户配置,**不进仓库**(在 home 下,不在 repo 目录)
- 测试用 **unittest**(跟 `test_archive.py`/`test_media_pause.py` 一致),venv 无 pytest;跑测试用 `python -m unittest`
- `main` 受保护: 所有改动在 worktree(`.claude/worktrees/issue-4-term-prompt/`),禁止 `git checkout -b`
- 代码风格跟 `archive.py`/`voice-ptt.py` 一致: 中文注释,stderr 警告带 `[voice-input]` 前缀,类型注解

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `terms.py` | 词表加载 + prompt 构造 + transcribe 参数组装。纯标准库(json/os/sys),无 GTK | 新建 |
| `test_terms.py` | `terms.py` 的 unittest 单测 | 新建 |
| `voice-ptt.py` | 第 20 行后加 import;第 179 行 `transcribe` 加 `initial_prompt`/可选 `hotwords` | 改 |
| `~/.config/voice-input/terms.json` | 13 个 terms 种子(用户配置,不进仓库) | 新建 |

`terms.py` 接口(后续 task 依赖这些确切签名):

```python
DEFAULT_TERMS_PATH = os.path.expanduser("~/.config/voice-input/terms.json")
def load_terms(path: str = DEFAULT_TERMS_PATH) -> dict: ...        # -> {"terms":[...],"hotwords":None|list}
def build_prompt(terms: list) -> str | None: ...                    # 空 -> None
def build_transcribe_kwargs(cfg: dict) -> dict: ...                 # hotwords 空 -> {}
```

---

### Task 1: `terms.py` 骨架 + `load_terms`

**Files:**
- Create: `terms.py`
- Create: `test_terms.py`
- Test: `test_terms.py::TestLoadTerms`

**Interfaces:**
- Produces: `DEFAULT_TERMS_PATH`, `load_terms(path) -> dict`(`{"terms": list, "hotwords": None}`)

- [ ] **Step 1: 写失败测试 `test_terms.py`**

```python
import json
import os
import tempfile
import unittest

from terms import DEFAULT_TERMS_PATH, load_terms


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_terms.TestLoadTerms -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'terms'`

- [ ] **Step 3: 写 `terms.py` 最小实现**

```python
"""专有名词软热词: 加载词表 + 构造 initial_prompt + 组装 transcribe 参数。

设计见 docs/2026-07-18-term-correction-design.md (v2)。纯标准库,无 GTK 依赖,可独立单测。
核心原则: 热词是增强,任何配置问题都不得阻断转写(降级到不传 prompt)。
"""

import json
import os
import sys

DEFAULT_TERMS_PATH = os.path.expanduser("~/.config/voice-input/terms.json")


def load_terms(path: str = DEFAULT_TERMS_PATH) -> dict:
    """读 terms.json。文件缺失/JSON 错 -> 返回空配置,不抛异常(降级)。

    返回 dict 至少含 "terms"(list) 与 "hotwords"(None|list)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("terms", [])
        cfg.setdefault("hotwords", None)
        return cfg
    except FileNotFoundError:
        return {"terms": [], "hotwords": None}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[voice-input] terms.json parse failed: {e}", file=sys.stderr)
        return {"terms": [], "hotwords": None}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_terms.TestLoadTerms -v`
Expected: PASS(4 tests)

- [ ] **Step 5: Commit**

```bash
git add terms.py test_terms.py
git commit -m "feat(terms): 新增词表加载 load_terms (#4)"
```

---

### Task 2: `build_prompt`

**Files:**
- Modify: `terms.py`(加 `build_prompt`)
- Modify: `test_terms.py`(加 `TestBuildPrompt`)

**Interfaces:**
- Consumes: `DEFAULT_TERMS_PATH`(Task 1)
- Produces: `build_prompt(terms: list) -> str | None`

- [ ] **Step 1: 写失败测试**

在 `test_terms.py` 顶部 import 加 `build_prompt`,并在 `if __name__` 前加:

```python
from terms import build_prompt


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
        # 前 30 个在,第 31 个(term30)不在
        self.assertIn("term29", prompt)
        self.assertNotIn("term30", prompt)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_terms.TestBuildPrompt -v`
Expected: FAIL with `ImportError: cannot import name 'build_prompt'`

- [ ] **Step 3: 写 `build_prompt`(加到 `terms.py` `load_terms` 后)**

```python
def build_prompt(terms: list) -> "str | None":
    """把 terms 嵌入一句自然中文,作为 Whisper initial_prompt(解码上文)。

    空 terms -> None(transcribe 不传 prompt,等价现状)。截断到前 30 词控 token
    (Whisper initial_prompt 上限 224 token)。措辞(中文句包装 vs 纯词列表)是
    实测调优点,合并后用户可按效果调整。
    """
    if not terms:
        return None
    sample = terms[:30]
    return "以下是本次内容可能涉及的术语:" + "、".join(sample) + "。"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_terms.TestBuildPrompt -v`
Expected: PASS(3 tests)

- [ ] **Step 5: Commit**

```bash
git add terms.py test_terms.py
git commit -m "feat(terms): 新增 build_prompt 构造 initial_prompt (#4)"
```

---

### Task 3: `build_transcribe_kwargs`

**Files:**
- Modify: `terms.py`(加 `build_transcribe_kwargs`)
- Modify: `test_terms.py`(加 `TestBuildTranscribeKwargs`)

**Interfaces:**
- Consumes: `load_terms` 返回的 cfg(Task 1)
- Produces: `build_transcribe_kwargs(cfg: dict) -> dict`

- [ ] **Step 1: 写失败测试**

import 加 `build_transcribe_kwargs`,加测试类:

```python
from terms import build_transcribe_kwargs


class TestBuildTranscribeKwargs(unittest.TestCase):
    def test_hotwords_none_returns_empty(self):
        # hotwords=None/缺失 -> 空 dict(不传 hotwords,等价现状)
        self.assertEqual(build_transcribe_kwargs({"hotwords": None}), {})
        self.assertEqual(build_transcribe_kwargs({}), {})

    def test_hotwords_empty_list_returns_empty(self):
        self.assertEqual(build_transcribe_kwargs({"hotwords": []}), {})

    def test_hotwords_present_returns_kwarg(self):
        kw = build_transcribe_kwargs({"hotwords": ["zima", "jfox"]})
        self.assertEqual(kw, {"hotwords": ["zima", "jfox"]})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_terms.TestBuildTranscribeKwargs -v`
Expected: FAIL with `ImportError: cannot import name 'build_transcribe_kwargs'`

- [ ] **Step 3: 写 `build_transcribe_kwargs`(加到 `terms.py` `build_prompt` 后)**

```python
def build_transcribe_kwargs(cfg: dict) -> dict:
    """组装 transcribe 的可选参数。默认空 dict(只传 language + initial_prompt)。

    hotwords 为 None/空/False 时不传(等价现状);有值时返回 {"hotwords": [...]}。
    把可选参数组装集中一处,voice-ptt 调用处用 ** 展开。
    """
    kw: dict = {}
    hotwords = cfg.get("hotwords")
    if hotwords:
        kw["hotwords"] = hotwords
    return kw
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_terms.TestBuildTranscribeKwargs -v`
Expected: PASS(3 tests)

- [ ] **Step 5: 跑全部 terms 测试确认无回归**

Run: `python -m unittest test_terms -v`
Expected: PASS(10 tests: 4+3+3)

- [ ] **Step 6: Commit**

```bash
git add terms.py test_terms.py
git commit -m "feat(terms): 新增 build_transcribe_kwargs 组装 hotwords (#4)"
```

---

### Task 4: `voice-ptt.py` 接入

**Files:**
- Modify: `voice-ptt.py:20`(import 区,`from media_pause import…` 后加一行)
- Modify: `voice-ptt.py:178-179`(`stop_recording` 内 `transcribe` 调用)

**Interfaces:**
- Consumes: `load_terms`, `build_prompt`, `build_transcribe_kwargs`(Task 1-3)
- 端到端验证(实际录音)由用户合并到 main 后手工做,不在此 task 阻断(见 Spec §8)

**注意:** `voice-ptt.py` 顶部 `import gi`/GTK 导致无法独立单测,本 task 用静态检查(py_compile + import terms)+ 手工核对替代端到端。

- [ ] **Step 1: 加 import(第 20 行 `from media_pause import…` 之后,`import gi` 之前)**

```python
from media_pause import pause_playing, resume, PAUSE_MEDIA_ENABLED
from terms import load_terms, build_prompt, build_transcribe_kwargs
import gi
```

- [ ] **Step 2: 改 `transcribe` 调用(约第 178-179 行,`stop_recording` 内 `try` 块)**

改前:
```python
    try:
        m = load_model()
        segments, info = m.transcribe(WAVFILE, language="zh")
        text = "".join(s.text for s in segments).strip()
```

改后:
```python
    try:
        m = load_model()
        cfg = load_terms()
        segments, info = m.transcribe(
            WAVFILE,
            language="zh",
            initial_prompt=build_prompt(cfg.get("terms", [])),
            **build_transcribe_kwargs(cfg),
        )
        text = "".join(s.text for s in segments).strip()
```

第 188 行 `archive_recording(WAVFILE, text, text)` **不动**(纯 terms,raw==final)。

- [ ] **Step 3: 静态验证 — 语法 + import**

Run: `python -m py_compile voice-ptt.py && python -c "from terms import load_terms, build_prompt, build_transcribe_kwargs; print(build_prompt(['zima','jfox']))"`
Expected: 无异常,打印含 zima/jfox 的中文句

- [ ] **Step 4: 静态验证 — voice-ptt 在缺 terms.json 时仍能正常组装(降级路径)**

Run: `python -c "from terms import load_terms, build_prompt, build_transcribe_kwargs; cfg=load_terms('/nonexistent'); print('prompt=', build_prompt(cfg.get('terms',[])), 'kw=', build_transcribe_kwargs(cfg))"`
Expected: `prompt= None kw= {}`(文件缺失降级到不传 prompt/hotwords)

- [ ] **Step 5: 跑全部单测确认无回归**

Run: `python -m unittest test_terms test_archive -v`
Expected: PASS(terms 10 + archive 原有用例)。`test_media_pause` 依赖 D-Bus/MPRIS,环境受限时单独跳过,非本次改动对象。

- [ ] **Step 6: Commit**

```bash
git add voice-ptt.py
git commit -m "feat(voice-ptt): transcribe 接入 initial_prompt 软热词 (#4)"
```

---

### Task 5: 创建 terms.json 种子(用户配置,不进仓库)

**Files:**
- Create: `~/.config/voice-input/terms.json`(在 home 下,**不在 repo**,不会被 git 跟踪)

**说明:** 这是用户配置,放在 XDG 目录。在 worktree 里创建它不影响 repo(worktree 路径在 repo 内,但此文件写到 `~/.config/`,绝对路径在 repo 外)。

- [ ] **Step 1: 建 `~/.config/voice-input/` 目录 + 写 terms.json**

```bash
mkdir -p ~/.config/voice-input
cat > ~/.config/voice-input/terms.json << 'EOF'
{
  "terms": [
    "zima", "jfox", "Claude", "Kimi", "DeepSeek", "GitHub",
    "Boktionary", "Wiktionary",
    "skill", "daemon", "babysit", "fragments", "transcript"
  ],
  "hotwords": null
}
EOF
```

- [ ] **Step 2: 验证 load_terms 能读到它**

Run: `python -c "from terms import load_terms; cfg=load_terms(); print('terms:', len(cfg['terms']), '词; hotwords:', cfg['hotwords'])"`
Expected: `terms: 13 词; hotwords: None`

- [ ] **Step 3: 确认它不在 git 跟踪范围(在 home 不在 repo)**

Run: `git status --short`
Expected: `~/.config/voice-input/terms.json` 不出现(它在 repo 外);仅 `terms.py`/`test_terms.py`/`voice-ptt.py` 等改动

- 无 commit(此文件在 repo 外)。

---

## 合并后用户实测(不在实现周期内,见 Spec §8)

- **历史音频回放**: `recordings/2026-07-19_225840/audio.wav`(原始转写"我用Z码做审查"),重跑加 terms 的 transcribe,看是否输出 `zima`
- 日常录"我用 zima 做代码审查"确认输出 `zima` 非 `Z码`
- 删 terms.json 验证降级;改 terms.json 验证热加载
- initial_prompt 效果不够时,把 terms 复制进 `hotwords` 字段对比
