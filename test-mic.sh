#!/bin/bash
# 测试麦克风录音 + 转写

# Derive the repo location from this script (#11): works from any clone path.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO_DIR/venv"

# Ask the venv interpreter for its site-packages (#11): survives in-place
# venv rebuilds that would leave a stale lib/python3.x dir behind.
SITE_PACKAGES="$($VENV/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
if [ -z "$SITE_PACKAGES" ]; then
    echo "test-mic.sh: venv not found under $REPO_DIR — see README Installation" >&2
    exit 1
fi
export LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

WAV="/tmp/test-mic.wav"

echo "录音 5 秒，请对着麦克风说话..."
arecord -d 5 -f S16_LE -r 16000 -c 1 -D default "$WAV" -q

if [ ! -f "$WAV" ]; then
    echo "录音失败"
    exit 1
fi

# 转写走 transcribe_once 入口(#16):引擎/模型经 VOICE_INPUT_ENGINE /
# VOICE_INPUT_MODEL 选择,不再内嵌 CUDA 调用(默认 cuda,与历史行为一致)
echo "转写中..."
TEXT=$("$VENV/bin/python3" "$REPO_DIR/transcribe_once.py" "$WAV")

rm -f "$WAV"

if [ -n "$TEXT" ]; then
    echo
    echo "识别结果:"
    echo "$TEXT"
else
    echo "未识别到语音"
fi
