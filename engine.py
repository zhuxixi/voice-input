"""转写引擎选择与模型构造: VOICE_INPUT_ENGINE / VOICE_INPUT_MODEL 的单一事实源。

设计见 docs/superpowers/specs/2026-09-19-engine-selectable-cpu-cuda-design.md (#16)。
纯标准库,无 GTK 依赖;faster_whisper 仅在 build_model 内懒导入(可注入工厂替代),
主程序(voice-ptt.py)与 CLI 工具(transcribe_once.py,经 test-mic.sh/voice-toggle.sh
调用)共用本模块,替代原先三处内嵌的 CUDA 调用。

默认行为契约(spec "Safety contract",test_engine.py 钉死):两个环境变量都不设时
必须与历史行为(HEAD 40f35bb)字节级等价 —— engine=cuda, model=large-v3,
构造参数 device="cuda", compute_type="float16"。勿改默认值;要改先改 spec。
"""

import os
import sys

# npu 是占位:选择时合法,构造时 NotImplementedError(fail fast,见 #19)。
VALID_ENGINES = ("cuda", "cpu", "auto", "npu")

ENV_ENGINE = "VOICE_INPUT_ENGINE"
ENV_MODEL = "VOICE_INPUT_MODEL"

# 契约默认值:不设环境变量 = 历史 CUDA 行为(#16 spec Safety contract)。
DEFAULT_ENGINE = "cuda"
DEFAULT_MODEL = "large-v3"


def model_name(env: dict) -> str:
    """VOICE_INPUT_MODEL -> 模型名,默认 "large-v3"。

    env 显式传 dict(不隐式读全局 os.environ),单测可构造变体而不污染进程环境。
    """
    return env.get(ENV_MODEL, DEFAULT_MODEL)


def engine_name(env: dict) -> str:
    """VOICE_INPUT_ENGINE -> 引擎名,默认 "cuda";非法值 ValueError。

    fail fast 且列出全部合法值:静默降级会把「配置坏了」伪装成「变慢了」,难排查。
    """
    value = env.get(ENV_ENGINE, DEFAULT_ENGINE)
    if value not in VALID_ENGINES:
        raise ValueError(
            f"[voice-input] unsupported {ENV_ENGINE}={value!r}; "
            f"valid values: {', '.join(VALID_ENGINES)}"
        )
    return value


def snapshots_base(model: str) -> str:
    """模型名对应的 HF hub snapshots 目录(仅拼路径,不检查存在性)。

    与历史 HEAD 的 SNAPSHOTS_DIR 常量同构:large-v3 时逐字相等(A1 契约测试钉死)。
    """
    return os.path.expanduser(
        f"~/.cache/huggingface/hub/models--Systran--faster-whisper-{model}/snapshots"
    )


def resolve_model_path(model: str = None, base: str = None) -> str:
    """选第一个含 model.bin 的快照子目录(#11 逻辑按模型名泛化)。

    兼容 huggingface_hub 的哈希目录与 download-model.sh 的 `downloaded` 目录,
    新装无需手工改名。base 可注入供单测造布局;找不到抛 RuntimeError,
    错误信息带模型名与下载命令(可行动)。
    """
    if model is None:
        model = model_name(os.environ)
    if base is None:
        base = snapshots_base(model)
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            cand = os.path.join(base, name)
            if os.path.isfile(os.path.join(cand, "model.bin")):
                return cand
    raise RuntimeError(
        f"Whisper model '{model}' not found under {base} — run ./download-model.sh {model}"
    )


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def build_model(engine: str = None, model: str = None,
                whisper_factory=None, warn=None):
    """按引擎构造并返回 WhisperModel(cwd 环境变量决定 engine/model,可显式传参覆盖)。

    whisper_factory / warn 为单测注入缝:工厂未注入时才懒 import faster_whisper,
    使本模块在被 import 时不拉起推理栈(无 GPU 机器可跑单测)。

    分支:
      cuda  构造参数是 HEAD load_model 原文逐字搬移(#16 契约,勿改)
      cpu   device="cpu", compute_type="int8"(无 NVIDIA 机器的兜底引擎)
      auto  先试 cuda,失败 warn 后降级 cpu int8(仅显式选用,默认不做)
      npu   NotImplementedError(#19 实现前 fail fast)
    """
    if engine is None:
        engine = engine_name(os.environ)
    elif engine not in VALID_ENGINES:
        raise ValueError(
            f"[voice-input] unsupported engine={engine!r}; "
            f"valid values: {', '.join(VALID_ENGINES)}"
        )
    if model is None:
        model = model_name(os.environ)
    if whisper_factory is None:
        from faster_whisper import WhisperModel as whisper_factory
    if warn is None:
        warn = _default_warn

    if engine == "npu":
        # 占位在解析模型前就 fail fast:「引擎未实现」与「模型在不在」无关,
        # 先报真实阻断原因(#19 实现后此分支改为懒导入 openvino-genai 构造)。
        raise NotImplementedError("NPU engine not implemented yet — see issue #19")

    path = resolve_model_path(model)

    if engine == "cuda":
        return whisper_factory(path, device="cuda", compute_type="float16")
    if engine == "cpu":
        return whisper_factory(path, device="cpu", compute_type="int8")
    if engine == "auto":
        try:
            return whisper_factory(path, device="cuda", compute_type="float16")
        except Exception as e:
            warn(
                f"[voice-input] {ENV_ENGINE}=auto: cuda load failed ({e}); "
                "falling back to cpu int8"
            )
            return whisper_factory(path, device="cpu", compute_type="int8")
    raise ValueError(  # 兜底:engine 显式传非法值(绕过 engine_name 校验)时
        f"[voice-input] unsupported engine={engine!r}; "
        f"valid values: {', '.join(VALID_ENGINES)}"
    )
