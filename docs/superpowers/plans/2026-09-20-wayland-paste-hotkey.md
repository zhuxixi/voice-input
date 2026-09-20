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

### 本地 CR 修正轮(pi workflow code-review,26 agents,2026-09-20)

10 条(8 CONFIRMED / 2 PLAUSIBLE):修 7、README 注记 1(并入 7)、延后 2(#19 记账)。

| # | 发现 | 处置 |
|---|------|------|
| 1+4 | voice-toggle.sh:43 吞 paste.py 退出码,失败仍弹成功通知;exit 3 契约在生产调用点是死代码 | ✅ mktemp 捕 stderr + if 检查退出码:失败 → notify-send 5s 错误通知(含 stderr),成功才弹结果 |
| 5 | 裸 python3 依赖 PATH(NixOS/精简 session 下 127 静默失败) | ✅ 改 `$VENV/bin/python3`(脚本上方预检已保证 venv 存在,与 SITE_PACKAGES 同一不变量) |
| 7 | _Parser 把 usage error 改 exit 3,偏离仓约定(2=usage/3=runtime) | ✅ 撤 _Parser 用 stock argparse;新增测试钉 exit 2 |
| 9 | method 集合三处手工编码 | ✅ dispatch table `_WAYLAND_DISPATCH` 单源 + VALID_METHODS 镜像测试 |
| 10 | CLI-over-env 解析三份逐字复制 | ✅ `_resolve()` helper 收敛 |
| 2 | XWayland 焦点窗口可能收不到 wtype 键(compositor 依赖) | 📝 README 双语限制清单注记(PLAUSIBLE,目标环境 KWin 原生窗口不构成回归) |
| 3 | type 法无 --clearmodifiers 等价(用户按住修饰键时字母变快捷键) | 📝 README 双语注记(opt-in 路径,wtype 无机制可清他人修饰键) |
| 6 | voice-ptt.py 保留第二份上屏实现(保护文件非目标) | 📝 延后:toggle/PTT 上屏统一记到 #19 债务清单 |
| 8 | 第三份 purity-check + CLI harness 副本(test_engine/test_bench 同款) | 📝 延后:测试 helper 抽取记到 #19 债务清单 |

修正后:106 测试绿(新增 2);保护文件 diff=0;shell 失败分支冒烟通过。

### v3 方向调整(用户拍板 A+,2026-09-20 上午)

实测发现:KWin 未实现 zwp_virtual_keyboard_v1(上游 wishlist bug 502882),wtype
在 KDE Wayland 原理性不可用;用户同时明确要 7700K 的按住说话手感。spec 升 v3:
wtype 全退场,全链路走内核层(evdev 监听 + wl-copy + ydotool 注入)。

- paste.py:`combo_to_wtype_args` → `combo_to_ydotool_args`(组合键名→evdev keycode
  映射表,linux/input-event-codes.h 稳定 ABI);wayland+paste = wl-copy + ydotool key
  序列;wayland+type = ydotool type;x11 契约不动;缺失工具提示同步
- voice_hold.py(新):evdev 监听 KEY_RIGHTALT(按下录音/松开转写上屏/autorepeat 忽略),
  状态机 transition + 设备谓词 is_keyboard_device(排除 ydotool 虚拟设备)+ pick_device
  (VOICE_INPUT_DEVICE 覆盖)均为纯函数;GTK 浮层 TOPLEVEL/keep_above,gi 缺失静默降级;
  media_pause 复用;转写走 transcribe_once.py(venv python)
- test_hold.py(新):A7-A9(状态机矩阵/设备谓词/pick_device 假注入/纯净 import)
- contrib/voice-hold.service(新):systemd user 单元,Environment 钉 VOICE_INPUT_ENGINE/
  MODEL(OmniBook 默认 cpu+small)
- README 双语 Wayland 章节整体重写为 A+ 流程;配置表旋钮改指 systemctl --user edit;
  文件表补 voice_hold.py/contrib;全量测试命令补 test_hold
- ydotoold 安装实况:Arch 包带 user 服务(ydotool.service),socket 默认路径与客户端
  不一致,已写 drop-in(-p /run/user/1000/.ydotool_socket);input 组权限待重登录生效

测试:118 项全绿(104 + test_hold 14)。真执行链路(ydotool 注入 + evdev 监听)属 U 域,
重登录后 U1-U4 实测。
