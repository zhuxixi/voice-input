"""录音归档:把每次录音的音频 + 转写文本存到 recordings/ 目录。

纯函数,仅依赖标准库,无 GTK/pynput 副作用,可独立单元测试。
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

ARCHIVE_DIR = os.path.expanduser("~/.local/share/voice-input/recordings")
ARCHIVE_ENABLED = os.environ.get("VOICE_INPUT_ARCHIVE", "1") != "0"
_TS_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})(?:_\d{1,3})?$")


def _env_nonneg_int(name, default):
    """Env value as a non-negative int; missing/garbage/negative -> default."""
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# #44: 0 = keep everything (the contract pinned by
# docs/2026-07-19-audio-archive-design.md §2); >0 prunes record dirs on startup.
ARCHIVE_KEEP_DAYS = _env_nonneg_int("VOICE_INPUT_ARCHIVE_KEEP_DAYS", 0)


def _fsync_dir(path: str) -> None:
    """best-effort fsync 目录条目(防掉电后新建文件/子目录条目未落盘)。失败记 stderr 但不致命。"""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as fe:
        print(f"[archive] _fsync_dir({path}) failed (non-fatal): {fe}", file=sys.stderr)


def archive_recording(wav_path: str, raw_text: str, final_text: str, archive_dir: str = ARCHIVE_DIR,
                      *, engine=None, model=None, vad=None, keep_source=False) -> str:
    """归档一条录音。顺序:建目录→写文本→copyfile wav + 校验 + 删源→记索引。

    回滚原则(wav 珍贵不可逆,基于 audio_confirmed 标志判断):
    - audio_confirmed=False(audio 数据未完整落到 rec_dir / 大小校验未通过)
      → rmtree(rec_dir)(清半成品,含截断 audio;源 wav 还在原位,交调用方 finally)
    - audio_confirmed=True(copyfile 成功 + 大小校验通过,但后续 copystat/remove/index 失败)
      → 保留 rec_dir(音频数据完整已在,索引可能缺,符合"丢索引不丢音频")
    copystat(权限/时间等元数据)失败降级为非致命——不影响数据完整性。

    Args:
        wav_path: 待归档的 wav 路径(keep_source=False 时成功路径会被删源)
        raw_text: Whisper 原始转写
        final_text: 最终输出(纠错后的上屏文本)
        archive_dir: 归档根目录(测试时可传入临时目录)

    Returns:
        归档目录路径(str)。

    Raises:
        任何 IO 异常向上抛,由调用方捕获(不影响转写主流程)。
    """
    if not os.path.isfile(wav_path):
        # #44: raise before makedirs, so a bogus path cannot litter an empty
        # archive root (the caller's guard tells the user what really happened).
        raise FileNotFoundError(f"archive: source wav missing: {wav_path}")
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
    _fsync_dir(archive_dir)  # 让 rec_dir 这个新目录条目落盘到 archive_dir(防掉电后"消失",kimi#8)

    record = {
        "ts": ts_iso,
        "dir": os.path.basename(rec_dir),
        "audio": "audio.wav",
        "raw": raw_text,
        "final": final_text,
        "enhanced": raw_text != final_text,
    }
    # #44: optional provenance / VAD-decision fields. Absent by default, so
    # records written by the voice-ptt path keep their original key set.
    if engine is not None:
        record["engine"] = engine
    if model is not None:
        record["model"] = model
    if vad is not None:
        record["vad"] = vad
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
        # 4. 元数据(权限/时间),失败非致命——不影响数据完整性(cc#10:记 stderr 不静默)
        try:
            shutil.copystat(wav_path, audio_dst)
        except OSError as cse:
            print(f"[archive] copystat failed (non-fatal): {cse}", file=sys.stderr)
        _fsync_dir(rec_dir)  # 让 audio.wav/raw.txt/final.txt 三个新文件条目落盘到 rec_dir(kimi#8)
        # 5. audio 完整 → 删源(此时 rec_dir 已有完整 audio)。
        # keep_source=True(#44): 调用方传入的是用户文件(如 transcribe_once 的
        # CLI 参数),只能拷贝,绝不能删。
        if not keep_source:
            os.remove(wav_path)
        # 6. 记索引(flush+fsync);若此步失败,audio_confirmed=True → 保留 rec_dir
        with open(os.path.join(archive_dir, "index.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(archive_dir)  # 让 index.jsonl 目录条目持久(首次创建时关键,cc#11/kimi#9)
    except Exception:
        if not audio_confirmed:
            # audio 数据未确认完整 → 清半成品(截断 audio/raw/final);源 wav 还在,交调用方 finally
            shutil.rmtree(rec_dir, ignore_errors=True)
        # audio_confirmed=True:audio 完整在 rec_dir,绝不删(源已删或还在都不影响音频安全)
        raise
    return rec_dir


def archive_result(wav_path: str, raw_text: str, final_text: str, *, engine=None,
                   model=None, vad=None, archive_dir: str = None, log=None,
                   keep_source=False) -> bool:
    """ARCHIVE_ENABLED gate + exception guard around `archive_recording` (#44).

    Returns True when the recording was archived, False when archiving is off
    or failed. Never raises: a data-infrastructure problem must not break the
    transcription that is about to reach the user.
    """
    if not ARCHIVE_ENABLED:
        return False
    log = log or (lambda msg: print(msg, file=sys.stderr))
    try:
        archive_recording(wav_path, raw_text, final_text,
                          archive_dir or ARCHIVE_DIR,
                          engine=engine, model=model, vad=vad,
                          keep_source=keep_source)
        return True
    except Exception as e:
        log(f"[archive] failed (non-fatal): {type(e).__name__}: {e}")
        return False


def prune_recordings(archive_dir: str = None, keep_days: int = 0, *, now=None) -> list:
    """Delete record dirs older than `keep_days`; returns the removed dir names.

    `keep_days` <= 0 (the default) is a no-op, preserving the "keep everything"
    contract. Only timestamped record directories are touched — `index.jsonl`
    and anything unrecognised stay as they are (append-only invariant, #44 D7).
    """
    if not keep_days or keep_days <= 0:
        return []
    root = archive_dir or ARCHIVE_DIR
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    removed = []
    for name in names:
        match = _TS_DIR_RE.match(name)
        if not match:
            continue
        try:
            created = datetime.strptime(f"{match.group(1)}_{match.group(2)}",
                                        "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        if created >= cutoff:
            continue
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed
