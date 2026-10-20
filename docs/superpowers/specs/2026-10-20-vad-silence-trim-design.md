# #32 根因报告 / 设计：hold 听写病态重复循环 + 静音幻觉

Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-20T22:47:50+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-20T22:59:28+08:00（round 1 处置）
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-20T23:08:26+08:00（round 2 处置）
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-20T23:16:50+08:00（round 3 处置，细化项）
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-20T23:20:02+08:00（收敛后事实修正：test_engine 在系统 Python 下本就红，A11 拆分）

Issue: https://github.com/zhuxixi/voice-input/issues/32
Umbrella 关联：#14（不在本 issue 范围）
调研证据：`research/01-repro-and-attribution.md`、`research/02-solution-space.md`

## 1. 根因

**Whisper 的输入混入了大段静音，解码器在静音上产生幻觉、在重复 token 上放大循环。** 不是 #27 热词回归，也不是模型缺陷：

- 复现音频 61.82s 中真实语音仅 29.94–39.42s；生产路径输出段首「词曲 李宗盛」、段尾「字幕製作人Zither Harp」，「这个」×7。
- 只裁首部静音 → 尾部出现「字幕」×几十的病态循环；只裁尾部静音 → 首部「词曲 李宗盛」仍在。两个幻觉分别由首、尾静音触发。
- `faster-whisper cpu`（同音频）输出准确、旧静态管线同样循环 → 与 #27 无关，是 OpenVINO 解码路径对静音 + 重复 token 的弱点。
- 解码参数不是必要修法：`prompt + repetition_penalty=1.3` 会把正常重复压成 1 次（过度抑制），`无 prompt + 1.3` 准确但丢热词。

**修法**：转写前用 VAD 只保留语音段，丢弃首尾静音与段内长静音。实测（同一音频）：幻觉消失，循环 ×7（与口述次数同量级），解码时间从 3.9s 降到 ~1.0s。

## 2. 设计决策

### D1 VAD 选型：`faster_whisper.vad`（Silero VAD v6）

- `venv` 内 faster-whisper 1.2.1 已随包分发 `faster_whisper/assets/silero_vad_v6.onnx` 与其代码；`onnxruntime` 本来就是 faster-whisper 的依赖（`faster_whisper-1.2.1.dist-info/METADATA: Requires-Dist: onnxruntime`）。
- **零新增 pip 依赖、零模型下载、零 vendor 文件。**
- 不采用能量阈值：同一音频阈值 0.006 残留底噪、0.04 切进语音丢字（`research/01` 表格）。
- 不采用 faster-whisper 的 `vad_filter=True`：NPU 适配器不走 faster-whisper，且会与预裁剪重复执行 VAD。

### D2 裁剪语义：保留全部语音 span，丢弃首尾与段内静音

- 用库默认参数：`threshold=0.5`、`min_silence_duration_ms=2000`、`speech_pad_ms=400`、`min_speech_duration_ms=0`（vad.py:37-42 实测默认值；不做额外最短语音过滤，避免吞短词）。
- 所有 span（含 400ms 前后 padding）按时间顺序拼接；span 之间 ≥2s 的静音被丢弃（短于 2s 的停顿被 VAD 合并，不受影响）。
- 既治段首/段尾幻觉（实测），也治 issue 记录的「段内静音」诱发面；代价是跨 2s 停顿的句读可能丢掉，由 U3 验证。
- 裁剪后时长 ≥ 原时长 99% → 不写临时文件，直接返回原路径（避免正常短句的无谓 IO）。该热路径由 A3b 单独锁定（方向写反会让幻觉修复静默失效）。

### D3 新模块 `vad_trim.py`（新增，全部为新增函数）

```python
DEFAULT_PAD_MS = 400
class TrimUnavailable(Exception): ...          # 内部：VAD 不可用/音频格式不支持

def span_bounds(spans, total_samples) -> list[tuple[int, int]]:
    """纯函数：把 VAD span（秒→采样点已完成）夹到 [0,total]，合并重叠，丢弃空 span。"""

def speech_spans(frames: bytes, sample_rate: int, *, vad_options=None) -> list[tuple[int, int]]:
    """新增：把 16-bit mono frames 转 float32 后调 faster_whisper.vad.get_speech_timestamps。
    numpy / faster_whisper 在函数内懒导入。返回采样点区间。"""

def spans_from_wav(path: str) -> tuple[bytes, list[tuple[int, int]]]:
    """新增：读 16kHz mono 16-bit wav（stdlib wave），返回原始 frames 与 span。
    非 16k/mono/16-bit → TrimUnavailable（调用方回退原音频）。"""

@contextlib.contextmanager
def trimmed_wav(path: str, *, detector=spans_from_wav, enabled: bool | None = None):
    """新增：产出裁剪后的临时 wav 路径；无语音时产出 None。
    - enabled 默认读环境变量 VOICE_INPUT_VAD（缺省开启，值为 "0" 关闭）
    - 关闭 / detector 抛 TrimUnavailable / 其他异常 → 产出原 path（降级，绝不阻断听写）
    - spans 为空 → 产出 None（调用方直接产出空文本，不解码）
    - 临时文件 mkstemp(0o600)，finally 删除；成功路径只删临时文件，不动原 wav
    """
```

`span_bounds`、`trimmed_wav` 的帧切分只用 stdlib（`wave` + bytes 切片），系统 Python 可直接测；`speech_spans`/`spans_from_wav` 依赖 numpy/faster-whisper（懒导入）。

### D4 接入点：三个转写入口，引擎适配器不动

只改调用点，`engine.py` 与两个适配器零改动（保住 #16 的 CUDA 逐字节契约）：

| 文件:行 | 现状 | 改法 |
|---|---|---|
| `transcribe_worker.py:105` | `model.transcribe(wav, ...)` | `with vad_trim.trimmed_wav(wav) as src: src is None → text=""; else transcribe(src)` |
| `transcribe_once.py:91` | 同上 | 同上 |
| `voice-ptt.py:250` | 同上 | 同上（最后统一清理原 wav） |

裁剪后的临时路径喂给现有 `model.transcribe(path)`，CUDA / CPU / NPU 三条引擎路径行为一致。

### D5 无语音行为：返回空文本，不解码

VAD 无 span 时 `trimmed_wav` 产出 `None`，调用方直接得到空文本（不调用 `model.transcribe`），从而：
- 误按热键 / 全程静音 → 不再产生「字幕by索兰娅」「词曲 李宗盛」；
- 省一次解码。

**风险（明确承认）**：极轻/极短语音可能被判为非语音而丢字。缓解：`VOICE_INPUT_VAD=0` 逃生开关；U1 用正常音量验证；不承诺「任何音量都不丢字」。

### D6 降级与开关

- `VOICE_INPUT_VAD=0` → 完全跳过 VAD（等价旧行为）。
- VAD 导入失败 / 音频格式意外（非 16k mono 16-bit）/ 任意异常 → 记录一行日志后回退原音频，转写照常。
- 不新增其他可调参数（pad、阈值固定），避免调参面扩散。

### D7 可观测性

每次听写输出一行 stderr/journal 日志：
`[voice-input] vad: 61.82s -> 9.95s (1 span)`；降级时 `vad: unavailable (...), using original audio`；无语音时 `vad: no speech, empty transcript`。**日志发射方：前两种在 `trimmed_wav` 内部打印；无语音行由调用方在 `src is None` 分支打印**（worker 走 `log`，`transcribe_once`/`voice-ptt` 走 stderr），因此 `trimmed_wav` 被 mock 时 A7 的 stderr 断言仍成立。
worker 日志走 journal，X11 走 stderr，沿用现有通道。

### D8 不改的东西

- 不改解码参数（`repetition_penalty` 等），不改模型（`small-int8-ov` 不动）。
- 不改 `_deliver` 的 0.3s settle（#39 独立处理）。
- 不做词表容量（#33）、不做 GNOME Wayland（#36）、不做分片多次转写。
- 不改 `voice_hold.py` 的录音/上屏/浮层逻辑。

## 3. 契约与边界

- **CUDA 契约**：`engine.py` 零改动；默认引擎/模型不变。新增模块不 import 引擎。
- **依赖契约**：生产路径新增依赖仅 `numpy` + `faster_whisper`（都已在 venv）；系统 Python 测试路径不导入它们。
- **文件契约**：原 wav 的创建/删除责任仍在各入口（`voice-ptt.py:267`、worker 不管文件），`vad_trim` 只管理自己的临时文件。
- **线程/时序**：VAD 在转写线程内同步执行（worker 单请求串行），不新增线程；VAD session 首次调用 ~105ms，之后 9.2ms/8s。worker 启动时不做额外预热（首次听写多 ~几十 ms，避免启动变慢；由 U4 观测）。
- **不做保证**：不保证消除所有重复（正常口吃仍可能被放大到接近口述次数）；只保证消除静音幻觉与病态长循环（≥31 次级别）。

## 4. 可测性拆分设计

| 层 | 单元 | 依赖 | 测试方式 |
|---|---|---|---|
| 纯函数 | `span_bounds` | stdlib | 构造 span 列表断言夹取/合并/空 span |
| 纯函数（无 VAD） | `trimmed_wav` 的启停/降级路径 | stdlib | 注入 `detector`，断言产出路径、None、原路径；断言临时文件生命周期 |
| 薄 IO | `spans_from_wav` | stdlib wave + numpy | 用临时 wav 断言格式拒绝分支（非 16k）；正常分支靠集成/实测 |
| VAD wrapper | `speech_spans` | numpy + faster_whisper | A6b：`venv/bin/python3 -m unittest test_vad_trim.TestSpeechSpans -v`；`skipUnless(numpy 与 faster_whisper 可导入)`，系统 Python 下 skip 不红。`unittest.mock.patch` 替换 `faster_whisper.vad.get_speech_timestamps`，断言入参为 16k float32 与返回换算 |
| 入口接线 | `transcribe_worker.worker_main` | fake model | A7/A8：`python3 -m unittest test_transcribe_worker.TestVadWiring -v`；patch `transcribe_worker.vad_trim.trimmed_wav`：None → 空文本且不调用模型；临时路径 → 模型收到该路径 |
| 入口接线（静态） | `transcribe_once.py` / `voice-ptt.py` | stdlib | 静态守卫：两个文件都包含 `vad_trim.trimmed_wav(` 调用 |
| 真实 E2E | NPU + 复现音频 | 真机 | 仅当 `~/.local/share/voice-input/debug/repro-loop-20260930.wav` 存在时运行，否则 skip |

测试边界：VAD 的识别质量不做单测（依赖真实语音），由 U1/U3 覆盖；`vad_trim` 不负责录音、清理原 wav、引擎选择。

## 5. 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | `span_bounds` 夹取/合并/空 span | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestSpanBounds -v` | 越界被夹到 `[0,total]`；重叠合并；空 span 丢弃；顺序保持 |
| A2 | `VOICE_INPUT_VAD=0` 完全旁路 | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_disabled_passthrough -v` | 产出原路径、无临时文件、detector 零调用 |
| A3 | 正常裁剪与临时文件生命周期 | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_trim_creates_and_removes_temp -v` | 上下文内文件存在且时长≈span+padding；退出后文件不存在；原 wav 不变 |
| A3b | ≥99% 热路径直接返回原路径 | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_near_full_length_passthrough -v`（注入覆盖 100% / 98% 两种 span） | ≥99% → 原路径且无临时文件；<99% → 走裁剪临时文件 |
| A4 | 无语音 → None | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_no_speech_yields_none -v` | 产出 None；不创建临时文件 |
| A5 | VAD 失败降级 | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_detector_error_falls_back -v`（detector 抛异常 / 抛 TrimUnavailable） | 产出原路径；日志含 `vad:`；不抛异常 |
| A6 | 非 16k/mono/16-bit 音频拒绝 | 自动化（unit，stdlib） | `python3 -m unittest test_vad_trim.TestTrimmedWav.test_rejects_non_16k -v`（临时 8k wav） | 回退原路径、不崩 |
| A6b | `speech_spans` float32 换算与 span 返回 | 自动化（unit，skipUnless 依赖可导入） | `venv/bin/python3 -m unittest test_vad_trim.TestSpeechSpans -v`（mock `get_speech_timestamps`） | 传入 float32/16k；span 采样点换算正确；系统 Python 下 skip 不红 |
| A7 | worker 接线：无语音返回空文本 | 自动化（integration，stdlib + fake model） | `python3 -m unittest test_transcribe_worker.TestVadWiring.test_no_speech_returns_empty_text -v` | fake model 未被调用；协议返回 `""`；可捕获 stderr 含 `vad: no speech` |
| A8 | worker 接线：裁剪路径传给模型 | 自动化（integration，fake model） | `python3 -m unittest test_transcribe_worker.TestVadWiring.test_trimmed_path_reaches_model -v` | fake model 收到的 wav 路径来自 `trimmed_wav` |
| A9 | X11 / test-mic 入口接线 | 自动化（static，stdlib） | `python3 -m unittest test_vad_trim.TestWiring -v`（读源码断言调用点） | `transcribe_once.py` 与 `voice-ptt.py` 均含 `vad_trim.trimmed_wav(` |
| A10 | 真实音频 E2E（NPU） | 自动化（automated E2E，三条件 skipUnless） | `LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu VOICE_INPUT_ENGINE=npu venv/bin/python3 -m unittest test_vad_trim.TestEndToEnd -v`；skip 谓词 = 复现音频存在 **且** `import numpy, faster_whisper, openvino_genai` 成功 **且** `VOICE_INPUT_ENGINE=npu` | 输出不含「词曲 李宗盛」「字幕」「Zither」；「这个」≤ 8 |
| A11a | 全仓回归（系统 Python，stdlib 套件） | 自动化（unit，stdlib） | `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim -v` | 全绿；`TestEndToEnd` 与 `TestSpeechSpans` 因 skip 谓词不满足而 skip（不红） |
| A11b | 引擎契约回归（venv Python） | 自动化（unit，venv） | `venv/bin/python3 -m unittest test_engine -v` | 全绿，锁住 `engine.py` CUDA 默认契约（实测系统 Python 跑该模块 8 个 numpy 用例必红，故拆分） |
| U1 | 真机正常听写不退化 | 用户实测 | 合并后在主 checkout `systemctl --user restart voice-hold`；正常音量说 3 句不带长停顿；再按住不说话 2s 松开 | 3 句文本与改动前同义、无丢字；空按键**不产生任何文本**；`journalctl` 出现 `vad:` 行；主观延迟无退化 |
| U2 | 长停顿样本不再幻觉 | 用户实测 | 同一服务下，按住说一句 → 停 5s → 再说一句 → 松开 | 输出无「词曲 李宗盛」「字幕…」「Zither」；两句内容都在 |
| U3 | 多停顿句读可接受 | 用户实测 | 一段包含 2 次以上 ≥2s 停顿的口述 | 内容完整；允许句读合并（预期取舍），不接受丢句 |
| U4 | 延迟不退化 | 用户实测（日志观测） | 合并后同一句：先加 drop-in `Environment=VOICE_INPUT_VAD=0` 采 N 次 `transcribed in X s` 作基线，再移除 drop-in 采 N 次 | 中位数增量 ≤ 0.05s；长静音样本解码时间应下降 |
| U5 | X11 路径（voice-ptt.py） | 用户实测（另一台机器） | 该机器 `git pull` 到含本改动的 commit（voice-ptt.sh 每次新起进程，无需重启服务）后 `./voice-ptt.sh` 正常听写一句 | 文本正常、无异常日志；本 session 无 X11 机器 → **pending** |

失败分支覆盖：A2（关闭）、A5（VAD 异常）、A6（格式异常）、A7（无语音）、A4（无 span）。U5 明确 pending，不宣称完成。

## 6. 落地与验证时机

- 代码从 worktree 出 PR；合并前只能跑自动化（A1–A11），**U1–U4 必须在合并后主 checkout 上执行**（systemd 单元 `ExecStart` 指向主 checkout，见 `contrib/voice-hold.service`；worktree 代码不会被服务加载）。
- U5 需要 X11 机器，本 session 不可执行，合并前在 PR 中标记 pending。
- 两份 README（`README.md` + `README.zh-CN.md`）同步：配置表新增 `VOICE_INPUT_VAD`，Testing 行加入 `test_vad_trim`（系统 Python 套件）并单列 `venv/bin/python3 -m unittest test_engine -v`（引擎契约，需 venv），NPU/Wayland 段落各补一句裁剪行为（与 #20 无冲突，只改相关行）。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| VAD 吞掉轻声/短词 | `VOICE_INPUT_VAD=0`；U1 正常音量验证；`min_speech_duration_ms=0`（库默认，不做最短语音过滤） |
| 段内静音拼接导致句读丢失 | U3 显式验收「允许句读合并、不接受丢句」 |
| 临时文件泄漏 | A3 断言 finally 清理；mkstemp 0o600 |
| 首次调用 VAD 增 ~100ms | U4 用日志观测；必要时后续在 worker 启动预热（本 spec 不做） |
| onnxruntime 在异常环境缺失 | D6 降级原音频，听写不中断 |
| 长音频 VAD 开销增长 | 线性（73ms/62s）；>5 分钟录音仍 <0.5s，暂不优化 |

## 8. 非目标

- 不改解码参数、不换模型、不做分片转写。
- 不处理 #39 的 `_deliver` settle。
- 不扩词表容量（#33）。
- 不新增 VAD 参数配置面（只留 `VOICE_INPUT_VAD` 开关）。
- 不保证消除正常口吃造成的轻微重复。
