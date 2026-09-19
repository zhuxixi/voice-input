#!/bin/bash
# 直接从 hf-mirror.com 下载 faster-whisper 模型文件
# 无需翻墙，不走 huggingface.co

set -e

MODEL=${1:-large-v3}
REPO="Systran/faster-whisper-${MODEL}"
MIRROR="https://hf-mirror.com"
DEST="$HOME/.cache/huggingface/hub/models--Systran--faster-whisper-${MODEL}/snapshots/downloaded"
# Derive the repo location from this script (#11): works from any clone path.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO_DIR/venv"

# 各 HF 仓库文件清单不同(#15):large-v3 有 preprocessor_config.json/vocabulary.json;
# small 没有 vocabulary.json/preprocessor_config.json,取而代之是 vocabulary.txt。
# 按模型显式配清单,未知模型报错列出支持名,避免把镜像 404 响应体下成假文件。
case "$MODEL" in
    large-v3)
        FILES="config.json preprocessor_config.json tokenizer.json vocabulary.json model.bin"
        ;;
    small)
        FILES="config.json tokenizer.json vocabulary.txt model.bin"
        ;;
    *)
        echo "不支持的模型: $MODEL (当前支持: large-v3, small)" >&2
        exit 1
        ;;
esac

echo "=== 下载 faster-whisper 模型: $MODEL ==="
echo "源: $MIRROR/$REPO"
echo ""

mkdir -p "$DEST"

# 下载文件，支持断点续传。curl -f: HTTP >=400 直接失败,不再把 404 响应体
# (如 "Entry not found")存成文件伪装成功(#15);下载到 .part,成功后原子改名。
download_file() {
    local filename=$1
    local filepath="$DEST/$filename"

    if [ -f "$filepath" ] && [ "$filename" != "model.bin" ]; then
        echo "  [跳过] $filename (已存在)"
        return 0
    fi

    echo -n "  [下载] $filename ... "
    local url="$MIRROR/$REPO/resolve/main/$filename"

    if curl -fL -# -o "$filepath.part" -C - --max-time 600 "$url"; then
        mv -f "$filepath.part" "$filepath"
    else
        rm -f "$filepath.part"
        echo "FAILED"
        return 1
    fi

    local size
    size=$(stat -c %s "$filepath")
    if [ $size -gt 1048576 ]; then
        echo "OK ($((size / 1048576)) MB)"
    else
        echo "OK (${size} B)"
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

# 验证模型(#16):走 engine.build_model(),模型取本次下载的 $MODEL,验证设备按
# VOICE_INPUT_ENGINE 选择(默认 cuda,与历史一致;无 NVIDIA 机器可
# VOICE_INPUT_ENGINE=cpu 验证)
echo "验证模型..."
if "$VENV/bin/python3" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
import engine
print('加载模型...')
engine.build_model(model='$MODEL')
print('模型加载成功!')
"; then
    echo ""
    echo "验证通过! 运行测试:"
    echo "  $REPO_DIR/test-mic.sh"
else
    echo ""
    echo "模型加载失败，文件可能不完整，请重新运行下载"
    exit 1
fi
