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
# 库路径前置收敛到 engine.py 单点(#19 债①),按 VOICE_INPUT_ENGINE 计算:
# cuda/auto -> venv nvidia pip 三路径(输出与历史逐字节一致,A8 钉死);
# npu -> ze 驱动库目录(ld.so 只在进程启动读 LD_LIBRARY_PATH,必须在 exec 前导出);
# cpu -> 空(惰性差异,spec Design #4)
LIB_PATHS="$("$VENV/bin/python3" -c "
import os, sys
sys.path.insert(0, '$REPO_DIR')
import engine
try:
    print(':'.join(engine.required_lib_paths(engine.engine_name(dict(os.environ)), '$SITE_PACKAGES')))
except ValueError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
")" || exit 1
if [ -n "$LIB_PATHS" ]; then
    export LD_LIBRARY_PATH="$LIB_PATHS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

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
    # #19: npu 已实现——按引擎选模型布局与默认模型,毫秒级不加载模型
    model = os.environ.get(engine.ENV_MODEL) or engine.default_model(eng)
    engine.resolve_model_path(model, layout='ov' if eng == 'npu' else 'ct2')
except (ValueError, RuntimeError) as e:
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
