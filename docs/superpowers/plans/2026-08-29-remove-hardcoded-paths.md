# Plan: Remove hardcoded absolute paths (#11)

Worktree: `.pi/worktrees/issue-11-remove-hardcoded-paths` (branch `issue-11-remove-hardcoded-paths`)

## Task 1 — voice-ptt.py

- `_REPO_DIR`/`VENV` from `__file__`; glob `python3*` site-packages for the three
  nvidia lib dirs in `LD_LIBRARY_PATH`
- Add `_resolve_model_path()` (scan snapshots for `model.bin`); use in `load_model()`
- Verify: `python3 -c "import ast; ast.parse(open('voice-ptt.py').read())"`; unit tests

## Task 2 — Shell scripts (voice-ptt.sh, voice-toggle.sh, test-mic.sh, download-model.sh)

- REPO_DIR derivation; dynamic site-packages (voice-ptt/voice-toggle/test-mic);
  dynamic MODEL_PATH (voice-toggle/test-mic); `-D default` for arecord in those two;
  download-model.sh: venv guard + `$REPO_DIR/test-mic.sh` hint
- Verify: `bash -n` on all four; run path-derivation snippet standalone

## Task 3 — Docs + READMEs

- sed the audio-archive plan doc (`/home/elling/...` → `~/...`)
- Both READMEs: remove Installation callouts + `hw:3` caveat
- Verify: `rg "/home/elling"` zero hits (tracked files)

## Task 4 — Behavioral verification

- Symlink venv into worktree (gitignored); `timeout 90 ./voice-ptt.sh` → "Ready!"
- Fresh clone to `~/tmp/voice-input-portable-test`; symlink venv; start → "Ready!"
- `python3 -m unittest test_terms test_archive test_media_pause`

## Task 5 — CR + PR + Zima + merge + cleanup

Reviewer fact/diff-check → PR ("Closes #11") → `zima:needs-review` → wait-cr.py →
convergence rules → squash merge → worktree remove → main pull.
