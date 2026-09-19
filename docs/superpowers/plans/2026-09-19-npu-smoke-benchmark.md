# NPU 冒烟与基准实现计划

> **For agentic workers:** Work ONLY inside this worktree. Steps use checkbox tracking. 每 task 一个 conventional commit,不 push,禁碰 main。

**Goal:** 验证 whisper-small 在 OmniBook NPU 4 上的推理链路并产出 CPU vs NPU 延迟数据,供 #19 决策。交付 `bench/npu-bench.py` + issue 数据表。

**Spec:** `docs/superpowers/specs/2026-09-19-npu-smoke-benchmark-design.md`(已批准)

## Global Constraints

- bench 模块纯度契约:模块顶层 **不得 import openvino**(与 engine.py 同款),openvino 只在 run_bench 内懒导入——单测(A2)在无模型/无 NPU 环境可跑
- LD_LIBRARY_PATH 由脚本自设(`set_npu_library_path`,注入式纯函数),不依赖调用方环境
- 每次转写带 alarm 超时(默认 120s),TIMEOUT 是数据点不是卡死
- 模型下载进 HF 缓存(不进 git);`*.wav` 已 gitignore,基准输入用 `/tmp/a6-trimmed.wav`(不存在则先录)
- 测试用 unittest 标准库;代码风格:中文注释、`[voice-input]`/`[bench]` stderr 前缀、类型注解
- venv 是 symlink → 主仓 venv(openvino 2026.4.0 / genai 2026.4.0.0 已装)

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `bench/npu-bench.py` | 基准脚本:parse_args / set_npu_library_path / format_report 纯函数 + run_bench 集成 | 新建 |
| `bench/__init__.py` | 包标记(便于从仓库根 import bench.npu_bench 做单测) | 新建(空) |
| `test_bench.py` | 纯函数单测(仓库根,与其他 test_* 一致) | 新建 |

接口(spec 契约,签名不得偏离):

```python
def parse_args(argv: list) -> argparse.Namespace: ...   # --model-dir --device --wav --runs=3 --timeout-s=120 --max-new-tokens
def set_npu_library_path(env: dict) -> str: ...          # 注入 dict,前插 /usr/lib/x86_64-linux-gnu,返回新值
def format_report(metrics: dict) -> str: ...             # metrics dict -> 机器可读文本块
def run_bench(model_dir: str, device: str, wav: str, runs: int = 3, timeout_s: int = 120,
              max_new_tokens: int = None) -> dict: ...   # 集成:真实模型+设备,返回 metrics dict
```

---

### Task 1: `bench/npu-bench.py` 纯函数层 + `test_bench.py`(A2)

**Files:** Create `bench/__init__.py`, `bench/npu-bench.py`, `test_bench.py`

**Test:** `./venv/bin/python -m unittest test_bench -v`;纯度:子进程 `import bench.npu_bench` 后 `openvino not in sys.modules`

**Steps:**
- [x] 按接口实现四个函数;run_bench 内:懒 import openvino_genai、NPU 时 config={"STATIC_PIPELINE": True}、计时(load/first/warm)、signal.alarm 包裹每次 transcribe、文本逐字记录、TIMEOUT 捕获后继续
- [x] 单测覆盖:parse_args 默认值与必填、--device 归一化(cpu/CPU→"CPU",npu/NPU→"NPU")、set_npu_library_path 前插且保留旧值、format_report 字段齐全且含 text_first、超时字段
- [x] `--help` 无副作用;commit `feat: NPU benchmark script pure layer (#17)`

### Task 2: 模型下载(A3 前置)

**Files:** 无新文件(下载进 `~/.cache/huggingface`);记录来源与量化配置到本文件末尾

**Test:** 目录存在且含 OpenVINO IR 文件(.xml/.bin 或 GenAI 目录结构)

**Steps:**
- [x] `./venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('OpenVINO/whisper-small-int8-ov')"`(直连 hf 实测可用;失败切 HF_ENDPOINT=https://hf-mirror.com)
- [x] 记录本地路径与仓库 README 声明的量化配置
- [ ] 无 commit(无代码变更)

### Task 3: CPU 基准(A3)

**Test:** `./venv/bin/python bench/npu-bench.py --model-dir <下载路径> --device CPU --wav /tmp/a6-trimmed.wav --runs 3` → exit 0,metrics 完整

**Steps:**
- [x] 跑通并保存原始输出到 `bench/results-2026-09-19-cpu.txt`(此文件进 git,作为数据留档)
- [ ] 若 /tmp/a6-trimmed.wav 不存在:通知父级(需用户说话录音),不得用静音文件凑数
- [ ] commit `test: CPU baseline results for whisper-small int8 ov (#17)`

### Task 4: NPU 基准(A4 + A1 复核)

**Test:** `--device NPU` 同参运行 → exit 0 且 any_run_timeout=false(或如实记录 TIMEOUT/hang)

**Steps:**
- [ ] 先在 bench 上下文里复核枚举(带 env 出 NPU,不带只有 CPU)→ 记录进结果文件头
- [ ] 跑 NPU 基准,首转(编译)与热跑分开记;结果存 `bench/results-2026-09-19-npu.txt`
- [ ] 若静态管线报模型不兼容(KV cache 类错误):按 fallback 链试 int4-ov → 记录每个失败的模型/错误/耗时,全败也是有效产出(如实写)
- [ ] commit `test: NPU results for whisper-small int8 ov (#17)`

### Task 5: 数据表汇总(A5)

**Test:** 两设备 metrics 齐全(或失败记录完整)

**Steps:**
- [ ] 汇总表(含 #16 A6 的 faster-whisper 3.4s 作参考行)写入本文件 Acceptance Log
- [ ] commit `docs: acceptance log + CPU/NPU comparison table (#17)`

---

## 验收映射

| Task | 覆盖验收 |
|---|---|
| 1 | A2 |
| 2 | A3(前置) |
| 3 | A1(部分,枚举复核在 4) A3 |
| 4 | A1 A4 |
| 5 | A5 |
| (父级执行) | U1(用户读转写文本判质量) U2(用户 go/no-go) |

## Acceptance Log

(Task 5 填写)

### Task 2 记录(2026-09-19)

- 直连 huggingface.co 失败(httpx Errno 101 Network is unreachable;昨日 curl 直连可用,今日不稳),按预案切 `HF_ENDPOINT=https://hf-mirror.com` 成功。
- 本地路径: `~/.cache/huggingface/hub/models--OpenVINO--whisper-small-int8-ov/snapshots/5b831719e093f86e1970be663e524fe001489f9b`
- 布局: encoder 92MB + decoder 154MB(INT8)+ tokenizer/detokenizer + preprocessor/tokenizer 配置,共 245MB。`openvino_config.json` 声明: optimum 2.1.0 / transformers 4.57.6 / NNCF default int8。
- **风险记录: 该导出无 `openvino_decoder_with_past_model.*`(KV cache 版 decoder)——openvino.genai#1728 指出 NPU 静态管线需要之;CPU 应不受影响,NPU 若拒绝按 fallback 链走。**

### Task 4/5 记录(2026-09-19,NPU 被驱动版本阻断——如实记录)

- 枚举复核过(带 env 出 NPU);设备属性可读(AI Boost / 33GB / driver 2026-04)。
- pip wheel 缺 NPU 编译器三件套 → 从官方 ubuntu22 同 build 归档提取注入 venv(可复现,见 results 文件)。
- 平台名实证: Lunar Lake = **NPU4000**(驱动 v1.38 固件标 NPU40xx;NPU5010/5020 也合法但非本机)。
- 正确平台名下**编译成功、执行失败**("No available devices",ze 图导入层),OV 2026.3/2026.4 两代一致
  → 判定: 驱动 1.32.1(2026-04)过旧,升级到 v1.38.0(2026-09-10,官方 LNL 验证)是解锁项。
- 模型 fallback 未启动: identity 模型同败,失败与模型无关。
- CPU 复核(2026.4 恢复后): mean 1.747s,与首测 1.768s 一致。

## Acceptance Log

| ID | 结果 | 证据 |
|----|------|------|
| A1 | ✅ | bench 上下文复核: 带 LD_LIBRARY_PATH=['CPU','NPU'],不带=['CPU'](results-npu.txt 头部) |
| A2 | ✅ | `test_bench` 14 tests OK(含纯度: 模块加载不拉 openvino;--help/缺 wav 退出码) |
| A3 | ✅ | 模型经 hf-mirror 下载(直连 Errno 101);CPU 管线过: load 1.53s / first 1.98s / warm mean 1.75s,文本非空(results-cpu.txt) |
| A4 | ⚠️ 如实记录为"被阻断" | NPU 执行 8 种组合全部失败于 ze 图导入层(编译 OK),证据链与判定见 results-npu.txt;按 spec 失败路径规则,这是有效产出(blocker finding) |
| A5 | ✅(单侧+阻断侧) | CPU 数字齐全;NPU 侧为完整失败矩阵;汇总表见下 |
| U1 | pending | NPU 无转写文本可比对(被 A4 阻断);CPU 文本: "今天测试语音输入引擎重构HoloWord这个是CPU引擎的验收"(参考 #16 A6 质量线) |
| U2 | pending | 用户决策: 建议路径 = 升级驱动 v1.38.0 后重试(需 sudo+模块重载/重启,系统级变更待确认) |

## CPU vs NPU 汇总(8.05s 中文语音输入)

| 引擎/设备 | 模型 | 加载 | 首转 | 热跑均值 | 文本 |
|---|---|---|---|---|---|
| faster-whisper CPU int8 (#16 A6 参考) | Systran small | ~0(预载) | 3.4s(单次总) | — | 今天测试语音输入引擎重购,Hello World,这个是CPU引擎的验收。 |
| ov-genai CPU (#17) | OpenVINO small-int8-ov | 1.53s | 1.98s | **1.75s** | 今天测试语音输入引擎重构HoloWord这个是CPU引擎的验收 |
| ov-genai NPU (#17) | 同上 | — | — | **被驱动阻断** | — |


### 终版(驱动 1.38.0 后,A4 翻盘 ✅)

驱动升级(本地重打包 AUR→1.38.0)+ 模块重载后,NPU 全链路通:
- A1 ✅(重验,含 re-exec 机制)/ A2 ✅ 18 tests / A3 ✅ CPU 1.74s / A4 ✅ **NPU 0.29s** / A5 ✅ 表见 results 文件
- 新增脚本能力: --npu-platform(驱动仍不报平台)、needs_reexec_for_npu(进程内改 env 对 ld.so 无效的兜底)
- U1: NPU 文本与 CPU 逐字一致(待用户确认即闭环) / U2: 数据强烈支持 #19 GO(待用户拍板)

### 本地 CR 修正轮(pi workflow code-review,30 agents,2026-09-19 22:27)

10 条 CONFIRMED:修 9(1/2/3/4/5/6/7/8/9),延后 1(10,同 #19 已挂的收敛债)。

| # | 发现 | 处置 |
|---|------|------|
| 1 | SIGALRM 打不断阻塞中的 C++ generate(),--timeout-s 防挂承诺不成立;且超时后伪造 first_transcribe_s、丢弃迟到结果 | ✅ 重设计:alarm 只置标志(结果/耗时永远真实,超预算如实标记);真死锁由 main() 的 fork 看门狗 SIGKILL 兑底(fork 在 openvino 导入前,无 NPU fork 风险;父进程 alarm 处理器抛异常打断 waitpid——PEP 475 会重试 no-op 打断的调用)。双端实测过(CPU 1.77s / NPU 0.30s,静态编译在子进程内完成) |
| 2+9 | startswith 与 split(':') 谓词发散,兄弟目录致 re-exec 空转 | ✅ 收敛为共享谓词 _lib_dir_present(分量精确匹配),两处共用;新增回归测试 |
| 3 | re-exec 用宿主 sys.argv,编程调用会 exec 无关工具 | ✅ reexec_command(argv, script_path) 纯函数,显式传参;新增单测 |
| 4 | _load_wav 漏检采样宽度,24-bit 被静默误读成垃圾数据 | ✅ getsampwidth() != 2 硬拒绝(ValueError→exit 3);新增单测(造 24-bit wav 断言拒绝) |
| 5-8 | README 双语:测试命令漏 test_bench、文件表缺 bench/ 条目 | ✅ 两文件四处补齐 |
| 10 | prepend 库路径模式第 6 份拷贝,已漂移 | 📝 延后 #19(engine.py 收敛,与 #16 CR 发现 8 同主题) |

修正后:22 bench tests + 全套件绿;CPU/NPU 双端经看门狗路径实跑一致。
