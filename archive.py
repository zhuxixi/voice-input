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
    """归档一条录音。顺序:建目录→写文本→move wav→记索引。

    回滚原则(wav 珍贵不可逆):
    - 基于 audio_dst 是否存在判断回滚(覆盖跨设备 move 部分成功的 edge case)
    - audio_dst 不存在(含 wav move 之前的失败)→ rmtree(rec_dir)(wav 还在原位,交给调用方 finally)
    - audio_dst 已存在(含 wav move 之后的失败、跨设备 copy 成功但 copystat/unlink 失败)→ 保留 rec_dir(音频已在,索引可能缺,符合"丢索引不丢音频")

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
    ts_dir = now.strftime("%Y-%m-%d_%H%M%S")   # fs-safe, 目录名
    ts_iso = now.strftime("%Y-%m-%dT%H:%M:%S")  # ISO, json
    rec_dir = os.path.join(archive_dir, ts_dir)
    seq = 0
    while True:  # 原子创建 + seq 上界(防理论无限循环)
        try:
            os.makedirs(rec_dir)
            break
        except FileExistsError:
            seq += 1
            if seq > 999:
                raise RuntimeError(f"archive: same-second dir collision limit hit under {archive_dir}")
            rec_dir = os.path.join(archive_dir, f"{ts_dir}_{seq}")

    record = {
        "ts": ts_iso,
        "dir": os.path.basename(rec_dir),
        "audio": "audio.wav",
        "raw": raw_text,
        "final": final_text,
        "enhanced": raw_text != final_text,
    }
    audio_dst = os.path.join(rec_dir, "audio.wav")
    try:
        # 1. 先写可重建的文本文件(各 flush+fsync)
        for name, content in (("raw.txt", raw_text), ("final.txt", final_text)):
            with open(os.path.join(rec_dir, name), "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        # 2. move 珍贵的 wav(跨设备时 copy+unlink+copystat,可能部分成功)
        shutil.move(wav_path, audio_dst)
        # 3. 记索引(flush+fsync);若此步失败,音频已在 rec_dir,保留
        with open(os.path.join(archive_dir, "index.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        # 只有音频还没落到 rec_dir 才 rmtree(此时 wav 仍在原位,交调用方 finally)
        # 若 audio_dst 已存在(含跨设备 copy 成功但后续 copystat/unlink 失败):保留 rec_dir,绝不丢音频
        if not os.path.exists(audio_dst):
            shutil.rmtree(rec_dir, ignore_errors=True)
        raise
    return rec_dir
