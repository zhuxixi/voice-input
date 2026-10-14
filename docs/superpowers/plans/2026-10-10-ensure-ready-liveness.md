# #37 `_ensure_ready` Idle-Death Liveness Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 空闲期死亡的常驻 worker 不再赔一句听写——`_ensure_ready` 快速路径加 `poll()` 存活检查，同一次请求内完成发现+重建+等就绪。

**Architecture:** 单文件修复（`worker_supervisor.py`）：新增模块级谓词 `_proc_alive(proc)`，快速路径死亡分支打日志（含 pid）→ `_kill_locked` → 落入既有 spawn 路径。测试侧：stub worker 新增 `die-after-ready` 模式（marker 文件区分代次），新增 3 个测试（A1 unit / A2 integration / A4 节流），A3 靠既有 9 模块全量回归。

**Tech Stack:** Python 3.14（stdlib-only：socket/subprocess/threading/unittest，无第三方依赖）

**Spec:** `docs/superpowers/specs/2026-10-10-ensure-ready-liveness-design.md`（v3，交叉复核收敛版）

## Global Constraints

- `worker_supervisor.py` / `worker_protocol.py` 顶层**纯标准库**（voice_hold 的 purity 契约，`TestModulePurity` 钉住）——不新增任何 import。
- `_kill_locked` **不改**（Python 3.14 实测 `Popen.kill()` 对已回收子进程静默 no-op）。
- 节流不加豁免：死亡重建照样过 `WORKER_RESTART_MIN_INTERVAL`(1.0s)（spec §2.3，A4 钉死）。
- 日志格式沿用 `[voice-hold] ...` 前缀；新日志行必须含 `died while idle` 与 pid。
- 工作目录：所有路径相对于 worktree 根 `/home/elling/work/git-repo/voice-input/.pi/worktrees/issue-37-ensure-ready-liveness`；git 操作用 `git -C <worktree>`；**禁碰主 checkout**。
- commit message 用英文 conventional commits；`git add` 按文件，**不用 `git add -A`**。

## Review Focus

1. **死亡竞态窗口（已知残留）**：`_ensure_ready` 返回 True 到 `request()` 真正 write 之间 worker 刚好死亡 → 走既有中途 EOF → `(None,"worker-exit")`，本次失败、下次重建。修法只能收窄不能消除该窗口（spec §2 明确）；由 Task 3 的 A2 测试注释说明，不加新测试。
2. **并发撞死 worker**：两线程同时检测到死亡 → 可能双日志；`_kill_locked` 幂等 + owner/joiner 单飞 spawn（spec §2.4）。生产形态单请求（#25 NG7），不加测试。
3. **僵尸回收**：`_proc_alive` 的 `poll()` 顺带 reap 僵尸——A1 的 kill+wait→False 断言已覆盖。
4. **stub marker 文件泄漏**：每测试独立 tmpdir（setUp mkdtemp + addCleanup rmtree），代次标记互不污染。
5. **谓词误杀活 worker**：`poll()` 对活进程返回 None——A3 回归的 `test_second_request_reuses_the_same_worker`（pid 不变）钉住。

---

### Task 1: `_proc_alive` 谓词（spec A1）

**Files:**
- Modify: `worker_supervisor.py`（在 `resident_enabled` 后加模块级函数）
- Test: `test_worker_supervisor.py`（新增 `TestProcAlive` 类）

**Interfaces:**
- Produces: `worker_supervisor._proc_alive(proc: subprocess.Popen) -> bool` — 子进程未退出返回 True；副作用仅一次 WNOHANG poll（顺带 reap 僵尸）。Task 3 依赖此签名。

- [ ] **Step 1: 写失败测试**

在 `test_worker_supervisor.py` 的 `TestResidentEnabled` 类后新增：

```python
class TestProcAlive(unittest.TestCase):
    """A1 (#37): liveness predicate — alive True, exited False (reaps zombie)."""

    def test_alive_process_returns_true(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        self.assertTrue(ws._proc_alive(proc))

    def test_exited_process_returns_false(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(ws._proc_alive(proc))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_worker_supervisor.TestProcAlive -v`
Expected: FAIL with `AttributeError: module 'worker_supervisor' has no attribute '_proc_alive'`

- [ ] **Step 3: 最小实现**

在 `worker_supervisor.py` 的 `resident_enabled` 函数后加：

```python
def _proc_alive(proc) -> bool:
    """True iff the child has not exited yet (one WNOHANG poll; reaps a zombie)."""
    return proc.poll() is None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_worker_supervisor.TestProcAlive -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git -C $WT add worker_supervisor.py test_worker_supervisor.py
git -C $WT commit -m "feat(supervisor): _proc_alive liveness predicate (#37, A1)"
```

### Task 2: `die-after-ready` stub 模式 + A2/A4 失败测试（spec A2/A4 的 test 侧）

**Files:**
- Modify: `test_worker_supervisor.py`（STUB_WORKER 加模式；新增 `TestIdleDeath` 类）

**Interfaces:**
- Consumes: `ws._proc_alive`（Task 1）；`SupervisorTestCase` fixture；`live_worker(sup)` 助手（:175-177）
- Produces: stub 模式 `die-after-ready` + env `STUB_MARKER`（marker 存在=重建代，正常应答；不存在=首代，发完 ready 即退出）

- [ ] **Step 1: stub 加 `die-after-ready` 模式**

改 `STUB_WORKER` 模板，两处插入（均相对既有行定位）：

① 在既有 `bad-line` 分支之后、既有 ready 行（`f.write((json.dumps({"event": "ready", ...` 那一行）**之前**插入：

```python
    die_after = (mode == "die-after-ready"
                 and not os.path.exists(os.environ["STUB_MARKER"]))
    if die_after:
        open(os.environ["STUB_MARKER"], "w").close()
```

② 在 ready 行及其 `f.flush()` **之后**、既有 `while True:` 服务循环**之前**插入：

```python
    if die_after:
        sys.exit(0)                      # 首代：发完 ready 即退出（模拟空闲死亡）
```

同时更新 STUB_WORKER 上方注释的模式清单（加 `die-after-ready`）。

- [ ] **Step 2: 写 A2/A4 失败测试**

文件末尾新增：

```python
class TestIdleDeath(SupervisorTestCase):
    """A2/A4 (#37): worker died while idle — same-request rebuild, no exemption
    from the restart throttle. Known residual: a death landing between the
    fast-path poll() and the request write still fails that dictation via the
    existing mid-request EOF path (spec §2, narrowed but not eliminable)."""

    mode = "die-after-ready"

    def setUp(self):
        super().setUp()
        self.env["STUB_MARKER"] = os.path.join(self._tmp, "stub-gen-marker")

    def test_idle_death_rebuilds_within_the_same_request(self):
        """A2: next request after idle death returns text, not worker-exit."""
        sup = self.make_supervisor()
        self.assertTrue(sup.prewarm())
        old_pid = live_worker(sup).pid
        live_worker(sup).wait(timeout=5)          # first instance exits on its own
        self.clock.advance(ws.WORKER_RESTART_MIN_INTERVAL + 0.01)
        text, reason = sup.request("/tmp/x.wav", timeout=10.0)
        self.assertEqual((text, reason), ("stub-text", None))
        self.assertNotEqual(live_worker(sup).pid, old_pid)
        self.assertTrue(any("died while idle" in m for m in self.logs), self.logs)

    def test_idle_death_within_throttle_interval_fails_throttled(self):
        """A4: no throttle exemption — death <1s after spawn => throttled."""
        sup = self.make_supervisor()
        with mock.patch.object(sup, "_spawn_fn", wraps=sup._spawn_fn) as spy:
            self.assertTrue(sup.prewarm())
            live_worker(sup).wait(timeout=5)
            self.clock.advance(0.5)               # < WORKER_RESTART_MIN_INTERVAL
            text, reason = sup.request("/tmp/x.wav", timeout=10.0)
            self.assertEqual((text, reason), (None, "throttled"))
            self.assertEqual(spy.call_count, 1)   # only prewarm's spawn
            self.assertTrue(any("died while idle" in m for m in self.logs), self.logs)
```

- [ ] **Step 3: 跑测试确认两个都失败**

Run: `python3 -m unittest test_worker_supervisor.TestIdleDeath -v`
Expected: 2 FAIL — A2 拿到 `(None, "worker-exit")` 而非 `("stub-text", None)`；A4 的 reason 是 `"worker-exit"` 而非 `"throttled"`（修复前快速路径不查 poll，请求打上死 socket EOF）

- [ ] **Step 4: Commit（测试先行）**

```bash
git -C $WT add test_worker_supervisor.py
git -C $WT commit -m "test(supervisor): die-after-ready stub + idle-death red tests (#37, A2/A4)"
```

### Task 3: `_ensure_ready` 快速路径存活检查（spec A2/A4 的实现侧）

**Files:**
- Modify: `worker_supervisor.py`（`_ensure_ready` 快速路径，约 :140-143）

**Interfaces:**
- Consumes: `ws._proc_alive`（Task 1）
- Produces: 行为契约——空闲死亡 → 日志 `worker died while idle (pid N) — rebuilding it for this dictation` + 同请求重建；<1s 内死亡 → `(None, "throttled")`

- [ ] **Step 1: 改快速路径**

`_ensure_ready` 的 `while True` 循环内，把：

```python
                if self._ready and self._startup is not None:
                    return True, None
```

改为：

```python
                if self._ready and self._startup is not None:
                    if _proc_alive(self._startup.proc):
                        return True, None
                    pid = self._startup.pid or self._startup.proc.pid
                    self._log(f"[voice-hold] worker died while idle (pid {pid}) — "
                              "rebuilding it for this dictation")
                    self._kill_locked()
                    # fall through: same iteration re-reads self._startup (now
                    # None) and takes the existing spawn path (D5 lazy detect,
                    # discovered at request start instead of mid-request EOF)
```

（`_kill_locked` 不改——`Popen.kill()` 对已回收子进程静默 no-op，Python 3.14 实测。）

- [ ] **Step 2: 跑 A2/A4 确认转绿**

Run: `python3 -m unittest test_worker_supervisor.TestIdleDeath -v`
Expected: PASS (2 tests)

- [ ] **Step 3: 跑 supervisor 全文件确认无回归**

Run: `python3 -m unittest test_worker_supervisor -v`
Expected: 全绿（含 pid 复用、shutdown 不 respawn、节流既有 pin）

- [ ] **Step 4: Commit**

```bash
git -C $WT add worker_supervisor.py
git -C $WT commit -m "fix(supervisor): liveness check on _ensure_ready fast path (#37)"
```

### Task 4: 全量回归（spec A3）

**Files:** 无改动（纯验证 + 收尾 commit 视情况）

- [ ] **Step 1: 跑 README 钉的 9 模块全量**

Run: `python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v`
Expected: 全绿

- [ ] **Step 2: 模块纯净性抽查（global constraint）**

Run: `python3 -m unittest test_worker_supervisor.TestModulePurity -v`
Expected: PASS（新谓词未引入新 import）

---

## 验收追溯

| spec ID | 归属 task |
|---|---|
| A1 | Task 1 |
| A2 | Task 2（红）→ Task 3（绿） |
| A3 | Task 4（另 Task 3 Step 3 预跑单文件） |
| A4 | Task 2（红）→ Task 3（绿） |
| U1/U2 | 用户实测，合并前 U1 走 spec §4 主路径（停服务+venv symlink+带 env 前台直跑），U2 合并后 48h |
