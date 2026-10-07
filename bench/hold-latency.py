#!/usr/bin/env python3
"""A/B latency bench for #25: per-dictation spawn vs resident worker round trip.

    venv/bin/python3 bench/hold-latency.py --wav /tmp/eight-seconds.wav --runs 5

Human-measured stage only (no hotkey, no arecord): the spawn path runs the real
`transcribe_once.py`, the resident path spawns `transcribe_worker.py` through
`WorkerSupervisor` and sends N requests. Run it while the daemon is idle; the wav
is user-supplied (never committed — the repo is public).
"""

import argparse
import os
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, REPO)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="spawn vs resident transcribe latency (#25)")
    p.add_argument("--wav", required=True)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--dry-run", action="store_true",
                   help="print what would run; do not touch the model")
    p.add_argument("--resident-only", action="store_true")
    p.add_argument("--spawn-only", action="store_true")
    return p.parse_args(argv)


def spawn_path(wav, runs, env_extra=None):
    import subprocess
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    script = os.path.join(REPO, "transcribe_once.py")
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    times = []
    for _ in range(runs):
        t0 = time.monotonic()
        subprocess.run([venv_py, script, wav], env=env, check=False,
                       stdout=subprocess.DEVNULL)
        times.append(time.monotonic() - t0)
    return times


def resident_path(wav, runs):
    import engine
    from worker_supervisor import WorkerSupervisor
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    sup = WorkerSupervisor([venv_py, os.path.join(REPO, "transcribe_worker.py")],
                           engine.child_env(dict(os.environ)))
    t0 = time.monotonic()
    ready = sup.prewarm()
    ready_s = time.monotonic() - t0
    times = []
    try:
        if not ready:
            raise RuntimeError("worker failed to become ready — see stderr above")
        for _ in range(runs):
            t0 = time.monotonic()
            text, reason = sup.request(wav, timeout=240.0)
            if reason:
                raise RuntimeError(f"request failed: {reason}")
            times.append(time.monotonic() - t0)
    finally:
        sup.shutdown()
    return ready_s, times


def report(label, times):
    print(f"{label}: min={min(times):.2f}s median={statistics.median(times):.2f}s "
          f"max={max(times):.2f}s runs={['%.2f' % t for t in times]}")


def main(argv=None) -> int:
    args = parse_args(argv)
    venv_py = os.path.join(REPO, "venv", "bin", "python3")
    if args.dry_run:
        print("spawn path  :", venv_py, os.path.join(REPO, "transcribe_once.py"),
              args.wav, f"x{args.runs}")
        print("resident path:", venv_py, os.path.join(REPO, "transcribe_worker.py"),
              args.wav, f"x{args.runs} (+ 1 prewarm)")
        return 0
    if not os.path.isfile(args.wav):
        print(f"wav not found: {args.wav}", file=sys.stderr)
        return 2
    if not args.resident_only:
        report("spawn   ", spawn_path(args.wav, args.runs))
    if not args.spawn_only:
        ready_s, times = resident_path(args.wav, args.runs)
        print(f"resident: spawn->ready={ready_s:.2f}s")
        report("resident", times)
    return 0


if __name__ == "__main__":
    sys.exit(main())
