#!/usr/bin/env python3
"""NPU/CPU 转写基准(#17):同一 OpenVINO 模型在 CPU 与 NPU 上跑同一段 wav。

用法:
    python bench/npu-bench.py --model-dir DIR --device {cpu,npu} --wav FILE
                              [--runs 3] [--timeout-s 120] [--max-new-tokens N]

设计见 docs/superpowers/specs/2026-09-19-npu-smoke-benchmark-design.md。
纯度契约:模块顶层不 import openvino(与 engine.py 同款),openvino_genai 只在
run_bench 内懒导入——单测在无模型/无 NPU 机器可跑。LD_LIBRARY_PATH 由脚本
自设(NPU 枚举前置,本机已验证的坑),不依赖调用方环境。

每次 transcribe 用 signal.alarm 包超时:hang 也是数据点(openvino.genai#1965
先例),超时记 any_run_timeout=true 后继续,不卡死会话。
"""

import argparse
import os
import signal
import sys
import time

# OmniBook(Arch + AUR intel-npu-driver-bin)上 OpenVINO 枚举 NPU 的前置:
# ze 驱动库目录须在 LD_LIBRARY_PATH(否则 available_devices 只有 CPU)。
NPU_LIB_DIR = "/usr/lib/x86_64-linux-gnu"


def parse_args(argv: list) -> argparse.Namespace:
    """解析命令行。--device 大小写不敏感,归一化为 "CPU"/"NPU"(非法值 argparse 报错退出 2)。"""
    p = argparse.ArgumentParser(
        prog="npu-bench.py",
        description="whisper-small OpenVINO CPU/NPU 转写基准(#17)",
    )
    p.add_argument("--model-dir", required=True, help="OpenVINO 模型目录(GenAI 兼容)")
    p.add_argument(
        "--device", required=True, help="设备:cpu|npu(大小写不敏感,归一化 CPU/NPU)"
    )
    p.add_argument("--wav", required=True, help="输入 wav(16kHz 左右的语音文件)")
    p.add_argument("--runs", type=int, default=3, help="热跑次数(首转之外),默认 3")
    p.add_argument(
        "--timeout-s", type=int, default=120,
        help="单次 transcribe 超时秒数(防 NPU hang),默认 120",
    )
    p.add_argument(
        "--max-new-tokens", type=int, default=None,
        help="限制生成长度(诊断用,默认不限)",
    )
    ns = p.parse_args(argv)
    dev = ns.device.strip().upper()
    if dev not in ("CPU", "NPU"):
        p.error(f"unsupported --device {ns.device!r} (use cpu or npu)")
    ns.device = dev
    return ns


def set_npu_library_path(env: dict) -> str:
    """在注入的 env dict 上前插 NPU 驱动库目录到 LD_LIBRARY_PATH,返回新值。

    只操作传入的 dict(单测可注入临时 dict,不污染 os.environ);
    幂等:已是前缀时原样返回,不重复拼接。
    """
    old = env.get("LD_LIBRARY_PATH", "")
    if old.startswith(NPU_LIB_DIR):
        return old
    env["LD_LIBRARY_PATH"] = f"{NPU_LIB_DIR}:{old}" if old else NPU_LIB_DIR
    return env["LD_LIBRARY_PATH"]


def format_report(metrics: dict) -> str:
    """metrics dict → 机器可读文本块(spec 契约的 11 个字段,缺一不可)。"""
    return (
        f"device={metrics['device']} model_dir={metrics['model_dir']} wav={metrics['wav']}\n"
        f"model_load_s={metrics['model_load_s']:.3f} "
        f"first_transcribe_s={metrics['first_transcribe_s']:.3f}\n"
        f"warm_runs={metrics['warm_runs']} "
        f"mean_s={metrics['warm_mean_s']:.3f} "
        f"min_s={metrics['warm_min_s']:.3f} "
        f"max_s={metrics['warm_max_s']:.3f}\n"
        f"text_first={metrics['text_first']}\n"
        f"any_run_timeout={'true' if metrics['any_run_timeout'] else 'false'}"
    )


class _RunTimeout(Exception):
    """SIGALRM 处理器抛出:单次 transcribe 超时(hang 数据点)。"""


def _alarm_handler(signum, frame):
    raise _RunTimeout()


def _extract_text(result) -> str:
    """兼容 genai generate 返回值形态(str / 带 .text / .texts 的结果对象)。"""
    if isinstance(result, str):
        return result.strip()
    for attr in ("text", "texts"):
        v = getattr(result, attr, None)
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, list) and v:
            return " ".join(str(x) for x in v).strip()
    return str(result).strip()


def _load_wav(path: str):
    """wav(16kHz mono S16LE) → float32 采样序列(genai generate 的入参形态)。

    numpy 懒导入(纯度契约同 openvino:模块顶层零重依赖)。
    """
    import wave

    import numpy as np

    with wave.open(path) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(
                f"[bench] warn: wav 非 16kHz mono(实际 {w.getframerate()}Hz "
                f"{w.getnchannels()}ch),whisper 内部会重采样,计时含重采样开销",
                file=sys.stderr,
            )
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def run_bench(model_dir: str, device: str, wav: str, runs: int = 3,
              timeout_s: int = 120, max_new_tokens: int = None) -> dict:
    """集成路径:真实加载模型并转写,返回 metrics dict(见 format_report 契约)。

    本函数是唯一碰 openvino 的地方(懒导入);NPU 走静态管线
    ({"STATIC_PIPELINE": True},官方 NPU 要求),CPU 默认动态。
    genai 2026.4 的 generate() 入参是原始音频浮点序列(不吃文件路径),
    language/max_new_tokens 走 kwargs(实测支持)。
    超时的 run 不计入 mean/min/max,any_run_timeout 如实置位。
    """
    set_npu_library_path(os.environ)  # NPU 枚举前置(本机已验证的坑)

    from openvino_genai import WhisperPipeline  # 懒导入(纯度契约)

    samples = _load_wav(wav)
    config = {"STATIC_PIPELINE": True} if device == "NPU" else {}

    t0 = time.perf_counter()
    pipe = WhisperPipeline(model_dir, device=device, **config)
    model_load_s = time.perf_counter() - t0

    def transcribe_once():
        kwargs = {"language": "zh"}
        if max_new_tokens is not None:
            kwargs["max_new_tokens"] = max_new_tokens
        return _extract_text(pipe.generate(samples, **kwargs))

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    try:
        # 首转(含 NPU 静态编译,可能显著偏慢,单独计)
        signal.alarm(timeout_s)
        t0 = time.perf_counter()
        first_timeout = False
        try:
            text_first = transcribe_once()
            first_transcribe_s = time.perf_counter() - t0
        except _RunTimeout:
            first_timeout = True
            text_first, first_transcribe_s = "", float(timeout_s)
        finally:
            signal.alarm(0)

        any_timeout = first_timeout
        times = []
        for _ in range(runs):
            signal.alarm(timeout_s)
            t0 = time.perf_counter()
            try:
                transcribe_once()
                times.append(time.perf_counter() - t0)
            except _RunTimeout:
                any_timeout = True
            finally:
                signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, old_handler)

    return {
        "device": device,
        "model_dir": model_dir,
        "wav": wav,
        "model_load_s": model_load_s,
        "first_transcribe_s": first_transcribe_s,
        "warm_runs": len(times),
        "warm_mean_s": sum(times) / len(times) if times else float("nan"),
        "warm_min_s": min(times) if times else float("nan"),
        "warm_max_s": max(times) if times else float("nan"),
        "text_first": text_first,
        "any_run_timeout": any_timeout,
    }


def main(argv: list = None) -> int:
    ns = parse_args(argv if argv is not None else sys.argv[1:])
    if not os.path.isfile(ns.wav):
        print(f"[bench] wav not found: {ns.wav}", file=sys.stderr)
        return 2
    try:
        metrics = run_bench(
            ns.model_dir, ns.device, ns.wav,
            runs=ns.runs, timeout_s=ns.timeout_s,
            max_new_tokens=ns.max_new_tokens,
        )
    except Exception as e:  # 模型/设备级失败:可行动错误 + exit 3(spec 契约)
        print(f"[bench] pipeline failure on {ns.device}: {e}", file=sys.stderr)
        return 3
    print(format_report(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
