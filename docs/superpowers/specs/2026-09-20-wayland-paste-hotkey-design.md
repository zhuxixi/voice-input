# #18 Spec：KDE Wayland 上屏分流 + toggle 热键（v2 · 经上屏方案 review 修订）

## 背景

OmniBook 会话是 KDE Plasma Wayland：xdotool/xsel 对原生 Wayland 窗口无效且本机未安装；pynput 全局监听不可用。#15 已装 wl-clipboard + wtype，#16 已收敛引擎入口。#18 补最后一公里：文本上屏 + 热键入口。

## 上屏方案 review 结论（2026-09-20，v1 spec 的 D2 翻转依据）

对「wtype 直打 vs wl-copy+粘贴」做了证据核查，结论：**直打法在本用户环境（CJK 听写 + fcitx5 + 智能编辑器）有三个有据可查的质量雷区，粘贴法全部免疫**：

| 雷区（直打法） | 证据 | 概率×痛感 |
|---|---|---|
| fcitx5 中文态把中英文混合文本的 ASCII 片段吞进拼音 preedit（CJK 走 Unicode keysym 直通，英文字母被 IME grab 消费） | 协议层：KWin 激活 IM 时持有 keyboard grab，虚拟键盘事件同样过 IM；fcitx5/Wayland preedit 类 bug 多发（fcitx5#953、ghostty#12679、vim#20257） | 中高 × 高（静默错字） |
| 编辑器括号/引号自动补全 doubling（`(` 变 `())`） | Monaco `editor.trigger('type')` 与物理按键同代码路径（SO#78541385）；模拟按键对应用不可区分 | 确定（VS Code 内）× 中 |
| 文本含换行 = 回车键，聊天软件提前发送 | 按键语义本身 | 低 × 高 |

粘贴法（wl-copy + 模拟粘贴键）天然免疫三者（剪贴板不经过键盘/IME 管线，原子插入）。代价两宗且均可承受：
1. **剪贴板被覆盖**——本机实测 Klipper 历史在跑（org.kde.klipper 存在），旧内容 Meta+V 可找回 → 痛感低
2. **组合键分裂**（ctrl+shift+v vs ctrl+v）——本 issue 验收目标 Konsole（默认）与 WezTerm（用户配置 215 行已显式绑 ctrl+shift+v→Clipboard）都认 ctrl+shift+v；浏览器 ctrl+shift+v=纯文本粘贴可用；ctrl+v-only 应用（VS Code/Kate/聊天）用组合键旋钮覆盖

另核查并否决两条「更优」假想路径：fcitx5 D-Bus **无 CommitString 接口**（/controller 只有 Activate/Deactivate，借道输入法上屏不存在）；「deactivate→type→restore」舞步只解 IME 雷区、不解编辑器雷区且引入状态竞态。nerd-dictation 等听写工具以打字为常态，但那是无 IME 的英文场景，结论不迁移。仓内先例：voice-ptt.py 的 X11 上屏本来就是粘贴法（xsel+ctrl+shift+v，L237-240）。

## 目标

1. voice-toggle.sh 上屏按 `XDG_SESSION_TYPE` 分流：wayland → 粘贴法（默认）/直打法（旋钮）；x11 → xdotool 现状
2. README 双语补 KDE 快捷键绑定说明 + 旋钮表 + 已知限制
3. X11 路径行为等价保留（7700K 契约）

## 非目标

- ❌ PTT 模式 Wayland 适配（pynput 整体不可用，README 注明）
- ❌ fcitx5 deactivate/restore 舞步（只解部分雷区、引入竞态；type 法用户如遇 IME 问题，README 指到 fcitx5-remote 手工方案）
- ❌ 按应用自动选组合键（kdotool 类焦点检测，过度工程）
- ❌ KDE 快捷键程序化绑定（kwriteconfig6 格式脆弱，手动说明）
- ❌ 保护文件：terms.py / archive.py / media_pause.py / voice-ptt.sh / voice-ptt.py 零改动

## 设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 新增 `paste.py`：纯标准库，纯函数构造命令序列 + 薄 CLI 执行层 | X11 契约 unit 钉死；本仓纯函数测试文化 |
| D2 | **Wayland 默认粘贴法**：`wl-copy`(stdin=text) + `wtype -M ctrl -M shift -k v -m shift -m ctrl` | review 结论：免疫 IME 吞字/编辑器 doubling/换行误发；Klipper 兜住剪贴板代价；验收目标组合键已验证 |
| D3 | `session_type`：`wayland` → wayland 法；**其余一切值 → xdotool 现状** | 历史行为优先（7700K 契约） |
| D4 | X11 argv 逐字节等于现状 `["xdotool","type","--clearmodifiers","--delay","0", text]` | 等价性可断言 |
| D5 | voice-toggle.sh 用系统 python3 调 paste.py | 纯标准库；上屏失败不耦合 venv 缺失 |
| D6 | CLI：`paste.py [--session-type T] [--method M] [--combo C] [--dry-run] [TEXT]`（TEXT 缺省读 stdin） | dry-run 集成测试零 mock + 排障利器 |
| D7 | 热键=README 手动绑定指导 | 见非目标 |
| D8 | 旋钮：`VOICE_INPUT_WAYLAND_METHOD=paste\|type`（默认 paste）、`VOICE_INPUT_PASTE_COMBO`（默认 `ctrl+shift+v`，修饰键合法集= wtype 的 shift/capslock/ctrl/logo/win/alt/altgr） | ctrl+v-only 应用与极端场景的逃生口；两旋钮均为纯函数参数，unit 可测 |

## 组件契约

```python
# paste.py（纯标准库；环境读取只发生在 main()）
Command = dict  # {"argv": list[str], "stdin": str | None}

def combo_to_wtype_args(combo: str) -> list[str]
    # "ctrl+shift+v" → ["-M","ctrl","-M","shift","-k","v","-m","shift","-m","ctrl"]
    # 非法修饰键 → ValueError（fail fast）

def paste_commands(session_type: str | None, method: str, combo: str, text: str) -> list[Command]
    # wayland + paste → [ {["wl-copy"], text}, {["wtype"]+combo_to_wtype_args(combo), None} ]
    # wayland + type  → [ {["wtype", text], None} ]
    # 其余（x11/空/未知，method/combo 无视） → [ {["xdotool","type","--clearmodifiers","--delay","0", text], None} ]

def main(argv) -> int  # 0=成功; 3=执行失败/参数非法（与 transcribe_once 语义一致）
    # 读 XDG_SESSION_TYPE / VOICE_INPUT_WAYLAND_METHOD / VOICE_INPUT_PASTE_COMBO（命令行覆盖环境）
    # 逐条执行命令序列（stdin 喂给对应命令）；任一失败 → stderr 可行动信息 + exit 3
```

voice-toggle.sh 改动仅 1 处：L41 `xdotool type ... "$TEXT"` → `printf '%s' "$TEXT" | python3 "$REPO_DIR/paste.py"`。

## Safety contract（7700K 零改动契约）

- 不设 XDG_SESSION_TYPE（或 =x11）时最终 argv 与 HEAD cdb9fbe 逐 argv 等价（A2 钉死）
- 保护文件 git diff 为空

## 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | wayland+paste 命令序列构造 | 自动化（unit） | `python3 -m unittest test_paste` | wl-copy(stdin=text) + wtype combo args，两命令顺序正确 |
| A2 | x11/空/未知 回落契约 | 自动化（unit） | 同上 | 三种输入均返回 D4 字面量 argv（method/combo 被无视） |
| A3 | combo 解析与非法拒绝 | 自动化（unit） | 同上 | "ctrl+v"→4 元素 argv；非法修饰键→ValueError |
| A4 | wayland+type 构造 | 自动化（unit） | 同上 | ["wtype", text] |
| A5 | CLI 集成：stdin + dry-run + 环境/旋钮覆盖 | 自动化（integration） | subprocess 跑 dry-run 断言 stdout | 输出命令序列与构造一致，exit 0 |
| A6 | 执行失败 exit 3 + stderr | 自动化（integration） | 受控 PATH 缺 wtype | exit 3、stderr 含缺失工具名 |
| U1 | Konsole 原生窗口 toggle 全流程 | 用户实测 | 绑 KDE 快捷键→说一句→再按→观察上屏；**fcitx5 切中文态，说含英文词的混合句**（粘贴法免疫性核心验证） | 两次文本完整上屏无缺字 |
| U2 | WezTerm 原生窗口同流程 | 用户实测 | 同 U1 换 WezTerm | 文本上屏 |
| U3 | 通知链路 | 用户实测 | U1 期间观察三态 notify-send | 正常显示 |
| U4 | 组合键旋钮（ctrl+v-only 应用） | 用户实测（可选） | VS Code 里默认流程（预期无效）→ `VOICE_INPUT_PASTE_COMBO=ctrl+v` 重试 | 旋钮生效、文本上屏 |

## 可测性拆分设计

- `combo_to_wtype_args` / `paste_commands` 纯函数（无 IO、无 environ）→ A1–A4 直接断言
- 环境读取收敛 main() → A5/A6 subprocess 集成测零 mock
- 文本经 stdin 传给 wl-copy（不经 argv）→ 无转义/长度问题，构造断言稳定
- voice-toggle.sh 不进自动化（diff 由 CR 保证只动 L41；shell 集成属 U1/U2）

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| ctrl+v-only 应用默认组合无效 | D8 组合键旋钮 + README 场景表；U4 验证 |
| 剪贴板被覆盖 | Klipper 历史找回（README 注明 Meta+V）；paste 法不触碰 primary selection |
| toggle 录音期间焦点漂移打错窗口 | issue 已接受，README 注明 |
| type 法 + fcitx5 中文态吞字 | 默认 paste 法免疫；README 给 type 法用户指 fcitx5-remote 手工方案 |
| KDE 快捷键环境缺 PATH | README 绑定命令写绝对路径；U1 即验证 |

## 用户实测时机

U1–U4 在实现合并前、本机 KDE Wayland 会话执行（非 pending 项——本机的 issue）。
