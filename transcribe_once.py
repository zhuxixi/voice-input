#!/usr/bin/env python3
"""一次性转写 CLI: python transcribe_once.py <wav> → 打印转写文本到 stdout。

test-mic.sh / voice-toggle.sh 的转写入口(#16:收敛原先三处内嵌 CUDA 片段,
引擎选择因此能到达这两个脚本)。引擎与模型经 VOICE_INPUT_ENGINE /
VOICE_INPUT_MODEL 选择,默认 cuda + large-v3(契约见 engine.py 模块注释)。
"""

import os
import subprocess
import sys

# Derive the repo location from this file (#11): works from any clone path.
# realpath matches the shell wrappers' readlink -f, so invoking through a
# symlink (e.g. exposing the script in PATH) still finds the repo.
_REPO_DIR = os.path.dirname(os.path.realpath(__file__))
VENV = os.path.join(_REPO_DIR, "venv")

# Ask the venv's own interpreter where its site-packages are (#11): survives
# in-place venv rebuilds that would leave a stale lib/python3.x dir behind.
# 与 voice-ptt.py 同源的 venv 守卫(#16)。库路径前置自 #19 起收敛到
# engine.py 按引擎计算(cuda/auto -> nvidia pip 三路径,与历史逐字节一致;
# cpu -> 无;npu -> ze 驱动库目录且必须进程启动时已在)。
try:
    _SITE = subprocess.check_output(
        [
            os.path.join(VENV, "bin", "python3"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
except Exception:
    _SITE = ""
if not _SITE or not os.path.isdir(_SITE):
    sys.stderr.write(
        f"[voice-input] venv site-packages not found under {VENV} — "
        "see README Installation\n"
    )
    sys.exit(1)

import engine  # 顶层纯标准库(#16);preamble 计算收敛到 engine 单点(#19 债①)

try:
    _ENG = engine.engine_name(dict(os.environ))
    _LIB_PATHS = engine.required_lib_paths(_ENG, _SITE)
except ValueError as e:
    # 非法引擎值:干净退出不裸 traceback(与 main() 内错误路径同款)
    sys.stderr.write(f"[voice-input] {e}\n")
    sys.exit(1)

# npu 的库路径必须在进程启动时就在 LD_LIBRARY_PATH 里(ld.so 只读一次,
# 进程内改无效——KB A/B 实证);缺失时给可行动报错,不做 re-exec 魔法
if _ENG == "npu" and not engine.has_library_paths(dict(os.environ), _LIB_PATHS):
    sys.stderr.write(
        "[voice-input] NPU engine requires LD_LIBRARY_PATH to contain "
        f"{engine.NPU_LIB_DIR} at process start (ld.so reads it once).\n"
        f"  export LD_LIBRARY_PATH={engine.NPU_LIB_DIR}"
        "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n"
        "  or launch via voice-ptt.sh / voice-toggle.sh / voice_hold "
        "(they inject it).\n"
    )
    sys.exit(1)

# cuda/auto 与 HEAD 输出逐字节一致(A6 钉死);cpu 不设置任何路径(惰性差异,
# spec Design #4);本进程内设置只影响子进程继承与个别 dlopen 场景
os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(
    dict(os.environ), _LIB_PATHS)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: transcribe_once.py <wav>", file=sys.stderr)
        return 2
    wav = sys.argv[1]
    model = engine.build_model()
    segments, info = model.transcribe(wav, language="zh")
    # join/strip 与 voice-ptt.py 转写路径同构
    text = "".join(s.text for s in segments).strip()
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        # 可行动错误(engine 校验/模型缺失等)干净退出,不裸 traceback
        print(f"[voice-input] transcribe failed: {e}", file=sys.stderr)
        sys.exit(1)
