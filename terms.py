"""专有名词软热词: 加载词表 + 构造 initial_prompt + 确定性纠错 + 组装 transcribe 参数。

设计见 docs/2026-07-18-term-correction-design.md (v2) 与
 docs/superpowers/specs/2026-10-23-terms-capacity-corrections-design.md (#33)。
纯标准库,无 GTK 依赖,可独立单测;engine/tokenizers 只在探测 tokenizer 时懒导入。
核心原则: 热词/纠错/预算都是增强,任何配置问题都不得阻断转写(降级到不传 prompt)。
"""

import glob
import json
import os
import re
import sys

DEFAULT_TERMS_PATH = os.path.expanduser("~/.config/voice-input/terms.json")

MAX_PROMPT_TOKENS = 200   # Whisper initial_prompt 硬限 224 - 24 安全余量(引导语/分隔符波动)
FALLBACK_TERM_CAP = 30    # 无 tokenizer 可用时的旧行为(terms[:30])
_ASCII_WORD_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


def load_terms(path: str = DEFAULT_TERMS_PATH) -> dict:
    """读 terms.json。任何异常/结构问题 -> 返回空配置,不抛(降级,不阻断转写)。

    返回 dict 至少含 "terms"(list)、"hotwords"(None|list) 与 "corrections"(dict)。
    三条返回路径(正常/顶层非 dict/except 兜底)都带 corrections 默认值;
    corrections 内部结构不校验(apply_corrections 自带降级,spec D1/D3)。
    except 兜底 Exception:覆盖 UnicodeDecodeError 等 ValueError 子类(非 UTF-8 文件)。
    isinstance(cfg, dict) 守卫:防 JSON 顶层非 dict(裸数组/字符串/数字)时 setdefault 抛 AttributeError。
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            print(f"[voice-input] terms.json 顶层非 dict: {type(cfg).__name__}", file=sys.stderr)
            return {"terms": [], "hotwords": None, "corrections": {}}
        cfg.setdefault("terms", [])
        cfg.setdefault("hotwords", None)
        cfg.setdefault("corrections", {})
        return cfg
    except Exception as e:
        print(f"[voice-input] terms.json load failed: {e}", file=sys.stderr)
        return {"terms": [], "hotwords": None, "corrections": {}}


def apply_corrections_counted(text, corrections):
    """确定性替换,返回 (新文本, 替换次数)。spec D1。

    所有合法键转义后按长度降序合并为**单个** alternation 正则,单次 re.sub + 回调查表
    => 单趟:替换结果不会被其它键二次命中({"a":"b","b":"c"} 输入 a 只出 b)。
    ASCII 词键(仅 [A-Za-z0-9_-])加整词边界 + 忽略大小写(防 sql 误伤 sqlite);
    其它键(中文/c++/foo.bar)按裸字面量匹配。空串值 = 删除。
    非法条目(空键/非 str 键值)跳过并打一行 stderr 警告;任何情况不抛、不改 text。
    """
    if not text or not isinstance(corrections, dict):
        return text, 0
    table = {}
    keys = []
    for key, value in corrections.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            print(f"[voice-input] corrections: skip invalid entry {key!r}", file=sys.stderr)
            continue
        table[key.casefold()] = value          # 仅大小写不同的键冲突:后者覆盖前者
        keys.append(key)
    if not keys:
        return text, 0

    def piece(key):
        if _ASCII_WORD_KEY.fullmatch(key):
            return r"(?<![A-Za-z0-9_-])" + re.escape(key) + r"(?![A-Za-z0-9_-])"
        return re.escape(key)                  # 中文 / c++ / foo.bar:裸字面量

    pattern = re.compile("|".join(piece(k) for k in sorted(keys, key=len, reverse=True)),
                         re.IGNORECASE)
    count = {"n": 0}

    def sub(match):
        count["n"] += 1
        return table.get(match.group(0).casefold(), match.group(0))

    return pattern.sub(sub, text), count["n"]


def apply_corrections(text, corrections):
    """spec D1 公开 API:替换后的文本(丢弃计数)。"""
    return apply_corrections_counted(text, corrections)[0]


_TOKENIZER_STATE = {"probed": False, "counter": None, "warned": False}


def _probe_counter():
    """按引擎拼路径找 whisper tokenizer.json,返回计数函数或 None。spec D2。

    模型名解析与 _model_label 同构(env 覆盖 > 引擎默认);npu 走 ov_snapshots_base,
    cuda/cpu/auto 走 Systran ct2 快照目录。进程内结果(含失败)由 _token_count 缓存。
    """
    try:
        import engine
    except Exception:
        return None
    try:
        name = engine.engine_name(dict(os.environ))
        model = os.environ.get(engine.ENV_MODEL) or engine.default_model(name)
        base = engine.ov_snapshots_base(model) if name == "npu" else engine.snapshots_base(model)
        candidates = sorted(glob.glob(os.path.join(base, "*", "tokenizer.json")))
        if not candidates:
            return None
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(candidates[0])  # 首个快照目录,与 resolve_model_path 取向一致
        return lambda text: len(tok.encode(" " + text.strip()).ids)
    except Exception:
        return None


def _token_count(text):
    """首次调用探测 tokenizer 并缓存结果(含失败,常驻 worker 不重探 IO)。spec D2。"""
    if not _TOKENIZER_STATE["probed"]:
        _TOKENIZER_STATE["probed"] = True
        _TOKENIZER_STATE["counter"] = _probe_counter()
    counter = _TOKENIZER_STATE["counter"]
    return counter(text) if counter else None


def build_prompt(terms: list, token_count=_token_count) -> "str | None":
    """把 terms 嵌入一句自然中文,作为 Whisper initial_prompt(解码上文)。

    空 terms 或非 list(string/int/dict) -> None(transcribe 不传 prompt,等价现状)。
    非 list 时逐词 join 会抛 TypeError 或被逐字符 join 成乱码,故 isinstance 守卫。
    词粒度贪心装填,整句(含引导语)<= MAX_PROMPT_TOKENS;至少保留一个词。
    token_count 返回 None(无 tokenizer)时回退 terms[:30] = 旧行为逐字节一致。
    措辞是实测调优点。
    """
    if not isinstance(terms, list) or not terms:
        return None
    intro = "以下是本次内容可能涉及的术语:"
    kept = []
    for term in terms:
        prompt = intro + "、".join([*kept, str(term)]) + "。"
        n = None
        if token_count is not None:
            try:
                n = token_count(prompt)
            except Exception:
                n = None
        if n is None:
            if not _TOKENIZER_STATE["warned"]:
                _TOKENIZER_STATE["warned"] = True
                print("[voice-input] terms: tokenizer unavailable, "
                      f"falling back to {FALLBACK_TERM_CAP}-word cap", file=sys.stderr)
            return intro + "、".join(str(t) for t in terms[:FALLBACK_TERM_CAP]) + "。"
        if n <= MAX_PROMPT_TOKENS or not kept:
            kept.append(str(term))
            if n > MAX_PROMPT_TOKENS:  # 首词单独就超预算:保留它,停止
                print(f"[voice-input] terms: kept {len(kept)}/{len(terms)} words "
                      f"(token budget {MAX_PROMPT_TOKENS})", file=sys.stderr)
                break
            continue
        print(f"[voice-input] terms: kept {len(kept)}/{len(terms)} words "
              f"(token budget {MAX_PROMPT_TOKENS})", file=sys.stderr)
        break
    return intro + "、".join(kept) + "。"


def build_transcribe_kwargs(cfg: dict) -> dict:
    """组装 transcribe 的可选参数。默认空 dict(只传 language + initial_prompt)。

    hotwords 为 None/空/False 时不传(等价现状);有值时返回 {"hotwords": <str>}。
    faster-whisper 1.2.1 的 hotwords 是 Optional[str](内部 .strip()),
    list 须 join 成空格分隔字符串,否则传 list 会 AttributeError 阻断转写。
    """
    kw: dict = {}
    hotwords = cfg.get("hotwords")
    if isinstance(hotwords, list) and hotwords:
        joined = " ".join(str(h) for h in hotwords)
        if joined.strip():               # 空 list/全空白元素 → 不传
            kw["hotwords"] = joined
    elif isinstance(hotwords, str) and hotwords.strip():
        kw["hotwords"] = hotwords
    # 其他类型(int/dict/bool)及空值不传:faster-whisper hotwords 是 Optional[str],非 str 经 .strip() 崩
    return kw


# --- prompt-echo guard (#43) ------------------------------------------------
# 被 VAD 裁剩的极短残片会让解码器「回读」initial_prompt,把词表词循环输出
# (2026-10-24 实测: Zima/DeepSeek/GitHub/Kimi ×19)。判定纯文本特征,不依赖音频。
ECHO_SINGLE_MIN = 4      # 单个词表词出现次数
ECHO_MULTI_MIN = 2       # 多个词表词各自出现次数
ECHO_SHORT_TEXT = 60     # 多词情形的文本长度上限(列举通常远短于此)


def _term_hits(text: str, terms) -> list:
    """按出现顺序返回命中的词表词(长词优先匹配,不重叠)。

    纯 ASCII 词加词边界,与 `apply_corrections_counted` 的口径一致:
    否则 `AI` 会命中 email/said,`Go` 会命中 Google,进而误判回声。
    """
    lowered = text.lower()
    ordered = sorted({str(t).strip() for t in terms if str(t).strip()},
                     key=len, reverse=True)
    if not ordered:
        return []
    pieces = []
    for term in ordered:
        escaped = re.escape(term.lower())
        if _ASCII_WORD_KEY.match(term):
            escaped = rf"(?<![a-z0-9_-]){escaped}(?![a-z0-9_-])"
        pieces.append(escaped)
    pattern = re.compile("|".join(pieces))
    hits = []
    pos = 0
    while True:
        match = pattern.search(lowered, pos)
        if not match:
            return hits
        hits.append(match.group(0))
        pos = match.end()


def _is_periodic(sequence: list) -> bool:
    """True when the whole sequence is a repetition of a shorter block (>= 2x)."""
    size = len(sequence)
    for period in range(1, size // 2 + 1):
        if size % period:
            continue
        if all(sequence[i] == sequence[i - period] for i in range(period, size)):
            return True
    return False


def hotword_dominated(text, terms) -> bool:
    """True when almost nothing is left after removing the hotword hits.

    Used before dropping an echoed transcript: a 19x hotspot loop leaves only
    punctuation behind, while a real sentence that happens to mention a hotword
    four times keeps its Chinese/English words.
    """
    hits = _term_hits(text, terms)
    if not hits:
        return False
    residual = text.lower()
    for hit in hits:
        residual = residual.replace(hit, " ", 1)
    residual = re.sub(r"[\W_]+", "", residual)
    return len(residual) <= max(3, int(0.2 * len(text)))


def looks_like_prompt_echo(text, terms) -> bool:
    """Heuristic: did the decoder read the hotword prompt back instead of audio?

    Fires when a) one hotword repeats >= 4 times, b) two or more hotwords repeat
    >= 2 times each in a short text, or c) the hotword sequence is periodic.
    Genuine enumeration ("Zima, DeepSeek, GitHub, Kimi", one hit each) stays False.
    """
    if not text or not terms:
        return False
    hits = _term_hits(text, terms)
    if not hits:
        return False
    counts = {}
    for hit in hits:
        counts[hit] = counts.get(hit, 0) + 1
    if max(counts.values()) >= ECHO_SINGLE_MIN:
        return True
    repeated = [term for term, count in counts.items() if count >= ECHO_MULTI_MIN]
    if len(repeated) >= 2 and len(text) < ECHO_SHORT_TEXT:
        return True
    if len(hits) >= 2 * ECHO_MULTI_MIN and _is_periodic(hits):
        return True
    return False
