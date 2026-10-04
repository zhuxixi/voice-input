#!/usr/bin/env python3
"""Resident transcription worker for the hold-to-talk daemon (#25).

Spawned by `worker_supervisor` with `--protocol-fd N` and serves newline-delimited
JSON requests (`{"id": N, "wav": path}` -> `{"id": N, "text": "…"}`) on that socket.
The model is built once; `terms.json` is re-read per request so hotword edits keep
taking effect without a restart (spec D9). stdout/stderr stay inherited (journal);
the protocol never shares them.

Exit codes (spec §4.4): 0 EOF/normal, 1 startup failure, 2 bad argv, 3 protocol junk.
"""

import argparse
import os
import socket
import sys
import time

_REPO_DIR = os.path.dirname(os.path.realpath(__file__))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from terms import (DEFAULT_TERMS_PATH, build_prompt, build_transcribe_kwargs,
                   load_terms)
from worker_protocol import (ProtocolError, encode_error, encode_ready, encode_text,
                             parse_line)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="transcribe_worker.py",
                                description="resident worker for voice_hold (#25)")
    p.add_argument("--protocol-fd", type=int, required=True,
                   help="inherited socket fd carrying the line protocol")
    return p.parse_args(argv)


def _preamble(log):
    """Mirror `transcribe_once.py`'s process preamble for **every** engine.

    The site-packages dir comes from this process (the worker *is* the venv
    interpreter), so no extra subprocess is needed. `required_lib_paths` needs it
    for cuda/auto (engine.py:106-116 raises without it); npu/cpu ignore it.
    """
    import sysconfig
    import engine
    site = sysconfig.get_paths()["purelib"]
    try:
        eng = engine.engine_name(dict(os.environ))
        paths = engine.required_lib_paths(eng, site)
    except ValueError as e:
        # invalid engine value: clean exit, no bare traceback (mirrors transcribe_once.py:47-53)
        log(f"[voice-input] {e}")
        return 1
    if eng == "npu" and not engine.has_library_paths(dict(os.environ), paths):
        log("[voice-input] NPU engine requires LD_LIBRARY_PATH to contain "
            f"{engine.NPU_LIB_DIR} at process start (ld.so reads it once).\n"
            f"  export LD_LIBRARY_PATH={engine.NPU_LIB_DIR}"
            "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\n"
            "  or launch via voice_hold (it injects it).")
        return 1
    os.environ["LD_LIBRARY_PATH"] = engine.prepend_library_path(dict(os.environ), paths)
    return 0


def worker_main(conn, *, model_factory=None, terms_loader=load_terms,
                clock=time.monotonic, log=None):
    """Serving loop. Returns 0 on protocol EOF, 3 on protocol junk (spec §4.4)."""
    import engine
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if model_factory is None:
        model_factory = engine.build_model
    t0 = clock()
    model = model_factory()
    eng = _engine_label(engine)
    log(f"[worker] model ready in {clock() - t0:.2f}s (engine={eng})")
    conn.write(encode_ready(os.getpid(), eng, _model_label(engine), clock() - t0))
    conn.flush()

    while True:
        line = conn.readline()
        if not line:                       # parent gone -> exit (no orphan, D11)
            log("[worker] protocol EOF, exiting")
            return 0
        try:
            req = parse_line(line)
        except ProtocolError as e:
            log(f"[worker] protocol error, exiting: {e}")
            return 3
        try:
            rid = req["id"]
            wav = req["wav"]
        except (KeyError, TypeError) as e:
            conn.write(encode_error(req.get("id") if isinstance(req, dict) else None,
                                    f"bad request: {e}"))
            conn.flush()
            continue
        try:
            prompt, extra_kw = None, {}
            try:
                cfg = terms_loader(DEFAULT_TERMS_PATH)
                prompt = build_prompt(cfg.get("terms", []))
                extra_kw = build_transcribe_kwargs(cfg)
            except Exception as te:        # degrade, never block transcription
                log(f"[worker] terms assemble failed, degrade to no-prompt: {te}")
            segments, _info = model.transcribe(wav, language="zh",
                                               initial_prompt=prompt, **extra_kw)
            text = "".join(s.text for s in segments).strip()
            conn.write(encode_text(rid, text))
        except Exception as e:             # worker stays alive (spec §4.4)
            conn.write(encode_error(rid, f"{type(e).__name__}: {e}"))
        conn.flush()


def _engine_label(engine) -> str:
    return os.environ.get(engine.ENV_ENGINE, engine.DEFAULT_ENGINE)


def _model_label(engine) -> str:
    return os.environ.get(engine.ENV_MODEL) or engine.default_model(_engine_label(engine))


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    log = lambda msg: print(msg, file=sys.stderr)      # noqa: E731
    rc = _preamble(log)
    if rc:
        return rc
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                             fileno=args.protocol_fd)
    except OSError as e:
        log(f"[worker] --protocol-fd {args.protocol_fd} is not a socket: {e}")
        return 1
    try:
        return worker_main(conn.makefile("rwb"), log=log)
    except Exception as e:
        log(f"[worker] startup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
