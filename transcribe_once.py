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
# 与 voice-ptt.py 同源的守卫 + nvidia 库路径块(#16 契约:cuda 机器直呼本 CLI 也
# 能找到 cublas/cudnn;无 nvidia 库的机器这些路径不存在,LD_LIBRARY_PATH 里
# 挂着无害)。
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
os.environ["LD_LIBRARY_PATH"] = (
    f"{_SITE}/nvidia/cublas/lib:"
    f"{_SITE}/nvidia/cudnn/lib:"
    f"{_SITE}/nvidia/cuda_nvrtc/lib"
    + (f':{os.environ.get("LD_LIBRARY_PATH", "")}')
)

import engine


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
