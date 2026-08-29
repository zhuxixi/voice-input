#!/bin/bash
# 测试麦克风录音 + 转写

# Derive the repo location from this script (#11): works from any clone path.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO_DIR/venv"

SITE_PACKAGES="$(ls -d "$VENV"/lib/python3.*/site-packages 2>/dev/null | head -1)"
if [ -z "$SITE_PACKAGES" ]; then
    echo "test-mic.sh: venv not found under $REPO_DIR — see README Installation" >&2
    exit 1
fi
export LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

WAV="/tmp/test-mic.wav"

# Pick the first snapshot dir containing model.bin (#11): accepts both the
# hashed dir (huggingface_hub layout) and the plain `downloaded` dir from
# download-model.sh, so a fresh install needs no manual renaming.
SNAPSHOTS="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots"
MODEL_PATH=""
for d in "$SNAPSHOTS"/*/; do
    [ -f "${d}model.bin" ] && MODEL_PATH="${d%/}" && break
done
if [ -z "$MODEL_PATH" ]; then
    echo "model not found under $SNAPSHOTS — run ./download-model.sh first" >&2
    exit 1
fi

echo "录音 5 秒，请对着麦克风说话..."
arecord -d 5 -f S16_LE -r 16000 -c 1 -D default "$WAV" -q

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
