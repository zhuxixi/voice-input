"""专有名词软热词: 加载词表 + 构造 initial_prompt + 组装 transcribe 参数。

设计见 docs/2026-07-18-term-correction-design.md (v2)。纯标准库,无 GTK 依赖,可独立单测。
核心原则: 热词是增强,任何配置问题都不得阻断转写(降级到不传 prompt)。
"""

import json
import os
import sys

DEFAULT_TERMS_PATH = os.path.expanduser("~/.config/voice-input/terms.json")


def load_terms(path: str = DEFAULT_TERMS_PATH) -> dict:
    """读 terms.json。文件缺失/JSON 错 -> 返回空配置,不抛异常(降级)。

    返回 dict 至少含 "terms"(list) 与 "hotwords"(None|list)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("terms", [])
        cfg.setdefault("hotwords", None)
        return cfg
    except FileNotFoundError:
        return {"terms": [], "hotwords": None}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[voice-input] terms.json parse failed: {e}", file=sys.stderr)
        return {"terms": [], "hotwords": None}
