# NPU Hotwords (stateful pipeline + terms wiring) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** NPU 引擎默认启用热词——`engine=npu` 时 `terms.json` 真实影响转写结果，hold 链路零改动受益。

**Architecture:** `engine.py` 的 NPU 适配器从静态管线切到 stateful 管线（构造期 `word_timestamps=True` 使 decoder 形状装得下提示词），`initial_prompt`/`hotwords` 从「告警丢弃」改为直通 `generate()`；`transcribe_once.py` 接上 `terms.py` 组装（独立 try 降级，绝不阻断）。cpu/cuda 分支字节级不动。

**Tech Stack:** Python 3.12 标准库 + unittest；openvino-genai 2026.4（仅运行时，测试全用注入工厂不触碰）；faster-whisper（cpu/cuda 路径，不动）。

**Spec:** `docs/superpowers/specs/2026-09-30-npu-hotwords-design.md`（本 plan 从 spec 推导；执行者两个都读）

**Work from:** `/home/elling/work/git-repo/voice-input/.pi/worktrees/issue-27-npu-hotwords`（所有路径以此为根；venv 已软链，`./venv/bin/python3` 可用）

## Global Constraints

- cuda/cpu/auto 分支逐字节不变：`construction_kwargs`、preamble、`build_model` 参数一律不碰（spec Safety contract）
- 零改动保护清单：`voice_hold.py` / `paste.py` / `voice-ptt.py` / `terms.py` / `archive.py` / `media_pause.py` / `voice-ptt.sh` / `voice-toggle.sh` / `test-mic.sh` / `bench/` / `download-model.py` / `download-model.sh` / 除 `test_engine.py` 外的全部 test 文件
- 不新增任何环境变量 / 用户配置（spec D2：无逃生开关）
- 空串提示词不透传（truthy 判断；spec D6——stateful 管线对任何已设值都崩）
- `transcribe_once` CLI 契约不变：usage 文本、退出码（0/1/2）、stdout 形态逐字保留
- commit message 用 conventional commits，全部带 `(#27)` 尾注
- `git add` 按文件 stage，禁止 `git add -A`

## Review Focus

（spec 未逐条展开、但最可能咬人的输入类别；每条已钉进所属 task）

1. **词表文件损坏 / 非 dict / 非 UTF-8** → 必须降级为无提示转写，不得阻断 → Task 3 Case 3（损坏 JSON）
2. **空字符串提示词** → 不得透传给 generate（任何已设值崩 stateful）→ Task 2 `test_adapter_empty_string_prompt_not_forwarded`
3. **模型构造抛异常（NPU 设备错/模型缺失）** → 顶层可行动报错 + exit 1，不得裸 traceback → Task 3 Case 4
4. **词表超 30 条** → 只取前 30（Whisper 224 token 上限）→ 既有 `test_terms.py` 契约，Task 5 回归绿即覆盖
5. **调用方传未知 transcribe kwargs（beam_size 等）** → adapter 静默忽略 → 既有 `test_adapter_ignores_extra_kwargs`，Task 2 重写时原样保留

---

### Task 1: `engine.npu_pipeline_kwargs()` 纯函数

**Files:**
- Modify: `engine.py`（`NPU_COMPILE_CACHE` 常量块之后新增函数）
- Test: `test_engine.py`（`TestNpuConstruction` 类之前新增 TestCase）

**Interfaces:**
- Consumes: `engine.NPU_COMPILE_CACHE`（既有常量，`engine.py:31`）
- Produces: `engine.npu_pipeline_kwargs() -> dict`，精确返回 `{"NPU_PLATFORM": "NPU4000", "word_timestamps": True, "CACHE_DIR": NPU_COMPILE_CACHE}`。Task 2 的 adapter 构造与构造测试都依赖此签名。

- [ ] **Step 1: 写失败测试**（spec A1）

在 `test_engine.py` 的 `class TestNpuConstruction` 定义行之前插入：

```python
class TestNpuPipelineKwargs(unittest.TestCase):
    """#27 A1: npu 构造参数单一定义点(stateful 管线 + word_timestamps)。"""

    def test_kwargs_contract(self):
        kwargs = engine.npu_pipeline_kwargs()
        self.assertEqual(kwargs, {
            "NPU_PLATFORM": "NPU4000",
            "word_timestamps": True,
            "CACHE_DIR": engine.NPU_COMPILE_CACHE,
        })
        # STATIC_PIPELINE 必须不存在:显式 True 会走带断言的静态管线(spec D1)
        self.assertNotIn("STATIC_PIPELINE", kwargs)
        self.assertIs(kwargs["word_timestamps"], True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python3 -m unittest test_engine.TestNpuPipelineKwargs -v`
Expected: FAIL，`AttributeError: module 'engine' has no attribute 'npu_pipeline_kwargs'`

- [ ] **Step 3: 最小实现**

在 `engine.py` 的 `NPU_COMPILE_CACHE = ...` 行之后插入：

```python
def npu_pipeline_kwargs() -> dict:
    """npu 构造参数单一定义点(#27):stateful 管线 + word_timestamps=True。

    不含 STATIC_PIPELINE:NPU 默认即 stateful;显式 True 会走带断言的静态
    管线,硬拒 initial_prompt/hotwords。word_timestamps 必须构造期传入:
    它决定 decoder 的 SDPA 分解与输入形状(pipeline.cpp:98-114),构造后
    在 generate() 传无效且会崩(连空串都崩,#27 调研 R3)。
    """
    return {
        "NPU_PLATFORM": "NPU4000",
        "word_timestamps": True,
        "CACHE_DIR": NPU_COMPILE_CACHE,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python3 -m unittest test_engine.TestNpuPipelineKwargs -v`
Expected: PASS（1 test OK）

- [ ] **Step 5: Commit**

```bash
git add engine.py test_engine.py
git commit -m "feat(engine): npu_pipeline_kwargs() single source for NPU pipeline construction (#27)"
```

---

### Task 2: adapter 切 stateful + 提示词透传 + warn 缝移除

**Files:**
- Modify: `engine.py:281-312`（`_NpuWhisperAdapter` 类：`__init__` / `transcribe` / docstring）与 `build_model` npu 分支、两处 docstring、`NPU_COMPILE_CACHE` 注释
- Test: `test_engine.py`（`TestNpuConstruction` 1 个测试重写 + docstring；`TestNpuAdapter` 2 个测试重写 + 1 个新增 + docstring）

**Interfaces:**
- Consumes: `engine.npu_pipeline_kwargs()`（Task 1）
- Produces: `_NpuWhisperAdapter(model_dir, pipeline_factory)`（**warn 参数删除**）；`transcribe(wav, language="zh", initial_prompt=None, hotwords=None, **_ignored)` 语义变为「有值透传、空值省略」。Task 3 不直接依赖新签名（经 `engine.build_model` 间接），但行为契约以本 task 为准。

- [ ] **Step 1: 重写构造测试为失败态**（spec A1/A2）

`test_engine.py` 中 `TestNpuConstruction` 的类 docstring 与第一个测试整体替换：

```python
class TestNpuConstruction(unittest.TestCase):
    """#19 A1 / #27 A1: npu 构造参数(device/NPU_PLATFORM/word_timestamps/CACHE_DIR/模型路径)。"""

    def test_npu_build_constructs_stateful_pipeline_kwargs(self):
        factory = _FakePipeFactory()
        with tempfile.TemporaryDirectory() as root:
            base = _make_ov_base(root)
            m = engine.build_model(engine="npu", model="small-int8-ov",
                                   pipeline_factory=factory, model_base=base)
        self.assertIsInstance(m, engine._NpuWhisperAdapter)
        self.assertEqual(len(factory.calls), 1)
        call = factory.calls[0]
        self.assertTrue(call["model_dir"].endswith("abc123"))
        # stateful 管线(#27):无 STATIC_PIPELINE,构造期 word_timestamps=True
        self.assertEqual(call["kwargs"], {
            "device": "NPU",
            **engine.npu_pipeline_kwargs(),
        })
```

（`test_npu_default_model_when_env_unset` / `test_npu_missing_model_actionable` 原样不动）

- [ ] **Step 2: 重写 adapter 测试为失败态**（spec A2 + Review Focus 2/5）

`TestNpuAdapter` 类 docstring 替换为：

```python
class TestNpuAdapter(unittest.TestCase):
    """#19 A3 / #27 A2: adapter 兼容契约 / 提示词透传 / wav 16-bit 硬校验。"""
```

`test_adapter_hotword_degradation_warns` 与 `test_adapter_no_warn_without_prompt` 两个测试**删除**，原位置替换为以下三个：

```python
    def test_adapter_forwards_prompt_and_hotwords(self):
        pipe = _FakeGenaiPipe()
        adapter = engine._NpuWhisperAdapter(
            "/fake/model", pipeline_factory=lambda d, **kw: pipe)
        adapter.transcribe(self.wav, language="zh",
                           initial_prompt="术语:重构", hotwords="zima jfox")
        # stateful 管线支持提示词(#27):原样透传给 generate
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {
            "language": "zh",
            "initial_prompt": "术语:重构",
            "hotwords": "zima jfox",
        })

    def test_adapter_omits_absent_prompt_kwargs(self):
        adapter, pipe = self._make_adapter()
        adapter.transcribe(self.wav, language="zh")
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {"language": "zh"})

    def test_adapter_empty_string_prompt_not_forwarded(self):
        # 空串不透传:stateful 管线对任何已设值都会在 roi 检查崩溃(#27 R3)
        adapter, pipe = self._make_adapter()
        adapter.transcribe(self.wav, language="zh", initial_prompt="", hotwords="")
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {"language": "zh"})
```

（`test_adapter_transcribe_contract` / `test_adapter_empty_result_empty_segments` / `test_adapter_ignores_extra_kwargs` / `_make_adapter` / setUp/tearDown 原样不动——`_make_adapter` 不传 warn，与旧签名兼容）

- [ ] **Step 3: 跑测试确认失败**

Run: `./venv/bin/python3 -m unittest test_engine.TestNpuConstruction test_engine.TestNpuAdapter -v`
Expected: `test_npu_build_constructs_stateful_pipeline_kwargs` FAIL（kwargs 里还有 `STATIC_PIPELINE: True`）；`test_adapter_forwards_prompt_and_hotwords` FAIL（prompt 被丢弃，kwargs 只有 language）。其余 PASS。

- [ ] **Step 4: 实现**

`engine.py` `_NpuWhisperAdapter` 类 docstring + `__init__` + `transcribe` 整体替换为：

```python
class _NpuWhisperAdapter:
    """openvino_genai WhisperPipeline -> faster-whisper WhisperModel 形态适配(#19)。

    暴露同签名 transcribe(),返回 (segments, info)——调用方(voice-ptt.py /
    transcribe_once.py)零改动。#27 起 stateful 管线 + 构造期 word_timestamps=True,
    initial_prompt/hotwords 直通 generate(静态管线的硬拒与降级告警已随
    STATIC_PIPELINE 一并移除)。
    """

    def __init__(self, model_dir: str, pipeline_factory):
        # stateful 管线(NPU 默认)+ 构造期 word_timestamps(#27):决定 decoder
        # SDPA 分解与输入形状,使 initial_prompt/hotwords 可用;CACHE_DIR:
        # 编译跨进程缓存
        self._pipe = pipeline_factory(model_dir, device="NPU",
                                      **npu_pipeline_kwargs())

    def transcribe(self, wav, language="zh", initial_prompt=None, hotwords=None,
                   **_ignored):
        # truthy 判断:None/空串不透传——stateful 管线对任何已设值都会崩(#27 R3)
        gen_kwargs = {}
        if initial_prompt:
            gen_kwargs["initial_prompt"] = initial_prompt
        if hotwords:
            gen_kwargs["hotwords"] = hotwords
        samples = _load_wav_samples(wav)
        result = self._pipe.generate(samples, language=language, **gen_kwargs)
        text = self._extract_text(result)
        segments = [SimpleNamespace(text=text)] if text else []
        return segments, SimpleNamespace(language=language)
```

（`_extract_text` 静态方法位于 transcribe 之后，保持不动）

`build_model` 的 npu 分支行替换为（去掉 `warn=warn`）：

```python
    if engine == "npu":
        path = resolve_model_path(model, base=model_base, layout="ov")
        if pipeline_factory is None:
            from openvino_genai import WhisperPipeline as pipeline_factory
        return _NpuWhisperAdapter(path, pipeline_factory)
```

`build_model` docstring 中 npu 行改为：`npu   _NpuWhisperAdapter(openvino_genai WhisperPipeline,stateful+word_timestamps+编译缓存)`；`NPU_COMPILE_CACHE` 上方注释改为：

```python
# NPU 编译缓存:stateful+word_timestamps 条目冷 ~155s 一次性,热 ~1.4s 跨进程
# 生效;目录总量 ~2.4GB(含历史静态管线条目,可整目录 rm 重建,代价一次冷编译)
# (2026-09-30 本机实测,#27)
```

- [ ] **Step 5: 跑测试确认通过 + 全组回归**

Run: `./venv/bin/python3 -m unittest test_engine -v`
Expected: 全部 PASS（含未改动的既有测试）

- [ ] **Step 6: Commit**

```bash
git add engine.py test_engine.py
git commit -m "feat(engine): switch NPU adapter to stateful pipeline, forward initial_prompt/hotwords (#27)"
```

---

### Task 3: `transcribe_once.py` 接入 terms 组装

**Files:**
- Modify: `transcribe_once.py`（import 区 + `main` 整体）与模块 docstring
- Test: `test_engine.py`（顶部补 `import json`；`TestTranscribeOnceEnv` 类之后新增 `TestTranscribeOnceTerms`）

**Interfaces:**
- Consumes: `terms.load_terms(path) -> dict`、`terms.build_prompt(list) -> str|None`、`terms.build_transcribe_kwargs(dict) -> dict`、`terms.DEFAULT_TERMS_PATH`（均既有，零改动）；`engine.build_model()`（被 monkeypatch）
- Produces: `transcribe_once.main(argv=None, terms_path=DEFAULT_TERMS_PATH) -> int`。CLI 行为（无参调用 `python transcribe_once.py <wav>`）与现状等价。

- [ ] **Step 1: 写失败测试**（spec A3 + Review Focus 1/3）

`test_engine.py` 顶部 import 区 `import os` 之前插入 `import json`。在 `TestTranscribeOnceEnv` 类定义结束之后（`test_transcribe_once_env_cpu_no_nvidia_paths` 之后）新增：

```python
class TestTranscribeOnceTerms(unittest.TestCase):
    """#27 A3: transcribe_once 词表接线与降级(cpu 引擎,假模型,干净子进程)。"""

    REPO = os.path.dirname(os.path.realpath(__file__))
    VENV_PY = os.path.join(REPO, "venv", "bin", "python3")

    def _run_main(self, terms_path: str):
        """干净子进程跑 main():patch build_model 为记录型假模型,打印标记行。"""
        code = (
            "import io, os, sys, types, contextlib\n"
            "sys.path.insert(0, %r)\n"
            "import engine, transcribe_once\n"
            "calls = {}\n"
            "class FakeModel:\n"
            "    def transcribe(self, wav, language='zh', initial_prompt=None, **kw):\n"
            "        calls['kwargs'] = dict(language=language,"
            " initial_prompt=initial_prompt, **kw)\n"
            "        return [types.SimpleNamespace(text=' 你好 ')], None\n"
            "engine.build_model = lambda *a, **k: FakeModel()\n"
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    rc = transcribe_once.main(['/nonexistent/t.wav'],"
            " terms_path=os.environ['TERMS_PATH'])\n"
            "print('RC', rc)\n"
            "print('PROMPT', repr(calls['kwargs'].get('initial_prompt')))\n"
            "print('EXTRA', sorted(k for k in calls['kwargs']"
            " if k not in ('language', 'initial_prompt')))\n"
            "print('OUT', buf.getvalue().strip())\n"
        ) % self.REPO
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "VOICE_INPUT_ENGINE",
                            "VOICE_INPUT_MODEL")}
        env.update({"VOICE_INPUT_ENGINE": "cpu", "TERMS_PATH": terms_path})
        p = subprocess.run([self.VENV_PY, "-c", code],
                           capture_output=True, text=True, env=env)
        markers = dict(
            line.split(" ", 1) for line in p.stdout.strip().splitlines()
            if line.startswith(("RC ", "PROMPT ", "EXTRA ", "OUT ")))
        return p, markers

    def test_terms_json_forwarded_as_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            tp = os.path.join(root, "terms.json")
            with open(tp, "w", encoding="utf-8") as f:
                json.dump({"terms": ["zima", "jfox"],
                           "hotwords": ["zima", "jfox"]}, f)
            p, m = self._run_main(tp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("zima", m["PROMPT"])            # 词表进 initial_prompt
        self.assertEqual(m["EXTRA"], "['hotwords']")  # hotwords 列表 join 后透传
        self.assertEqual(m["OUT"], "你好")             # join/strip 走真实代码

    def test_missing_terms_degrades_to_no_prompt(self):
        p, m = self._run_main(
            os.path.join(tempfile.gettempdir(), "no-such-terms-27.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(m["PROMPT"], "None")
        self.assertEqual(m["OUT"], "你好")
        self.assertIn("[voice-input] terms.json load failed", p.stderr)

    def test_corrupt_terms_degrades_to_no_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            tp = os.path.join(root, "terms.json")
            with open(tp, "w", encoding="utf-8") as f:
                f.write("{ not json")
            p, m = self._run_main(tp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(m["PROMPT"], "None")
        self.assertEqual(m["OUT"], "你好")

    def test_model_error_actionable_exit_1(self):
        code = (
            "import runpy, sys\n"
            "sys.path.insert(0, %r)\n"
            "import engine\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('boom')\n"
            "engine.build_model = boom\n"
            "sys.argv = ['transcribe_once.py', '/nonexistent/t.wav']\n"
            "runpy.run_path(%r, run_name='__main__')\n"
        ) % (self.REPO, os.path.join(self.REPO, "transcribe_once.py"))
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "VOICE_INPUT_ENGINE",
                            "VOICE_INPUT_MODEL")}
        env["VOICE_INPUT_ENGINE"] = "cpu"
        p = subprocess.run([self.VENV_PY, "-c", code],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("[voice-input] transcribe failed: boom", p.stderr)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python3 -m unittest test_engine.TestTranscribeOnceTerms -v`
Expected: `test_terms_json_forwarded_as_prompt` FAIL（`PROMPT` 为 `None`——main 还不接 terms）；`test_missing_terms_degrades_to_no_prompt` 与 `test_corrupt_terms_degrades_to_no_prompt` FAIL（stderr 里还没有 `terms.json load failed` 告警行）；`test_model_error_actionable_exit_1` PASS（既有行为，回归钉死）。

- [ ] **Step 3: 实现**

`transcribe_once.py` 的 import 区在 `import engine` 行之后插入：

```python
from terms import (DEFAULT_TERMS_PATH, build_prompt, build_transcribe_kwargs,
                   load_terms)
```

模块 docstring 末尾追加一行：`词表热词(#27): main() 组装 terms.initial_prompt/hotwords,所有引擎统一生效,组装失败降级为无提示转写。`

`main` 函数整体替换为：

```python
def main(argv=None, terms_path=DEFAULT_TERMS_PATH) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("usage: transcribe_once.py <wav>", file=sys.stderr)
        return 2
    wav = argv[0]
    # terms 组装独立 try:热词问题降级到无 prompt,绝不阻断转写(与 voice-ptt.py
    # 同构;load_terms/build_* 内部已降级,这里是双保险)(#27)
    try:
        cfg = load_terms(terms_path)
        prompt = build_prompt(cfg.get("terms", []))
        extra_kw = build_transcribe_kwargs(cfg)
    except Exception as te:
        prompt, extra_kw = None, {}
        print(f"[voice-input] terms assemble failed, degrade to no-prompt: {te}",
              file=sys.stderr)
    model = engine.build_model()
    segments, info = model.transcribe(wav, language="zh",
                                      initial_prompt=prompt, **extra_kw)
    # join/strip 与 voice-ptt.py 转写路径同构
    text = "".join(s.text for s in segments).strip()
    print(text)
    return 0
```

（文件其余部分——preamble、`if __name__ == "__main__"` 块——逐字不动）

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python3 -m unittest test_engine -v`
Expected: 全部 PASS（含 Task 2 的测试与 `TestTranscribeOnceEnv` 既有四条）

- [ ] **Step 5: Commit**

```bash
git add transcribe_once.py test_engine.py
git commit -m "feat(transcribe_once): wire terms hotwords into CLI transcription (#27)"
```

---

### Task 4: 示例服务超时 + README 双语更新

**Files:**
- Modify: `contrib/voice-hold.service:16-25`（注释块 + 超时行）
- Modify: `README.md:192-226`（NPU 段）、`README.md:176`（超时行）、`README.md:228-245`（Custom vocabulary 段）
- Modify: `README.zh-CN.md:168-183`（NPU 段）、`README.zh-CN.md:154`（超时行）、`README.zh-CN.md:185-200`（自定义词汇段）

**Interfaces:**
- Consumes: 无代码依赖（纯文档）；数字依据 spec Research-backed facts 4/5
- Produces: 无（文档与示例配置；U2 的操作说明来源）

- [ ] **Step 1: 改 `contrib/voice-hold.service`**

注释块（`# NPU 引擎(#19)...` 至 `# cpu 备选...` 五行）与三个 Environment 行整体替换为：

```ini
# NPU 引擎(#19/#27):stateful 管线 + 构造期 word_timestamps(热词启用)。
# 首次转写一次性冷编译 ~155s——超时给 240s 兜住;之后热缓存加载 ~1.4s/次,
# 转写 ~1.0s/次。编译缓存 ~/.cache/voice-input/npu-compile-cache(~2.4GB,
# 含历史静态管线条目;可 rm 整目录重建,代价一次 155s 冷编译)。
# 常驻 worker 免每次加载见 #25。
# 无需 LD_LIBRARY_PATH——voice_hold 会给转写子进程自动注入。
# cpu 备选(无 NPU 机器):VOICE_INPUT_ENGINE=cpu + VOICE_INPUT_MODEL=small
Environment=VOICE_INPUT_ENGINE=npu
Environment=VOICE_INPUT_MODEL=small-int8-ov
Environment=VOICE_INPUT_TRANSCRIBE_TIMEOUT=240
```

- [ ] **Step 2: 改 `README.md`（EN）**

NPU 段首段（`` `VOICE_INPUT_ENGINE=npu` transcribes...`` 起至 ``(benchmark: #17).``）替换为：

```markdown
`VOICE_INPUT_ENGINE=npu` transcribes on the Intel NPU via openvino-genai's
`WhisperPipeline` — the stateful pipeline built with `word_timestamps=True`,
which is what makes `initial_prompt`/`hotwords` work on NPU (#27). Measured on
an OmniBook (Core Ultra 258V, whisper-small int8): **~1.0s per 8s dictation
with the term list applied** (the previous static pipeline did ~0.3s but cannot
carry hotwords), plus ~1.4s model load per dictation.
```

Notes 三条 bullet 整体替换为：

```markdown
- The first transcription compiles the pipeline once (**~155s** — use
  `VOICE_INPUT_TRANSCRIBE_TIMEOUT` ≥ 240 for the first run; the bundled example
  service already sets 240). A compile cache
  (`~/.cache/voice-input/npu-compile-cache`, ~2.4GB incl. legacy entries) cuts
  later model loads to ~1.4s. Safe to delete — rebuilt on next run (one ~155s
  recompile).
- Hotwords (`terms.json`) **are supported on NPU** since #27 (stateful pipeline
  + construction-time `word_timestamps`). They are a probabilistic bias, not a
  guarantee — they can occasionally change nearby common words too. The Wayland
  hold-to-talk path picks them up via transcribe_once.py.
- Each dictation still pays ~1.4s model load (subprocess-per-dictation design,
  #18); a resident transcription worker is tracked as #25.
```

超时说明行（`- Transcription is bounded by...`）行末追加一句：`For the first NPU run after a cache wipe use ≥240s (cold compile).`

Custom vocabulary 段的 bullet 列表末尾追加：

```markdown
- Works on every engine: cuda/cpu (faster-whisper) and npu (stateful pipeline,
  #27).
```

- [ ] **Step 3: 改 `README.zh-CN.md`（与 EN 逐段对应）**

NPU 段首段（`` `VOICE_INPUT_ENGINE=npu` 经 openvino-genai...`` 起至「（基准见 #17）。」）替换为：

```markdown
`VOICE_INPUT_ENGINE=npu` 经 openvino-genai 的 `WhisperPipeline` 在 Intel NPU 上
转写——stateful 管线 + 构造期 `word_timestamps=True`，正是它让 `initial_prompt`/
热词在 NPU 上可用（#27）。OmniBook（Core Ultra 258V，whisper-small int8）实测：
**带词表约 1.0s / 8 秒语音**（旧静态管线约 0.3s 但带不了热词），另加每次约 1.4s
模型加载。
```

「注意：」一段整体替换为：

```markdown
注意：首次转写一次性冷编译约 **155s**（首次运行把 `VOICE_INPUT_TRANSCRIBE_TIMEOUT`
设 ≥240——仓库示例服务已设 240）；编译缓存 `~/.cache/voice-input/npu-compile-cache`
（约 2.4GB，含历史条目；可整目录删，代价一次 155s 重编译）让后续加载降到约 1.4s。
热词（terms.json）自 #27 起 **NPU 支持**（stateful 管线 + 构造期 word_timestamps）；
热词是概率性软引导而非保证，偶尔也会改动附近的常用词。Wayland 按住说话链路经
transcribe_once.py 自动吃到词表。每次听写仍有约 1.4s 加载（#18 子进程架构），
常驻转写 worker 见 #25。
```

超时说明行（`- 转写受 ... 约束：`）行末追加：`NPU 清缓存后首次运行建议 ≥240s（冷编译）。`

自定义词汇段 bullet 列表末尾追加：

```markdown
- **全引擎生效**：cuda/cpu（faster-whisper）与 npu（stateful 管线，#27）均支持
```

- [ ] **Step 4: 静态验证**

Run: `grep -n "STATIC_PIPELINE\|static pipeline\|静态管线" README.md README.zh-CN.md contrib/voice-hold.service`
Expected: 仅 `contrib` 注释里允许出现「静态管线」（历史条目说明）；README 两个文件的 NPU 段不再有「static pipeline/静态管线不支持热词」表述。

Run: `grep -n "240" contrib/voice-hold.service README.md README.zh-CN.md | wc -l`
Expected: ≥ 4（三处文件都有 240）

Run: `./venv/bin/python3 -m unittest test_engine -v`
Expected: 全部 PASS（文档改动不影响测试，防手滑）

- [ ] **Step 5: Commit**

```bash
git add contrib/voice-hold.service README.md README.zh-CN.md
git commit -m "docs: NPU hotwords — update READMEs (EN+ZH) and example service timeout (#27)"
```

---

### Task 5: 全量回归 + 保护清单核对（A4 门）

**Files:**
- 无新改动（纯验证；若发现问题，修复后按所属文件重新 commit）

**Interfaces:**
- Consumes: Task 1–4 全部产物
- Produces: A4 验收证据（命令输出 + diff 清单），回贴 issue 用

- [ ] **Step 1: 全量套件**

Run: `./venv/bin/python3 -m unittest test_engine test_terms test_archive test_media_pause test_hold test_paste test_bench -v`
Expected: 全部 PASS，0 failures / 0 errors（记录总测试数）

- [ ] **Step 2: 保护清单核对**

Run: `git diff main --name-only`
Expected: **恰好 8 个路径**——6 个产品文件（`engine.py` / `transcribe_once.py` / `test_engine.py` / `contrib/voice-hold.service` / `README.md` / `README.zh-CN.md`）+ 2 个流程文档（`docs/superpowers/specs/2026-09-30-npu-hotwords-design.md` / `docs/superpowers/plans/2026-09-30-npu-hotwords.md`）。spec Scope 的「6 个文件」指产品文件；spec/plan 是 github-issue-driven 流程自身的产物（Step 5/6 落 worktree）。

Run: `git diff main --stat -- voice_hold.py paste.py voice-ptt.py terms.py archive.py media_pause.py voice-ptt.sh voice-toggle.sh test-mic.sh bench download-model.py download-model.sh test_terms.py test_archive.py test_media_pause.py test_hold.py test_paste.py test_bench.py`
Expected: 空输出（零改动）

- [ ] **Step 3: 结果留痕**

把 Step 1 的测试统计与 Step 2 的两份清单记入本 plan 末尾「Verification log」小节（追加写入，不 commit 单独一步，随 Task 5 如有修复则一并提交；无修复则 plan 文档更新单独 commit：`docs: record A4 verification log (#27)`）。

---

### Task 6: U1 / U2 用户实测（post-merge 人工，实现者不执行）

**Files:** 无（操作清单，结果回贴 issue #27）

- [ ] **U1 真机 NPU 热词生效**

前置：merge 后主仓 `git pull`；本机 `systemctl --user edit voice-hold` 加 drop-in `Environment=VOICE_INPUT_TRANSCRIBE_TIMEOUT=240`，`systemctl --user restart voice-hold`；确认 `~/.config/voice-input/terms.json` 存在（30 词）。

步骤：hold 听写 3 句话，每句含 1–2 个词表词（建议「我用 zima 做审查」「打开 jfox 看看」「这个 Kimi 怎么用」）。

通过标准：≥2 句词表词以正确拼写上屏；3 句原始转写与端到端耗时（journalctl 时间戳）回贴 issue。

- [ ] **U2 冷编译与超时兜底**

步骤：`rm -rf ~/.cache/voice-input/npu-compile-cache` → 第 1 次听写计时（预期 ~155s，必须 <240s 不被杀、文本正常上屏）→ 第 2 次听写计时（预期 <5s）。

通过标准：两轮耗时如实回贴 issue；第 1 次在 240s 内完成。

---

## Verification log

执行日期：2026-09-30；分支 `issue-27-npu-hotwords`，验证基线 HEAD = `f3001c0`（main = `32b2090`），工作区干净。

### A4-1 全量回归（Step 1）

```
$ ./venv/bin/python3 -m unittest test_engine test_terms test_archive test_media_pause test_hold test_paste test_bench -v
----------------------------------------------------------------------
Ran 165 tests in 0.860s

OK
```

结果：**165 tests, 0 failures, 0 errors**。逐模块计数：

| 模块 | 测试数 |
| --- | --- |
| test_engine | 70 |
| test_terms | 20 |
| test_archive | 8 |
| test_media_pause | 4 |
| test_hold | 16 |
| test_paste | 31 |
| test_bench | 16 |
| 合计 | 165 |

### A4-2 保护清单核对（Step 2）

改动面（`git diff main --name-only`）恰好 8 个路径 = 6 个产品文件 + 2 个流程文档：

```
README.md
README.zh-CN.md
contrib/voice-hold.service
docs/superpowers/plans/2026-09-30-npu-hotwords.md
docs/superpowers/specs/2026-09-30-npu-hotwords-design.md
engine.py
test_engine.py
transcribe_once.py
```

零改动清单（`git diff main --stat -- voice_hold.py paste.py voice-ptt.py terms.py archive.py media_pause.py voice-ptt.sh voice-toggle.sh test-mic.sh bench download-model.py download-model.sh test_terms.py test_archive.py test_media_pause.py test_hold.py test_paste.py test_bench.py`）输出为空（0 字节）。18 个受保护路径全部未被触碰。

### 结论

A4 门通过：全量套件绿，保护清单零改动，改动面与 spec Scope 一致。
