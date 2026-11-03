# README Bilingual Multi-Engine + Wayland Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring README.md and README.zh-CN.md in sync with the shipped multi-engine (cpu/cuda/npu) + dual-platform (X11/KDE Wayland) reality, closing the last gap of umbrella #14.

**Architecture:** Pure documentation edit of two files at matched positions, plus one stdlib-only static check script (kept in the issue research dir, NOT committed). No code changes.

**Tech Stack:** Markdown, Python 3 stdlib (check script), shields.io badges.

**Spec:** `docs/superpowers/specs/2026-11-03-readme-multi-engine-wayland-design.md` (D1–D6 changes, acceptance matrix A1/A2/A3/U1/U2)

## Global Constraints

- Only the 6 spots D1–D6 may change; all other paragraphs stay verbatim (sole exception: D3's NPU prerequisite edits EN+ZH).
- Every change is applied to BOTH files at the same structural position, semantically equivalent (not literal translation).
- Facts come from code (`engine.py`, `terms.py`) and issue evidence (#17/#27/#14); never write behavior the code doesn't have.
- corrections direction: wrong→right mapping; `"sql": "skill"` corrects misheard "sql" to "skill". JSON examples are already correct — only the prose is flipped.
- research/ = `~/.github-issue-driven/zhuxixi/voice-input/issue-20/research/`, NOT the repo.
- Commit messages in English, conventional commits.

## File Structure

| File | Action |
|---|---|
| `$WT/README.md` | Edit D1–D6 spots |
| `$WT/README.zh-CN.md` | Edit D1–D6 spots (mirrored) |
| `~/.github-issue-driven/zhuxixi/voice-input/issue-20/research/check-readme-sync.py` | New stdlib static check script (not committed) |

## Tasks

### Task 1: Static check script first (test-first for docs)

**Acceptance IDs:** A1, A2, A3

- [ ] Write `check-readme-sync.py` in the research dir with pure functions per spec's 可测性拆分:
  - `extract_headings(path)` — `##`/`###` sequence, tracking fenced code blocks, excluding `#` comment lines inside code
  - `extract_section_structures(path)` — per-section `{bullets, table_rows}` by heading ORDINAL position (not text); bullets include `-` items, numbered items, nested
  - `extract_env_vars(path)` — regex `VOICE_INPUT_[A-Z_]+`
  - `extract_layout_files(path)` — backtick filenames in Project Layout table
  - A1 layer-3 sentence-pair assertions for D1–D6 (badge `KDE Wayland` both; Requirements three engine rows both; NPU prereq `render`+`usermod` both; Features multi-engine both; Roadmap two new `[x]` both; corrections direction both, no flipped `skill`→`sql` prose)
  - A2: doc env vars ⊆ code env vars (`rg -o 'VOICE_INPUT_[A-Z_]+' *.py`); layout files exist; root-level `.py` files (incl. 12 test_*.py, excl. bench/, venv/, __pycache__/) all appear in layout table
  - A3: badge URLs well-formed shields.io; in-doc anchors match heading slugs (per-file: EN #requirements, ZH #依赖)
  - A1 layer-2 exemption: NPU section bullet count exempt entirely (ZH prose legacy); prereq alignment covered by layer-3 + PR diff
  - Exit 0/1; stdlib only; run as `python3 check-readme-sync.py <repo-root>`
- [ ] Run it against the UNMODIFIED worktree → expect failures on the new D1–D6 assertions (red = assertions actually test something); note which fail (baseline: badge, Requirements, render, Features, Roadmap, corrections should fail; layout file coverage should fail on missing engine.py etc.)

### Task 2: EN README D1–D3 (badge, Requirements, NPU prereq + corrections)

**Acceptance IDs:** A1 (③ badge/requirements/render), U1 (NPU route content), U2 (CUDA/CPU content)

- [ ] D1: Platform badge → `platform-Linux%20%28X11%20%C2%B7%20KDE%20Wayland%29` (X11 · KDE Wayland); engine badge → multi-engine (CUDA · CPU · NPU), shields-escaped; intro sentence gains one clause: multi-engine selectable (CUDA default, CPU fallback, Intel NPU via openvino-genai). Anchor stays `#requirements`.
- [ ] D2: Requirements first line → Linux with X11 (tested Ubuntu 24.04) or KDE Wayland (tested Plasma 6.7.5 / Arch); GNOME Wayland untested (#36). Engine deps split three lines: CUDA route (NVIDIA GPU + CUDA); CPU route (no GPU needed); NPU route (Intel NPU + driver ≥1.38.0, see NPU engine section). Mark `xdotool`/`xsel` as X11-only. Keep Python 3.12 / ALSA / PipeWire / GTK3 lines untouched.
- [ ] D3 EN: NPU `Prerequisites (all three):` → `(all four):`, insert render-group bullet: `render` group membership for `/dev/accel/accel0` (`sudo usermod -aG render $USER`, log out and back in).
- [ ] D3 EN corrections prose: flip the mis-stated direction in the hotwords section — the mapping is wrong→right (`"sql": "skill"` fixes misheard "sql" to "skill"); do NOT touch the JSON example.
- [ ] Run check script → EN-side D1/D2/D3 assertions should now pass (script still red overall until ZH done).

### Task 3: ZH README D1–D3 mirror

**Acceptance IDs:** A1 (③ same assertions, both-files), U1

- [ ] D1 ZH: same badge updates; ZH badge anchors stay `#依赖` / `#关于模型`; intro gains equivalent multi-engine clause.
- [ ] D2 ZH: 依赖 section — first line X11 + KDE Wayland (GNOME Wayland 未测 #36); three engine dep lines; `xdotool`/`xsel` 标注 X11-only; common deps untouched.
- [ ] D3 ZH: NPU 前置散文段（README.zh-CN.md ~L220「三个前置（缺一不可）」）refactor into a 4-bullet list matching EN (driver / venv openvino / LD_LIBRARY_PATH / render group) — content unchanged, structure only; 「三个前置」→「四个前置」. Notes/注意 prose section stays as-is.
- [ ] D3 ZH corrections prose at L254 (散文 bullet「稳定错字（skill→sql…）」) — fix direction; do NOT touch L248 JSON.
- [ ] Run check script → D1–D3 assertions all green.

### Task 4: EN+ZH D4–D6 (Features, Project Layout, Roadmap)

**Acceptance IDs:** A1 (③ features/roadmap), A2 (layout coverage)

- [ ] D4 both: Features「GPU accelerated / GPU 加速」entry → multi-engine entry: CUDA large-v3 resident VRAM (default); CPU fallback; NPU via openvino-genai stateful pipeline (#27), hotwords on every engine.
- [ ] D5 both: Project Layout table — add rows for `engine.py`, `transcribe_once.py`, `vad_trim.py`, `transcribe_guard.py`, `transcribe_worker.py`, `worker_supervisor.py`, `worker_protocol.py`; extend test row to list all 12 test_*.py by name; bench/ row gains `hold-latency.py`. EN/ZH same rows.
- [ ] D6 both: Roadmap add two `[x]` entries with issue refs: multi-engine selectable (cpu/cuda/npu, #16/#19/#27); Wayland support (KDE hold-to-talk + ydotool paste + layer-shell overlay, #18/#24).
- [ ] Run check script → expect full green (A1+A2+A3).

### Task 5: Full verification + commit

**Acceptance IDs:** A1, A2, A3 (final); U1/U2 deferred post-merge

- [ ] `python3 check-readme-sync.py $WT` → exit 0, all assertions pass; record output in research dir.
- [ ] `git -C $WT diff --stat` → only README.md + README.zh-CN.md touched; eyeball full diff for stray edits outside D1–D6.
- [ ] `git -C $WT add README.md README.zh-CN.md && git -C $WT commit -m "docs(readme): bilingual multi-engine + Wayland sync (#20)"`.
- [ ] Post-merge manual (deferred, tracked on issue): U1 on OmniBook after merge (pull main, walk NPU route incl. `groups` for render); U2 CUDA/CPU description cross-check vs #14 comment + engine.py. These are pending until merge; final report must present them as pending, not done.
