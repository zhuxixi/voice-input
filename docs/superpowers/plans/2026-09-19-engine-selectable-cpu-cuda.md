# 引擎可选（VOICE_INPUT_ENGINE=cpu|cuda）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work ONLY inside this worktree; never touch the main checkout.

**Goal:** 转写引擎与模型名可用环境变量选择（`VOICE_INPUT_ENGINE`=cuda|cpu|auto|npu占位, 默认 cuda;`VOICE_INPUT_MODEL` 默认 large-v3）,默认路径与 HEAD `40f35bb` 字节级等价（7700K 零配置不变）。

**Architecture:** 新建 `engine.py`（单一事实源,懒导入,无 GTK）+ `transcribe_once.py`（CLI 转写入口,收敛 shell 内嵌片段）;`voice-ptt.py` 只换模型加载来源;`download-model.sh` 按模型配清单 + `curl -f` 原子写 + engine 感知验证。

**Tech Stack:** Python 3.12（venv,经 symlink `worktree/venv` → 主仓 venv）,faster-whisper 1.2.1,unittest 标准库,bash。

**Spec:** `docs/superpowers/specs/2026-09-19-engine-selectable-cpu-cuda-design.md`(已批准)

## Global Constraints

- **保护文件零改动**（Task 3/6 验证 `git diff main --stat` 为空）: `terms.py` / `archive.py` / `media_pause.py` / `voice-ptt.sh`
- **LD_LIBRARY_PATH 逻辑零改动**: voice-ptt.py 模块头 preamble、所有 shell 的 export 原样保留
- **转写调用点零改动**: `voice-ptt.py` 的 `m.transcribe(WAVFILE, language="zh", initial_prompt=prompt, **extra_kw)` 一字不动
- cuda 分支构造参数 = HEAD 原文逐字搬移: `WhisperModel(path, device="cuda", compute_type="float16")`
- 测试用 unittest 标准库（`python -m unittest`）,不引入 pytest
- 代码风格与现有模块一致: 中文注释、stderr 前缀 `[voice-input]`、类型注解、`transcribe_once.py` 头部含 voice-ptt.py 同款 venv/LD_LIBRARY_PATH preamble
- 每个 Task 一个 conventional commit（`feat:`/`refactor:`/`test:`/`fix:`）,**不 push**;main 分支禁碰
- 本 worktree 的 `venv` 是指向主仓 venv 的 symlink（.git/info/exclude 已排除）,测试直接用 `./venv/bin/python`

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `engine.py` | 引擎选择 + 模型路径解析 + 模型构造分发。纯 stdlib,faster_whisper 懒导入 | 新建 |
| `test_engine.py` | engine.py 单测（A1–A5,无 GPU/无模型/无 GTK） | 新建 |
| `transcribe_once.py` | `python transcribe_once.py <wav>` → 打印转写文本 | 新建 |
| `voice-ptt.py` | 删本地 `SNAPSHOTS_DIR`/`_resolve_model_path`,`load_model` 改为 `engine.build_model()` 薄包装;启动错误捕获扩到 ValueError | 改 |
| `test-mic.sh` / `voice-toggle.sh` | 内嵌 python 片段 + 快照循环 → 调用 `transcribe_once.py` | 改 |
| `download-model.sh` | 按模型配 FILES、`curl -fL` + `.part` 原子写、`$(( ))` 替代 bc、验证走 engine | 改 |

`engine.py` 精确接口（Task 依赖这些签名）:

```python
VALID_ENGINES = ("cuda", "cpu", "auto", "npu")
def model_name(env: dict) -> str: ...      # VOICE_INPUT_MODEL → 默认 "large-v3"
def engine_name(env: dict) -> str:         # VOICE_INPUT_ENGINE → 默认 "cuda"
    # 非法值 raise ValueError(列出合法值)
def resolve_model_path(model: str = None, base: str = None) -> str:
    # base 默认 ~/.cache/huggingface/hub/models--Systran--faster-whisper-{model}/snapshots
    # sorted 扫描含 model.bin 的子目录;找不到 raise RuntimeError(提示 ./download-model.sh <model>)
def build_model(engine: str = None, model: str = None,
                whisper_factory=None, warn=None) -> "WhisperModel":
    # whisper_factory 未注入时懒 import faster_whisper.WhisperModel
    # cuda: factory(path, device="cuda", compute_type="float16")   [HEAD 逐字搬移]
    # cpu:  factory(path, device="cpu",  compute_type="int8")
    # auto: 先 cuda,Exception → warn 到 stderr 再 cpu int8
    # npu:  NotImplementedError("NPU engine not implemented yet — see issue #19")
```

---

### Task 1: `engine.py` + `test_engine.py`（A1–A5）

**Files:**
- Create: `engine.py`, `test_engine.py`

**Test:** `./venv/bin/python -m unittest test_engine -v`

**Steps:**
- [x] 按上述接口实现 `engine.py`（四个函数 + 常量;模块级零副作用、零 faster_whisper import）
- [x] `test_engine.py` 覆盖:
  - A1 默认契约: `engine_name({})`=="cuda"、`model_name({})`=="large-v3"、默认 base 目录名 == `models--Systran--faster-whisper-large-v3/snapshots`（与 HEAD 常量字符串逐字比对）
  - A2 路径泛化: tmpdir 造 large-v3/small 两种快照布局 → 正确目录;无 model.bin → RuntimeError 且信息含 `download-model.sh` 与模型名
  - A3 校验: 未知 engine → ValueError 信息列出四个合法值;`npu` → NotImplementedError
  - A4 构造契约: 注入 stub factory,断言 cuda→`(path, device="cuda", compute_type="float16")`、cpu→`(path, device="cpu", compute_type="int8")`
  - A5 auto 降级: stub factory 对 cuda 抛 Exception → warn 被调用、cpu int8 被构造
- [x] 单测全绿;`git add engine.py test_engine.py && git commit -m "feat: engine module with VOICE_INPUT_ENGINE/MODEL selection (#16)"`

### Task 2: `transcribe_once.py` CLI（A6）

**Files:**
- Create: `transcribe_once.py`

**Test:** 录一段真实说话 wav → `VOICE_INPUT_ENGINE=cpu VOICE_INPUT_MODEL=small ./venv/bin/python transcribe_once.py <wav>` → exit 0、stdout 非空中文

**Steps:**
- [x] 头部 preamble 与 voice-ptt.py 同源（_REPO_DIR/venv/_SITE/LD_LIBRARY_PATH 六行块）
- [x] `main()`: argv[1]=wav;`engine.build_model()` + `transcribe(wav, language="zh")`;join/strip 同 voice-ptt.py;print(text);异常 → stderr + exit 1
- [x] 实测（OmniBook,需用户配合说一句话,或用 #15 归档外的临时录音）;`git commit -m "feat: transcribe_once CLI entry point (#16)"`

### Task 3: `voice-ptt.py` 接入 engine（A9 前半）

**Files:**
- Modify: `voice-ptt.py`

**Test:** `./venv/bin/python -m py_compile voice-ptt.py`;`git diff main --stat -- terms.py archive.py media_pause.py voice-ptt.sh` 为空;全套件 `python3 -m unittest test_terms test_archive test_media_pause test_engine` 绿

**Steps:**
- [x] 删 `SNAPSHOTS_DIR` 与 `_resolve_model_path`;`import engine`（与 archive/terms 同区块）
- [x] `load_model()` 体 → `engine.build_model()`（global model 缓存与 preload 流程不动）
- [x] `main()` 启动错误捕获 `except RuntimeError` 扩为 `except (RuntimeError, ValueError)`（非法 env 干净退出而非裸 traceback）
- [x] 其余一切不动（preamble/GUI/转写调用点/归档/媒体暂停）;commit `refactor: voice-ptt uses engine module for model loading (#16)`

### Task 4: shell 包装收敛（A7）

**Files:**
- Modify: `test-mic.sh`, `voice-toggle.sh`

**Test:** `bash -n` 两脚本通过;grep 断言: 无内嵌 `WhisperModel(`、含 `transcribe_once.py`、保留 `LD_LIBRARY_PATH` export

**Steps:**
- [x] 两脚本删快照目录循环 + 内嵌 python 片段 → `TEXT=$("$VENV/bin/python3" "$REPO_DIR/transcribe_once.py" "$WAV")`(test-mic 保留原打印措辞;toggle 保留 xdotool type 与 notify-send——那是 #18 范围)
- [x] commit `refactor: test-mic/voice-toggle use transcribe_once entry (#16)`

### Task 5: `download-model.sh` 加固（A8 + U2）

**Files:**
- Modify: `download-model.sh`

**Test:** A8 grep: `curl -f`、`-C -` + HEAD content-length、per-model case、无 `bc`、verify 走 engine;U2 隔离实跑: `HOME=$(mktemp -d) ./download-model.sh small`（真 venv 验证需 VOICE_INPUT_ENGINE=cpu）→ 4 文件落地、无 "Entry not found"、验证步骤过;**重跑应跳过已完整的 model.bin（HEAD 对账）**

**Steps:**
- [x] `case "$MODEL"` 配清单: large-v3=今日 5 文件;small=`config.json tokenizer.json vocabulary.txt model.bin`;未知 → 报错列出支持名
- [x] `download_file`: `curl -fL ... -o "$filepath.part"` 成功才 `mv`;失败 rm .part 并 FAILED
- [x] 大小显示 `$((size/1048576))` MB;verify 改 `"$VENV/bin/python3" -c "import sys; sys.path.insert(0, '$REPO_DIR'); import engine; engine.build_model(model='$MODEL'); print('model load OK')"`（U2 实跑发现:验证必须显式传本次下载的模型名,否则隔离 HOME 下按 env 默认找 large-v3 必失败）
- [x] U2 隔离实跑（mktemp HOME,不碰真实缓存）;commit `fix: download-model validates HTTP status, per-model file lists (#16)`

### Task 6: 回归 + 验收对账（A9 全量）

**Test:** 全套件绿;`git diff main --stat` 仅含预期 7 文件;保护文件零 diff;A1–A8 结果汇总;U1 标 pending

**Steps:**
- [x] `./venv/bin/python -m unittest test_terms test_archive test_media_pause test_engine -v` 全绿
- [x] `git diff main --stat` 逐文件核对;`git diff main -- terms.py archive.py media_pause.py voice-ptt.sh` 空
- [x] 验收矩阵逐项对账（A1–A8 实际命令与结果、U1 pending、U2 已跑）写入本文件末尾 "Acceptance Log";commit `test: acceptance log for #16`

---

## 验收映射（plan task ↔ spec 验收 ID）

| Task | 覆盖验收 |
|---|---|
| 1 | A1 A2 A3 A4 A5 |
| 2 | A6 |
| 3 | A9（保护文件部分）|
| 4 | A7 |
| 5 | A8 U2 |
| 6 | A9（全量）+ 对账（U1 用户实测,pending 允许）|

## Acceptance Log

执行环境: OmniBook worktree `issue-16-engine-selectable-cpu-cuda`,CPython 3.12.14,faster-whisper 1.2.1。

| ID | 结果 | 实际命令与输出摘要 |
|----|------|--------------------|
| A1 | ✅ | `./venv/bin/python -m unittest test_engine -v` 19 tests OK;默认契约:engine_name({})=="cuda"、model_name({})=="large-v3"、snapshots_base("large-v3") 与 HEAD 常量逐字相等 |
| A2 | ✅ | tmpdir 布局:downloaded/哈希目录均能命中;无 model.bin → RuntimeError 含 "download-model.sh" 与模型名 |
| A3 | ✅ | 未知值 "gpu" → ValueError 列出全部四值;npu → NotImplementedError 含 "#19" |
| A4 | ✅ | stub 工厂断言:cuda→(path, device="cuda", compute_type="float16")(逐字契约)、cpu→(path, device="cpu", compute_type="int8") |
| A5 | ✅ | cuda 抛错 → warn 恰一次(含 auto/cuda 字样)→ cpu int8 构造成功;cuda 成功时零降级零告警 |
| A6 | ✅(管线级) | `VOICE_INPUT_ENGINE=cpu VOICE_INPUT_MODEL=small ./venv/bin/python transcribe_once.py /tmp/a16-ambient.wav` → exit 0,非空输出「字幕by索兰娅」(无人声噪声的经典幻觉,预期);错误路径:缺失 wav → exit 1 + stderr;非法 engine → exit 1 + 列合法值。**真人说话验证 pending 用户**(spec 允许) |
| A7 | ✅ | `bash -n` 两脚本过;grep:两脚本内嵌 `WhisperModel(` 计数 0、均含 transcribe_once.py 引用、均保留 `export LD_LIBRARY_PATH`;voice-toggle.sh 保留 `xdotool type` |
| A8 | ✅ | grep: `curl -fL`(L51)、`.part` 原子改名(L51-54)、`case "$MODEL"` per-model 清单(L18)、bc 引用 0、verify 走 `import engine`(L88)。行为验证:small 仓库缺失的 preprocessor_config.json → curl exit 22、零残留文件(旧版会存 15B 404 body 伪装成功);未知模型 medium → 报错 exit 1 |
| A9 | ✅ | 全套 51 tests OK;`git diff main --stat` 仅含预期 7 个代码/文档文件;`git diff main -- terms.py archive.py media_pause.py voice-ptt.sh` 为空;voice-ptt.py 的转写调用点与 LD_LIBRARY_PATH preamble 零改动(diff 逐行核对) |
| U1 | ⏳ pending | 需在 7700K(CUDA)机器实跑:无 env 启动 ./voice-ptt.sh → cuda float16 large-v3 预加载 + 按住转写知常。下次物理接触该机器时执行 |
| U2 | ✅ | 隔离实跑 `HOME=$(mktemp -d) VOICE_INPUT_ENGINE=cpu ./download-model.sh small` → exit 0,恰 4 文件(config.json/tokenizer.json/vocabulary.txt 459861B/model.bin 461MB),无 "Entry not found" body,cpu 验证步骤「模型加载成功」。**发现并修复**:verify 需显式传 `model='$MODEL'`,否则隔离 HOME 下按 env 默认找 large-v3 必失败 |

模块纯度附加验证: 子进程 `import engine` 后 `faster_whisper not in sys.modules`(无 GPU 机器可跑单测的契约)✅

### CR 修正轮(2026-09-19,pi workflow code-review,28 agents)

CR 原始发现 10 条(合并重复后 8 条独立):修复 5(1/2/3/4/6),记录延后 3(5/7/8 → #19/#20)。

| 发现 | 处置 | 修复内容与验证 |
|------|------|----------------|
| 1 voice-toggle.sh 预检丢失(CONFIRMED) | ✅ 已修 | 第一次按键分支恢复毫秒级预检(engine_name + resolve_model_path + npu 占位,notify-send 弹可行动错误并拒绝录音);transcribe 调用去掉 `2>/dev/null` 不再吞 stderr。`bash -n` 过 |
| 2 download-model.sh 断点续传被毁(CONFIRMED) | ✅ 已修 | 弃 `.part`+失败即删方案,改 `-C -` 直写目标文件(跨运行续传保留)+ HEAD Content-Length 完整性对账(已完整的 model.bin 跳过,顺带修复旧代码整包重下 3GB 的存量回归)。U2 重跑:首跑 4 文件干净落地,重跑 model.bin 显示「已完整: 461 MB」跳过 ✅ |
| 3 test-mic.sh 录音后才报错(CONFIRMED) | ✅ 已修 | 录音提示前加同款毫秒级预检,新装机器不再先说 5 秒话才看到模型缺失错误。`bash -n` 过 |
| 4 download-model.py 第 4 处硬编码(CONFIRMED,存量漏网) | ✅ 已修 | 构造参数收敛到新增的 `engine.construction_kwargs()`(单一定义点,build_model 与之共用);npu 干净报错;顺带修 #11 之前的过时提示路径。`py_compile` 过 |
| 5 verify 目标歧义(PLAUSIBLE·低) | 📝 不修 | 验证加载的恰是运行时会加载的目录(sorted-first 同逻辑),相对 main 无用户可见回归;记录于 #19 |
| 6 engine.py 校验三处重复(CONFIRMED) | ✅ 已修 | 收敛为 `_validated_engine()` 单点 + `construction_kwargs()` 单点;删除不可达的尾部 raise。新增 5 个单测,56 tests OK |
| 7 DEST 与 snapshots_base 双写(PLAUSIBLE) | 📝 延后 | 当前逐字节一致无现实故障;#19/#20 统一收敛(记录于 #19) |
| 8 preamble 第 5 份拷贝(PLAUSIBLE) | 📝 延后 | spec 明确接受本次复制;#19 追加 NPU 路径时统一收敛到 engine 可调用函数(记录于 #19) |

修正后回归: 56 tests OK(`test_terms test_archive test_media_pause test_engine`);保护文件仍零 diff;U2 隔离重跑全过(含 skip 逻辑)。

### Zima CR Round 1（PR #21，2026-09-19 18:08，pi-cr-meta）

0 blocking / 3 low advisory，全部当场修复（增量轮验证中）：

| # | Advisory | 处置 |
|---|----------|------|
| 1 | voice-ptt.py main() 不捕获 npu 占位的 NotImplementedError，GUI 入口裸 traceback，与其他三处入口不一致 | ✅ except 扩为 (RuntimeError, ValueError, NotImplementedError) |
| 2 | download-model.py 非法 VOICE_INPUT_ENGINE 值裸 traceback | ✅ try/except ValueError → stderr + exit 1，与其他入口对齐 |
| 3 | download-model.sh 非model.bin 文件「已存在即跳过」不校验完整性，#15 遗留 15B 404 假文件会被永久跳过 | ✅ HEAD 完整性对账推广到所有文件：匹配→跳过；小文件不符→删掉重下；model.bin 不符→保留断点续传；HEAD 失败→退回旧规则。场景实测：预置 15B 假 config.json + 完整 model.bin → 假文件重下(2370B 真内容)、model.bin 跳过(461MB)、cpu 验证过 |

修复后回归：56 tests OK；py_compile/bash -n 过；自愈场景实测过。
