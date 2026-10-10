# Spec: `_ensure_ready` idle-death liveness check (issue #37)

Drafted: zai-coding-cn/glm-5.3 (selected) · zai-coding-cn/glm-5.3 (physical) @ 2026-10-09T22:45:00+08:00
Revised: zai-coding-cn/glm-5.3 (selected) · zai-coding-cn/glm-5.3 (physical) @ 2026-10-10T21:52:00+08:00（第 1 轮处置 3/3：R1-G1–G2 接受并改，R1-D1 接受并改）
Revised: zai-coding-cn/glm-5.3 (selected) · zai-coding-cn/glm-5.3 (physical) @ 2026-10-10T22:03:00+08:00（第 2 轮处置 1/1：R2-G1 接受并改）

- Issue: zhuxixi/voice-input#37（#25 验收发现）
- Base: main @ 01edd99
- Type: bug fix（根因已定位，见 §1）
- 上游设计: docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md §4.3 / D5 / D7 / D15

## 1. 根因（已证，不重复调查）

`worker_supervisor._ensure_ready` 快速路径只看内存标志：

```python
if self._ready and self._startup is not None:
    return True, None
```

worker 空闲期死亡（`kill -9` / 崩溃）后 `_ready` 仍为 True；下一次 `request()` 发请求
→ readline EOF → `_fail("worker-exit")` → **该句听写丢弃**，重建留给再下一次（真机
journal 证据见 issue 正文）。主进程存活、热键不失效，问题仅牺牲一句。

## 2. 修法设计

**语义仍是 D5「惰性检测」**：不引入监控线程/定时器；唯一变化是死亡发现时机从
「请求中途 EOF」提前到「请求开头一次 `poll()`（waitpid WNOHANG，µs 级）」——
发现 + 重建 + 等 ready 在**同一次请求**预算内完成（`VOICE_INPUT_TRANSCRIBE_TIMEOUT`，
voice_hold 默认 120s），那句听写被救回。

### 2.1 改动点（单文件：`worker_supervisor.py`）

新增模块级谓词（副作用仅一次 WNOHANG poll，含回收僵尸）：

```python
def _proc_alive(proc) -> bool:
    """True iff the child has not exited yet (one WNOHANG poll; reaps a zombie)."""
    return proc.poll() is None
```

`_ensure_ready` 快速路径改为：

```python
if self._ready and self._startup is not None:
    if _proc_alive(self._startup.proc):
        return True, None
    pid = self._startup.pid or self._startup.proc.pid
    self._log(f"[voice-hold] worker died while idle (pid {pid}) — "
              "rebuilding it for this dictation")
    self._kill_locked()
    # fall through：同一循环迭代重读 self._startup（已 None）→ 既有 spawn 路径
```

日志行含 pid 与「died while idle → rebuilding」语义，与 #25 的日志钉法一致。

### 2.2 为什么 `_kill_locked` 不用改（已实测）

Python 3.14（本机 3.14.7 验证）：`Popen.kill()` 对已退出/已回收子进程是**静默 no-op**
（`send_signal` 先 poll returncode，bpo-37424；ProcessLookupError 已抑制，bpo-40550），
`wait()` 立即返回。死亡分支下 `_kill_locked` 干净收敛：kill no-op、wait 即回、conn/sock
正常关闭，无虚假错误日志、无 fd 泄漏。

### 2.3 约束遵守

| 约束 | 遵守方式 |
|------|----------|
| D5 惰性检测 + 节流 | 无新线程/定时器；死亡后若距上次 spawn < `WORKER_RESTART_MIN_INTERVAL`(1.0s)，`_spawn_locked` 照旧拒绝 → 请求以 `"throttled"` 明示失败（防雪崩语义原样，不加豁免） |
| D7 挂死处理 | `poll()` 只管已退出进程；活而不响应仍由既有请求超时杀（不动） |
| D15 锁纪律 | 未新增锁内阻塞读；`poll()` µs 级；`_kill_locked` 在死亡进程上立即返回（既有调用方本就在锁内调它） |
| 每次 request 至多一次 spawn | 落入既有 `_spawn_locked` 路径，天然继承 |

### 2.4 并发与竞态说明

- 两个线程同时撞上死 worker：均可能打出一行死亡日志（真实事件，可接受）；`_kill_locked`
  幂等（后到者拿到 `_startup=None` 直接返回）；随后 owner/joiner 逻辑保证单飞 spawn。
- `shutdown()` 竞态：`_shutdown_done` 检查在快速路径之前（既有顺序），关停后不 respawn。
- 健康路径：`poll()` 对活进程返回 None，行为与现状逐字节一致。

### 2.5 备选方案（已否决）

- **维持现状（EOF 发现）**：每赔一句听写——本 issue 的存在理由。
- **后台监控线程/心跳**：D5 明确否决（多一套生命周期，防雪崩更难）。

## 3. 非目标

- 监控线程 / 周期性健康检查 / 心跳协议。
- 挂死检测（D7 超时已覆盖）。
- `_deliver` 的 0.3s settle 优化（#25 决策点④，另立 issue）。
- `voice_hold.py` 的 `worker unavailable` 降级文案（中途真死亡仍需要它，保留）。

## 4. 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | 谓词 `_proc_alive` | 自动化验证（unit） | `python3 -m unittest test_worker_supervisor -v` 新增 unit 测试：真实 Popen 活→True；kill+wait 后→False | 断言通过 |
| A2 | 空闲死亡同请求重建（核心修复） | 自动化验证（integration，真实 stub 子进程） | 同命令新增 integration 测试：`die-after-ready` stub（首实例发完 ready 即退出，marker 文件区分代次）→ `prewarm()` 成功 → 等首实例退出 → FakeClock 推进过节流间隔 → `request()` | 返回 `("stub-text", None)` 而非 `(None,"worker-exit")`；日志含 `died while idle (pid N)` 行；新 worker pid ≠ 旧 pid |
| A3 | 健康快速路径无回归 | 自动化验证（integration） | README.md:386 钉的 9 模块全量命令：`python3 -m unittest test_terms test_archive test_media_pause test_bench test_paste test_hold test_worker_protocol test_worker_supervisor test_transcribe_worker -v` | 全绿；`test_second_request_reuses_the_same_worker` 仍 pin 同一 pid（poll 不误杀活 worker）；`test_request_after_shutdown_degrades_without_respawn` 仍 pin 关停不 respawn |
| A4 | 空闲死亡后 <1s 的重建被节流拒绝（D5 防雪崩，不加豁免） | 自动化验证（integration） | 同 A2 命令新增测试：die-after-ready stub → `prewarm()` 成功 → 等首实例退出 → FakeClock 仅推进 0.5s（< `WORKER_RESTART_MIN_INTERVAL`）→ `request()`（`_spawn_fn` wraps spy） | 返回 `(None, "throttled")`；spawn spy 计数不增加（本请求未发起第二次 spawn）；日志仍含 `died while idle` 行 |
| U1 | 真机空闲死亡恢复 | 用户实测 | **合并前主路径**：① `systemctl --user stop voice-hold`（释放 #28 单实例 flock，voice_hold.py:146 否则手动实例拿不到锁）；② worktree 根 `ln -s <主 checkout 绝对路径>/venv venv`（预检要 `REPO_DIR/venv/bin/python3`，voice_hold.py:32/505-511，worktree 无 venv）；③ worktree 内前台直跑并注入生产 env（engine.py:73/52 默认 cuda+large-v3，本机无 CUDA，缺 env 必失败；env 经 `engine.child_env` 直通 worker）：`VOICE_INPUT_ENGINE=npu VOICE_INPUT_MODEL=small-int8-ov VOICE_INPUT_TRANSCRIBE_TIMEOUT=240 python3 voice_hold.py`（此三值为 2026-10-10 unit 快照，以 `systemctl --user show voice-hold -p Environment` 实际输出为准；日志=stderr）；④ 从 stderr `worker ready … (pid N)` 取 pid，`kill -9 <pid>`；⑤ **下一次**听写按住说话松手；⑥ 验毕 `systemctl --user start voice-hold` 恢复。**合并后补验路径**：主 checkout `git pull`（systemd `ExecStart` 指向主 checkout，不拉取测的是旧代码）→ `systemctl --user restart voice-hold`（日志=journal）→ 复核同组断言 | 该句正常上屏，全程 ≤ ~2.5s（含重建热加载 ~0.6s）；日志（主路径=stderr / 补验=journal）无 `worker unavailable` 丢句行，出现 `died while idle … rebuilding` 行；再下一句稳态 `transcribed in X.XXs (worker)` |
| U2 | 热路径无可感知延迟 | 用户实测 | 日常听写若干句，查 journal `transcribed in X.XXs (worker)` 行 | 与 #25 验收基线一致（~0.7–0.8s 量级），无可感知卡顿 |

U1 合并前主路径（停服务 + venv symlink + worktree 前台直跑）保证「PR 合并前 U1 必须通过」可达；
合并后补验路径复核 systemd 部署形态。U2 合并后 48h 内日常观察补验。

## 5. 可测性拆分设计（自动化项的实现约束）

- **A1 边界（unit）**：`_proc_alive(proc)` 为模块级独立谓词——输入仅 Popen，输出仅 bool，
  副作用孤立为一次 WNOHANG poll。测试用真实 `subprocess.Popen`（sleep 进程）双态断言，
  不碰协议/socket，毫秒级完成。
- **A2 边界（integration）**：行为边界 = supervisor 公共契约（`prewarm()`/`request()`）
  × 真实 stub 子进程（真 socketpair、真协议），与 #25 的 A3–A6 同型——不打假 socket。
  stub 新增 `die-after-ready` 模式：以 marker 文件区分代次（首实例发完 ready 行即
  `sys.exit(0)`；重建实例走正常应答循环），使「空闲死亡→同请求重建→拿到文本」全链路
  确定性复现，无 sleep-race。
- **A4 边界（integration）**：与 A2 同一 die-after-ready stub 基建，只差 FakeClock 推进量
  （0.5s < 1.0s 节流间隔）——确定性命中 `_spawn_locked` 的节流分支，无 sleep-race；断言
  口径是「失败原因明示为 `throttled` 且无第二次 spawn」，钉死 D5 不豁免。
- **A3 边界（回归）**：不加新测试，靠既有套件钉住（pid 复用、shutdown 不 respawn、
  节流语义均已有 pin）；命令用 README.md:386 的 9 模块全量原样。
- 实现不得把谓词内联回 `_ensure_ready`（会丢 unit 边界），也不得让谓词承担日志/杀进程
  职责（副作用必须隔离在分支内）。
