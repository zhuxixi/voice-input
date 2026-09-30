# #28 单实例保护 + 录音路径唯一化 — 设计

- issue：`zhuxixi/voice-input#28`
- 基线：`main @ 5f8bf72`
- 状态：**已批准**（2026-09-30 design gate；v2 = v1 + 评审修正 B1/B2/M1–M3/m1–m4/n1，见 §9；四个决策点按推荐确认：锁放 `/tmp`、退出码 4 + unit 配 `RestartPreventExitStatus=4`、X11 两脚本一并改、日志带路径）
- 流程：github-issue-driven 步 3→4；根因报告沿用 issue 正文（R1 已做事实修正），本文件专注方案设计

---

## 1. 背景

hold 链路的重复上屏有两层成因：`voice_hold.py` 没有任何单实例保护（systemd 只约束同一 unit，对「手敲 `python3 voice_hold.py`」无效），且录音写死 `/tmp/voice-input-hold.wav`，两个实例会互相 unlink/截断同一个文件——后者让「重复」呈现为间歇性（详见 issue 正文与调研 R1）。

**关键约束（决定验收形态）**：本机诱因（9/29 的 `newgrp` 幽灵实例）已清除，当前只有 systemd 单实例在跑 → **症状不可复现，验收必须靠自动化测试**，不能靠「用一段时间没再出现」。

## 2. 目标与非目标

**目标（G）**

- G1：同一用户下，`voice_hold.py` 的第二个实例必须**拒绝启动**并以可诊断的方式退出，且不引发 systemd 重启循环。
- G2：录音文件不再共用固定路径，即使锁被绕过也不会发生跨实例的删/截断竞态。
- G3：上述两条都有自动化测试钉子（锁语义、路径唯一化），回归由 README 钉定的全量套件覆盖。
- G4（顺带收益）：`_deliver` 不再依赖全局固定路径 → 与 #29 spec L49 记录的「驱动 `_deliver` 需要伪造全局路径」障碍一并解除。

**非目标（NG）**

- NG1：设备选择/「设备认错失聪」——另一条挂账，issue 正文已明确排除。
- NG2：`transition()`/busy-drop 语义、`paste.py` 与 #29 的 paste 接缝（#29 plan 的反向约束）。
- NG3：`#25` 常驻 worker 架构（本 issue 不重构转写进程模型）。
- NG4：不引入新依赖；`voice_hold.py` 的模块纯度契约（顶层 stdlib-only）必须保持。
- NG5：不做「跨脚本全局互斥」（`voice-hold` 与 `voice-ptt` 并存允许存在——两者是不同平台链路）。

## 3. 设计决策

| ID | 决策 | 理由 | 否决的备选 |
|---|---|---|---|
| D1 | 用 **`fcntl.flock(LOCK_EX\|LOCK_NB)`** 在锁文件上做互斥；文件以 `O_RDWR\|O_CREAT\|**O_NOFOLLOW**`、0600 打开 | stdlib（纯度契约）；进程死亡内核自动释放 → **不会留下陈旧锁**（无需 PID 存活探测、无需 `kill -0` 清理逻辑）。`O_NOFOLLOW` 挡 `/tmp` 下的 symlink 指向（实测以 ELOOP 拒绝） | ① PID 文件：陈旧锁需额外存活判断，判断本身有竞态；② 三方库（`filelock`/`pid`）：破纯度契约 |
| D2 | 锁文件放 **`<tmpdir>/voice-input-hold-<uid>.lock`**（`tmpdir` 见 D9），权限 0600 | **本 issue 的历史场景就是「幽灵进程熬过注销」**：它由 `setsid nohup` 起、在 `/tmp` 之外活了一整天。`$XDG_RUNTIME_DIR` 在注销时被 logind 整目录删除 → 幽灵持有的锁落在**已删除的 inode** 上，新会话会建出新锁文件、互斥失效。放 `$TMPDIR` 才能覆盖这个场景（`/tmp` 只在开机清理，那时进程必然已死） | `$XDG_RUNTIME_DIR/voice-input/hold.lock`：更符合惯例、更私密，但注销即失效，**恰好漏掉本 bug 的原始触发路径**，否决 |
| D3 | **任何锁获取失败都按退出码 4 退出**——包括「被占（EWOULDBLOCK）」与「打不开（EACCES/ELOOP 等一切 OSError）」，`acquire_singleton_lock` **永不抛异常**；两种失败文案不同 | `Restart=always` **对 exit 0 也会重启**，要避免循环只能靠 `RestartPreventExitStatus=4` 精确命中退出码——若让 OSError 炸 traceback，退出码非 4 → 防重启不命中 → **复刻本 issue 要消灭的 3s 带病循环**（直到默认 start-limit 5 次才停）。统一按 4 退出后：瞬时故障仍可由 systemd 重试兜底，持续性故障（如 /tmp 被预占）被 start-limit 自然限流，且文案直接给出 errno 可诊断 | ① exit 0：仍会被 `Restart=always` 重启且掩盖问题；② OSError 各配独立退出码：unit 要枚举一串 `RestartPreventExitStatus`，脆弱 |
| D4 | 录音路径改为 **每次录音 `tempfile.mkstemp(prefix="voice-input-hold-", suffix=".wav", dir=tmpdir)`** | 唯一化到「每次录音」而非「每进程」，同进程的两次录音也不可能互撞；`mkstemp` 保证 O_EXCL 创建，无 TOCTOU；路径仍只经 argv 传给 `transcribe_once.py`，无契约变更 | ① PID 后缀：只防进程间，不防同进程重入；② 保留固定名只加锁：锁一旦被绕过（排查时人为干预）竞态即回归，与 G2 冲突 |
| D5 | 启动顺序：**锁 → preflight → evdev/设备选择** | 锁是最具决定性的事实（「已有实例在跑」比「venv 缺失」更该先报）；且抢到锁后失锁路径不存在，后续代码无需回滚锁 | 放最后：会白跑 preflight 并掩盖多实例事实 |
| D6 | 第二实例的报错信息**包含持有者 PID 与锁路径**，PID 文案写 **`last holder pid N`**（尽力而为的诊断信息，不是判定依据） | 用户要能一行定位「是谁占着」。PID 从锁文件内容读出——**内容可能是已死的前任持有者**（拿锁与写 pid 之间的微秒窗、上次运行残留），故文案必须带 `last holder` 限定，展示前可 `os.kill(pid, 0)` 探活追加 `(dead?)` 标注；**判定依据始终是 flock 本身** | 不写 PID：用户需自己 `lsof`/`ps` 找 |
| D7 | **X11 链路（`voice-ptt.py` + `voice-toggle.sh`）一并做路径唯一化**，但**不加锁** | R1 实测：固定名是**按脚本分域**的，`hold.wav` 与 `recording.wav` 互不影响；真正互撞的是 `voice-ptt` ↔ `voice-toggle` 这一对（同为 X11 链路、共用 `recording.wav`）。加锁会与 NG5 冲突（hold 与 ptt 本可并存） | 只改 hold：X11 那对竞态留着（issue 正文「顺带发现 1」明确建议一并处理） |
| D8 | `recording` 日志行**带上本次录音路径** | 路径不再固定后，调试配方（JFox《hold 链路无归档时抓听写音频与归因的调试法》所用的「拷固定路径快照」）会失效；日志带路径是新的抓音入口。已核：README 无钉死的日志样例，无文档连带 | 不加日志：调试时无法知道当前 wav 名 |
| D9 | `runtime_tmpdir(env)`：`$TMPDIR` **非空且为绝对路径**才用，否则 `tempfile.gettempdir()` | `tempfile.gettempdir()` 对相对路径是拒绝的；env 分支必须同语义，否则用户误设相对 `TMPDIR` 会让锁/录音落到相对路径（相对锁路径 = 每个 cwd 一把锁，互斥完全失效） | 直接透传 `$TMPDIR`：行为与 stdlib 语义分叉 |

## 4. 组件契约与数据流

### 4.1 新增纯函数 / 副作用接缝（全部在 `voice_hold.py`）

```python
LOCK_STEM = "voice-input-hold"          # 锁文件名主干
EXIT_DUPLICATE_INSTANCE = 4             # D3
RECORDING_PREFIX = "voice-input-hold-"  # 录音文件名前缀
RECORDING_SUFFIX = ".wav"

def runtime_tmpdir(env) -> str:
    """纯:锁/录音所在目录。$TMPDIR 非空且为绝对路径则用它,否则
    tempfile.gettempdir()(D9——与 stdlib 拒绝相对路径的语义一致)。"""

def singleton_lock_path(uid: int, tmpdir: str) -> str:
    """纯:<tmpdir>/voice-input-hold-<uid>.lock(无 I/O)。"""

def acquire_singleton_lock(path: str):
    """副作用唯一的锁入口。-> (fd, holder_pid, error) 三元组,成功=(fd, None, None)。
    失败**永不抛异常**(D3/B1):
      - 被占   -> (None, <锁文件里的 pid,尽力而为>, None)
      - 打不开 -> (None, None, "<errno 可读串>")[EACCES/ELOOP 等,含 O_NOFOLLOW 命中]
    open 用 O_RDWR|O_CREAT|O_NOFOLLOW,mode 0600;成功后 truncate + 写入自身 pid。
    fd 为 non-inheritable(PEP 446 默认,**不得显式打开继承**——M3 契约:
    arecord/transcribe 子进程不得持有锁,否则挂死的转写子进程会把锁拖过
    父进程死亡,造成"实例已死但新实例拒绝启动"的永久失聪);fd 必须由
    调用方长期持有(关闭即释放锁)。"""

def new_recording_path(tmpdir: str) -> str:
    """副作用唯一的新建录音文件入口:mkstemp + close(fd) -> 返回路径。"""
```

### 4.2 接线（改动点）

| 位置 | 改动 |
|---|---|
| `HoldDaemon.__init__` | `self.wav_path = None`；`self._lock_fd = None` |
| `HoldDaemon.run()` | 第一件事抢锁；`fd is None` → 按 D6/D3 打印（被占：`another instance already running (last holder pid N)`；打不开：`cannot open lock file <path>: <err>`，均附 `— refusing to start`）并 `return EXIT_DUPLICATE_INSTANCE`；成功则 `self._lock_fd = fd` 持有到进程结束 |
| `HoldDaemon.start_recording()` | 删掉 `unlink(WAVFILE)`；`self.wav_path = new_recording_path(runtime_tmpdir(self.env))`；`arecord` 的 `Popen` 包 try/except——**失败时先 `unlink(self.wav_path)` 再把异常抛出去**（M1：不留空文件，daemon 崩溃/重启语义不变）；日志改 `recording -> <path>`（D8） |
| `HoldDaemon._deliver()` | 读局部 `wav = self.wav_path`（`None` → 记录并 return，防御性）；尺寸检查、`transcribe_once.py <wav>`、`finally` 清理全部改用 `wav` |
| 模块常量与注释 | `WAVFILE` 删除（仅 `voice_hold.py` 自用，测试与文档无引用，已核）；**`_paste_and_report` docstring（L316-318）同步改**——它提到 "a real WAVFILE at a fixed /tmp path"（m2） |
| `voice-ptt.py` | `WAVFILE` 常量 → 每次录音 `tempfile.mkstemp` 路径（模块全局变量在 start 时重新赋值；stop 侧先快照局部再 sleep 0.3，防快速重录换路径）；无单测覆盖，靠 A11 的 `py_compile` + 改动保持纯机械 |
| `voice-toggle.sh` | 每次录音 `mktemp` 唯一路径，路径经 sidecar `"$PIDFILE.wav"` 跨按键传递。**执行期裁决（2026-09-30）**：该脚本是热键逐按键进程（两次按键=两个进程），plan 原案的「每进程 mktemp」会让第二次按键死等自己新建的空文件——改为 start 分支 mktemp 后写 sidecar、stop 分支读 sidecar（缺失/指向不存在的 wav → 一行报错 + 非零退出，不挂死），结束时 wav + sidecar 一并 `rm -f`，sidecar 生命周期镜像 PIDFILE |
| `contrib/voice-hold.service` | 加 `RestartPreventExitStatus=4`（D3）+ 注释说明与锁的配合；A11 有 static 钉子防将来被误清 |

### 4.3 数据流（修复后）

```
启动:  runtime_tmpdir → singleton_lock_path → acquire_singleton_lock
       ├─ 被占/打不开 → 打印诊断(持有者 pid 或 errno) → exit 4
       │               (systemd 因 RestartPreventExitStatus=4 不再重启)
       └─ 成功 → 持有 fd(不可继承) → preflight → 选设备 → read_loop
录音:  start_recording → new_recording_path → arecord <唯一路径> (日志打印路径)
上屏:  _deliver → 尺寸检查 → transcribe_once.py <唯一路径> → paste → finally unlink
```

## 5. 可测性拆分设计（必答）

**测试边界**：本 issue 的全部功能点都能在不碰 arecord / evdev / GTK / 真实转写的前提下验证——fs 与进程行为被收敛到两个独立函数（`acquire_singleton_lock`、`new_recording_path`）里，判断逻辑全部落在纯函数（`runtime_tmpdir`、`singleton_lock_path`）上。

| 拆分出的单元 | 类型 | 隔离方式 | 可测内容 |
|---|---|---|---|
| `runtime_tmpdir(env)` | 纯函数 | 传 dict | `$TMPDIR` 有/无/空/**相对路径**四种分支（D9） |
| `singleton_lock_path(uid, tmpdir)` | 纯函数 | 传参 | 路径拼接契约（含 uid、`.lock` 结尾、落在给定目录） |
| `acquire_singleton_lock(path)` | 副作用接缝（fs + flock） | 传 `tmp_path`；不触碰其它全局 | 互斥、诊断 PID、释放后重获、无陈旧锁、**打不开不抛**、**fd 不可继承**、**锁文件内容非数字时的健壮性** |
| `new_recording_path(tmpdir)` | 副作用接缝（fs） | 传 `tmp_path` | 唯一性、目录、文件确实被创建 |
| `HoldDaemon.run()` 的抢锁分支 | 接线 | `mock` 掉 `pick_device`/`acquire_singleton_lock` | 失败时返回 4 且**不进入监听**；两种失败文案各一行 |
| `HoldDaemon.start_recording()` / `_deliver()` | 接线 | `mock` `subprocess.Popen`/`check_output`（`_deliver` 链路同时 mock `_spawn_paste`——直接复用 #29 的接缝，n1） | argv 用实例路径、日志含路径、Popen 失败清理、`finally` 清理 |

**刻意不做的事**：不把锁逻辑塞进 `_preflight()`（它是「环境缺失」语义，与「已有实例在跑/锁打不开」是不同类事实，混在一起会让退出码语义含混）；不为了可测性给 `HoldDaemon` 注入新依赖（沿用 `env` dict + `mock` 的既有先例）。

**跨进程验证的防 flaky 设计（B2）**：互斥的真正证明必须有一个**真实第二进程**（同进程内两个 fd 验的是 flock 的 ofd 语义，不是进程语义）。子进程持锁后必须往 stdout 打一行 ready，父进程 `readline` 收到才做断言——否则父进程可能赶在子进程拿锁前抢空锁，断言翻转（pi-agent-board #95 式慢性 flaky 的经典成因）。结束后 kill + `wait()` 回收再断言可重获；整例套 5s 超时保护。已实测：同进程双 fd 二次 flock 抛 `BlockingIOError`，`os.open` 默认 non-inheritable（PEP 446）——两条语义前提均成立。

## 6. 验收矩阵（必答）

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|---|---|---|---|---|
| A1 | 目录/锁路径推导（纯） | 自动化（unit） | `python3 -m unittest test_hold.TestSingletonLock -v` | `$TMPDIR` 有/无/空/相对路径四种分支正确；路径含 uid、以 `.lock` 结尾 |
| A2 | 同进程二次获取被拒（被占路径） | 自动化（unit） | 同上 | 第一次 `(fd, None, None)` 且 `fd is not None`；第二次 `(None, pid, None)` 且 `pid == os.getpid()` |
| A3 | 释放后可重获（无陈旧锁） | 自动化（unit） | 同上 | `os.close(fd)` 之后再获取成功 |
| A4 | **跨进程**互斥（含握手） | 自动化（integration） | 同上：子进程 `sys.executable -c` 持锁后 stdout 打 ready 行，父进程 readline 同步；kill+wait 后再断言 | 子进程持锁期间父进程获取返回 `(None, <子进程pid>, None)`；子进程退出后可获取；整例 ≤5s |
| A5 | **打不开不抛异常**（B1） | 自动化（unit） | 同上：预置 chmod 000 的锁文件；再预置 symlink（O_NOFOLLOW） | 两种情况均返回 `(None, None, err)` 且**无异常**；err 非空 |
| A6 | 锁 fd 不可继承（M3） | 自动化（unit） | 同上：成功获取后 | `os.get_inheritable(fd) is False` |
| A7 | 录音路径唯一化 + Popen 失败清理（M1） | 自动化（unit） | `python3 -m unittest test_hold.TestRecordingPath -v`（mock `Popen`） | 两次调用路径不同、均在注入 tmpdir 下、文件已创建（0 字节）；mock `Popen` 抛 OSError → 该 wav 被 unlink 且异常继续上抛 |
| A8 | `_deliver` 用实例路径并清理 | 自动化（unit） | 同上（mock `check_output` + `_spawn_paste`） | 传给 `transcribe_once.py` 的 argv[2] == `self.wav_path`；结束后该文件已被 unlink |
| A9 | 第二实例退出码与不监听 | 自动化（unit） | `python3 -m unittest test_hold.TestDuplicateInstance -v`（mock 抢锁失败，被占/打不开两种形态） | `run()` 返回 4；`pick_device`/`read_loop` 未被调用；两种文案各恰好一行 |
| A10 | 录音日志含路径 | 自动化（unit） | 同上（mock `Popen` + `redirect_stdout`） | 输出含 `recording -> ` 与本次路径 |
| A11 | static：固定路径归零 + 语法 + unit 钉子（M2） | 自动化（static/build） | `bash -n voice-toggle.sh`；`python3 -m py_compile voice-ptt.py`；`rg -n "voice-input-recording.wav" --glob '!docs/**' --glob '!venv/**'` 与 `rg -n '"/tmp/voice-input-hold.wav"' --glob '!docs/**' --glob '!venv/**'`（两个旧固定名都归零）；`grep -q 'RestartPreventExitStatus=4' contrib/voice-hold.service` | 四项全过：语法零错、**两个**固定录音路径全仓归零（历史 docs 除外）、unit 钉子在位 |
| A12 | 全仓回归 | 自动化（unit） | README:373 钉定套件：`python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v` | 全部通过（含 `TestModulePurity`） |
| U1 | 服务在跑时手工起第二实例 | 用户实测 | 手工执行 `python3 voice_hold.py`，观察 stderr 与 `echo $?` | 打印 `another instance already running (last holder pid <服务pid>)`；退出码 4；**听写功能不受影响**（随即按住右 Alt 说一句仍正常上屏） |
| U2 | systemd 不再重启循环 | 用户实测 | ① 改本机 `~/.config/systemd/user/voice-hold.service` 加 `RestartPreventExitStatus=4` + `systemctl --user daemon-reload`；② 手工实例占锁；③ `systemctl --user restart voice-hold`；④ `journalctl --user -u voice-hold -f` 观察 ≥10s | 只有一条拒绝日志，**无 3s 周期重启**；`systemctl --user status` 显示 failed 而非 activating 循环；停掉手工实例后 `systemctl --user restart voice-hold` 能恢复 |
| U3 | 真机端到端仍正常 | 用户实测 | 按住右 Alt 说一句正常内容；`journalctl --user -u voice-hold -n 5` 看 `recording ->` 行 | 文字**只上屏一次**；日志里的录音路径带随机后缀；该 wav 在上屏后被删除 |

> 本机部署偏差提醒（调研 R2）：`~/.config/systemd/user/` 下的 unit 是手工副本（timeout 120 + drop-in 240），**不能假设改 `contrib/` 就生效**——U2 的步骤①就是为它准备的。

## 7. 风险与已知限制

1. **锁不覆盖的组合场景**（均为「锁失效但路径唯一化仍能防互相删录音」的残余）：① 幽灵与新实例落在**不同 `$TMPDIR`**（用户中途改环境）；② 锁文件被手动删除；③ **不同 uid 的实例并存**（fast-user-switching / root 手工运行）——per-uid 锁对它们不互斥，两个实例会各自转写各自录音 → 仍会重复上屏。单用户笔记本上可接受；诊断入口是日志里的 `recording ->` 路径与锁文件内容。
2. **调试配方变更**：路径唯一化会让 JFox 的「拷固定路径快照」配方失效 → README 与《hold 链路无归档时抓听写音频与归因的调试法》需同步改为「从 journal 读 `recording ->` 路径 / glob `voice-input-hold-*.wav`」。**该文档更新是 post-merge 动作，不改写进本 issue 的代码范围**（README 归 #20）。
3. **U2 会把服务置为 failed 态**：手工实例占锁时服务不再「带病运行」。这是刻意取舍（宁可明确失败也不双份上屏），但用户需知道恢复动作是「停掉手工实例 → `systemctl --user restart voice-hold`」。
4. **`mkstemp` 残留（已收窄）**：录音中被 `kill -9` 会留下一个 wav（`/tmp` 由 systemd-tmpfiles 开机清理）。`arecord` 启动失败路径的泄漏已由 M1 的 try/except 清理收窄；不做主动清扫（避免按 mtime 扫描的复杂度）。

## 8. 改动文件清单

| 文件 | 改动 |
|---|---|
| `voice_hold.py` | 4 个新函数 + 常量；`run()`/`start_recording()`/`_deliver()` 接线；删 `WAVFILE`；`_paste_and_report` docstring 同步改（m2） |
| `test_hold.py` | 3 个新测试类（`TestSingletonLock` A1–A6 / `TestRecordingPath` A7–A8、A10 / `TestDuplicateInstance` A9） |
| `contrib/voice-hold.service` | `RestartPreventExitStatus=4` + 注释（A11 static 钉住） |
| `voice-ptt.py` / `voice-toggle.sh` | 录音路径唯一化（D7；A11 的 py_compile/bash -n 覆盖语法） |
| 文档（post-merge） | JFox 调试笔记 + README 抓音说明（#20 范围） |

## 9. 评审记录

- **2026-09-30 执行期裁决（SDD ledger，代码即裁决结果）**：
  - D6 优于 plan §4.2：被占分支报错文案补上锁路径（`— lock <path>`），holder 缺失时渲染 `unknown`；
  - voice-toggle.sh 改 sidecar 方案（见 §4.2 行内裁决注）；
  - voice-ptt.py `<1KB` 早退补 unlink（镜像 `voice_hold.py` 同款修复）；
  - A11 grep 扩为两个旧固定名（`voice-input-recording.wav` + `/tmp/voice-input-hold.wav`）。
- **2026-09-30 design-gate 前自评（v1→v2）**：
  - **B1**（blocking）：v1 只定义「被占」路径，`/tmp` 被他人预占 0600 文件（实测 EACCES）或 symlink（实测 ELOOP）时行为未定义——OSError 炸 traceback 会让退出码 ≠ 4、`RestartPreventExitStatus` 不命中，**复刻本 issue 要消灭的重启循环**。修正：`acquire_singleton_lock` 永不抛 + `O_NOFOLLOW` + 统一 exit 4（D1/D3/4.1，A5）。
  - **B2**（blocking）：A4 跨进程测试父进程可能赶在子进程拿锁前抢空锁 → 必然 flaky（#95 式）。修正：ready 行握手 + kill/wait + 5s 超时（§5、A4）。
  - **M1**：`arecord` Popen 失败会泄漏 mkstemp 空文件（对旧代码的微小回归）。修正：try/except 清理再抛（4.2，A7）。
  - **M2**：A9 只查字符串不够。修正：补 `py_compile`/`bash -n` + contrib unit 的 `RestartPreventExitStatus` static 钉子（A11）。
  - **M3**：锁 fd 不可继承是从「转写子进程挂死」场景推出的正确性属性。修正：写进 4.1 契约 + A6 断言。
  - **m1–m4/n1**：PID 文案加 `last holder` 限定（D6）；`_paste_and_report` docstring 连带改（4.2/§8）；`$TMPDIR` 相对路径校验（D9/A1）；多 uid 场景入已知限制（§7.1）；A8 mock `_spawn_paste`（§5）。
  - **实测背书**：同进程双 fd 二次 `flock` → `BlockingIOError`；`os.open` 默认 non-inheritable（PEP 446）；`engine.child_env` cpu 引擎纯 passthrough（A8 mock 链路可行）；`test-mic.sh` 用独立路径不在冲突域；README 无日志样例/退出码钉子（D8 无文档连带）。
