# Spec: NPU engine — openvino-genai WhisperPipeline integration (#19)

- Date: 2026-09-29
- Status: design approved in chat (§1–§7), revised after two review rounds（7700K 零行为约束撤上屏统一、U2 可判定化、cpu 惰性差异声明）, final draft
- Umbrella: #14. Depends on: #16 (closed), #17 (closed), #18 (closed)
- Research: `research/npu-engine-integration.md` (same dir) + issue #19 调研评论

## Goal

`VOICE_INPUT_ENGINE=npu` 端到端可用：OmniBook (Lunar Lake NPU, KDE Wayland) 上按住热键 → 录音 → NPU 转写 → 上屏。单次转写（热缓存）目标量级 ~1–2s（加载 0.8s + 转写 0.3s + 进程开销），对齐 #17 基准。顺路收敛一条延后技术债：LD_LIBRARY_PATH preamble 收敛（范围 A 的上屏统一因 7700K 零行为变化约束撤出，见 Open decisions #5）。其余 7 条债继续挂账。

**硬约束：7700K（X11 + cuda 引擎机器）零行为变化**——本 spec 对 voice-ptt.py / voice-ptt.sh / voice-toggle.sh / test-mic.sh / transcribe_once.py 的所有改动必须是输出字节等价的纯重构（环境变量值、上屏行为、默认模型逐项不变），由 A5/A7/A8 钉死。

## Scope / Non-goals

**In scope**

1. engine.py npu 分支真实现（adapter 形态）+ OV 模型路径解析
2. LD_LIBRARY_PATH 单点计算（engine.required_lib_paths）与各注入点接线
3. 热词在 NPU 的优雅降级 + stderr 告警
4. voice-ptt.py 内嵌 nvidia preamble 拷贝收敛（上屏段一行不动）
5. transcribe_once.py preamble 第 5 份拷贝收敛（债①的 Python 侧）
6. bench/npu-bench.py 同语义库路径函数收编为 engine.prepend_library_path（第 6 份拷贝，test_bench 断言应仍绿）
7. voice-toggle.sh / test-mic.sh 预检 npu 占位改真实验证（按引擎选模型布局：npu→ov 谓词，其他→ct2 谓词）；download-model.py 的 npu 拒绝文案更新（改为指向 OV 模型获取命令，不再是「未实现」）
8. systemd contrib 示例更新（npu 配置）；README NPU 章节（模型获取、缓存体积、冷编译说明）
9. e2e 实测数据回贴 issue #19

**Non-goals**

- 上屏统一（voice-ptt.py → paste.py）：撤出。paste.py 的 X11 契约是 `xdotool type` 逐字键入，与 voice-ptt.py 现状「xsel 剪贴板 + Ctrl+Shift+V」不同，统一会改变 7700K 行为（违反硬约束）；债继续挂账
- download-model.sh 支持 OV 模型下载（DEST 双写债挂账；模型已在 HF 缓存，README 记手动命令）
- 浮层 / 录音生命周期 / paths helper / 测试 helper 四组副本统一（挂账）
- NPU 冷启动 prewarm、CACHE_DIR env knob（YAGNI）
- 常驻转写 worker（voice_hold 改为模型常驻子进程架构，消掉每次听写 0.8s 加载）——已定方向，单独立 issue #25 跟踪，本 issue 不做
- 动态管线、return_timestamps 等被 adapter 掩盖的 genai 能力
- Wayland 听写链路接入热词（transcribe_once 加载 terms）——现状 cpu/cuda 也不接，属独立需求，挂账
- faster-whisper 三引擎（cuda/cpu/auto）任何行为变化

## Research-backed facts（设计依据，勿推翻需新证据）

| 事实 | 来源 |
|------|------|
| NPU 三前置：驱动 ≥1.38.0（已装）、显式 NPU_PLATFORM="NPU4000"、进程启动时 LD_LIBRARY_PATH 含 /usr/lib/x86_64-linux-gnu | #17 + JFox 202609192220428379 |
| 48s 静态编译不跨进程持久；CACHE_DIR 编译缓存：冷 46.7s / 热 0.8s / 848MB | 2026-09-29 本机实测（issue 评论） |
| NPU 静态管线 C++ 硬 check 拒绝 initial_prompt 与 hotwords；动态管线在 NPU 上 generate 崩 | 同上 |
| genai 2026.4：generate(raw_speech_input=float32[], language="zh") → .texts；wav 需 stdlib wave + numpy | JFox 202609192220486528 + bench/npu-bench.py |
| 进程内改 LD_LIBRARY_PATH 对 ld.so 无效 | JFox（A/B 实证） |

## Design decisions

1. **Adapter 适配器**（已选，备选见 issue 设计讨论）：`build_model("npu")` 返回 duck-typing 兼容 faster-whisper 的 `_NpuWhisperAdapter`，调用方零改动。签名兼容由单测钉死（A3）。
2. **模型名映射**：新增 `default_model(engine) -> str` 单点（npu→`small-int8-ov`，其他→`large-v3` 契约默认不变），build_model 与两处 shell 预检共用；不设 VOICE_INPUT_MODEL 时 npu 不会错找不存在的 `OpenVINO/whisper-large-v3`。映射 HF repo `OpenVINO/whisper-small-int8-ov` → `models--OpenVINO--whisper-small-int8-ov/snapshots/`；快照谓词 `openvino_encoder_model.xml`。非 npu 引擎的解析逻辑逐字不变。
3. **CACHE_DIR** 固定 `~/.cache/voice-input/npu-compile-cache`（expanduser，无 env knob）。
4. **环境注入分层**：engine.py 单点计算 → voice_hold 经 `engine.child_env()`（npu 时注入 NPU 路径，其他引擎原样拷贝由 transcribe_once 进程内 preamble 维持现状——voice_hold 不碰 venv site 探测）/ shell wrappers export / transcribe_once 裸调缺路径时给可行动报错（不做 re-exec）。**cpu 惰性差异声明**：现状 wrapper/transcribe_once 无条件设置 nvidia 路径，收敛后 engine=cpu 时不再设置——功能惰性（CPU 推理不依赖 cublas/cudnn，无 N 卡机器上这些路径本就不存在），7700K 默认 cuda 零影响；A6/A8 的「等价」限定于 cuda/auto 分支。**prepend 幂等差异声明**：父环境已含目标路径时 prepend 去重不重复拼接（HEAD 会重复追加）——ld.so 对重复分量语义等价，属惰性差异
5. **热词降级**：adapter 每次转写检查 initial_prompt/hotwords，非空则 stderr 告警一行后忽略。
6. **上屏不统一**（裁决修正）：voice-ptt.py 上屏段一行不动；统一债挂账。

## Component contracts

### engine.py（modified，顶层保持纯标准库）

新增（全部可独立单测）：

```python
def required_lib_paths(engine: str, site_packages: str | None = None) -> list[str]
    # cuda/auto -> [f"{site}/nvidia/cublas/lib", cudnn, cuda_nvrtc]（与现拷贝逐字同序）
    # npu -> ["/usr/lib/x86_64-linux-gnu"]; cpu -> []
    # site_packages None 时 cuda/auto 抛 ValueError（防悄悄算错路径）

def prepend_library_path(env: dict, paths: list[str]) -> str
    # 前(paths) | (旧前缀)，幂等：已全含的分量不重复拼
    # 只操作传入 dict（可测、不污染 os.environ）

def has_library_paths(env: dict, paths: list[str]) -> bool
    # 「paths 已全部在 env 的 LD_LIBRARY_PATH 中」谓词（分量精确匹配，bench _lib_dir_present 语义收敛至此）
    # prepend_library_path 的幂等核心；transcribe_once 的 npu 缺路径检查与 bench 的
    # needs_reexec 判定共用本谓词——防第 7 份拷贝

def default_model(engine: str) -> str
    # 引擎感知的默认模型名单点：npu -> "small-int8-ov"，其他 -> "large-v3"（#16 契约不动）
    # build_model 与 shell 预检共用，防 npu 无 MODEL 时错找不存在的 repo

def child_env(env: dict) -> dict
    # spawn transcribe 子进程用的 env：npu 时 prepend NPU 库路径（ld.so 进程启动前置），
    # 其他引擎/非法引擎原样拷贝放行（由 transcribe_once 进程内 preamble / 可行动报错维持现状）。
    # 纯函数，voice_hold 唯一注入点

def ov_snapshots_base(model: str) -> str
    # expanduser(f"~/.cache/huggingface/hub/models--OpenVINO--whisper-{model}/snapshots")

def resolve_model_path(model=None, base=None, layout="ct2") -> str
    # layout: "ct2"(默认,谓词 model.bin,现状逐字不变) | "ov"(谓词 openvino_encoder_model.xml)
    # 报错信息按布局区分下载命令：ct2 -> ./download-model.sh <model>；
    # ov -> huggingface-cli download OpenVINO/whisper-<model>

def _load_wav_samples(path: str) -> "numpy.ndarray"   # stdlib wave 校验(16-bit 硬校验,沿 bench 语义) + 懒 numpy
class _NpuWhisperAdapter:
    # .transcribe(wav, language="zh", initial_prompt=None, hotwords=None, **_ignored)
    #   -> (segments, info); segments=[SimpleNamespace(text=...)] 或空列表
    # 构造: pipeline_factory 可注入(单测);默认懒 import openvino_genai 构造
    #   og.WhisperPipeline(model_dir, device="NPU", NPU_PLATFORM="NPU4000",
    #                      STATIC_PIPELINE=True, CACHE_DIR=<见上>)
    # initial_prompt/hotwords 非 None -> stderr warn 一行(含原因),继续转写
```

`build_model`：npu 分支替换两处 NotImplementedError（construction_kwargs 的 npu 分支改 ValueError——见 Open decisions #3）；model=None 时取 `os.environ.get(ENV_MODEL, default_model(engine))`，resolve_model_path 用 ov 布局。新增 `model_base=None` 可选注入缝（单测造布局用；None 时行为与现状逐字一致）。

### transcribe_once.py（modified）

- 删除内嵌 nvidia preamble 块（第 5 份拷贝）。顺序修正：先 `import engine`（顶层纯标准库，安全）→ 计算 `engine.required_lib_paths(eng, site)` 并 `prepend_library_path(os.environ, ...)` → 后续 build_model 懒导入推理栈。cuda 下环境变量值与现状逐字节一致（A6 钉死）；engine=cpu 时不再设置 nvidia 路径（惰性差异，见 Design decisions #4）
- npu 且 `not engine.has_library_paths(os.environ, required)`：可行动报错 exit 1（报错文本含确切 export 命令）
- 转写调用与热词：维持现状调用形态（adapter 兼容使其零改动）；本 CLI 不加载 terms 是现状，npu 下同样不接——Wayland 听写链路热词接入是独立需求挂账，本 issue 只保证 X11 voice-ptt 链路的热词降级语义（adapter warn，A3/U2 验证）

### voice_hold.py（modified，最小面）

- spawn transcribe_once 的 `check_output` 增加 env 参数：`env=engine.child_env(self.env)`（顶部新增 `import engine`——顶层纯标准库，script 目录在 sys.path 可导入）
- 不动浮层/录音生命周期/paths（挂账）

### voice-ptt.sh / voice-toggle.sh / test-mic.sh（modified，同款收敛）

- nvidia 三路径 export 段替换为：用 venv python 算 `engine.required_lib_paths`（模式同现有 SITE_PACKAGES 探测），按引擎 export；engine=npu 时导出 NPU 路径、跳过 nvidia 段（issue 正文 #4）；engine=cpu 时导出空（惰性差异，见 Design decisions #4）
- voice-toggle.sh / test-mic.sh 预检：删 npu NotImplementedError 占位；按引擎选布局调用——`eng=engine.engine_name(env)` 后 `engine.resolve_model_path(layout="ov" if eng=="npu" else "ct2")`（model 缺省时经 `default_model(eng)`），毫秒级不加载模型

### voice-ptt.py（modified，仅 preamble）

- 33-38 行内嵌 nvidia preamble 拷贝收敛为 `engine.required_lib_paths` + `prepend_library_path`（先 import engine 再设置）；cuda 下 LD_LIBRARY_PATH 值逐字节不变（A7 钉死）
- **上屏段（:231-245，xsel+xdotool）一行不动**——7700K 零行为变化硬约束；转写调用零改动（adapter 兼容）

### bench/npu-bench.py（modified，收编第 6 份拷贝）

- `set_npu_library_path`/`_lib_dir_present` 删除，改调 `engine.prepend_library_path` / `engine.has_library_paths`（needs_reexec 的哨兵逻辑保留在 bench，路径判定走 engine）；test_bench.py 既有断言应仍绿（A5 回归含）

### download-model.py（modified，一行文案）

- npu 拒绝分支保留（下载 faster-whisper 格式对 npu 无意义），文案从「NPU engine not implemented yet — see issue #19」改为「npu engine uses OpenVINO models — see README」（否则实现后它变成谎言）

### contrib/voice-hold.service（modified，示例）

- Engine 示例改 npu + MODEL=small-int8-ov，注释注明：无需 LD_LIBRARY_PATH（voice_hold 注入）、首次编译 ~47s、缓存 ~850MB

### README.md / README.zh-CN.md（minimal）

- NPU 章节：模型手动获取命令（hf/huggingface-cli download OpenVINO/whisper-small-int8-ov）、三前置、缓存说明。双语同步欠账记 #20 之外单独处理（见 Open decisions）

## Degradation / failure paths

- **热词**：NPU 不支持 → warn + 无 prompt 转写（terms.py 既有原则：增强不阻断）
- **模型缺失**：resolve_model_path RuntimeError 带模型名与获取命令（可行动）
- **裸调 transcribe_once + npu 无 env**：exit 1 + export 命令提示
- **CACHE_DIR 所在盘满/无写权限**：genai 构造抛错 → 沿现有 main() 干净退出路径；README 注明缓存可 rm 重建（代价 47s）
- **NPU 设备被占用/驱动异常**：genai 构造/推理抛 RuntimeError → daemon 侧现状错误路径（voice-ptt 捕获打日志；voice_hold 子进程超时/非零退出已有处理）

## Safety contract（既有行为不变，契约测试钉死）

- 不设任何环境变量 = cuda + large-v3 + float16 构造参数（HEAD 现状，A5 既有契约测试全绿）
- **7700K（X11 + cuda）零行为变化**：voice-ptt.py 上屏段、各 wrapper 的 cuda 导出行、transcribe_once 的 cuda preamble 输出（路径集与顺序）全部字节级不变（A5/A7/A8 钉死）
- cpu/auto 分支、resolve_model_path ct2 布局、download-model.* 全部零改动

## Acceptance matrix

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | npu 构造参数正确（device/NPU_PLATFORM/STATIC_PIPELINE/CACHE_DIR/模型路径） | 自动化验证（unit） | `venv/bin/python3 -m unittest test_engine -v -k npu`（fake pipeline_factory 断言 kwargs） | 断言全过 |
| A2 | OV 模型解析（default_model 矩阵/布局谓词/分布局错误信息） | 自动化验证（unit） | `venv/bin/python3 -m unittest test_engine -v -k ov_`（tmp_path 造 ct2/ov/空布局；default_model 引擎矩阵） | 断言全过 |
| A3 | adapter 兼容契约：签名、segments/info 形状、文本拼接、热词降级 warn、wav 16-bit 硬校验 | 自动化验证（unit） | `venv/bin/python3 -m unittest test_engine -v -k adapter`（fake pipeline + tmp wav） | 断言全过 |
| A4 | required_lib_paths 引擎矩阵（cuda 输出与历史硬编码字符串逐字节相等）+ prepend 幂等 + has_library_paths 谓词 + child_env（npu 注入/cpu 透传/非法引擎放行） | 自动化验证（unit） | `venv/bin/python3 -m unittest test_engine -v -k lib_paths` | 断言全过 |
| A5 | 既有行为不变（cuda/cpu/auto/ct2/默认值/bench） | 自动化验证（unit，既有契约） | `venv/bin/python3 -m unittest test_engine test_terms test_archive test_bench -v`（全量回归） | 全绿（npu 占位契约测试按新契约改写：build_model('npu') 构造 adapter、construction_kwargs('npu')→ValueError，其余既有测试不变） |
| A6 | transcribe_once preamble 收敛后输出等价 + npu 缺路径报错 | 自动化验证（integration） | `venv/bin/python3 -m unittest test_engine -v -k transcribe_once_env`（子进程 harness：设/不设 env 的行为） | 断言全过 |
| A7 | 7700K 零行为变化证明 | 自动化验证（static） | 三项静态检查：git diff 无 voice-ptt.py 上屏段（xsel/xdotool 块）改动；voice-ptt.py 源内无硬编码 nvidia 库路径残留（已收敛为 engine 调用）；import engine 先于 preamble 设置（源顺序） | 三项全过（字节等价由 A4 的 cuda 历史字符串断言承担） |
| A8 | shell wrappers 收敛后 cuda 段等价、npu 段正确 | 自动化验证（integration） | `bash -n`（语法）+ 子进程跑 wrapper 的 export 计算（注入 fake venv python） | 断言全过 |
| U1 | OmniBook e2e：npu 听写 → Wayland 上屏；延迟数据 | 用户实测 | service 切 ENGINE=npu → 按住右 Alt 说 8s 中文 → 松开；计时 = journalctl -o short-precise 的 delivered 行时间戳与松手时刻差（辅以秒表），连续 5 次取中位；数据回贴 #19 | 文本上屏正确；热缓存单次端到端 ≤5s（目标 ~2s） |
| U2 | 真实 NPU 上热词降级行为（adapter 真机验证） | 用户实测 | 跑 spec 给定片段：`VOICE_INPUT_ENGINE=npu venv/bin/python -c "import engine; m = engine.build_model(); segs, _ = m.transcribe('/tmp/x.wav', language='zh', initial_prompt='术语测试'); print(''.join(s.text for s in segs))"`（wav 用任意 16kHz 中文录音） | stderr 出现热词降级告警行，且 stdout 正常输出转写文本（不断流不崩）。注：Wayland 听写链路现状不传热词（transcribe_once 不加载 terms），本项验证的是 adapter 真机降级语义（X11 voice-ptt 接 npu 时同路径生效） |
| U3 | e2e 冷编译首次行为 | 用户实测 | rm -rf ~/.cache/voice-input/npu-compile-cache → 重启 service → 第一次听写 | ~47s 内完成不超时；第二次恢复 ~2s |

自动化层级说明：A1–A5 unit（engine.py 纯函数/注入缝，最低成本足够）；A6/A8 需要真子进程/真 shell（integration）；A7 是纯静态检查（diff + 源检查，字节等价由 A4 承担）。CI 无 NPU 硬件，genai 真构造/真推理只能 U1–U3 用户实测，无法自动化的原因：硬件单台且不可在 CI 复现；可执行时机：实现合并前在 OmniBook 执行。

## Testability split design（A1–A8 的实现约束）

| 拆分单元 | 形态 | 测试边界 |
|----------|------|----------|
| `required_lib_paths` / `default_model` | 纯函数（入参注入，不读 os.environ） | 引擎 × site_packages 矩阵；异常路径 |
| `ov_snapshots_base` | 纯函数 | 模型名 → 路径串 |
| `resolve_model_path(layout=…)` | 纯路径逻辑（base 注入，tmp_path 布局） | ct2 谓词现状不变 + ov 谓词 + 空/坏布局报错 |
| `_load_wav_samples` | 副作用仅文件读；numpy 懒导入 | 16-bit 校验、采样率告警、float32 归一化 |
| `_NpuWhisperAdapter.__init__` | pipeline_factory 注入缝 | 默认 kwargs 断言（A1）；不 import genai 即可测 |
| `_NpuWhisperAdapter.transcribe` | 依赖注入 fake pipeline + tmp wav | 返回形状、热词 warn、多余 kwargs 忽略 |
| transcribe_once env 收敛 | 子进程 harness（既有模式） | cuda 输出等价；npu 报错文本 |
| wrapper export 计算 | shell 子进程 + fake python | 输出行与预期逐字比对 |

实现硬约束：以上拆分不得在实现阶段重新耦合（如 transcribe 内直接读 os.environ、wrapper 内重算路径列表绕过 engine.py）。测试 helper 三副本的债继续挂账：新增子进程测试沿用 test_engine.py 现有内联模式，不造第四种写法。

## Open decisions（已裁决记录）

1. ~~接口形态：adapter vs dispatch vs Protocol~~ → adapter（用户 2026-09-29 选定）
2. ~~技术债范围~~ → 初选范围 A（preamble 收敛 + 上屏统一）；review 后发现 paste.py 的 X11 契约是 `xdotool type`，与 voice-ptt.py 现状不同，上屏统一违反 7700K 零行为变化约束 → 范围修正为仅 preamble 收敛（见 #5）
3. construction_kwargs 对 npu：改为 `raise ValueError("npu engine has no faster-whisper construction kwargs; use build_model")`（npu 不再有 fw 构造参数，ValueError 语义比 NotImplementedError 准确——它不再是"未实现"而是"不适用"）
4. README 双语：本 issue 只改英文版为主，中文版 NPU 段落同步一小节；完整双语同步仍归 #20
5. ~~上屏统一（voice-ptt.py → paste.py）~~ → 撤出本 issue（用户 2026-09-29 裁决：不能改 7700K 机器任何行为）。paste.py X11 走 `xdotool type`、voice-ptt.py 走 xsel+Ctrl+Shift+V，两者不等价；统一债继续挂账，待将来以「7700K 可接受的行为变化」为前提单独立 issue
