# 录音归档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把每次语音录音 + Whisper 转写结果成对归档到 `~/.local/share/voice-input/recordings/`(每条录音一目录 `audio.wav` / `raw.txt` / `final.txt` + 一份 append-only `index.jsonl`),供回溯与未来 A/B 评测。

**Architecture:** 新增纯函数模块 `archive.py`(仅依赖 Python 标准库,无 GTK/pynput,可独立单元测试);`voice-ptt.py` 在 `stop_recording()` 转写后接入,把 wav **copyfile+校验+删源** 到归档目录(替代原 `os.unlink`,显式 copyfile 保留源直至数据完整性确认),归档独立 `try` 不阻断转写+粘贴。

**Tech Stack:** Python 3.12 标准库(`os` / `shutil` / `json` / `wave` / `datetime` / `unittest`),**零新依赖**。

## Global Constraints

- **零新依赖**:仅用 Python 标准库(不引入 pytest 等,测试用 `unittest`)
- **不破坏现有转写+粘贴流程**:归档任何失败都降级,转写结果照常粘贴
- **/tmp 录音用完即清**:归档成功 → wav 被 move 走;归档失败/异常 → finally 兜底 `unlink`
- **所有改动在 worktree**,禁碰 `main`
- `git add <file>` 按文件 stage,**禁用 `git add -A`**(避免 sweep 临时文件)
- venv 解释器:`/home/elling/.local/share/voice-input/.claude/worktrees/issue1-archive/venv/bin/python`(worktree 内 venv;若不存在则用主 checkout 的 `~/.local/share/voice-input/venv/bin/python`,见 Task 1 Step 0)

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `archive.py` | 纯函数归档逻辑:`archive_recording()` + `ARCHIVE_DIR` / `ARCHIVE_ENABLED` 常量。零 GTK 依赖,可独立测试 | 新建 |
| `test_archive.py` | `archive.py` 的单元测试(unittest) | 新建 |
| `voice-ptt.py` | 在 `stop_recording()` 接入归档:import archive + 改 finally 附近的清理逻辑 | 改 |

**为什么拆 `archive.py` 而非写进 `voice-ptt.py`**:`voice-ptt.py` 顶部即 `import gi` / `from pynput import keyboard`,直接 import 该模块会触发 GTK 初始化等 side effects,测试环境(无 display)无法独立加载。把归档逻辑抽成无副作用的纯函数模块,测试只需 `from archive import archive_recording`。

---

## Task 1: `archive.py` — 归档纯函数(TDD)

**Files:**
- Create: `archive.py`
- Test: `test_archive.py`

**Interfaces:**
- Produces:
  - `ARCHIVE_DIR: str` — 归档根目录(默认 `~/.local/share/voice-input/recordings`)
  - `ARCHIVE_ENABLED: bool` — 开关(环境变量 `VOICE_INPUT_ARCHIVE`,默认 `True`,`"0"` 关)
  - `archive_recording(wav_path: str, raw_text: str, final_text: str, archive_dir: str = ARCHIVE_DIR) -> str` — 归档一条录音,返回归档目录路径;任何异常向上抛

**实现说明(对 spec 的微调)**:`enhanced` 字段用 `raw_text != final_text` **自动判断**(spec 原写"当前固定 false")。当前 `raw == final` → `enhanced=False`;未来接入 A&B 后 `final = apply_corrections(raw)` 不同于 raw → 自动 `True`。比硬编码更前瞻,且行为等价。

- [ ] **Step 0: 确认 venv 可用**

```bash
cd /home/elling/.local/share/voice-input/.claude/worktrees/issue1-archive
ls venv/bin/python 2>/dev/null && echo "worktree venv ok" || echo "用主 venv: ~/.local/share/voice-input/venv/bin/python"
```

worktree 通常**不含 venv**(git 忽略)。若无,后续命令一律用主 checkout 的 `~/.local/share/voice-input/venv/bin/python`(标准库测试不需要 worktree 内 venv)。记下实际用的解释器路径 `PY`。

- [ ] **Step 1: 写失败测试 `test_archive.py`**

Create `test_archive.py`:

```python
import json
import os
import shutil
import tempfile
import unittest
import wave

from archive import archive_recording


def _make_wav(path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 100)


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.archive_dir = os.path.join(self.tmp, "recordings")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wav(self, name="in.wav"):
        p = os.path.join(self.tmp, name)
        _make_wav(p)
        return p

    def test_basic_archive(self):
        wav = self._wav()
        rec_dir = archive_recording(wav, "你好 zima", "你好 zima", archive_dir=self.archive_dir)
        self.assertTrue(os.path.isfile(os.path.join(rec_dir, "audio.wav")))
        self.assertEqual(
            open(os.path.join(rec_dir, "raw.txt"), encoding="utf-8").read(), "你好 zima"
        )
        self.assertEqual(
            open(os.path.join(rec_dir, "final.txt"), encoding="utf-8").read(), "你好 zima"
        )
        self.assertFalse(os.path.exists(wav))  # wav 被 move 走

    def test_index_jsonl_two_records(self):
        archive_recording(self._wav("a.wav"), "a", "a", archive_dir=self.archive_dir)
        archive_recording(self._wav("b.wav"), "b", "b", archive_dir=self.archive_dir)
        lines = (
            open(os.path.join(self.archive_dir, "index.jsonl"), encoding="utf-8")
            .read()
            .strip()
            .split("\n")
        )
        self.assertEqual(len(lines), 2)
        r0, r1 = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual(r0["raw"], "a")
        self.assertEqual(r1["raw"], "b")
        for r in (r0, r1):
            for k in ("ts", "dir", "audio", "final", "enhanced"):
                self.assertIn(k, r)

    def test_enhanced_flag_when_raw_ne_final(self):
        archive_recording(self._wav(), "z码", "zima", archive_dir=self.archive_dir)
        rec = json.loads(
            open(os.path.join(self.archive_dir, "index.jsonl"), encoding="utf-8").readline()
        )
        self.assertTrue(rec["enhanced"])
        self.assertEqual(rec["final"], "zima")

    def test_dir_unique_across_calls(self):
        for i in range(3):
            archive_recording(self._wav(f"{i}.wav"), "x", "x", archive_dir=self.archive_dir)
        dirs = [d for d in os.listdir(self.archive_dir) if not d.endswith(".jsonl")]
        self.assertEqual(len(dirs), 3)

    def test_unwritable_dir_raises(self):
        with self.assertRaises(Exception):
            archive_recording(self._wav(), "x", "x", archive_dir="/proc/cannot-create-xxx")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.local/share/voice-input/venv/bin/python -m unittest test_archive -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archive'`

- [ ] **Step 3: 写最小实现 `archive.py`**

Create `archive.py`:

```python
"""录音归档:把每次录音的音频 + 转写文本存到 recordings/ 目录。

纯函数,仅依赖标准库,无 GTK/pynput 副作用,可独立单元测试。
"""

import json
import os
import shutil
import sys
from datetime import datetime

ARCHIVE_DIR = os.path.expanduser("~/.local/share/voice-input/recordings")
ARCHIVE_ENABLED = os.environ.get("VOICE_INPUT_ARCHIVE", "1") != "0"


def _fsync_dir(path: str) -> None:
    """best-effort fsync 目录条目(防掉电后新建文件/子目录条目未落盘)。失败忽略。"""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


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
    _fsync_dir(archive_dir)  # 让 rec_dir 这个新目录条目落盘到 archive_dir(防掉电后"消失",kimi#8)

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
        # 4. 元数据(权限/时间),失败非致命——不影响数据完整性(cc#10:记 stderr 不静默)
        try:
            shutil.copystat(wav_path, audio_dst)
        except OSError as cse:
            print(f"[archive] copystat failed (non-fatal): {cse}", file=sys.stderr)
        _fsync_dir(rec_dir)  # 让 audio.wav/raw.txt/final.txt 三个新文件条目落盘到 rec_dir(kimi#8)
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.local/share/voice-input/venv/bin/python -m unittest test_archive -v`
Expected: PASS,5 个测试全绿(`test_basic_archive` / `test_index_jsonl_two_records` / `test_enhanced_flag_when_raw_ne_final` / `test_dir_unique_across_calls` / `test_unwritable_dir_raises`)

- [ ] **Step 5: Commit**

```bash
cd /home/elling/.local/share/voice-input/.claude/worktrees/issue1-archive
git add archive.py test_archive.py
git commit -m "feat(archive): add recording archive module with jsonl index

Pure-function module (stdlib only, no GTK deps) that moves the wav
into a timestamped dir, writes raw.txt/final.txt, and appends a JSON
record to index.jsonl. Covers issue #1 storage layer.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `voice-ptt.py` 接入归档

**Files:**
- Modify: `voice-ptt.py`(顶部 import 区 + `stop_recording()` 第 125-147 行附近)
- 接口参考(来自 Task 1):`from archive import archive_recording, ARCHIVE_ENABLED`

**Interfaces:**
- Consumes: `archive.archive_recording(wav_path, raw_text, final_text)`、`archive.ARCHIVE_ENABLED`
- 修改后 `stop_recording` 的保证:正常路径归档(move 走 wav),finally 跳过 unlink;异常/归档失败路径 finally 兜底 unlink。

- [ ] **Step 1: 加 import**

Modify `voice-ptt.py` 顶部 import 区(在第 17 `import signal` 之后、`import gi` 之前加一行,保持标准库 import 聚拢):

```python
import subprocess
import threading
import time
import signal
from archive import archive_recording, ARCHIVE_ENABLED
import gi
```

(即把 `from archive import archive_recording, ARCHIVE_ENABLED` 插入到 `import signal` 与 `import gi` 之间。)

- [ ] **Step 2: 改 `stop_recording` 的清理逻辑**

Modify `voice-ptt.py` 的 `stop_recording()`(原第 125-134 行):

改前:
```python
    try:
        m = load_model()
        segments, info = m.transcribe(WAVFILE, language="zh")
        text = "".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[voice-input] Error: {e}", file=sys.stderr)
        text = ""
    finally:
        if os.path.exists(WAVFILE):
            os.unlink(WAVFILE)
```

改后:
```python
    text = ""  # 预置:transcribe 抛 BaseException(KeyboardInterrupt)时 finally 不 NameError
    try:
        m = load_model()
        segments, info = m.transcribe(WAVFILE, language="zh")
        text = "".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[voice-input] Error: {e}", file=sys.stderr)
        text = ""
    finally:
        # 归档(置于 finally 套件内,独立 try,失败不影响转写/粘贴)
        if ARCHIVE_ENABLED and text and os.path.exists(WAVFILE):
            try:
                archive_recording(WAVFILE, text, text)
            except Exception as ae:
                print(f"[voice-input] archive failed: {ae}", file=sys.stderr)
        if os.path.exists(WAVFILE):  # 异常/归档失败 → wav 还在 → 清理
            os.unlink(WAVFILE)
```

关键:`text = ""` 预置防 BaseException 路径 NameError;归档 `if/try` 整体置于 `finally` 套件内,其后才是 `unlink` 兜底。正常路径 `archive_recording` 把 wav move 走 → `os.path.exists(WAVFILE)` 为 False → 跳过 unlink;归档失败或 transcribe 异常时 wav 还在 → unlink(/tmp 不残留)。

- [ ] **Step 3: 语法校验**

Run: `~/.local/share/voice-input/venv/bin/python -m py_compile voice-ptt.py`
Expected: 无输出(编译通过)。`py_compile` 只编译不执行,不触发 GTK 加载,适合校验含 side-effect import 的脚本。

同时确认 `archive.py` 也能编译:`~/.local/share/voice-input/venv/bin/python -m py_compile archive.py`

- [ ] **Step 4: 重跑 Task 1 测试确认未回归**

Run: `~/.local/share/voice-input/venv/bin/python -m unittest test_archive -v`
Expected: 5 个测试仍全绿(voice-ptt.py 改动不影响 archive 模块)。

- [ ] **Step 5: 集成验证(手工 / 半自动)**

此项需真实按键录音 + GPU,无法全自动。两条验证路径,至少做 A:

**A. 端到端真实录音(推荐)**:
1. 确认旧的 voice-ptt 进程不影响:`pkill -f voice-ptt.py`(用户当前有 PID 3498765 在跑旧版,需重启加载新代码)
2. 在 worktree 启动新版:`~/.local/share/voice-input/venv/bin/python voice-ptt.py`(注意:worktree 的 voice-ptt.py 用的是主 checkout 的 venv,模型路径硬编码绝对路径,可正常运行)
3. 按住右 Cmd 录一句中文(含一个专有名词,如"zima"),松开
4. 检查:`ls ~/.local/share/voice-input/recordings/` 应出现时间戳目录 + `index.jsonl`;目录内有 `audio.wav`(可播放)/`raw.txt`/`final.txt`;`/tmp/voice-input-recording.wav` 不存在(已 move 走)
5. Ctrl+C 退出,恢复用户原进程

**B. 关闭开关验证降级**:
`VOICE_INPUT_ARCHIVE=0 ~/.local/share/voice-input/venv/bin/python voice-ptt.py` → 录音后 `recordings/` 不增长,`/tmp` 仍被清理。

- [ ] **Step 6: 更新 README(可选,推荐)**

Modify `README.md` 的「文件说明」表,加一行:

```markdown
| `archive.py` | 录音归档(音频 + 转写文本存到 `~/.local/share/voice-input/recordings/`) |
```

并在「使用」节末尾加归档说明:

```markdown
## 录音归档

默认每次录音的音频 + 转写结果会归档到 `~/.local/share/voice-input/recordings/`(每条一目录 + `index.jsonl` 索引),供回溯与评测。关闭:`VOICE_INPUT_ARCHIVE=0 ./voice-ptt.sh`。
```

- [ ] **Step 7: Commit**

```bash
cd /home/elling/.local/share/voice-input/.claude/worktrees/issue1-archive
git add voice-ptt.py README.md
git commit -m "feat(archive): integrate recording archive into voice-ptt

Wire archive_recording into stop_recording: on success the wav is
moved into the archive dir (finally skips unlink); on archive failure
or transcribe exception, finally unlinks as before. Archive runs in
its own try so it never blocks transcription/paste. Closes #1.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review(plan 写完后自查)

**1. Spec coverage**:
- 存储结构(recordings/index.jsonl + 每条目录 audio.wav/raw.txt/final.txt)→ Task 1 Step 3 ✓
- 字段 ts/dir/audio/raw/final/enhanced → Task 1 Step 3 record dict ✓
- 开关 ARCHIVE_ENABLED 默认开、env 控制 → Task 1 Step 3 + Task 2 Step 5B ✓
- 接入点 stop_recording、move 替代 unlink、独立 try 不阻断 → Task 2 Step 2 ✓
- 错误降级(归档失败不阻断)→ Task 2 Step 2 的独立 try + finally 兜底 ✓
- 单元测试 → Task 1 ✓;集成测试 → Task 2 Step 5 ✓

**2. Placeholder scan**:无 TBD/TODO;每步含完整代码或确切命令 + 预期输出 ✓

**3. Type consistency**:`archive_recording(wav_path, raw_text, final_text, archive_dir=ARCHIVE_DIR) -> str` 在 Task 1 定义、Task 2 调用,签名一致 ✓;`ARCHIVE_ENABLED`、`ARCHIVE_DIR` 命名跨任务一致 ✓
