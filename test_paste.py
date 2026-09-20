import os
import subprocess
import sys
import tempfile
import unittest

import paste

REPO = os.path.dirname(os.path.abspath(__file__))

# Canonical ydotool argv tail for the default combo (spec contract example):
# ctrl=29 shift=42 v=47 — press modifiers L2R, tap key, release reversed.
CTRL_SHIFT_V = ["key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]

# Historical X11 argv, byte-equivalent with HEAD cdb9fbe (spec D4): the old
# voice-toggle.sh line `xdotool type --clearmodifiers --delay 0 "$TEXT"`.
def xdotool_argv(text):
    return ["xdotool", "type", "--clearmodifiers", "--delay", "0", text]


class TestComboToWtypeArgs(unittest.TestCase):
    """A3: combo parsing, press order, release order, whitelist rejection."""

    def test_canonical_combo_full_form(self):
        # spec contract example: modifiers pressed left-to-right, key, then
        # released in reverse order
        self.assertEqual(
            paste.combo_to_ydotool_args("ctrl+shift+v"), CTRL_SHIFT_V
        )

    def test_single_modifier_combo(self):
        # explicit press/release keeps every combo self-contained; same
        # construction as the spec example
        self.assertEqual(
            paste.combo_to_ydotool_args("ctrl+v"),
            ["key", "29:1", "47:1", "47:0", "29:0"],
        )

    def test_all_whitelisted_modifiers_accepted(self):
        mods = list(paste._MOD_KEYCODES)
        combo = "+".join(mods) + "+v"
        expected = ["key"]
        for m in mods:
            expected.append(f"{paste._MOD_KEYCODES[m]}:1")
        expected += ["47:1", "47:0"]
        for m in reversed(mods):
            expected.append(f"{paste._MOD_KEYCODES[m]}:0")
        self.assertEqual(paste.combo_to_ydotool_args(combo), expected)

    def test_invalid_modifier_raises_listing_whitelist(self):
        with self.assertRaises(ValueError) as ctx:
            paste.combo_to_ydotool_args("ctrl+bogus+Insert")
        msg = str(ctx.exception)
        self.assertIn("bogus", msg)
        for m in paste._MOD_KEYCODES:
            self.assertIn(m, msg)

    def test_implausible_key_raises(self):
        # plan A3: "ctrl+bogus" must be rejected — "bogus" is neither a valid
        # modifier nor a plausible key (single char / Capitalized keysym)
        with self.assertRaises(ValueError) as ctx:
            paste.combo_to_ydotool_args("ctrl+bogus")
        self.assertIn("bogus", str(ctx.exception))

    def test_named_keys_accepted(self):
        # keycode table names (layout-independent evdev codes)
        self.assertEqual(
            paste.combo_to_ydotool_args("shift+insert"),
            ["key", "42:1", "110:1", "110:0", "42:0"],
        )
        self.assertEqual(
            paste.combo_to_ydotool_args("v"), ["key", "47:1", "47:0"],
        )

    def test_empty_combo_raises(self):
        for bad in ("", "+", "++"):
            with self.assertRaises(ValueError):
                paste.combo_to_ydotool_args(bad)

    def test_keycode_table_pins(self):
        # pin the load-bearing evdev codes (linux/input-event-codes.h ABI)
        self.assertEqual(paste._MOD_KEYCODES["ctrl"], 29)
        self.assertEqual(paste._MOD_KEYCODES["shift"], 42)
        self.assertEqual(paste._KEY_KEYCODES["v"], 47)
        self.assertEqual(paste._KEY_KEYCODES["insert"], 110)


class TestPasteCommands(unittest.TestCase):
    """A1/A2/A4: command-sequence construction per session type."""

    def test_wayland_paste_sequence(self):  # A1
        cmds = paste.paste_commands("wayland", "paste", "ctrl+shift+v", "你好 hi")
        self.assertEqual(len(cmds), 2)
        # step 1: clipboard write, text via stdin (never via argv)
        self.assertEqual(cmds[0], {"argv": ["wl-copy"], "stdin": "你好 hi"})
        # step 2: simulated paste combo, no stdin
        self.assertEqual(cmds[1]["argv"], ["ydotool"] + CTRL_SHIFT_V)
        self.assertIsNone(cmds[1]["stdin"])

    def test_wayland_custom_combo(self):
        cmds = paste.paste_commands("wayland", "paste", "ctrl+v", "t")
        self.assertEqual(
            cmds[1]["argv"],
            ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
        )

    def test_wayland_type_sequence(self):  # A4
        cmds = paste.paste_commands("wayland", "type", "ctrl+shift+v", "hi")
        self.assertEqual(cmds, [{"argv": ["ydotool", "type", "hi"], "stdin": None}])

    def test_x11_fallthrough_variants(self):  # A2
        # x11 / None / empty / unknown (incl. case variants) all fall back to
        # the historical xdotool argv — method/combo must be ignored there
        # (an invalid combo must NOT raise on the x11 path)
        for st in ("x11", None, "", "weird", "WAYLAND"):
            with self.subTest(session_type=st):
                self.assertEqual(
                    paste.paste_commands(st, "type", "bogus+combo", "t"),
                    [{"argv": xdotool_argv("t"), "stdin": None}],
                )

    def test_x11_argv_byte_equivalent_with_head(self):  # D4 explicit
        # quoting/space-sensitive text survives as a single argv element
        text = 'quote " and  space\nnewline'
        cmds = paste.paste_commands("x11", "paste", "ctrl+shift+v", text)
        self.assertEqual(cmds[0]["argv"], xdotool_argv(text))

    def test_wayland_invalid_method_raises_listing_valid(self):
        with self.assertRaises(ValueError) as ctx:
            paste.paste_commands("wayland", "bogus", "ctrl+v", "t")
        self.assertIn("bogus", str(ctx.exception))
        for m in paste.VALID_METHODS:
            self.assertIn(m, str(ctx.exception))


class TestModulePurity(unittest.TestCase):
    """Module purity: import paste must stay side-effect free (mirrors
    test_engine.py: importable on machines without wl-copy/ydotool/xdotool)."""

    def test_import_in_clean_subprocess(self):
        code = "import paste; print(paste.DEFAULT_METHOD, paste.DEFAULT_COMBO)"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO, capture_output=True,
            env={"PATH": "/usr/bin:/bin"},  # no wl-copy/ydotool/xdotool needed
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("paste ctrl+shift+v", proc.stdout.decode())


class TestCLIIntegration(unittest.TestCase):
    """A5/A6: real CLI via subprocess — dry-run output and failure contract."""

    def _run(self, args, env_extra=None, input_text=b"hi", path=None):
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("XDG_SESSION_TYPE")
            and not k.startswith("VOICE_INPUT_")
        }
        if env_extra:
            env.update(env_extra)
        if path is not None:
            env["PATH"] = path
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "paste.py")] + args,
            input=input_text, capture_output=True, env=env,
        )

    def test_dry_run_wayland_paste_prints_sequence(self):  # A5
        proc = self._run(["--session-type", "wayland", "--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode()
        self.assertIn("wl-copy", out)
        self.assertIn(" ".join(["ydotool"] + CTRL_SHIFT_V), out)

    def test_dry_run_wayland_type(self):
        proc = self._run(
            ["--session-type", "wayland", "--method", "type", "--dry-run"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("ydotool type hi", proc.stdout.decode())

    def test_dry_run_x11_prints_xdotool(self):
        proc = self._run(["--session-type", "x11", "--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("xdotool type --clearmodifiers --delay 0 hi",
                      proc.stdout.decode())

    def test_session_type_from_env(self):
        proc = self._run(["--dry-run"], env_extra={"XDG_SESSION_TYPE": "wayland"})
        self.assertIn("wl-copy", proc.stdout.decode())

    def test_method_knob_from_env(self):
        proc = self._run(
            ["--dry-run"],
            env_extra={
                "XDG_SESSION_TYPE": "wayland",
                "VOICE_INPUT_WAYLAND_METHOD": "type",
            },
        )
        self.assertIn("ydotool type hi", proc.stdout.decode())

    def test_combo_knob_from_env(self):
        proc = self._run(
            ["--dry-run"],
            env_extra={
                "XDG_SESSION_TYPE": "wayland",
                "VOICE_INPUT_PASTE_COMBO": "ctrl+v",
            },
        )
        self.assertIn("ydotool key 29:1 47:1 47:0 29:0", proc.stdout.decode())

    def test_cli_flags_override_env(self):
        proc = self._run(
            ["--session-type", "x11", "--dry-run"],
            env_extra={
                "XDG_SESSION_TYPE": "wayland",
                "VOICE_INPUT_WAYLAND_METHOD": "type",
            },
        )
        self.assertIn("xdotool", proc.stdout.decode())
        self.assertNotIn("wl-copy", proc.stdout.decode())

    def test_missing_tool_exit3_and_actionable_stderr(self):  # A6
        with tempfile.TemporaryDirectory() as empty_path:
            proc = self._run(["--session-type", "wayland"], path=empty_path)
        self.assertEqual(proc.returncode, 3, proc.stdout.decode())
        err = proc.stderr.decode()
        self.assertIn("wl-copy", err)          # names the missing tool
        self.assertIn("pacman -S", err)        # actionable install hint

    def test_invalid_method_env_exit3(self):
        proc = self._run(
            ["--session-type", "wayland", "--dry-run"],
            env_extra={"VOICE_INPUT_WAYLAND_METHOD": "bogus"},
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("bogus", proc.stderr.decode())

    def test_invalid_combo_env_exit3(self):
        proc = self._run(
            ["--session-type", "wayland", "--dry-run"],
            env_extra={"VOICE_INPUT_PASTE_COMBO": "ctrl+bogus"},
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("bogus", proc.stderr.decode())

    def test_positional_text_wins_over_stdin(self):
        # positional text is used even when stdin is also piped
        proc = self._run(
            ["--session-type", "wayland", "--method", "type", "--dry-run", "argv text"],
        )
        self.assertIn("ydotool type argv text", proc.stdout.decode())

    def test_real_execution_feeds_stdin_bytes(self):
        # Live U1 regression: the real wayland+paste path feeds the text to
        # wl-copy via stdin — subprocess requires bytes, a str crashes with
        # TypeError (dry-run and missing-tool tests never execute a real
        # stdio-consuming child, which is why this slipped through CR)
        with tempfile.TemporaryDirectory() as td:
            copy_out = os.path.join(td, "copied.txt")
            ydotool_out = os.path.join(td, "ydotool.txt")
            for name, body in [
                ("wl-copy", f'#!/bin/sh\ncat > "{copy_out}"\n'),
                ("ydotool", f'#!/bin/sh\necho "$@" > "{ydotool_out}"\n'),
            ]:
                p = os.path.join(td, name)
                with open(p, "w") as f:
                    f.write(body)
                os.chmod(p, 0o755)
            proc = self._run(
                ["--session-type", "wayland"],
                path=td + ":/usr/bin:/bin",  # shims first; cat/echo resolvable
                env_extra={"COPY_OUT": copy_out, "YDOTOOL_OUT": ydotool_out},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            with open(copy_out) as f:
                self.assertEqual(f.read(), "hi")      # stdin delivered as bytes
            with open(ydotool_out) as f:
                self.assertIn("47:1", f.read())       # combo followed (v=47)

    def test_usage_error_exits_2_per_repo_convention(self):
        # CR finding 7: usage errors keep the stock argparse exit code 2
        # (repo convention: 2=usage, 3=runtime — transcribe_once / npu-bench)
        proc = self._run(["--bogus-flag"])
        self.assertEqual(proc.returncode, 2)

    def test_dispatch_table_covers_valid_methods(self):
        # CR finding 9: VALID_METHODS must mirror the dispatch table exactly,
        # so an error message can never advertise a value the code rejects
        self.assertEqual(set(paste.VALID_METHODS), set(paste._WAYLAND_DISPATCH))
        for m in paste.VALID_METHODS:
            cmds = paste.paste_commands("wayland", m, "ctrl+shift+v", "t")
            self.assertTrue(cmds)


if __name__ == "__main__":
    unittest.main()
