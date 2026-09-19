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
    p.add_argument(
        "--npu-platform", default=None,
        help="NPU 平台名(如 NPU4000=Lunar Lake);驱动不自动上报时需显式指定",
    )
    ns = p.parse_args(argv)
    dev = ns.device.strip().upper()
    if dev not in ("CPU", "NPU"):
        p.error(f"unsupported --device {ns.device!r} (use cpu or npu)")
    ns.device = dev
    return ns


def _lib_dir_present(path: str) -> bool:
    """共享谓词:LD_LIBRARY_PATH 是否已含 NPU 库目录(按分量精确匹配)。

    单一定义点,set_npu_library_path 与 needs_reexec_for_npu 共用——避免
    startswith 前缀匹配与 split(':') 分量匹配语义发散(CR 发现 2/9:
    兄弟目录 /usr/lib/x86_64-linux-gnu-extras 会让两者判定相反,re-exec 空转)。
    """
    return NPU_LIB_DIR in [p for p in path.split(os.pathsep) if p]


def set_npu_library_path(env: dict) -> str:
    """在注入的 env dict 上前插 NPU 驱动库目录到 LD_LIBRARY_PATH,返回新值。

    只操作传入的 dict(单测可注入临时 dict,不污染 os.environ);
    幂等:已含(分量精确匹配)时原样返回,不重复拼接。
    """
    old = env.get("LD_LIBRARY_PATH", "")
    if _lib_dir_present(old):
        return old
    env["LD_LIBRARY_PATH"] = f"{NPU_LIB_DIR}{os.pathsep}{old}" if old else NPU_LIB_DIR
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
    """保留旧名以兼容文档叙述;当前 handler 不再抛异常(CR 发现 1 修正)。"""


# alarm 到点只置标志不抛异常:异常会在字节码边界任意处炸掉、丢失迟到完成的
# 结果;标志语义下耗时与文本永远真实,真死锁由 main() 的 fork 看门狗兑底。
_overran = {"flag": False}


def _alarm_handler(signum, frame):
    _overran["flag"] = True


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

    校验全部在 numpy 导入之前完成(zima CR round1 发现 1:拒绝路径
    不得依赖重依赖,否则无 numpy 机器上单测直接 ModuleNotFoundError,
    破坏「测试仅标准库」声明);numpy 仅在真正需要转换时才导入。
    """
    import wave

    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            # CR 发现 4:24-bit PCM 会被 int16 解读成垃圾样本且 exit 0,
            # 垃圾文本归档成数据——采样宽度必须硬校验,不能只 warn
            raise ValueError(
                f"wav 采样宽度 {w.getsampwidth()} 字节 ≠ 2(int16);"
                f"先转成 16kHz mono 16-bit(arecord -f S16_LE 即是)"
            )
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(
                f"[bench] warn: wav 非 16kHz mono(实际 {w.getframerate()}Hz "
                f"{w.getnchannels()}ch),whisper 内部会重采样,计时含重采样开销",
                file=sys.stderr,
            )
        data = w.readframes(w.getnframes())

    import numpy as np

    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def needs_reexec_for_npu(env: dict) -> bool:
    """True 当 env 缺 NPU 库路径且未带 reexec 哨兵(纯函数,单测覆盖)。

    背景:A/B 实测(2026-09-19)进程内改 os.environ['LD_LIBRARY_PATH'] 对 ld.so 无效
    (动态链接器只在进程启动读一次)——NPU 枚举要求启动时已含驱动库目录,
    缺失时由 main() 带正确 env 重新 exec 自身一次。
    """
    if env.get("_NPU_BENCH_REEXEC"):
        return False
    return not _lib_dir_present(env.get("LD_LIBRARY_PATH", ""))


def reexec_command(argv: list, script_path: str) -> list:
    """构造 re-exec 的 execve argv(纯函数,单测覆盖)。

    用显式传入的 argv(main 的参数优先,而非宿主进程 sys.argv)与脚本绝对路径——
    编程调用 main([...]) 时 sys.argv 属于宿主程序,直接用会 exec 无关工具(CR 发现 3)。
    """
    return [sys.executable, script_path] + list(argv)


def run_bench(model_dir: str, device: str, wav: str, runs: int = 3,
              timeout_s: int = 120, max_new_tokens: int = None,
              npu_platform: str = None) -> dict:
    """集成路径:真实加载模型并转写,返回 metrics dict(见 format_report 契约)。

    本函数是唯一碰 openvino 的地方(懒导入);NPU 走静态管线
    ({"STATIC_PIPELINE": True},官方 NPU 要求),CPU 默认动态。
    genai 2026.4 的 generate() 入参是原始音频浮点序列(不吃文件路径),
    language/max_new_tokens 走 kwargs(实测支持)。
    超时语义(CR 发现 1 修正):SIGALRM 无法中断阻塞中的 C++ generate()
    (Python 信号只在字节码间隙执行),迟到完成的 run 记录真实耗时与真实
    文本、置 any_run_timeout=true(超预算标记),不伪造 first_transcribe_s、
    不丢弃结果;真死锁由 main() 的 fork 看门狗兑底(父进程 kill 子进程)。
    """
    set_npu_library_path(os.environ)  # NPU 枚举前置(本机已验证的坑)

    from openvino_genai import WhisperPipeline  # 懒导入(纯度契约)

    samples = _load_wav(wav)
    config = {"STATIC_PIPELINE": True} if device == "NPU" else {}
    if device == "NPU" and npu_platform:
        # 驱动 1.38.0 仍不向编译器上报平台描述(AUTO_DETECT 失败),需显式指定
        config["NPU_PLATFORM"] = npu_platform

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
        # 首转(含 NPU 静态编译,可能显著偏慢,单独计)。alarm 只置标志:
        # 迟到完成的 run 记真实耗时与真实文本并标记超预算,不丢结果不伪造数字
        _overran["flag"] = False
        signal.alarm(timeout_s)
        t0 = time.perf_counter()
        text_first = transcribe_once()
        first_transcribe_s = time.perf_counter() - t0
        signal.alarm(0)

        any_timeout = _overran["flag"]
        times = []
        for _ in range(runs):
            _overran["flag"] = False
            signal.alarm(timeout_s)
            t0 = time.perf_counter()
            transcribe_once()
            times.append(time.perf_counter() - t0)
            signal.alarm(0)
            any_timeout = any_timeout or _overran["flag"]
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


def _run_with_watchdog(fn, wall_s: float):
    """在子进程里跑 fn(返回 metrics dict),父进程看门狗在 wall_s 后 SIGKILL。

    CR 发现 1:SIGALRM 无法中断阻塞中的 C++ generate()(Python 信号只在字节码
    间隙执行),真死锁只能靠进程级 kill。fork 发生在 openvino 导入之前
    (main 里先 fork 再由子进程懒导入),无 NPU 上下文 fork 风险。父进程侧
    用抛异常的 alarm 处理器打断 waitpid(PEP 475 会重试被 no-op handler
    打断的系统调用,必须抛)。metrics JSON 约几百字节,进 64KB 管道缓冲
    不会阻塞子进程退出;若未来 metrics 变大改 temp 文件回传。返回 None =
    看门狗触发。
    """
    import json as _json

    def _parent_alarm(signum, frame):
        raise _RunTimeout()

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # 子进程:跑完整基准(openvino 在此之后才导入),结果过管道回传
        os.close(r)
        status = 3
        try:
            os.write(w, _json.dumps({"__metrics__": fn()}).encode())
            status = 0
        except BaseException as e:  # 子进程里任何失败都带回错误,不裸炸父进程
            try:
                os.write(w, _json.dumps({"__error__": str(e)}).encode())
            except OSError:
                pass
        finally:
            os.close(w)
            os._exit(status)
    os.close(w)
    old_handler = signal.signal(signal.SIGALRM, _parent_alarm)
    try:
        signal.alarm(int(wall_s))
        _, wait_status = os.waitpid(pid, 0)  # 父进程可被 alarm 打断
        signal.alarm(0)
        data = b""
        while True:
            chunk = os.read(r, 65536)
            if not chunk:
                break
            data += chunk
        os.close(r)
        if not data:
            # 区分子进程死因(zima CR round1 建议 2):非看门狗死(如 OOM 被
            # SIGKILL、段错误)不得误报成 watchdog/deadlock
            if os.WIFSIGNALED(wait_status):
                raise RuntimeError(
                    f"bench child killed by signal {os.WTERMSIG(wait_status)} "
                    "(OOM?); no metrics returned")
            raise RuntimeError(
                f"bench child exited status {os.WEXITSTATUS(wait_status)} "
                "without output")
        payload = _json.loads(data)
        if "__error__" in payload:
            raise RuntimeError(payload["__error__"])
        return payload["__metrics__"]
    except _RunTimeout:
        pass
    finally:
        # 恢复原 handler(zima CR round1 建议 1:不永久污染宿主的 alarm 语义,
        # 编程式复用 main()/嵌入宿主时尤其重要)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(r)
        except OSError:
            pass
    return None  # 看门狗触发:会话存活,数据点记为 watchdog timeout


def main(argv: list = None) -> int:
    ns = parse_args(argv if argv is not None else sys.argv[1:])
    if not os.path.isfile(ns.wav):
        print(f"[bench] wav not found: {ns.wav}", file=sys.stderr)
        return 2
    if ns.device == "NPU" and needs_reexec_for_npu(dict(os.environ)):
        # 带上 NPU 库路径重新 exec(ld.so 只在启动时读 LD_LIBRARY_PATH,
        # 运行中修改无效;哨兵防循环;argv 用调用方显式传入的,防宿主
        # sys.argv 误 exec 无关工具,CR 发现 3)
        env = dict(os.environ)
        set_npu_library_path(env)
        env["_NPU_BENCH_REEXEC"] = "1"
        os.execve(sys.executable, reexec_command(
            argv if argv is not None else sys.argv[1:],
            os.path.abspath(__file__)), env)
    # 看门狗预算:加载(可能含 49s NPU 静态编译,给 3 倍超时余量)+ (runs+1) 次
    # transcribe + 60s 常数;真死锁在此预算后被 SIGKILL,会话不卡死
    wall = 3 * ns.timeout_s + (ns.runs + 1) * ns.timeout_s + 60
    try:
        metrics = _run_with_watchdog(
            lambda: run_bench(
                ns.model_dir, ns.device, ns.wav,
                runs=ns.runs, timeout_s=ns.timeout_s,
                max_new_tokens=ns.max_new_tokens, npu_platform=ns.npu_platform),
            wall)
    except Exception as e:  # 模型/设备级失败:可行动错误 + exit 3(spec 契约)
        print(f"[bench] pipeline failure on {ns.device}: {e}", file=sys.stderr)
        return 3
    if metrics is None:
        print(
            f"[bench] watchdog fired after {wall}s (hard kill) — "
            "transcribe likely deadlocked; see --timeout-s", file=sys.stderr)
        return 3
    print(format_report(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
