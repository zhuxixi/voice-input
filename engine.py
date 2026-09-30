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
from types import SimpleNamespace

# npu 已接入:OpenVINO GenAI 管线(build_model 的 adapter);cuda/cpu/auto 不变。
VALID_ENGINES = ("cuda", "cpu", "auto", "npu")

ENV_ENGINE = "VOICE_INPUT_ENGINE"
ENV_MODEL = "VOICE_INPUT_MODEL"

# 契约默认值:不设环境变量 = 历史 CUDA 行为(#16 spec Safety contract)。
DEFAULT_ENGINE = "cuda"
DEFAULT_MODEL = "large-v3"

# engine=npu 时进程启动前置库目录(ze 驱动库;OmniBook/Arch 本机已验证)
NPU_LIB_DIR = "/usr/lib/x86_64-linux-gnu"

# NPU 编译缓存:stateful+word_timestamps 条目冷 ~155s 一次性,热 ~1.4s 跨进程
# 生效;目录总量 ~2.4GB(含历史静态管线条目,可整目录 rm 重建,代价一次冷编译)
# (2026-09-30 本机实测,#27)
NPU_COMPILE_CACHE = os.path.expanduser("~/.cache/voice-input/npu-compile-cache")


def npu_pipeline_kwargs() -> dict:
    """npu 构造参数单一定义点(#27):stateful 管线 + word_timestamps=True。

    不含 STATIC_PIPELINE:NPU 默认即 stateful;显式 True 会走带断言的静态
    管线,硬拒 initial_prompt/hotwords。word_timestamps 必须构造期传入:
    它决定 decoder 的 SDPA 分解与输入形状(pipeline.cpp:98-114),构造后
    在 generate() 传无效且会崩(连空串都崩,#27 调研 R3)。
    """
    return {
        "NPU_PLATFORM": "NPU4000",
        "word_timestamps": True,
        "CACHE_DIR": NPU_COMPILE_CACHE,
    }


def model_name(env: dict) -> str:
    """VOICE_INPUT_MODEL -> 模型名,默认 "large-v3"。

    env 显式传 dict(不隐式读全局 os.environ),单测可构造变体而不污染进程环境。
    """
    return env.get(ENV_MODEL, DEFAULT_MODEL)


def _validated_engine(value: str) -> str:
    """引擎名单点校验:engine_name / build_model / construction_kwargs 共用。

    fail fast 且列出全部合法值:静默降级会把「配置坏了」伪装成「变慢了」,难排查。
    """
    if value not in VALID_ENGINES:
        raise ValueError(
            f"[voice-input] unsupported engine {value!r}; "
            f"valid values: {', '.join(VALID_ENGINES)}"
        )
    return value


def engine_name(env: dict) -> str:
    """VOICE_INPUT_ENGINE -> 引擎名,默认 "cuda";非法值 ValueError。"""
    return _validated_engine(env.get(ENV_ENGINE, DEFAULT_ENGINE))


def construction_kwargs(engine: str) -> dict:
    """引擎名 -> WhisperModel 构造参数(单一定义点,build_model 与 download-model.py 共用)。

    cuda 分支是 HEAD load_model 原文逐字搬移(#16 契约,勿改);auto 的首次尝试
    与 cuda 同参;npu 无 faster-whisper 侧构造参数(OpenVINO 管线走 build_model)。
    """
    _validated_engine(engine)
    if engine in ("cuda", "auto"):  # auto 首次尝试即 cuda 参数(float16)
        return {"device": "cuda", "compute_type": "float16"}
    if engine == "cpu":
        return {"device": "cpu", "compute_type": "int8"}
    # npu 没有 faster-whisper 侧构造参数(OpenVINO 管线走 build_model 的 adapter)——
    # "不适用"而非"未实现"(#19)
    raise ValueError(
        "[voice-input] npu engine has no faster-whisper construction kwargs; "
        "use build_model() (OpenVINO pipeline)"
    )


def snapshots_base(model: str) -> str:
    """模型名对应的 HF hub snapshots 目录(仅拼路径,不检查存在性)。

    与历史 HEAD 的 SNAPSHOTS_DIR 常量同构:large-v3 时逐字相等(A1 契约测试钉死)。
    """
    return os.path.expanduser(
        f"~/.cache/huggingface/hub/models--Systran--faster-whisper-{model}/snapshots"
    )


def required_lib_paths(engine: str, site_packages: str = None) -> list:
    """引擎 -> 进程启动前置库路径列表(#19 收敛 5+1 份拷贝的单点)。

    cuda/auto: venv nvidia pip 库三件套(与历史拷贝逐字同序,A4 钉死);
    npu: ze 驱动库目录(ld.so 只在进程启动读 LD_LIBRARY_PATH);
    cpu: 无。site_packages 为 None 时 cuda/auto 抛 ValueError(宁可报错不静默算错)。
    """
    _validated_engine(engine)
    if engine in ("cuda", "auto"):
        if not site_packages:
            raise ValueError(
                "[voice-input] required_lib_paths: cuda/auto requires site_packages "
                "(venv site-packages dir)"
            )
        return [
            f"{site_packages}/nvidia/cublas/lib",
            f"{site_packages}/nvidia/cudnn/lib",
            f"{site_packages}/nvidia/cuda_nvrtc/lib",
        ]
    if engine == "npu":
        return [NPU_LIB_DIR]
    return []


def has_library_paths(env: dict, paths: list) -> bool:
    """paths 是否已全部在 env 的 LD_LIBRARY_PATH 中(分量精确匹配)。

    精确分量匹配防兄弟目录误判(/usr/lib/x86_64-linux-gnu-extras 不算包含)。
    """
    present = [p for p in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    return all(p in present for p in paths)


def prepend_library_path(env: dict, paths: list) -> str:
    """在传入 env dict 上前置 paths 到 LD_LIBRARY_PATH,返回新值。幂等。

    只操作传入 dict(不写 os.environ,可测)。已含分量不重复拼——HEAD 会重复
    追加,去重对 ld.so 语义等价(spec Design #4 惰性差异声明)。
    """
    old = env.get("LD_LIBRARY_PATH", "")
    missing = [p for p in paths if not has_library_paths(env, [p])]
    if not missing:
        env["LD_LIBRARY_PATH"] = old
        return old
    prefix = os.pathsep.join(missing)
    env["LD_LIBRARY_PATH"] = f"{prefix}{os.pathsep}{old}" if old else prefix
    return env["LD_LIBRARY_PATH"]


def default_model(engine: str) -> str:
    """引擎感知的默认模型名单点:npu -> small-int8-ov(OpenVINO 格式),其他 -> large-v3。

    build_model 与 shell 预检共用;不设 VOICE_INPUT_MODEL 时 npu 不会错找
    不存在的 OpenVINO/whisper-large-v3。#16 契约(cuda 默认 large-v3)不动。
    """
    _validated_engine(engine)
    return "small-int8-ov" if engine == "npu" else DEFAULT_MODEL


def ov_snapshots_base(model: str) -> str:
    """OpenVINO 模型的 HF hub snapshots 目录(仅拼路径,不检查存在性)。"""
    return os.path.expanduser(
        f"~/.cache/huggingface/hub/models--OpenVINO--whisper-{model}/snapshots"
    )


def child_env(env: dict) -> dict:
    """spawn transcribe 子进程用的 env(voice_hold 唯一注入点)。

    npu 时 prepend NPU 库目录(ld.so 进程启动前置,进程内改无效——KB 实证);
    其他引擎/非法引擎原样拷贝放行,由 transcribe_once 的进程内 preamble 与
    可行动报错维持现状错误路径。
    """
    out = dict(env)
    try:
        eng = engine_name(out)
    except ValueError:
        return out
    if eng == "npu":
        out["LD_LIBRARY_PATH"] = prepend_library_path(out, required_lib_paths("npu"))
    return out


# 布局谓词:快照子目录含此文件才算对应格式的有效模型
_LAYOUT_PREDICATE = {"ct2": "model.bin", "ov": "openvino_encoder_model.xml"}


def resolve_model_path(model: str = None, base: str = None, layout: str = "ct2") -> str:
    """选第一个含布局谓词文件的快照子目录(#11 逻辑按模型名+格式泛化)。

    layout="ct2"(默认):谓词 model.bin,与历史行为逐字一致(A5 契约);
    layout="ov":谓词 openvino_encoder_model.xml(OpenVINO/GenAI 布局)。
    base 可注入供单测造布局;找不到抛 RuntimeError,错误信息按布局区分
    下载命令(ct2 -> download-model.sh;ov -> huggingface-cli)。
    """
    if layout not in _LAYOUT_PREDICATE:
        raise ValueError(f"[voice-input] unknown model layout {layout!r}")
    predicate = _LAYOUT_PREDICATE[layout]
    if model is None:
        model = model_name(os.environ)
    if base is None:
        base = ov_snapshots_base(model) if layout == "ov" else snapshots_base(model)
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            cand = os.path.join(base, name)
            if os.path.isfile(os.path.join(cand, predicate)):
                return cand
    hint = (f"./download-model.sh {model}" if layout == "ct2"
            else f"huggingface-cli download OpenVINO/whisper-{model}")
    raise RuntimeError(
        f"Whisper model '{model}' ({layout}) not found under {base} — run {hint}"
    )


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def build_model(engine: str = None, model: str = None,
                whisper_factory=None, warn=None,
                pipeline_factory=None, model_base: str = None):
    """按引擎构造并返回转写模型(cwd 环境变量决定 engine/model,可显式传参覆盖)。

    whisper_factory / pipeline_factory / warn / model_base 为单测注入缝:
    工厂未注入时才懒 import 对应推理栈,本模块被 import 时不拉起任何重依赖。
    model_base 注入快照根目录(单测造布局);None 时行为与历史逐字一致。

    分支:
      cuda  构造参数是 HEAD load_model 原文逐字搬移(#16 契约,勿改)
      cpu   device="cpu", compute_type="int8"(无 NVIDIA 机器的兜底引擎)
      auto  先试 cuda,失败 warn 后降级 cpu int8(仅显式选用,默认不做)
      npu   _NpuWhisperAdapter(openvino_genai WhisperPipeline,stateful+word_timestamps+编译缓存)
    """
    if engine is None:
        engine = engine_name(os.environ)
    else:
        _validated_engine(engine)
    if model is None:
        model = os.environ.get(ENV_MODEL) or default_model(engine)
    if warn is None:
        warn = _default_warn

    if engine == "npu":
        path = resolve_model_path(model, base=model_base, layout="ov")
        if pipeline_factory is None:
            from openvino_genai import WhisperPipeline as pipeline_factory
        return _NpuWhisperAdapter(path, pipeline_factory)

    if whisper_factory is None:
        from faster_whisper import WhisperModel as whisper_factory

    path = resolve_model_path(model, base=model_base)

    if engine == "auto":
        try:
            return whisper_factory(path, **construction_kwargs("cuda"))
        except Exception as e:
            warn(
                f"[voice-input] {ENV_ENGINE}=auto: cuda load failed ({e}); "
                "falling back to cpu int8"
            )
            return whisper_factory(path, **construction_kwargs("cpu"))
    return whisper_factory(path, **construction_kwargs(engine))


def _load_wav_samples(path: str):
    """wav(16-bit PCM) -> float32 采样序列(genai generate 的入参形态)。

    校验先于 numpy 导入(拒绝路径不依赖重依赖);16-bit 硬校验沿 bench 语义
    (24-bit 会被 int16 解读成垃圾样本且静默成功,必须硬拒)。
    """
    import wave

    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            raise ValueError(
                f"wav 采样宽度 {w.getsampwidth()} 字节 ≠ 2(int16);"
                "先转成 16kHz mono 16-bit(arecord -f S16_LE 即是)"
            )
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(
                f"[voice-input] warn: wav 非 16kHz mono(实际 {w.getframerate()}Hz "
                f"{w.getnchannels()}ch),whisper 内部会重采样",
                file=sys.stderr,
            )
        data = w.readframes(w.getnframes())

    import numpy as np  # 懒导入:模块顶层保持纯标准库

    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class _NpuWhisperAdapter:
    """openvino_genai WhisperPipeline -> faster-whisper WhisperModel 形态适配(#19)。

    暴露同签名 transcribe(),返回 (segments, info)——调用方(voice-ptt.py /
    transcribe_once.py)零改动。#27 起 stateful 管线 + 构造期 word_timestamps=True,
    initial_prompt/hotwords 直通 generate(静态管线的硬拒与降级告警已随
    STATIC_PIPELINE 一并移除)。
    """

    def __init__(self, model_dir: str, pipeline_factory):
        # stateful 管线(NPU 默认)+ 构造期 word_timestamps(#27):决定 decoder
        # SDPA 分解与输入形状,使 initial_prompt/hotwords 可用;CACHE_DIR:
        # 编译跨进程缓存
        self._pipe = pipeline_factory(model_dir, device="NPU",
                                      **npu_pipeline_kwargs())

    def transcribe(self, wav, language="zh", initial_prompt=None, hotwords=None,
                   **_ignored):
        # truthy 判断:None/空串不透传——stateful 管线对任何已设值都会崩(#27 R3)
        gen_kwargs = {}
        if initial_prompt:
            gen_kwargs["initial_prompt"] = initial_prompt
        if hotwords:
            gen_kwargs["hotwords"] = hotwords
        samples = _load_wav_samples(wav)
        result = self._pipe.generate(samples, language=language, **gen_kwargs)
        text = self._extract_text(result)
        segments = [SimpleNamespace(text=text)] if text else []
        return segments, SimpleNamespace(language=language)

    @staticmethod
    def _extract_text(result) -> str:
        """兼容 generate 返回值形态(str / .text / .texts 列表)。沿 bench 语义。"""
        if isinstance(result, str):
            return result.strip()
        for attr in ("text", "texts"):
            v = getattr(result, attr, None)
            if isinstance(v, str):
                return v.strip()
            if isinstance(v, list):
                return " ".join(str(x) for x in v).strip()
        return str(result).strip()
