# #33 设计：词表容量与确定性纠错（corrections + token 预算）

Drafted: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-23T22:40:00+08:00
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-23T22:52:30+08:00（round 1 处置）
Revised: deepseek/deepseek-flash (selected) · deepseek/deepseek-flash (physical) @ 2026-10-23T23:02:10+08:00（round 2 收敛轮，消化 7 细化）

Issue: https://github.com/zhuxixi/voice-input/issues/33
调研：`research/01-context-and-design.md`
前作：#4 v2 设计（`docs/2026-07-18-term-correction-design.md` §6 Phase 2 草图）、#27（NPU 热词）、#32（三入口接线模式）

## 1. 问题

两个独立缺陷：

1. **稳定错字无兜底**：热词是概率偏置，`skill→sql`、`pjob→批诏` 这类固定错误（10-21 #33 评论有实测记录）永远不会被词表修掉。
2. **30 词硬上限浪费预算**：`build_prompt` 的 `terms[:30]` 是 224 token 硬限的粗近似。当前 30 词实测仅 147 token；纯中文词表可容纳 ~45–50 词，而上限卡在 30。

## 2. 设计决策

### D1 `apply_corrections`（方案 B，新增纯函数）

```python
def apply_corrections(text: str, corrections) -> str
```

- `corrections` 来自 `terms.json` 新增键 `"corrections"`：`{"sql": "skill", "批诏": "pjob"}`，wrong→right。
- **匹配语义**：
  - 键含非 ASCII（中文键）或**含 `[A-Za-z0-9_-]` 之外 ASCII 字符**（如 `c++`、`foo.bar`）→ 字面量子串匹配（无边界）；
  - 键全 ASCII 且仅含 `[A-Za-z0-9_-]` → **大小写不敏感 + 整词边界**（防 `sql` 误伤 `sqlite`）；casefold 后仅大小写不同的两键冲突时**后者覆盖前者**（last wins，README 提示避免）；
  - 替换值用配置原样，**允许空串**（删除语义，如幻觉残渣）；键为空串或键值非 str → 跳过该条目。
- **实现技术（钉死，防链式）**：全部合法键转义后按**键长降序**合并为**单个 alternation 正则**，ASCII 键裹 `(?<![A-Za-z0-9_-])…(?![A-Za-z0-9_-])` 边界，中文键裸字面量，`re.IGNORECASE`，**单次 `re.sub` + 回调查表**（表键 casefold 归一）。单次扫描即单趟：`{"a":"b"}` 的替换结果不会被 `{"b":"c"}` 二次命中（A3b 钉死）。长键排前保证同一位置最长匹配优先（Python re 按交替顺序取首个命中）。
- **降级**：`corrections` 缺失/非 dict/条目键值非法 → 跳过该条目并打一行 stderr 警告；**任何情况不抛异常、不改 text**。
- 空文本/None 输入原样返回。

### D2 token 预算截断（方案 C，改造 `build_prompt`）

```python
MAX_PROMPT_TOKENS = 200   # 224 硬限 - 24 安全余量（引导语 + 分隔符波动）
FALLBACK_TERM_CAP = 30    # 无 tokenizer 时的现状行为

def _token_count(text: str) -> int | None   # 懒导入，找不到 tokenizer 返回 None
def build_prompt(terms, token_count=_token_count) -> str | None
```

- `build_prompt` 逐词累加（引导语也计入），**装入下一个词会超预算即停**——保证整句 ≤ `MAX_PROMPT_TOKENS`，词粒度截断、保持顺序。
- `_token_count` 探测顺序（进程内首次探测后缓存结果，含失败缓存；**复用 `engine.py` 现有助手拼路径，不发明基目录，不硬编码模型名**）：
  1. 懒 `import engine`；模型名统一解析为 `os.environ.get(engine.ENV_MODEL) or engine.default_model(engine.engine_name(os.environ))`（与 `_model_label` 同构）：`engine.ov_snapshots_base(model)` 下的 `*/tokenizer.json`（npu 分支），`engine.snapshots_base(model)`（cuda/cpu 分支，Systran ct2 快照，7700K 路径依赖此条）。
  均用 `tokenizers.Tokenizer.from_file` 计数。`engine` 导入失败、两路径均无文件或 `tokenizers` 不可用（如 system python 裸跑）→ 返回 None，`build_prompt` 回退 `terms[:30]`（**现状行为，非错误**）并打一行提示（首次）。失败缓存意味着 tokenizer.json 事后出现也不重探（直到进程重启），属声明的取舍（常驻 worker 避免每请求重探 IO）。
- `token_count` 参数可注入（单测用 fake counter，纯 stdlib 可测）。
- 超预算截断时 stderr 一行：`[voice-input] terms: kept N/M words (token budget 200)`。

### D3 配置 schema（`terms.json`）

```json
{
  "terms": [...],
  "hotwords": null,
  "corrections": {"sql": "skill", "批诏": "pjob"}
}
```

- `load_terms` 对 `corrections` 做 `setdefault({}, )`；**两条降级返回字面量同步加 `"corrections": {}`**（顶层非 dict 与 except 兜底分支），不校验内部结构（D1 自带降级）。
- 热加载语义不变：worker 每请求重读，改完即生效。

### D4 接线（三入口 + 归档）

| 调用点 | 改动 |
|---|---|
| `transcribe_worker.py`（hold 主路径） | 转写得到 `text` 后：`text = apply_corrections(text, cfg.get("corrections"))`；corrections 与 terms 同一次 `load_terms` 读取（复用现有 per-request 加载）；有替换时 log 一行 `[voice-input] corrections: N replaced` |
| `transcribe_once.py` | 同上；stdout 打印纠正后文本 |
| `voice-ptt.py`（X11） | 同上；**归档分离 raw/final**：`archive_recording(wav, raw_text, final_text)`（archive.py 现有双文本接口，零改动），上屏 final |

三处的 terms 组装 try 块扩展为同时取 `corrections`；组装失败仍降级（无 prompt、无 corrections，转写不中断）。

### D5 降级与安全

- corrections/budget 任何异常不阻断转写（沿袭 #4 §3.5 原则）。
- corrections 只允许 wrong→right 单向映射；文档提示「只放非正常词作键，避免误伤正常词」（v2 设计原话）。
- prompt 预算收紧不会改变现状行为（当前 30 词 147 < 200，不触发截断）。

### D6 可观测性

- 替换发生：`[voice-input] corrections: N replaced`（N=总替换次数）。
- 词表截断：`[voice-input] terms: kept N/M words (token budget 200)`。
- tokenizer 缺失回退：`[voice-input] terms: tokenizer unavailable, falling back to 30-word cap`（仅首次）。

### D7 非目标

- 不做方案 A（焦点窗口动态选词；Wayland 无焦点窗口查询，#36 已析）。
- 不换模型、不做 LLM 纠错、不动 hotwords 语义。
- 不做 corrections 的通配/正则/上下文条件匹配（保持朴素确定性）。
- 不给 token 预算加环境变量（常量 200，写死并注释依据）。

## 3. 契约与边界

- `terms.py` 保持「模块顶层纯标准库」；`tokenizers` 懒导入（venv 依赖，system python 裸跑回退 30 上限）。
- CUDA 契约不变：`engine.py` 零改动。
- 归档契约：`archive.py` 零改动；voice-ptt 的 `enhanced = raw != final` 由现有逻辑自动落盘。
- 单测在 system python 下全绿（fake counter / 无 corrections）；真实 tokenizer 用例在 venv 下跑（skipUnless），复用 #32 的 A6b 模式。
- 不保证：corrections 不能修复所有错字（只修配置表里列出的）；预算截断对中英混合词表的收益取决于实际 token 构成。

## 4. 可测性拆分设计

| 层 | 单元 | 测试方式 |
|---|---|---|
| 纯函数 | `apply_corrections` | unit（stdlib）：中文替换、ASCII 整词/大小写、最长优先、非法条目跳过、恒等 |
| 纯函数 | `build_prompt(token_count=fake)` | unit（stdlib）：预算内全保留、超预算停在词边界、counter=None 回退 30、空表 None |
| 懒 IO | `_token_count` 探测与缓存 | unit（stdlib）：monkeypatch 探测路径，断言进程内缓存（含失败缓存）与 None 回退；真实路径归 A10 |
| 配置 | `load_terms` corrections 键 | unit：缺失→{}，非法类型不炸 |
| 入口接线 | worker | integration（fake model + fake corrections）：替换作用于响应文本；terms+corrections 同源热加载 |
| 入口接线 | transcribe_once | **功能接线**：扩展 `test_engine.py` 既有 `TestTranscribeOnceTerms`（干净子进程 + 假模型 + terms_path 注入）加 corrections 用例（stdout 应为纠正后文本）；voice-ptt 因 GTK 依赖保留 static 断言（源码含 `apply_corrections(`、归档传 raw/final 两个变量） |
| 真实 tokenizer | `_token_count` 真路径 + 现役词表不截断 | venv + OV snapshot（skipUnless）：30 词 ≤200 不截断；60 中文词触发截断且计数真实 |
| 全仓回归 | — | #32 的 A11a/A11b 套件原样跑绿 |

测试边界：不测 Whisper 对 prompt 的行为变化（那是 #4/#27 已验的既有语义）；不测 archive 落盘细节（archive.py 自有测试）。

## 5. 验收矩阵

| ID | 功能点 | 验收方式 | 具体验证 | 通过标准 |
|----|--------|----------|----------|----------|
| A1 | 中文键子串替换 | 自动化（unit，stdlib） | `python3 -m unittest test_terms.TestApplyCorrections -v` | `批诏`→`pjob` 生效；无键时恒等 |
| A2 | ASCII 整词 + 大小写不敏感 + 不误伤 | 自动化（unit，stdlib） | 同上 | `sql`/`SQL`→`skill`；`sqlite` 不被改 |
| A3 | 最长键优先 + 不链式 | 自动化（unit，stdlib） | 同上 | 重叠键时长键生效；**A3b：`{"a":"b","b":"c"}` 输入 `a` 输出 `b`（非 `c`）；空值删除语义** |
| A4 | 非法条目降级不炸 | 自动化（unit，stdlib） | 同上 | 跳过 + 警告；text 原样；无异常 |
| A5 | 预算截断（fake counter） | 自动化（unit，stdlib） | `python3 -m unittest test_terms.TestTokenBudget -v` | 词边界停；顺序保持；日志行出现 |
| A6 | counter=None 回退 30 + 探测缓存语义 | 自动化（unit，stdlib） | 同上，含**改造既有 `test_truncates_to_30_terms` 为显式注入 `token_count=None`**；新增 monkeypatch 用例断言探测结果进程内缓存（含失败缓存，二次调用不重探） | 与现状 `[:30]` 一致；提示行仅首次；缓存行为钉住；venv 下跑同样绿 |
| A7 | `load_terms` corrections 解析 | 自动化（unit，stdlib） | `python3 -m unittest test_terms.TestLoadTermsCorrections -v`；**既有 `test_missing_file_returns_empty_no_raise` / `test_invalid_json_returns_empty_no_raise` / `TestLoadTermsRobustness` 两处等值断言同步加 `corrections: {}`** | **合法文件缺键 → `{}`；顶层非 dict / 文件缺失 / JSON 错的降级返回也含 `corrections: {}`**；不抛 |
| A8 | worker 接线 | 自动化（integration，stdlib） | `python3 -m unittest test_transcribe_worker.TestCorrectionsWiring -v` | 响应文本已纠正；raw 不外泄；热加载含 corrections |
| A9 | transcribe_once 功能接线 + voice-ptt 静态接线 | 自动化（integration + static，stdlib） | `python3 -m unittest test_engine.TestTranscribeOnceTerms -v`（新增 corrections 用例）+ `python3 -m unittest test_terms.TestCorrectionsWiring -v`（voice-ptt 源码断言） | transcribe_once stdout 为纠正后文本；voice-ptt 含 `apply_corrections(` 且归档传 raw/final |
| A10 | 真实 tokenizer + 预算行为 | 自动化（unit，venv，skipUnless **任一探测路径命中 tokenizer.json** 且 tokenizers 可导入） | `venv/bin/python3 -m unittest test_terms.TestRealTokenizer -v`；用**内置 fixture**（30 词复刻现役构成，不读用户配置；现役实测 147 token 仅作注释） | fixture 真实计数在 100–200 区间且不截断；60 个中文词 fixture 截断到预算内且停在词边界；**不与 og.Tokenizer 做逐字相等断言**（跨实现计数只做区间合理性） |
| A11 | 全仓回归 | 自动化（unit） | #32 的 A11a（system python）+ A11b（venv `test_engine`）命令 | 全绿（含 #32 的 `test_vad_trim`） |
| U1 | 真机听写纠正生效 | 用户实测 | 合并后主 checkout **`git pull` 到含本改动的 commit**，再 `systemctl --user restart voice-hold`；corrections 配 `{"sql":"skill","批诏":"pjob","退黑素":"褪黑素"}`；口述含 skill/pjob/褪黑素的句子 | 三个词输出正确；journal 出现 `corrections: N replaced` |
| U2 | X11 路径（voice-ptt） | 用户实测（7700K） | 与 #32 U5 同机同趟：`git pull` 后 `./voice-ptt.sh` 听写含错字词 | 文本纠正、归档 raw≠final；**可 pending**（与 #32 U5 合并执行） |

失败分支覆盖：A4（非法配置）、A6（tokenizer 缺失）、A8 的 terms 组装失败降级（沿用 worker 既有用例模式）。

## 6. 落地与验证时机

- 代码走 worktree + PR（流程同 #32）；U1 合并后主 checkout 验证；U2 与 #32 U5 同机补测，pending 可接受。
- README 双语同步：配置表加 `corrections` 行；「Custom vocabulary」节加 corrections 子节与 token 预算说明（与 #20 无冲突，只改相关行）。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| ASCII 整词正则误伤（如键 `pi`） | 文档提示只放非正常词作键；A2 单测钉住整词语义 |
| corrections 链式替换意外 | 单次 `re.sub` 扫描，D1 钉死实现技术；A3b 钉住不接力 |
| tokenizer 探测在生产路径（system python）失败 | 回退 30 上限 = 现状，无损；A6 钉住 |
| 预算 200 对极端混合词表仍不够 | 词粒度截断保证不超；日志可观测；预算写死有注释依据 |
| 替换把用户本来想输出的词改掉 | wrong→right 表由用户手维护；README 提示键只放错形 |

## 8. 非目标（重申）

不做动态选词（A）、不做正则/条件 corrections、不动 hotwords、不加预算环境变量、不保证修正所有错字。
