"""Text-delivery (paste) command builder: wayland / x11 single source of truth.

Design: docs/superpowers/specs/2026-09-20-wayland-paste-hotkey-design.md (#18).
Pure stdlib. Command construction is pure (no IO, no environ reads) so unit
tests assert argv directly; environment reads happen only in main().

Safety contract (spec, pinned by test_paste.py): when XDG_SESSION_TYPE is
unset or "x11", the delivered argv must stay byte-equivalent with the
historical HEAD cdb9fbe line `xdotool type --clearmodifiers --delay 0 "$TEXT"`.
Wayland sessions default to the clipboard-paste method (wl-copy + wtype
ctrl+shift+v): immune to fcitx5 preedit capture of ASCII fragments, editor
auto-close doubling and newline-as-Enter. Typing mode stays as a knob.
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
    # combo unused for typing; kept for dispatch signature parity
    return [{"argv": ["wtype", text], "stdin": None}]


def _wayland_paste(combo, text):
    return [
        {"argv": ["wl-copy"], "stdin": text},
        {"argv": ["wtype"] + combo_to_wtype_args(combo), "stdin": None},
    ]


# Single source for the wayland method set (CR finding 9): the dispatch
# table below is the only place methods are wired; VALID_METHODS mirrors it
# for error messages and tests.
_WAYLAND_DISPATCH = {"paste": _wayland_paste, "type": _wayland_type}

# wtype(1) modifier whitelist (man page, verified against wtype 0.4).
WTYPES_MODIFIERS = ("shift", "capslock", "ctrl", "logo", "win", "alt", "altgr")

WAYLAND = "wayland"

# Command = {"argv": list[str], "stdin": str | None} — a single step of the
# delivery sequence; stdin carries the text for clipboard-style commands so
# argv stays free of quoting/length concerns.


def _plausible_key(name: str) -> bool:
    """Plausible final combo component: single ASCII alnum char ("v", "V",
    "5") or an xkb keysym-style Capitalized name ("Insert", "Home").
    Multi-char lowercase words like "bogus" are rejected: they are almost
    certainly a typo'd modifier, and wtype would fail on them at runtime
    anyway — better to fail fast with a whitelist than deep inside xkb.
    """
    if not name:
        return False
    if len(name) == 1:
        return name.isascii() and name.isalnum()
    return name.isascii() and name[0].isupper() and name[1:].isalpha()


def combo_to_wtype_args(combo: str) -> list:
    """Paste combo -> wtype argument list.

    "ctrl+shift+v" -> ["-M","ctrl","-M","shift","-k","v","-m","shift","-m","ctrl"]:
    the last '+'-separated component is the key, the rest are modifiers;
    modifiers are pressed left-to-right and released in reverse (spec contract).
    Unknown modifier or implausible key -> ValueError (fail fast: a typo'd
    knob must not silently no-op the paste). wtype auto-releases modifiers
    on exit, so the explicit trailing releases are redundant but keep the
    sequence self-contained.
    """
    parts = [p for p in combo.split("+") if p]
    if not parts:
        raise ValueError(f"empty paste combo {combo!r}")
    key, mods = parts[-1], parts[:-1]
    for m in mods:
        if m not in WTYPES_MODIFIERS:
            raise ValueError(
                f"invalid modifier {m!r} in paste combo {combo!r}; "
                f"valid modifiers: {', '.join(WTYPES_MODIFIERS)}"
            )
    if not _plausible_key(key):
        raise ValueError(
            f"invalid key {key!r} in paste combo {combo!r}; last component "
            f"must be a key like 'v' or 'Insert', the rest must be modifiers "
            f"from: {', '.join(WTYPES_MODIFIERS)}"
        )
    args = []
    for m in mods:
        args += ["-M", m]
    args += ["-k", key]
    for m in reversed(mods):
        args += ["-m", m]
    return args


def paste_commands(session_type, method, combo, text):
    """Build the ordered command sequence delivering `text` to the focused window.

    wayland + paste -> [wl-copy (text via stdin), wtype <paste combo>]
    wayland + type  -> [wtype text]
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
                f"(pacman -S wl-clipboard wtype, or xdotool for X11)",
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
