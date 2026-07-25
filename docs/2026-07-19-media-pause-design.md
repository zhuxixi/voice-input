# 录音自动暂停媒体设计文档 — issue #3

- 日期: 2026-07-19
- 状态: 设计待审
- 关联: GitHub issue #3、`voice-ptt.py` 的 `start_recording()` / `stop_recording()`、PR #2 归档降级范式
- 调研依据: `~/.claude/github-issue-driven/zhuxixi/voice-input/issue-3/research/01-环境与方案选型.md`

## 1. 目标

按右 Command 键开始录音时,**自动暂停正在外放的媒体**(用户场景:Chrome 里放音乐 / 带声音的视频),录音结束(arecord 停止)后**自动恢复**播放。消除「外放音乐被麦克风收进去、污染 Whisper 转写」的干扰,免去手动按暂停键。

## 2. 非目标(YAGNI)

- ❌ 不做 sink 静音(`pactl/wpctl set-sink-mute`)——用户明确偏好「暂停」(续播体验更好,记位置);静音整条 sink 还会误伤系统提示音。本期纯走媒体暂停。
- ❌ 不做 app 级 MPRIS 之外的控制(游戏/无 MPRIS 的源)——用户只用 Chrome 听音,Chrome 走 MPRIS,覆盖核心场景。
- ❌ 不用 PipeWire sink-input 检测「真在出声」作为门控——MPRIS `PlaybackStatus` 既是探测器又是控制通道,一套搞定;sink-input 检测属过度精确,留作未来增强。
- ❌ 不安装 `playerctl`(新依赖)——项目已 `import gi`,原生走 Gio D-Bus 调 MPRIS,零新依赖。
- ❌ 不裸发媒体键 `xdotool key XF86AudioPlay`——它是 toggle,当前没播放时按一下反而会**开始播放**(录音时突然炸音乐,踩坑)。

## 3. 检测与控制机制(MPRIS via Gio D-Bus)

**实测现状**(2026-07-19):
- Chrome 注册了 MPRIS 播放器 `org.mpris.MediaPlayer2.chromium.instanceXXXXX`(实例号每次会变,运行时枚举)。
- `PlaybackStatus` 属性:`"Playing"` / `"Paused"` / `"Stopped"`。探测当前=`Paused`(无播放),与 PipeWire sink-inputs 为空互相印证。

**门控信号** = MPRIS `PlaybackStatus == "Playing"`。
**控制** = 同一 MPRIS 接口:
- 暂停:`org.mpris.MediaPlayer2.Player.Pause`(幂等,不会启动已停止的播放器,无 toggle 风险)
- 恢复:`org.mpris.MediaPlayer2.Player.Play`(只对「我们暂停过的」播放器发,避免把录音前本就停止的播放器误启动)

**为何枚举所有 `org.mpris.MediaPlayer2.*` 而非写死 `chromium.*`**:用户只用 Chrome,实际只匹配到 Chrome;但通用枚举更稳——换播放器也自动覆盖,且零额外成本。

**实现层**:Python 原生 `gi.repository.Gio` + `GLib`(项目已 `import gi`,复用,零新依赖,无 subprocess):
- 列名:`Gio.bus_get_sync(Gio.BusType.SESSION, None)` 拿连接,再 `call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", ...)` 过滤 `org.mpris.MediaPlayer2.*`
- 读状态:`call_sync(... "org.freedesktop.DBus.Properties" "Get" ("org.mpris.MediaPlayer2.Player","PlaybackStatus"))`
- Pause/Play:`call_sync(... "org.mpris.MediaPlayer2.Player" "Pause"/"Play")`

## 4. 组件设计(`media_pause.py` 纯逻辑模块 + voice-ptt.py 接入)

对齐 `archive.py` 的拆分:把**可测的决策逻辑**抽到独立模块(仅依赖 `gi`,无 pynput/GTK 窗口副作用),`voice-ptt.py` 只 import 接入。

### `PAUSE_MEDIA_ENABLED`

```python
PAUSE_MEDIA_ENABLED = os.environ.get("VOICE_INPUT_PAUSE_MEDIA", "1") != "0"
```
默认开;`VOICE_INPUT_PAUSE_MEDIA=0` 关。可在 `voice-ptt.sh` / `.desktop` 设。命名对齐 `VOICE_INPUT_ARCHIVE`。

### `_select_to_pause(statuses: dict[str, str]) -> list[str]`(纯函数,单测核心)

输入 `{bus_name: PlaybackStatus}`,返回状态为 `"Playing"` 的 bus name 列表。**纯逻辑,无 IO,直接单测**。

### `pause_playing() -> list[str]`(有副作用:调 Gio)

1. `bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)`
2. 枚举(`call_sync ListNames`)过滤出 MPRIS 播放器名
3. 逐个读 `PlaybackStatus`,组成 `statuses` dict
4. `to_pause = _select_to_pause(statuses)`
5. 对 `to_pause` 中**每个**名字调 `Player.Pause`;**逐个 try/except**(单个失败不中断其余,已成功暂停的仍记下);返回实际成功暂停的名字列表

返回值 = 真正被暂停的 bus name 列表(交给调用方记住,恢复时用)。

### `resume(names: list[str]) -> None`(有副作用:调 Gio)

对 `names` 中**每个**名字调 `Player.Play`;**逐个 try/except**(单个失败不阻断其余,记 stderr)。只恢复传入的名字——录音前本就停止的不在列表里,不会被误启动。

### 模块边界

`media_pause.py` 仅依赖 `os` + `gi.repository.Gio, GLib`。**不** import pynput/GTK,不依赖 display,决策逻辑可独立单测(纯函数 `_select_to_pause`)+ Gio 调用薄封装(手工集成验证,同 archive/voice-ptt 的 GTK 部分)。

## 5. 接入点(start/stop_recording 改造)

### `start_recording()`(当前 L89-107):**启动 arecord 之前**暂停

新增模块全局 `_paused_players: list[str] = []`。

```python
def start_recording():
    global recording, rec_proc, active_window, _paused_players
    if recording:
        return
    recording = True
    # ...(active_window / 删旧 wav 不变)...
    if PAUSE_MEDIA_ENABLED:
        try:
            _paused_players = pause_playing()   # 仅 Recording 的被暂停,记下名字
        except Exception as pe:
            print(f"[voice-input] pause_media failed: {pe}", file=sys.stderr)
            _paused_players = []
    rec_proc = subprocess.Popen(["arecord", ...], ...)
    GLib.idle_add(show_overlay, "● REC", "#ff5555")
```

关键点:
- **在 arecord 启动前暂停**——消除「录音头几帧还在收外放」的窗口(暂停是一次快速 D-Bus call,毫秒级)
- 失败(无 MPRIS / D-Bus 异常)→ `_paused_players=[]`,stderr 警告,**录音照常**

### `stop_recording()`(当前 L110-157):**arecord 停掉后、慢转写前**恢复

在 `rec_proc.terminate()/wait()` 之后插入:

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

    # 恢复媒体(arecord 已停,麦克风不再收音;尽早恢复,用户一松手音乐就回)
    if PAUSE_MEDIA_ENABLED and _paused_players:
        try:
            resume(_paused_players)
        except Exception as re:
            print(f"[voice-input] resume_media failed: {re}", file=sys.stderr)
        finally:
            _paused_players = []

    GLib.idle_add(hide_overlay)
    time.sleep(0.3)
    # ...(转写 / 归档 / 粘贴 不变)...
```

关键点:
- **arecord 停后立即恢复**(用户选的时机):音乐秒回;转写期间(麦克风已停)继续播放对识别无影响
- 只恢复 `_paused_players` 里的名字——录音前本就停止的不会被误启动
- resume 后 `finally: _paused_players = []` 清空,防重入残留
- 失败不阻断后续转写/粘贴

## 6. 错误处理与降级

| 故障 | 行为 |
|---|---|
| 无 MPRIS 播放器(Chrome 没放媒体 / 未注册) | `pause_playing` 返回 `[]` → 不暂停;stop 时 `_paused_players` 空 → 跳过恢复。录音完全不受影响 |
| D-Bus 不可用 / `call_sync` 异常 | 捕获 → stderr 警告 → `_paused_players=[]` → 录音照常 |
| 某播放器 `Pause` 失败(其余成功) | 逐个 try/except,失败者不进返回列表,成功者仍暂停;恢复只动成功的 |
| `Resume`/`Play` 失败 | 逐个 try/except,stderr 警告,不阻断转写/粘贴;媒体可能需用户手动续播(可接受) |
| `VOICE_INPUT_PAUSE_MEDIA=0` | 全程跳过,行为同改造前 |

核心原则(同 PR #2 归档):**媒体暂停/恢复是增强,任何故障不得阻断录音/转写/粘贴主流程**。

## 7. 测试

### 7.1 单元(新增 `test_media_pause.py`)

纯函数 `_select_to_pause`:
- `{"a":"Playing","b":"Paused","c":"Stopped"}` → `["a"]`
- `{}` → `[]`
- 全 `"Playing"` → 全选
- 全 `"Paused"` → `[]`

### 7.2 集成(手工,同归档)

- Chrome 放音乐 → 按右 Command 录音 → 音乐暂停 → 松手 → 转写粘贴正常 → 音乐自动续播
- Chrome **没**放音乐(已暂停/无标签页)→ 录音 → **不会**误启动播放(验证非 toggle)
- `VOICE_INPUT_PAUSE_MEDIA=0` → 录音期间音乐继续放(功能关闭)
- `py_compile voice-ptt.py`(顶部 GTK import 无法直接 import 运行,用 py_compile 校验语法,同 PR #2)

## 8. 文件改动清单

| 文件 | 动作 |
|---|---|
| `media_pause.py` | 新建:MPRIS 暂停/恢复模块(`pause_playing` / `resume` / `_select_to_pause` 纯函数 + `PAUSE_MEDIA_ENABLED` 常量;依赖 `os` + `gi`) |
| `voice-ptt.py` | 改:`from media_pause import pause_playing, resume, PAUSE_MEDIA_ENABLED`;`start_recording()` 启动 arecord 前暂停 + `_paused_players` 全局;`stop_recording()` arecord 停后恢复 |
| `test_media_pause.py` | 新建:`_select_to_pause` 纯函数单元测试 |
| `README.md` | 改:文件说明表 + 「媒体自动暂停」小节 + `VOICE_INPUT_PAUSE_MEDIA` 开关 |

## 9. 与 issue「待讨论」两项的定案

- **静音范围** → 改为「暂停正在播放的 MPRIS 媒体」(用户偏好,优于 sink 静音);枚举所有 `org.mpris.MediaPlayer2.*`,实际命中 Chrome。
- **恢复时机** → arecord 停掉后立即恢复(用户选定),音乐秒回。

## 10. 后续(不在本期)

- 若出现无 MPRIS 的外放源(游戏等)仍干扰 → 再考虑 sink 静音作补充。
- 想区分「播放中但被静音」→ 加 PipeWire sink-input 检测作更精确门控。
