#!/usr/bin/env python3
"""一次性转写 CLI: python transcribe_once.py <wav> → 打印转写文本到 stdout。

test-mic.sh / voice-toggle.sh 的转写入口(#16:收敛原先三处内嵌 CUDA 片段,
引擎选择因此能到达这两个脚本)。引擎与模型经 VOICE_INPUT_ENGINE /
VOICE_INPUT_MODEL 选择,默认 cuda + large-v3(契约见 engine.py 模块注释)。
词表热词(#27): main() 组装 terms.initial_prompt/hotwords,所有引擎统一生效,组装失败降级为无提示转写。
确定性纠错(#33): terms.json 的 corrections 在 join 后单趟应用到 stdout 文本。
归档(#44): 与 hold 链路同一套 archive.archive_result —— 原始 wav + raw/final + VAD 决策。
"""

import os
import subprocess
import sys

# 顶层 import 一律 stdlib-only(#16/#19)，import 期不再有任何副作用：
# venv 探测 / 引擎校验 / LD_LIBRARY_PATH 注入都收进 _preamble()，由 main() 调用
# (#44 D10)，这样模块可被测试 import 并直接驱动 main()。
import archive
import engine
import vad_trim
from terms import (DEFAULT_TERMS_PATH, apply_corrections, build_prompt,
                   build_transcribe_kwargs, load_terms)

# Derive the repo location from this file (#11): works from any clone path.
# realpath matches the shell wrappers' readlink -f, so invoking through a
# symlink (e.g. exposing the script in PATH) still finds the repo.
_REPO_DIR = os.path.dirname(os.path.realpath(__file__))
VENV = os.path.join(_REPO_DIR, "venv")


def _preamble() -> int:
    """venv site-packages probe + engine validation + LD_LIBRARY_PATH injection.

    Ask the venv's own interpreter where its site-packages are (#11): survives
    in-place venv rebuilds that would leave a stale lib/python3.x dir behind.
    库路径前置自 #19 起收敛到 engine.py 按引擎计算(cuda/auto -> nvidia pip
    三路径,与历史逐字节一致; cpu -> 无; npu -> ze 驱动库目录且必须进程启动时已在)。

    Returns 0 on success, or an exit code after writing an actionable message.
    """
    try:
        site = subprocess.check_output(
            [
                os.path.join(VENV, "bin", "python3"),
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        site = ""
    if not site or not os.path.isdir(site):
        sys.stderr.write(
            f"[voice-input] venv site-packages not found under {VENV} — "
            "see README Installation\n"
        )
        return 1

    try:
        eng = engine.engine_name(dict(os.environ))
        lib_paths = engine.required_lib_paths(eng, site)
    except ValueError as e:
        # 非法引擎值:干净退出不裸 traceback(与 main() 内错误路径同款)
        sys.stderr.write(f"[voice-input] {e}\n")
        return 1

    # npu 的库路径必须在进程启动时就在 LD_LIBRARY_PATH 里(ld.so 只读一次,
    # 进程内改无效——KB A/B 实证);缺失时给可行动报错,不做 re-exec 魔法
    if eng == "npu" and not engine.has_library_paths(dict(os.environ), lib_paths):
        sys.stderr.write(
            "[voice-input] NPU engine requires LD_LIBRARY_PATH to contain "
            f"{engine.NPU_LIB_DIR} at process start (ld.so reads it once).\n"
            f"  export LD_LIBRARY_PATH={engine.NPU_LIB_DIR}"
            "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n"
            "  or launch via voice-ptt.sh / voice-toggle.sh / voice_hold "
            "(they inject it).\n"
        )
        return 1

    # cuda/auto 与 HEAD 输出逐字节一致(A6 钉死);cpu 不设置任何路径(惰性差异,
    # spec Design #4);本进程内设置只影响子进程继承与个别 dlopen 场景
    os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(
        dict(os.environ), lib_paths)
    return 0


def _labels():
    """(engine, model) for the archive index; never raises (#44)."""
    try:
        eng = os.environ.get(engine.ENV_ENGINE, engine.DEFAULT_ENGINE)
        model = os.environ.get(engine.ENV_MODEL) or engine.default_model(eng)
        return eng, model
    except Exception:
        return None, None


def main(argv=None, terms_path=DEFAULT_TERMS_PATH) -> int:
    rc = _preamble()
    if rc:
        return rc
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("usage: transcribe_once.py <wav>", file=sys.stderr)
        return 2
    wav = argv[0]
    # terms 组装独立 try:热词问题降级到无 prompt,绝不阻断转写(与 voice-ptt.py
    # 同构;load_terms/build_* 内部已降级,这里是双保险)(#27)
    try:
        cfg = load_terms(terms_path)
        prompt = build_prompt(cfg.get("terms", []))
        extra_kw = build_transcribe_kwargs(cfg)
        corrections = cfg.get("corrections", {})
    except Exception as te:
        prompt, extra_kw = None, {}
        corrections = {}
        print(f"[voice-input] terms assemble failed, degrade to no-prompt: {te}",
              file=sys.stderr)
    # Module attributes (not from-imported snapshots) so tests can patch them.
    if archive.ARCHIVE_ENABLED:
        try:
            archive.prune_recordings(keep_days=archive.ARCHIVE_KEEP_DAYS)
        except Exception as pe:                      # prune never breaks a run
            print(f"[archive] prune failed (non-fatal): {pe}", file=sys.stderr)
    model = engine.build_model()
    stats = {}
    with vad_trim.trimmed_wav(wav, stats=stats) as src:
        if src is None:
            # #44 D5: an empty transcript still gets archived — a "VAD heard
            # nothing" sample is exactly the negative evidence #43 needs.
            print("[voice-input] vad: no speech, empty transcript", file=sys.stderr)
            text = ""
        else:
            segments, info = model.transcribe(src, language="zh",
                                              initial_prompt=prompt, **extra_kw)
            text = "".join(s.text for s in segments).strip()
    raw = text
    text = apply_corrections(raw, corrections)
    eng_label, model_label = _labels()
    # keep_source=True: `wav` is the caller's file (CLI argument, or a temp path
    # owned — and later deleted — by the hold daemon). Freezing it here would be
    # silent data loss for manual runs.
    archive.archive_result(wav, raw, text, engine=eng_label, model=model_label,
                           vad=stats, keep_source=True)
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
