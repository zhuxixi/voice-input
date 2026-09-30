# Spec: NPU hotwords — stateful 管线切换 + terms 接入 hold 链路 (#27)

> Issue: #27 · 状态: draft（待用户确认）· 日期: 2026-09-30
> 调研依据: issue #27 调研 R1–R4（本 issue 评论区 + `~/.claude/github-issue-driven/zhuxixi/voice-input/issue-27/research/`）

## Goal

NPU 引擎默认启用热词：`engine=npu` 时词表 `~/.config/voice-input/terms.json` 真实影响转写结果（专有名词正确拼写率提升），hold 链路（voice_hold → transcribe_once）零改动即受益。不新增任何用户配置项。

## Scope / Non-goals

**Scope**（改动面 6 个文件）：

- `engine.py` — NPU 构造参数单一定义点 + adapter 透传提示词
- `transcribe_once.py` — 接入 terms 组装（所有引擎统一受益）
- `test_engine.py` — 改写 3 个既有 NPU 测试 + 新增 2 组测试
- `contrib/voice-hold.service` — 超时 120→240 + 注释更新
- `README.md` / `README.zh-CN.md` — NPU 段与热词段改写 + 冷编译说明

**Non-goals**（全部有既有归属或属运维）：

- 常驻 worker（#25）；后处理纠错 corrections（#4 Phase 2）；large-v3 模型切换（#27 后续）；hold 链路归档接线（独立需求）；旧静态编译缓存条目自动清理（运维备注：`rm -rf ~/.cache/voice-input/npu-compile-cache` 后重建）；`bench/npu-bench.py` 改造（基准脚本维持静态管线语义，数字与本引擎路径的差异在 README 注明）

**零改动保护清单**（实现后 `git diff main --stat` 逐文件核对为空）：

`voice_hold.py` / `paste.py` / `voice-ptt.py` / `terms.py` / `archive.py` / `media_pause.py` / `voice-ptt.sh` / `voice-toggle.sh` / `test-mic.sh` / `bench/` / `download-model.py` / `download-model.sh` / 既有 test 文件除 `test_engine.py` 外全部

## Research-backed facts（设计依据，勿推翻需新证据）

1. NPU 静态管线（`STATIC_PIPELINE=True`）在 C++ 层断言硬拒 `initial_prompt`/`hotwords`（pipeline_static.cpp:1151-1152，本机 2026-09-30 复验）
2. NPU 默认管线是 stateful（pipeline.cpp：`!use_static_pipeline → WhisperPipelineStatefulImpl`），无禁用断言
3. `word_timestamps` 必须在**构造期**传入：构造函数读 `m_generation_config.word_timestamps` 决定 decoder SDPA 分解与输入形状（pipeline.cpp:98-114）；构造后在 generate() 传无效，任何已设 prompt 值（含空字符串）在 roi 检查崩溃
4. 正确组合实测（OmniBook · small-int8-ov · 8s 中文）：构造期 `word_timestamps=True` + `initial_prompt=243字符` → 0.99~1.2s 正常返回；30 词表使 `HoloWord`→`Hello World` 确定性生效（3+ 次复现）；副作用 `语音`→`语言` 同步复现（软引导双向）
5. 成本实测：冷编译 155s（CACHE_DIR 可缓存）；热构建 1.42s；generate 1.0~1.2s（静态管线对照 0.54s）；缓存新增条目后 CACHE_DIR 总量 ~2.4GB
6. 上游依据：openvino.genai#3533（含 Lunar Lake 用户同配方验证）+ PR #3518
7. cpu/cuda 的 faster-whisper 路径本就支持 `initial_prompt`（voice-ptt.py:208-221 在用）

## Design decisions（已裁决，2026-09-30 用户确认）

| # | 决策 | 备选与否决理由 |
|---|---|---|
| D1 | NPU 管线切 stateful + 构造期 `word_timestamps=True`，删 `STATIC_PIPELINE=True` | 静态管线被 R4 排除 |
| D2 | 热词 NPU 默认启用，**无逃生开关** | 用户拍板「NPU 必须加载热词」；YAGNI；#25 后速度差距收敛 ~0.5s |
| D3 | 冷编译 155s 由**部署侧**吸收：`VOICE_INPUT_TRANSCRIBE_TIMEOUT=240` + README 说明，代码零兜底 | 用户拍板；一次性事件 |
| D4 | adapter 的 warn-and-drop 降级分支**删除**（存在理由已消失），warn 注入缝一并移除 | 留着是死代码 |
| D5 | `transcribe_once.py` 对**所有引擎**统一接 terms（不按引擎门控） | cpu/cuda 本就支持；门控增加分支复杂度零收益 |
| D6 | 空串提示词**不透传**（truthy 判断） | R3 实测：任何已设值（含空串）使 stateful 崩溃 |

## Component contracts

### engine.py（modified）

**新增纯函数**（放在 `NPU_COMPILE_CACHE` 常量之后）：

```python
def npu_pipeline_kwargs() -> dict:
    """npu 构造参数单一定义点(#27):stateful 管线 + word_timestamps=True。

    不含 STATIC_PIPELINE:NPU 默认即 stateful;显式 True 会走带断言的静态
    管线,硬拒 initial_prompt/hotwords。word_timestamps 必须构造期传入:
    它决定 decoder 的 SDPA 分解与输入形状(pipeline.cpp:98-114),构造后
    在 generate() 传无效。
    """
    return {
        "NPU_PLATFORM": "NPU4000",
        "word_timestamps": True,
        "CACHE_DIR": NPU_COMPILE_CACHE,
    }
```

**`_NpuWhisperAdapter`**：

- `__init__(self, model_dir, pipeline_factory)` — 删除 `warn` 参数与 `self._warn`；构造改为 `pipeline_factory(model_dir, device="NPU", **npu_pipeline_kwargs())`；类 docstring 改写（去掉「静态管线 C++ 硬拒…降级告警」表述，改为「stateful + word_timestamps 构造期启用，提示词直通」）
- `transcribe(self, wav, language="zh", initial_prompt=None, hotwords=None, **_ignored)`：

```python
gen_kwargs = {}
if initial_prompt:            # truthy:None/"" 不透传(D6,空串会崩 stateful)
    gen_kwargs["initial_prompt"] = initial_prompt
if hotwords:
    gen_kwargs["hotwords"] = hotwords
samples = _load_wav_samples(wav)
result = self._pipe.generate(samples, language=language, **gen_kwargs)
# _extract_text 及返回值形态不变
```

- `build_model` 的 npu 分支：`return _NpuWhisperAdapter(path, pipeline_factory)`（去掉 `warn=warn`）；`build_model` 签名不变（warn 仍供 auto 分支用）；`build_model` docstring 的 npu 行与模块 docstring 同步更新
- `NPU_COMPILE_CACHE` 注释更新：冷 155s（stateful+wt 条目）/ 热 1.4s；总缓存 ~2.4GB，含历史静态条目 848MB（可整目录 rm 重建，代价一次冷编译）

### transcribe_once.py（modified）

- import 区新增：`from terms import load_terms, build_prompt, build_transcribe_kwargs, DEFAULT_TERMS_PATH`（模块 docstring 补一句「terms 组装与 voice-ptt.py 同构，所有引擎统一生效」）
- `main` 签名改为可注入：

```python
def main(argv=None, terms_path=DEFAULT_TERMS_PATH) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("usage: transcribe_once.py <wav>", file=sys.stderr)
        return 2
    wav = argv[0]
    # terms 组装独立 try:热词问题降级到无 prompt,绝不阻断转写(与
    # voice-ptt.py:208-221 同构;load_terms/build_* 内部已降级,双保险)
    try:
        cfg = load_terms(terms_path)
        prompt = build_prompt(cfg.get("terms", []))
        extra_kw = build_transcribe_kwargs(cfg)
    except Exception as te:
        prompt, extra_kw = None, {}
        print(f"[voice-input] terms assemble failed, degrade to no-prompt: {te}",
              file=sys.stderr)
    model = engine.build_model()
    segments, info = model.transcribe(wav, language="zh", initial_prompt=prompt, **extra_kw)
    text = "".join(s.text for s in segments).strip()
    print(text)
    return 0
```

- CLI 契约不变：用法文本、退出码（2 参数错 / 0 成功 / 1 顶层异常）与 stdout 形态逐字保留

### test_engine.py（modified）

- `TestNpuConstruction.test_npu_build_constructs_adapter_with_static_kwargs` → 重写为 `test_npu_build_constructs_stateful_pipeline_kwargs`：断言 fake factory 收到 `kwargs == {"NPU_PLATFORM": "NPU4000", "word_timestamps": True, "CACHE_DIR": NPU_COMPILE_CACHE}` 且无 `STATIC_PIPELINE` 键，位置参数 `device="NPU"`
- 新增 `TestNpuPipelineKwargs.test_kwargs_contract`：纯函数直测（精确 dict、无 STATIC_PIPELINE、`word_timestamps is True`）
- `TestNpuAdapter`：`test_adapter_hotword_degradation_warns` → 重写为 `test_adapter_forwards_prompt_and_hotwords`（fake pipe 记录，断言 `initial_prompt`/`hotwords` 透传进 generate）；`test_adapter_no_warn_without_prompt` → 重写为 `test_adapter_omits_absent_prompt_kwargs`（None 时 generate kwargs 不含这两个键）；新增 `test_adapter_empty_string_prompt_not_forwarded`（`""` 不透传，D6 守卫）；`test_adapter_transcribe_contract` / `test_adapter_empty_result_empty_segments` / `test_adapter_ignores_extra_kwargs` 原样保留
- 新增 `TestTranscribeOnceTerms`（沿用 `TestTranscribeOnceEnv` 的干净子进程 harness 内联模式）：子进程内 `VOICE_INPUT_ENGINE=cpu` import 后 monkeypatch `engine.build_model` 返回记录型假模型，再调 `transcribe_once.main(["<wav>"], terms_path=<临时词表>)`，经 stdout 标记断言：
  1. 正常词表 → 假模型收到含词表词的 `initial_prompt`，RC=0，文本正常打印
  2. `terms_path` 指向不存在文件 → 假模型收到 `initial_prompt=None`，RC=0，文本仍打印，stderr 出现 `[voice-input] terms.json load failed` 告警行（load_terms 内部降级路径）
- 假模型形态：`transcribe(self, wav, language="zh", initial_prompt=None, **kw)` 返回 `([SimpleNamespace(text=" 你好 ")], None)`（join/strip 语义走真实代码）

### contrib/voice-hold.service（modified）

`Environment=VOICE_INPUT_TRANSCRIBE_TIMEOUT=120` → `240`；注释块改写：首跑冷编译 ~155s（240s 兜得住）、之后热构建 ~1.4s/次、缓存 ~2.4GB 可整目录 rm 重建、#25 常驻后免每次构建

### README.md / README.zh-CN.md（modified，双语同步）

1. NPU 引擎段：删「static pipeline」表述；性能数字更新（转写 ~1.0s/次（原 0.3s 为静态管线极速档，现默认热词开启）；构建 ~1.4s（#25 跟踪））；新增首跑冷编译 ~155s 警示 + `VOICE_INPUT_TRANSCRIBE_TIMEOUT≥240` 建议 + 预热路径（跑一次 `test-mic.sh` 或任意一次听写）
2. Known limitations 热词条目：从「NPU 不支持（warn 降级）」改为「NPU 支持（stateful 管线 + 构造期 word_timestamps，#27）」；提示词为概率性软引导、可能双向影响（附 `语音→语言` 实测例）；0.8s 每次加载条目更新为 ~1.4s 并保留 #25 指向
3. Custom vocabulary 段：补一句「对所有引擎生效（含 NPU）」
4. hold 超时说明行：默认 120s 不变，补「NPU 首跑建议 240s」

### 部署项（post-merge 人工，不进本 PR）

- 本机 `~/.config/systemd/user/voice-hold.service`（非仓库文件）手动同步 TIMEOUT=240：`systemctl --user edit voice-hold` drop-in

## Degradation / failure paths

| 场景 | 行为 |
|---|---|
| terms.json 缺失/损坏/非 dict/非 UTF-8 | `load_terms` 内部降级空表 → `prompt=None` → 无提示转写 + stderr 一行告警（既有语义，不阻断） |
| terms 组装意外异常 | `main` 内 try 兜底 → 同上（双保险） |
| `hotwords: null` | `extra_kw` 空 → 不传（现状） |
| 词表超 30 词 | `build_prompt` 只取前 30（现状，README 已有说明） |
| NPU 库路径缺失 / 模型缺失 | #19 既有可行动报错路径，不变 |
| stateful 冷编译 >120s | 部署侧吸收（D3）；代码不兜底 |

## Safety contract（既有行为不变，契约测试钉死）

- cuda/cpu/auto 分支：`construction_kwargs`、preamble 计算、`build_model` 参数逐字节不变；既有契约测试（含 cuda preamble 逐字节比对）**零改动**
- `transcribe_once` 无词表时的行为与现状等价（`prompt=None` 传参与不传在 faster-whisper / adapter 两侧语义相同）
- 零改动保护清单见 Scope（`git diff main --stat` 核对）

## Acceptance matrix

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | `npu_pipeline_kwargs()` 内容契约 | 自动化验证（unit） | `./venv/bin/python3 -m unittest test_engine -v`（TestNpuPipelineKwargs） | 精确返回三元 dict；无 `STATIC_PIPELINE` 键；测试通过 |
| A2 | adapter 提示词透传/省略 | 自动化验证（unit） | 同上（TestNpuAdapter 全组） | prompt/hotwords 有值时透传进 `generate`；None 与 `""` 时不出现这两个键；extra kwargs 静默忽略；测试通过 |
| A3 | transcribe_once 词表接线 + 降级 | 自动化验证（unit） | 同上（TestTranscribeOnceTerms，干净子进程） | 正常词表 → 假模型收到含词表词的 initial_prompt 且 RC=0；词表缺失 → initial_prompt=None、RC=0、stderr 有降级告警 |
| A4 | 既有契约与全套件回归 | 自动化验证（unit） | `./venv/bin/python3 -m unittest test_engine test_terms test_archive test_media_pause test_hold test_paste test_bench -v` | 全绿；cuda preamble 逐字节测试未改动且通过；`git diff main --stat` 仅含 Scope 列出的 6 个文件 |
| U1 | 真机 NPU 热词生效 | 用户实测 | 部署后 hold 听写 3 句话，每句含 1–2 个词表词（如 zima / jfox / Kimi） | ≥2 句以正确拼写上屏；记录端到端延迟与 3 句原始转写回贴 issue |
| U2 | 冷编译与超时兜底 | 用户实测 | `rm -rf ~/.cache/voice-input/npu-compile-cache` 后：第 1 次听写计时（预期 ~155s，须 <240s 不被杀），第 2 次听写计时（预期 <5s） | 两轮耗时如实回贴 issue；第 1 次在 240s 内完成且文本正常上屏 |

## Testability split design（A1–A4 的实现约束）

- **`npu_pipeline_kwargs()`**：纯函数、无 IO、无状态 — 直接断言返回值；不 import openvino_genai
- **`_NpuWhisperAdapter`**：构造经既有 `pipeline_factory` 注入缝（`_FakePipeFactory` 记录 kwargs）；`transcribe` 经 `_FakeGenaiPipe` 记录 `generate` 调用；wav 用既有 `_write_wav` helper 造真实 16k/16bit 文件走真实 `_load_wav_samples`。测试边界：engine 的 npu 分支全程不触碰 openvino_genai 与 NPU 硬件
- **`transcribe_once.main(argv, terms_path)`**：新增两个注入参数（argv 与 terms_path）即全部所需缝；测试在干净子进程中 monkeypatch `engine.build_model` 为记录型假模型，三重隔离（词表文件 / 模型 / preamble 环境）互不污染主测试进程
- **terms.py 零改动** — 复用其既有纯函数与 `test_terms.py` 契约；不新增 terms 侧接口
- 实现不得把上述已拆分的纯函数/注入缝重新耦合回 adapter 内联（如把 kwargs dict 内联进构造调用）

## Risks（已接受，README/注释留痕）

1. 单次听写延迟回归：~1.5s → ~2.5s（#25 常驻后收敛 ~1.0s）
2. CACHE_DIR 总量 ~2.4GB（含遗留静态条目，可手动清理）
3. 提示词双向副作用（`语音`→`语言` 实测）：软引导本质，README 明示「概率性收益」
4. `word_timestamps=True` 的额外解码开销已含在实测 1.0~1.2s 内，无隐藏成本
