# Plan — hold 链路接入录音归档 + VAD 决策元数据（#44）

Spec: `docs/superpowers/specs/2026-10-26-hold-archive-vad-metadata-design.md`（定稿 sha256 `8bff8315…`）
授权：`auto`（设计 gate 已放行；合并前仍需用户明确同意）

验收 ID 对照：A1–A13 自动化，U1/U2 用户实测。每个 task 末尾标出其覆盖的 ID。

---

## T1 — `vad_trim.py`：默认值集中 + `vad_summary` + `stats` 采集

文件：`vad_trim.py`、`test_vad_trim.py`

1. 新增模块常量 `DEFAULT_THRESHOLD = 0.5`；`DEFAULT_PAD_MS` 保留。新增 `default_vad_options()` 返回
   `VadOptions(threshold=DEFAULT_THRESHOLD, speech_pad_ms=DEFAULT_PAD_MS, min_speech_duration_ms=0)`（惰性 import，保持 stdlib-only 顶层）。
2. 新增纯函数 `vad_summary(bounds, total_samples, sample_rate=SAMPLE_RATE, options=None) -> dict`：
   返回 `threshold` / `speech_pad_ms` / `total_s` / `kept_s` / `kept_ratio` / `spans`，秒与 ratio 四舍五入到 2/3 位；`sample_rate<=0` 时秒数为 0.0；不 import faster_whisper（options duck-typed）。
3. `spans_from_wav(path, *, vad_options=None)` 转发给 `speech_spans`，默认 `default_vad_options()`。
4. `speech_spans` 默认值改为 `default_vad_options()`（行为不变，仅集中）。
5. `trimmed_wav(path, *, detector=None, enabled=None, log=None, stats=None)`：进入时 `stats.clear()`（None 则忽略），
   按 §4.2 契约填 `status` + 字段：`disabled` / `kept`（含 99% passthrough）/ `no_speech` / `degraded(reason)`。
   控制流、异常语义、「永不抛」契约不变。

验证：`venv/bin/python3 -m unittest test_vad_trim.TestVadSummary test_vad_trim.TestStats -v`

覆盖：A1、A2

---

## T2 — `archive.py`：元数据字段 + `archive_result` 壳 + `prune_recordings`

文件：`archive.py`、`test_archive.py`

1. `ARCHIVE_KEEP_DAYS = _env_nonneg_int("VOICE_INPUT_ARCHIVE_KEEP_DAYS", 0)`（模块级，坏值/负数 → 0）。
2. `archive_recording(..., *, engine=None, model=None, vad=None)`：record 仅在非 None 时增加对应键；
   现有调用与回滚顺序不变。
3. `archive_result(wav_path, raw_text, final_text, *, engine=None, model=None, vad=None, archive_dir=None, log=None) -> bool`：
   `archive_dir = archive_dir or ARCHIVE_DIR`（None 归一）；`ARCHIVE_ENABLED` 为假直接 False；
   调 `archive_recording`，任何异常 log `[archive] ... failed (non-fatal)` 后返回 False。
4. `prune_recordings(archive_dir=None, keep_days=0, *, now=None) -> list[str]`：`keep_days<=0` → `[]`；
   只扫直接子目录，目录名 `%Y-%m-%d_%H%M%S` 或 `..._<seq>`，解析失败跳过；早于 `now - keep_days` 天 → rmtree；
   `index.jsonl` 不动。

验证：`python3 -m unittest test_archive -v`

覆盖：A3、A4

---

## T3 — `transcribe_worker.py`：接线（归档先于回包）+ prune

文件：`transcribe_worker.py`、`test_transcribe_worker.py`

1. `worker_main(..., archive_dir=None)`；`target_dir = archive_dir or archive.ARCHIVE_DIR`。
2. prune：进入 serve 循环前、`ready` 行发出之前，`if archive.ARCHIVE_ENABLED: try prune_recordings(target_dir, keep_days=archive.ARCHIVE_KEEP_DAYS) except log`。
3. 请求处理：`stats = {}`；`with trimmed_wav(wav, stats=stats) as src:`；`raw = text` → `text, n = apply_corrections_counted(...)`；
   `archive.archive_result(wav, raw, text, engine=eng, model=model_label, vad=stats, archive_dir=target_dir, log=log)`；
   **然后** `conn.write(encode_text(...)); conn.flush()`。no_speech 分支同样走归档（status=no_speech）。
4. `import archive` 顶层加入（stdlib-only，不破坏 `TestModulePurity` 之类约束——检查仓内是否真有该测试）。

验证：`python3 -m unittest test_transcribe_worker -v`

覆盖：A5、A6、A7、A8、A9、A12

---

## T4 — `transcribe_once.py`：`_preamble()` 抽取 + 统一 no_speech + 归档 + prune

文件：`transcribe_once.py`、`test_archive.py`

1. 把 import 期的 venv 探测 / 引擎校验 / `LD_LIBRARY_PATH` 注入整段搬进 `_preamble() -> int`，返回 0 或错误码；
   `main()` 开头 `rc = _preamble(); if rc: return rc`。import 阶段不再叉子进程、不再改 `os.environ`。
   `engine` / `vad_trim` / `terms` 的顶层 import 保留（都是 stdlib-only 模块）。
2. 转写段改为统一流程：`src is None → text=""`，否则 transcribe；`raw = text`；`text = apply_corrections(...)`；
   `archive.archive_result(wav, raw, text, engine=..., model=..., vad=stats)`；**然后** `print(text)`。
3. `main()` 中 `ARCHIVE_ENABLED` 为真时 `prune_recordings(keep_days=ARCHIVE_KEEP_DAYS)` 一次，异常吞掉。

验证：`python3 -m unittest test_archive -v`（含新增 `TestTranscribeOnceHook` / `TestTranscribeOnceImport`）

覆盖：A9b、A10、A13

---

## T5 — README 双语 + 全量回归

文件：`README.md`、`README.zh-CN.md`

1. Configuration 表新增 `VOICE_INPUT_ARCHIVE`（已有则补 hold 生效说明）与 `VOICE_INPUT_ARCHIVE_KEEP_DAYS`（默认 0=不清理，说明累积量级）。归档章节写明：目录、每次听写一条 `audio.wav/raw.txt/final.txt`、index 含 `vad` 对象、prune 只删目录不动 index、SIGKILL 残留边界。
2. 说明 `transcribe_once.py` 手动运行也会归档。

验证：
- ① `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker test_vad_trim -v`
- ② `venv/bin/python3 -m unittest test_engine -v`
- ③ `VOICE_INPUT_ENGINE=npu venv/bin/python3 -m unittest test_vad_trim -v`

覆盖：A11、U2（人工核对两语言一致性）

---

## 交付

- 分支：`issue-44-hold-archive-vad-metadata`（worktree `.pi/worktrees/issue-44-hold-archive-vad-metadata`）
- 两个 commit：`feat(archive): VAD metadata + retention prune`、`feat(archive): wire hold + transcribe_once`
- 本地 CR：fresh-context subagent（`code-review` 模式）审完再 push
- PR → `zima:needs-review` → 前台等 CR → 修 → 合并前停（`Merge: requires-explicit-user-approval`）
