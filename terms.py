"""专有名词软热词: 加载词表 + 构造 initial_prompt + 组装 transcribe 参数。

设计见 docs/2026-07-18-term-correction-design.md (v2)。纯标准库,无 GTK 依赖,可独立单测。
核心原则: 热词是增强,任何配置问题都不得阻断转写(降级到不传 prompt)。
"""

import json
import os
import sys

DEFAULT_TERMS_PATH = os.path.expanduser("~/.config/voice-input/terms.json")


def load_terms(path: str = DEFAULT_TERMS_PATH) -> dict:
    """读 terms.json。任何异常/结构问题 -> 返回空配置,不抛(降级,不阻断转写)。

    返回 dict 至少含 "terms"(list) 与 "hotwords"(None|list)。
    except 兜底 Exception:覆盖 UnicodeDecodeError 等 ValueError 子类(非 UTF-8 文件)。
    isinstance(cfg, dict) 守卫:防 JSON 顶层非 dict(裸数组/字符串/数字)时 setdefault 抛 AttributeError。
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            print(f"[voice-input] terms.json 顶层非 dict: {type(cfg).__name__}", file=sys.stderr)
            return {"terms": [], "hotwords": None}
        cfg.setdefault("terms", [])
        cfg.setdefault("hotwords", None)
        return cfg
    except Exception as e:
        print(f"[voice-input] terms.json load failed: {e}", file=sys.stderr)
        return {"terms": [], "hotwords": None}


def build_prompt(terms: list) -> "str | None":
    """把 terms 嵌入一句自然中文,作为 Whisper initial_prompt(解码上文)。

    空 terms 或非 list(string/int/dict) -> None(transcribe 不传 prompt,等价现状)。
    非 list 时 terms[:30] 会抛 TypeError 或被逐字符 join 成乱码,故 isinstance 守卫。
    截断到前 30 词控 token(Whisper initial_prompt 上限 224 token)。措辞是实测调优点。
    """
    if not isinstance(terms, list) or not terms:
        return None
    sample = terms[:30]
    return "以下是本次内容可能涉及的术语:" + "、".join(str(t) for t in sample) + "。"


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
