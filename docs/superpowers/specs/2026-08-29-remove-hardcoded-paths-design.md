# Spec: Remove hardcoded absolute paths for portability (#11)

## Goal

No tracked file references the author's machine (`/home/elling`). Every runtime
path derives from the script's own location or the user's home. Behavior on the
author's machine is byte-identical: repo lives at the same path, default audio
source is the same mic.

## Design

### Path derivation (core)

| What | Before | After |
|---|---|---|
| Repo dir (sh) | literal `/home/elling/.local/share/voice-input` | `REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"` |
| Repo dir (py) | literal in `VENV` | `_REPO_DIR = os.path.dirname(os.path.abspath(__file__))` |
| venv python | `"$VENV/bin/python3"` (unchanged, venv-relative already) | unchanged |
| System python | `/usr/bin/python3` in `voice-ptt.sh` exec | **keep** (distro-standard, needed for gi/GTK) |
| site-packages | literal `lib/python3.12/...` | glob `lib/python3*/site-packages` (first hit) |
| Model snapshot | literal hash dir `edaa852e...` | scan `snapshots/*/` for the dir containing `model.bin` (first hit, sorted) |

Model snapshot auto-resolution also fixes the download/dir mismatch: fresh users
get `snapshots/downloaded` from `download-model.sh`, which the code now accepts.

### Scripts affected

- `voice-ptt.sh`: REPO_DIR, dynamic site-packages for LD_LIBRARY_PATH/PYTHONPATH,
  `exec /usr/bin/python3 "$REPO_DIR/voice-ptt.py"`
- `voice-toggle.sh`: REPO_DIR, dynamic site-packages, dynamic MODEL_PATH, `arecord -D default`
- `test-mic.sh`: same as voice-toggle.sh
- `download-model.sh`: REPO_DIR/venv only; final hint prints `$REPO_DIR/test-mic.sh`
- `voice-ptt.py`: `_REPO_DIR`/`VENV` from `__file__`, glob site-packages for nvidia
  libs, `_resolve_model_path()` raising a clear "run ./download-model.sh" error
- `docs/superpowers/plans/2026-07-19-audio-archive.md`: `/home/elling/.local/share/voice-input` → `~/.local/share/voice-input`
- `README.md` / `README.zh-CN.md`: drop the two Installation callouts (hardcoded
  paths + model snapshot) and the `hw:3` caveat — all obsolete after this change

### Guards

- site-packages glob empty → loud error with the venv creation command
- model scan empty → loud error pointing at `./download-model.sh`

## Local-machine invariants (must not change)

- Derived `VENV` == `/home/elling/.local/share/voice-input/venv` (same checkout location)
- Derived model dir == the existing `edaa852e...` snapshot (contains `model.bin`)
- `-D default` == the same PD200X mic (PipeWire default source)
- `/usr/bin/python3` exec unchanged

## Non-goals

- History rewrite (username remains in old commits; separate decision, see issue)
- Windows support (#9)
- Renaming scripts / restructuring into a package

## Acceptance (from issue #11)

- `rg "/home/elling"` zero hits in tracked files
- Unit tests pass; `bash -n` clean on all scripts
- Worktree smoke: `./voice-ptt.sh` reaches "Ready!" (model preloads)
- Fresh clone to `~/tmp/` derives paths correctly and starts
