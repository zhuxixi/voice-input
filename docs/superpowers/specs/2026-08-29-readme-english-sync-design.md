# Spec: README English rewrite & content sync (#10)

## Goal

Rewrite `README.md` in English (GitHub-first audience) and keep the Chinese version as
`README.zh-CN.md` with cross-links, while syncing all content to the current code
(hotwords #4, PipeWire capture #6, archive #1, media pause #3).

## Scope / Non-goals

- **In scope**: `README.md` (rewrite, English), `README.zh-CN.md` (new, synced Chinese
  translation = old README + all content fixes). No other files.
- **Non-goals**: no code changes, no script path changes (that is #11), no install-flow
  simplification (blocked by #11; Installation documents the current hardcoded-path
  constraint honestly and links #11).

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language | English main + `README.zh-CN.md` | Issue #10 §0; existing repo description is already English |
| Structure | Badges → one-liner → TOC → Features → Requirements → Installation → Usage → Configuration → How it works → Files → Model → Resources → Hardware → Roadmap → Testing → License | markdown-pro best practice; config scattered → one table |
| Hotwords | New "Custom vocabulary" section under Configuration; Roadmap marks initial_prompt as done (#4), keeps post-correction as future | Terms feature is implemented |
| Audio capture | "PipeWire default" explanation replaces "edit -D hw:3" advice; `pactl`/`wpctl` for switching mic; `arecord -l` demoted to troubleshooting | #6 behavior |
| Installation | Keep clone/venv/pip/download steps + explicit callout: 5 files hardcode an absolute path until #11; users must adjust | Honest for current HEAD |
| Tests | `python3 -m unittest ...` (stdlib style, no pytest in venv) | Matches test files |

## Factual contract (verified against HEAD df1ea85)

- Env vars: `VOICE_INPUT_ARCHIVE`, `VOICE_INPUT_PAUSE_MEDIA`; default enabled, `=0` disables
- Hotwords config: `~/.config/voice-input/terms.json`, `{"terms": [...], "hotwords": null}`;
  terms → initial_prompt (first 30 terms); hotwords → faster-whisper `hotwords` kwarg;
  any config failure degrades to no-prompt, never blocks transcription
- Capture: `arecord -D default` (PipeWire), 16 kHz mono S16_LE
- Key: `Alt_R` (Right Cmd on Mac keyboards, Right Alt on PC) hold-to-record
- Overlay: red "● REC" while recording, blue "DONE" on success
- Output: clipboard via `xsel` + `xdotool key ctrl+shift+v` into the previously active window
- Archive: `~/.local/share/voice-input/recordings/` per-recording dir + `index.jsonl`
- Model: faster-whisper large-v3, float16 CUDA, ~3.9 GB VRAM resident

## Acceptance criteria

- Both READMEs factually consistent with code (CR fact-check pass)
- English README has: badges, TOC, unified Configuration table, hotwords section,
  PipeWire capture note, updated file table (terms.py, tests, docs/), Testing section
- Installation documents the hardcoded-path constraint + links #11
- Roadmap updated: hotwords done; Windows (#9) listed
