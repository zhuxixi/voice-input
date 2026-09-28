"""Text-delivery (paste) command builder: wayland / x11 single source of truth.

Design: docs/superpowers/specs/2026-09-20-wayland-paste-hotkey-design.md (#18).
Pure stdlib. Command construction is pure (no IO, no environ reads) so unit
tests assert argv directly; environment reads happen only in main().

Safety contract (spec, pinned by test_paste.py): when XDG_SESSION_TYPE is
unset or "x11", the delivered argv must stay byte-equivalent with the
historical HEAD cdb9fbe line `xdotool type --clearmodifiers --delay 0 "$TEXT"`.
Wayland sessions default to the clipboard-paste method (wl-copy + ydotool
pressing the paste combo): immune to fcitx5 preedit capture of ASCII
fragments, editor auto-close doubling and newline-as-Enter. ydotool replaces
wtype because KWin (and Mutter) do not implement zwp_virtual_keyboard_v1
(upstream wishlist bug 502882) — kernel-level uinput injection works on every
Wayland compositor, X11 and the TTY alike. Typing mode stays as a knob.
"""

import argparse
import os
import subprocess
import sys

ENV_SESSION = "XDG_SESSION_TYPE"
ENV_METHOD = "VOICE_INPUT_WAYLAND_METHOD"
ENV_COMBO = "VOICE_INPUT_PASTE_COMBO"

# Knob defaults (spec D8). paste = clipboard paste; type = simulated typing.
DEFAULT_METHOD = "paste"
DEFAULT_COMBO = "ctrl+shift+v"

VALID_METHODS = ("paste", "type")


def _wayland_type(combo, text):
    # combo unused for typing; kept for dispatch signature parity.
    # CR finding 3: ydotool types through a keyboard mapping — CJK characters
    # cannot be emitted at all and would be silently dropped; fail fast instead
    if not text.isascii():
        raise ValueError(
            "wayland type method supports ASCII only (ydotool types via a "
            "keymap; CJK would be silently dropped) — keep the default "
            "paste method for Chinese text"
        )
    return [{"argv": ["ydotool", "type", text], "stdin": None}]


def _wayland_paste(combo, text):
    return [
        {"argv": ["wl-copy"], "stdin": text},
        {"argv": ["ydotool"] + combo_to_ydotool_args(combo), "stdin": None},
    ]


# Single source for the wayland method set (CR finding 9): the dispatch
# table below is the only place methods are wired; VALID_METHODS mirrors it
# for error messages and tests.
_WAYLAND_DISPATCH = {"paste": _wayland_paste, "type": _wayland_type}

# evdev keycodes (linux/input-event-codes.h — stable kernel ABI), used to
# translate the semantic paste combo into ydotool key events.
KEY_RIGHTALT = 100  # shared with voice_hold.py (single source, CR finding 9)
_MOD_KEYCODES = {
    "ctrl": 29, "shift": 42, "alt": 56, "altgr": KEY_RIGHTALT,
    "logo": 125, "win": 125, "capslock": 58,
}
_KEY_KEYCODES = {
    # letters (qwerty positions, layout-independent at the evdev level)
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11,
    "insert": 110, "delete": 111, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "up": 103, "down": 108,
    "left": 105, "right": 106, "enter": 28, "return": 28, "space": 57,
    "tab": 15, "esc": 1, "escape": 1, "backspace": 14,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
}

WAYLAND = "wayland"

# Command = {"argv": list[str], "stdin": str | None} — a single step of the
# delivery sequence; stdin carries the text for clipboard-style commands so
# argv stays free of quoting/length concerns.


def combo_to_ydotool_args(combo: str) -> list:
    """Paste combo -> ydotool argument list (numeric keycodes, stable ABI).

    "ctrl+shift+v" -> ["key","29:1","42:1","47:1","47:0","42:0","29:0"]:
    the last '+'-separated component is the key, the rest are modifiers;
    modifiers are pressed left-to-right and released in reverse (spec contract).
    Unknown modifier or key -> ValueError (fail fast: a typo'd knob must not
    silently no-op the paste).
    """
    parts = [p for p in combo.lower().split("+") if p]
    if not parts:
        raise ValueError(f"empty paste combo {combo!r}")
    key, mods = parts[-1], parts[:-1]
    codes = []
    for m in mods:
        if m not in _MOD_KEYCODES:
            raise ValueError(
                f"invalid modifier {m!r} in paste combo {combo!r}; "
                f"valid modifiers: {', '.join(_MOD_KEYCODES)}"
            )
        codes.append(f"{_MOD_KEYCODES[m]}:1")
    kc = _KEY_KEYCODES.get(key, _MOD_KEYCODES.get(key))
    if kc is None:
        raise ValueError(
            f"invalid key {key!r} in paste combo {combo!r}; the last "
            f"component must be a key (a-z, 0-9, insert, enter, f1-f12, ...)"
        )
    codes += [f"{kc}:1", f"{kc}:0"]
    for m in reversed(mods):
        codes.append(f"{_MOD_KEYCODES[m]}:0")
    return ["key"] + codes


def paste_commands(session_type, method, combo, text):
    """Build the ordered command sequence delivering `text` to the focused window.

    wayland + paste -> [wl-copy (text via stdin), ydotool <paste combo>]
    wayland + type  -> [ydotool type text]
    anything else (x11 / None / "" / unknown; method & combo are deliberately
    ignored there) -> xdotool argv byte-equivalent with HEAD cdb9fbe (spec D4):
    ["xdotool", "type", "--clearmodifiers", "--delay", "0", text]
    """
    if session_type == WAYLAND:
        handler = _WAYLAND_DISPATCH.get(method)
        if handler is None:
            raise ValueError(
                f"unsupported wayland paste method {method!r}; "
                f"valid values: {', '.join(VALID_METHODS)}"
            )
        return handler(combo, text)
    return [{
        "argv": ["xdotool", "type", "--clearmodifiers", "--delay", "0", text],
        "stdin": None,
    }]


def _resolve(cli_value, env, var, default):
    """Knob resolution shared by all three knobs (CR finding 10): CLI flag
    wins, then environment variable, then spec default."""
    if cli_value is not None:
        return cli_value
    return env.get(var, default)


def main(argv=None) -> int:
    """CLI entry: read knobs (CLI flags override the environment), deliver text.

    TEXT positional is optional; when omitted the text is read from stdin
    (voice-toggle.sh pipes the transcript in). Exit codes: 0 = delivered
    (or dry-run printed), 3 = invalid knobs or a command failed.
    """
    parser = argparse.ArgumentParser(
        prog="paste.py",
        description="Deliver text to the focused window (wayland paste / x11 typing).",
        # stock usage-error exit code 2 (repo convention: 2=usage, 3=runtime,
        # same as transcribe_once.py / npu-bench.py — CR finding 7)
    )
    parser.add_argument(
        "text", nargs="?",
        help="text to deliver; read from stdin when omitted",
    )
    parser.add_argument(
        "--session-type", default=None,
        help="override session type (default: $XDG_SESSION_TYPE)",
    )
    parser.add_argument(
        "--method", default=None,
        help=f"wayland delivery method (default: ${ENV_METHOD} or {DEFAULT_METHOD})",
    )
    parser.add_argument(
        "--combo", default=None,
        help=f"paste key combo for the paste method (default: ${ENV_COMBO} or {DEFAULT_COMBO})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the command sequence instead of executing it",
    )
    ns = parser.parse_args(argv)

    env = os.environ
    session_type = _resolve(ns.session_type, env, ENV_SESSION, None)
    method = _resolve(ns.method, env, ENV_METHOD, DEFAULT_METHOD)
    combo = _resolve(ns.combo, env, ENV_COMBO, DEFAULT_COMBO)

    text = ns.text if ns.text is not None else sys.stdin.read()

    try:
        commands = paste_commands(session_type, method, combo, text)
    except ValueError as e:
        print(f"[voice-input] paste: {e}", file=sys.stderr)
        return 3

    if ns.dry_run:
        for cmd in commands:
            marker = (
                f" <stdin: {len(cmd['stdin'])} chars>"
                if cmd["stdin"] is not None else ""
            )
            print(" ".join(cmd["argv"]) + marker)
        return 0

    for cmd in commands:
        try:
            # stdin must be bytes for subprocess (str raises TypeError in
            # communicate(); found in live U1 testing — dry-run/missing-tool
            # tests never exercised a real stdio-consuming child)
            proc = subprocess.run(
                cmd["argv"],
                input=cmd["stdin"].encode() if cmd["stdin"] is not None else None,
            )
        except FileNotFoundError as e:
            print(
                f"[voice-input] paste: required tool not found: "
                f"{e.filename or e} — install it "
                f"(pacman -S wl-clipboard ydotool, or xdotool for X11)",
                file=sys.stderr,
            )
            return 3
        if proc.returncode != 0:
            print(
                f"[voice-input] paste: command failed "
                f"(exit {proc.returncode}): {' '.join(cmd['argv'])}",
                file=sys.stderr,
            )
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
