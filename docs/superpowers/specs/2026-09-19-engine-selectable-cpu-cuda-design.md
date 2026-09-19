# Spec: Selectable transcription engine — VOICE_INPUT_ENGINE=cpu|cuda (#16)


## Goal

Make the transcription engine selectable via environment variables while keeping the
default path byte-equivalent to today's behavior on the CUDA machine (i7-7700K +
RTX 2080 Ti, zero env vars set):

- `VOICE_INPUT_ENGINE` ∈ {`cuda` (default), `cpu`, `auto`, `npu` (placeholder → #19)}
- `VOICE_INPUT_MODEL` ∈ model name, default `large-v3` (e.g. `small` for CPU smoke)

Unset both → exactly today's `WhisperModel(<large-v3 snapshots dir>, device="cuda",
compute_type="float16")` and the existing transcribe call.

## Scope / Non-goals

**In scope**: `engine.py` (new, single source of truth), `transcribe_once.py` (new CLI),
`voice-ptt.py` (swap local model logic for engine import), `test-mic.sh` /
`voice-toggle.sh` (inline CUDA snippets → transcribe_once.py call), `download-model.sh`
(HTTP-code validation, per-model file lists, bc removal, engine-aware verify),
`test_engine.py` (new tests), README one-row config-table addition deferred to #20.

**Non-goals**: NPU implementation (#19; only a fail-fast placeholder here), Wayland
typing/hotkey (#18), LD_LIBRARY_PATH restructuring (#16 touches none of it; #19 appends
NPU paths gated on engine=npu), hotwords/terms behavior change, README rewrite (#20).

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Default engine | `cuda` (strict current behavior), `auto` opt-in only | 7700K must not change execution path or gain silent CPU fallback; auto-probing by default turns "broken" into "slow" |
| Engine home | New `engine.py` module, GTK-free, no faster_whisper import at module level (lazy) | voice-ptt.py imports gi at top; CLI tools must reuse engine without GTK; single source of truth vs 3 drifting copies |
| CLI entry | New `transcribe_once.py <wav>` printing text | test-mic.sh / voice-toggle.sh inline python snippets are copy #2 and #3 of the same logic; convergence needed so engine selection reaches them; #18 then only swaps typing |
| Model resolution | `resolve_model_path(model)` generalizes the snapshots dir name; default string identical to today's | Behavior-preserving generalization |
| cuda branch | Verbatim move of the existing constructor line into engine.py | Reviewable as a move, not rewrite |
| npu value | `NotImplementedError("see #19")` at construction time | Reserve the enum, fail fast, no dead code |
| Unknown engine value | Fail fast with message listing valid values | Better than silent fallback (debuggability) |
| download-model.sh validation | `curl -f` + download to `.part` then atomic rename | #15: hf-mirror 404 body ("Entry not found", 15 B) saved as file and reported OK |
| download-model.sh file lists | Per-model manifest: large-v3 keeps today's 5 files; small = config.json, tokenizer.json, vocabulary.txt, model.bin | Repos differ per model (#15); unknown model → error listing supported names |
| download-model.sh verify | Calls `engine.build_model()` (respects VOICE_INPUT_ENGINE, default cuda) | Removes the hardcoded-cuda verify failure on no-CUDA machines |
| Size display | Bash integer arithmetic (`$((size/1048576))`) | OmniBook has no `bc` |
| LD_LIBRARY_PATH | Untouched in #16 (voice-ptt.py module block + all shell exports stay) | Missing nvidia paths are harmless; zero-risk posture for 7700K; NPU paths are #19, engine-gated |

## Component contracts

### engine.py (new, pure stdlib at import time)

```python
model_name(env=os.environ) -> str            # VOICE_INPUT_MODEL, default "large-v3"
engine_name(env=os.environ) -> str           # VOICE_INPUT_ENGINE, default "cuda";
                                             # raises ValueError listing valid values
resolve_model_path(model=None, base=None) -> str
    # default base: ~/.cache/huggingface/hub/models--Systran--faster-whisper-{model}/snapshots
    # scans sorted subdirs for one containing model.bin; raises RuntimeError with
    # actionable message ("run ./download-model.sh <model>")
build_model(engine=None, model=None, whisper_factory=None, warn=None) -> WhisperModel
    # whisper_factory: lazily imports faster_whisper when not injected (unit-test seam)
    # cuda: factory(path, device="cuda", compute_type="float16")   [verbatim move]
    # cpu:  factory(path, device="cpu",  compute_type="int8")
    # auto: try cuda, on Exception warn+stderr then cpu int8
    # npu:  NotImplementedError("see #19")
```

No module-level side effects; no GTK; no faster_whisper import until build_model.

### transcribe_once.py (new)

`python transcribe_once.py <wav>` → loads via engine.build_model(), transcribes with
`language="zh"`, prints text (same join/strip as voice-ptt.py) to stdout, exit 0;
errors → non-zero exit + stderr. Reuses the venv-site-packages LD_LIBRARY_PATH preamble
block (same 6 lines as voice-ptt.py) so it is standalone.

### voice-ptt.py (modified)

- Delete local `SNAPSHOTS_DIR` / `_resolve_model_path` / `load_model` body; keep
  `load_model()` as thin wrapper caching `engine.build_model()` in global `model`.
- LD_LIBRARY_PATH preamble, GUI flow, transcribe call site, terms/archive/media_pause
  imports: unchanged.

### test-mic.sh / voice-toggle.sh (modified)

- Inline python snippets + snapshots loops → `"$VENV/bin/python3" "$REPO_DIR/transcribe_once.py" "$WAV"`.
- Their LD_LIBRARY_PATH exports stay (needed on cuda, harmless on cpu).
- voice-toggle.sh `xdotool type` untouched (#18 scope).
- test-mic.sh keeps its printed labels/prompts.

### download-model.sh (modified)

- FILES per model (see decisions); `curl -fL -o "$filepath.part"` + `mv` on success.
- Verify step: `"$VENV/bin/python3" -c "import sys; sys.path.insert(0, REPO_DIR); import engine; engine.build_model()"`.
- bc → `$(( ))`.

## Degradation / failure paths

- Unknown engine/model env value → fail fast at startup/CLI with actionable message.
- Model not downloaded → RuntimeError naming the missing model and the download command.
- auto + cuda load failure → stderr warning + cpu int8 fallback (explicit opt-in only).
- transcribe_once.py failure → non-zero exit, empty stdout, stderr reason.

## Safety contract for the CUDA machine (7700K)

1. No env var set ⇒ engine=cuda, model=large-v3, constructor kwargs and resolved path
   string identical to HEAD 40f35bb.
2. terms.py / archive.py / media_pause.py: zero diff.
3. voice-ptt.sh: zero diff. voice-ptt.py LD_LIBRARY_PATH block: zero diff.
4. Transcribe call site in voice-ptt.py: zero diff.
5. Enforced by regression test A1 (default contract) + static checks A7/A9.

## Acceptance matrix

| ID | Feature | Verification | How | Pass criteria |
|----|---------|--------------|-----|---------------|
| A1 | Default contract (engine=cuda, model=large-v3, dir name) | Automated — unit | `./venv/bin/python -m unittest test_engine -v` | Contract test passes: defaults literal-equal to legacy strings |
| A2 | resolve_model_path generalization | Automated — unit | same | tmpdir fixtures (large-v3 layout / small layout / missing) → correct dir or RuntimeError; default base dir name == legacy |
| A3 | Engine value validation + npu placeholder | Automated — unit | same | unknown value → ValueError listing valid; `npu` → NotImplementedError |
| A4 | Constructor kwargs per engine (byte contract) | Automated — unit | same | injected stub factory records kwargs: cuda→(cuda,float16) exactly; cpu→(cpu,int8) |
| A5 | auto fallback | Automated — unit | same | stub factory raises for cuda → warn on stderr + cpu int8 used |
| A6 | transcribe_once CLI end-to-end (real model) | Automated — integration (local) | `VOICE_INPUT_ENGINE=cpu VOICE_INPUT_MODEL=small ./venv/bin/python transcribe_once.py <spoken wav>` on OmniBook | exit 0, non-empty stdout, plausible Chinese text |
| A7 | Shell wrappers converged (static) | Automated — static | grep: test-mic.sh/voice-toggle.sh contain no `WhisperModel(` inline, call transcribe_once.py, keep LD_LIBRARY_PATH export | all three greps as expected |
| A8 | download-model.sh hardening (static) | Automated — static | grep: `curl -f`, `.part` atomic move, per-model lists present, no `bc`, verify via engine | all present |
| A9 | Existing suites green + protected files untouched | Automated — unit + static | `python3 -m unittest test_terms test_archive test_media_pause`; `git diff --stat` for terms/archive/media_pause/voice-ptt.sh empty vs main | all pass / empty diff |
| U1 | 7700K CUDA regression check | User manual | On 7700K: checkout branch, no env vars, `./voice-ptt.sh`, hold-key record+transcribe | Model loads cuda float16; PTT flow works as before. Timing: next physical access to that machine (pending allowed) |
| U2 | download-model.sh small fresh run | User manual (observed) | Fresh cache dir: `./download-model.sh small` on OmniBook | 4 files land correctly, no "Entry not found" body, verify step passes under cpu |

## Testability split design (for A1–A5)

- All of A1–A5 test **engine.py pure/injectable functions only** — no GPU, no model
  download, no GTK, stdlib unittest; runnable on any machine (incl. 7700K).
- Test boundaries:
  - `env` is a passed dict (tests construct variants; never mutate os.environ).
  - `resolve_model_path(base=...)` gets a tmpdir with fabricated snapshot layouts;
    missing-vocab edge (model.bin absent everywhere) asserts the RuntimeError text.
  - `whisper_factory` injection isolates constructor dispatch from the real
    faster_whisper import; the stub records (path, device, compute_type).
  - `warn` injection captures fallback messages without asserting on stderr plumbing.
- Integration boundary (A6) exercises the real faster_whisper + small model + wav I/O
  once, on OmniBook only; failure there does not gate unit-level correctness elsewhere.

## Open decisions for user

1. Default engine = strict `cuda` (recommended) vs `auto` — encoded above as cuda.
2. LD_LIBRARY_PATH untouched in #16 — encoded above as untouched.
