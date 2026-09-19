# Spec: NPU smoke test & benchmark — whisper-small on OmniBook NPU (#17)


## Goal

Independently verify the NPU inference chain (OpenVINO → level-zero → NPU 4) with
whisper-small, and produce local latency/quality data that decides whether #19
integrates the NPU engine. Code deliverable: `bench/npu-bench.py`. Data deliverable:
CPU-vs-NPU results table in issue #17.

## Scope / Non-goals

**In scope**: `bench/npu-bench.py` (new), model acquisition (download into HF cache,
not committed), results + verdict in #17. Python deps `openvino`/`openvino-genai`
already in venv (documented, not pinned — pinning/docs deferred to #19/#20).

**Non-goals**: engine integration into voice-ptt.py (#19), Wayland (#18), NPU
utilization tooling (xpu-smi absent — wall-time only, recorded as limitation),
larger models (turbo/large hang risk #1965).

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Primary model | `OpenVINO/whisper-small-int8-ov` (prebuilt) | official org export, INT8, GenAI-compatible README; zero local toolchain |
| Fallback chain | int4-ov → `Intel/whisper-small-openvino` → local optimum export (KV-cache fix per #1728) | NPU compatibility of prebuilts is unverified until run; #1728 shows decoder KV cache matters for static pipeline |
| Pipeline config | `WhisperPipeline(model_dir, device=..., {"STATIC_PIPELINE": True})` on NPU; plain on CPU | docs: NPU requires static pipeline |
| Comparison axes | (a) ov-genai CPU, (b) ov-genai NPU — same model dir, isolated device variable; reference: faster-whisper cpu small 3.4s from #16 A6 | apples-to-apples within OpenVINO; fw number as context only |
| LD_LIBRARY_PATH | bench script self-sets `os.environ["LD_LIBRARY_PATH"] = "/usr/lib/x86_64-linux-gnu:..."` before importing openvino | self-contained; encodes the jfox-documented requirement in code |
| Hang protection | per-run `signal.alarm` timeout (default 120s) → run reported as TIMEOUT, script continues | #1965 precedent; hang is a datapoint, not a stuck session |
| Metrics | model_load_s, first_transcribe_s (includes compile), then N warm runs → mean/min/max | NPU static compile can dominate first run; report separately |
| Runs | 1 first + 3 warm (default, `--runs` overridable) | enough for a decision, cheap to run |
| Bench input | wav path argument; primary input `/tmp/a6-trimmed.wav` (8.1s zh speech) | real speech from #16 A6; *.wav gitignored, not committed |
| Text quality | recorded verbatim per device in output table; human judges (U1) | automated similarity scoring would be circular (same model family) |

## bench/npu-bench.py contract

```
usage: npu-bench.py --model-dir DIR --device {cpu,npu,CPU,NPU} --wav FILE
                    [--runs N=3] [--timeout-s S=120] [--max-new-tokens N]

behavior:
  1. prepend /usr/lib/x86_64-linux-gnu to LD_LIBRARY_PATH before openvino import
  2. t0: WhisperPipeline(model_dir, device=DEV, config={"STATIC_PIPELINE": True} on NPU)
     (STATIC_PIPELINE off on CPU unless --static given — CPU default is dynamic)
  3. first transcribe (timed, may include NPU compile)
  4. N warm transcribes (each timed, alarm-guarded)
  5. print machine-readable block:
     device=... model_dir=... wav=...
     model_load_s=... first_transcribe_s=...
     warm_runs=N mean_s=... min_s=... max_s=...
     text_first=<verbatim transcript of first run>
     any_run_timeout=true/false
exit codes: 0 ok; 2 usage; 3 model/device failure (printed actionable error)
```

Testability split:
- `parse_args(argv) -> Namespace` — pure, unit-testable (test_bench.py)
- `format_report(metrics: dict) -> str` — pure, unit-testable
- `set_npu_library_path(env=dict) -> str` — pure (mutates injected dict), unit-testable
- `run_bench(model_dir, device, wav, runs, timeout_s)` — integration only (real model
  + device); NOT unit-mocked (mocking openvino_genai would test nothing)
- Test boundary: unit tests import bench module WITHOUT importing openvino
  (openvino import lives inside run_bench / lazy) — same purity contract as engine.py.

## Acceptance matrix

| ID | Feature | Verification | How | Pass criteria |
|----|---------|--------------|-----|---------------|
| A1 | NPU enumeration with/without env | Automated — integration | `LD_LIBRARY_PATH=... python -c "ov.Core().available_devices"` (already run; re-run inside bench context) | with env → contains NPU; without → CPU only; recorded in issue |
| A2 | bench script unit layer | Automated — unit | `./venv/bin/python -m unittest test_bench -v` (parse_args/format_report/set_npu_library_path, no openvino import) | tests pass; module import does not pull openvino |
| A3 | prebuilt model download + CPU pipeline run | Automated — integration | `huggingface_hub`/curl download `OpenVINO/whisper-small-int8-ov`; `bench/npu-bench.py --device CPU --runs 3` | exit 0, non-empty text, metrics printed |
| A4 | NPU pipeline run completes on small | Automated — integration (E2E) | `bench/npu-bench.py --device NPU --runs 3` | exit 0, non-empty text, no hang (alarm-guarded); if TIMEOUT/hang → recorded honestly as NPU blocker finding (still "executed", verdict informs #19) |
| A5 | latency table CPU vs NPU | Automated — integration | A3+A4 outputs compiled into table, posted to #17 | both devices' model_load/first/warm numbers present (or failure documented per A4) |
| U1 | NPU transcript quality | User manual | user reads NPU vs CPU transcripts of the same wav | transcripts both reasonable (comparable to #16 A6's quality bar); user judges |
| U2 | verdict for #19 | User manual | user reads A5 table + U1 | explicit go/no-go/conditional decision recorded in #17 |

Pending-by-nature: U1/U2 require the data to exist first; executed right after A3-A5
in the same session.

## Failure paths / honesty rules

- Prebuilt model incompatible with static NPU pipeline → try fallback chain; each
  attempt recorded (model, error, time). All fallbacks fail → issue documents blocker;
  #19 decision becomes "fix export first" — still a valid #17 outcome.
- NPU hang → alarm reports TIMEOUT; do not retry beyond configured runs.
- CPU pipeline unexpectedly fails → that's a blocker finding too (GenAI vs faster-
  whisper difference), recorded.
