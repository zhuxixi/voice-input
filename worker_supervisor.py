"""Supervisor for the resident transcription worker (#25).

Stdlib-only at the top level: `voice_hold.py` (pinned stdlib-only) imports this
module. The supervisor owns one worker child process: spawn, ready handshake,
request/response, timeouts, kill and throttled restart. Blocking reads happen
outside `self._lock` — the lock only guards short state updates (spec D15).

Design: docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md §4.3
"""

import socket
import subprocess
import sys
import threading
import time

from worker_protocol import ProtocolError, encode_request, parse_line

ENV_RESIDENT = "VOICE_INPUT_RESIDENT"
WORKER_RESTART_MIN_INTERVAL = 1.0   # min seconds between two spawn attempts (D5)
PREWARM_READY_TIMEOUT = 600.0       # ready-wait ceiling; older startup = failed (§4.3)
KILL_WAIT_TIMEOUT = 5.0             # how long _kill() waits for the child to die


def resident_enabled(env: dict) -> bool:
    """Pure: the escape hatch. Exactly "0" disables (#25 D8)."""
    return env.get(ENV_RESIDENT, "1") != "0"
