# hold 链路接入录音归档 + VAD 决策元数据 — 设计（issue #44）

Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-26T22:50:00+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-26T23:05:00+08:00（R1：G1/G2/G3 + D1/D2/D3）
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-26T23:16:00+08:00（R2：G1/G2 + D1/D2/D3/D4，新增 D10）

关联：#44（本 issue）、#1（原始归档 issue，已关闭）、#43（依赖本设计的样本产出）
调研：`research/topic-1-research.md`

## 1. 问题与目标

`archive.py` 只在 `voice-ptt.py`（X11 路径）生效；`voice_hold.py` 成为日常主路径后归档没跟过来，`recordings/` 目录从未创建，听写录音说完即焚。#43 排查轻声丢句时两条关键样本无法回放，只能靠日志里的时长反推。

目标：让 hold 链路（常驻 worker + legacy `transcribe_once.py`）把每次听写的**原始音频 + 转写文本 + VAD 决策依据**成对落盘，供 #43 定阈值与后续回归评测。

## 2. 决策表

| ID | 决策 | 理由 | 被否方案 |
|---|---|---|---|
| D1 | 归档执行者 = **转写进程**（`transcribe_worker.py` / `transcribe_once.py`） | 元数据天然齐全（raw/final/vad/engine/model），协议零改动，legacy 与 resident 行为一致 | 父进程归档：拿不到 raw 与 VAD 统计，需扩协议 + 给 legacy 另开通道 |
| D2 | **归档在回包前**（源 wav 所有权全程只属于 worker） | 父进程一收到响应就在 `finally` 里删源（`voice_hold.py:485-486` 的 `if os.path.exists(wav): os.unlink(wav)`，`worker_supervisor.request` 读到 text 行即返回）；「先回包」把归档推进跨进程竞态窗口，可致归档静默丢失。回包前归档时源文件无人竞争。代价是一次 ~200 KB 写 + fsync（本地 SSD 个位数毫秒，相对 0.8–1.4 s 转写可忽略） | 先回包再归档（R1-G1 竞态） |
| D3 | 落盘**原始 wav**（未裁剪） | 调阈值需要原始音频重跑 VAD；裁剪后音频可由参数重建，反之不能 | 落裁剪后音频：阈值一变历史样本就作废 |
| D4 | 元数据用**显式可选关键字参数**（`engine` / `model` / `vad`），不用自由 `dict` | 契约可测、字段可 grep；缺省时 record 与今天逐字节一致 | `extra: dict` 自由合并：字段集合无约束，容易漂 |
| D5 | **空转写也归档**（`vad.status="no_speech"`） | 「整句被裁光」正是 #43 最缺的负样本；空转写 + 无语音判定就是它的签名 | 只归档有文本的：丢掉最有诊断价值的样本 |
| D6 | 保留策略：默认**全量保留**，`VOICE_INPUT_ARCHIVE_KEEP_DAYS`（默认 0=不清理）在进程启动时清理一次 | 与既有设计文档 §2 一致；显式给出退出阀而不是默默沿用旧假设 | 默认按 30 天清理：未经用户同意删数据 |
| D7 | prune **只删目录，不动 `index.jsonl`** | 保住 append-only 不变量，避免重写索引时的并发追加丢失 | 重写 index：需要锁，收益仅是「索引干净」 |
| D8 | **worker 协议不改**（`PROTOCOL_VERSION` 保持 1） | 归档发生在转写进程内部，父进程无需知道 | 扩 `encode_text` 回传元数据：为父进程归档服务，D1 已否决该路线 |
| D9 | 失败隔离：归档异常 log 到 stderr，**绝不影响转写结果与上屏** | 与 `voice-ptt.py:272` 同语义 | 失败即报错：把数据基础设施问题升级成输入法不可用 |
| D11 | `archive_recording(..., keep_source=True)` 供 `transcribe_once.py` 使用：**永不删除调用方给的文件** | CLI 参数是用户文件；`transcribe_once.py` 现在会被手动运行/被 `test-mic.sh` 调用，删输入 = 静默数据丢失。hold legacy 分支的临时 wav 由父进程 `finally` 删除，不依赖归档 | 让 CLI 也删源：手动跑一次就毁掉一条录音 |
| D10 | 把 `transcribe_once.py` 的 **import 期副作用**（venv 站点探测 + 引擎校验 + `LD_LIBRARY_PATH` 注入）抽成 `_preamble()`，由 `main()` 调用 | 现状导致模块不可 import（无 venv 环境直接 `sys.exit(1)`）、无法单测；worker 已是这个形状（`transcribe_worker._preamble`），对齐即可 | 保持 import 期副作用 + 改用 subprocess 集成测：慢、且照样污染进程环境 |

## 3. 数据流

```
voice_hold.py::_work
  └─ supervisor.request(wav)  ──socketpair──▶  transcribe_worker.worker_main
                                                  ├─ vad.trimmed_wav(wav, stats=st)   # st 记录裁剪决策
                                                  ├─ model.transcribe(src|wav)        # 返回 raw
                                                  ├─ apply_corrections_counted(raw)   # 返回 final
                                                  ├─ archive.archive_result(wav, raw, final,
                                                  │        engine=, model=, vad=st)      # D2：回包前归档（含删源）
                                                  └─ conn.write(encode_text(final)); flush()
  ├─ (父) os.path.exists(wav) 为 False → 跳过 unlink   # voice_hold.py:484-486 现状兼容；源已被归档删除
  └─ _paste_and_report(final)
```

legacy 分支（`VOICE_INPUT_RESIDENT=0` 或 supervisor 不可用）走 `transcribe_once.py`，同一套 `trimmed_wav(stats=...)` + `archive_result(...)`，顺序同为「归档 → 打印」；`no_speech` 分支也必须归档（§4.4）。

## 4. 组件契约

### 4.1 `archive.py`

```python
ARCHIVE_DIR        # 不变
ARCHIVE_ENABLED    # 不变（VOICE_INPUT_ARCHIVE != "0"，导入时求值）
ARCHIVE_KEEP_DAYS  # 新增：VOICE_INPUT_ARCHIVE_KEEP_DAYS，非负整数，坏值/缺省 → 0

def archive_recording(wav_path, raw_text, final_text, archive_dir=ARCHIVE_DIR, *,
                      engine=None, model=None, vad=None, keep_source=False) -> str:
    """现有语义与回滚原则完全不变。
    三个新参数全为 None 时，record 的键集合与今天一致（向后兼容）。
    engine/model：仅非 None 时写入。
    keep_source=True：只拷贝、不删源（transcribe_once 用，见 D11）。
    源文件不存在时在 makedirs 之前就抛 FileNotFoundError（不留下空的 archive 根目录）。
    vad：dict（来自 vad_trim.vad_summary() + status），作为 record["vad"] 整体写入。"""

def archive_result(wav_path, raw_text, final_text, *, engine=None, model=None, vad=None,
                   archive_dir=ARCHIVE_DIR, log=None, keep_source=False) -> bool:
    """ARCHIVE_ENABLED 判定 + 异常兜底的一层壳。
    `archive_dir` 显式传 None 归一为 ARCHIVE_DIR（防止调用方把 None 直传击穿默认值）。
    返回 True=已归档 / False=未归档（开关关闭或失败）。任何异常只 log，绝不上抛。"""

def prune_recordings(archive_dir=ARCHIVE_DIR, keep_days=0, *, now=None) -> list[str]:
    """keep_days <= 0 → []（no-op）。
    now 可注入（默认 datetime.now()）。
    ├─ 扫描 archive_dir 下的直接子目录，目录名匹配 %Y-%m-%d_%H%M%S 或 %Y-%m-%d_%H%M%S_<seq>
    ├─ 解析失败或不匹配的条目跳过，绝不删非本次产物
    └─ 目录名时间戳 < now - keep_days 天 → rmtree，返回被删目录名列表
    index.jsonl 不动（D7）。"""
```

record 示例（新增字段仅在有值时出现）：

```json
{"ts": "2026-10-26T23:01:12", "dir": "2026-10-26_230112", "audio": "audio.wav",
 "raw": "zima 帮我看一下这个问题", "final": "Zima 帮我看一下这个问题", "enhanced": true,
 "engine": "npu", "model": "small-int8-ov",
 "vad": {"status": "kept", "threshold": 0.5, "speech_pad_ms": 400,
         "total_s": 6.65, "kept_s": 6.38, "kept_ratio": 0.959, "spans": 1}}
```

### 4.2 `vad_trim.py`

```python
DEFAULT_THRESHOLD = 0.5          # 新增：集中 faster_whisper VadOptions 的隐性默认值
DEFAULT_PAD_MS = 400

def default_vad_options():        # 集中默认值（现在散在 speech_spans 内联）
def vad_summary(bounds, total_samples, sample_rate=SAMPLE_RATE, options=None) -> dict:
    """纯函数：不 import faster_whisper，options 只读 .threshold / .speech_pad_ms（duck-typed）。
    返回 {"status" 由调用方补, "threshold", "speech_pad_ms",
          "total_s", "kept_s", "kept_ratio", "spans"}；sample_rate<=0 时秒数为 0。"""
def spans_from_wav(path, *, vad_options=None)   # 新增可选参数，默认 default_vad_options()

@contextmanager
def trimmed_wav(path, *, detector=None, enabled=None, log=None, stats=None)
    """stats：可选 dict，进入时 clear()，退出前原地填充：
       status ∈ {"disabled","kept","no_speech","degraded"}
       ├─ disabled   : VOICE_INPUT_VAD=0，只有 status
       ├─ kept       : 含 vad_summary 全字段（含 passthrough 即 kept>=99% 未生成临时文件）
       ├─ no_speech  : spans 为空，yield None；含 vad_summary 字段（spans=0, kept_s=0）
       └─ degraded   : 另加 "reason" ∈ {"unavailable","format","error"}
       控制流与异常语义不变（永不抛）。"""
```

`kept` 与 `no_speech` 互斥且完备：只要 detector 成功返回 spans，stats 必有 summary 字段。

### 4.3 `transcribe_worker.py`

```python
def worker_main(conn, *, model_factory=None, terms_loader=load_terms,
                clock=time.monotonic, log=None, archive_dir=None)   # 新增最后一个 kw
```

`archive_dir` 语义：`None` → 用 `archive.ARCHIVE_DIR`；非 None → 归档与 prune 都只用这个目录（测试注入用）。

请求处理顺序（D2：归档在回包前）：

```python
stats = {}
with vad_trim.trimmed_wav(wav, stats=stats) as src:
    ...  text = "".join(...)
raw = text
text, n_fixed = apply_corrections_counted(text, corrections)
archive.archive_result(wav, raw, text, engine=eng, model=model_label,
                       vad=stats, archive_dir=archive_dir or archive.ARCHIVE_DIR,
                       log=log)                                        # D2：先归档（含删源）
conn.write(encode_text(rid, text)); conn.flush()                        # 再回包
```

- **必须传请求里的 `wav`（原始未裁剪），不得传 with 块内的 `src`**（D3）。`src` 在 with 块外仍在作用域内，误传会让 A5 也绿而样本失去调阈值价值；A5 因此额外断言 `audio.wav` 与源文件逐字节一致

- `eng` / `model_label` 复用 `worker_main` 启动时已算好的 `_engine_label(engine)` / `_model_label(engine)`
- `archive_result` 自带异常兜底，不会把错误升级成 `encode_error`；归档失败时源 wav 保留，由父进程 `finally` 清理
- **prune 调用点在 `worker_main` 内、`ready` 行发出之前**（不是 `main()`/`_preamble`，否则测试 harness 覆盖不到）：
  `if archive.ARCHIVE_ENABLED: archive.prune_recordings(archive_dir or archive.ARCHIVE_DIR, keep_days=archive.ARCHIVE_KEEP_DAYS)`，
  整段 `try/except` 包住，失败只 log。`ARCHIVE_ENABLED=0` 时不做任何磁盘操作（D6/§5）

### 4.4 `transcribe_once.py`

同一套，但需要**去掉 `src is None` 的提前 return**，让 `no_speech` 与正常路径汇合（D5 在 legacy 也要成立，R1-G2 指出原写法会漏掉空转写）；同时按 D10 把 import 期副作用收进 `_preamble()`，让本模块可 import：

```python
stats = {}
with vad_trim.trimmed_wav(wav, stats=stats) as src:
    if src is None:
        text = ""                      # no_speech：仍要走归档
    else:
        segments, info = model.transcribe(src, language="zh", initial_prompt=prompt, **extra_kw)
        text = "".join(s.text for s in segments).strip()
raw = text
text = apply_corrections(text, corrections)
archive.archive_result(wav, raw, text, engine=..., model=..., vad=stats,
                       keep_source=True)          # D2 归档在前；D11 不删输入
print(text)                                       # 结果通道在后
```

`main()` 中在 `archive.ARCHIVE_ENABLED` 为真时同样调一次 `prune_recordings(keep_days=archive.ARCHIVE_KEEP_DAYS)`（短进程每次启动清一次，`keep_days=0` 时是 no-op）。

`main()` 开头先调 `_preamble()`：`_preamble()` 返回非 0 时直接 `return` 该码（行为与今日 `sys.exit(1)` 等价），否则继续。`import transcribe_once` 后**不再有** subprocess 探测与 `os.environ["LD_LIBRARY_PATH"]` 改写。

## 5. 降级与失败处理

| 情形 | 行为 | 观测量 |
|---|---|---|
| `VOICE_INPUT_ARCHIVE=0` | 零写盘：不建目录、不清理 | `archive_result` 立即返回 False |
| 归档目录不可写 / 磁盘满 | `archive_recording` 抛 → `archive_result` 捕获 | stderr `[archive] ... failed (non-fatal)`；转写照常返回 |
| index 追加失败（audio 已完整） | 现有语义：保留目录、抛异常 → 被捕获 | 同上；音频不丢 |
| VAD 不可用 / 格式不符 | `stats.status="degraded"` + reason，仍归档原始音频 | index 行 `vad.status=degraded` |
| 父进程读超时 | supervisor 立刻 `proc.kill()`（SIGKILL，`worker_supervisor.py:262-264` / `306-326`），worker 不会再执行归档；父进程的 `finally` 删源照旧 | 无归档记录；`[voice-hold]` 日志一行 |
| SIGKILL 恰好落在归档窗口内（回包前） | `archive_recording` 的回滚逻辑没有执行机会，可能留下半成品 `recordings/<ts>/`（只有部分文件、无索引行） | 接受残留：`VOICE_INPUT_ARCHIVE_KEEP_DAYS>0` 时由 prune 按龄清理；默认 0 时不清理。**这是已知保证边界，不在本次做原子 rename**（见 §9 R2） |
| 源 wav 被其它进程删掉（防御性） | `archive_recording` 抛 `FileNotFoundError` → `archive_result` 捕获并 log | stderr 一行，无副作用 |
| prune 失败（权限 / 目录不可读） | log 后继续，不影响转写 | stderr 一行 |

## 6. 非目标

- 不改 worker 协议版本（`worker_protocol.py` 的 `PROTOCOL_VERSION` 保持 1）、不改 `worker_protocol.py`
- 不加 VAD 阈值环境变量（#43 负责；本设计只如实记录当时生效值）
- 不做 SQLite / 检索 UI / 统计脚本
- 不做加密、脱敏、按内容分类
- 不改 `voice-ptt.py` 行为（它传三个位置参数，新参数全默认）
- 不把归档移出关键线程（先回包已足够；线程化留作后续）

## 7. 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|---|---|---|---|---|
| A1 | `vad_summary` 纯函数 | 自动化（unit） | `venv/bin/python3 -m unittest test_vad_trim.TestVadSummary -v` | 0 span / 全覆盖 / 部分覆盖 / sample_rate 边界均返回预期秒数与 ratio |
| A2 | `stats` 四态填充 | 自动化（unit） | `venv/bin/python3 -m unittest test_vad_trim.TestStats -v` | disabled→仅 status；kept→含 summary；no_speech→spans=0 且 yield None；degraded→带 reason |
| A3 | record 元数据字段 | 自动化（unit） | `python3 -m unittest test_archive.TestMeta -v` | 传参时 index 行含 engine/model/vad；不传时键集合与旧版一致 |
| A4 | `prune_recordings` | 自动化（unit） | `python3 -m unittest test_archive.TestPrune -v` | keep_days=0 无操作；过期目录删、新目录留；index 行数不变；坏 env 归 0 |
| A5 | worker 归档钩子 | 自动化（integration） | `python3 -m unittest test_transcribe_worker.TestArchiveHook -v` | 临时 archive_dir 出现 audio.wav/raw.txt/final.txt + index 行含 vad；且音频为**原始未裁剪** wav：`audio.wav` 与源文件字节数/内容 hash 相同，trimmed 临时路径不出现在归档目录（D3） |
| A6 | 归档先于回包 | 自动化（unit） | 同上 | 记录到的调用序列为 `archive → write` |
| A7 | 归档失败不阻断 | 自动化（unit） | 同上 | stub 抛异常时响应仍含 text，worker 存活 |
| A8 | 开关关闭零写盘 | 自动化（unit） | 同上 | archive_dir 不存在 |
| A9 | 空转写也归档（worker） | 自动化（unit） | 同上 | `src=None` 时仍产生 index 行，`vad.status=no_speech` |
| A9b | 空转写也归档（legacy） | 自动化（unit） | `python3 -m unittest test_archive.TestTranscribeOnceHook.test_no_speech_is_archived -v` | `transcribe_once` 在 no_speech 分支也落盘，stdout 仍为一行空串 |
| A10 | `transcribe_once` 归档 | 自动化（integration） | `python3 -m unittest test_archive.TestTranscribeOnceHook -v` | 临时 archive_dir 有记录，raw/final 语义与 worker 一致 |
| A11 | 全量回归 | 自动化（integration） | ① `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim -v` ② `venv/bin/python3 -m unittest test_engine -v` ③ `VOICE_INPUT_ENGINE=npu venv/bin/python3 -m unittest test_vad_trim -v` | 三条命令全绿；③ 中 `TestSpeechSpans`、`TestEndToEnd` 均执行（`TestEndToEnd` 的 `skipUnless` 要求 `VOICE_INPUT_ENGINE=npu` + repro wav + npu 依赖，故显式带 env；缺任一前置时记 pending 并说明，不得谎报为通过） |
| A12 | prune 接线 | 自动化（unit） | `python3 -m unittest test_transcribe_worker.TestArchiveHook.test_prune_runs_once_on_start -v` | `worker_main` 启动时 `prune_recordings` 被调用一次且收到 `archive_dir`/`KEEP_DAYS`；`ARCHIVE_ENABLED=0` 时不被调用 |
| A14 | `transcribe_once` 不删输入（D11） | 自动化（unit） | `python3 -m unittest test_archive.TestTranscribeOnceHook -v` | speech / no_speech 两种情况下源 wav 都仍在；`archive_recording(keep_source=True)` 单测覆盖拷贝不删源 |
| A13 | `transcribe_once` 可 import（D10） | 自动化（integration） | `python3 -m unittest test_archive.TestTranscribeOnceImport -v`（内部 `subprocess.run([sys.executable, "-c", "import transcribe_once; print('ok')"], env=<无 LD_LIBRARY_PATH>)`） | 子进程退出码 0、stdout 含 `ok`；父进程 `os.environ` 未被改写 |
| U1 | 真机端到端 | 用户实测 | ① `cd ~/work/git-repo/voice-input && git pull`（确认在含本次改动的 commit）② `systemctl --user restart voice-hold`（常驻 worker 由 daemon 启动期预热，不重启会跑旧代码）③ `journalctl --user -u voice-hold -n 20` 确认出现 `[worker] model ready` ④ 按住右 Alt 说一句中文 ⑤ 检查 `~/.local/share/voice-input/recordings/` 最新目录与 `index.jsonl` | 出现目录三件套 + 索引行含 `vad` 字段；上屏延迟无可感变化；若延迟可感则记录实测值并回退 D2 方案 |
| U2 | README 双语 | 用户实测（人工核对） | 通读 README.md 与 README.zh-CN.md 的归档章节 | 目录位置、开关、保留期、字段说明齐全且两语言一致 |

## 8. 可测性拆分设计

| 层 | 拆分出的单元 | 依赖 | 测法 |
|---|---|---|---|
| 计算 | `vad_summary(bounds, total, sr, options)` | 无（duck-typed options） | 纯函数直接断言 dict |
| 采集 | `trimmed_wav(stats=...)` | detector 注入点已存在 | 注入返回固定 spans 的 detector，断言 stats 四态 |
| 落盘 | `archive_recording(..., vad=)` | stdlib + `archive_dir` 注入 | 临时目录，读回 `index.jsonl` |
| 策略 | `prune_recordings(now=)` | stdlib + 时间注入 | 造过期/新鲜目录，断言删除集合 |
| 壳 | `archive_result(...)` | 上述；`ARCHIVE_ENABLED` 模块级可 patch | 断言 False/True 与「抛异常不外溢」 |
| 接线 | `worker_main(archive_dir=)` / `transcribe_once.main()` | worker：现有 `FakeModel`（`model_factory` 注入点）；transcribe_once：D10 后模块可 import，patch `engine.build_model` + `vad_trim.trimmed_wav` **以及 `transcribe_once._preamble` 返回 0**，再直接 `main([wav])`；进程内跑时 env 不会被 `_preamble` 污染（因为它被 patch 掉了） | socketpair harness 读回响应 + 查磁盘；transcribe_once 断言 `main()` 返回码与归档产物 |
| 时序 | 归档先于回包 | 同上 + `archive_result` stub | harness 记录事件序 `archive → write`（A6） |
| 生命周期 | prune 接线 | `prune_recordings` 可 patch | 断言启动时被调一次且收到注入的 `archive_dir`；开关关闭时不调（A12） |

关键边界：`archive_result` 是唯一允许 `try/except` 的位置，worker 循环因此不需要新增错误分支；`vad_trim` 仍保持「永不抛」的既有契约。

## 8b. 实施期发现的契约变更（记录在案）

| 项 | 原契约 | 现契约 | 影响 |
|---|---|---|---|
| `test_engine.TestTranscribeOnceEnv`（#19 A6） | 断言 **import** `transcribe_once` 就写 `LD_LIBRARY_PATH` / npu 缺路径即退出 1 | D10 后改为断言 `_preamble()` 的返回值与等价字符串（逐字节不变） | 唯一 import 方是测试；真实调用方（脚本/`check_output`）行为不变。4 个用例已同步改写 |
| `archive_recording` 遇缺失源 | 先 `makedirs`，复制失败后 rmtree 记录目录（父目录 `recordings/` 残留） | 先 `isfile` 检查即抛 | 避免空归档根目录被测试/误用制造出来 |

## 9. 风险与开放问题

| ID | 风险 | 评估 / 对策 |
|---|---|---|
| R1 | 归档 fsync 落在回包前，直接进上屏路径 | 代价是一次 ~200 KB 写 + 一次 index 追加的 fsync（本地 SSD 预期个位数毫秒，基线已 ~0.8–1.4 s）。U1 记录实测延迟；若可感则回退为「先 open 源 wav fd、回包后走 fd 归档」（`archive_recording` 需支持预开 fd，R1-G1 方案 b） |
| R1b | 归档失败时源 wav 残留 | 归档失败不删源 → 父进程 `finally` 的 `os.path.exists(wav)` 为真 → 照旧清理，无泄漏 |
| R7 | `archive_result` 内部异常被静默 | 必 log 到 stderr（与 `voice-ptt.py` 同语义）；不做重试（重试会把失败拖成长尾） |
| R2 | prune 后 index 残留指向已删目录的行 | 接受，README 写明；重写索引不在本次范围（D7） |
| R2b | SIGKILL 落在归档窗口 → 半成品 `recordings/<ts>/` 残留 | 已知边界，§5 写明；`KEEP_DAYS>0` 时被 prune 清掉。彻底方案（先写 `<ts>.partial` 再原子 rename）需改 `archive_recording` 的目录创建与 index 顺序，本次不做 |
| R3 | worker 与 ptt 重复归档同一条录音 | 不发生：ptt 路径不经 worker，hold 路径父进程不归档 |
| R4 | `transcribe_once.py` 被手动 CLI 使用时也归档 | 预期行为，README 说明 |
| R5 | 测试依赖 patch 模块级 `ARCHIVE_ENABLED` | 与现有 `test_vad_trim` patch env 的风格一致，可接受 |
| R6 | 录音含隐私内容长期留存 | 默认全量保留是用户既有选择；`KEEP_DAYS` 提供退出阀，README 写明 |

## 10. 交付切片

单个 PR（改动约 150 行 + 测试），内部两个 commit：

1. `feat(archive): VAD metadata + retention prune` — `archive.py`、`vad_trim.py`、对应测试。行为零变化（新参数全默认）。
2. `feat(archive): wire hold + transcribe_once` — 两处接线、`transcribe_once` 的 `_preamble()` 抽取（D10）、README 双语、接线测试。

理由：切片 1 单独合入无用户可见收益，而接线本身风险低（先回包 + 异常兜底），拆成两个 PR 只是多一次 CR 往返。
