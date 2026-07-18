"""录音归档:把每次录音的音频 + 转写文本存到 recordings/ 目录。

纯函数,仅依赖标准库,无 GTK/pynput 副作用,可独立单元测试。
"""

import json
import os
import shutil
from datetime import datetime

ARCHIVE_DIR = os.path.expanduser("~/.local/share/voice-input/recordings")
ARCHIVE_ENABLED = os.environ.get("VOICE_INPUT_ARCHIVE", "1") != "0"


def archive_recording(wav_path: str, raw_text: str, final_text: str, archive_dir: str = ARCHIVE_DIR) -> str:
    """归档一条录音:建时间戳目录,move wav,写 raw/final,追加 index.jsonl。

    Args:
        wav_path: 待归档的 wav 路径(会被 move 走)
        raw_text: Whisper 原始转写
        final_text: 最终输出(当前 == raw;未来接纠错后可能不同)
        archive_dir: 归档根目录(测试时可传入临时目录)

    Returns:
        归档目录路径(str)。

    Raises:
        任何 IO 异常向上抛,由调用方捕获(不影响转写主流程)。
    """
    now = datetime.now()
    ts_dir = now.strftime("%Y-%m-%d_%H%M%S")   # fs-safe, 用于目录名
    ts_iso = now.strftime("%Y-%m-%dT%H:%M:%S")  # ISO, 用于 json
    rec_dir = os.path.join(archive_dir, ts_dir)
    seq = 1
    while os.path.exists(rec_dir):  # 同秒冲突:加序号后缀
        rec_dir = os.path.join(archive_dir, f"{ts_dir}_{seq}")
        seq += 1
    os.makedirs(rec_dir, exist_ok=True)

    shutil.move(wav_path, os.path.join(rec_dir, "audio.wav"))

    with open(os.path.join(rec_dir, "raw.txt"), "w", encoding="utf-8") as f:
        f.write(raw_text)
    with open(os.path.join(rec_dir, "final.txt"), "w", encoding="utf-8") as f:
        f.write(final_text)

    record = {
        "ts": ts_iso,
        "dir": os.path.basename(rec_dir),
        "audio": "audio.wav",
        "raw": raw_text,
        "final": final_text,
        "enhanced": raw_text != final_text,
    }
    with open(os.path.join(archive_dir, "index.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return rec_dir
