# #20 spec：README 双语同步多引擎与 Wayland 安装说明

Drafted: kimi-coding/k3.1-pro (selected) · kimi-coding/k3.1-pro (physical) @ 2026-11-03T23:20:00+08:00
Revised: kimi-coding/k3.1-pro (selected) · kimi-coding/k3.1-pro (physical) @ 2026-11-03T23:30:00+08:00 — 第 1 轮复核修订：接受 R1-G1/D1/D2/D3/D4 全部五条
Revised: kimi-coding/k3.1-pro (selected) · kimi-coding/k3.1-pro (physical) @ 2026-11-03T23:42:00+08:00 — 第 2 轮复核修订：接受 R2-G1/D1/D2/D3/D4/D5 全部六条
Revised: kimi-coding/k3.1-pro (selected) · kimi-coding/k3.1-pro (physical) @ 2026-11-03T23:52:00+08:00 — 第 3 轮复核修订：接受 R3-D1/D2/D3 三条细化（该轮真缺口为 0，收敛）

Umbrella: #14（本 spec 收敛后 #14 四项验收全部闭环，可随本 issue 一并关闭）

## 目标与非目标

**目标**：README.md 与 README.zh-CN.md 同步呈现「三引擎（cpu/cuda/npu）+ 双平台（X11/KDE Wayland）」现状，新用户按文档可完成任一引擎的安装；中英结构一致、无单语滞后。

**非目标**：
- GNOME Wayland 适配文档（#36 缺口 C，届时单独补）
- 新增功能、代码改动、重构文档结构
- 重写已有准确章节（VAD/归档/热词/NPU 专节/Wayland 已知限制保持原样；**唯一例外：D3 指定的 NPU 前置段改动——EN 插入 render 权限项并改计数「all three→all four」/ ZH 前置段 bullet 化**，见 D3）

## 现状与改动清单（调研依据：research/readme-audit.md）

基线：main @ 70552a5。以下 6 项改动全部**中英同步、同位置同结构**。

### D1 徽章与引擎定位（EN L3-6 / ZH 对应行）
- Platform 徽章：`platform-Linux (X11)` → `platform-Linux (X11 · KDE Wayland)`，锚点双份各自保持指向本文件对应标题（EN `#requirements`；ZH `#依赖`，不改断）
- 引擎徽章：`engine-CUDA + faster-whisper` → `engine-CUDA / CPU / NPU`（徽章文本中 `/` 用 %2F 转义或改用 `-` 分隔，以 shields.io 实际渲染为准，取 `-CUDA·CPU·NPU-` 风格）
- 简介段补一句：支持多引擎可选（CUDA 默认，CPU 兜底，Intel NPU 经 openvino-genai）

### D2 Requirements 按引擎重写（EN L53-64 / ZH L42-52）
- 首行：`Linux with X11 (tested on Ubuntu 24.04) or KDE Wayland (tested on Plasma 6.7.5 / Arch)`；GNOME Wayland 注明未测（见 #36）
- 引擎依赖拆三行：CUDA 路线（NVIDIA GPU + CUDA）；CPU 路线（无 GPU 要求）；NPU 路线（Intel NPU + 驱动 ≥1.38.0，详见 NPU 引擎节）
- 公共依赖（Python 3.12 / ALSA / PipeWire / GTK3）保持不动；`xdotool`/`xsel` 标注 X11-only；Wayland 侧依赖已在 Wayland 节，不重复

### D3 NPU 前置补 render 组权限（NPU 引擎节 Prerequisites，EN ~L260 / ZH ~L220）
EN 侧在现有三件套 bullet 中插入权限项（「all three」改「all four」）；**ZH 侧前置段当前是散文（「三个前置（缺一不可）：…」分号并列），本 spec 明确要求把 ZH 前置段重构为与 EN 对应的 4 条 bullet 列表**（语义不变，仅结构对齐；这是非目标「NPU 专节保持原样」的唯一例外）：
- `render` 组成员（访问 `/dev/accel/accel0`）：`sudo usermod -aG render $USER`，注销重登生效
- ZH NPU 节 Notes 段保持散文，**不重写**（既有结构差异，A1 第②层按豁免处理，见验收矩阵）
- 修正 README 中 corrections 示例的说明错位（**EN/ZH 同位置同改**：EN 热词节 corrections bullet「skill→sql」与 **ZH README.zh-CN.md L254**（散文 corrections bullet「稳定错字（`skill`→`sql`、`pjob`→`批诏`）」）均与 JSON 示例方向相反；**注意 ZH L248 是方向正确的 JSON 示例行，不可动**）。代码事实已核对：`terms.py` `apply_corrections_counted`（L46-81）是 错→对 映射，示例 JSON `"sql": "skill"` 即把误识别的 sql 纠正为 skill；两份 README 的说明文字统一按此方向改写

### D4 Features 引擎条目（EN L38-39 / ZH L35）
- 「GPU accelerated」条目改为「多引擎」条目：CUDA large-v3 常驻显存（默认）；CPU 兜底；NPU 经 openvino-genai stateful pipeline（#27），热词全引擎可用

### D5 Project Layout 文件表补全（EN L355-372 / ZH L275-292）
按仓库实际文件补齐（main @ 70552a5 实列）：
- 引擎与转写：`engine.py`（引擎分发单一事实源）、`transcribe_once.py`（CLI 转写入口）、`vad_trim.py`（Silero VAD 静音裁剪，#32）、`transcribe_guard.py`（词表回声熔断，#43）
- 常驻 worker：`transcribe_worker.py` / `worker_supervisor.py` / `worker_protocol.py`（#25 三件套，各一行）
- 测试：**全列 12 个 `test_*.py`**（main @ 70552a5 实列：test_archive / test_bench / test_engine / test_hold / test_media_pause / test_paste / test_terms / test_transcribe_guard / test_transcribe_worker / test_vad_trim / test_worker_protocol / test_worker_supervisor），EN/ZH 同表同行，不做分组省略
- bench/ 行补 `hold-latency.py`（hold 延迟基准，#25）

### D6 Roadmap 勾选已完成项（EN L427+ / ZH L341+）
新增两条 `[x]`（带 issue 号，风格同现有自定义词汇条目）：
- `[x]` 多引擎可选（cpu/cuda/npu，#16/#19/#27）
- `[x]` Wayland 支持（KDE hold-to-talk + ydotool 上屏 + layer-shell 浮层，#18/#24）
GNOME Wayland 不入 Roadmap 勾选（未完成，#36 跟踪）。

## 契约与边界

- **只动上述 6 处**，其余段落逐字保留；中英同步 = 同一 PR 内两份文件同位置修改，语义等价（不要求逐字对译，沿用现有行文风格）
- **事实来源**：引擎行为以 `engine.py` / `transcribe_once.py` 代码为准；NPU 数据以 #17/#27 实测为准；平台矩阵以 #18/#24 spec 为准。文档不得写入代码中不存在的行为
- **corrections 方向修正**（D3 附带）先读 `terms.py` 确认映射方向再落笔，属文档 bug 修正，非新功能
- 默认引擎、降级行为与配置表已有描述保持一致，D2/D4 不引入与配置表矛盾的表述

## 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | 中英结构同步 | 自动化（static） | 脚本分三层断言：① 标题层——提取两份 README 的 `##`/`###` 标题序列（排除代码块内 `#` 注释行），数量与顺序一一对应；② **章节内结构层**——**按标题序号位置对齐**（第①层先行保证序列一致；中英标题文本不同，不按文本对齐）后逐节比对 bullet 条数与表格行数，计数口径含 `-` 与编号列表项、含嵌套子 bullet；**豁免白名单：NPU 节 bullet 计数整节豁免（既有 ZH 散文结构差异，非本 PR 引入）；前置段对齐改由 A1 第③层「NPU 前置节双份各含 render 与 usermod」句级断言 + PR diff 人工复核覆盖**；③ **改动点句级配对层**——D1–D6 每处改动在双份均有对应关键句：徽章行双份含 `KDE Wayland`；Requirements 双份各含 CPU/CUDA/NPU 三路线行；NPU 前置节双份各含 `render` 与 `usermod`；Features 双份各含多引擎条目；Roadmap 节双份各含两条新 `[x]`（#16/#19/#27 与 #18/#24）；corrections 说明双份方向一致（错→对，不含反向 `skill`→`sql` 表述） | 三层全部通过。脚本存 research/ 留档（research/ = issue 工作区 `~/.github-issue-driven/zhuxixi/voice-input/issue-20/research/`，**不入库**） |
| A2 | 文档—代码一致性 | 自动化（static） | 配置表全部 `VOICE_INPUT_*` 变量名对照代码 `os.environ`/`getenv` 实际读取集合（`rg -o 'VOICE_INPUT_[A-Z_]+' *.py`，实测 14 个），无文档引用不存在的变量；Project Layout 所列文件在仓库均存在，且仓库**根目录**全部 `.py` 文件（含 12 个 test_*.py；不含 bench/、venv/、__pycache__/，bench/ 以单独一行覆盖）在表中**逐一出现、无豁免** | 变量集合无文档多出项；文件双向覆盖 100% |
| A3 | 徽章与链接有效 | 自动化（static） | 徽章 URL 为合法 shields.io 格式；文档内锚点链接（#requirements 等）与标题 slug 匹配 | 无断链锚点 |
| U1 | NPU 路线安装可复现 | 用户实测（OmniBook） | **时机：PR 合并后**，在 OmniBook `git pull` 至含本 PR 的 main（或合并前 checkout 本 PR 分支），按改后 README 的 NPU 路线逐项核对本机现有环境：render 组（`groups` 含 render）/ 驱动版本 ≥1.38.0 / venv openvino / LD_LIBRARY_PATH 注入 / 模型下载命令，不拆环境只核对 | 每项与文档描述一致；若文档步骤与实际环境冲突则修正文档 |
| U2 | CUDA/CPU 路线文档等价 | 用户实测（对照） | 对照 #14 验收评论（2026-10-29 7700K 实测），确认改后 Requirements/CUDA 描述与该实测环境一致；CPU 路线描述与 `engine.py` cpu 分支（`device="cpu"`，compute_type int8）不矛盾 | CUDA 描述无失真，X11 默认路径表述保持；CPU 行不暗示 GPU 要求 |

## 可测性拆分（A1/A2/A3）

静态校验脚本 `check-readme-sync.py`（放 research/ 留档，评估后可选择入 `bench/`，本 spec 默认**不入库**，避免文档校验引入维护面）：
- `extract_headings(path) -> list[str]`：纯函数，逐行扫描、跟踪 fenced code block 状态，排除代码内 `#` 行
- `extract_section_structures(path) -> dict[heading, {bullets: int, table_rows: int}]`：纯函数，按标题序号位置切节（不按标题文本），统计每节 bullet 条数（含 `-` 与编号列表项、含嵌套子 bullet）与表格行数（A1 第②层数据源；NPU 节 bullet 计数整节豁免，见验收矩阵）
- `extract_env_vars(path) -> set[str]`：纯函数，正则提取 `VOICE_INPUT_[A-Z_]+`
- `extract_layout_files(path) -> list[str]`：纯函数，提取 Project Layout 表反引号文件名
- 句级配对断言（A1 第③层）：按节切片后做子串匹配，D1–D6 每处一组断言常量，中英各一份
- 对照侧：`code_env_vars()` 用 `rg -o 'VOICE_INPUT_[A-Z_]+' *.py` 取代码实际集合；文件存在性用 `os.path.exists`
- 测试边界：脚本自身不依赖第三方库（stdlib only），`python3 check-readme-sync.py` 退出码 0/1 即通过/失败

## 风险与开放点

- 徽章 URL 转义细节以实现时 shields.io 实际渲染为准（文档改动无运行时风险）
- D3 corrections 修正若发现 README 另有同类错位，一并修正并在 PR 描述列出（仍属文档 bug 范畴）
- U1 若发现 README 与 OmniBook 实际环境不一致（如驱动版本号漂移），以实测为准修文档，并在 issue 评论记录

## 第 2 轮复核修订摘要（2026-11-03）

| 编号 | 裁定 | 处置 |
|---|---|---|
| R2-G1 | 接受并改 | D3 显式要求 ZH NPU 前置段 bullet 化（非目标加唯一例外）；ZH NPU Notes 段保持散文并列入 A1 第②层豁免白名单 |
| R2-D1 | 接受并改 | D3 ZH 行号 L248 → L254，并注明 L248 JSON 方向正确不可动 |
| R2-D2 | 接受并改 | A1 第②层补对齐机制（按标题序号位置）与计数口径（含 `-`/编号列表/嵌套） |
| R2-D3 | 接受并改 | D1 锚点改为双份各自保持（EN #requirements / ZH #依赖） |
| R2-D4 | 接受并改 | A2 范围收窄为根目录 .py，排除 bench/、venv/、__pycache__/ |
| R2-D5 | 接受并改 | A1 通过标准注明 research/ = issue 工作区绝对路径，不入库 |

## 第 3 轮复核修订摘要（2026-11-03，该轮真缺口为 0，收敛）

| 编号 | 裁定 | 处置 |
|---|---|---|
| R3-D1 | 接受并改（选 b 方案） | NPU 节 bullet 计数整节豁免，不做 Notes 子块扣减解析；前置段对齐由 A1 第③层句级断言 + PR diff 人工复核覆盖 |
| R3-D2 | 接受并改 | D3 EN 锚点 ~L273 → ~L260；D4 EN L37-38 → L38-39 |
| R3-D3 | 接受并改 | 非目标例外扩为「D3 指定的 NPU 前置段改动（EN 插入 render 权限项并改计数 / ZH 前置段 bullet 化）」 |

## 第 1 轮复核修订摘要（2026-11-03）

| 编号 | 裁定 | 处置 |
|---|---|---|
| R1-G1 | 接受并改 | A1 扩为三层：标题序列 + 章节内 bullet/表格行数比对 + D1–D6 句级配对断言；章节内结构与句级断言直接覆盖单语滞后失败模式 |
| R1-D1 | 接受并改 | U1 补时机：PR 合并后 pull main（或 checkout PR 分支），核对步骤含 `groups` 命令 |
| R1-D2 | 接受并改 | D3 corrections 修正显式 EN/ZH 同改（ZH L248 同错位已确认）；A1 第③层纳入 Roadmap 双份 `[x]` 与 corrections 方向断言 |
| R1-D3 | 接受并改 | U2 扩为 CUDA/CPU 双路线，CPU 对照 engine.py cpu 分支 |
| R1-D4 | 接受并改 | D5 钉死全列 12 个 test 文件；A2 改为双向覆盖无豁免 |
