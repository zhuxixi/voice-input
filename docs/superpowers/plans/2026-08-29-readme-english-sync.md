# Plan: README English rewrite & content sync (#10)

Worktree: `.pi/worktrees/issue-10-readme-english-sync` (branch `issue-10-readme-english-sync`)

## Task 1 — Write English README.md

Full rewrite per spec structure. Verify each factual claim against the contract in
the spec (env vars, key, paths, file table, commands).

- Files: `README.md` (overwrite)
- Check: every section present per spec; no author-path leakage (link #11 instead of
  embedding the path); language switcher link to `README.zh-CN.md`

## Task 2 — Write README.zh-CN.md

Chinese version = old README content + all content fixes (hotwords section, PipeWire
capture, file table, config table, tests, Roadmap, badges, TOC, license link).

- Files: `README.zh-CN.md` (new)
- Check: structure mirrors English version 1:1; no author-path leakage

## Task 3 — Fact-check review

Independent reviewer subagent checks both READMEs against the code in the worktree
(claims vs `voice-ptt.py`, `terms.py`, `archive.py`, `media_pause.py`, shell scripts).

- Fix findings, re-run until clean.

## Task 4 — PR + Zima CR + merge

`gh pr create` (body links #10 via "Closes #10"), label `zima:needs-review`, block-wait
CR per zima-pr-monitor, merge on convergence, cleanup worktree.
