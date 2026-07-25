"""录音时自动暂停/恢复 MPRIS 媒体(用户的 Chrome 音乐/视频)。

纯逻辑(本文件的 _select_to_pause)仅依赖标准库,可独立单元测试;
Gio D-Bus 薄封装 pause_playing/resume 依赖 gi(系统 python3 自带,venv 无),
import 本模块本身不连 D-Bus(连接只在函数内发生),无副作用。
"""

import os
import sys

from gi.repository import Gio, GLib

PAUSE_MEDIA_ENABLED = os.environ.get("VOICE_INPUT_PAUSE_MEDIA", "1") != "0"

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"

# 单次 D-Bus call_sync 超时(ms)。默认 -1≈25s,某播放器无响应会阻塞按键回调/
# GTK 主循环。显式 2s 让故障快速失败回归降级路径(CR: cc#3/kimi#2)。
_DBUS_TIMEOUT_MSEC = 2000


def _select_to_pause(statuses):
    """纯函数:给定 {bus_name: PlaybackStatus},返回状态为 "Playing" 的 bus name 列表。

    Args:
        statuses: {bus_name(str): PlaybackStatus(str, "Playing"/"Paused"/"Stopped")}

    Returns:
        list[str]: 状态 == "Playing" 的 bus name(保持插入序,确定性)。
    """
    return [name for name, status in statuses.items() if status == "Playing"]


def _session_bus():
    """获取 session bus 连接。失败抛异常(由调用方捕获)。"""
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _list_mpris_players(bus):
    """枚举 session bus 上所有 org.mpris.MediaPlayer2.* 播放器 bus name。"""
    res = bus.call_sync(
        DBUS_NAME, DBUS_PATH, DBUS_IFACE, "ListNames",
        None, GLib.VariantType.new("(as)"),
        Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MSEC, None,
    )
    return [n for n in res.unpack()[0] if n.startswith(MPRIS_PREFIX)]


def _playback_status(bus, name):
    """读某播放器的 PlaybackStatus。成功返回 str,失败返回 None(跳过该播放器)。"""
    try:
        res = bus.call_sync(
            name, PLAYER_PATH, PROPS_IFACE, "Get",
            GLib.Variant("(ss)", (PLAYER_IFACE, "PlaybackStatus")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MSEC, None,
        )
        return res.unpack()[0]
    except Exception as e:  # GLib.Error / 解包异常均吞,契合"对故障不抛"契约(cc#4)
        print(f"[media_pause] read status {name} failed: {e}", file=sys.stderr)
        return None


def _call_player_method(bus, name, method):
    """调 Player 的无参方法(Pause/Play)。失败抛异常(由调用方捕获)。"""
    bus.call_sync(
        name, PLAYER_PATH, PLAYER_IFACE, method,
        None, None,
        Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MSEC, None,
    )


def pause_playing():
    """暂停所有当前 "Playing" 的 MPRIS 播放器,返回被暂停的 bus name 列表。

    对故障不抛(枚举失败返回 []);单播放器 Pause 失败跳过 + 记 stderr。
    调用方(voice-ptt)拿到返回值后,在 stop 时 resume 这些名字。
    """
    try:
        bus = _session_bus()
        names = _list_mpris_players(bus)
    except Exception as e:
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
        except Exception as e:
            print(f"[media_pause] Pause {name} failed: {e}", file=sys.stderr)
    return paused


def resume(names):
    """恢复(Play)给定的 bus name 列表。逐个 best-effort,失败记 stderr,不抛。"""
    if not names:
        return
    try:
        bus = _session_bus()
    except Exception as e:
        print(f"[media_pause] resume bus failed: {e}", file=sys.stderr)
        return
    for name in names:
        try:
            _call_player_method(bus, name, "Play")
        except Exception as e:
            print(f"[media_pause] resume {name} failed: {e}", file=sys.stderr)
