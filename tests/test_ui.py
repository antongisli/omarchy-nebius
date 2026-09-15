"""Keyboard regressions and narrow-terminal content bounds."""

import curses
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_ui as ui


class Screen:
    def __init__(self, keys, width=80, height=24):
        self.keys = iter(keys)
        self.width, self.height = width, height
        self.frames = []
        self.lines = {}
        self.draws = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.lines = {}
        self.draws = []

    def addstr(self, y, x, text, attr=0):
        size = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
        assert 0 <= y < self.height and x + size < self.width, (y, x, text)
        self.draws.append((y, x, text, attr))
        line = self.lines.get(y, "").ljust(x)
        self.lines[y] = line[:x] + text

    def refresh(self):
        self.frames.append("\n".join(self.lines.get(y, "") for y in range(self.height)))

    def get_wch(self):
        return next(self.keys)

    def keypad(self, value):
        pass

    def timeout(self, value):
        pass


def app(keys, width=80, height=24):
    screen = Screen(keys, width, height)
    with patch.object(curses, "has_colors", return_value=False), patch.object(curses, "curs_set"):
        return ui.App(screen), screen


class KeyboardTests(unittest.TestCase):
    def test_labels_details_and_choices_have_distinct_indentation_and_spacing(self):
        application, screen = app(["\n"], 80, 30)
        application.menu("Menu", [("First", "First description", 1), ("Second", "Second description", 2)])
        label = next(draw for draw in screen.draws if "› First" in draw[2])
        detail = next(draw for draw in screen.draws if draw[2] == "First description")
        second = next(draw for draw in screen.draws if draw[2].strip() == "Second [2]")
        self.assertEqual(label[1], 2)
        self.assertEqual(detail[1], 6)
        self.assertEqual(detail[0], label[0] + 1)
        self.assertEqual(second[0], detail[0] + 2)
        self.assertTrue(label[3] & curses.A_REVERSE)
        self.assertEqual(ui.cell_width(label[2]), 75)
        self.assertTrue(second[3] & curses.A_BOLD)

    def test_sections_are_not_selectable_and_end_keeps_last_choice_visible(self):
        rows = [(f"Choice {n}", f"Detail {n}", n, "First group" if n < 4 else "Second group") for n in range(9)]
        application, screen = app([curses.KEY_END, "\n"], 48, 20)
        self.assertEqual(application.menu("Menu", rows), 8)
        self.assertIn("› Choice 8", screen.frames[-1])
        self.assertIn("Detail 8", screen.frames[-1])
        self.assertIn("9 of 9", screen.frames[-1])
        application, screen = app(["j", "\n"])
        self.assertEqual(application.menu("Menu", [("First", "", 1, "Group A"), ("Second", "", 2, "Group B")]), 2)

    def test_narrow_configuration_shows_both_allocations_and_keyboard_controls(self):
        application, screen = app(["\n"], 48, 20)
        detail = "16 vCPU · 200 GiB RAM\nOn-demand: 4 available\nPreemptible: unavailable"
        application.menu("Configuration", [("1× H100 · eu-north1", detail, 1)],
                         notes=["Choose GPU count and region. Your project comes next."],
                         actions={"p": "allocation", "r": "refresh"})
        for text in ["On-demand: 4 available", "Preemptible: unavailable", "[Enter]", "[Esc]", "[P]", "[?]"]:
            self.assertIn(text, screen.frames[-1])

    def test_menu_help_keeps_full_truncated_details_and_does_not_select(self):
        application, screen = app(["?", curses.KEY_NPAGE, curses.KEY_NPAGE, "\n", "\n"], 48, 20)
        rows = [("Choice", "\n".join(f"Detail {n}" for n in range(8)), "selected")]
        self.assertEqual(application.menu("Menu", rows), "selected")
        self.assertIn("Menu help", "\n".join(screen.frames))
        self.assertIn("Detail 7", "\n".join(screen.frames))
        self.assertEqual(len(screen.frames), 5)

    def test_wrapping_preserves_json_indents_and_unicode_cell_bounds(self):
        application, screen = app([])
        self.assertEqual(application.wrap('{\n  "spec": {\n    "state": "RUNNING"\n  }\n}'),
                         ['{', '  "spec": {', '    "state": "RUNNING"', '  }', '}'])
        lines = application.wrap("  " + "机器 " * 20, 16)
        self.assertTrue(all(line.startswith("  ") and ui.cell_width(line) <= 16 for line in lines))

    def test_proposed_name_is_editable_and_preserved(self):
        application, screen = app(["\x15", *"training-project", "\n"])
        self.assertEqual(application.edit("Project name", "gpu-eu-west1", validate=ui.core._safe_name), "training-project")

    def test_escape_does_not_create_or_confirm(self):
        application, screen = app(["\x1b"])
        with self.assertRaises(ui.Back):
            application.menu("Create project", [("Create", "gpu-eu-west1", True)])

    def test_search_and_vim_navigation(self):
        application, screen = app(["/", *"h200", "\n", "j", "\n"])
        result = application.menu("Capacity", [("H100", "Finland", 1), ("H200", "Paris", 2), ("H200", "Kansas", 3)])
        self.assertEqual(result, 3)

    def test_uppercase_jump_shortcut_does_not_conflict_with_j_navigation(self):
        application, screen = app(["J"])
        self.assertEqual(application.menu("Home", [("VM", "", "vm")], actions={"J": "jump"}), "jump")

    def test_allocation_switch_is_available_without_selection(self):
        application, screen = app(["p"])
        self.assertEqual(application.menu("Capacity", [], actions={"p": "toggle"}), "toggle")
        application.toggle_allocation()
        self.assertEqual(application.allocation, "preemptible")

    def test_allocation_switch_is_at_top_and_highlights_only_selected_mode(self):
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            for mode in ("on_demand", "preemptible"):
                with self.subTest(width=width, mode=mode):
                    application, screen = app(["\n"], width, height)
                    application.allocation = mode
                    application.menu("GPU capacity", [("H100", "8 available", "gpu")],
                                     subtitle="Live snapshot", actions={"p": "toggle"})
                    top = screen.frames[-1].splitlines()[3]
                    self.assertIn("[ On-demand | Preemptible ]", top)
                    self.assertIn("[P] switch", top)
                    segments = [draw for draw in screen.draws if draw[0] == 3 and draw[2].strip() in ("On-demand", "Preemptible")]
                    selected = [draw[2].strip() for draw in segments if draw[3] & curses.A_REVERSE]
                    self.assertEqual(selected, [ui.allocation_label(mode)])
                    self.assertIn("Live snapshot", screen.frames[-1])

    def test_p_updates_capacity_for_both_modes_without_a_cloud_request(self):
        for launch in (False, True):
            application, screen = app(["P", "p", "q"], 48, 20)
            application.capacity = {"source": "live", "offerings": [{
                "gpu_label": "H100", "gpu_count": 1, "region": "eu-north1",
                "on_demand": {"available": 8}, "preemptible": {"available": 0},
            }]}
            with patch.object(application, "read", side_effect=AssertionError("P must reuse the same capacity snapshot")), \
                 patch.object(application, "mutate", side_effect=AssertionError("Switching must not mutate resources")):
                application.capacity_flow(launch=launch)
            self.assertIn("8 available", screen.frames[0])
            self.assertIn("unavailable", screen.frames[1])
            self.assertIn("8 available", screen.frames[2])
            self.assertEqual(application.allocation, "on_demand")

    def test_p_still_types_into_search_instead_of_switching_mode(self):
        application, screen = app(["/", "p", "\n", "\n"], 48, 20)
        self.assertEqual(application.menu("Capacity", [("GPU", "", "gpu")], actions={"p": "toggle"}), "gpu")
        self.assertIn("Search: p", screen.frames[-1])
        self.assertEqual(application.allocation, "on_demand")

    def test_allocation_switch_is_absent_from_unrelated_menus(self):
        application, screen = app(["\n"])
        application.menu("Your VMs", [("VM", "Running", "vm")])
        self.assertNotIn("Preemptible", screen.frames[-1])

    def test_kubernetes_visibility_setting_is_keyboard_first_and_defaults_hidden(self):
        application, screen = app(["i", "\x1b"], 80, 24)
        with tempfile.TemporaryDirectory() as state, \
             patch.object(ui.core, "PREFERENCES_FILE", Path(state) / "preferences.json"), \
             self.assertRaises(ui.Back):
            application.preferences()
        rendered = "\n".join(screen.frames)
        self.assertIn("Kubernetes nodes", rendered)
        self.assertIn("HIDDEN", screen.frames[0])
        self.assertIn("SHOWN", screen.frames[-1])
        self.assertIn("[I]", screen.frames[-1])

    def test_long_rows_and_wide_characters_stay_inside_narrow_terminal(self):
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            application, screen = app(["\n"], width, height)
            application.menu("Overview", [("A very long VM name " * 10 + "机器", "Long region/project " * 12, "vm")],
                             notes=["A diagnostic that is very long " * 15], footer="Enter connect   P allocation   R refresh   Esc back")
            self.assertIn("Enter", screen.frames[-1])

    def test_error_details_scroll_without_overflow(self):
        application, screen = app(["d", curses.KEY_NPAGE, curses.KEY_NPAGE, "k", "\n"], 60, 22)
        application.message("Quota error", "SSD storage quota exceeded. Choose another region.", details="\n".join(f"trace detail {i} " * 4 for i in range(60)), error=True)
        self.assertIn("Lines", screen.frames[-1])

    def test_narrow_confirmation_requires_paging_through_billing_terms(self):
        notes = ["training-box · H200 · eu-west1", "my-project · Preemptible · 200 GiB SSD",
                 "Reuse boot disk: h200-0909-2312-boot. No new disk will be created.",
                 "Estimate: $2.45/hour", "Auto-stop: 2 hours; disks remain billable until deleted.",
                 "Nebius may stop this VM when capacity is needed."]
        application, screen = app(["\n"] * 30, 48, 20)
        self.assertTrue(application.confirm_launch(notes, "{}"))
        self.assertGreater(len(screen.frames), 1)
        frames = "\n".join(screen.frames)
        self.assertIn("Estimate:", frames)
        self.assertIn("Auto-stop:", frames)
        self.assertIn("remain billable", frames)
        self.assertIn("Reuse boot disk:", frames)
        self.assertNotIn("Enter: create VM", screen.frames[0])
        self.assertIn("Enter: create VM", screen.frames[-1])

    def test_narrow_confirmation_escape_never_submits(self):
        application, screen = app(["\x1b"], 48, 20)
        self.assertFalse(application.confirm_launch(["billing terms " * 30], "{}"))

    def test_reusable_disk_is_not_presented_as_a_vm_or_recovery_block(self):
        application, screen = app(["\n", "q", "q"], 60, 24)
        application.inventory = {"vms": [], "recovery": [], "source": "live", "reusable_disks": [{
            "name": "saved-boot", "disk_id": "computedisk-test", "disk_gib": 200,
            "project": {"project_name": "my-project", "region": "eu-north1"},
        }]}
        with patch.object(application, "read", return_value=application.inventory):
            application.overview()
        frames = "\n".join(screen.frames)
        self.assertIn("BOOT DISK AVAILABLE", frames)
        self.assertIn("No VM was created", frames)
        self.assertNotIn("RECOVERY NEEDED", frames)

    def test_explicit_zero_capacity_wins_over_high_level(self):
        self.assertEqual(ui.available({"available": 0, "level": "high"}), "unavailable")

    def test_vm_actions_no_longer_offer_auto_stop(self):
        application, screen = app([curses.KEY_END, "\x1b"], 80, 40)
        with self.assertRaises(ui.Back):
            application.vm_actions({"id": "computeinstance-test", "name": "test", "state": "running", "region": "eu-north1",
                                    "allocation": "preemptible", "project_name": "personal", "managed": True, "can_delete": True,
                                    "auto_stop_hours": 2, "auto_stop_warning": "old warning"})
        self.assertNotIn("Auto-stop", "\n".join(screen.frames))
        self.assertNotIn("old warning", "\n".join(screen.frames))

    def test_manage_shortcut_targets_highlighted_vm_and_preserves_jump_hint(self):
        application, screen = app([curses.KEY_DOWN, "M"], 48, 20)
        value = application.menu("Jump into a VM", [("One", "", {"id": "one"}), ("Two", "", {"id": "two"})],
                                 actions={"m": lambda vm: {"manage": vm}}, footer="Enter SSH   M actions   Esc back")
        self.assertEqual(value, {"manage": {"id": "two"}})
        self.assertIn("[Enter] SSH", screen.frames[-1])
        self.assertIn("[M] actions", screen.frames[-1])

    def test_manage_with_no_search_matches_does_not_return_an_invalid_selection(self):
        application, screen = app(["/", "z", "\n", "M", "\x1b"])
        with self.assertRaises(ui.Back):
            application.menu("Jump", [("One", "", {"id": "one"})], actions={"m": lambda vm: {"manage": vm}})

    def test_delete_vm_enter_does_not_confirm_and_escape_cancels(self):
        application, screen = app(["d", "\n", "\x1b"])
        vm = {"id": "computeinstance-test", "name": "test", "state": "running", "region": "eu-north1",
              "allocation": "preemptible", "project_name": "personal", "can_delete": True, "managed": True}
        with patch.object(application, "mutate") as mutate:
            application.vm_actions(vm)
            mutate.assert_not_called()
        self.assertIn("[Esc] cancel", screen.frames[-1])

    def test_unused_disk_delete_enter_does_not_confirm_and_escape_cancels(self):
        application, screen = app(["d", "\n", "\x1b"], 48, 20)
        disk = {"name": "unused-boot", "disk_id": "computedisk-test", "disk_gib": 200,
                "project": {"region": "eu-north1", "project_name": "personal"}}
        with patch.object(application, "mutate") as mutate:
            application.disk_actions(disk)
            mutate.assert_not_called()
        self.assertIn("[Esc] cancel", screen.frames[-1])

    def test_actions_remain_visible_and_in_bounds_for_vm_states(self):
        for state in ["running", "stopped", "disk remains"]:
            for width, height in [(48, 20), (80, 24), (120, 44)]:
                application, screen = app([curses.KEY_END, "\x1b"], width, height)
                vm = {"id": "computeinstance-test", "name": "long-name-" * 5, "state": state,
                      "region": "eu-north1", "allocation": "preemptible", "project_name": "my-project",
                      "can_delete": True, "managed": True, "instance_deleted": state == "disk remains"}
                with self.assertRaises(ui.Back):
                    application.vm_actions(vm)
                self.assertIn("Delete", screen.frames[-1])


if __name__ == "__main__":
    unittest.main()
