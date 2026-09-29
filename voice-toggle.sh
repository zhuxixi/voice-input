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
        # 上屏分流走 paste.py(#18 v3):wayland 粘贴(wl-copy+ydotool)/直打,x11 保留
        # xdotool 现状。$VENV python(CR 发现 5:裸 python3 依赖 PATH,venv 已在
        # 上方预检保证存在)。退出码必检查(CR 发现 1/4):失败时 stderr 转成
        # 错误通知而非成功通知,transcript 不再静默丢失
        PASTE_ERRFILE=$(mktemp)
        if printf '%s' "$TEXT" | "$VENV/bin/python3" "$REPO_DIR/paste.py" 2>"$PASTE_ERRFILE"; then
            notify-send -t 1000 "Voice Input" "$TEXT"
        else
            notify-send -t 5000 "Voice Input" "上屏失败: $(cat "$PASTE_ERRFILE")"
        fi
        rm -f "$PASTE_ERRFILE"
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
    # #19: npu 已实现——按引擎选模型布局与默认模型,毫秒级不加载模型
    model = os.environ.get(engine.ENV_MODEL) or engine.default_model(eng)
    engine.resolve_model_path(model, layout='ov' if eng == 'npu' else 'ct2')
except (ValueError, RuntimeError) as e:
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
