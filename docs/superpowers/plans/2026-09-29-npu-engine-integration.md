# NPU Engine Integration Implementation Plan (#19)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `VOICE_INPUT_ENGINE=npu` 端到端可用（OmniBook Wayland：voice_hold → transcribe_once → paste），顺路收敛 LD_LIBRARY_PATH preamble 技术债。

**Architecture:** engine.py 新增 `_NpuWhisperAdapter`（duck-typing 兼容 faster-whisper 的 `transcribe()` 签名，调用方零改动）+ 库路径单点计算（`required_lib_paths`/`prepend_library_path`/`has_library_paths`/`child_env`）；NPU 构造带 `CACHE_DIR` 编译缓存（冷 47s/热 0.8s）；热词在 NPU 降级告警不阻断。

**Tech Stack:** Python stdlib + openvino-genai 2026.4（懒导入）、bash wrappers、unittest（venv 无 pytest）。

**Spec:** `docs/superpowers/specs/2026-09-29-npu-engine-integration-design.md`（验收矩阵 A1–A8 / U1–U3，每个 task 头部标注归属 ID）

## Global Constraints

- **Work from:** worktree 根 `$WT=/home/elling/work/git-repo/voice-input/.pi/worktrees/issue-19-npu-engine`；所有文件操作用 `$WT` 绝对路径，git 用 `git -C $WT`
- **前置（开工前一次性）**：`ln -s /home/elling/work/git-repo/voice-input/venv $WT/venv`——venv gitignored 不进 worktree，但 transcribe_once/wrapper 的 `_SITE` 探测和测试子进程都依赖 `$WT/venv`（符号链接经 realpath 后解析到主仓 venv，行为一致）
- **7700K（X11 + cuda）零行为变化**：不设环境变量 = cuda + large-v3 + float16；voice-ptt.py 上屏段（xsel/xdotool）一行不动；cuda/auto 下 LD_LIBRARY_PATH 输出与历史逐字节一致
- engine.py 顶层保持纯标准库：openvino_genai / numpy 只在函数内懒导入
- 已声明惰性差异（spec Design #4，不算违反零变化）：engine=cpu 时不再设置 nvidia 路径；prepend 对父环境已含分量去重（HEAD 会重复追加，ld.so 语义等价）
- commit message 用英文 conventional commits；`git add <file>` 按文件 stage，禁止 `git add -A`
- 每个 task 末尾跑全量回归：`cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause`（bench/hold/paste 顶层均纯标准库，venv python 可跑）

## Review Focus

1. **父环境已含目标路径**：prepend 幂等去重 vs HEAD 重复追加——ld.so 语义等价，Task 1 测试钉死幂等语义
2. **非法 VOICE_INPUT_ENGINE**：transcribe_once / voice-ptt.py 现在在模块加载期报错退出（HEAD 在 build_model 时机）——消息文本与退出码不变，仅时机提前，仅影响非法配置
3. **空字符串 VOICE_INPUT_MODEL**：视为未设置走 `default_model`（HEAD 拿空串找模型报错）——病理输入的惰性差异
4. **异常 wav 格式**：24-bit 硬校验 ValueError；非 16kHz/mono warn 但继续（whisper 内部重采样）——Task 2 测试钉死
5. **CACHE_DIR 并发首编译**：两进程同时冷编译由 genai 内部处理，本计划不防；README 注明缓存可 rm 重建

---

### Task 1: engine.py 纯函数层（库路径三函数 + default_model + ov 布局解析 + child_env）

**验收归属：A2（ov_resolve）、A4（lib_paths）**

**Files:**
- Modify: `$WT/engine.py`（在 `snapshots_base` 后追加；改 `resolve_model_path`）
- Test: `$WT/test_engine.py`（追加 TestLibPaths / TestOvResolve 两个 TestCase）

**Interfaces:**
- Produces（后续 task 依赖的确切签名）:
  - `engine.NPU_LIB_DIR: str` = `"/usr/lib/x86_64-linux-gnu"`
  - `engine.required_lib_paths(engine: str, site_packages: str = None) -> list`
  - `engine.has_library_paths(env: dict, paths: list) -> bool`
  - `engine.prepend_library_path(env: dict, paths: list) -> str`
  - `engine.default_model(engine: str) -> str`
  - `engine.ov_snapshots_base(model: str) -> str`
  - `engine.resolve_model_path(model=None, base=None, layout="ct2") -> str`
  - `engine.child_env(env: dict) -> dict`

- [ ] **Step 1: 写失败测试**（追加到 `$WT/test_engine.py` 末尾）

```python
class TestLibPaths(unittest.TestCase):
    """#19 A4: 库路径单点计算 / prepend 幂等 / has_library_paths 谓词 / child_env。"""

    SITE = "/venv/lib/python3.12/site-packages"
    LEGACY_CUDA = [
        f"{SITE}/nvidia/cublas/lib",
        f"{SITE}/nvidia/cudnn/lib",
        f"{SITE}/nvidia/cuda_nvrtc/lib",
    ]

    def test_lib_paths_cuda_byte_equal_legacy(self):
        self.assertEqual(
            engine.required_lib_paths("cuda", self.SITE), self.LEGACY_CUDA)

    def test_lib_paths_auto_same_as_cuda(self):
        self.assertEqual(
            engine.required_lib_paths("auto", self.SITE), self.LEGACY_CUDA)

    def test_lib_paths_cpu_empty(self):
        self.assertEqual(engine.required_lib_paths("cpu"), [])

    def test_lib_paths_npu(self):
        self.assertEqual(
            engine.required_lib_paths("npu"), ["/usr/lib/x86_64-linux-gnu"])

    def test_lib_paths_cuda_requires_site(self):
        with self.assertRaises(ValueError):
            engine.required_lib_paths("cuda")

    def test_lib_paths_invalid_engine(self):
        with self.assertRaises(ValueError):
            engine.required_lib_paths("tpu", self.SITE)

    def test_prepend_empty_env(self):
        env = {}
        got = engine.prepend_library_path(env, self.LEGACY_CUDA)
        self.assertEqual(got, ":".join(self.LEGACY_CUDA))
        self.assertEqual(env["LD_LIBRARY_PATH"], got)

    def test_prepend_appends_existing_byte_equal_legacy(self):
        # HEAD 历史输出: f"{p1}:{p2}:{p3}:{old}" —— 逐字节一致
        env = {"LD_LIBRARY_PATH": "/opt/keep"}
        got = engine.prepend_library_path(env, self.LEGACY_CUDA)
        self.assertEqual(got, ":".join(self.LEGACY_CUDA) + ":/opt/keep")

    def test_prepend_idempotent(self):
        env = {"LD_LIBRARY_PATH": "/a"}
        engine.prepend_library_path(env, [engine.NPU_LIB_DIR])
        engine.prepend_library_path(env, [engine.NPU_LIB_DIR])
        self.assertEqual(env["LD_LIBRARY_PATH"], f"{engine.NPU_LIB_DIR}:/a")

    def test_prepend_empty_paths_noop(self):
        env = {"LD_LIBRARY_PATH": "/keep"}
        self.assertEqual(engine.prepend_library_path(env, []), "/keep")

    def test_has_library_paths_exact_component_match(self):
        # 兄弟目录 /usr/lib/x86_64-linux-gnu-extras 不得误判为已含
        env = {"LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu-extras"}
        self.assertFalse(engine.has_library_paths(env, [engine.NPU_LIB_DIR]))
        env2 = {"LD_LIBRARY_PATH": f"/opt/x:{engine.NPU_LIB_DIR}"}
        self.assertTrue(engine.has_library_paths(env2, [engine.NPU_LIB_DIR]))

    def test_child_env_npu_injects(self):
        out = engine.child_env({"VOICE_INPUT_ENGINE": "npu", "X": "1"})
        self.assertEqual(out["LD_LIBRARY_PATH"], engine.NPU_LIB_DIR)
        self.assertEqual(out["X"], "1")

    def test_child_env_npu_idempotent_when_present(self):
        out = engine.child_env({
            "VOICE_INPUT_ENGINE": "npu",
            "LD_LIBRARY_PATH": engine.NPU_LIB_DIR,
        })
        self.assertEqual(out["LD_LIBRARY_PATH"], engine.NPU_LIB_DIR)

    def test_child_env_cpu_passthrough_copy(self):
        src = {"VOICE_INPUT_ENGINE": "cpu", "LD_LIBRARY_PATH": "/opt/x"}
        out = engine.child_env(src)
        self.assertEqual(out, src)
        self.assertIsNot(out, src)

    def test_child_env_invalid_engine_passthrough(self):
        src = {"VOICE_INPUT_ENGINE": "bogus"}
        self.assertEqual(engine.child_env(src), src)


class TestOvResolve(unittest.TestCase):
    """#19 A2: default_model 矩阵 / ov 布局谓词 / 分布局错误信息。"""

    def test_default_model_matrix(self):
        self.assertEqual(engine.default_model("npu"), "small-int8-ov")
        self.assertEqual(engine.default_model("cuda"), "large-v3")
        self.assertEqual(engine.default_model("cpu"), "large-v3")
        self.assertEqual(engine.default_model("auto"), "large-v3")

    def test_default_model_invalid_engine(self):
        with self.assertRaises(ValueError):
            engine.default_model("bogus")

    def test_ov_snapshots_base_value(self):
        self.assertEqual(
            engine.ov_snapshots_base("small-int8-ov"),
            os.path.expanduser(
                "~/.cache/huggingface/hub/"
                "models--OpenVINO--whisper-small-int8-ov/snapshots"),
        )

    def test_resolve_ov_layout_found(self):
        with tempfile.TemporaryDirectory() as root:
            snap = os.path.join(root, "snapshots", "abc123")
            os.makedirs(snap)
            open(os.path.join(snap, "openvino_encoder_model.xml"), "wb").close()
            got = engine.resolve_model_path("small-int8-ov", base=os.path.join(root, "snapshots"), layout="ov")
            self.assertEqual(got, snap)

    def test_resolve_ov_layout_missing_actionable_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as ctx:
                engine.resolve_model_path("small-int8-ov", base=root, layout="ov")
            self.assertIn("huggingface-cli download OpenVINO/whisper-small-int8-ov", str(ctx.exception))

    def test_resolve_ct2_error_mentions_download_model_sh(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as ctx:
                engine.resolve_model_path("large-v3", base=root)
            self.assertIn("./download-model.sh large-v3", str(ctx.exception))

    def test_resolve_ov_layout_ignores_ct2_only_snapshot(self):
        # ct2 布局快照(model.bin)不应被 ov 谓词误认
        with tempfile.TemporaryDirectory() as root:
            snap = os.path.join(root, "snapshots", "dl")
            os.makedirs(snap)
            open(os.path.join(snap, "model.bin"), "wb").close()
            with self.assertRaises(RuntimeError):
                engine.resolve_model_path("small-int8-ov", base=os.path.join(root, "snapshots"), layout="ov")

    def test_resolve_unknown_layout(self):
        with self.assertRaises(ValueError):
            engine.resolve_model_path("x", base="/tmp", layout="onnx")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestLibPaths test_engine.TestOvResolve -v`
Expected: FAIL（`AttributeError: module 'engine' has no attribute 'required_lib_paths'`）

- [ ] **Step 3: 实现**（`$WT/engine.py`）

在 `ENV_MODEL = "VOICE_INPUT_MODEL"` 常量区后加常量，在 `snapshots_base` 函数后插入以下函数；`resolve_model_path` 整函数替换：

```python
# engine=npu 时进程启动前置库目录(ze 驱动库;OmniBook/Arch 本机已验证)
NPU_LIB_DIR = "/usr/lib/x86_64-linux-gnu"
```

```python
def required_lib_paths(engine: str, site_packages: str = None) -> list:
    """引擎 -> 进程启动前置库路径列表(#19 收敛 5+1 份拷贝的单点)。

    cuda/auto: venv nvidia pip 库三件套(与历史拷贝逐字同序,A4 钉死);
    npu: ze 驱动库目录(ld.so 只在进程启动读 LD_LIBRARY_PATH);
    cpu: 无。site_packages 为 None 时 cuda/auto 抛 ValueError(宁可报错不静默算错)。
    """
    _validated_engine(engine)
    if engine in ("cuda", "auto"):
        if not site_packages:
            raise ValueError(
                "[voice-input] required_lib_paths: cuda/auto requires site_packages "
                "(venv site-packages dir)"
            )
        return [
            f"{site_packages}/nvidia/cublas/lib",
            f"{site_packages}/nvidia/cudnn/lib",
            f"{site_packages}/nvidia/cuda_nvrtc/lib",
        ]
    if engine == "npu":
        return [NPU_LIB_DIR]
    return []


def has_library_paths(env: dict, paths: list) -> bool:
    """paths 是否已全部在 env 的 LD_LIBRARY_PATH 中(分量精确匹配)。

    精确分量匹配防兄弟目录误判(/usr/lib/x86_64-linux-gnu-extras 不算包含)。
    """
    present = [p for p in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    return all(p in present for p in paths)


def prepend_library_path(env: dict, paths: list) -> str:
    """在传入 env dict 上前置 paths 到 LD_LIBRARY_PATH,返回新值。幂等。

    只操作传入 dict(不写 os.environ,可测)。已含分量不重复拼——HEAD 会重复
    追加,去重对 ld.so 语义等价(spec Design #4 惰性差异声明)。
    """
    old = env.get("LD_LIBRARY_PATH", "")
    missing = [p for p in paths if not has_library_paths(env, [p])]
    if not missing:
        env["LD_LIBRARY_PATH"] = old
        return old
    prefix = os.pathsep.join(missing)
    env["LD_LIBRARY_PATH"] = f"{prefix}{os.pathsep}{old}" if old else prefix
    return env["LD_LIBRARY_PATH"]


def default_model(engine: str) -> str:
    """引擎感知的默认模型名单点:npu -> small-int8-ov(OpenVINO 格式),其他 -> large-v3。

    build_model 与 shell 预检共用;不设 VOICE_INPUT_MODEL 时 npu 不会错找
    不存在的 OpenVINO/whisper-large-v3。#16 契约(cuda 默认 large-v3)不动。
    """
    _validated_engine(engine)
    return "small-int8-ov" if engine == "npu" else DEFAULT_MODEL


def ov_snapshots_base(model: str) -> str:
    """OpenVINO 模型的 HF hub snapshots 目录(仅拼路径,不检查存在性)。"""
    return os.path.expanduser(
        f"~/.cache/huggingface/hub/models--OpenVINO--whisper-{model}/snapshots"
    )


def child_env(env: dict) -> dict:
    """spawn transcribe 子进程用的 env(voice_hold 唯一注入点)。

    npu 时 prepend NPU 库目录(ld.so 进程启动前置,进程内改无效——KB 实证);
    其他引擎/非法引擎原样拷贝放行,由 transcribe_once 的进程内 preamble 与
    可行动报错维持现状错误路径。
    """
    out = dict(env)
    try:
        eng = engine_name(out)
    except ValueError:
        return out
    if eng == "npu":
        out["LD_LIBRARY_PATH"] = prepend_library_path(out, required_lib_paths("npu"))
    return out
```

`resolve_model_path` 替换为：

```python
# 布局谓词:快照子目录含此文件才算对应格式的有效模型
_LAYOUT_PREDICATE = {"ct2": "model.bin", "ov": "openvino_encoder_model.xml"}


def resolve_model_path(model: str = None, base: str = None, layout: str = "ct2") -> str:
    """选第一个含布局谓词文件的快照子目录(#11 逻辑按模型名+格式泛化)。

    layout="ct2"(默认):谓词 model.bin,与历史行为逐字一致(A5 契约);
    layout="ov":谓词 openvino_encoder_model.xml(OpenVINO/GenAI 布局)。
    base 可注入供单测造布局;找不到抛 RuntimeError,错误信息按布局区分
    下载命令(ct2 -> download-model.sh;ov -> huggingface-cli)。
    """
    if layout not in _LAYOUT_PREDICATE:
        raise ValueError(f"[voice-input] unknown model layout {layout!r}")
    predicate = _LAYOUT_PREDICATE[layout]
    if model is None:
        model = model_name(os.environ)
    if base is None:
        base = ov_snapshots_base(model) if layout == "ov" else snapshots_base(model)
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            cand = os.path.join(base, name)
            if os.path.isfile(os.path.join(cand, predicate)):
                return cand
    hint = (f"./download-model.sh {model}" if layout == "ct2"
            else f"huggingface-cli download OpenVINO/whisper-{model}")
    raise RuntimeError(
        f"Whisper model '{model}' ({layout}) not found under {base} — run {hint}"
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestLibPaths test_engine.TestOvResolve -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause 2>&1 | tail -3`
Expected: `OK`（resolve_model_path 默认 layout="ct2"，既有测试不受影响）

- [ ] **Step 6: Commit**

```bash
git -C $WT add engine.py test_engine.py
git -C $WT commit -m "feat(engine): library-path helpers, engine-aware default model, OV layout resolution (#19)"
```

---

### Task 2: engine.py NPU adapter + build_model npu 分支

**验收归属：A1（npu_construct）、A3（adapter）、A5（npu 占位测试改写）**

**Files:**
- Modify: `$WT/engine.py`（追加 `_load_wav_samples` / `_NpuWhisperAdapter` / `NPU_COMPILE_CACHE`；改 `build_model` / `construction_kwargs`）
- Test: `$WT/test_engine.py`（改写两处 npu 占位测试；追加 TestNpuConstruction / TestNpuAdapter）

**Interfaces:**
- Consumes: Task 1 的 `resolve_model_path(layout="ov")` / `default_model`
- Produces:
  - `engine.NPU_COMPILE_CACHE: str` = expanduser("~/.cache/voice-input/npu-compile-cache")
  - `engine._load_wav_samples(path: str)` → numpy float32 数组
  - `engine._NpuWhisperAdapter(model_dir, pipeline_factory, warn=None)`，`.transcribe(wav, language="zh", initial_prompt=None, hotwords=None, **_ignored) -> (segments, info)`
  - `build_model(..., pipeline_factory=None, model_base=None)`（新增两个可选注入缝，默认行为不变）

- [ ] **Step 1: 改写两处 npu 占位测试 + 写新失败测试**

`$WT/test_engine.py:138-141` 的 `test_npu_build_raises_not_implemented` **删除**；`:214-216` 的 `test_npu_raises_not_implemented` 改写为：

```python
    def test_construction_kwargs_npu_value_error(self):
        # #19: npu 不再有 faster-whisper 构造参数——"不适用"而非"未实现"
        with self.assertRaises(ValueError) as ctx:
            engine.construction_kwargs("npu")
        self.assertIn("build_model", str(ctx.exception))
```

追加到文件末尾：

```python
class _FakeGenaiPipe:
    """记录 generate 调用的假 WhisperPipeline(A1/A3 用,不碰 openvino_genai)。"""

    def __init__(self, texts=("你好",)):
        self.texts = list(texts)
        self.generate_calls = []

    def generate(self, samples, **kwargs):
        self.generate_calls.append({"n_samples": len(samples), "kwargs": kwargs})
        from types import SimpleNamespace
        return SimpleNamespace(texts=self.texts)


class _FakePipeFactory:
    """记录构造 kwargs 的假 WhisperPipeline 工厂。"""

    def __init__(self, pipe=None):
        self.calls = []
        self.pipe = pipe or _FakeGenaiPipe()

    def __call__(self, model_dir, **kwargs):
        self.calls.append({"model_dir": model_dir, "kwargs": kwargs})
        return self.pipe


def _make_ov_base(root: str, model: str = "small-int8-ov") -> str:
    """tmpdir 造 OV 快照布局,返回 snapshots 目录(resolve_model_path base 注入)。"""
    base = os.path.join(root, f"models--OpenVINO--whisper-{model}", "snapshots")
    snap = os.path.join(base, "abc123")
    os.makedirs(snap)
    open(os.path.join(snap, "openvino_encoder_model.xml"), "wb").close()
    return base


def _write_wav(path: str, samples=((0, 16384, -16384)), rate=16000, width=2, channels=1):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            import struct
            w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        else:
            w.writeframes(bytes(len(samples) * width * channels))


class TestNpuConstruction(unittest.TestCase):
    """#19 A1: npu 构造参数(device/NPU_PLATFORM/STATIC_PIPELINE/CACHE_DIR/模型路径)。"""

    def test_npu_build_constructs_adapter_with_static_kwargs(self):
        factory = _FakePipeFactory()
        with tempfile.TemporaryDirectory() as root:
            base = _make_ov_base(root)
            m = engine.build_model(engine="npu", model="small-int8-ov",
                                   pipeline_factory=factory, model_base=base)
        self.assertIsInstance(m, engine._NpuWhisperAdapter)
        self.assertEqual(len(factory.calls), 1)
        call = factory.calls[0]
        self.assertTrue(call["model_dir"].endswith("abc123"))
        self.assertEqual(call["kwargs"], {
            "device": "NPU",
            "NPU_PLATFORM": "NPU4000",
            "STATIC_PIPELINE": True,
            "CACHE_DIR": engine.NPU_COMPILE_CACHE,
        })

    def test_npu_default_model_when_env_unset(self):
        factory = _FakePipeFactory()
        with tempfile.TemporaryDirectory() as root:
            base = _make_ov_base(root)  # small-int8-ov 布局
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VOICE_INPUT_MODEL", None)
                engine.build_model(engine="npu", pipeline_factory=factory,
                                   model_base=base)
        self.assertIn("abc123", factory.calls[0]["model_dir"])

    def test_npu_missing_model_actionable(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as ctx:
                engine.build_model(engine="npu", model="small-int8-ov",
                                   pipeline_factory=_FakePipeFactory(), model_base=root)
            self.assertIn("huggingface-cli download", str(ctx.exception))


class TestNpuAdapter(unittest.TestCase):
    """#19 A3: adapter 兼容契约 / 热词降级 warn / wav 16-bit 硬校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = os.path.join(self.tmp.name, "t.wav")
        _write_wav(self.wav)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_adapter(self, texts=("你好",), pipe=None):
        pipe = pipe or _FakeGenaiPipe(texts)
        return engine._NpuWhisperAdapter("/fake/model", pipeline_factory=lambda d, **kw: pipe), pipe

    def test_adapter_transcribe_contract(self):
        adapter, pipe = self._make_adapter(texts=["你好", "世界"])
        segments, info = adapter.transcribe(self.wav, language="zh")
        self.assertEqual("".join(s.text for s in segments), "你好 世界")
        self.assertEqual(info.language, "zh")
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {"language": "zh"})
        self.assertEqual(pipe.generate_calls[0]["n_samples"], 3)

    def test_adapter_empty_result_empty_segments(self):
        adapter, _ = self._make_adapter(texts=[])
        segments, info = adapter.transcribe(self.wav)
        self.assertEqual(segments, [])

    def test_adapter_hotword_degradation_warns(self):
        warns = []
        pipe = _FakeGenaiPipe()
        adapter = engine._NpuWhisperAdapter(
            "/fake/model", pipeline_factory=lambda d, **kw: pipe, warn=warns.append)
        adapter.transcribe(self.wav, language="zh",
                           initial_prompt="术语:重构", hotwords="HoloWord")
        self.assertEqual(len(warns), 1)
        self.assertIn("not supported", warns[0])
        # generate 不得收到 initial_prompt/hotwords(NPU 静态管线 C++ 硬拒)
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {"language": "zh"})

    def test_adapter_no_warn_without_prompt(self):
        warns = []
        pipe = _FakeGenaiPipe()
        adapter = engine._NpuWhisperAdapter(
            "/fake/model", pipeline_factory=lambda d, **kw: pipe, warn=warns.append)
        adapter.transcribe(self.wav, language="zh")
        self.assertEqual(warns, [])

    def test_adapter_ignores_extra_kwargs(self):
        adapter, pipe = self._make_adapter()
        adapter.transcribe(self.wav, language="zh", beam_size=5, task="transcribe")
        self.assertEqual(pipe.generate_calls[0]["kwargs"], {"language": "zh"})

    def test_load_wav_samples_int16_normalized(self):
        import numpy as np
        arr = engine._load_wav_samples(self.wav)
        self.assertEqual(arr.dtype, np.float32)
        np.testing.assert_allclose(arr, [0.0, 0.5, -0.5], atol=1e-4)

    def test_load_wav_samples_rejects_24bit(self):
        wav24 = os.path.join(self.tmp.name, "24.wav")
        _write_wav(wav24, width=3)
        with self.assertRaises(ValueError) as ctx:
            engine._load_wav_samples(wav24)
        self.assertIn("采样宽度", str(ctx.exception))

    def test_load_wav_samples_warns_non_16k(self):
        import contextlib, io
        wav8k = os.path.join(self.tmp.name, "8k.wav")
        _write_wav(wav8k, rate=8000)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            engine._load_wav_samples(wav8k)
        self.assertIn("重采样", buf.getvalue())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestNpuConstruction test_engine.TestNpuAdapter -v 2>&1 | tail -5 && venv/bin/python3 -m unittest test_engine -v -k construction_kwargs 2>&1 | tail -5`
Expected: 新两类 FAIL（`AttributeError: ... '_NpuWhisperAdapter'`）；改写后的 construction_kwargs 测试 FAIL（仍抛 NotImplementedError 而非 ValueError）

- [ ] **Step 3: 实现**（`$WT/engine.py`）

文件顶部 import 区加 `from types import SimpleNamespace`。常量区（NPU_LIB_DIR 旁）加：

```python
# NPU 静态编译缓存:冷 ~47s 一次性,热 0.8s 跨进程生效(2026-09-29 本机实测,#19)
NPU_COMPILE_CACHE = os.path.expanduser("~/.cache/voice-input/npu-compile-cache")
```

`construction_kwargs` 末尾的 `raise NotImplementedError(...)` 替换为：

```python
    # npu 没有 faster-whisper 侧构造参数(OpenVINO 管线走 build_model 的 adapter)——
    # "不适用"而非"未实现"(#19)
    raise ValueError(
        "[voice-input] npu engine has no faster-whisper construction kwargs; "
        "use build_model() (OpenVINO pipeline)"
    )
```

文件末尾追加：

```python
def _load_wav_samples(path: str):
    """wav(16-bit PCM) -> float32 采样序列(genai generate 的入参形态)。

    校验先于 numpy 导入(拒绝路径不依赖重依赖);16-bit 硬校验沿 bench 语义
    (24-bit 会被 int16 解读成垃圾样本且静默成功,必须硬拒)。
    """
    import wave

    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            raise ValueError(
                f"wav 采样宽度 {w.getsampwidth()} 字节 ≠ 2(int16);"
                "先转成 16kHz mono 16-bit(arecord -f S16_LE 即是)"
            )
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(
                f"[voice-input] warn: wav 非 16kHz mono(实际 {w.getframerate()}Hz "
                f"{w.getnchannels()}ch),whisper 内部会重采样",
                file=sys.stderr,
            )
        data = w.readframes(w.getnframes())

    import numpy as np  # 懒导入:模块顶层保持纯标准库

    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class _NpuWhisperAdapter:
    """openvino_genai WhisperPipeline -> faster-whisper WhisperModel 形态适配(#19)。

    暴露同签名 transcribe(),返回 (segments, info)——调用方(voice-ptt.py /
    transcribe_once.py)零改动。NPU 静态管线 C++ 硬拒 initial_prompt/hotwords
    (pipeline_static.cpp:1145),按 terms.py 原则降级告警、绝不阻断转写。
    """

    def __init__(self, model_dir: str, pipeline_factory, warn=None):
        # NPU_PLATFORM: 驱动 1.38.0 仍不上报平台描述,AUTO_DETECT 失败(#17);
        # STATIC_PIPELINE: NPU 官方要求;CACHE_DIR: 静态编译跨进程缓存
        self._pipe = pipeline_factory(
            model_dir,
            device="NPU",
            NPU_PLATFORM="NPU4000",
            STATIC_PIPELINE=True,
            CACHE_DIR=NPU_COMPILE_CACHE,
        )
        self._warn = warn if warn is not None else _default_warn

    def transcribe(self, wav, language="zh", initial_prompt=None, hotwords=None,
                   **_ignored):
        if initial_prompt or hotwords:
            self._warn(
                "[voice-input] NPU engine: initial_prompt/hotwords not supported "
                "by the static pipeline — terms degraded (ignored), "
                "transcribing without them"
            )
        samples = _load_wav_samples(wav)
        result = self._pipe.generate(samples, language=language)
        text = self._extract_text(result)
        segments = [SimpleNamespace(text=text)] if text else []
        return segments, SimpleNamespace(language=language)

    @staticmethod
    def _extract_text(result) -> str:
        """兼容 generate 返回值形态(str / .text / .texts 列表)。沿 bench 语义。"""
        if isinstance(result, str):
            return result.strip()
        for attr in ("text", "texts"):
            v = getattr(result, attr, None)
            if isinstance(v, str):
                return v.strip()
            if isinstance(v, list):
                return " ".join(str(x) for x in v).strip()
        return str(result).strip()
```

`build_model` 整函数替换为：

```python
def build_model(engine: str = None, model: str = None,
                whisper_factory=None, warn=None,
                pipeline_factory=None, model_base: str = None):
    """按引擎构造并返回转写模型(cwd 环境变量决定 engine/model,可显式传参覆盖)。

    whisper_factory / pipeline_factory / warn / model_base 为单测注入缝:
    工厂未注入时才懒 import 对应推理栈,本模块被 import 时不拉起任何重依赖。
    model_base 注入快照根目录(单测造布局);None 时行为与历史逐字一致。

    分支:
      cuda  构造参数是 HEAD load_model 原文逐字搬移(#16 契约,勿改)
      cpu   device="cpu", compute_type="int8"(无 NVIDIA 机器的兜底引擎)
      auto  先试 cuda,失败 warn 后降级 cpu int8(仅显式选用,默认不做)
      npu   _NpuWhisperAdapter(openvino_genai WhisperPipeline,静态管线+编译缓存)
    """
    if engine is None:
        engine = engine_name(os.environ)
    else:
        _validated_engine(engine)
    if model is None:
        model = os.environ.get(ENV_MODEL, default_model(engine))
    if warn is None:
        warn = _default_warn

    if engine == "npu":
        if pipeline_factory is None:
            from openvino_genai import WhisperPipeline as pipeline_factory
        path = resolve_model_path(model, base=model_base, layout="ov")
        return _NpuWhisperAdapter(path, pipeline_factory, warn=warn)

    if whisper_factory is None:
        from faster_whisper import WhisperModel as whisper_factory

    path = resolve_model_path(model, base=model_base)

    if engine == "auto":
        try:
            return whisper_factory(path, **construction_kwargs("cuda"))
        except Exception as e:
            warn(
                f"[voice-input] {ENV_ENGINE}=auto: cuda load failed ({e}); "
                "falling back to cpu int8"
            )
            return whisper_factory(path, **construction_kwargs("cpu"))
    return whisper_factory(path, **construction_kwargs(engine))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestNpuConstruction test_engine.TestNpuAdapter -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause 2>&1 | tail -3`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git -C $WT add engine.py test_engine.py
git -C $WT commit -m "feat(engine): NPU adapter for openvino-genai WhisperPipeline (#19)"
```

---

### Task 3: transcribe_once.py preamble 收敛 + npu 缺路径检查

**验收归属：A6（transcribe_once_env）**

**Files:**
- Modify: `$WT/transcribe_once.py`（ preamble 块，约 :17-45）
- Test: `$WT/test_engine.py`（追加 TestTranscribeOnceEnv）

**Interfaces:**
- Consumes: Task 1 的 `engine.required_lib_paths` / `has_library_paths` / `prepend_library_path` / `NPU_LIB_DIR`

- [ ] **Step 1: 写失败测试**（追加到 `$WT/test_engine.py` 末尾）

```python
class TestTranscribeOnceEnv(unittest.TestCase):
    """#19 A6: transcribe_once preamble 收敛后输出等价 + npu 缺路径可行动报错。

    干净子进程 import transcribe_once(模块顶层副作用会设置 LD_LIBRARY_PATH),
    打印结果供比对。沿用本文件既有子进程 harness 内联模式(helper 抽取债挂账)。
    """

    REPO = os.path.dirname(os.path.realpath(__file__))
    VENV_PY = os.path.join(REPO, "venv", "bin", "python3")

    def _import_in_subprocess(self, env_extra):
        code = (
            "import os, sys; sys.path.insert(0, %r);"
            "import transcribe_once;"
            "print(os.environ.get('LD_LIBRARY_PATH', ''))" % self.REPO
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "VOICE_INPUT_ENGINE",
                            "VOICE_INPUT_MODEL")}
        env.update(env_extra)
        p = subprocess.run([self.VENV_PY, "-c", code],
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout.strip(), p.stderr

    def _venv_site(self):
        return subprocess.check_output(
            [self.VENV_PY, "-c",
             "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            text=True).strip()

    def test_transcribe_once_env_cuda_preamble_byte_equal_legacy(self):
        site = self._venv_site()
        rc, out, err = self._import_in_subprocess({"LD_LIBRARY_PATH": "/opt/keep"})
        self.assertEqual(rc, 0, err)
        # HEAD 历史串逐字节: f"{site}/nvidia/cublas/lib:{site}/nvidia/cudnn/lib:{site}/nvidia/cuda_nvrtc/lib:{old}"
        self.assertEqual(
            out,
            f"{site}/nvidia/cublas/lib:{site}/nvidia/cudnn/lib:"
            f"{site}/nvidia/cuda_nvrtc/lib:/opt/keep")

    def test_transcribe_once_env_npu_missing_path_actionable(self):
        rc, out, err = self._import_in_subprocess({"VOICE_INPUT_ENGINE": "npu"})
        self.assertEqual(rc, 1)
        self.assertIn("/usr/lib/x86_64-linux-gnu", err)
        self.assertIn("export LD_LIBRARY_PATH", err)

    def test_transcribe_once_env_npu_with_path_ok(self):
        rc, out, err = self._import_in_subprocess({
            "VOICE_INPUT_ENGINE": "npu",
            "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu",
        })
        self.assertEqual(rc, 0, err)
        self.assertIn("/usr/lib/x86_64-linux-gnu", out)

    def test_transcribe_once_env_cpu_no_nvidia_paths(self):
        # 惰性差异(spec Design #4):cpu 不再设置 nvidia 路径,旧值原样保留
        rc, out, err = self._import_in_subprocess({
            "VOICE_INPUT_ENGINE": "cpu", "LD_LIBRARY_PATH": "/opt/keep"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "/opt/keep")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestTranscribeOnceEnv -v`
Expected: `test_..._npu_missing_path_actionable` FAIL（现状不检查，rc=0）；`test_..._cpu_no_nvidia_paths` FAIL（现状无条件前置 nvidia 路径）

- [ ] **Step 3: 实现**（`$WT/transcribe_once.py`）

替换 `import engine` 之前/之后的 preamble 区（现状：`_SITE` 探测 → 守卫 → `os.environ["LD_LIBRARY_PATH"] = (nvidia 三段)` → `import engine`）为：

```python
try:
    _SITE = subprocess.check_output(
        [
            os.path.join(VENV, "bin", "python3"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
except Exception:
    _SITE = ""
if not _SITE or not os.path.isdir(_SITE):
    sys.stderr.write(
        f"[voice-input] venv site-packages not found under {VENV} — "
        "see README Installation\n"
    )
    sys.exit(1)

import engine  # 顶层纯标准库(#16);preamble 计算收敛到 engine 单点(#19 债①)

try:
    _ENG = engine.engine_name(dict(os.environ))
    _LIB_PATHS = engine.required_lib_paths(_ENG, _SITE)
except ValueError as e:
    # 非法引擎值:干净退出不裸 traceback(与 main() 内错误路径同款)
    sys.stderr.write(f"[voice-input] {e}\n")
    sys.exit(1)

# npu 的库路径必须在进程启动时就在 LD_LIBRARY_PATH 里(ld.so 只读一次,
# 进程内改无效——KB A/B 实证);缺失时给可行动报错,不做 re-exec 魔法
if _ENG == "npu" and not engine.has_library_paths(dict(os.environ), _LIB_PATHS):
    sys.stderr.write(
        "[voice-input] NPU engine requires LD_LIBRARY_PATH to contain "
        f"{engine.NPU_LIB_DIR} at process start (ld.so reads it once).\n"
        "  export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu"
        "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n"
        "  or launch via voice-ptt.sh / voice-toggle.sh / voice_hold "
        "(they inject it).\n"
    )
    sys.exit(1)

# cuda/auto 与 HEAD 输出逐字节一致(A6 钉死);cpu 不设置任何路径(惰性差异,
# spec Design #4);本进程内设置只影响子进程继承与个别 dlopen 场景
os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(
    dict(os.environ), _LIB_PATHS)
```

`main()` 与 `__main__` 块不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestTranscribeOnceEnv -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + Commit**

```bash
cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause 2>&1 | tail -3
git -C $WT add transcribe_once.py test_engine.py
git -C $WT commit -m "refactor(transcribe_once): converge LD_LIBRARY_PATH preamble into engine.py, actionable npu env error (#19)"
```

---

### Task 4: voice_hold.py spawn env 注入

**验收归属：A4 child_env 的集成点（单测在 Task 1）；U1 前置**

**Files:**
- Modify: `$WT/voice_hold.py`（import 区 + `_deliver` 的 check_output 调用，约 :249）

**Interfaces:**
- Consumes: Task 1 的 `engine.child_env(env: dict) -> dict`

- [ ] **Step 1: 实现**（两处小改）

import 区（`import time` 之后、常量区之前）加：

```python
import engine  # 顶层纯标准库(#16 同款);npu 子进程 env 注入的唯一来源(#19)
```

`_deliver` 中的 check_output 调用改为：

```python
                text = subprocess.check_output(
                    [VENV_PY, os.path.join(REPO_DIR, "transcribe_once.py"),
                     WAVFILE],
                    # npu 时注入 NPU 库目录(ld.so 只在子进程启动读一次);
                    # 其他引擎原样拷贝,维持现状(#19)
                    env=engine.child_env(self.env),
                    stderr=sys.stderr, text=True,
                    timeout=self._transcribe_timeout()).strip()
```

- [ ] **Step 2: 回归 + 纯度契约确认**

Run: `cd $WT && venv/bin/python3 -m unittest test_hold -v 2>&1 | tail -3`
Expected: `OK`（engine 顶层纯标准库，test_hold 的「import 无副作用」契约不破）

- [ ] **Step 3: Commit**

```bash
git -C $WT add voice_hold.py
git -C $WT commit -m "feat(voice_hold): inject NPU library path into transcribe subprocess env (#19)"
```

---

### Task 5: 三个 shell wrapper 的 export 收敛 + 预检改造

**验收归属：A8（wrapper 等价性）**

**Files:**
- Modify: `$WT/voice-ptt.sh`（:15 export 行）
- Modify: `$WT/voice-toggle.sh`（:14 export 行 + 预检块 :63-72）
- Modify: `$WT/test-mic.sh`（:14 export 行 + 预检块 :25-36）
- Test: `$WT/test_engine.py`（追加 TestWrapperExport）

**Interfaces:**
- Consumes: Task 1 的 `engine.required_lib_paths` / `default_model` / `resolve_model_path(layout=…)`

- [ ] **Step 1: 写失败测试**（追加到 `$WT/test_engine.py` 末尾）

```python
class TestWrapperExport(unittest.TestCase):
    """#19 A8: wrapper 的 export 计算与收敛后语义一致(cuda 逐字节/npu 正确/cpu 空)。

    测的是 wrapper 里那段 python 计算片段本身(与 voice-ptt.sh/voice-toggle.sh/
    test-mic.sh 内嵌代码逐字相同),而非整个 wrapper——wrapper 端到端需 fake venv,
    超出最低成本层级;bash -n 语法检查在 Task 步骤里跑。
    """

    REPO = os.path.dirname(os.path.realpath(__file__))
    VENV_PY = os.path.join(REPO, "venv", "bin", "python3")
    SITE = "/site"
    SNIPPET = (
        "import os, sys; sys.path.insert(0, %r); import engine; "
        "print(':'.join(engine.required_lib_paths("
        "engine.engine_name(dict(os.environ)), %r)))" % (REPO, SITE)
    )

    def _run(self, env_extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("VOICE_INPUT_ENGINE", "VOICE_INPUT_MODEL")}
        env.update(env_extra)
        p = subprocess.run([self.VENV_PY, "-c", self.SNIPPET],
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout.strip(), p.stderr

    def test_wrapper_export_unset_defaults_cuda_legacy_order(self):
        rc, out, _ = self._run({})
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"{self.SITE}/nvidia/cublas/lib:"
                              f"{self.SITE}/nvidia/cudnn/lib:"
                              f"{self.SITE}/nvidia/cuda_nvrtc/lib")

    def test_wrapper_export_npu(self):
        rc, out, _ = self._run({"VOICE_INPUT_ENGINE": "npu"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "/usr/lib/x86_64-linux-gnu")

    def test_wrapper_export_cpu_empty(self):
        rc, out, _ = self._run({"VOICE_INPUT_ENGINE": "cpu"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
```

- [ ] **Step 2: 跑测试确认通过**（计算层 Task 1 已就绪，此测试直接应绿）

Run: `cd $WT && venv/bin/python3 -m unittest test_engine.TestWrapperExport -v`
Expected: PASS（若 FAIL 说明 Task 1 语义有漏，先修 Task 1）

- [ ] **Step 3: 改三个 wrapper**

三个文件中相同的 export 行：

```bash
export LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

统一替换为：

```bash
# 库路径前置收敛到 engine.py 单点(#19 债①),按 VOICE_INPUT_ENGINE 计算:
# cuda/auto -> venv nvidia pip 三路径(输出与历史逐字节一致,A8 钉死);
# npu -> ze 驱动库目录(ld.so 只在进程启动读 LD_LIBRARY_PATH,必须在 exec 前导出);
# cpu -> 空(惰性差异,spec Design #4)
LIB_PATHS="$("$VENV/bin/python3" -c "
import os, sys
sys.path.insert(0, '$REPO_DIR')
import engine
try:
    print(':'.join(engine.required_lib_paths(engine.engine_name(dict(os.environ)), '$SITE_PACKAGES')))
except ValueError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
")" || exit 1
if [ -n "$LIB_PATHS" ]; then
    export LD_LIBRARY_PATH="$LIB_PATHS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
```

voice-toggle.sh 与 test-mic.sh 的预检块，删除：

```python
    if eng == 'npu':
        raise NotImplementedError('NPU engine not implemented yet - see issue #19')
    engine.resolve_model_path()
```

替换为：

```python
    # #19: npu 已实现——按引擎选模型布局与默认模型,毫秒级不加载模型
    model = os.environ.get(engine.ENV_MODEL) or engine.default_model(eng)
    engine.resolve_model_path(model, layout='ov' if eng == 'npu' else 'ct2')
```

（`except (ValueError, RuntimeError, NotImplementedError)` 中的 `NotImplementedError` 一并删掉，两处都是。）

- [ ] **Step 4: 语法检查 + 静态确认 + cuda 等价实测**

```bash
bash -n $WT/voice-ptt.sh && bash -n $WT/voice-toggle.sh && bash -n $WT/test-mic.sh && echo SYNTAX-OK
# 静态:硬编码 nvidia 行已消失,收敛调用存在于三个 wrapper
grep -l "nvidia/cublas" $WT/voice-ptt.sh $WT/voice-toggle.sh $WT/test-mic.sh && echo "FAIL: 硬编码残留" || echo "OK: 无硬编码残留"
grep -c "required_lib_paths" $WT/voice-ptt.sh $WT/voice-toggle.sh $WT/test-mic.sh  # 期望各 ≥1
# 数值:默认(cuda,不设 ENGINE)下真 venv 复跑 export 计算,与历史前缀逐字节一致
cd $WT
SITE="$(venv/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
env -u VOICE_INPUT_ENGINE venv/bin/python3 -c "
import os, sys; sys.path.insert(0, '.')
import engine
print(':'.join(engine.required_lib_paths(engine.engine_name(dict(os.environ)), '$SITE')))
"
```
Expected 输出: `<site>/nvidia/cublas/lib:<site>/nvidia/cudnn/lib:<site>/nvidia/cuda_nvrtc/lib`（与 HEAD export 行前缀逐字节一致）

- [ ] **Step 5: 全量回归 + Commit**

```bash
cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause 2>&1 | tail -3
git -C $WT add voice-ptt.sh voice-toggle.sh test-mic.sh test_engine.py
git -C $WT commit -m "refactor(wrappers): engine-aware LD_LIBRARY_PATH export, real npu precheck (#19)"
```

---

### Task 6: voice-ptt.py preamble 收敛（上屏段不动）

**验收归属：A7（7700K 零行为变化证明）**

**Files:**
- Modify: `$WT/voice-ptt.py`（preamble 块 :33-38 + 去掉 :46 的重复 import engine）

**Interfaces:**
- Consumes: Task 1 的 `engine.required_lib_paths` / `prepend_library_path`

- [ ] **Step 1: 实现**

`:33-38` 的块：

```python
os.environ["LD_LIBRARY_PATH"] = (
    f"{_SITE}/nvidia/cublas/lib:"
    f"{_SITE}/nvidia/cudnn/lib:"
    f"{_SITE}/nvidia/cuda_nvrtc/lib"
    + (f':{os.environ.get("LD_LIBRARY_PATH", "")}')
)
```

替换为：

```python
# preamble 计算收敛到 engine.py 单点(#19 债①);cuda 下输出与历史逐字节一致
# (A4 钉死)。engine 顶层纯标准库,先 import 再设置安全;真正生效的注入在
# voice-ptt.sh(进程启动前置),本进程内设置只影响子进程继承
import engine
try:
    os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(
        dict(os.environ),
        engine.required_lib_paths(engine.engine_name(dict(os.environ)), _SITE),
    )
except ValueError as e:
    # 非法引擎值:干净退出不裸 traceback(与 main() 内错误路径同款)
    sys.stderr.write(f"[voice-input] {e}\n")
    sys.exit(1)
```

同时删除 `:46` 重复的 `import engine` 行。**上屏段（:231-245，xsel/xdotool）一行不动。**

- [ ] **Step 2: A7 静态三查**

```bash
# 1) diff 中上屏段零改动(xsel/xdotool 行不出现增删)
git -C $WT diff main -- voice-ptt.py | grep -E "^[-+].*(xsel|xdotool)" && echo "FAIL: 上屏段被动了" || echo "OK-1: 上屏段零改动"
# 2) 源内无硬编码 nvidia 库路径残留
grep -n "nvidia/cublas" $WT/voice-ptt.py && echo "FAIL: 硬编码残留" || echo "OK-2: 无硬编码残留"
# 3) import engine 先于 preamble 设置(行号前者 < 后者)
python3 - <<'EOF'
lines = open("/home/elling/work/git-repo/voice-input/.pi/worktrees/issue-19-npu-engine/voice-ptt.py").readlines()
imp = next(i for i, l in enumerate(lines) if l.strip() == "import engine")
setld = next(i for i, l in enumerate(lines) if "prepend_library_path" in l)
print("OK-3: import 先于设置" if imp < setld else "FAIL: 顺序反了")
EOF
```
Expected: 三个 OK

- [ ] **Step 3: Commit**

```bash
git -C $WT add voice-ptt.py
git -C $WT commit -m "refactor(voice-ptt): converge nvidia preamble into engine.py (paste block untouched) (#19)"
```

---

### Task 7: bench/npu-bench.py 收编第 6 份拷贝

**验收归属：A5（回归含 test_bench）**

**Files:**
- Modify: `$WT/bench/npu-bench.py`（删 `NPU_LIB_DIR`/`_lib_dir_present`/`set_npu_library_path`；三处调用点改 engine）
- Test: `$WT/test_bench.py`（删 set_npu_library_path 的 4 个测试——语义已由 Task 1 的 TestLibPaths 覆盖）

**Interfaces:**
- Consumes: Task 1 的 `engine.prepend_library_path` / `has_library_paths` / `required_lib_paths`

- [ ] **Step 1: 先跑现状 test_bench 确认基线绿**

Run: `cd $WT && venv/bin/python3 -m unittest test_bench -v 2>&1 | tail -3`
Expected: `OK`（基线）

- [ ] **Step 2: 实现**

`bench/npu-bench.py` 顶部（stdlib imports 之后）加：

```python
# engine.py 在仓库根目录(bench/ 上一级);顶层纯标准库,import 不破纯度契约
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import engine
```

删除 `NPU_LIB_DIR` 常量、`_lib_dir_present`、`set_npu_library_path` 三个定义。三处调用点替换：

`run_bench` 内 `set_npu_library_path(os.environ)` →

```python
    engine.prepend_library_path(os.environ, engine.required_lib_paths("npu"))  # NPU 枚举前置
```

`needs_reexec_for_npu` 函数体 →

```python
def needs_reexec_for_npu(env: dict) -> bool:
    """True 当 env 缺 NPU 库路径且未带 reexec 哨兵(纯函数,单测覆盖)。

    路径判定收敛到 engine.has_library_paths(#19);哨兵语义不变。
    """
    if env.get("_NPU_BENCH_REEXEC"):
        return False
    return not engine.has_library_paths(env, engine.required_lib_paths("npu"))
```

`main` 内 `set_npu_library_path(env)` → `engine.prepend_library_path(env, engine.required_lib_paths("npu"))`

`test_bench.py`：删除测试 `set_npu_library_path` 的 4 个方法（约 :60-85，含 `bench.set_npu_library_path` 与 `bench.NPU_LIB_DIR` 引用），其余不动。

- [ ] **Step 3: 跑 test_bench 确认绿**

Run: `cd $WT && venv/bin/python3 -m unittest test_bench -v 2>&1 | tail -3`
Expected: `OK`（needs_reexec / reexec_command / 纯度 / format_report 等断言不变应仍绿）

- [ ] **Step 4: Commit**

```bash
git -C $WT add bench/npu-bench.py test_bench.py
git -C $WT commit -m "refactor(bench): drop 6th library-path copy, use engine helpers (#19)"
```

---

### Task 8: download-model.py 文案 + contrib service 示例 + README 双语 NPU 节

**验收归属：spec 矩阵外文档项，支撑 U1/U3 可执行性（实测按此配置进行）**

**Files:**
- Modify: `$WT/download-model.py`（:87-89）
- Modify: `$WT/contrib/voice-hold.service`
- Modify: `$WT/README.md`（`:192` 的 `### Custom vocabulary (hotwords)` 标题前插入新节）
- Modify: `$WT/README.zh-CN.md`（`:169` 的 `### 自定义词汇（热词）` 标题前插入新节）

- [ ] **Step 1: download-model.py**

```python
        if eng == "npu":
            # #19: npu 已实现但用 OpenVINO 格式模型,本脚本只下载 faster-whisper 格式
            print("npu engine uses OpenVINO models — see README "
                  "(huggingface-cli download OpenVINO/whisper-small-int8-ov)",
                  file=sys.stderr)
            sys.exit(1)
```

- [ ] **Step 2: contrib/voice-hold.service**

`[Service]` 段的 Environment 三行替换为：

```ini
# NPU 引擎(#19):首次转写一次性静态编译 ~47s(默认超时 120s 兜得住),
# 之后热缓存 ~1-2s/次;编译缓存 ~/.cache/voice-input/npu-compile-cache
# (~850MB,可 rm 重建,代价是一次 47s 重编译)。
# 无需 LD_LIBRARY_PATH——voice_hold 会给转写子进程自动注入。
# cpu 备选(无 NPU 机器):VOICE_INPUT_ENGINE=cpu + VOICE_INPUT_MODEL=small
Environment=VOICE_INPUT_ENGINE=npu
Environment=VOICE_INPUT_MODEL=small-int8-ov
Environment=VOICE_INPUT_TRANSCRIBE_TIMEOUT=120
```

- [ ] **Step 3: README.md**（`### Custom vocabulary (hotwords)` 前插入）

```markdown
### NPU engine (Intel AI Boost, e.g. Lunar Lake)

`VOICE_INPUT_ENGINE=npu` transcribes on the Intel NPU via openvino-genai's
`WhisperPipeline` (static pipeline). Measured on an OmniBook (Core Ultra 258V,
whisper-small int8): **~0.3s per 8s dictation vs ~3.4s on CPU — about 12× faster**
(benchmark: #17).

Prerequisites (all three):

- NPU driver ≥ 1.38.0 (`intel-npu-driver-bin`; older drivers fail at graph import)
- `openvino` + `openvino-genai` in the venv (`venv/bin/pip install openvino openvino-genai`)
- `LD_LIBRARY_PATH` containing `/usr/lib/x86_64-linux-gnu` **at process start** —
  voice-ptt.sh / voice-toggle.sh / voice_hold inject it automatically. For direct
  CLI use: `export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}`

Model (OpenVINO format — separate from the faster-whisper models):

```bash
huggingface-cli download OpenVINO/whisper-small-int8-ov
```

Under `engine=npu`, `VOICE_INPUT_MODEL` defaults to `small-int8-ov`.

Notes:

- The first transcription compiles the static pipeline once (~47s, within the
  default 120s timeout). A compile cache (`~/.cache/voice-input/npu-compile-cache`,
  ~850MB) cuts later model loads to ~0.8s. Safe to delete — rebuilt on next run
  (one 47s recompile).
- Hotwords (`terms.json`) are NOT supported by the NPU static pipeline: the engine
  prints a warning and transcribes without them (never blocks dictation). On the
  Wayland hold-to-talk path, hotwords are not wired at all (transcribe_once.py does
  not load terms — pre-existing behavior, unchanged).
- Each dictation still pays ~0.8s model load (subprocess-per-dictation design, #18);
  a resident transcription worker is tracked as #25.
```

- [ ] **Step 4: README.zh-CN.md**（`### 自定义词汇（热词）` 前插入）

```markdown
### NPU 引擎（Intel AI Boost，如 Lunar Lake）

`VOICE_INPUT_ENGINE=npu` 经 openvino-genai 的 `WhisperPipeline`（静态管线）在 Intel
NPU 上转写。OmniBook（Core Ultra 258V，whisper-small int8）实测：**8 秒语音约 0.3s，
对比 CPU 约 3.4s——快约 12 倍**（基准见 #17）。

三个前置（缺一不可）：NPU 驱动 ≥ 1.38.0；venv 装 `openvino` + `openvino-genai`；
进程启动时 `LD_LIBRARY_PATH` 含 `/usr/lib/x86_64-linux-gnu`（三个 wrapper 与
voice_hold 会自动注入；手工跑 CLI 需自己 export）。

模型是 OpenVINO 格式（与 faster-whisper 模型分开）：
`huggingface-cli download OpenVINO/whisper-small-int8-ov`；`engine=npu` 时
`VOICE_INPUT_MODEL` 默认 `small-int8-ov`。

注意：首次转写一次性静态编译约 47s（默认 120s 超时兜得住）；编译缓存
`~/.cache/voice-input/npu-compile-cache`（约 850MB，可删，代价是一次 47s 重编译）
让后续加载降到约 0.8s。热词（terms.json）在 NPU 静态管线上不支持：引擎告警后
忽略热词继续转写，绝不阻断；Wayland 按住说话链路现状本就不接热词（不变）。
每次听写仍有约 0.8s 加载（#18 的子进程架构），常驻转写 worker 见 #25。
```

- [ ] **Step 5: Commit**

```bash
git -C $WT add download-model.py contrib/voice-hold.service README.md README.zh-CN.md
git -C $WT commit -m "docs: NPU engine section (EN+ZH), npu example service, download-model npu message (#19)"
```

---

### Task 9: 全量回归 + 验收对账（A5/A7/A8 汇总）

**验收归属：A5、A7、A8 汇总执行**

- [ ] **Step 1: 全量测试**

Run: `cd $WT && venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench test_hold test_paste test_media_pause -v 2>&1 | tail -5`
Expected: `OK`，且 TestNpuConstruction / TestNpuAdapter / TestLibPaths / TestOvResolve / TestTranscribeOnceEnv / TestWrapperExport 六个新测试类在 -v 输出中全 PASS

- [ ] **Step 2: A7 静态三查**（重跑 Task 6 Step 2 的三条命令，确认仍全 OK）

- [ ] **Step 3: 验收矩阵对账**

逐行核对 spec 验收矩阵：A1–A8 各条的验证命令在本 plan 中已实际执行且通过，记录实际命令与结果（合并前报告用）；U1–U3 标记 pending 转 Task 10。

- [ ] **Step 4: Commit（如有对账记录文件变更则提交，否则跳过）**

---

### Task 10: 用户实测 U1/U2/U3（合并前在 OmniBook 执行）

**验收归属：U1、U2、U3**

> 顺序说明：U3（冷编译）先跑——它建立的缓存是 U1（热缓存计时）的前提。

- [ ] **U3 冷编译首次行为**

```bash
rm -rf ~/.cache/voice-input/npu-compile-cache
# 部署新 service(参考 contrib/voice-hold.service 的 npu 配置)
systemctl --user daemon-reload && systemctl --user restart voice-hold
# 按住右 Alt 说一段话,松开——第一次约 47s 内完成(默认超时 120s 兜住),不报错
journalctl --user -u voice-hold -n 20
```
通过标准：第一次听写 ~47s 内完成不超时；第二次恢复 ~2s 量级。

- [ ] **U1 e2e npu 听写 + 延迟数据**

```bash
# service 已为 ENGINE=npu(U3 已配);确认:
systemctl --user show voice-hold -p Environment | grep -o "VOICE_INPUT_ENGINE=npu"
journalctl --user -u voice-hold -f -o short-precise &
# 连续 5 次:按住右 Alt 说 8s 中文 -> 松开,记录松手到 delivered 行的耗时
```
通过标准：文本正确上屏（Wayland paste）；5 次中位 ≤5s（目标 ~2s）；数据回贴 #19。

- [ ] **U2 真实 NPU 热词降级**

```bash
arecord -q -f S16_LE -r 16000 -c 1 -D default -d 5 /tmp/u2.wav
cd /home/elling/work/git-repo/voice-input  # 主 checkout 或 worktree 均可(代码已合并后)
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
VOICE_INPUT_ENGINE=npu venv/bin/python3 -c "
import engine
m = engine.build_model()
segs, info = m.transcribe('/tmp/u2.wav', language='zh', initial_prompt='术语测试:重构。')
print(''.join(s.text for s in segs))
"
```
通过标准：stderr 出现 `NPU engine: initial_prompt/hotwords not supported` 告警行，stdout 正常输出转写文本（不断流不崩）。

---

## Self-Review 记录

- **Spec 覆盖**：In-scope 1→T2/T1，2→T1/T3/T4/T5/T6，3→T2，4→T6，5→T3，6→T7，7→T5/T8，8→T8，9→T10；A1→T2，A2→T1，A3→T2，A4→T1，A5→T1/T2/T7/T9，A6→T3，A7→T6/T9，A8→T5/T9，U1–U3→T10。无孤儿 task、无无归属验收项。
- **占位符扫描**：无 TBD/TODO；所有代码块为完整可执行内容。
- **类型一致性**：`required_lib_paths(engine, site_packages=None)` / `child_env(env)` / `_NpuWhisperAdapter(model_dir, pipeline_factory, warn)` / `build_model(..., pipeline_factory=None, model_base=None)` 在 Tasks 1–7 间签名一致。
- **Review Focus 五条**：各由 Task 1/2 测试或已声明惰性差异覆盖。
