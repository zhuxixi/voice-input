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
    # 模型缺失/加载失败时非零退出、TEXT 为空,落入「未识别到语音」分支
    TEXT=$("$VENV/bin/python3" "$REPO_DIR/transcribe_once.py" "$WAVFILE" 2>/dev/null)

    rm -f "$WAVFILE"

    if [ -n "$TEXT" ]; then
        xdotool type --clearmodifiers --delay 0 "$TEXT"
        notify-send -t 1000 "Voice Input" "$TEXT"
    else
        notify-send -t 1000 "Voice Input" "未识别到语音"
    fi
else
    # 第一次按：开始录音
    # 走 PipeWire default，与 voice-ptt.py #6 行为一致，避免 EBUSY 抢占
    arecord -q -f S16_LE -r 16000 -c 1 -D default "$WAVFILE" &
    echo $! > "$PIDFILE"
    notify-send -t 1000 "Voice Input" "录音中... (再按一次停止)"
fi
