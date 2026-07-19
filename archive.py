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
    """归档一条录音。顺序:建目录→写文本→copyfile wav + 校验 + 删源→记索引。

    回滚原则(wav 珍贵不可逆,基于 audio_confirmed 标志判断):
    - audio_confirmed=False(audio 数据未完整落到 rec_dir / 大小校验未通过)
      → rmtree(rec_dir)(清半成品,含截断 audio;源 wav 还在原位,交调用方 finally)
    - audio_confirmed=True(copyfile 成功 + 大小校验通过,但后续 copystat/remove/index 失败)
      → 保留 rec_dir(音频数据完整已在,索引可能缺,符合"丢索引不丢音频")
    copystat(权限/时间等元数据)失败降级为非致命——不影响数据完整性。

    Args:
        wav_path: 待归档的 wav 路径(成功路径会被删源)
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
    audio_confirmed = False  # audio 数据已完整落到 rec_dir(并校验过)
    try:
        # 1. 先写可重建的文本文件(各 flush+fsync)
        for name, content in (("raw.txt", raw_text), ("final.txt", final_text)):
            with open(os.path.join(rec_dir, name), "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        # 2. copy audio 数据(保留源;copyfile 只 copy 数据,不 copy 元数据)
        shutil.copyfile(wav_path, audio_dst)
        with open(audio_dst, "rb") as f:
            os.fsync(f.fileno())  # fsync audio(kimi#8)
        # 3. 大小校验(防 copyfile 中途失败留截断,cc#9/kimi#9)
        if os.path.getsize(audio_dst) != os.path.getsize(wav_path):
            raise OSError("archive: wav copy incomplete (size mismatch)")
        audio_confirmed = True  # audio 数据完整确认
        # 4. 元数据(权限/时间),失败非致命——不影响数据完整性
        try:
            shutil.copystat(wav_path, audio_dst)
        except OSError:
            pass
        # 5. audio 完整 → 删源(此时 rec_dir 已有完整 audio)
        os.remove(wav_path)
        # 6. 记索引(flush+fsync);若此步失败,audio_confirmed=True → 保留 rec_dir
        with open(os.path.join(archive_dir, "index.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        if not audio_confirmed:
            # audio 数据未确认完整 → 清半成品(截断 audio/raw/final);源 wav 还在,交调用方 finally
            shutil.rmtree(rec_dir, ignore_errors=True)
        # audio_confirmed=True:audio 完整在 rec_dir,绝不删(源已删或还在都不影响音频安全)
        raise
    return rec_dir
