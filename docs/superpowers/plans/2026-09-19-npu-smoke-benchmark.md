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
- [ ] 跑通并保存原始输出到 `bench/results-2026-09-19-cpu.txt`(此文件进 git,作为数据留档)
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
