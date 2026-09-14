"""User binding safety and keyboard-only shortcut editing, no desktop changes."""
import curses
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_shortcuts as keys
from test_ui import app


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "bindings.lua"
        self.original = '-- Other app\no.bind("SUPER + SHIFT + G", "GPU Workspace", "keep-other")\n'
        self.file.write_text(self.original)
        self.path_patch = patch.object(keys, "path", return_value=self.file)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def runtime(self, before=(), *, fail_verify=False):
        def run(args, env, *, input=None):
            if args == ["hyprctl", "-j", "configerrors"]:
                return '[""]'
            if args == ["hyprctl", "-j", "binds"]:
                text = self.file.read_text()
                own = []
                if not fail_verify and keys.TAG in text:
                    chord = keys.configured(text)
                    if chord:
                        parts = chord.split(" + ")
                        own = [{"modmask": sum(keys.MASKS[part] for part in parts[:-1]),
                                "key": parts[-1], "description": keys.DESCRIPTION}]
                return json.dumps(list(before) + own)
            return ""
        return patch.object(keys, "run", side_effect=run)

    def test_safe_chords_and_injection_rejection(self):
        self.assertEqual(keys.normalize("ctrl + super + m"), keys.DEFAULT)
        for value in ('SUPER + M"; os.execute("bad")', "CTRL + M", "SUPER + F25", "SUPER + CTRL + CTRL + M", "SUPER + "):
            with self.assertRaises(keys.ShortcutError):
                keys.normalize(value)

    def test_save_change_and_disable_preserve_other_bindings(self):
        with patch.object(keys, "environment", return_value={}), self.runtime():
            keys.change(keys.DEFAULT)
            self.assertEqual(keys.configured(), keys.DEFAULT)
            keys.change("SUPER + ALT + F12")
            self.assertNotIn(keys.DEFAULT, self.file.read_text())
            self.assertTrue(self.file.read_text().startswith(self.original))
            keys.change(None)
            self.assertIsNone(keys.configured())
            self.assertIn("disabled", self.file.read_text())
            keys.change(None, uninstall=True)
        self.assertEqual(self.file.read_text(), self.original)

    def test_conflict_changes_nothing(self):
        conflict = {"modmask": 68, "key": "M", "description": "Another app"}
        with patch.object(keys, "environment", return_value={}), self.runtime([conflict]):
            with self.assertRaisesRegex(keys.ShortcutError, "already used by Another app"):
                keys.change(keys.DEFAULT)
        self.assertEqual(self.file.read_text(), self.original)

    def test_numbered_lua_binding_is_a_conflict(self):
        row = {"modmask": 68, "key": "", "keycode": 0, "description": "Bar panel 1"}
        self.assertEqual(keys.matching("SUPER + CTRL + 1", [row], "SUPER CTRL + 1 → Bar panel 1"), [row])
        self.assertEqual(keys.matching(keys.DEFAULT, [row], "SUPER CTRL + 1 → Bar panel 1"), [])
        with self.assertRaises(keys.ShortcutError):
            keys.matching(keys.DEFAULT, [row], "")

    def test_failed_live_verification_restores_original_file(self):
        with patch.object(keys, "environment", return_value={}), self.runtime(fail_verify=True):
            with self.assertRaisesRegex(keys.ShortcutError, "Previous bindings were restored"):
                keys.change(keys.DEFAULT)
        self.assertEqual(self.file.read_text(), self.original)

    def test_duplicate_or_reversed_markers_are_never_rewritten(self):
        for text in (keys.START + "\n", keys.END + "\n" + keys.START + "\n", keys.render(keys.DEFAULT) * 2):
            self.file.write_text(text)
            with self.assertRaises(keys.ShortcutError):
                keys.change(keys.DEFAULT)
            self.assertEqual(self.file.read_text(), text)

    def test_manually_edited_owned_block_is_preserved(self):
        text = keys.render(keys.DEFAULT).replace("shell toggle nebius", "shell toggle other")
        self.file.write_text(text)
        with self.assertRaisesRegex(keys.ShortcutError, "edited manually"):
            keys.change(keys.DEFAULT)
        self.assertEqual(self.file.read_text(), text)

    def test_symlink_is_not_followed(self):
        link = self.file.with_name("link.lua")
        link.symlink_to(self.file)
        with patch.object(keys, "path", return_value=link):
            with self.assertRaises(keys.ShortcutError):
                keys.change(keys.DEFAULT)
        self.assertEqual(self.file.read_text(), self.original)

    def test_disabled_choice_is_not_reenabled_by_setup(self):
        self.file.write_text(keys.render(None))
        with patch.object(sys, "argv", ["shortcuts", "ensure-default"]), patch.object(keys, "save") as save:
            self.assertEqual(keys.main(), 0)
            save.assert_not_called()

    def test_uninstall_ignores_unrelated_dotfile_symlink(self):
        link = self.file.with_name("link.lua")
        link.symlink_to(self.file)
        with patch.object(keys, "path", return_value=link), patch.object(keys, "environment") as env:
            keys.change(None, uninstall=True)
            env.assert_not_called()
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.file.read_text(), self.original)

    def test_syntax_failure_leaves_original_file(self):
        with patch.object(keys, "environment", return_value={}), patch.object(keys, "config_ok"), \
             patch.object(keys, "live", return_value=[]), patch.object(keys, "run", side_effect=["", keys.ShortcutError("Bad Lua")]):
            with self.assertRaisesRegex(keys.ShortcutError, "Bad Lua"):
                keys.change(keys.DEFAULT)
        self.assertEqual(self.file.read_text(), self.original)

    def test_concurrent_user_edit_is_not_overwritten(self):
        def change_during_validation(args, env, *, input=None):
            if args[0] == "luac":
                self.file.write_text(self.original + "-- User just edited this\n")
            return ""
        with patch.object(keys, "environment", return_value={}), patch.object(keys, "config_ok"), \
             patch.object(keys, "live", return_value=[]), patch.object(keys, "run", side_effect=change_during_validation):
            with self.assertRaisesRegex(keys.ShortcutError, "changed while saving"):
                keys.change(keys.DEFAULT)
        self.assertIn("User just edited this", self.file.read_text())


class ShortcutFormTests(unittest.TestCase):
    def test_fresh_disable_records_opt_out_and_reports_it(self):
        application, _ = app([])
        from nebius_ui import Back
        with patch.object(keys, "configured", return_value=None), \
             patch.object(application, "menu", side_effect=["disable", Back()]) as menu, \
             patch.object(application, "confirm_launch", return_value=True) as confirm, \
             patch.object(keys, "save", return_value="Shortcut is disabled") as save, \
             patch.object(application, "message") as message:
            with self.assertRaises(Back):
                application.keyboard_shortcuts()
            save.assert_called_once_with(None)
            confirm.assert_called_once()
            message.assert_called_once_with("Shortcut saved", "Shortcut is disabled")
            self.assertIn("choose a key", menu.call_args_list[0].args[1][0][1])
            self.assertIn("Keep shortcut disabled", menu.call_args_list[0].args[1][1][0])

    def test_form_saves_edited_key_and_arrow_selected_modifiers(self):
        application, screen = app(["g", "\t", curses.KEY_RIGHT, "\n"], 48, 20)
        with patch.object(keys, "save", return_value="Saved") as save:
            self.assertEqual(application.shortcut_form(), "Saved")
        save.assert_called_once_with("SUPER + SHIFT + G")
        self.assertIn("Super+Shift+G", "\n".join(screen.frames))

    def test_escape_does_not_write(self):
        application, _ = app(["z", "\x1b"])
        with patch.object(keys, "save") as save:
            from nebius_ui import Back
            with self.assertRaises(Back):
                application.shortcut_form()
            save.assert_not_called()

    def test_conflict_is_visible_and_does_not_exit_form(self):
        application, screen = app(["\n", "\x1b"], 48, 20)
        from nebius_ui import Back
        with patch.object(keys, "save", side_effect=keys.ShortcutError("Key already used by another app. Choose another.")):
            with self.assertRaises(Back):
                application.shortcut_form()
        self.assertIn("already used", screen.frames[-1])
        self.assertIn("F2", screen.frames[-1])


if __name__ == "__main__":
    unittest.main()
