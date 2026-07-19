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
