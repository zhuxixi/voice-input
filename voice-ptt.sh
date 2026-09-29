#!/bin/bash
# 语音输入守护进程 - 按住右 Command 录音，松开转写

# Derive the repo location from this script (#11): works from any clone path.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO_DIR/venv"

# Ask the venv interpreter for its site-packages (#11): survives in-place
# venv rebuilds that would leave a stale lib/python3.x dir behind.
SITE_PACKAGES="$($VENV/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
if [ -z "$SITE_PACKAGES" ]; then
    echo "voice-ptt.sh: venv not found under $REPO_DIR — see README Installation" >&2
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
# 用系统 Python（有 gi/GTK），加 venv 的包路径
export PYTHONPATH="$SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 "$REPO_DIR/voice-ptt.py"
