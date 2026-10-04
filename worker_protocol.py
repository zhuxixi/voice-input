"""Line-delimited JSON protocol between the hold daemon and its resident worker (#25).

Stdlib-only by contract: `voice_hold.py` (pinned stdlib-only by TestModulePurity)
imports this module transitively. One message = one UTF-8 JSON line, `\\n`-terminated,
carried on a dedicated socketpair fd (never on the worker's stdout — see spec D2/D10).

Design: docs/superpowers/specs/2026-10-01-resident-transcribe-worker-design.md §4.2
"""

import json

PROTOCOL_VERSION = 1
EVENT_READY = "ready"


class ProtocolError(Exception):
    """Peer sent a line that is not a valid protocol message."""


def encode(msg: dict) -> bytes:
    """The single serialization exit (adds the newline)."""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def encode_ready(pid: int, engine: str, model: str, load_s: float) -> bytes:
    return encode({
        "event": EVENT_READY, "protocol": PROTOCOL_VERSION,
        "pid": pid, "engine": engine, "model": model, "load_s": load_s,
    })


def encode_request(rid: int, wav: str) -> bytes:
    return encode({"id": rid, "wav": wav})


def encode_text(rid: int, text: str) -> bytes:
    return encode({"id": rid, "text": text})


def encode_error(rid, error: str) -> bytes:
    return encode({"id": rid, "error": error})


def parse_line(line: bytes) -> dict:
    """bytes -> dict. Bad JSON / non-object / non-int `id` -> ProtocolError.

    Event lines (`event`) carry no `id` and are valid (#25 §4.2 rule 3); a present
    `id` must be a true int or null (null = error line without a request id); bool
    is rejected (int subclass).
    """
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(f"bad JSON: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError(f"not a JSON object: {type(msg).__name__}")
    if "id" in msg and msg["id"] is not None and (
            isinstance(msg["id"], bool) or not isinstance(msg["id"], int)):
        raise ProtocolError(f"id must be int, got {type(msg['id']).__name__}")
    return msg
