# Plan — 轻声/气声 VAD 修复 + 词表回声熔断（#43）

Spec: `docs/superpowers/specs/2026-10-27-vad-quiet-echo-guard-design.md`（含 §8b 实施期发现）

| Task | 内容 | 文件 | 覆盖验收 |
|---|---|---|---|
| T1 | 阈值参数化（默认 0.3）+ RMS + 能量门重跑 + 兜底 options | `vad_trim.py`、`test_vad_trim.py` | A1–A5、A3b、A12 |
| T2 | 回声判定纯函数 | `terms.py`、`test_terms.py` | A6 |
| T3 | guard 模块（丢 prompt 重转 / 二次命中丢结果） | `transcribe_guard.py`、`test_transcribe_guard.py` | A7–A9、A8b |
| T4 | 两处接线（worker / CLI）+ 归档字段 | `transcribe_worker.py`、`transcribe_once.py`、`test_transcribe_worker.py`、`test_archive.py` | A10、A11 |
| T5 | README 双语 + 全量回归 + 真实样本回归 | `README*.md` | A13、A14、U4 |

验证命令：

- ① `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim test_transcribe_guard`
- ② `venv/bin/python3 -m unittest test_engine`
- ③ `VOICE_INPUT_ENGINE=npu LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu venv/bin/python3 -m unittest test_vad_trim`
- A14 `VOICE_INPUT_ARCHIVE=0 VOICE_INPUT_ENGINE=npu LD_LIBRARY_PATH=… venv/bin/python3 transcribe_once.py <归档音频>`
