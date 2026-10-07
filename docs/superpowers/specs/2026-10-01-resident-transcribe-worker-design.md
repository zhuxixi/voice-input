# #25 常驻转写 worker — 设计

- issue：`zhuxixi/voice-input#25`（umbrella #14 后续；接续 #19 NPU 引擎接入）
- 基线：`main @ 93f39f1`
- 状态：**复核收敛（第 3 轮：真缺口 0，细化 1 已改）— 待 ⏸ design gate 用户确认**
- 流程：github-issue-driven 步 3→4；调研 R1–R3 在 `~/.claude/github-issue-driven/zhuxixi/voice-input/issue-25/research/`
- Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-01T12:52:00+08:00
- Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-01T13:47:00+08:00（第 2 轮处置 7/7：R2-G1–G2 接受并改，R2-D1–D5 接受并改）
- Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-01T13:51:00+08:00（第 3 轮收敛收尾：R3-D1 接受并改，仅动头部元数据；本版本即 design gate 版本）

---

## 1. 背景与实测基线

hold 链路（Wayland）每次听写都 spawn 一个新的转写子进程（`voice_hold._deliver` → `transcribe_once.py`），
于是「进程启动 + venv site 探测 + `import openvino_genai` + NPU 管线构造 + 新进程首转」这套固定成本**每次都重付**。

实测（本机 OmniBook，NPU `small-int8-ov`，编译缓存热，8s wav，2026-10-01，三次取样）：

| 测量项 | 实测 |
|---|---|
| 每次听写全路径 `transcribe_once.py <wav>` | 3.60 / 3.64 / 3.60s（median 3.636s） |
| ├ venv site 探测（子进程） | 0.015s |
| ├ `import openvino_genai` | 0.332s |
| ├ `build_model()`（**与音频长度无关**） | 0.71–0.76s |
| └ 新进程首次 `transcribe()` 相对同进程稳态的额外开销 | ~0.4s |
| 常驻原型：spawn → `ready`（含 import + 构造） | 0.71s（一次性） |
| 常驻原型：请求往返（含解码，IPC 在内） | 2.76 / 2.30 / 2.23s（IPC 0.01–0.09s） |
| 模型常驻 RSS | 1.07–1.10 GB |

**结论**：每次听写给的是**与说话时长无关的固定成本 ≈ 1.1–1.45s**；同一 wav 上 3.64s → 2.23s，实测省 ~1.4s
（issue 正文的「~0.8s 加载」偏保守）。真机语义：`0.3s（settle sleep）+ 固定成本 + decode + ~0.2s（paste）`。

**必须显式声明的现实**：常驻消不掉 decode 本身。#32（`small-int8-ov` 解码病态）在原型里被复现——
同一 wav 带热词 prompt 时稳定吐 304 token 的「同时 同时 …」。因此 issue 的「松手到上屏 ≤1s」
只在**无 #32 病态的短句**上成立；长句/病态输入按实测如实记录。

## 2. 目标与非目标

**目标（G）**

- G1：hold 链路的转写模型**常驻**在一个独立子进程里，跨听写复用；单次听写不再付「探测 + import + 构造 + 首转」固定成本。
- G2：**保住 #18 的故障隔离语义**——worker 挂死 → 超时 SIGKILL + 本次听写失败；`voice_hold` 主进程存活、热键不失效；下一次听写自动重建 worker。
- G3：worker 启动失败（模型缺失/引擎配置错/venv 坏）走**可行动报错**路径（stderr → journal），且修好后**无需重启 daemon**即可自愈。
- G4：可回退与可对比——`VOICE_INPUT_RESIDENT=0` 退回今天的 per-dictation spawn；默认常驻。
- G5：无孤儿、无模型泄漏——worker 在协议通道 EOF（父进程消失）时自退；supervisor 退出时显式 kill；systemd cgroup 兜底。
- G6：热词即时生效等既有行为**逐条等价**（§4.5 等价性清单），并给 A/B 实测提供可复现证据。

**非目标（NG）**

- NG1：不把模型搬进 `voice_hold` 主进程（issue 明确排除；隔离归零）。
- NG2：不动 X11 链路（`voice-ptt.py` 本来就常驻）与 `transcribe_once.py` CLI（test-mic / voice-toggle / 手工调试依赖）。
- NG3：不处理 #32（解码病态）、#33（词表容量）、#24（浮层/转写中指示）——与本 issue 正交。
- NG4：不改 `_deliver` 的 `time.sleep(0.3)` settle（避免混淆 A/B 归因；它是常驻之外另一个 ~0.3s 收益，建议另立 issue）。
- NG5：不做「按需释放模型」（idle 卸载再加载）——issue 已表态常驻占用可接受。
- NG6：不新增依赖（协议只用 stdlib 的 `socket`/`json`）、不改 `voice_hold.py` 的顶层纯标准库契约。
- NG7：不做请求级并发/队列（busy-drop 语义下同时最多一个在飞请求）。

## 3. 设计决策

| ID | 决策 | 理由 | 否决的备选 |
|---|---|---|---|
| D1 | 进程模型：`voice_hold`（supervisor）+ **独立 worker 子进程**；worker 用 venv python 跑新脚本 `transcribe_worker.py` | 保住 #18 隔离语义：原生推理栈崩/挂只死 worker；模型常驻成本落在 worker 的 RSS 上 | 模型进主进程（崩了连热键一起死）；独立 systemd 转写服务（两 unit 的排序/权限/重启重新设计，收益仅「重启 daemon 不重载模型」） |
| D2 | 传输：`socket.socketpair()` 一端经 `Popen(pass_fds=…)` 交给 worker（`--protocol-fd N`），**行分隔 JSON** | 协议独占 fd，库噪音只能去 stderr（journal）；**EOF 天然等价「对端已死」**，不踩 #29 的「PIPE 等 EOF 假挂死」；stdlib | stdin/stdout PIPE（协议与库噪音共用 stdout；若把 stdout 重定向到 stderr 则失去 EOF 判死）；`/tmp` 下 Unix socket 文件（多 socket 文件生命周期/权限/陈旧残留三件事） |
| D3 | 协议模块独立成 `worker_protocol.py`（纯函数，两侧共用） | 编解码是两侧唯一共享的契约，纯函数可单测；避免「worker 反向 import supervisor」的怪依赖 | 编解码塞进 supervisor 模块（worker 侧 import 名不副实）；两侧各写一份（契约漂移） |
| D4 | **启动后台预热**：`run()` 起一条 daemon 线程 `prewarm()`，worker 在守护进程启动时就把模型加载好；evdev 监听不被阻塞 | 冷编译 155s / 热构造 0.7s 都挪到启动期付；第一次听写不再等加载 | 首次听写懒加载（首按要等 0.7–155s，用户体验差） |
| D5 | worker 死亡**惰性检测**（下次请求时重建）+ 每次听写**至多一次** spawn 尝试 + 最小重启间隔 `WORKER_RESTART_MIN_INTERVAL`(1.0s)；被节流挡下的请求以 `"throttled"` 明示失败 | 不引入监控线程；避免「模型坏 → 每次按键都 spawn」雪崩；后台预热失败后下一次听写自动重试 → 修好模型即自愈；节流失败必须可诊断，不得静默成功 | 常驻监控线程/定时器（多一套生命周期）；无节流重试；节流后静默返回空文本（分不清「没识别到」与「没重试」） |
| D6 | 超时**复用** `VOICE_INPUT_TRANSCRIBE_TIMEOUT`（默认 120，本机 drop-in 240），不新增旋钮 | 今天的语义就是「一次听写的 spawn 全程（含冷编译）不得超时」；常驻后同一语义覆盖「等 ready + 请求往返」 | 新增独立旋钮（配置面膨胀、语义分叉） |
| D7 | 请求走 `id` 关联；超时/EOF/协议破坏 → **SIGKILL worker** + 本次听写失败；worker 回 `error` 但进程存活时**不杀** | 挂死必须杀（否则状态不可信）；可恢复的转写异常（坏 wav/异常）不该付一次重建成本 | 一律杀（重建 0.7s 白付）；一律不杀（挂死会传染到下次请求） |
| D8 | 逃生开关 `VOICE_INPUT_RESIDENT`（默认 `"1"`，**恰好 `"0"` 才关**，与 `VOICE_INPUT_ARCHIVE`/`VOICE_INPUT_PAUSE_MEDIA` 同款解析） | NPU 占用异常时的手动降级；也是 A/B 实测与归因的前提 | 无开关（出问题只能改代码） |
| D9 | worker 侧**每请求重读 `terms.json`** 并组装 prompt | 保持「编辑热词即时生效，无需重启」的现状语义 | 启动时缓存 terms（热词编辑要重启才生效——行为回归） |
| D10 | worker 的 stderr / stdout **继承**（→ journal），协议通道独占；worker 日志不写协议 | 与今天 `stderr=sys.stderr` 一致，报错落在 journal 里可诊断；库噪音污染不了协议 | 父进程捕获 stderr 转发（额外管道 + #29 同族风险） |
| D11 | 孤儿三层防护：① worker 读协议 EOF 即退（已实测自退）② supervisor `shutdown()` 显式 kill ③ systemd `KillMode=control-group`（默认）兜底 | 1.1GB 模型进程不能残留占着 NPU；三层里任何一层失效都不至于泄漏 | 只靠 systemd（手工 `python3 voice_hold.py` 场景会泄漏） |
| D12 | 新增 `bench/hold-latency.py`：同一 wav 上分别测「spawn 路径」与「常驻路径」，输出中位/极值 | issue 明确要求「端到端对比实测，数据回贴」；可复现的证据比一次性手测强 | 只靠手测记录（不可复现、不可回归） |
| D13 | **两行**延迟日志：`stop_recording` 打 release 标记行；`_deliver` 打 `transcribed in X.XXs (worker\|spawn)`（转写段耗时） | 让「松手→上屏」可在真机从 journal 复算（issue 的核心验收数字必须可审计）；两个窗口分开口径：**transcribe 窗口** = `request()` 调用耗时（不含 0.3s settle），**end-to-end 窗口** = release 行 → delivered 行 | 无日志（真机验收只能靠手感）；把时长塞进既有 `delivered:` 行（破坏 test_hold.py:200/214 的现有断言，且两个窗口混成一个） |
| D14 | 不重构 `engine.py` 的进程前导（venv site 探测 / `LD_LIBRARY_PATH` 前置），worker 自己用现有 engine helper 组装一份（**镜像 `transcribe_once.py`：对全部引擎算路径**，见 §4.4） | 7700K cuda/X11 路径是本仓的硬约束（历史 spec 钉过字节等价），本 issue 不值得为 10 行去动它；重复债务记进 §7 | 顺手抽 `engine.prepare_process_env()` 并改两个调用方（扩大 blast radius，收益与本 issue 无关） |
| D15 | 启动协调：**单一 in-flight `_Startup`**（proc/conn + `ready_event` + error + owner），锁只保护状态读改写的短临界区；阻塞读不持锁；owner 独占读 ready 行，加入者用自己的预算等 event | 预热（最长 600s）与请求（120/240s）并发时双方各有 deadline，谁都不被对方拖死；同一 socket 只能有一个读者，event 是唯一安全的交接口令；加入者超时只失败自己这次听写、不杀仍在编译的 worker（保住 155s 冷编译） | 一把大锁串行化整个等待（U3 场景下请求被拖满 600s，违反 D6/G2）；每方各自 spawn（两个 worker 抢 NPU、内存翻倍） |

## 4. 组件契约与数据流

### 4.1 模块与角色

| 文件 | 角色 | 运行解释器 | 顶层依赖约束 |
|---|---|---|---|
| `worker_protocol.py`（新） | 协议编解码（纯函数 + `ProtocolError`） | 两侧都要能 import | stdlib-only |
| `worker_supervisor.py`（新） | `WorkerSupervisor`：spawn / ready 握手 / 请求 / 超时 / kill / 节流 | system `python3`（随 `voice_hold`） | stdlib-only |
| `transcribe_worker.py`（新） | worker 主循环：加载模型一次，循环处理请求 | **venv** `python3` | 允许重型栈（`engine`/`terms`），但只在 `main()`/工厂内懒加载 |
| `voice_hold.py`（改） | 接线：预热、`_deliver` 走 supervisor（或旧 spawn） | system `python3` | 不变（`TestModulePurity` 仍须过） |

### 4.2 协议（`worker_protocol.py`）

一条消息 = 一行 UTF-8 JSON（`json.dumps(..., ensure_ascii=False)` + `"\n"`），通道为 socketpair 的一端。

```python
PROTOCOL_VERSION = 1

class ProtocolError(Exception):
    """对端发来的行不是合法协议消息（坏 JSON / 形状不符）。"""

def encode(msg: dict) -> bytes                      # 唯一序列化出口（含换行）
def encode_ready(pid: int, engine: str, model: str, load_s: float) -> bytes
    # -> {"event":"ready","protocol":1,"pid":…,"engine":"npu","model":"small-int8-ov","load_s":0.71}
def encode_request(rid: int, wav: str) -> bytes      # -> {"id":<int>,"wav":"<绝对路径>"}
def encode_text(rid: int, text: str) -> bytes        # -> {"id":<int>,"text":"…"}
def encode_error(rid, message: str) -> bytes         # -> {"id":<int|null>,"error":"<单行>"}
def parse_line(line: bytes) -> dict                  # 坏 JSON / 非 dict / id 非 int -> ProtocolError
```

规则：① 未知字段容忍（前向兼容）；② `error` 只承载单行诊断（换行由 JSON 转义）；
③ 响应行有 `id`；**事件行（`event`）没有 `id` 也合法**——`parse_line` 只在 `id` 字段**存在**时要求它是 int；
④ 协议不做版本协商（同仓两侧同版本），但保留 `protocol` 字段供将来拒绝不匹配。

### 4.3 supervisor 契约（`worker_supervisor.py`）

```python
ENV_RESIDENT = "VOICE_INPUT_RESIDENT"
WORKER_RESTART_MIN_INTERVAL = 1.0     # 两次 spawn 之间的最小间隔（秒）
PREWARM_READY_TIMEOUT = 600.0         # 预热等 ready 的上限（冷编译 ~155s 的 3.9x 余量）

def resident_enabled(env: dict) -> bool          # 纯：env.get(ENV_RESIDENT, "1") != "0"

class WorkerSupervisor:
    """常驻转写 worker 的唯一起居者：spawn/握手/请求/超时/杀/节流。

    线程安全说明（D15）：只在 `_deliver` 工作线程与 prewarm 线程中被调用。
    `self._lock` 只保护状态字段读改写的**短临界区，不跨任何阻塞读**；
    并发启动由单一 in-flight `_Startup` 协调（owner 独占读 ready 行 + `ready_event`），
    加入者用自己的预算等 event，超时只失败自己这次听写、不杀 worker。
    """
    def __init__(self, command: list, env: dict, *, spawn=None,
                 clock=time.monotonic, log=None): ...
        # command = [VENV_PY, <repo>/transcribe_worker.py]；env 由调用方给 engine.child_env(...)
        # spawn(command, env, fd) -> Popen 为单测注入缝（默认 subprocess.Popen），
        # clock/log 同为注入缝（默认 time.monotonic / 打印 stderr）

    @property
    def ready(self) -> bool: ...

    def prewarm(self) -> bool:
        """后台线程入口：spawn + 等 ready（上限 PREWARM_READY_TIMEOUT）。
        成功记日志：`[voice-hold] worker ready in X.XXs (pid N, engine=…)`（load_s 取自 ready 行；A15 钉格式）；
        失败记可行动日志并返回 False（不抛）。
        失败后 supervisor 停在 not-ready：下一次 request 会按 D5 的节流自行重试 →
        用户修好模型/引擎配置后无需重启 daemon。"""

    def request(self, wav: str, timeout: float) -> tuple[str | None, str | None]:
        """一次听写的转写请求 -> (text, error)。
        流程：确保 ready（必要时 spawn，等 ready 的剩余预算来自 timeout）→ 发请求 →
        读到同 id 响应（成功）/ error / EOF / 超时。
        error 取值（供调用方日志区分）：
          "timeout"      **两种情形分开处置（D15）**：
                         ① 请求已在飞（发出请求后读超时）→ 已 SIGKILL worker，下次请求重建；
                         ② 等 ready 期间（含加入者预算耗尽）→ **不杀** worker，仅失败本次听写（保住冷编译）
          "worker-exit"  ready 前或请求中 EOF（含 spawn 失败/启动即退）
          "protocol"     行不合法 / id 不匹配
          "throttled"    距上次 spawn 不足 WORKER_RESTART_MIN_INTERVAL，本次不重试（等下一次听写）
          "transcribe:<msg>"  worker 回 error（worker 存活，不杀）
        """
    def shutdown(self) -> None:
        """kill worker + 关通道；**幂等**（连调两次不抛、worker 必已退出）；run() 的 finally 调用（A15 钉）。"""
```

内部规则：
- **锁与启动协调（D15，唯一权威描述）**：`self._lock` 只用于「读改写 `_proc/_conn/_ready/_last_attempt/_startup`」这类短临界区，
  **任何阻塞读（ready 握手、响应等待）都不得持锁**。启动是单一 in-flight 对象
  `_Startup(proc, conn, ready_event, error)`：
  ① 先在锁内检查——已 ready 直接用现成 conn；有 in-flight 启动则取引用（当加入者）；都没有则（受节流约束）创建 `_Startup` 并 spawn，自己当 owner；
  ② owner 在锁外 `settimeout(自己的预算)` 读 ready 行，然后置 `ready_event`（成功）或写 error 并杀 worker（失败/超时）；
  ③ 加入者在锁外 `ready_event.wait(自己的剩余预算)`——**不读同一 socket**（同一 socket 只能有一个读者）；
  加入者预算耗尽只返回 `"timeout"`（本次听写失败），**不杀 worker**（owner 可能仍在冷编译，杀掉等于白扔 155s）。
- **spawn**：`socketpair()` → `child.set_inheritable` 交给 `Popen(cmd + ["--protocol-fd", str(child.fileno())], env=env, pass_fds=(child.fileno(),), stdin=DEVNULL)`（stdout/stderr 继承）→ 父进程 `child.close()`。`WORKER_RESTART_MIN_INTERVAL` 内的重复 spawn 尝试被挡下并返回 `"throttled"`；`Popen` 抛 `OSError` → 记一条可行动日志，返回 `"worker-exit"`。
- **ready 握手**：读第一行必须是 `{"event":"ready"}`；读到 EOF/坏行/其他事件 → 视为启动失败（worker 的 stderr 已经在 journal 里，supervisor 只补一行「worker exited before ready」）。
- **请求读**：`conn.settimeout(剩余预算)`；`socket.timeout` → `"timeout"`；`EOF` → `"worker-exit"`；`ProtocolError`/id 不匹配 → `"protocol"`。三种都走 `_kill()`。
- **id**：从 1 递增的进程内计数器（每次 spawn 不重置也没关系，只用于配对）。
- **`_kill()`**：`proc.kill()` → `proc.wait(timeout=…)`（超时再记一行）→ 关 conn → 置 `ready=False`。杀不掉只记录、不阻塞调用方。
- **不新增线程**：除 prewarm 外不额外起监控线程。

### 4.4 worker 契约（`transcribe_worker.py`）

```python
def parse_args(argv) -> Namespace           # 唯一参数：--protocol-fd N（缺失/非 int -> exit 2）
def worker_main(conn, *, model_factory=None, terms_loader=load_terms,
                clock=time.monotonic, log=None) -> int:
    """worker 主循环（可测）：build 一次模型 -> ready -> 循环「读请求/转写/回文本」。
    退出码（与下节退出码表一致）：协议 EOF（父进程消失）→ 返回 **0**；
    `ProtocolError`（坏行）→ 记录后返回 **3**（A7 钉）。"""
def main(argv=None) -> int
```

语义要求：
- **前导（镜像 `transcribe_once.py` 顶层，**全部引擎都算路径**）**：进程内 `sysconfig.get_paths()['purelib']` 取 site-packages
  （**不再起子进程**——worker 自己就是 venv 解释器）→ `engine.required_lib_paths(eng, site)`（cuda/auto 消费 site；
  `engine.py:106-116` 契约要求 site 必填，缺了直接 ValueError——所以 worker **不能**写成「非 npu 就不用管库路径」）
  → `os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(dict(os.environ), paths)`
  → npu 且缺 `NPU_LIB_DIR` 时打印与 `transcribe_once.py` 同款可行动报错并 `exit 1`；非法引擎值同样干净退出（不裸 traceback）。
- **模型**：`engine.build_model()`（经 `model_factory` 注入缝），失败 → stderr 可行动报错 + `exit 1`。
- **terms**：**每请求**重读（D9）；组装失败降级为无 prompt 并只告警（与 `transcribe_once.py::main` 同构）。
- **转写调用**：`model.transcribe(wav, language="zh", initial_prompt=prompt, **extra_kw)`，`text = "".join(s.text for s in segments).strip()`（与 CLI 同构）。
- **异常**：单次转写异常 → 回 `encode_error(rid, f"{type(e).__name__}: {e}")`，**worker 继续存活**；协议坏行 → 记录后退出（让 supervisor 重建）。
- **退出**：`readline()` 返回空（父进程消失）→ 直接 `return 0`（D11 第①层）。
- **stdout/stderr**：只用于日志（协议走 socket）；不设 `--wav` 参数（请求里带路径）。
- **退出码**：`0` = 协议 EOF/正常退出；`1` = 启动失败（venv/库路径/模型/引擎配置）；`2` = 参数非法（`--protocol-fd` 缺失或非 int）；`3` = 协议破坏（坏行）。supervisor 不依赖这些码，仅供 journal 判定死因。

### 4.5 行为等价性清单（实现必须逐条保住）

| 行为 | 今天（spawn 路径） | 常驻后 | 由哪条验收钉住 |
|---|---|---|---|
| 热词即时生效 | 每次新进程重读 `terms.json` | worker 每请求重读 | A7 |
| 热词/引擎错误降级不阻断 | 独立 try → 无 prompt 转写 | 同构 | A7 |
| 引擎/模型选择单一事实源 | `engine.py` | worker 走同一个 `engine.build_model`（注入工厂即证明调用点） | A7 |
| NPU 库路径注入 | `engine.child_env` 给子进程 | 不变：`HoldDaemon` 用 `engine.child_env(self.env)` 构造 supervisor，spawn 收到的 env 原样传递 | A9（构造参数断言） |
| 挂死处理 | `check_output(timeout)` 杀子进程 | socket 超时 → SIGKILL worker → 本次失败 | A4 |
| 可行动报错落 journal | 子进程 stderr 继承 | worker stderr 继承（不吞、不改道） | A5 |
| wav 生命周期 | 父进程建、`finally` 删 | 不变（worker 只读） | A9 |
| 延迟可测性（D13） | 只有 `recording ->` 与 `delivered:` 两行，松手时刻无锚点 | release 行 + 转写耗时行齐全，两个窗口口径明确 | A9 |
| busy 期间按键丢弃 | `transition(..., busy=True)` | 不变（本 issue 不碰） | A12（既有测试） |
| CLI 独立可用 | `transcribe_once.py` | 一行不改 | A11（static：`git diff --name-only main -- transcribe_once.py` 为空） |

### 4.6 数据流

```
启动（voice_hold.run）
  抢锁 → preflight → 选设备
  ├─ 仅当 `resident_enabled(env)` 为真: daemon 线程 supervisor.prewarm() ──spawn──▶ worker: import/build_model ──▶ {"event":"ready"}
  └─ 主线程: dev.read_loop()  ← 立即开始监听（不等预热）

一次听写
  按住 → start_recording（arecord <唯一 wav>）
  松手 → stop_recording（杀 arecord/恢复媒体/藏浮层）→ 工作线程 _deliver
         ├ 尺寸检查 →（resident）supervisor.request(wav, VOICE_INPUT_TRANSCRIBE_TIMEOUT)
         │     ready? ──否──▶ 有 in-flight 启动就**加入等待**（自己的预算，D15）；没有就 spawn + 等 ready
         │                    （加入者预算耗尽：本次听写失败，但**不杀**仍在编译的 worker）
         │     发 {"id":N,"wav":…} ──▶ worker: 重读 terms → model.transcribe → {"id":N,"text":…}
         │     超时/EOF/协议坏 ──▶ SIGKILL worker，本次 text 为空（下次听写自动重建）
         └（!resident）旧路径：check_output([VENV_PY, transcribe_once.py, wav])
         → _paste_and_report(text)（接缝不变）→ finally unlink(wav) → busy=False

退出（SIGTERM / Ctrl-C / 异常）
  run() 的 finally → supervisor.shutdown()（kill worker）
  父进程被 SIGKILL → worker 读到协议 EOF 自退（实测 ≤3.1s，在飞解码结束后）
```

## 5. 可测性拆分设计（必答）

**测试边界**：全部功能点都能在**不加载任何模型、不碰 NPU、不碰 evdev/GTK** 的前提下验证——
协议是纯函数，进程行为靠「真实子进程 + 打桩 worker 脚本」验证，转写语义靠注入 fake model 验证。

| 拆分出的单元 | 类型 | 隔离方式 | 可测内容 |
|---|---|---|---|
| `worker_protocol.encode_*` / `parse_line` | 纯函数 | 直接调用 | 往返一致、`ensure_ascii=False`、坏 JSON/非 dict/`id` 非 int → `ProtocolError`、未知字段容忍 |
| `resident_enabled(env)` | 纯函数 | 传 dict | 缺省 = 开；`"0"` = 关；`"false"`/`"1"`/空串的行为按 `!= "0"` 契约 |
| `WorkerSupervisor.request` + `prewarm` | 副作用接缝（进程 + socket） | **打桩 worker**（tmp 目录里的 python 脚本，按环境变量扮演：正常应答/不回/即退/坏行/慢 ready） | 握手、成功往返、超时杀进程、EOF 检测、坏协议、error 分支不杀、节流（spawn 次数 + `"throttled"`）、启动协调（慢 ready + 双线程：owner/加入者；两种 `"timeout"` 分开处置）、`shutdown` 幂等、prewarm 成功日志格式与失败返回 False |
| `transcribe_worker.worker_main` | 副作用接缝（socket + 注入工厂） | `socketpair()` 在进程内成对 + `model_factory=lambda: FakeModel(...)` | ready 行内容、请求→文本、terms 每请求重载（改 `terms.json` 后第 2 次请求生效）、转写异常→error 行且存活、EOF→返回 0、**坏行→返回 3** |
| `HoldDaemon._deliver`（resident 分支） | 接线 | mock supervisor + 既有 `_spawn_paste` 接缝 | 成功路径调用 paste；error 路径不 paste、日志为可行动文案、`busy` 复位 |
| `HoldDaemon._deliver`（legacy 分支） | 接线 | mock `check_output` | `VOICE_INPUT_RESIDENT=0` 时 argv 仍是 `[VENV_PY, transcribe_once.py, wav]`，不 spawn worker；release 行与 `transcribed in X.XXs (spawn)` 行同样打出（U4 的 journal 判定依赖） |
| `run()` 的预热接线 | 接线 | mock `WorkerSupervisor` | 预热在工作线程（不阻塞 `read_loop`）；`finally` 调 `shutdown()` |
| `bench/hold-latency.py` | 脚本 | `--dry-run` | 参数解析 + 打印将执行的命令（不跑真模型） |
| 新模块顶层纯度 | 静态（干净子进程 import） | `subprocess.run([sys.executable, "-c", "import worker_protocol, worker_supervisor, transcribe_worker"], env={"PATH": …})`（沿用 `TestModulePurity` 的隔离 env 模式） | 三个新模块顶层 import 不拉起重依赖、不碰设备 |
| `stop_recording` / `_deliver` 的日志行 | 接线 | mock supervisor / `_spawn_paste` + `redirect_stderr` | release 行与 `transcribed in …` 行各恰好一行 |

**刻意不做的事**：不为了可测性给 `HoldDaemon` 注入新依赖（沿用 `env` dict + `mock` 的既有先例）；
不给 `WorkerSupervisor` 加「假 socket 工厂」这种二阶抽象——打桩 worker 是真实子进程，测的就是真实链路；
不在 CI 里跑真模型计时（计时项走 bench 脚本 + 真机实测）。

**防 flaky 设计**：打桩 worker 的「已就绪」必须靠协议行握手（不许用 `sleep` 猜）；
超时类断言用短超时（0.2–0.5s）+ 断言「进程确实死了」而不是只看返回码；
节流断言比较 spawn 调用次数（注入 `spawn` 计数）而不是比较墙钟时间；
整例用 `unittest` + `mock`，不依赖网络/模型/设备。

## 6. 验收矩阵（必答）

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|---|---|---|---|---|
| A1 | 协议编解码（纯） | 自动化（unit） | `python3 -m unittest test_worker_protocol -v` | 往返一致；坏 JSON/非 dict/坏 id 抛 `ProtocolError`；未知字段容忍 |
| A2 | 常驻开关解析（纯） | 自动化（unit） | `python3 -m unittest test_worker_supervisor -v` | 缺省开、`"0"` 关、`"false"`/`""`/`"1"` 按 `!= "0"` 契约 |
| A3 | 成功往返 + ready 握手 + spawn 契约 | 自动化（integration，打桩 worker） | `python3 -m unittest test_worker_supervisor -v` | `request()` 返回打桩文本；`ready` 在握手后为真；打桩 worker 收到的 argv 含 `--protocol-fd <N>` 且该 fd 是有效 socket；注入的 `spawn` 收到 `env` 与调用方传入的完全一致 |
| A4 | 挂死 → 超时杀 + 下次重建 + 并发启动协调 | 自动化（integration） | 同上（打桩「不回」/「慢应答」/「慢 ready」；注入 `clock` 推进过节流间隔） | 返回 `(None,"timeout")`；超时 ≤ 设定值 + 0.5s；`poll()` 非 None；`clock` 推进过 `WORKER_RESTART_MIN_INTERVAL` 后下一次 `request` 会 spawn 新 worker（pid 变化）；**慢 ready 场景下加入者按自己预算失败、worker 不被杀**（D15 加入者语义） |
| A5 | 启动即退/坏协议 → 可行动失败 | 自动化（integration） | 同上（打桩「即退并往 stderr 写报错」/「回坏行」；`spawn` 注入计数与 kwargs 断言） | 返回 `(None,"worker-exit"\|"protocol")`；supervisor 经注入 `log` 记一条可行动失败行；**stderr 继承可断言**——用 `mock.patch` 拦截 `worker_supervisor.subprocess.Popen`（或注入的同形 spawn 转发）检查 kwargs：stdout/stderr 均非 `PIPE`/`DEVNULL`（即直达 journal）；**不产生无界 spawn**（单次请求 spawn ≤1） |
| A6 | 节流 | 自动化（unit） | 同上（注入 `spawn` 计数 + 假 clock） | 间隔内的第二次请求返回 `(None,"throttled")` 且 spawn 计数不增；clock 推进过间隔后允许一次 spawn |
| A7 | worker 主循环 + terms 每请求重载 | 自动化（unit，socketpair + fake model） | `python3 -m unittest test_transcribe_worker -v` | 首行 `ready`；请求→文本；改 terms 后第 2 次请求带上新 prompt；转写异常回 `error` 行且进程存活；EOF 返回 0；**构造坏行 → `worker_main` 返回 3**（退出码表钉） |
| A8 | worker EOF 自退（真进程） | 自动化（integration） | 同上：子进程跑 `worker_main`（fake model），父进程关闭 socket | 子进程在 1s 内自行退出（`poll()` 非 None），退出码 0 |
| A9 | `_deliver` resident 分支接线 + 构造契约 + 延迟日志 | 自动化（unit） | `python3 -m unittest test_hold -v` | 成功 → 调 `_spawn_paste`；error → 不 paste 且日志含 `worker`；两种路径 `busy` 都复位；`HoldDaemon` 以 `command=[VENV_PY, <repo>/transcribe_worker.py]` 与 `env=engine.child_env(...)` 构造 supervisor（mock 断言）；`stop_recording` 打 release 行、`_deliver` 打 `transcribed in X.XXs (worker\|spawn)` 行；`request` 被以 `timeout=self._transcribe_timeout()` 调用（mock 断言，D6 旋钮不被写死） |
| A10 | `_deliver` legacy 分支接线 | 自动化（unit） | 同上（`VOICE_INPUT_RESIDENT=0`） | 走 `check_output([VENV_PY, transcribe_once.py, wav])`，不 spawn worker；release 行与 `transcribed in X.XXs (spawn)` 行照打（U4 的 journal 判定依赖） |
| A11 | 模块纯度 + 静态检查 | 自动化（static） | `python3 -m py_compile voice_hold.py worker_protocol.py worker_supervisor.py transcribe_worker.py`；`TestModulePurity`（voice_hold）全绿；**干净子进程 import 断言覆盖三个新模块**（沿用 `TestModulePurity` 的隔离 env 模式）；`rg -n "transcribe_once" voice_hold.py` 仅剩三处合法引用（模块 docstring / legacy 分支 / #18 起的 preflight 存在性检查），旧内联 spawn 调用归零；`git diff --name-only main -- transcribe_once.py` 为空 | 全部通过；新模块顶层无重依赖 import（重依赖只在 `main()`/工厂内）；CLI 未被改动 |
| A12 | 全仓回归 | 自动化（unit） | README Testing 节（本 issue 更新后同时是套件出处）：`python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v` | 全绿；README 命令与实际回归面一致 |
| A13 | bench 脚本参数面 | 自动化（static/unit） | `python3 bench/hold-latency.py --dry-run --wav /tmp/x.wav --runs 3`（`--dry-run` 不校验文件存在、不加载模型） | 打印两条路径的命令与轮次 |
| A14 | 常驻路径实测有加速（**本机 + 真引擎**） | 自动化（automated E2E，本机手动触发、无 CI 依赖） | `venv/bin/python3 bench/hold-latency.py --wav <用户自备 8s 中文 wav> --runs 5`（先确认 daemon 空闲；wav 用 `arecord -f S16_LE -r 16000 -c 1 -D default` 录 8s，**放仓外**如 `/tmp`、**不进 git**；不用合成正弦音——它触发 #32 病态，不代表真实听写） | 常驻请求中位 < spawn 全路径中位，差值 ≥0.8s；同时记录 spawn→ready 时间；结果回贴 issue |
| A15 | prewarm 日志（成功格式/失败返回）+ `shutdown` 幂等 | 自动化（integration） | `python3 -m unittest test_worker_supervisor -v`（打桩「即退」经 `prewarm()` 触发；成功路径用正常打桩 worker） | 成功：log 行匹配 `worker ready in X.XXs (pid …)`；失败：`prewarm()` 返回 False 且不抛、log 记一行可行动文案，随后 `request()`（clock 推进过间隔）会重试 spawn；`shutdown()` 连调两次不抛且 worker 进程已退出 |

### 验收前置（U1–U5 共用，缺一不可）

```bash
# ① 代码落位：systemd ExecStart 指向**主 checkout**，不拉取就在旧代码上验收
cd ~/work/git-repo/voice-input && git pull --ff-only
# ② 确认本机 unit 与 contrib 的差异（本机是手工副本：240s 超时在 drop-in 里）
diff ~/.config/systemd/user/voice-hold.service contrib/voice-hold.service
# ③ 重启并确认新代码在跑
systemctl --user restart voice-hold && systemctl --user status voice-hold | head -5
```

- 合并前预验收（可选）：把本机 unit 的 `ExecStart` 临时指向 worktree 绝对路径 + `daemon-reload`，验后还原。
- 每个 U 项结论都要记录**所用代码版本**（`git -C ~/work/git-repo/voice-input rev-parse --short HEAD`）。

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|---|---|---|---|---|
| U1 | 真机听写热路径延迟 | 用户实测 | 按「验收前置」部署后：按住右 Alt 说一句短句（无 #32 病态），从 journal 取 **release 行 → delivered 行** 的差值（end-to-end 窗口） | 短句 end-to-end（release→delivered，含 settle+转写+paste）≤1s（如实记录多句样本；长句/病态输入按实测记录，不宣称达标）；同时记录 `transcribed in …` 行用于分解 |
| U2 | `kill -9` worker 后自动恢复 | 用户实测 | `systemctl --user status voice-hold` 记下 worker PID → `kill -9 <worker pid>` → 再听写一句 → `journalctl --user -u voice-hold -n 20` | 主进程 PID 不变；journal 出现新 worker 的 ready 行；本次听写文本正常上屏 |
| U3 | 预热生效 + 不阻塞监听 | 用户实测 | `systemctl --user restart voice-hold` 后**立即**（1s 内）按住右 Alt 说一句；再看 journal 的 ready 行 | 热键立即有反应（浮层出现）；journal 有 `worker ready in …s`；首次听写不因预热而失败 |
| U4 | 逃生开关真机可用 | 用户实测 | ① `systemctl --user edit voice-hold` 加 drop-in：`[Service]` + `Environment=VOICE_INPUT_RESIDENT=0`；② `systemctl --user daemon-reload && systemctl --user restart voice-hold`；③ 听写一句；④ 验后删除 drop-in 并 `daemon-reload && restart` 还原 | 文本正常上屏；journal **无** `worker ready` 行、出现 legacy spawn 路径日志（`transcribed in … (spawn)`）；还原后 ready 行回来 |
| U5 | 常驻与 CLI 路径共存 | 用户实测 | worker 常驻时跑 `VOICE_INPUT_ENGINE=npu ./test-mic.sh`（或 `venv/bin/python3 transcribe_once.py <wav>`） | CLI 同时能加载自己的模型并输出文本（NPU 可被多进程共享）；不出现设备占用失败 |

> 说明：U1 的「≤1s」按 R1/R2 的限定口径验收（无 #32 病态的短句）；#32 导致的 decode 膨胀如实记录，不并入本 issue 的达标声明。
> U4 必须用 drop-in 而不是 `VAR=… systemctl …` 前缀——后者不会进入服务环境（systemd 不继承调用方 env）。

## 7. 风险与已知限制

1. **收益上限受 decode 支配**：常驻只消掉固定 1.1–1.45s；#32（解码病态）可让单次 decode 膨胀到 2s+。因此本 issue 的达标口径必须是「短句热路径」。
2. **常驻占用**：worker 常驻 RSS 1.07–1.10GB，且长期持有 NPU 模型（issue 已接受）。缓解：`VOICE_INPUT_RESIDENT=0` 逃生开关 + 崩溃即重建。
3. **NPU 驱动级挂死**：worker 挂死由超时覆盖；若驱动本身卡死，重建后的 worker 可能同样卡死（日志会连续出现 timeout 行）→ 诊断路径：`journalctl --user -u voice-hold` + `dmesg`；恢复靠重载驱动/重启。与今天行为一致（今天也是每次听写都失败），但可见性更好（预热期就会暴露）。
4. **预热被请求超时打断**：冷编译期间到达的听写若超出 `VOICE_INPUT_TRANSCRIBE_TIMEOUT`，**本次听写失败但不杀 worker**（保住进行中的编译，§4.3 情形 (b)）；只有等 ready 超过 `PREWARM_READY_TIMEOUT`(600s) 才判定启动失败并 kill，下一次听写重建。
5. **孤儿窗口**：父进程在 worker 解码中被 `SIGKILL` 时，worker 会在该次解码结束后才自退（实测 ≤3.1s）。systemd 场景由 cgroup kill 覆盖。
6. **手工启动路径**：`python3 voice_hold.py` 直接跑（非 systemd）时，Ctrl-C 依赖 `finally: shutdown()`；若被 `kill -9`，靠 D11 第①层。
7. **前导代码第三份拷贝**（D14 的代价）：`transcribe_once.py` / `voice-ptt.py` / `transcribe_worker.py` 各有一份「依赖库路径 + 可行动报错」。本 issue 不动前两个；建议后续单独立 issue 抽取 `engine.prepare_process_env()`。
8. **文档连带**：README（EN+ZH）里描述「每次听写额外付约 1.4 秒模型加载（#18 子进程架构）」以及「常驻转写 worker 由 #25 跟踪」的旧表述必须更新，否则文档撒谎；配置表新增 `VOICE_INPUT_RESIDENT`；**Testing 节命令纳入三个新测试模块**；contrib unit 注释需改为描述常驻 worker 现状。**注意本机 unit 是手工副本（非 contrib 软链）**，真机验证项需按实际文件判断（§6「验收前置」）。
9. **cuda 引擎的库路径生效边界（沿用现状，不在本 issue 修）**：worker 与 `transcribe_once.py` 一样在**进程内**写 `os.environ["LD_LIBRARY_PATH"]`，而真正 dlopen 生效依赖**调用方进程启动时**的 `LD_LIBRARY_PATH`（shell wrapper / systemd）；`engine.child_env` 只对 npu 注入。因此 hold+cuda 的支持面与今天完全相同——本 issue 不改善也不恶化。

## 8. 改动文件清单

| 文件 | 改动 |
|---|---|
| `worker_protocol.py`（新） | 协议常量 + `encode_*` / `parse_line` / `ProtocolError` |
| `worker_supervisor.py`（新） | `resident_enabled`、`WorkerSupervisor`（spawn/握手/请求/超时/杀/节流/`shutdown`） |
| `transcribe_worker.py`（新） | worker 主循环 + 前导 + 参数解析 |
| `voice_hold.py` | 构造 supervisor（`engine.child_env` + `resident_enabled`）；`run()` 在常驻模式下起预热线程（daemon 线程，不阻塞 `read_loop`）+ `finally: shutdown()`；`_deliver` 双分支（resident/legacy）+ 阶段耗时日志 |
| `test_worker_protocol.py`（新） | A1 |
| `test_worker_supervisor.py`（新） | A2–A6 + A15（打桩 worker + 启动协调 + 节流 + prewarm 失败 + 新模块纯度） |
| `test_transcribe_worker.py`（新） | A7/A8（fake model + socketpair + 顶层纯度） |
| `test_hold.py` | A9/A10（`_deliver` 双分支 + 构造契约 + release/耗时日志 + 预热接线） |
| `bench/hold-latency.py`（新） | spawn vs 常驻计时对比，`--dry-run` |
| `README.md` / `README.zh-CN.md` | NPU 节 + 配置表（`VOICE_INPUT_RESIDENT`）+ 已知限制/资源段落 + **Testing 套件命令纳入三个新测试模块** |
| `contrib/voice-hold.service` | 注释更新（描述常驻 worker 现状与 `VOICE_INPUT_RESIDENT` 逃生开关；保留 timeout 说明） |

## 9. 待确认的决策点（⏸ design gate）

1. 默认常驻 + `VOICE_INPUT_RESIDENT=0` 逃生开关（D8）——推荐。
2. 启动后台预热（D4）vs 首次听写懒加载——推荐预热。
3. 验收口径：U1 的「≤1s」限定为「无 #32 病态的短句」，长句/病态如实记录——推荐。
4. 不顺手改 `_deliver` 的 0.3s settle（NG4），另立 issue——推荐。
5. 新增 `bench/hold-latency.py`（D12）作为 issue 要求的对比证据——推荐。
