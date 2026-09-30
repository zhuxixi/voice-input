# #29 Spec：hold 链路上屏假超时修复（去管道 + 丢弃可观测）

Issue: zhuxixi/voice-input#29 · 日期：2026-09-30 · 状态：**approved（2026-09-30 用户确认；三轮交叉复核后定稿）**

## 背景与根因（一句话）

`voice_hold._deliver()` 用 `stderr=subprocess.PIPE` 等待 paste.py，而 wl-copy 派生的常驻剪贴板进程继承管道写端使 EOF 永不到来 → 30s 假超时 → 忙态按键被静默丢弃 35–40s（用户感受「按住 Alt 没反应」，24h 内 51% 命中）。完整机制与证据见 issue #29 正文及 `research/root-cause-and-contracts.md`。

## 设计决策

**D1 · 去管道（根因修复）**
`_deliver()` 的 paste 子进程调用去掉 `stderr=subprocess.PIPE`，stderr 继承直达 journal。paste.py 的错误输出自带 `[voice-input]` 前缀，可观测性不降。
（拒绝的替代：在 paste.py 侧给 wl-copy 关 fd——wl-copy 无此选项；改用 `wl-copy -f` 前台化——反而把常驻语义拉进前台，更差。）

**D2 · 超时收紧为模块常量**
新增 `PASTE_TIMEOUT = 10`（模块级，带注释：实测上屏 <0.2s，10s 已 50 倍余量；30s 的唯一效果是把假超时代价放大三倍）。不设环境变量旋钮——超时不是用户可调语义，暴露旋钮只会让人调回 30s 复现 bug。
（拒绝 1s：慢盘首次起 python 解释器与 ydotool 偶发抖动下太紧，误杀真成功。）

**D3 · 失败分支适配**
stderr 不再捕获后，`proc.stderr` 不存在；失败分支改打定稿文案（实现照抄）：
`[voice-hold] paste failed: paste.py exit {rc}`
stderr 细节由 paste.py 自己进 journal（自带 `[voice-input]` 前缀）。

**D4 · 超时文案去误导**
现文案 `is ydotoold responsive?` 已被证伪（ydotool 单测 0.03s），改为指向 journal 中 paste.py 的输出与 wl-copy 派生行为说明。定稿文案（实现照抄，不再现场设计）：
`[voice-hold] paste timed out after %ds (hung child killed) — paste.py output should be right above; if it isn't, check ydotoold liveness (#29)`

**D5 · 忙态丢弃可观测（不动语义）**
`transition()` 的忙态丢弃语义是 #18 v3 评审决策（防幽灵录音），**保留**。新增：`handle_key_value()` 在丢弃发生且 `value == _PRESS` 时打一行定稿文案（实现照抄）：
`[voice-hold] key press dropped while busy (transcribe/paste in flight)`
`_REPEAT` 与 `_RELEASE` 不打（长按 repeat 每秒几十条，必须静默）。判定抽为模块级纯函数 `is_dropped_press(value, was_busy) -> bool`（与 `transition` 同层，零副作用，可独立测试）。

**D6 · 同构风险挂账（只注释不改行为）**
`_deliver()` 的转写调用 `check_output(..., stderr=sys.stderr, timeout=…)` 同样等 stdout 管道 EOF；当前引擎链路不派生常驻进程，未观察到问题。加注释注明该约束及 #29 机制，防未来引擎改动踩坑。

## 非目标

- 不改 `transition()` 忙态丢弃语义（A3 回归钉住）。
- 不改 paste.py（x11 字节等价契约 pinned by test_paste.py，且其行为正确）。
- 不处理 #28（flock 单实例锁）与「设备认错」问题（用户明确：本 issue 单独做）。
- 不做 #25（常驻转写 worker）。

## 可测性拆分设计（实现硬约束）

1. **副作用隔离（三层接缝）**：
   - `HoldDaemon._spawn_paste(self, text) -> (rc|None, timed_out)`：唯一触碰 subprocess 的 paste 点；内部捕获 `TimeoutExpired` 返回 `(None, True)`（`subprocess.run` 超时时已自行杀掉子进程，"hung child killed" 语义不变）。
   - `HoldDaemon._paste_and_report(self, text)`：调用 `_spawn_paste`，分支打日志（超时文案/失败 exit N/成功 `delivered:`）——A4/A5 直接测它，依赖仅 `_spawn_paste` 一个。
   - `_deliver`：转写编排 + 调用 `_paste_and_report`，不再直接触碰 paste 的 subprocess。
   **为什么必须有第二层接缝**：直接驱动 `_deliver` 需要 sleep 0.3s、伪造固定路径 `/tmp/voice-input-hold.wav`（全局路径，与本机在跑的 daemon 相撞）、patch `check_output`——测试面扩大到整个转写编排，脆且慢；接缝把 A4/A5 缩到最小测试面。
   A1 测试经 `with mock.patch.object(voice_hold.subprocess, "run", ...) as m:` 捕获 kwargs 断言契约（**必须 context manager**：patch 的是共享 subprocess 模块的属性，用例结束需自动恢复，避免污染同进程其他测试）。
2. **纯函数**：新增模块级 `is_dropped_press(value, was_busy) -> bool`（D5 的判定，与 `transition` 同层、零副作用，可独立测试）；`handle_key_value` 只负责调用它并打日志。日志断言用 `contextlib.redirect_stderr`（代码内 `file=sys.stderr` 在调用时求值，重定向生效）。
3. **常量**：`PASTE_TIMEOUT` 模块级，测试直接断言 `<= 10`。
4. **测试边界**：不新增依赖（mock/contextlib 均为 stdlib）；沿用 test_hold.py 的 unittest 风格；模块纯度契约（stdlib-only import）不受影响（mock 只进测试文件）。

## 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | paste 子进程调用无 stdout/stderr 管道、timeout=PASTE_TIMEOUT、argv 不变、文本经 `input=` 走 stdin（不入 argv，防中文/长度问题） | 自动化（unit） | `python3 -m unittest test_hold.TestPasteSpawnContract -v`（mock 捕获 subprocess.run kwargs） | kwargs 无 `stdout=/stderr=` 管道，timeout==10，`input==text.encode()`，argv==`[sys.executable, <REPO>/paste.py]` |
| A2 | 忙态丢弃 press 打一行日志，repeat/release 静默 | 自动化（unit） | `python3 -m unittest test_hold.TestBusyDropLogging -v`（redirect_stderr） | press 恰一行、repeat/release 零行，丢弃语义不变 |
| A3 | 既有状态机/设备/纯度及全仓行为回归 | 自动化（unit） | README:363 钉定的全量套件：`python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v` | 全部通过（含 `test_press_while_busy_dropped`） |
| A4 | 超时文案不再指向 ydotoold；PASTE_TIMEOUT 上界 | 自动化（unit） | TestPasteSpawnContract：fake `_spawn_paste` 返回 `(None, True)` 驱动 `_paste_and_report`，redirect_stderr 捕获输出；另断言模块常量 | 输出含 "timed out after" 且不含 "ydotoold responsive"，`PASTE_TIMEOUT <= 10` |
| A5 | 结果分支：失败打 `paste.py exit {rc}` 且不读 proc.stderr；成功打 `delivered:` | 自动化（unit） | TestPasteSpawnContract：fake `_spawn_paste` 分别返回 `(3, False)` 与 `(0, False)` 驱动 `_paste_and_report` | 失败：stderr 恰一行含 `exit 3`、无 `delivered:`、无 AttributeError；成功：恰一行 `delivered:` |
| U1 | 真机端到端 | 用户实测 | merge 后：主 checkout `git -C ~/work/git-repo/voice-input pull`（systemd 单元 ExecStart 指向主 checkout，不 pull 测的是旧代码）→ `systemctl --user restart voice-hold` → 连续听写 ≥10 次 → 看 `journalctl --user -u voice-hold` | `paste timed out` 归零、每次均有 `delivered:`；转写期间按 Alt 能看到 drop 日志。同时承担「51% 超时是否另有成因」的甄别角色 |

注：D6（转写调用的管道 EOF 约束注释）为文档性改动，**不做自动化验证**——注释存在性不构成可执行行为，其正确性由 #29 正文机制描述支撑。

## 风险与回滚

- 改动集中在 `voice_hold.py` 一个文件 + `test_hold.py` 测试；无接口变更、无依赖变更。
- **无新增共享状态**：`_paste_and_report` 仅在工作线程内调用并打日志；`is_dropped_press` 纯函数；主线程/工作线程边界不变。
- **README 无需变更（已核对）**：README 仅记载 `VOICE_INPUT_TRANSCRIBE_TIMEOUT`，无任何 paste 超时/等待相关描述，不会因本次改动陈旧。
- 主要残余风险：若 51% 超时中另有成因（复现间歇性所致），A1 修复后超时不归零——U1 即为甄别手段；届时按 journal 新日志（D4/D5 后信息量大增）另立调查。
- 回滚：revert 单个 commit 即可。
