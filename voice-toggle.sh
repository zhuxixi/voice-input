#!/bin/bash
# 全局语音输入：按一下开始录音，再按一下停止并转写输入到当前窗口

# Derive the repo location from this script (#11): works from any clone path.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO_DIR/venv"

# Ask the venv interpreter for its site-packages (#11): survives in-place
# venv rebuilds that would leave a stale lib/python3.x dir behind.
SITE_PACKAGES="$($VENV/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
if [ -z "$SITE_PACKAGES" ]; then
    echo "voice-toggle.sh: venv not found under $REPO_DIR — see README Installation" >&2
    exit 1
fi
export LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

PIDFILE="/tmp/voice-input-recording.pid"
WAVFILE="/tmp/voice-input-recording.wav"

if [ -f "$PIDFILE" ]; then
    # 第二次按：停止录音 → 转写 → 打字
    PID=$(cat "$PIDFILE")
    rm -f "$PIDFILE"
    kill "$PID" 2>/dev/null
    sleep 0.5

    # 等待文件写入完成
    while [ ! -s "$WAVFILE" ]; do sleep 0.1; done

    notify-send -t 500 "Voice Input" "转写中..."

    # 转写走 transcribe_once 入口(#16):引擎/模型经 VOICE_INPUT_ENGINE /
    # VOICE_INPUT_MODEL 选择,不再内嵌 CUDA 调用(默认 cuda,与历史行为一致);
    # stderr 不再重定向(2>/dev/null 会吞掉 engine 的可行动错误,CR 发现 1),
    # 预检已挡掉绝大多数配置错误,这里失败时终端里能看到真实原因
    TEXT=$("$VENV/bin/python3" "$REPO_DIR/transcribe_once.py" "$WAVFILE")

    rm -f "$WAVFILE"

    if [ -n "$TEXT" ]; then
        # 上屏分流走 paste.py(#18):wayland 粘贴(wl-copy+wtype)/直打,x11 保留
        # xdotool 现状;用系统 python3(paste.py 纯标准库,不依赖 venv)
        printf '%s' "$TEXT" | python3 "$REPO_DIR/paste.py"
        notify-send -t 1000 "Voice Input" "$TEXT"
    else
        notify-send -t 1000 "Voice Input" "未识别到语音"
    fi
else
    # 第一次按：先快速预检(#16 CR 发现 1):引擎值合法 + 模型已在本地,
    # 不加载模型(毫秒级),失败即弹可行动错误并拒绝录音——重建旧代码
    # 「第一次按键前模型必须已验证」的不变量,避免录完一段话才报错、
    # 且录音已被删除的精失体验
    PRECHECK_ERR=$("$VENV/bin/python3" -c "
import os, sys
sys.path.insert(0, '$REPO_DIR')
import engine
try:
    eng = engine.engine_name(dict(os.environ))
    if eng == 'npu':
        raise NotImplementedError('NPU engine not implemented yet - see issue #19')
    engine.resolve_model_path()
except (ValueError, RuntimeError, NotImplementedError) as e:
    print(e)
    sys.exit(1)
" 2>&1)
    if [ $? -ne 0 ]; then
        echo "voice-toggle.sh: $PRECHECK_ERR" >&2
        notify-send -t 5000 "Voice Input" "$PRECHECK_ERR"
        exit 1
    fi

    # 开始录音
    # 走 PipeWire default，与 voice-ptt.py #6 行为一致，避免 EBUSY 抢占
    arecord -q -f S16_LE -r 16000 -c 1 -D default "$WAVFILE" &
    echo $! > "$PIDFILE"
    notify-send -t 1000 "Voice Input" "录音中... (再按一次停止)"
fi
