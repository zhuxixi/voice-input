# 语音输入专有名词识别增强 — 设计文档

- 创建: 2026-07-18
- 修订: 2026-07-25
- 状态: 设计待审 (v2)
- 关联: issue #4, `voice-ptt.py`, `archive.py`

## 修订记录

- **v1 (2026-07-18)**: A 后处理纠错 + B initial_prompt 软热词；单文件；`prompt=` 参数
- **v2 (2026-07-25)**: 简化为**纯 terms 方案**(只维护正确词表)。基于实测核实调整:
  - `prompt` 参数名应为 `initial_prompt`(faster-whisper 1.2.1 核实),v1 照搬会 TypeError
  - 接入点行号因归档 #1 重构失效(现 `transcribe` 在 179 行,非 127)
  - 拆 `terms.py` 独立模块(归档教训: `voice-ptt.py` 顶部 GTK import 阻碍单测)
  - corrections(A 方案)**移入非目标**: 先实测 terms 效果,不够再加;`archive.py` 已预留 raw/final 分离,未来启用零代码、改 JSON 即可

## 1. 背景与问题

现有语音输入栈:

- 引擎: faster-whisper 1.2.1 + Whisper large-v3(本地 CUDA,双 2080 Ti)
- 流程: 按住右 Cmd 录音 → Whisper 转写 → 写剪贴板 → xdotool 切回焦点 → `Ctrl+Shift+V` 粘贴
- 主脚本: `~/.local/share/voice-input/voice-ptt.py`
- 关键特性: **绕过 IBus/Fcitx5 输入法通道**,直接走剪贴板

**问题**: OOV(词表外)专有名词被误识别为形近中文。实测样本(来自 `recordings/index.jsonl`):

- `zima` → `Z码` / `兹马`("我用Z码做审查"、"Z码标签")
- `issue` → `艺术`("先创建这个艺术"、"这个艺术先不做")
- zima 漂移: 有时 `Zima` 有时 `Z码`

根因: 这些词不在 Whisper 解码词表里,解码器按发音猜了个形近中文。

**约束**: 不重训模型、不换引擎。

## 2. 目标与非目标

**目标**: 在不重训、不换引擎前提下,通过 `initial_prompt` 软热词降低 OOV 专有名词误识别,使 `zima`/`jfox` 这类高频术语被正确输出。

**非目标**:

- ❌ 不重训/微调 Whisper
- ❌ 不换引擎到 FunASR 等支持硬热词的方案(为几个词不值)
- ❌ **不做 corrections 后处理纠错**(v2 决策): 先实测 terms 够不够;不够再加,`archive.py` 接口已预留
- ❌ 不做 LLM 上下文纠错
- ❌ 不做自动词表提取(手填 13 个核心词;fragments 提炼验证过,去噪后也就这些)

## 3. 方案: B initial_prompt 软热词(纯 terms)

### 3.1 数据流

```
按住右 Cmd 录音 → stop_recording (voice-ptt.py,行号随实现变动不硬标)
  m = load_model()
  # terms 组装独立 inner try(异常降级到无 prompt,不阻断 transcribe,见 §3.5)
  cfg    = load_terms()
  prompt = build_prompt(cfg.get("terms", []))     ← 软热词(源头引导)
  extra  = build_transcribe_kwargs(cfg)           ← hotwords 可选,默认不传
  m.transcribe(WAVFILE, language="zh", initial_prompt=prompt, **extra)
  text = "".join(s.text for s in segments).strip()
  archive_recording(WAVFILE, text, text)          ← 不变(纯 terms,raw==final)
  写剪贴板 → 粘贴
```

terms 作为解码上文: 模型"见过"这些词的正确拼写,解码到对应发音时倾向照抄 → 整句因关键名词对了而正确。

`initial_prompt` 是**概率性软引导**,不保证 100%——zima 的漂移本身就是证据。它的价值是把正确率从"基本靠运气"拉到"大概率对"。残留的顽固误识别留给未来 corrections 兜底(见 §6)。

### 3.2 配置文件 `~/.config/voice-input/terms.json`

XDG 配置目录,个人词表与代码解耦。手动维护,热加载(每次转写重读)。

```json
{
  "terms": [
    "zima", "jfox", "Claude", "Kimi", "DeepSeek", "GitHub",
    "Boktionary", "Wiktionary",
    "skill", "daemon", "babysit", "fragments", "transcript"
  ],
  "hotwords": null
}
```

- `terms`: 正确的专有名词,喂 `build_prompt`。13 个核心词(从 JFox fragments 1897 条语音输入提炼 + 去噪,移除了 faster-whisper/CTranslate2/PipeWire/fcitx5 等项目内部术语)
- `hotwords`: `null` = 不传(默认);想试词级加权时填 `["zima","jfox",...]`(复用 terms,非额外维护)

### 3.3 模块 `terms.py`(独立·可单测·无 GTK 依赖)

与 `archive.py`/`media_pause.py` 一致,独立模块。3 个函数:

#### `load_terms(path=DEFAULT_TERMS_PATH) -> dict`

```python
def load_terms(path=DEFAULT_TERMS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):        # 顶层非 dict(裸数组/字符串)降级
            return {"terms": [], "hotwords": None}
        cfg.setdefault("terms", [])
        cfg.setdefault("hotwords", None)
        return cfg
    except Exception:                        # 覆盖 FileNotFoundError/JSONDecodeError/UnicodeDecodeError 等,降级不阻断
        return {"terms": [], "hotwords": None}
```

文件缺失/JSON 错 → 返回空配置,**不抛异常**(降级,不阻断转写)。

#### `build_prompt(terms: list) -> str | None`

```python
def build_prompt(terms):
    if not isinstance(terms, list) or not terms:   # 非 list(string/int/dict)降级
        return None
    sample = terms[:30]   # 截断 30 个 term 控规模(Whisper initial_prompt 上限 224 token 的近似)
    return "以下是本次内容可能涉及的术语:" + "、".join(str(t) for t in sample) + "。"
```

返回 `None` 时 `transcribe` 不传 prompt(等价现状)。措辞(中文句包装 vs 纯词列表)留作实测调优点。

#### `build_transcribe_kwargs(cfg: dict) -> dict`

```python
def build_transcribe_kwargs(cfg):
    kw = {}
    hotwords = cfg.get("hotwords")
    if isinstance(hotwords, list) and hotwords:
        kw["hotwords"] = " ".join(str(h) for h in hotwords)   # 非空 list join 成 str
    elif isinstance(hotwords, str) and hotwords.strip():
        kw["hotwords"] = hotwords
    # 其他类型(int/dict/bool)及空值不传:faster-whisper hotwords 是 Optional[str],非 str 经 .strip() 崩
    return kw
```

把 transcribe 的可选参数组装集中一处,默认空 dict(只传 `language` + `initial_prompt`)。

### 3.4 接入点(`voice-ptt.py` 改动)

**顶部 import 区**新增:

```python
from terms import load_terms, build_prompt, build_transcribe_kwargs
```

**第 179 行**(归档 #1 重构后的真实位置,v1 标的 127 已失效):

```python
# 改前
segments, info = m.transcribe(WAVFILE, language="zh")
# 改后
cfg = load_terms()
segments, info = m.transcribe(
    WAVFILE, language="zh",
    initial_prompt=build_prompt(cfg.get("terms", [])),
    **build_transcribe_kwargs(cfg),
)
```

第 180、188、194 行**不变**: 纯 terms 方案下 `raw==final`,归档仍 `archive_recording(WAVFILE, text, text)`。

**热加载**: `load_terms()` 每次 `stop_recording` 调用,文件小(JSON 几百字节),IO 开销可忽略。改完 `terms.json` 下次录音立即生效,无需重启常驻进程。

### 3.5 错误处理与降级

| 故障 | 行为 |
|---|---|
| `terms.json` 不存在 | `load_terms` 返回空配置,不传 prompt(等同现状) |
| JSON 格式错误 | 同上,并 stderr 打印警告 |
| `terms` 为空 | `build_prompt` 返回 None,不传 prompt |
| `hotwords` 缺失/空 | `build_transcribe_kwargs` 不含该键,不传 |
| `load_terms` 任何异常 | 返回空配置,转写不中断 |

核心原则: **热词是增强,任何配置问题都不得阻断原本的转写流程**。

## 4. hotwords 可选开关(效果对比手段)

`initial_prompt`(上文软引导,成熟)与 `hotwords`(CTranslate2 词级加权,中文 OOV 效果未实测)可叠加,不互斥。

`hotwords` 默认 `null`(关)。它是"试效果"的对比手段: 若 `initial_prompt` 对某词效果不够,把 `terms` 复制进 `hotwords` 字段重试,看词级加权是否更好。零额外维护(复用 terms)。

## 5. 测试 `test_terms.py`(独立单测)

- `build_prompt([])` → `None`
- `build_prompt(["zima","jfox"])` → 含两词的中文句
- `build_prompt(超 30 词)` → 截断到 30
- `load_terms()` 文件缺失 → `{"terms":[],"hotwords":None}` 不抛异常
- `load_terms()` JSON 错 → 空 dict + 不抛
- `build_transcribe_kwargs({"hotwords":None})` → `{}`(空,不传 hotwords)
- `build_transcribe_kwargs({"hotwords":["zima"]})` → `{"hotwords":"zima"}`(list join 成 str)

`terms.py` 无 GTK import,可独立 `python -m unittest`(venv 无 pytest;先例: `test_archive.py`/`test_media_pause.py`)。

## 6. 渐进路线

- **Phase 1(本次 #4)**: 纯 terms。维护 13 词,喂 `initial_prompt`。合并到 main 后用户实测(见 §8)。
- **Phase 2(条件触发,非必须)**: 若实测后某词顽固残留(如 `Z码` 仍出现),启用 corrections 兜底——
  - `terms.py` 加 `apply_corrections(text, corrections)`(朴素 `str.replace`,只放非正常词的 wrong 避免误伤)
  - `voice-ptt.py` 第 180 行分离 `raw`/`final`,`archive_recording(WAVFILE, raw, final)`
  - `archive.py` **零改动**(raw/final/enhanced 接口已就绪,`enhanced = raw != final`)
  - 往 `terms.json` 加 `"corrections": {"Z码":"zima"}`,热加载生效

Phase 2 的所有接口在 Phase 1 已预留,触发时零代码改 JSON 即可启用(除 `apply_corrections` 函数本身)。

## 7. 文件改动清单(Phase 1)

| 文件 | 动作 |
|---|---|
| `terms.py` | **新建**: 3 个函数(`load_terms`/`build_prompt`/`build_transcribe_kwargs`) |
| `voice-ptt.py` | 改: 顶部 import + 第 179 行 transcribe 加 `initial_prompt`/可选 `hotwords` |
| `test_terms.py` | **新建**: 单元测试 |
| `~/.config/voice-input/terms.json` | **新建**: 13 个 terms + `hotwords:null`(用户手动放,不进仓库) |
| `archive.py` | **零改动** |

## 8. 验证(合并到 main 后用户实测)

用户习惯合并后手工测(不在 CR 前阻断)。验证项:

- **历史音频回放**: 用 `recordings/2026-07-19_225840/audio.wav`(原始转写"我用Z码做审查"),重跑 `transcribe(initial_prompt=build_prompt(terms))`,看是否输出 `zima`。直接证明 terms 对 zima 有没有用。
- **日常使用**: 录"我用 zima 做代码审查",确认输出 `zima` 非 `Z码`
- **降级**: 删 `terms.json`,转写仍正常
- **热加载**: 改 `terms.json` 不重启,下次录音生效
- **hotwords 对比**(可选): `initial_prompt` 效果不够时,填 `hotwords` 重试对比

若历史音频回放显示 terms 已让 zima 正确 → Phase 2 不触发,corrections 永不实现。

## 9. 为什么不重训(结论备忘)

- OOV 是词表问题,非声学模型问题,重训收益不对等
- 重训需 GPU 集群 + 大量标注音频,成本极高
- 剪贴板架构天然允许转写后改文本(Phase 2 corrections 用这个特权)
- Whisper 生态无成熟硬热词,真要硬热词得换引擎(FunASR Paraformer),为几个词不值
- `initial_prompt` 注入术语是 Whisper 经典技巧,对 large-v3 + 中文 + 英文专有名词应有显著效果(具体提升幅度以 §8 实测为准)
