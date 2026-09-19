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

# 录音前快速预检(#16 CR 发现 3):引擎值合法 + 模型已在本地,不加载模型。
# 重建旧代码「录音前 fail fast」的时序,新装机器不必先说 5 秒话才看到
# 「模型未下载」的错误,也避免「转写失败」与「未识别到语音」混在同一输出
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
    echo "test-mic.sh: $PRECHECK_ERR" >&2
    exit 1
fi

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
