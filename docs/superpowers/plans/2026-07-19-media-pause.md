# 录音自动暂停媒体 Implementation Plan — issue #3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按右 Command 键开始录音时自动暂停正在外放的 MPRIS 媒体（用户的 Chrome 音乐/视频），录音结束（arecord 停止）后自动恢复。

**Architecture:** 新建纯逻辑模块 `media_pause.py`（MPRIS via Gio D-Bus，零新依赖，复用项目已 import 的 `gi`）。`_select_to_pause` 是可单测的纯函数；`pause_playing()`/`resume()` 是 Gio 薄封装（手工集成验证，同 PR #2 的 GTK/arecord 部分）。`voice-ptt.py` 在 `start_recording()` 启动 arecord 前调 `pause_playing()`、`stop_recording()` arecord 停后调 `resume()`，各包独立 try/except 降级，绝不阻断录音/转写/粘贴。

**Tech Stack:** Python 3.12（系统 `/usr/bin/python3`，有 `gi`）、PyGObject `Gio`/`GLib`（D-Bus）、MPRIS2、unittest。

**Spec:** `docs/2026-07-19-media-pause-design.md`（commit d270f14）

## Global Constraints

- **运行 Python = 系统 `/usr/bin/python3`**（有 `gi`）；venv 的 python **没有** gi。`voice-ptt.sh` 已用 `/usr/bin/python3` + `PYTHONPATH` 指向 venv site-packages（faster-whisper/nvidia 来自 venv，gi/pynput 来自系统）。测试也用 `python3`。
- **零新依赖**：只复用 `gi`（Gio/GLib），不引入 `playerctl`、不裸发媒体键。
- **降级铁律**：媒体暂停/恢复是增强，任何故障（无 MPRIS / D-Bus 异常 / 单播放器失败）不得阻断录音/转写/粘贴。
- **非 toggle**：用 MPRIS `Pause`（幂等，不会启动已停止的播放器）+ 只 `Play`「我们暂停过的」播放器——绝不误启动。
- **接入点**：`start_recording()` 在 `subprocess.Popen(["arecord", ...])` **之前**暂停；`stop_recording()` 在 `rec_proc.terminate()/wait()` **之后**恢复。
- **`git add` 按文件**，禁止 `git add -A`（仓库有 untracked 的 `recordings/`、`docs/2026-07-18-term-correction-design.md` 等，不能 sweep 进 commit）。
- **MPRIS Gio 调用已实测可用**（原型验证）：`Gio.bus_get_sync(Gio.BusType.SESSION, None)` 拿连接；`ListNames` 用 `call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", None, GLib.VariantType.new("(as)"), ...)`，结果 `res.unpack()[0]` 是名字列表；`Get PlaybackStatus` 用 `(ss)` 参数 + `(v)` 回复，`res.unpack()[0]` 是状态字符串；`Pause`/`Play` 用 `None` 参数 + `None` 回复。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `media_pause.py` | MPRIS 暂停/恢复：纯函数 `_select_to_pause` + 常量 + Gio 薄封装 `pause_playing`/`resume`。仅依赖 `os`/`sys`/`gi(Gio,GLib)`，无 pynput/GTK，import 无副作用（D-Bus 只在函数内连）。 | 新建 |
| `test_media_pause.py` | `_select_to_pause` 纯函数单测（unittest，对齐 `test_archive.py`）。 | 新建 |
| `voice-ptt.py` | 接入：import + `_paused_players` 全局 + start/stop 调用（独立 try 降级）。 | 改 |
| `README.md` | 功能列表 + 「媒体自动暂停」小节 + 文件表 `media_pause.py` 行 + `VOICE_INPUT_PAUSE_MEDIA` 开关。 | 改 |

---

## Task 1: `media_pause.py` — 纯函数 `_select_to_pause` + 常量（TDD）

**Files:**
- Create: `media_pause.py`
- Test: `test_media_pause.py`

**Interfaces:**
- Produces: `_select_to_pause(statuses: dict[str, str]) -> list[str]`（纯函数，输入 `{bus_name: PlaybackStatus}`，返回 `"Playing"` 的名字列表）；常量 `PAUSE_MEDIA_ENABLED`。

- [ ] **Step 1: 写失败测试 `test_media_pause.py`**

```python
import unittest

from media_pause import _select_to_pause


class TestSelectToPause(unittest.TestCase):
    def test_picks_only_playing(self):
        self.assertEqual(
            _select_to_pause({"a": "Playing", "b": "Paused", "c": "Stopped"}),
            ["a"],
        )

    def test_empty(self):
        self.assertEqual(_select_to_pause({}), [])

    def test_all_playing(self):
        names = _select_to_pause({"a": "Playing", "b": "Playing"})
        self.assertEqual(sorted(names), ["a", "b"])

    def test_none_playing(self):
        self.assertEqual(
            _select_to_pause({"a": "Paused", "b": "Stopped"}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_media_pause -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'media_pause'`（模块还没建）。

- [ ] **Step 3: 建 `media_pause.py`（纯函数 + 常量，此步不 import gi）**

```python
"""录音时自动暂停/恢复 MPRIS 媒体(用户的 Chrome 音乐/视频)。

纯逻辑(本文件的 _select_to_pause)仅依赖标准库,可独立单元测试;
Gio D-Bus 薄封装 pause_playing/resume 依赖 gi(系统 python3 自带,venv 无),
import 本模块本身不连 D-Bus(连接只在函数内发生),无副作用。
"""

import os
import sys

PAUSE_MEDIA_ENABLED = os.environ.get("VOICE_INPUT_PAUSE_MEDIA", "1") != "0"


def _select_to_pause(statuses):
    """纯函数:给定 {bus_name: PlaybackStatus},返回状态为 "Playing" 的 bus name 列表。

    Args:
        statuses: {bus_name(str): PlaybackStatus(str, "Playing"/"Paused"/"Stopped")}

    Returns:
        list[str]: 状态 == "Playing" 的 bus name(保持插入序,确定性)。
    """
    return [name for name, status in statuses.items() if status == "Playing"]
```

> `sys` 本步暂未用到,但 Task 2 的 `print(..., file=sys.stderr)` 会用;先 import 避免后续改 import 块。可接受(对齐 archive.py 顶部一次性 import 风格)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_media_pause -v`
Expected: PASS — 4 个 test 全过（`Ran 4 tests ... OK`）。

- [ ] **Step 5: 提交**

```bash
git add media_pause.py test_media_pause.py
git commit -m "feat(media_pause): 纯函数 _select_to_pause + 常量 (issue #3)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `media_pause.py` — Gio D-Bus 层 + `pause_playing`/`resume`

**Files:**
- Modify: `media_pause.py`（加 `gi` import + Gio 辅助 + 两个公开函数）

**Interfaces:**
- Consumes: Task 1 的 `_select_to_pause`、`PAUSE_MEDIA_ENABLED`（voice-ptt 读常量,本任务函数本身不 gate——gate 在调用方）。
- Produces: `pause_playing() -> list[str]`、`resume(names: list[str]) -> None`。两者对 D-Bus 故障**不抛**（内部捕获,返回 `[]` / per-name 跳过 + 记 stderr）。

> **测试策略说明（同 PR #2 范式）**：纯决策逻辑已在 Task 1 单测覆盖。Gio D-Bus 调用依赖活跃 session bus + 真实播放器,无法在无 D-Bus 的单测环境里干净地 mock（且 mock Variant 构造脆弱）,故本任务用**只读 smoke 验证**（对活的总线枚举 + 读状态,不调 Pause/Play 不改状态）;Pause/Play 的真实效果由 Task 5 手工端到端验证。这与 voice-ptt.py 里 arecord/xdotool 调用「py_compile + 手工验证」一致。

- [ ] **Step 1: 给 `media_pause.py` 加 gi import**

在文件顶部 import 区（`import sys` 之后）加:

```python
from gi.repository import Gio, GLib
```

- [ ] **Step 2: 加 MPRIS 常量块**

在 `PAUSE_MEDIA_ENABLED = ...` 之后加:

```python
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"
```

- [ ] **Step 3: 加 Gio 辅助函数**

在 `_select_to_pause` 之后加:

```python
def _session_bus():
    """获取 session bus 连接。失败抛 GLib.Error(由调用方捕获)。"""
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _list_mpris_players(bus):
    """枚举 session bus 上所有 org.mpris.MediaPlayer2.* 播放器 bus name。"""
    res = bus.call_sync(
        DBUS_NAME, DBUS_PATH, DBUS_IFACE, "ListNames",
        None, GLib.VariantType.new("(as)"),
        Gio.DBusCallFlags.NONE, -1, None,
    )
    return [n for n in res.unpack()[0] if n.startswith(MPRIS_PREFIX)]


def _playback_status(bus, name):
    """读某播放器的 PlaybackStatus。成功返回 str,失败返回 None(跳过该播放器)。"""
    try:
        res = bus.call_sync(
            name, PLAYER_PATH, PROPS_IFACE, "Get",
            GLib.Variant("(ss)", (PLAYER_IFACE, "PlaybackStatus")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE, -1, None,
        )
        return res.unpack()[0]
    except GLib.Error as e:
        print(f"[media_pause] read status {name} failed: {e}", file=sys.stderr)
        return None


def _call_player_method(bus, name, method):
    """调 Player 的无参方法(Pause/Play)。失败抛 GLib.Error(由调用方捕获)。"""
    bus.call_sync(
        name, PLAYER_PATH, PLAYER_IFACE, method,
        None, None,
        Gio.DBusCallFlags.NONE, -1, None,
    )
```

- [ ] **Step 4: 加公开函数 `pause_playing` / `resume`**

在 `_call_player_method` 之后加:

```python
def pause_playing():
    """暂停所有当前 "Playing" 的 MPRIS 播放器,返回被暂停的 bus name 列表。

    对 D-Bus 故障不抛(枚举失败返回 []);单播放器 Pause 失败跳过 + 记 stderr。
    调用方(voice-ptt)拿到返回值后,在 stop 时 resume 这些名字。
    """
    try:
        bus = _session_bus()
        names = _list_mpris_players(bus)
    except GLib.Error as e:
        print(f"[media_pause] enumerate failed: {e}", file=sys.stderr)
        return []
    statuses = {}
    for name in names:
        status = _playback_status(bus, name)
        if status is not None:
            statuses[name] = status
    paused = []
    for name in _select_to_pause(statuses):
        try:
            _call_player_method(bus, name, "Pause")
            paused.append(name)
        except GLib.Error as e:
            print(f"[media_pause] Pause {name} failed: {e}", file=sys.stderr)
    return paused


def resume(names):
    """恢复(Play)给定的 bus name 列表。逐个 best-effort,失败记 stderr,不抛。"""
    if not names:
        return
    try:
        bus = _session_bus()
    except GLib.Error as e:
        print(f"[media_pause] resume bus failed: {e}", file=sys.stderr)
        return
    for name in names:
        try:
            _call_player_method(bus, name, "Play")
        except GLib.Error as e:
            print(f"[media_pause] resume {name} failed: {e}", file=sys.stderr)
```

- [ ] **Step 5: py_compile 校验语法**

Run: `python3 -m py_compile media_pause.py`
Expected: 无输出（语法 OK）。

- [ ] **Step 6: 只读 smoke 验证（对活的总线,不调 Pause/Play）**

Run:
```bash
python3 -c "from media_pause import _session_bus, _list_mpris_players, _playback_status; b=_session_bus(); ps=_list_mpris_players(b); print('players:', ps); print('statuses:', {n: _playback_status(b, n) for n in ps})"
```
Expected: 打印 `players: ['org.mpris.MediaPlayer2.chromium.instanceXXXXX']`（实例号会变）+ `statuses: {...: 'Paused'}` 或 `'Playing'`（取决于当时 Chrome 是否在放）。**关键**：能列出 Chrome、能读出状态字符串，即读路径通。无播放时返回 `[]` 也可（说明此时 pause_playing 会 no-op）。

- [ ] **Step 7: 回归 Task 1 单测（确认改动没破坏纯函数）**

Run: `python3 -m unittest test_media_pause -v`
Expected: PASS — 4 个 test 仍全过。

- [ ] **Step 8: 提交**

```bash
git add media_pause.py
git commit -m "feat(media_pause): Gio D-Bus 层 pause_playing/resume (issue #3)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `voice-ptt.py` 接入

**Files:**
- Modify: `voice-ptt.py:19`（import）、`voice-ptt.py:33-36`（全局）、`voice-ptt.py:89-107`（start_recording）、`voice-ptt.py:110-120`（stop_recording）

**Interfaces:**
- Consumes: Task 2 的 `pause_playing` / `resume` / `PAUSE_MEDIA_ENABLED`。

> **测试策略**：voice-ptt.py 顶部 `import gi` + GTK 初始化，无法在无 display 环境 import 运行，用 `python3 -m py_compile voice-ptt.py` 校验语法（同 PR #2）。真实按键集成由 Task 5 手工验证。

- [ ] **Step 1: 加 import（L19 后）**

当前 L19：
```python
from archive import archive_recording, ARCHIVE_ENABLED
```
改为：
```python
from archive import archive_recording, ARCHIVE_ENABLED
from media_pause import pause_playing, resume, PAUSE_MEDIA_ENABLED
```

- [ ] **Step 2: 加全局 `_paused_players`（L33-36 块）**

当前：
```python
recording = False
rec_proc = None
active_window = None
overlay_window = None
```
改为：
```python
recording = False
rec_proc = None
active_window = None
overlay_window = None
_paused_players = []  # 录音开始时被暂停的 MPRIS 播放器 bus name,stop 时 resume
```

- [ ] **Step 3: `start_recording()` 启动 arecord 前暂停（L89-107）**

当前函数体（删旧 wav 之后、arecord Popen 之前插入）：
```python
def start_recording():
    global recording, rec_proc, active_window
    if recording:
        return
    recording = True
    try:
        active_window = subprocess.check_output(
            ["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        active_window = None
    if os.path.exists(WAVFILE):
        os.unlink(WAVFILE)
    rec_proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-D", "hw:3", WAVFILE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    GLib.idle_add(show_overlay, "● REC", "#ff5555")
    print("[voice-input] Recording...", flush=True)
```
改为（改 global 行 + 插入暂停块）：
```python
def start_recording():
    global recording, rec_proc, active_window, _paused_players
    if recording:
        return
    recording = True
    try:
        active_window = subprocess.check_output(
            ["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        active_window = None
    if os.path.exists(WAVFILE):
        os.unlink(WAVFILE)
    if PAUSE_MEDIA_ENABLED:
        try:
            _paused_players = pause_playing()
        except Exception as pe:
            print(f"[voice-input] pause_media failed: {pe}", file=sys.stderr)
            _paused_players = []
    rec_proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-D", "hw:3", WAVFILE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    GLib.idle_add(show_overlay, "● REC", "#ff5555")
    print("[voice-input] Recording...", flush=True)
```

- [ ] **Step 4: `stop_recording()` arecord 停后恢复（L110-120）**

当前函数头（rec_proc 块之后、`GLib.idle_add(hide_overlay)` 之前插入）：
```python
def stop_recording():
    global recording, rec_proc
    if not recording:
        return
    recording = False
    if rec_proc:
        rec_proc.terminate()
        rec_proc.wait(timeout=2)
        rec_proc = None

    GLib.idle_add(hide_overlay)
    time.sleep(0.3)
```
改为（改 global 行 + 插入恢复块）：
```python
def stop_recording():
    global recording, rec_proc, _paused_players
    if not recording:
        return
    recording = False
    if rec_proc:
        rec_proc.terminate()
        rec_proc.wait(timeout=2)
        rec_proc = None

    if PAUSE_MEDIA_ENABLED and _paused_players:
        try:
            resume(_paused_players)
        except Exception as re:
            print(f"[voice-input] resume_media failed: {re}", file=sys.stderr)
        finally:
            _paused_players = []

    GLib.idle_add(hide_overlay)
    time.sleep(0.3)
```

- [ ] **Step 5: py_compile 校验**

Run: `python3 -m py_compile voice-ptt.py`
Expected: 无输出（语法 OK）。

- [ ] **Step 6: 提交**

```bash
git add voice-ptt.py
git commit -m "feat(voice-ptt): 录音自动暂停/恢复媒体 (issue #3)

start_recording 启动 arecord 前 pause_playing,stop_recording arecord 停后
resume,各包独立 try 降级不阻断主流程。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `README.md` 更新

**Files:**
- Modify: `README.md`（L9-14 功能列表、L57-59 录音归档小节后、L82-89 文件表）

- [ ] **Step 1: 功能列表加一条（L9-14 块内）**

在「**开机自启**」那条**之前**插入（即列表最后一项功能）：
```markdown
- **自动暂停媒体**：录音时自动暂停正在外放的音乐/视频（Chrome 等支持 MPRIS 的播放器），结束后自动恢复
```

- [ ] **Step 2: 加「媒体自动暂停」小节（在「### 录音归档」小节之后，即 L59 之后）**

```markdown
### 媒体自动暂停

录音开始时自动暂停正在外放的媒体（Chrome 等通过 MPRIS 暴露的播放器），避免外放声音被麦克风收进去干扰识别；录音结束（松手后）自动恢复播放。零新依赖（走系统 D-Bus）。关闭：`VOICE_INPUT_PAUSE_MEDIA=0 ./voice-ptt.sh`。
```

- [ ] **Step 3: 文件表加 `media_pause.py` 行（在 `archive.py` 那行之后）**

```markdown
| `media_pause.py` | 录音时自动暂停/恢复 MPRIS 媒体（Chrome 等），走系统 D-Bus，零依赖 |
```

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs: README 加媒体自动暂停说明 (issue #3)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 手工端到端集成验证（合并门，同 PR #2）

> 真实按键 + Chrome + 麦克风 + GUI，自动化做不了。**这是合并前的硬门**。

**Files:** 无（验证现有改动）

- [ ] **Step 1: 关闭自启的旧实例，重启加载新代码**

Run: `pkill -f voice-ptt.py`（杀旧实例）；然后 `./voice-ptt.sh &`（或按你平时方式重启）。
Expected: 终端打印 `[voice-input] Ready! ...`。

- [ ] **Step 2: 场景 A — Chrome 正在放音乐**

在 Chrome 打开一个音乐/视频标签页并**开始播放**。按住右 Command 录一句话（如「测试自动暂停」），松开。
Expected:
1. 按下瞬间 → Chrome 音乐**暂停**
2. 松开 → 转写结果粘贴到当前窗口 + 蓝色 DONE
3. 转写粘贴后 → Chrome 音乐**自动续播**

- [ ] **Step 3: 场景 B — Chrome 没在放（已暂停/无媒体标签）**

让 Chrome 处于**暂停/无播放**状态。按住右 Command 录一句，松开。
Expected: 录音转写正常，**Chrome 不会被误启动播放**（验证非 toggle）。

- [ ] **Step 4: 场景 C — 功能关闭**

Run: `VOICE_INPUT_PAUSE_MEDIA=0 ./voice-ptt.sh &`。Chrome 放音乐，录音。
Expected: 录音期间 Chrome 音乐**继续放**（功能关闭，行为同改造前）。

- [ ] **Step 5: 记录验证结果到 issue**

全通过后，在 issue #3 评论：「手工集成验证通过（场景 A/B/C）」。若任一失败，回到对应 Task 修。

---

## Self-Review

**Spec coverage**：
- §3 机制（MPRIS PlaybackStatus 门控 + Pause/Play）→ Task 2 `_playback_status`/`_call_player_method`/`pause_playing`/`resume` ✓
- §4 组件（media_pause.py + `_select_to_pause` 纯函数 + `PAUSE_MEDIA_ENABLED`）→ Task 1 + Task 2 ✓
- §5 接入点（start 前 pause / stop arecord 停后 resume）→ Task 3 ✓
- §6 降级（独立 try、逐播放器、不阻断）→ Task 2（函数内捕获）+ Task 3（voice-ptt 外层 try）✓
- §7 测试（`_select_to_pause` 单测 + 手工集成）→ Task 1 单测、Task 2 smoke、Task 5 手工 ✓
- §8 文件清单（media_pause.py / voice-ptt.py / test_media_pause.py / README.md）→ Task 1-4 ✓

**Placeholder scan**：无 TBD/TODO；每个代码 step 都是完整代码；命令带 expected 输出。

**Type/签名一致性**：`_select_to_pause(statuses)->list[str]`、`pause_playing()->list[str]`、`resume(names)->None`、`PAUSE_MEDIA_ENABLED` 在 Task 1/2/3 全程一致；voice-ptt 的 `_paused_players: list[str]` 全局在 start 写、stop 读+清空，一致。

**与 PR #2 范式对齐**：纯函数单测（test_media_pause ↔ test_archive）、Gio/GTK 部分用 py_compile + 手工验证、独立 try 降级、`VOICE_INPUT_*` 开关、`git add <file>`。✓
