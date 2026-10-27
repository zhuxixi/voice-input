# VAD 轻声/气声失效修复 + 词表回声熔断 — 设计（issue #43）

Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-27T23:14:00+08:00

关联：#43（本 issue）、#32（引入裁剪，回归面）、#33（热词 prompt 变长后回声更明显）、#44（提供本轮可回放样本）
调研：`research/topic-1-repro-typing.md`

## 1. 问题

Silero VAD 的默认 `threshold=0.5` 对两类真实语音失效，另外热词 prompt 在被裁残片上会被解码器回读：

| 分型 | 现象 | 触发条件 | 阈值是否可解 |
|---|---|---|---|
| A 轻声带声 | 6.91s 只保留 63%，文本只剩尾巴 | VAD 概率落在 0.3–0.5 带 | **是**，0.3 → 96% |
| B 气声耳语 | 整条 `no_speech`，0% 保留 | 只有气流、声带不振动（逐帧概率峰值 0.08） | **否**，0.15 仍 0% |
| C 词表回声 | `Zima、DeepSeek、GitHub、Kimi` ×19 | A/B 裁剪后残片 + 非空 `initial_prompt` | 需文本层熔断 |

误报代价核对：62s 复现带静音段在 `t=0.25` 仍 0%；10-24 的 7 条正常音量抓包在 0.3 下覆盖率 0~+3 个百分点。**现值 0.5 过于激进，0.3 在现有样本上零误报代价。**

## 2. 决策表

| ID | 决策 | 理由 | 被否方案 |
|---|---|---|---|
| D1 | 新增 `VOICE_INPUT_VAD_THRESHOLD`，**默认 0.5 → 0.3** | 分型 A 唯一有效旋钮；0.35 仍 63%，0.25 无额外收益 | 保持 0.5 + 只做重试：分型 A 靠重试要求「整条无 span」，而 A 型是有 span 但被切短，重试不会触发 |
| D2 | 新增 `VOICE_INPUT_VAD_PAD_MS` / `VOICE_INPUT_VAD_MIN_SILENCE_MS`，**默认值不动**（400 / 2000） | 只把现值显式化，为后续调参留口；本轮无证据支持改默认 | 借机调 pad 到 600：`kept_ratio` 已 96%，无收益 |
| D3 | 分型 B 用**能量门 + 低阈值重跑**：`no_speech` 且**响亮帧数** ≥ `MIN_LOUD_FRAMES=5`（20 ms/帧，均高于 `ENERGY_FLOOR_DBFS = −58`）时，用 `threshold=0.1` 再跑一次；且仅当 `0.1 < 当前阈值` 才重跑 | 气声与底噪之间有 7 dB 可判区间；Silero 对气声概率 ≤0.08，只能靠「降得足够低」+「确认不是静音」 | 直接对 `no_speech` 无条件重跑：纯静音会重跑，浪费且增加误判面；切成两个模型：引入新权重，超出范围 |
| D4 | 重跑**只在默认 detector 路径**生效（`detector` 注入时跳过） | `detector` 是固定参数的测试注入点，语义是「已算好的 span」；重跑需要能换参数的检测器，混用会让测试失真 | 给 detector 加 `vad_options` 参数：破坏既有注入契约（`test_vad_trim.py` 两个测试类共 18 处注入（TestTrimmedWav 12 + TestStats 6）） |
| D5 | 语音检测与能量计算都留在 `vad_trim`，**RMS 用 stdlib 实现**（`array('h')`），不引入 numpy 到该模块顶层 | 维持 `vad_trim` 顶层 stdlib-only 的既有契约（`voice_hold` 侧依赖此性质） | 用 numpy 算 RMS：`vad_trim` 顶层会被迫 import 三方库 |
| D6 | 词表回声判定做成 `terms.looks_like_prompt_echo(text, terms)` **纯函数** | 可单测、无 IO；判定与 prompt 构造同属 `terms` 的关注点 | 放 worker 内联：两处调用点重复 |
| D7 | 回声处置 = **丢 `initial_prompt`（及 `hotwords`）重转一次**；重转仍命中且文本**由热词主导**才返回空串，否则保留重转结果 | 回声根因是 prompt 被回读，去掉 prompt 是对症下药；仍命中说明残片太短，宁可不出字也不能灌词表 | 直接返回空串：丢掉可能本来可用的内容；调 `repetition_penalty`：#32 已证会压坏正常重复 |
| D8 | 重转逻辑抽成新模块 `transcribe_guard.py::transcribe_guarded(...)`，worker 与 `transcribe_once` 共用 | 两处调用点同构，避免复制；可注入 fake model 单测 | 各自内联：行为容易漂 |
| D9 | stats 增 `retried`（bool）与 `echo_guard`（`none`/`retried`/`dropped`） | 排障时需要知道「这条是被重跑/熔断过的」，否则事后无法解释静默空串 | 只打日志：归档里查不到 |

## 3. 数据流（改动点加粗）

```
worker / transcribe_once
  ├─ **trimmed_wav(wav, stats=stats)**
  │     ├─ **options 在 trimmed_wav 内部解析**：`vad_options or vad_options_from_env()`
  │     │   （解析/import 失败落在既有 degraded 分支，调用点不需要单独 try）
  │     ├─ spans = spans_from_wav(wav, vad_options=options)
  │     ├─ bounds 非空 → 走今天的行为
  │     └─ **bounds 为空 → rms_dbfs(frames) > -58 ? 用 threshold=0.1 重跑 → 仍空则 no_speech**
  ├─ raw = text
  ├─ **text = transcribe_guarded(model, src, prompt=prompt, extra_kw=..., terms=..., log=...)**
  │     ├─ 首转（带 prompt）
  │     ├─ **looks_like_prompt_echo(text, terms) → 丢 prompt 重转**
  │     └─ **重转仍命中 → ""**
  ├─ text = apply_corrections_counted(...)
  ├─ archive.archive_result(..., vad=stats)   # stats 里带 retried / echo_guard
  └─ 回包 / 打印
```

`no_speech` 分支（`src is None`）不进入 `transcribe_guarded`（没有音频可转），但仍归档 `status=no_speech`（#44 既有行为）。

## 4. 组件契约

### 4.1 `vad_trim.py`

```python
DEFAULT_THRESHOLD = 0.3          # was 0.5（D1）
DEFAULT_PAD_MS = 400
DEFAULT_MIN_SILENCE_MS = 2000
RETRY_THRESHOLD = 0.1            # D3
ENERGY_FLOOR_DBFS = -58.0        # D3

ENV_THRESHOLD = "VOICE_INPUT_VAD_THRESHOLD"
ENV_PAD_MS = "VOICE_INPUT_VAD_PAD_MS"
ENV_MIN_SILENCE_MS = "VOICE_INPUT_VAD_MIN_SILENCE_MS"

def parse_number(raw, default, cast=float) -> float | int
    """坏值/空值 → default（纯函数，供单测直接断言）。"""

def vad_options_from_env(env=None):
    """按 env 构造 VadOptions；三个键任一坏值回退默认值，不抛异常。
    构造本身可能抛 ImportError（faster-whisper 缺失）——**调用点在 trimmed_wav 内部**，
    由既有 try 兜成 degraded，不在调用点裸露。"""

def default_vad_options():        # 保留（等价 vad_options_from_env({})）

def rms_dbfs(frames: bytes) -> float
    """16-bit mono PCM 的 RMS 电平（dBFS）。空输入 → -inf；纯 stdlib。"""

def spans_from_wav(path, *, vad_options=None)     # 默认 vad_options_from_env()

@contextmanager
def loud_frame_count(frames, floor_dbfs=ENERGY_FLOOR_DBFS, frame_ms=LOUD_FRAME_MS) -> int

def trimmed_wav(path, *, detector=None, enabled=None, log=None, stats=None,
                vad_options=None)
    """`vad_options=None` 时**在 try 内部**调 vad_options_from_env()（#43 D3 的接线点）。
    新增：no_speech 时按 D3 重跑一次；stats 增 retried 字段。"""
```

`stats` 契约（在 #44 基础上追加）：
- `retried`: bool，**只在进入检测路径（`kept` / `no_speech`）时出现**；`disabled` / `degraded` 不写该键。
  语义是「尝试过重跑」（重跑过但仍是空也记 True），这样归档能区分「没重跑」与「重跑无效」
  （`test_vad_trim.TestStats.test_disabled` 对该 dict 做精确相等断言，写键会破它）
- `echo_guard`: `"none"` / `"retried"` / `"dropped"`，**只在调用过 `transcribe_guarded` 时写**
  （`kept`、`degraded`、`disabled` 三条路径都会转写 → 都会写；只有 `no_speech` 没有音频可转、不写）
- 重跑发生时 `retried=True`，且 `threshold` 记录**实际生效**的重跑阈值（0.1）
- 其余字段语义不变

日志（沿用 `[voice-input] vad:` 前缀）：
- `vad: no speech at 0.30, retry at 0.10 (rms -54.3 dBFS) -> 3.21s kept`
- `vad: no speech (rms -61.0 dBFS below -58.0 floor), empty transcript`

### 4.2 `terms.py`

```python
ECHO_SINGLE_MIN = 4        # 单词表词出现次数阈值
ECHO_MULTI_MIN = 2         # 多词各出现次数阈值
ECHO_SHORT_TEXT = 60       # 多词情形的文本长度上限

def hotword_dominated(text, terms) -> bool
    """去掉热词命中后剩下的实义字符 <= max(3, 20% 原长) → 文本由热词主导。"""

def looks_like_prompt_echo(text, terms) -> bool
    """命中任一条即判回声：
       a) 某个词表词出现 >= 4 次；
       b) >= 2 个词表词各出现 >= 2 次，且 len(text) < 60；
       c) 词表词的交替序列连续重复 >= 2 轮。
       空 text / 空 terms → False。大小写不敏感（词表含英文）。"""
```

判定用**子串计数**而非分词（中文无空格）；词表词按长度降序匹配以避免短词吃掉长词。
纯 ASCII 词加词边界（与 `apply_corrections_counted` 同口径），否则 `AI` 会命中 `email`/`said`。
真实列举（每个词一次）不触发。

### 4.3 `transcribe_guard.py`（新增，stdlib-only）

```python
def transcribe_guarded(model, src, *, prompt, extra_kw, terms, log) -> tuple[str, str]
    """首转；若 prompt 非空且 looks_like_prompt_echo：
       1. log 一行；丢 prompt 与 hotwords 重转一次（这两个都是 prompt 式条件）
       2. 重转仍命中且 hotword_dominated(重转结果) → 返回 ""
       3. 重转仍命中但文本由真实内容主导 → 保留重转结果（`mode="retried"`）
       4. 重转不命中 → 返回重转结果
       无回声 → 返回首转结果，不额外调用模型。"""
```

`language` **硬编码 `"zh"`**（与两个调用点现值一致；多语言是 §6 非目标）。
返回 `(text, mode)`，`mode ∈ {"none","retried","dropped"}`，调用方写入 `stats["echo_guard"]`。

### 4.4 调用点

`trimmed_wav` 的调用点保持原样（不额外构造 options，D3/G3）：

- `transcribe_worker.py`：`trimmed_wav(wav, stats=stats)`；`text, mode = transcribe_guarded(...)`；`stats["echo_guard"] = mode`。
- `transcribe_once.py`：同上。

**echo guard 的调用点只列上面两处**。第三个 `trimmed_wav` 调用点 `voice-ptt.py:253`（X11 路径）
会让阈值与能量重跑生效（因为它走同一个 `vad_trim`），但**不接 echo guard**：该文件依赖 GTK/gi，
本机无法 import 测试，接未经测试的熔断反而更危险。此边界写入 §6 非目标并登记风险。

## 5. 降级与失败处理

| 情形 | 行为 |
|---|---|
| `VOICE_INPUT_VAD_*` 值非法 | 回退默认值，转写照常（不抛） |
| `vad_options_from_env()` 构造 VadOptions 失败（如 faster-whisper 缺失） | `trimmed_wav` 走既有 degraded 路径 |
| 重跑本身抛异常 | 按 `degraded` 处理，用原音频转写（既有语义） |
| 重跑后仍无 span | `status=no_speech`，不调用模型（与今天一致） |
| 回声重转抛异常 | 沿用调用点既有异常路径（worker 回 `encode_error`、CLI 非零退出）；**不吞异常**——这是真正的转写失败 |
| 重转仍命中回声 | 返回空串 + 日志 + `echo_guard="dropped"`；不归档空串覆盖已有内容（归档发生在回包前，照旧记录 raw=""） |

## 6. 非目标

- 不训练/替换 VAD 模型
- 不改 `speech_pad_ms` / `min_silence_duration_ms` 的默认值
- 不做自适应阈值（按概率分布归一）；本轮数据不支持其必要性
- 不改 worker 协议、不改归档格式（只往 `vad` 对象里加 `retried` 与 `echo_guard` 两个字段，无结构变更）
- 不做多语言/多模型差异处理
- **`voice-ptt.py`（X11 路径）不接 echo guard**：它依赖 GTK/gi，本机无法 import 测试；该文件仍会因共用 `vad_trim` 而享有阈值 0.3 与能量重跑，但分型 C 在其链路上不防护（见 §4.4 边界与 §9 R7）

## 7. 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|---|---|---|---|---|
| A1 | 环境变量解析 | 自动化（unit） | `venv/bin/python3 -m unittest test_vad_trim.TestOptionsFromEnv -v` | 三个键分别生效；空值/非数字/负数 → 回退默认；`parse_number` 的边界用例通过 |
| A2 | 默认阈值 0.3 | 自动化（unit） | 同上 | `default_vad_options().threshold == 0.3`，pad 400，min_silence 2000 |
| A3 | 气声重跑 | 自动化（unit） | `venv/bin/python3 -m unittest test_vad_trim.TestEnergyRetry -v` | RMS > −58 且首轮无 span → 发生第二次检测，stats `retried=True`、`threshold=0.1`；结果采用第二次 span |
| A4 | 静音不重跑 | 自动化（unit） | 同上 | 合成 −61 dBFS 静音 → 只调一次检测，`status=no_speech`、`retried=False` |
| A3b | 重跑失败降级 | 自动化（unit） | 同上 | 第二次检测抛异常 → `status=degraded`、`reason="error"`、yield 原音频、不抛 |
| A5 | RMS 计算 | 自动化（unit） | 同上 | 已知幅度方波/正弦的 dBFS 与手算一致（±0.5 dB）；空 frames → `-inf` |
| A6 | 回声判定 | 自动化（unit） | `python3 -m unittest test_terms.TestPromptEcho -v` | 19 遍循环 True；列举一次 False；长文本含单词表词 2 次 False；空文本/空词表 False；交替重复 2 轮 True |
| A7 | 回声重转 | 自动化（unit） | `python3 -m unittest test_transcribe_guard -v` | 首转命中 → 第二次调用 `initial_prompt is None`；返回第二次文本；`mode="retried"` |
| A8 | 二次命中丢弃 | 自动化（unit） | 同上 | 两次都命中 → 返回 `""`，`mode="dropped"`，模型被调 2 次 |
| A9 | 无回声零开销 | 自动化（unit） | 同上 | 正常文本只调 1 次模型，`mode="none"` |
| A8b | 重转异常外泄 | 自动化（unit） | 同上 | 重转时 fake model 抛 RuntimeError → 异常从 guard 原样抛出（不被吞） |
| A10 | worker 接线 | 自动化（integration） | `python3 -m unittest test_transcribe_worker.TestEchoGuard -v` | fake model 返回回声文本时，响应为重转结果；`archive` 行 `vad.echo_guard="retried"` |
| A11 | `transcribe_once` 接线 | 自动化（integration） | `python3 -m unittest test_archive.TestTranscribeOnceEcho -v` | 同上（stdout 为重转文本） |
| A12 | 默认阈值回归 | 自动化（integration） | `python3 -m unittest test_transcribe_worker -v` | 既有用例全绿；`trimmed_wav` 在不传 `vad_options` 时行为与 0.3 一致 |
| A13 | 全量回归 | 自动化（integration） | ① `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim test_transcribe_guard -v` ② `venv/bin/python3 -m unittest test_engine -v` ③ `VOICE_INPUT_ENGINE=npu LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu venv/bin/python3 -m unittest test_vad_trim -v` | 三条全绿；③ 中 `TestEndToEnd` 不 skip |
| A14 | 真实样本回归 | 用户实测（脚本化） | 在仓库根执行，沿用生成样本的引擎：<br>`VOICE_INPUT_ENGINE=npu LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu venv/bin/python3 transcribe_once.py ~/.local/share/voice-input/recordings/2026-10-27_230451/audio.wav`（分型 A）<br>同上换成 `2026-10-27_230459/audio.wav`（分型 B） | A 型：`kept_ratio ≥ 0.9` 且文本完整（硬指标是 kept_ratio，与引擎无关；文本只在 npu/small-int8-ov 下比对）；B 型：不再 `no_speech` 或至少产生非空文本（若不达，记录实测值并说明） |
| U1 | 真机轻声听写 | 用户实测 | **部署前置**：`cd ~/work/git-repo/voice-input && git pull`（确认含本 PR 提交）→ `systemctl --user restart voice-hold`（常驻 worker 只在 daemon 启动时加载代码，不重启跑的是旧代码）→ `journalctl --user -u voice-hold -n 20` 确认 `[worker] model ready`。然后小声（带声）说 5 句 | 每句完整，无半句截断 |
| U2 | 真机气声听写 | 用户实测 | 以气声说 3 句 | 至少 2 句出字；否则记录为已知边界 |
| U3 | 正常场景无回归 | 用户实测 | 正常音量说 3 句 + 静默按一次热键 | 转写正常；静默仍为 `no_speech`，无幻觉 |
| U4 | 文档 | 用户实测（人工核对） | 通读 README 双语配置表 | 三个新环境变量 + 回声熔断行为说明齐全且两语言一致 |

## 8. 可测性拆分设计

| 层 | 单元 | 依赖 | 测法 |
|---|---|---|---|
| 解析 | `parse_number` / `vad_options_from_env` | 无（VadOptions 惰性 import） | 纯函数断言；坏值用例覆盖 None/""/"abc"/负数 |
| 能量 | `rms_dbfs(frames)` | stdlib only | 构造已知振幅的 int16 帧，断言 dBFS |
| 检测 | `trimmed_wav(...)` | `spans_from_wav` 可 patch（模块属性） | patch 成「按 threshold 返回不同 spans」的假检测器，断言重跑与 stats |
| 判定 | `looks_like_prompt_echo` | 无 | 纯函数，边界用例 |
| 编排 | `transcribe_guarded` | fake model（记录 `initial_prompt`） | 断言调用次数、kwargs、返回值与 mode |
| 接线 | worker / transcribe_once | 既有 harness（`FakeModel`、socketpair、临时归档目录） | 端到端断言响应文本与归档 `vad.echo_guard` |

关键边界：`vad_trim` 顶层保持 stdlib-only；重跑只发生在默认 detector 路径（D4）；`transcribe_guarded` 不做任何 try/except（异常由调用点既有路径处理）。

## 9. 风险与开放问题

| ID | 风险 | 评估 / 对策 |
|---|---|---|
| R1 | 阈值降到 0.3 后，背景噪声更容易被当语音 → 幻觉回升 | 现有样本 0% 误报；上线后靠归档的 `kept_ratio` + 静默样本监控；必要时 `VOICE_INPUT_VAD_THRESHOLD=0.5` 一键回退 |
| R2 | 气声重跑（0.1）可能把噪声当语音，产生垃圾文本 | 能量门（−58 dBFS）挡掉纯静音；气声本身就该出字，垃圾文本由回声/纠错层兜底 |
| R3 | 回声判定的假阳性（用户真的重复一个热词 ≥4 次） | 只在 `prompt` 非空时判定，且措辞重复只是触发**重转**；是否丢弃由 `hotword_dominated` 决定——真实句子去掉热词后仍有大量实义字符，会保留。最坏情况是长文里热词占绝对多数被丢一句，已写进 README |
| R4 | −58 dBFS 门槛样本不足（1 条气声 + 1 条静音） | 写成常量并文档化；补录静音样本后可调；实现不依赖精确值（0.1 重跑 + 能量门双重条件） |
| R5 | `transcribe_guarded` 返回 tuple 改动两处调用点 | 两处都有完整测试覆盖；类型变化在 PR 内一次到位 |
| R7 | `voice-ptt.py`（X11）获得阈值/重跑收益但无 echo guard | §6/§4.4 显式声明的边界；该链路在本机无法测试，接未测代码风险更大。7700K 真机验证时若出现 19 遍循环，再单独立项 |
| R6 | 分型 C 无本轮复现音频 | 用 10-24 的 19 遍文本做回归用例；规则基于文本形态，不依赖音频 |

## 10. 交付切片

单个 PR：
1. `feat(vad): threshold env + energy-gated retry (#43)` — `vad_trim.py` + 测试
2. `feat(terms): prompt-echo guard (#43)` — `terms.py`、`transcribe_guard.py` + 测试
3. `feat(vad): wire guard into worker + CLI (#43)` — 两处调用点、README 双语 + 测试


## 8b. 实施期发现（记录在案）

| 项 | 发现 | 处置 |
|---|---|---|
| 系统解释器下的测试 | `vad_options_from_env()` 需要 import `VadOptions`，在无 faster-whisper 的系统解释器里会 ImportError；而 `detector` 注入路径本来不需要它 | 新增 stdlib `_PlainOptions` 兜底 + `_resolve_options(vad_options, injected)`：注入 detector 时用兜底对象；默认路径构造失败也降级到它（真实检测仍会因缺 faster-whisper 走 degraded）。`TestOptionsFromEnv` 改为 `skipUnless(HAS_VAD)` |
| 重跑阈值 0.1 救不了气声 | A14 实测：`2026-10-27_230459`（3.45s，RMS −54.3 dBFS）在 0.1 下仍 0 覆盖；逐档实测 0.08 → 0%、**0.05 → 32.4%**、0.03 → 52.3% | 维持 F2 的 0.1（与 spec 既定「仍无 span 则保持空」一致）。气声列为**已知边界**：`VOICE_INPUT_VAD=0` 实测该样本可转出「Hello,能不能听到我说话?Hello,…」，但 0.05/0.03 级阈值会把安静房间噪声当语音，需先用归档里的噪声样本评估，不在本次扩大 |
| 分型 A 实测恢复 | `2026-10-27_230451`（6.91s）在 0.3 下保留 **6.61s / 95.7%**（0.5 时 63%），文本从「再说清楚点吧」变为完整句 | A14 通过 |

### 8c. 本地 fresh-context CR 后的修订（2026-10-28）

| 发现 | 处置 |
|---|---|
| P1-1 纯 ASCII 热词无词边界（`AI` 命中 `email`） | `_term_hits` 对 ASCII 词加 `(?<![a-z0-9_-])…(?![a-z0-9_-])`，与 corrections 同口径 |
| P1-2 假阳性会直接丢内容，spec R3 描述不实 | 新增 `hotword_dominated`：重转仍命中时只有「热词主导」才丢，否则保留；R3 与 README 同步改写 |
| P2-1 阈值已低于 0.1 仍重跑 | 加 `RETRY_THRESHOLD < options.threshold` 守卫 |
| P2-2 `retried` 只表示「重跑成功」 | 改为表示「尝试过重跑」，同步改测试与 §4.1 |
| P2-3 disabled 路径也写 `echo_guard` | 以实现为准（disabled 仍转写），更新 §4.1 |
| P2-4 整段 RMS 被单个咔哒声骗过 | 改为 `loud_frame_count`：20 ms 帧计数 ≥ 5 才重跑 |
| P2-5 规则 (c) 无长度上限 | 保留（长序列仍值得重转），丢弃由 `hotword_dominated` 兜住 |
| P2-6 `_with_threshold` 兜底丢字段 | 补齐 `min_silence_duration_ms` 拷贝 |
| P2-8 重转保留 `hotwords` | 重转剔除 `hotwords`（`_without_hotwords`） |
| P2-9 commit scope 只写 vad | 合并为 `feat(vad,terms): …` |
