#!/bin/bash
# 直接从 hf-mirror.com 下载 faster-whisper 模型文件
# 无需翻墙，不走 huggingface.co

set -e

MODEL=${1:-large-v3}
REPO="Systran/faster-whisper-${MODEL}"
MIRROR="https://hf-mirror.com"
DEST="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-${MODEL}/snapshots/downloaded"
VENV="/home/elling/.local/share/voice-input/venv"

FILES="config.json preprocessor_config.json tokenizer.json vocabulary.json model.bin"

echo "=== 下载 faster-whisper 模型: $MODEL ==="
echo "源: $MIRROR/$REPO"
echo ""

mkdir -p "$DEST"

# 下载文件，支持断点续传
download_file() {
    local filename=$1
    local filepath="$DEST/$filename"

    if [ -f "$filepath" ] && [ "$filename" != "model.bin" ]; then
        echo "  [跳过] $filename (已存在)"
        return 0
    fi

    echo -n "  [下载] $filename ... "
    local url="$MIRROR/$REPO/resolve/main/$filename"

    # 用 curl 下载，支持断点续传
    local code
    code=$(curl -L -# -o "$filepath" -w "%{http_code}" -C - --max-time 600 "$url" 2>&1) || true

    if [ -f "$filepath" ] && [ "$(stat -c %s "$filepath" 2>/dev/null)" -gt 0 ]; then
        local size
        size=$(stat -c %s "$filepath")
        if [ $size -gt 1048576 ]; then
            echo "OK ($(echo "scale=1; $size/1048576" | bc) MB)"
        else
            echo "OK (${size} B)"
        fi
    else
        echo "FAILED"
        return 1
    fi
}

for f in $FILES; do
    download_file "$f" || {
        echo ""
        echo "下载 $f 失败!"
        echo "重试: $0 $MODEL"
        exit 1
    }
done

echo ""
echo "所有文件下载完成!"
echo "模型路径: $DEST"
echo ""

# 更新 test-mic.sh 中的模型路径
echo "验证模型..."
"$VENV/bin/python3" -c "
from faster_whisper import WhisperModel
print('加载模型...')
model = WhisperModel('$DEST', device='cuda', compute_type='float16')
print('模型加载成功!')
" && {
    echo ""
    echo "验证通过! 运行测试:"
    echo "  ~/.local/share/voice-input/test-mic.sh"
} || {
    echo ""
    echo "模型加载失败，文件可能不完整，请重新运行下载"
}
