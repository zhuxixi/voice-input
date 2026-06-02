#!/bin/bash
# 全局语音输入：按一下开始录音，再按一下停止并转写输入到当前窗口

VENV="/home/elling/.local/share/voice-input/venv"
export LD_LIBRARY_PATH="$VENV/lib/python3.12/site-packages/nvidia/cublas/lib:$VENV/lib/python3.12/site-packages/nvidia/cudnn/lib:$VENV/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

PIDFILE="/tmp/voice-input-recording.pid"
WAVFILE="/tmp/voice-input-recording.wav"
MODEL_PATH="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478"

if [ -f "$PIDFILE" ]; then
    # 第二次按：停止录音 → 转写 → 打字
    PID=$(cat "$PIDFILE")
    rm -f "$PIDFILE"
    kill "$PID" 2>/dev/null
    sleep 0.5

    # 等待文件写入完成
    while [ ! -s "$WAVFILE" ]; do sleep 0.1; done

    notify-send -t 500 "Voice Input" "转写中..."

    TEXT=$("$VENV/bin/python3" -c "
from faster_whisper import WhisperModel
model = WhisperModel('$MODEL_PATH', device='cuda', compute_type='float16')
segments, info = model.transcribe('$WAVFILE', language='zh')
text = ''.join(s.text for s in segments).strip()
print(text)
" 2>/dev/null)

    rm -f "$WAVFILE"

    if [ -n "$TEXT" ]; then
        xdotool type --clearmodifiers --delay 0 "$TEXT"
        notify-send -t 1000 "Voice Input" "$TEXT"
    else
        notify-send -t 1000 "Voice Input" "未识别到语音"
    fi
else
    # 第一次按：开始录音
    arecord -q -f S16_LE -r 16000 -c 1 -D hw:3 "$WAVFILE" &
    echo $! > "$PIDFILE"
    notify-send -t 1000 "Voice Input" "录音中... (再按一次停止)"
fi
