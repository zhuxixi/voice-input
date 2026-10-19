# Spec: hold 全屏可见反馈状态机 + layer-shell 浮层（issue #24）

Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-18T22:32:00+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-18T22:53:00+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-18T23:00:00+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-18T23:08:00+08:00

- Issue: zhuxixi/voice-input#24（Umbrella #14；来自 #18 真机验收反馈）
- Base: main @ d6b7821
- Type: UX 增强（主循环重构 + 浮层状态机 + layer-shell）
- 调研: `~/.claude/github-issue-driven/zhuxixi/voice-input/issue-24/research/current-architecture-and-scope.md`

## 1. 背景与根因

现状有两个独立问题，必须一起解：

1. **全屏下完全看不到浮层**（2026-10-18 用户实测）：用户在 **wezterm 全屏** 下工作。
   KDE Wayland 下普通 TOPLEVEL 窗口压不住全屏窗口（`keep_above` 被合成器无视），
   REC 浮层一直藏在后面——用户从未见过任何指示。
2. **转写期零反馈**：`run()` 用阻塞的 `dev.read_loop()`（voice_hold.py:567），
   松手后 `stop_recording()` 藏浮层 → 起 `_deliver` 工作线程 → 主线程立刻回去阻塞，
   转写完成前屏幕没有任何状态；GTK 只能在主线程碰（`_deliver` 不能调 overlay）。

**Spike 已证**：KDE Plasma 6.7.5（KWin 6.7）下，`gtk-layer-shell` 的 `OVERLAY` 层窗口
可盖住全屏 wezterm，且 `keyboard_mode=NONE` 不抢键盘焦点（2026-10-18 21:26 实测通过）。
→ 视觉反馈路线可行。

## 2. 设计

### 2.1 反馈状态机（纯函数）

```python
# states:  "idle" | "recording" | "transcribing" | "done"
# events:  "start" | "stop" | "ok" | "fail" | "done_timeout"
# actions: "show_rec" | "show_transcribing" | "show_done" | "hide" | None
def feedback_transition(state: str, event: str) -> tuple[str, str | None]:
```

| 当前状态 | 事件 | 新状态 | 动作 |
|---|---|---|---|
| idle | start | recording | show_rec |
| recording | stop | transcribing | show_transcribing |
| transcribing | ok | done | show_done |
| transcribing | fail | idle | hide |
| done | done_timeout | idle | hide |
| done | start | recording | show_rec（DONE 未闪完即开始下一句） |
| 其它组合 | — | 不变 | None |

- `start/stop` 事件由按键处理路径在调用 `start_recording()/stop_recording()` 时同步投递；
  `busy` 期间按键丢弃语义**不变**（`transition()` 原样）。既有
  `start_recording()`/`stop_recording()` 内的 `overlay.show("● REC")`/`overlay.hide()` 直调
  （voice_hold.py:314/:346）**删除**——浮层唯一驱动点是 `_tick` 应用状态机动作（否则松手时
  hide→show(转写中…) 会闪烁，并让 A4 的调用断言语义漂移）。
- `ok/fail` 由 `_deliver` 工作线程经队列投递（见 2.4）；`fail` 覆盖：无语音、转写超时、
  转写异常、录音过短、**上屏失败/上屏超时**。
- `ok` 只在文本真正上屏后投递：`_paste_and_report(text)` 由「只打日志」改为返回 `bool`
  （`True` = `paste.py` exit 0；`False` = rc≠0 或超时），既有日志行为不变（待新增接口，见 A4）。
- 语义：失败不弹错误提示（journal 已有可诊断日志），直接隐藏。

### 2.2 主循环改造

`run()` 的 `for event in dev.read_loop()` 替换为：

```python
import select
while True:
    for event in iter_events(dev, TICK_S, select.select):   # TICK_S = 0.2
        if event.type == EV_KEY and event.code == KEY_RIGHTALT:
            self.handle_key_value(event.value)
    self._tick()          # 泵 GTK + 应用反馈状态 + 处理 DONE 死线
```

- `iter_events(dev, timeout, select_fn)`：模块级函数（I/O 接缝，可注入）——
  `select_fn([dev.fd], [], [], timeout)`；就绪时以非阻塞方式读完全部 pending 事件
  （把 fd 设为非阻塞或使用 evdev 的非阻塞读取 API，以实测语义为准；
  捕获 `BlockingIOError`/空读），超时返回空列表。
  按键延迟不变（select 有事件立即醒）；evdev 断连的 OSError 继续向上传播 →
  systemd 重启，语义不变。
- `_tick()`：① 消费工作线程队列 → 状态机事件；② 应用状态机（overlay 变化才调用 GTK）；
  ③ `done` 状态超过 0.4s（初始值，U1 观感确认后可调）→ `done_timeout`。
- 既有 `_pump()` 继续承担 GTK 事件泵；`_tick` 只经 `overlay.show()/hide()` 公共入口驱动，
  **不得直接调 `_pump()`**——浮层未初始化/已禁用时 `_pump` 会 `AttributeError`，
  把异常抛进主循环会杀死 daemon（show/hide 内部已有 try → `_disable`）。所有 GTK 调用都在主线程。

### 2.3 浮层（layer-shell）

`_Overlay` 的 `_try_init()` 增加 layer-shell 分支：

```python
OVERLAY_W, OVERLAY_H = 160, 48   # 模块常量（沿用现有窗口尺寸）

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
    if not GtkLayerShell.is_supported():
        raise RuntimeError("layer-shell not supported by this compositor")
    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
    GtkLayerShell.set_namespace(win, "voice-hold-overlay")
    # 居中：按主显示器逻辑几何 + 固定窗口尺寸算边距，锚 LEFT+TOP
    display = win.get_display() or Gdk.Display.get_default()
    geom = display.get_primary_monitor().get_geometry()
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, True)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.LEFT,
                             (geom.width - OVERLAY_W) // 2)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.TOP,
                             (geom.height - OVERLAY_H) // 2)
    GtkLayerShell.set_exclusive_zone(win, 0)
except Exception:
    pass   # 回退：现有普通 GTK 窗口行为（keep_above 等），不得影响录/转写
```

（`Gdk` 从 `gi.repository` 取；`OVERLAY_W/H` 落为模块常量。不引入 `python-cairo`：部署解释器
`/usr/bin/python3`（service:11）上 `import cairo` 实测 ModuleNotFoundError，且 `gi.repository.cairo.Region`
的 boxed 不可直接实例化。）

指针穿透必须在窗口 **realize 之后**设置：`_try_init()` 阶段 `win.get_window()` 为 `None`，
硬设会被上面的 `except Exception: pass` 吞掉，真实路径静默失去穿透（A2 用宽松假件还会全绿）。
因此 `_try_init` 只做 layer-shell 配置，`show()` 在 `show_all()` 之后调用：

```python
def _apply_ptr_passthrough(self):
    """Pass clicks through to the window below; needs a realized window."""
    if not self._layer_shell_active:   # layer-shell 分支启用标记（_try_init 成功时置位）
        return
    gdk_win = self._win.get_window()
    if gdk_win is None:          # not realized yet: retried on the next show()
        return
    try:
        gdk_win.set_pass_through(True)   # GDK 3.24.52 typelib 实测存在
    except Exception as e:       # lose passthrough only; keep the overlay alive
        print(f"[voice-hold] pointer passthrough unavailable: {e}",
              file=sys.stderr)
```

- 每次 `show()` 都幂等重试，因此首次 realize 后即生效；穿透失败只打日志，不调
  `_disable()`（不得因穿透失败丢浮层；A2 覆盖该分支）。

- 尺寸沿用 160×48 无边框；文案与配色：`● REC` 红 `#ff5555` → `转写中…` 琥珀 `#ffaa00`
  → `DONE` 绿 `#55ff55`（初始 0.4s 后隐藏，U1 观感确认后可调）。
- 键盘穿透靠 `keyboard_mode=NONE`，指针穿透靠 realize 后的 `gdk_win.set_pass_through(True)`
  （零 `python-cairo` 依赖）——两者分别覆盖 U2 的键入与点击。
- 残余风险（保证边界）：`set_pass_through` 只做了本机 typelib 存在性与合成器行为之外的验证，
  KWin 6.7 下的实际穿透由 U2 真机确认；若穿透无效，点击症状与 #24 浮层不可见无关，不会误判主修复。
- 非 Wayland 回退靠显式 `is_supported()` 守卫（0.10.1 已实测存在该 API），不依赖库在 X11
  下恰好批出可捕获异常；若库走 C 层 abort，`except Exception` 接不住。
- `GtkLayerShell` 缺失（未装包/非 Wayland）或任何初始化异常 → 静默回退现有行为；
  浮层失败隔离沿用 `_disable()`（录/转写永不依赖浮层）。
- 已知限制：多显示器只锚主屏（非目标）。

### 2.4 工作线程 → UI 通道

- `_deliver` 只在结束时投递一条消息到 `queue.SimpleQueue`：
  `("ok",)` **仅当 `_paste_and_report` 返回 True（文本确实上屏）**；
  其余所有路径（无语音、录音过短、转写超时/异常、上屏失败/超时）投递 `("fail",)`。
  `finally: busy=False` 不变。
- **单一 FIFO 通道**：`start`/`stop`/`ok`/`fail` 共用同一个 `queue.SimpleQueue`（按键侧与
  工作线程侧同队）——保证工作线程的 `ok` 不会先于同一句的 `stop` 被消费（双队列下
  `recording + ok` 会命中「其它组合」被静默丢弃，浮层卡在 REC）。
- **投递先于起线程**：`stop` 事件必须在 `stop_recording()` 启动 `_deliver` 工作线程之前入队
  （voice_hold.py:348 是该方法最后一步），不得依赖 `_deliver` 首行 `time.sleep(0.3)`
  这个未声明的偶然余量；否则重构缩短刷盘等待会让 `ok` 抢先入队，静默重现卡 REC。
- 主线程 `_tick()` 用非阻塞 `get_nowait()` 消费；队列为空不做事。
- `_deliver` 绝不触碰 GTK（保持既有约束）。

### 2.5 边界与错误

| 场景 | 行为 |
|---|---|
| 浮层初始化失败/运行中异常 | `_disable()` 静默禁用（既有），录/转写继续 |
| 无语音/录音过短/转写超时 | 状态机 `fail → hide`，journal 日志照旧 |
| 上屏失败/paste 超时 | `fail → hide`；`paste.py exit N` / `timed out` 日志照旧，**不显示 DONE** |
| 浮层可见期间鼠标点击 | realize 后 `set_pass_through(True)` → 点击穿透到下层窗口 |
| 非 Wayland（X11） | `GtkLayerShell.is_supported()` 为 False → 抛异常 → 回退普通窗口 |
| DONE 期间用户开始新听写 | `done + start → recording`，正常显示 REC |
| 转写中按键 | `busy` 丢弃语义不变（既有 `transition()`） |
| evdev 拔插 | `iter_events` 抛 OSError → 进程退出 → systemd 重启（既有语义） |
| 长时间无事件 | select 每 0.2s 超时 → `_tick()` 照常泵 GTK（DONE 死线依赖它） |

### 2.6 非目标

- 动态点动画/进度条（先静态文案；如需再加）。
- 声音提示（设计默认：只用视觉通道；U1 实测时请用户确认观感）。
- 多显示器锚定、浮层尺寸/字体可配置。
- KWin 窗口规则、gtk-layer-shell 之外的实现。
- terms 热词（#27 已完成）；newgrp 排障文档（另立）。

### 2.7 文档改动

- `README.md` / `README.zh-CN.md` 双语的依赖说明各补一条：`gtk-layer-shell`（Arch `extra`，
  0.10.1 实测）用于全屏可见浮层；缺失或非 Wayland 时静默回退普通窗口（见 A6）。

## 3. 验收矩阵

### 3.1 真机验收部署前置（U1/U2 共用）

systemd 单元 `contrib/voice-hold.service:11` 的 `ExecStart` 指向主 checkout
（`%h/work/git-repo/voice-input/voice_hold.py`）。合并前实测必须在主 checkout 部署 PR 分支，
否则测到的是旧代码：

1. 主 checkout 切到 PR 分支：`git -C ~/work/git-repo/voice-input fetch origin && git checkout issue-24-<slug>`（分支名以实际 PR 为准）。
2. `systemctl --user restart voice-hold`（进程启动时读取当前文件，不重启仍是旧代码）。
3. `journalctl --user -u voice-hold -n 20` 确认新启动日志无异常。
4. 按 U1/U2 实测。
5. 测完 `git checkout main`；服务继续运行分支代码直到下次重启，合并后的正常重启回到 main。

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | 状态机纯函数 | 自动化（unit） | `python3 -m unittest test_hold.TestFeedbackTransition -v` 新增：全部 6 条显式转移 + 未知事件保持 | 断言通过；未知事件返回 `(state, None)` |
| A2 | layer-shell 初始化与回退 | 自动化（unit，注入假 Gtk/GtkLayerShell 模块） | `python3 -m unittest test_hold.TestOverlayLayerShell -v` 新增：调用顺序/参数（OVERLAY、keyboard NONE、namespace、anchors、margins、exclusive 0）；假 win 复现 realize 时序（`get_window()` 在 `show_all()` 前返回 None、之后返回记录器），断言 `_apply_ptr_passthrough` 在 `show_all()` 后调用 `set_pass_through(True)` 且未 realize 时不崩；**穿透调用抛异常时只输出一行 stderr、`_dead` 保持 False、后续 `show()` 仍正常**；`is_supported()` False 或注入的 layer-shell stub 抛异常时回退普通窗口且 `_inited=True` | 断言通过；回退路径不抛异常；穿透失败不丢浮层 |
| A3 | 事件迭代器 `iter_events` | 自动化（unit，fake dev + fake select） | `python3 -m unittest test_hold.TestIterEvents -v` 新增：超时返回 `[]`；就绪时读完全部 pending；OSError 原样传播 | 断言通过 |
| A4 | 工作线程通道 + `_tick` | 自动化（unit/integration，overlay Mock） | `python3 -m unittest test_hold.TestDeliverTick -v` 新增：`_deliver` 的 ok/fail/异常路径均投递且 `busy=False`；**paste 失败/超时投递 fail 且不显示 DONE**；`_tick` 消费队列并驱动 `feedback_transition`；DONE 超时触发 `done_timeout`；`overlay.show` 抛异常 → `_disable` 后 `_tick` 不抛、后续空转；`handle_key_value` 投递侧：press/release 后状态机事件队列收到 `start`/`stop`（队列可注入）；单队列跨生产者顺序：先投 `stop` 再投 `ok`，两次 `_tick` 后状态为 `done`；`stop_recording` 不再直调 overlay | 断言通过 |
| A5 | 回归 | 自动化（integration） | README.md:386 的 9 模块全量命令 | 全绿 |
| A6 | README 依赖说明 | 自动化（static，手动跑） | `rg -q gtk-layer-shell README.md && rg -q gtk-layer-shell README.zh-CN.md`，并各含回退说明 | 两份 README 分别命中 |
| U1 | 真机全屏全链路 | 用户实测（按 §3.1 部署前置） | 主 checkout 部署 PR 分支并重启后，wezterm 全屏：按住右 Alt 说话松手 | REC 居中可见 → 转写中… → 上屏后绿色 DONE 约 0.4s → 消失；全程不丢按键 |
| U2 | 不抢焦点/不吞点击 | 用户实测（按 §3.1 部署前置） | 浮层出现期间在终端继续键入；再点击浮层所在位置 | 输入不丢、无焦点跳变；点击落到下层终端 |
| U3 | 回退路径（可选，允许合并后补验） | 用户实测 | 前提：PR 分支仍在 checkout（与 U1/U2 同场，§3.1 步骤 5 之前）或 PR 已合入 main。临时 `pacman -R gtk-layer-shell` → `systemctl --user restart voice-hold` 验证回退；测完 `sudo pacman -S gtk-layer-shell && systemctl --user restart voice-hold` 恢复 | 回退时录/转写正常；恢复后浮层重新盖住全屏（复用 U1 一轮） |

U1/U2 合并前必须通过；U3 允许合并后补验（自动化 A2 已覆盖逻辑）。

## 4. 可测性拆分设计（自动化项的实现约束）

- **A1 边界（unit）**：`feedback_transition(state, event)` 模块级纯函数，输入/输出全是
  str/tuple，无 I/O、无全局状态——直接断言，沿用 `transition()` 的测试风格。
- **A2 边界（unit）**：`_Overlay` 构造签名增加可选注入
  `_Overlay(gtk_module=None, layer_shell_module=None)`；默认 None 时保持现有 lazy import
  （构造零 GTK 副作用，`TestOverlayLazyInit` 继续通过）。测试用假模块记录调用并断言
  参数（假模块需带 `Layer`/`KeyboardMode`/`Edge` 常量与 `is_supported()`；假 win 复现
  真实 realize 时序：`get_window()` 在 `show_all()` 前返回 None、之后返回假 gdk window；
  假 win 另提供 `get_display()` → 假 display（`get_primary_monitor()` → 假 monitor
  （`get_geometry()` → 带 width/height 的对象）），使居中链路全程走假件；假 gdk window
  记录 `set_pass_through(True)` 调用且可配置 `side_effect` 抛异常），不启动真实 GTK 主循环。
- **A3 边界（unit）**：`iter_events(dev, timeout, select_fn)` 把 select 与设备都做成参数；
  测试用假 dev（`fd`、可排队的 `read()` 序列、可抛 OSError）和假 select（返回
  `(rlist, [], [])` 或空）驱动，不碰真实 evdev。
- **A4 边界（unit/integration）**：`_deliver` 与 `_tick` 之间只通过
  `queue.SimpleQueue` 通信；测试直接调用 `_tick()` + `overlay=Mock()`，断言
  `show_done/hide` 的调用与时机（注入 clock 或仅断言状态）；`_deliver` 的 ok/fail
  用 mock 的转写入口驱动，`_paste_and_report` 的 bool 返回值（待新增）mock 成
  成功/失败两路；`overlay.show` 抛异常用 Mock 的 side_effect 驱动；`handle_key_value`
  投递侧用可注入的事件队列断言 `start`/`stop`；穿透失败分支同样用 side_effect 驱动，
  断言只打日志且 `_dead` 保持 False。
- **A5 边界（回归）**：`test_hold.py` 既有 overlay/transition 断言保持；新增测试放同文件。
- 实现不得把状态机内联进 `_tick`（会丢 A1 边界），也不得让工作线程直接调 overlay。
