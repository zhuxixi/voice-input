# #28 Singleton Lock + Unique Recording Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `voice_hold.py` a flock-based single-instance gate (second instance exits 4, no systemd restart loop) and replace the fixed `/tmp` recording paths with per-recording `mkstemp` paths in `voice_hold.py`, `voice-ptt.py` and `voice-toggle.sh`.

**Architecture:** Two pure functions (`runtime_tmpdir`, `singleton_lock_path`) derive paths; two side-effect seams (`acquire_singleton_lock`, `new_recording_path`) own all fs/flock work. `HoldDaemon.run()` gates on the lock before preflight; `start_recording()`/`_deliver()` hand a per-recording path through an instance attribute.

**Tech Stack:** Python 3 stdlib only (`fcntl`, `tempfile`, `os`), `unittest` + `mock`, bash `mktemp` for the shell script.

**Spec:** `docs/superpowers/specs/2026-09-30-singleton-lock-design.md` (approved v2; §6 has the acceptance matrix this plan traces to)

## Global Constraints

- `voice_hold.py` module top level stays **stdlib-only** (pinned by `test_hold.TestModulePurity`). No new dependencies anywhere. — spec NG4
- Do NOT touch: `transition()` semantics, `paste.py`, device selection, #29's paste seam, #25 worker architecture. — spec NG1/NG2/NG3
- Exit code contract: 3 = preflight failure (existing), **4 = duplicate/unlockable instance** (`EXIT_DUPLICATE_INSTANCE`). — spec D3
- `acquire_singleton_lock` **never raises**; any failure is `(None, …)` and `run()` maps it to exit 4. — spec D3/B1
- Lock fd is **non-inheritable** (PEP 446 default; never `set_inheritable(True)`). — spec M3
- Lock path: `<tmpdir>/voice-input-hold-<uid>.lock`, opened `O_RDWR|O_CREAT|O_NOFOLLOW`, 0600. — spec D1/D2
- `$TMPDIR` is honored only when non-empty AND absolute; otherwise `tempfile.gettempdir()`. — spec D9
- Test style: `unittest` + `mock` in `test_hold.py`; every new class isolates fs under a per-test tmpdir; never touch GTK/D-Bus/arecord/evdev from tests. — spec §5
- Full suite (README L363): `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v` must pass after every task.
- All work in this worktree; commit per task, conventional commits, English messages; `git add <file>` per file (never `git add -A`).

## Review Focus

Inputs/conditions the spec implies but individual happy-path tests don't exercise; each is pinned by the test named here in its owning task:

1. **Relative `$TMPDIR`** — must fall back to `gettempdir()` (a relative lock path = one lock per cwd, mutex silently gone). → Task 1 `test_runtime_tmpdir_env_variants`.
2. **Lock file un-openable** (foreign-owned 0600 file → EACCES; symlink → ELOOP via `O_NOFOLLOW`) — must return `(None, None, err)`, never raise, never spawn listeners. → Task 2 `test_open_failure_never_raises`, Task 6 `test_open_error_exits_4_with_errno_message`.
3. **Garbage in the lock file** (crash mid-pid-write) — failed acquirer must degrade to `holder=None`, not crash. → Task 2 `test_garbage_lock_file_content_degrades_to_no_holder`.
4. **Hung transcribe child must not inherit the lock** — a hung child keeping the flock past parent death would make every new instance refuse to start (permanent deafness). → Task 2 `test_second_acquire_blocked_pid_reported_fd_not_inheritable`.
5. **`arecord` spawn failure mid-recording-start** — must leave no empty wav slot behind and still propagate the exception. → Task 4 `test_start_recording_popen_failure_leaves_no_file`.
6. **`_deliver` racing a fast re-press** — the worker must snapshot `self.wav_path` (and survive `wav_path is None`). → Task 5 `test_deliver_without_path_reports_and_returns`.

---

### Task 0: Baseline the worktree

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite before touching anything**

Run: `cd <worktree> && python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v 2>&1 | tail -5`
Expected: `OK` (same as main @ `5f8bf72`).

- [ ] **Step 2: If venv-dependent failures appear, link the main repo's venv**

The worktree has no `venv/` (gitignored). If (and only if) Step 1 fails on venv paths, run `ln -s /home/elling/work/git-repo/voice-input/venv <worktree>/venv` and re-run. Do not commit the symlink (it is ignored).

---

### Task 1: Pure path derivation (`runtime_tmpdir`, `singleton_lock_path`, constants) — A1

**Files:**
- Modify: `voice_hold.py` (imports block + near line 30 where `WAVFILE` lives)
- Test: `test_hold.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (later tasks rely on these exact names): `LOCK_STEM = "voice-input-hold"`, `EXIT_DUPLICATE_INSTANCE = 4`, `RECORDING_PREFIX = "voice-input-hold-"`, `RECORDING_SUFFIX = ".wav"`, `runtime_tmpdir(env) -> str`, `singleton_lock_path(uid: int, tmpdir: str) -> str`.

- [ ] **Step 1: Write the failing test** — append to `test_hold.py` (and add `shutil`, `tempfile` to the import block at the top):

```python
class TestSingletonLock(unittest.TestCase):
    """A1-A6 (#28): lock path derivation + flock semantics. All fs work is
    isolated under a per-test tmpdir; nothing here touches arecord/evdev."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-lock-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _path(self):
        return voice_hold.singleton_lock_path(os.getuid(), self._tmp)

    # A1
    def test_runtime_tmpdir_env_variants(self):
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": "/var/tmp"}),
                         "/var/tmp")
        self.assertEqual(voice_hold.runtime_tmpdir({}),
                         tempfile.gettempdir())
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": ""}),
                         tempfile.gettempdir())
        # D9: relative TMPDIR must be refused — a relative lock path would
        # mean one lock per cwd and the mutex would silently vanish
        self.assertEqual(voice_hold.runtime_tmpdir({"TMPDIR": "rel/dir"}),
                         tempfile.gettempdir())

    # A1
    def test_singleton_lock_path_contract(self):
        p = voice_hold.singleton_lock_path(1000, "/var/tmp")
        self.assertEqual(p, "/var/tmp/voice-input-hold-1000.lock")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_hold.TestSingletonLock -v`
Expected: FAIL with `AttributeError: module 'voice_hold' has no attribute 'runtime_tmpdir'`.

- [ ] **Step 3: Implement** — in `voice_hold.py`, add `import fcntl` and `import tempfile` to the stdlib import block, add the constants right after `ENV_DEVICE = "VOICE_INPUT_DEVICE"` (keep the `WAVFILE = ...` line for now — Task 7 removes it), and add the two functions after `pick_device()`:

```python
LOCK_STEM = "voice-input-hold"          # lock filename stem (#28)
EXIT_DUPLICATE_INSTANCE = 4             # D3: refused start, never restart-loop
RECORDING_PREFIX = "voice-input-hold-"  # per-recording wav prefix (#28)
RECORDING_SUFFIX = ".wav"


def runtime_tmpdir(env) -> str:
    """Lock/recording directory (#28 D9): $TMPDIR only when set to a
    non-empty ABSOLUTE path, else tempfile.gettempdir(). Refusing relative
    paths matches gettempdir semantics — a relative lock path would mean
    one lock per cwd and the mutex would silently vanish."""
    cand = (env or {}).get("TMPDIR", "")
    if cand and os.path.isabs(cand):
        return cand
    return tempfile.gettempdir()


def singleton_lock_path(uid: int, tmpdir: str) -> str:
    """Lock file path for uid (pure, no I/O)."""
    return os.path.join(tmpdir, f"{LOCK_STEM}-{uid}.lock")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_hold.TestSingletonLock -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run purity + full suite**

Run: `python3 -m unittest test_hold -v`
Expected: all PASS (imports stayed stdlib-only).

- [ ] **Step 6: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): lock/recording path derivation helpers (#28)"
```

---

### Task 2: `acquire_singleton_lock` — A2/A3/A5/A6 + garbage robustness

**Files:**
- Modify: `voice_hold.py` (after `singleton_lock_path`)
- Test: `test_hold.py` (inside `TestSingletonLock`)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (path passed in), but tests use `singleton_lock_path`.
- Produces: `acquire_singleton_lock(path: str) -> tuple[int|None, int|None, str|None]`; success `(fd, None, None)`; busy `(None, holder_pid|None, None)`; un-openable `(None, None, strerror_str)`. Never raises.

- [ ] **Step 1: Write the failing tests** — append inside `TestSingletonLock`:

```python
    # A2 + A6
    def test_second_acquire_blocked_pid_reported_fd_not_inheritable(self):
        path = self._path()
        fd, holder, err = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd)
        self.assertIsNone(holder)
        self.assertIsNone(err)
        # M3: children (arecord/transcribe) must never hold the lock — a
        # hung child keeping the flock past parent death would make every
        # new instance refuse to start (permanent deafness)
        self.assertFalse(os.get_inheritable(fd))
        self.addCleanup(os.close, fd)
        fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNone(fd2)
        self.assertIsNone(err2)
        self.assertEqual(holder2, os.getpid())  # we are the holder
        with open(path) as f:  # file carries our pid for the next acquirer
            self.assertEqual(f.read(), str(os.getpid()))

    # Review Focus 3: garbage in the pid slot (crash mid-write) degrades
    # to holder=None instead of crashing the failed acquirer
    def test_garbage_lock_file_content_degrades_to_no_holder(self):
        path = self._path()
        fd, _, _ = voice_hold.acquire_singleton_lock(path)
        self.addCleanup(os.close, fd)
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, b"not-a-pid")
        fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNone(fd2)
        self.assertIsNone(err2)
        self.assertIsNone(holder2)

    # A3
    def test_release_then_reacquire_no_stale_lock(self):
        path = self._path()
        fd, _, _ = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd)
        os.close(fd)  # process-death analogue: kernel releases the flock
        fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd2)
        self.assertIsNone(err2)
        os.close(fd2)

    # A5 / Review Focus 2: un-openable lock file must not raise
    def test_open_failure_never_raises(self):
        # occupied-by-another semantics: a file we lack permission to open
        p = os.path.join(self._tmp, "blocked.lock")
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o600)
        res = voice_hold.acquire_singleton_lock(p)
        self.assertIsNone(res[0])
        self.assertIsNone(res[1])
        self.assertTrue(res[2])  # readable errno string, no exception
        # symlink must be rejected by O_NOFOLLOW (ELOOP)
        target = os.path.join(self._tmp, "target")
        with open(target, "w"):
            pass
        link = os.path.join(self._tmp, "link.lock")
        os.symlink(target, link)
        res2 = voice_hold.acquire_singleton_lock(link)
        self.assertIsNone(res2[0])
        self.assertIsNone(res2[1])
        self.assertTrue(res2[2])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestSingletonLock -v`
Expected: the 4 new tests FAIL with `AttributeError: ... 'acquire_singleton_lock'`.

- [ ] **Step 3: Implement** — after `singleton_lock_path`:

```python
def acquire_singleton_lock(path: str):
    """Single-instance gate — the only place flock lives (#28).

    Returns (fd, holder_pid, error); NEVER raises (spec D3/B1):
      success        -> (fd, None, None) — caller keeps fd open for the
                        process lifetime (close releases the lock); fd is
                        non-inheritable (PEP 446) so arecord/transcribe
                        children never hold the lock (spec M3).
      already locked -> (None, <pid read from the file, best effort>, None)
      cannot open    -> (None, None, "<strerror>")  # EACCES/ELOOP/… incl.
                                                              O_NOFOLLOW
    The pid inside the file is diagnostic only ("last holder" — may be a
    dead predecessor); the mutex decision is always the flock itself.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        return None, None, (os.strerror(e.errno) if e.errno else str(e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 32).decode(errors="replace").strip()
            if data.isdigit():
                holder = int(data)
        except OSError:
            pass
        os.close(fd)
        return None, holder, None
    except OSError as e:
        os.close(fd)
        return None, None, (os.strerror(e.errno) if e.errno else str(e))
    try:  # record our pid for the next failed acquirer (diagnostic only)
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd, None, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold.TestSingletonLock -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): flock single-instance gate, never-raising (#28)"
```

---

### Task 3: Cross-process mutex with ready-line handshake — A4

**Files:**
- Test: `test_hold.py` (inside `TestSingletonLock`)

**Interfaces:**
- Consumes: `acquire_singleton_lock` (Task 2), `REPO` constant already in `test_hold.py`.
- Produces: nothing (evidence only).

- [ ] **Step 1: Write the test** — append inside `TestSingletonLock`:

```python
    # A4 — real second process, with the B2 ready-line handshake: asserting
    # before the child provably holds the lock would race its flock and
    # flip the assertion (chronic-flaky classic, cf. pi-agent-board #95)
    def test_cross_process_mutex(self):
        path = self._path()
        code = (
            "import sys, time; sys.path.insert(0, %r); import voice_hold;"
            "fd, _, _ = voice_hold.acquire_singleton_lock(%r);"
            "assert fd is not None, 'child failed to acquire';"
            "print('ready', flush=True); time.sleep(30)"
        ) % (REPO, path)
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, text=True, cwd=REPO)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            fd, holder, err = voice_hold.acquire_singleton_lock(path)
            self.assertIsNone(fd)
            self.assertIsNone(err)
            self.assertEqual(holder, proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=5)  # the flock dies with the child
        fd2, holder2, err2 = voice_hold.acquire_singleton_lock(path)
        self.assertIsNotNone(fd2)  # A3 semantics across processes
        self.assertIsNone(err2)
        os.close(fd2)
```

- [ ] **Step 2: Run it three times (flakiness guard)**

Run: `for i in 1 2 3; do python3 -m unittest test_hold.TestSingletonLock.test_cross_process_mutex -v || break; done`
Expected: PASS all 3 runs, each well under 5s.

- [ ] **Step 3: Commit**

```bash
git add test_hold.py
git commit -m "test(hold): cross-process flock mutex with ready handshake (#28)"
```

---

### Task 4: `new_recording_path` + `start_recording` wiring — A7/A10 + M1

**Files:**
- Modify: `voice_hold.py` (`HoldDaemon.__init__`, `start_recording`, new function after `acquire_singleton_lock`)
- Test: `test_hold.py` (new class `TestRecordingPath`)

**Interfaces:**
- Consumes: `RECORDING_PREFIX`, `RECORDING_SUFFIX`, `runtime_tmpdir` (Task 1).
- Produces: `new_recording_path(tmpdir: str) -> str`; `HoldDaemon.wav_path: str|None` attribute (Task 5 depends on it).

- [ ] **Step 1: Write the failing tests** — append to `test_hold.py`:

```python
class TestRecordingPath(unittest.TestCase):
    """A7/A8/A10 (#28): per-recording unique wav path through
    start_recording/_deliver, with arecord/paste/transcribe mocked."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vh-wav-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _daemon(self):
        d = voice_hold.HoldDaemon(env={"TMPDIR": self._tmp,
                                       "VOICE_INPUT_ENGINE": "cpu"})
        d.overlay = mock.Mock()   # never touch GTK from tests
        d._media_pause = None     # never touch D-Bus from tests
        return d

    # A7
    def test_new_recording_path_unique_in_dir(self):
        p1 = voice_hold.new_recording_path(self._tmp)
        p2 = voice_hold.new_recording_path(self._tmp)
        self.assertNotEqual(p1, p2)
        for p in (p1, p2):
            self.assertEqual(os.path.dirname(p), self._tmp)
            self.assertTrue(os.path.exists(p))  # slot created
            self.assertEqual(os.path.getsize(p), 0)

    # A7 + A10
    def test_start_recording_uses_fresh_path_and_logs_it(self):
        d = self._daemon()
        with mock.patch.object(voice_hold.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            d.start_recording()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "arecord")
        self.assertEqual(argv[-1], d.wav_path)          # A7: unique path
        self.assertTrue(os.path.exists(d.wav_path))     # slot exists
        self.assertIn("recording -> ", out.getvalue())  # A10: log carries it
        self.assertIn(d.wav_path, out.getvalue())

    # A7/M1/Review Focus 5: arecord spawn failure leaves no empty slot
    def test_start_recording_popen_failure_leaves_no_file(self):
        d = self._daemon()

        def boom(*a, **k):
            raise OSError("arecord missing")

        with mock.patch.object(voice_hold.subprocess, "Popen",
                               side_effect=boom):
            with self.assertRaises(OSError):
                d.start_recording()
        self.assertFalse(os.path.exists(d.wav_path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestRecordingPath -v`
Expected: 3 FAIL (`AttributeError: ... 'new_recording_path'` / `start_recording` still uses the fixed `WAVFILE`).

- [ ] **Step 3: Implement**

3a. In `HoldDaemon.__init__`, after `self.rec_proc = None` add:

```python
        self.wav_path = None    # per-recording path, set by start_recording (#28)
```

3b. After `acquire_singleton_lock` add:

```python
def new_recording_path(tmpdir: str) -> str:
    """Fresh unique recording slot (#28 D4): mkstemp gives O_EXCL creation
    (no TOCTOU with another instance); arecord opens/truncates the path."""
    fd, path = tempfile.mkstemp(
        prefix=RECORDING_PREFIX, suffix=RECORDING_SUFFIX, dir=tmpdir)
    os.close(fd)
    return path
```

3c. Replace the body of `start_recording` (drop the `unlink(WAVFILE)` block; wrap `Popen`; new log line):

```python
    def start_recording(self):
        self.wav_path = new_recording_path(runtime_tmpdir(self.env))
        if self._media_pause and self._media_pause.PAUSE_MEDIA_ENABLED:
            try:
                self._paused = self._media_pause.pause_playing()
            except Exception as e:
                print(f"[voice-hold] pause_media failed: {e}", file=sys.stderr)
        try:
            self.rec_proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
                 "-D", "default", self.wav_path],
                stdout=subprocess.DEVNULL, stderr=sys.stderr)
        except Exception:
            # M1 (#28): leave no empty slot behind; daemon crash/restart
            # semantics unchanged (the exception still propagates)
            try:
                os.unlink(self.wav_path)
            except OSError:
                pass
            raise
        self.overlay.show("● REC", "#ff5555")
        print(f"[voice-hold] recording -> {self.wav_path}", flush=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold.TestRecordingPath -v`
Expected: 3 PASS.

- [ ] **Step 5: Run full hold suite**

Run: `python3 -m unittest test_hold -v`
Expected: all PASS (state-machine/busy-drop tests untouched).

- [ ] **Step 6: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): per-recording wav path + spawn-failure cleanup (#28)"
```

---

### Task 5: `_deliver` uses the instance path — A8 + defensive snapshot

**Files:**
- Modify: `voice_hold.py:252-292` (`_deliver`)
- Test: `test_hold.py` (inside `TestRecordingPath`)

**Interfaces:**
- Consumes: `HoldDaemon.wav_path` (Task 4), `_spawn_paste` (#29 seam).
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests** — append inside `TestRecordingPath`:

```python
    # A8
    def test_deliver_transcribes_instance_path_and_cleans_up(self):
        d = self._daemon()
        wav = voice_hold.new_recording_path(self._tmp)
        with open(wav, "wb") as f:
            f.write(b"\0" * 2048)   # pass the <1KB guard without arecord
        d.wav_path = wav
        d._spawn_paste = mock.Mock(return_value=(0, False))  # #29 seam
        with mock.patch.object(voice_hold.subprocess, "check_output",
                               return_value="你好\n") as co:
            d._deliver()
        argv = co.call_args.args[0]
        self.assertTrue(argv[1].endswith("transcribe_once.py"))
        self.assertEqual(argv[2], wav)              # A8: argv carries it
        self.assertFalse(os.path.exists(wav))       # A8: cleaned up
        d._spawn_paste.assert_called_once_with("你好")
        self.assertFalse(d.busy)                    # always un-busied

    # Review Focus 6: worker survives a missing wav_path (snapshot guard)
    def test_deliver_without_path_reports_and_returns(self):
        d = self._daemon()
        d.wav_path = None
        with mock.patch.object(voice_hold.subprocess, "check_output") as co:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                d._deliver()
        co.assert_not_called()
        self.assertIn("no recording path", err.getvalue())
        self.assertFalse(d.busy)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestRecordingPath.test_deliver_transcribes_instance_path_and_cleans_up test_hold.TestRecordingPath.test_deliver_without_path_reports_and_returns -v`
Expected: FAIL (first: transcribe argv is the old fixed path → `argv[2] != wav`; second: `_deliver` still reads `WAVFILE`, no "no recording path" line).

- [ ] **Step 3: Implement** — in `_deliver`, snapshot the path right after the flush sleep and switch every `WAVFILE` use in this method to the local:

```python
        try:
            time.sleep(0.3)  # let arecord finish flushing the wav (mirrors voice-ptt)
            wav = self.wav_path  # snapshot: busy-drop keeps this stable, but
            if not wav:          # never trust a None path in a worker thread
                print("[voice-hold] no recording path (internal error)",
                      file=sys.stderr)
                return
            if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                print("[voice-hold] recording empty/too short (<1KB)",
                      file=sys.stderr)
                return
```

and in the transcribe block replace both `WAVFILE` occurrences (`transcribe_once.py", WAVFILE]` → `wav]`) and the cleanup:

```python
            finally:
                if os.path.exists(wav):
                    os.unlink(wav)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold.TestRecordingPath -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): _deliver consumes per-recording path (#28)"
```

---

### Task 6: `run()` lock gate — A9

**Files:**
- Modify: `voice_hold.py` (`HoldDaemon.__init__` + `run()`)
- Test: `test_hold.py` (new class `TestDuplicateInstance`)

**Interfaces:**
- Consumes: `acquire_singleton_lock`, `singleton_lock_path`, `runtime_tmpdir`, `EXIT_DUPLICATE_INSTANCE` (Tasks 1–2).
- Produces: `HoldDaemon._lock_fd` attribute; `run()` returns 4 on any lock failure before preflight/pick_device.

- [ ] **Step 1: Write the failing tests** — append to `test_hold.py`:

```python
class TestDuplicateInstance(unittest.TestCase):
    """A9 (#28): a failed lock acquisition exits 4 before any listening."""

    def _daemon(self):
        return voice_hold.HoldDaemon(env={})

    def test_busy_lock_exits_4_without_listening(self):
        d = self._daemon()
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(None, 4242, None)), \
             mock.patch.object(
                 voice_hold.HoldDaemon, "_preflight",
                 return_value=[]) as pre, \
             mock.patch.object(voice_hold, "pick_device") as pick:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = d.run()
        self.assertEqual(rc, 4)
        self.assertEqual(rc, voice_hold.EXIT_DUPLICATE_INSTANCE)
        pre.assert_not_called()   # D5: lock gate comes before preflight
        pick.assert_not_called()
        out = err.getvalue()
        self.assertIn("another instance already running", out)
        self.assertIn("last holder pid 4242", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    # Review Focus 2: un-openable lock maps to exit 4 too (never a
    # traceback exit code that would defeat RestartPreventExitStatus)
    def test_open_error_exits_4_with_errno_message(self):
        d = self._daemon()
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(None, None, "Permission denied")), \
             mock.patch.object(voice_hold, "pick_device") as pick:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = d.run()
        self.assertEqual(rc, 4)
        pick.assert_not_called()
        out = err.getvalue()
        self.assertIn("cannot open lock file", out)
        self.assertIn("Permission denied", out)

    def test_success_keeps_lock_fd_for_process_lifetime(self):
        d = self._daemon()
        fd = os.open(os.devnull, os.O_RDONLY)  # stand-in for a real lock fd
        self.addCleanup(os.close, fd)
        with mock.patch.object(
                voice_hold, "acquire_singleton_lock",
                return_value=(fd, None, None)), \
             mock.patch.object(
                 voice_hold.HoldDaemon, "_preflight",
                 return_value=["fake missing item"]):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = d.run()   # preflight failure -> 3 (existing semantics)
        self.assertEqual(rc, 3)
        self.assertEqual(d._lock_fd, fd)   # held, not closed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_hold.TestDuplicateInstance -v`
Expected: FAIL (run() has no lock gate yet; first two tests will crash into `pick_device`/`_preflight` of the real environment, third has no `_lock_fd`).

- [ ] **Step 3: Implement**

3a. In `HoldDaemon.__init__` after `self.rec_proc = None` (next to `self.wav_path`):

```python
        self._lock_fd = None   # singleton flock held for process lifetime (#28)
```

3b. At the top of `run()`, before `missing = self._preflight()`:

```python
        lock_path = singleton_lock_path(os.getuid(),
                                        runtime_tmpdir(self.env))
        fd, holder, err = acquire_singleton_lock(lock_path)
        if fd is None:
            # D3/B1 (#28): busy AND un-openable both exit 4 — any other
            # exit path would restart-loop under Restart=always until the
            # start limit trips. Exit 4 is pinned by RestartPreventExitStatus.
            if err is None:
                detail = ("another instance already running "
                          f"(last holder pid {holder})")
            else:
                detail = f"cannot open lock file {lock_path}: {err}"
            print(f"[voice-hold] {detail} — refusing to start",
                  file=sys.stderr)
            return EXIT_DUPLICATE_INSTANCE
        self._lock_fd = fd  # close = release; process exit releases anyway
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_hold.TestDuplicateInstance -v`
Expected: 3 PASS.

- [ ] **Step 5: Run the full hold suite**

Run: `python3 -m unittest test_hold -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add voice_hold.py test_hold.py
git commit -m "feat(hold): refuse duplicate instances via flock gate, exit 4 (#28)"
```

---

### Task 7: Remove `WAVFILE` + fix the stale docstring — spec §4.2 (m2)

**Files:**
- Modify: `voice_hold.py` (line 30 constant; `_paste_and_report` docstring)

**Interfaces:**
- Consumes: Tasks 4–6 (no remaining `WAVFILE` uses).
- Produces: nothing new.

- [ ] **Step 1: Verify no code references remain**

Run: `grep -n "WAVFILE" voice_hold.py`
Expected: only the constant line (`WAVFILE = "/tmp/voice-input-hold.wav"`) and the docstring mention in `_paste_and_report`.

- [ ] **Step 2: Delete the constant and update the docstring**

Delete the `WAVFILE = "/tmp/voice-input-hold.wav"` line. In `_paste_and_report`, replace the docstring tail:

```python
        """Deliver `text` and log the outcome (#29 test seam: depends only
        on _spawn_paste so the result branches are unit-testable without
        faking the transcribe pipeline — driving _deliver would still need
        a real recording file, per-recording path since #28)."""
```

- [ ] **Step 3: Verify zero references and run the full suite**

Run: `grep -n "WAVFILE" voice_hold.py; python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v 2>&1 | tail -3`
Expected: grep prints nothing; suite `OK` (A12 checkpoint).

- [ ] **Step 4: Commit**

```bash
git add voice_hold.py
git commit -m "refactor(hold): drop the fixed WAVFILE constant (#28)"
```

---

### Task 8: X11 scripts — `voice-ptt.py` + `voice-toggle.sh` path uniqueness (D7, part of A11)

**Files:**
- Modify: `voice-ptt.py` (imports, `WAVFILE` constant L60, `start_recording` L132-148, `stop_recording` L195-236)
- Modify: `voice-toggle.sh:34`

**Interfaces:**
- Consumes: nothing from voice_hold (deliberately — no cross-script import; `tempfile` inline).
- Produces: per-recording unique paths in both scripts.

- [ ] **Step 1: voice-ptt.py — imports and constant**

Add `import tempfile` to the stdlib import block near the top. Replace `WAVFILE = "/tmp/voice-input-recording.wav"` with:

```python
# Per-recording unique path (#28 D7): no more shared fixed name — a
# voice-ptt instance and a voice-toggle run (both X11 path, same old name)
# used to truncate/delete each other's recording. Assigned in
# start_recording; stop_recording snapshots it before its 0.3s settle.
WAVFILE = None
```

- [ ] **Step 2: voice-ptt.py — start_recording**

Change the `global` line to include `WAVFILE`:

```python
def start_recording():
    global recording, rec_proc, active_window, _paused_players, WAVFILE
```

Replace the `if os.path.exists(WAVFILE): os.unlink(WAVFILE)` block with:

```python
    fd, WAVFILE = tempfile.mkstemp(prefix="voice-input-recording-",
                                    suffix=".wav")
    os.close(fd)
```

- [ ] **Step 3: voice-ptt.py — stop_recording snapshot + local uses**

Right after the `if not recording: return` / `recording = False` guard lines, add:

```python
    wav = WAVFILE  # snapshot: a fast re-press during the 0.3s settle must
                   # not swap the path under this transcription (#28)
```

Then in `stop_recording` only, replace every `WAVFILE` reference with `wav` (four sites: the `<1KB` size check, `m.transcribe(wav, ...)`, `archive_recording(wav, text, text)`, and the final `os.unlink` cleanup). Do NOT touch any other function.

- [ ] **Step 4: voice-toggle.sh**

Replace `WAVFILE="/tmp/voice-input-recording.wav"` with:

```bash
# Per-recording unique path (#28 D7): no shared fixed name with voice-ptt.py
WAVFILE="$(mktemp --suffix=.wav "${TMPDIR:-/tmp}/voice-input-recording-XXXXXX")"
```

- [ ] **Step 5: Static checks (A11 half)**

Run: `bash -n voice-toggle.sh && python3 -m py_compile voice-ptt.py && echo SYNTAX-OK`
Expected: `SYNTAX-OK`.

Run: `rg -n "voice-input-recording.wav" --glob '!docs/**' --glob '!venv/**' . || echo NO-FIXED-PATH`
Expected: `NO-FIXED-PATH`.

- [ ] **Step 6: Commit**

```bash
git add voice-ptt.py voice-toggle.sh
git commit -m "feat(x11): per-recording wav paths in voice-ptt and voice-toggle (#28)"
```

---

### Task 9: contrib unit — `RestartPreventExitStatus=4` (A11 half)

**Files:**
- Modify: `contrib/voice-hold.service` ([Service] section, after `RestartSec=3`)

- [ ] **Step 1: Edit the unit**

```ini
# #28: exit 4 = another voice_hold already holds the singleton flock.
# Never restart-loop on it — the other instance is the live one; stopping
# it and `systemctl --user restart voice-hold` is the recovery path.
RestartPreventExitStatus=4
```

- [ ] **Step 2: Static check**

Run: `grep -q 'RestartPreventExitStatus=4' contrib/voice-hold.service && echo UNIT-PINNED`
Expected: `UNIT-PINNED`.

- [ ] **Step 3: Commit**

```bash
git add contrib/voice-hold.service
git commit -m "feat(hold): pin RestartPreventExitStatus=4 for the lock exit code (#28)"
```

---

### Task 10: Acceptance sweep + manual verification checklist (A11/A12 + U1–U3)

**Files:** none (verification only; U items are executed by the user on the real machine)

- [ ] **Step 1: Full automated sweep**

Run:
```bash
python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold -v 2>&1 | tail -3
bash -n voice-toggle.sh && python3 -m py_compile voice-ptt.py && echo SYNTAX-OK
rg -n "voice-input-recording.wav" --glob '!docs/**' --glob '!venv/**' . || echo NO-FIXED-PATH
grep -q 'RestartPreventExitStatus=4' contrib/voice-hold.service && echo UNIT-PINNED
```
Expected: suite `OK`; `SYNTAX-OK`; `NO-FIXED-PATH`; `UNIT-PINNED`. Record the four outputs for the PR body (A11/A12 evidence).

- [ ] **Step 2: Verify acceptance-ID ↔ task bidirectional trace**

| Acceptance | Covered by |
|---|---|
| A1 | Task 1 |
| A2 (+garbage robustness) | Task 2 |
| A3 | Task 2 |
| A4 | Task 3 |
| A5 | Task 2 |
| A6 | Task 2 |
| A7 (+M1) | Task 4 |
| A8 | Task 5 |
| A9 | Task 6 |
| A10 | Task 4 |
| A11 | Tasks 8–10 |
| A12 | Tasks 7, 10 |
| U1/U2/U3 | Step 3 (user-run; mark `pending` until executed) |

- [ ] **Step 3: Manual verification checklist (user-run, mark each `pending` until done)**

- [ ] U1: with the systemd service running, run `python3 voice_hold.py` by hand → stderr shows `another instance already running (last holder pid <service pid>)`, `echo $?` is 4, and dictation (hold Right Alt) still works. → **pending**
- [ ] U2: add `RestartPreventExitStatus=4` to `~/.config/systemd/user/voice-hold.service` + `systemctl --user daemon-reload`; hold the lock with a manual instance; `systemctl --user restart voice-hold`; watch `journalctl --user -u voice-hold -f` ≥10s → exactly one refusal line, no 3s restart cycle, unit `failed` not `activating`; after stopping the manual instance, `systemctl --user restart voice-hold` recovers. → **pending**
- [ ] U3: hold Right Alt, dictate one sentence → text lands exactly once; `journalctl --user -u voice-hold -n 5` shows `recording -> ` with a random-suffixed path; that wav no longer exists after delivery. → **pending**
