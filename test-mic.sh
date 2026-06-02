#!/bin/bash
# 测试麦克风录音 + 转写

VENV="/home/elling/.local/share/voice-input/venv"
export LD_LIBRARY_PATH="$VENV/lib/python3.12/site-packages/nvidia/cublas/lib:$VENV/lib/python3.12/site-packages/nvidia/cudnn/lib:$VENV/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
WAV="/tmp/test-mic.wav"
MODEL_PATH="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478"

echo "录音 5 秒，请对着麦克风说话..."
arecord -d 5 -f S16_LE -r 16000 -c 1 -D hw:3 "$WAV" -q

if [ ! -f "$WAV" ]; then
    echo "录音失败"
    exit 1
fi

echo "转写中..."
"$VENV/bin/python3" -c "
from faster_whisper import WhisperModel
model = WhisperModel('$MODEL_PATH', device='cuda', compute_type='float16')
segments, info = model.transcribe('$WAV', language='zh')
text = ''.join(s.text for s in segments).strip()
print()
print('识别结果:')
print(text)
"

rm -f "$WAV"
