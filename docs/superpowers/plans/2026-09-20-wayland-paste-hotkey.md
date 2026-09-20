# Plan: #18 KDE Wayland 上屏分流 + toggle 热键

Spec: `docs/superpowers/specs/2026-09-20-wayland-paste-hotkey-design.md`（v2）
Worktree: `.pi/worktrees/issue-18-wayland-paste-hotkey`（所有路径相对 worktree 根）

## Task 1: `paste.py` 纯函数层（A1–A4）

- 新建 `paste.py`（纯标准库，模块顶层零非标准导入）
  - `Command = dict`（`{"argv": list[str], "stdin": str | None}`）
  - `combo_to_wtype_args(combo: str) -> list[str]`：`"ctrl+shift+v"` → `["-M","ctrl","-M","shift","-k","v","-m","shift","-m","ctrl"]`；按 `+` 切分，末位=key、其余=修饰键；非法修饰键（∉ {shift, capslock, ctrl, logo, win, alt, altgr}，wtype man 实测集）→ `ValueError`
  - `paste_commands(session_type, method, combo, text) -> list[Command]`：
    - `wayland` + `paste` → `[{"argv":["wl-copy"],"stdin":text}, {"argv":["wtype"]+combo_to_wtype_args(combo),"stdin":None}]`
    - `wayland` + `type` → `[{"argv":["wtype",text],"stdin":None}]`
    - 其他（`x11`/空/`None`/未知，无视 method/combo）→ `[{"argv":["xdotool","type","--clearmodifiers","--delay","0",text],"stdin":None}]`
- 验证：`python3 -c "import paste"` 零副作用（纯度）
- 验收归属：A1/A2/A3/A4

## Task 2: `paste.py` CLI 执行层（A5/A6）

- `main(argv) -> int`：argparse `--session-type/--method/--combo/--dry-run` + 位置 TEXT（缺省读 stdin）
  - 环境默认值：`XDG_SESSION_TYPE` / `VOICE_INPUT_WAYLAND_METHOD`（缺省 `paste`）/ `VOICE_INPUT_PASTE_COMBO`（缺省 `ctrl+shift+v`）；命令行参数覆盖环境变量
  - `--dry-run`：逐行打印将执行的命令（`argv` + stdin 标记）到 stdout，不执行，exit 0
  - 执行：`subprocess.run(cmd["argv"], input=cmd["stdin"])`，任一 returncode≠0 → stderr 打印失败命令与可行动提示 → exit 3
- 验收归属：A5/A6

## Task 3: `test_paste.py` 单测（A1–A4）

- 新建 `test_paste.py`（标准库 unittest，与 test_engine/test_bench 同风格）
- 覆盖：
  - A1：`paste_commands("wayland","paste","ctrl+shift+v",t)` 返回两命令序列（wl-copy stdin=t；wtype combo argv）
  - A2：`"x11"`/`None`/`""`/`"weird"` 四种输入 → xdotool D4 字面量 argv，method/combo 传极端值也不影响
  - A3：`combo_to_wtype_args("ctrl+v")` → 4 元素；`"ctrl+bogus"` → ValueError
  - A4：`paste_commands("wayland","type",...,t)` → `[{"argv":["wtype",t],...}]`
- 验证：`python3 -m unittest test_paste -v` 全绿
- 验收归属：A1–A4

## Task 4: CLI 集成测试（A5/A6，进 `test_paste.py`）

- subprocess 跑真 CLI：
  - A5：`echo -n hi | python3 paste.py --session-type wayland --dry-run` → stdout 含 `wl-copy` 与 `wtype` 命令行、exit 0；`--method type` 变体断言 `wtype hi`；`--session-type x11` 断言 xdotool；`XDG_SESSION_TYPE` 环境变量路径（不用参数）同样断言
  - A6：受控 `PATH`（空目录）跑 `--session-type wayland`（非 dry-run）→ exit 3、stderr 含缺失工具名
- 验证：全量 `python3 -m unittest test_paste test_engine test_bench test_terms test_archive test_media_pause` 全绿
- 验收归属：A5/A6

## Task 5: `voice-toggle.sh` 一行替换（U1/U2 前置）

- L41 `xdotool type --clearmodifiers --delay 0 "$TEXT"` → `printf '%s' "$TEXT" | python3 "$REPO_DIR/paste.py"`
- 验证：`bash -n voice-toggle.sh` 语法过；diff 仅此一行
- 验收归属：U1/U2 的通路（shell 层不进自动化，spec 可测性拆分约定）

## Task 6: README 双语（U1/U2/U4 支撑 + 热键 D7）

- `README.md` + `README.zh-CN.md`：
  1. 新增「Wayland（KDE）」小节：KDE 快捷键绑定步骤（System Settings → Keyboard → Shortcuts → Add New → Command → `bash /绝对路径/voice-toggle.sh` → 绑键）
  2. 旋钮表：`VOICE_INPUT_WAYLAND_METHOD`（paste/type，默认 paste）、`VOICE_INPUT_PASTE_COMBO`（默认 ctrl+shift+v；ctrl+v-only 应用如 VS Code 的场景说明）
  3. 已知限制：录音期间焦点漂移会打向新焦点窗口；PTT 模式（pynput）Wayland 下不可用；剪贴板被覆盖可用 Klipper 历史（Meta+V）找回；type 法遇 fcitx5 吞字的手工缓解（fcitx5-remote）
  4. 文件表补 `paste.py` / `test_paste.py` 行；全量测试命令补 `test_paste`
- 验证：双语四处一致（对照检查）
- 验收归属：D7/D8 文档面

## Task 7: 全量回归 + U 实测清单准备（U1–U4）

- 全量测试套件跑一遍（85+ 新增，全绿）
- `git diff main --stat` 核对改动面：仅 `paste.py` / `test_paste.py` / `voice-toggle.sh`(1 行) / README×2；保护文件 diff 为空
- 在 issue #18 贴 U1–U4 实测步骤清单（Konsole fcitx5 混合句 / WezTerm / 通知三态 / 可选 VS Code 旋钮）
- 验收归属：U1–U4 准备

## Task 8: 本地 CR（步 8）

- pi workflow `code-review` 模式跑分支 diff，处置发现（延后项记录 #19/#20）
- 验收归属：全矩阵对账
