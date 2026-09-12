#!/usr/bin/env python3
"""Nebius: GPU capacity, VM management, SSH and local port forwarding.

Use arrows or j/k to move, Enter to select, / to search, and Esc to go back.
Billable and destructive operations require a configuration review and confirmation.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import locale
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any

import nebius_core as core
import nebius_catalog as catalog
import nebius_jobs as jobs
import nebius_timing as timing
import nebius_ports as ports
import nebius_ssh as ssh_client
import nebius_shortcuts as shortcuts


class Back(Exception):
    pass


class Background(Exception):
    pass


def clean(value: Any) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in str(value))


def fit(value: Any, width: int) -> str:
    text = clean(value)
    if cell_width(text) <= max(0, width):
        return text
    result, cells = "", 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if cells + size > max(0, width - 1):
            break
        result += char
        cells += size
    return result + "…" if width > 0 else ""


def cell_width(value: str) -> int:
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in value)


def available(value: dict[str, Any]) -> str:
    if value.get("available") is not None:
        count = int(value["available"])
        return f"{count} available" if count else "unavailable"
    level = str(value.get("level") or "unknown")
    return {"limit_reached": "quota limit", "high": "high availability", "medium": "medium availability",
            "low": "low availability"}.get(level, "unknown")


def allocation_label(value: str) -> str:
    return "Preemptible" if value == "preemptible" else "On-demand"


def menu_shortcuts(rows, actions):
    """Stable action letters; numbered shortcuts for resource choices."""
    preferred = {"connect": "c", "stop": "s", "start": "t", "delete": "d", "ports": "p",
                 "settings": "e", "storage": "o", "details": "i", "add": "n", "open": "o",
                 "copy": "c", "pause": "p", "resume": "r", "remove": "d", "name": "n",
                 "allocation": "p", "project": "l", "review": "r", "reuse": "u",
                 "repair": "f", "recover": "r", "archive": "a", "__new": "n",
                 "overview": "v", "get": "g", "capacity": "c", "activity": "a", "account": "s"}
    used = {k.lower() for k in actions} | {"j", "k", "q"}
    result = {}
    for index, row in enumerate(rows):
        explicit = next((key for key, value in actions.items() if not callable(value) and value == row[2]), None)
        if explicit:
            result[index] = explicit
            continue
        wanted = preferred.get(row[2]) if isinstance(row[2], str) else None
        candidates = ([wanted] if wanted else []) + list("1234567890")
        key = next((key for key in candidates if key not in used), None)
        if key:
            result[index] = key
            used.add(key)
    return result


def key_label(key):
    return "Shift+" + key if key.isupper() else key.upper()


class App:
    def __init__(self, screen, entry="overview", vm_id=None, username=None):
        self.screen = screen
        self.entry = entry
        self.entry_vm = {"id": vm_id, "name": vm_id, "ssh_user": username}
        self.capacity: dict[str, Any] = {}
        self.inventory: dict[str, Any] = {}
        self.allocation = "on_demand"
        self.notice = ""
        self.color = curses.has_colors() and "NO_COLOR" not in os.environ
        self.selection = curses.A_REVERSE
        if self.color:
            curses.start_color()
            curses.use_default_colors()
            # xterm-256 approximations of Nebius lime (#E0FF4F) and ink (#052B42).
            # Never redefine terminal palette slots: that can leak into SSH/apps.
            lime, ink = (191, 17) if curses.COLORS >= 256 else (curses.COLOR_YELLOW, curses.COLOR_BLACK)
            # Light Omarchy themes need dark ink, not lime body text. Outside
            # Omarchy, leave accent text at the terminal's own foreground.
            accent = -1
            try:
                theme = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
                match = re.search(r'^mode\s*=\s*"(light|dark)"\s*$', theme.read_text(), re.MULTILINE)
                mode = match.group(1) if match else None
                accent = ink if mode == "light" else lime if mode == "dark" else -1
            except OSError:
                pass
            curses.init_pair(1, accent, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, ink, lime)
            self.selection = curses.color_pair(4)
        self.accent = curses.color_pair(1) if self.color else curses.A_BOLD
        self.error = curses.color_pair(2) if self.color else curses.A_BOLD
        self.ok = curses.color_pair(3) if self.color else curses.A_BOLD
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def put(self, y: int, x: int, text: Any, attr=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            try:
                self.screen.addstr(y, x, fit(text, width - x - 1), attr)
            except curses.error:
                pass

    def wrap(self, text: Any, width: int | None = None) -> list[str]:
        """Wrap terminal cells, retaining indentation in JSON and help text."""
        width = max(1, width if width is not None else self.screen.getmaxyx()[1] - 6)
        result = []
        for raw in str(text).expandtabs(4).splitlines() or [""]:
            line = clean(raw)
            leading = len(line) - len(line.lstrip())
            indent = " " * min(leading, width // 3)
            remaining = line[leading:]
            if not remaining:
                result.append("")
            while remaining:
                end, cells = 0, len(indent)
                for char in remaining:
                    if cells + cell_width(char) > width:
                        break
                    end += 1
                    cells += cell_width(char)
                if not end:
                    # A single wide glyph cannot fit a one-cell viewport.
                    result.append(indent + "…")
                    remaining = remaining[1:]
                    continue
                if end < len(remaining):
                    boundary = remaining.rfind(" ", 0, end + 1)
                    if boundary > 0:
                        end = boundary
                result.append(indent + remaining[:end].rstrip())
                remaining = remaining[end:].lstrip()
        return result

    def key_hints(self, footer):
        hints = []
        for hint in re.split(r"\n|\s{2,}", footer):
            hint = hint.replace("↑↓ / j k", "↑↓/jk").replace("Enter / Esc", "Enter/Esc")
            key, _, description = hint.partition(" ")
            hints.append(f"[{key}] {description}" if description else hint)
        return hints

    def allocation_switch(self, y: int, allocation: str):
        """A compact segmented control; reverse video also works without color."""
        x = 2
        self.put(y, x, "[", self.accent)
        x += 1
        for index, mode in enumerate(("on_demand", "preemptible")):
            if index:
                self.put(y, x, "|", self.accent)
                x += 1
            label = " " + allocation_label(mode) + " "
            attr = self.selection | curses.A_BOLD if allocation == mode else 0
            self.put(y, x, label, attr)
            x += cell_width(label)
        self.put(y, x, "]", self.accent)
        hint = "[P] switch" if self.screen.getmaxyx()[1] - x >= 14 else "[P]"
        self.put(y, x + 3, hint, self.accent)

    def frame(self, title: str, subtitle="", footer="↑↓ / j k move   Enter select   / search   Esc back", *, allocation=None) -> int:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.put(1, 2, "NEBIUS", self.selection | curses.A_BOLD)
        self.put(1, 11, "/ " + title, curses.A_BOLD)
        if allocation is not None:
            self.allocation_switch(3, allocation)
        self.put(4 if allocation is not None else 3, 2, subtitle)
        self.put(5 if allocation is not None else 4, 2, "─" * max(0, width - 4), self.accent)
        foot = []
        for row in footer.splitlines():
            line = ""
            for hint in self.key_hints(row):
                candidate = line + (" " if width < 64 else "  ") + hint if line else hint
                if line and cell_width(candidate) > width - 6:
                    foot.extend(self.wrap(line))
                    line = hint
                else:
                    line = candidate
            foot.extend(self.wrap(line))
        # Never drop the leading navigation keys on a narrow terminal.
        foot_top = max(8, height - len(foot) - 1)
        self.put(foot_top - 1, 2, "─" * max(0, width - 4))
        for index, line in enumerate(foot[:height - foot_top - 1]):
            self.put(foot_top + index, 2, line, self.accent)
        return foot_top - 2

    def key(self):
        try:
            return self.screen.get_wch()
        except curses.error:
            return None

    def menu(self, title, rows, *, subtitle="", notes=None, actions=None, selected=0, footer=None):
        """Rows contain (label, detail, value[, section]); headings never select."""
        query = ""
        searching = False
        scroll = 0
        actions = actions or {}
        notes = notes or []
        allocation = actions.get("p") in ("__allocation", "allocation", "toggle")
        load_rows = rows if callable(rows) else None
        if load_rows:
            rows = load_rows()
        refreshed_at = time.monotonic()
        filtered = []
        while True:
            previous_id = None
            if load_rows and not searching and time.monotonic() - refreshed_at >= 2:
                if filtered and selected < len(filtered):
                    value = filtered[selected][2]
                    previous_id = value.get("id") if isinstance(value, dict) else value
                rows = load_rows()
                refreshed_at = time.monotonic()
            filtered = [row for row in rows if query.casefold() in f"{row[0]} {row[1]}".casefold()]
            if previous_id is not None:
                selected = next((i for i, row in enumerate(filtered)
                                 if (row[2].get("id") if isinstance(row[2], dict) else row[2]) == previous_id), selected)
            shortcuts = menu_shortcuts(filtered, actions)
            selected = min(max(0, selected), max(0, len(filtered) - 1))
            hints = footer or "↑↓ / j k move   Enter select   / search   Esc back"
            if "?" not in hints:
                hints += "   ? help"
            escape_hint = next((part for part in re.split(r"\s{2,}", hints) if part.startswith("Esc ")), "Esc back")
            enter_hint = next((part for part in re.split(r"\s{2,}", hints) if part.startswith("Enter ")), "Enter select")
            compact = "↑↓ / j k move   " + enter_hint + "   " + escape_hint
            # Derive hints from this screen's real bindings, not just the letter P.
            named_hints = {part.split(" ", 1)[0].lower(): part.split(" ", 1)[1]
                           for part in re.split(r"\s{2,}", hints) if " " in part}
            context_keys = []
            row_keys = set(shortcuts.values())
            for key in actions:
                if key in row_keys:
                    continue  # Printed beside the choice itself.
                label = "switch" if key == "p" and allocation else named_hints.get(key.lower(),
                    {"p": "ports", "a": "activity", "r": "refresh", "m": "actions", "g": "GPU"}.get(key, "select"))
                context_keys.append(key_label(key) + " " + label)
            row_hints = {"connect": "SSH", "stop": "stop", "start": "start", "delete": "delete",
                         "ports": "ports", "settings": "username", "storage": "disks", "details": "details",
                         "add": "add port", "open": "browser", "copy": "copy", "pause": "pause", "resume": "retry",
                         "remove": "remove", "name": "name", "allocation": "switch", "project": "project",
                         "review": "review", "get": "GPU", "overview": "VMs", "capacity": "capacity",
                         "activity": "activity", "account": "account", "jump": "jump", "__new": "new project"}
            for index, key in shortcuts.items():
                if not key.isdigit():
                    value = filtered[index][2]
                    label = row_hints.get(value, re.sub(r"^\[[^]]+\]\s*", "", filtered[index][0])) if isinstance(value, str) else "select"
                    context_keys.append(key_label(key) + " " + label)
            context_keys += ["/ search", "? help"]
            compact += "\n" + "   ".join(context_keys)
            bottom = self.frame(title, subtitle, compact, allocation=self.allocation if allocation else None)
            height, width = self.screen.getmaxyx()
            y = 7 if allocation else 6
            note_lines = [line for note in notes for line in self.wrap(note, width - 8)]
            note_slots = max(1 if notes else 0, min(3, bottom - y - 7))
            clipped_notes = len(note_lines) > note_slots
            for line in note_lines[:max(0, note_slots - (1 if clipped_notes else 0))]:
                self.put(y, 4, line)
                y += 1
            if clipped_notes:
                self.put(y, 4, "[F2] Read full summary", self.accent)
                y += 1
            if notes:
                y += 1
            if query or searching:
                self.put(y, 4, "Search: " + query + ("_" if searching else ""), self.accent)
                y += 2
            room = max(1, bottom - y - 1)
            detail_limit = min(3, max(1, room - 1 - int(any(len(row) > 3 and row[3] for row in filtered))))
            content, bounds = [], []
            previous_section = None
            for index, row in enumerate(filtered):
                section = row[3] if len(row) > 3 else ""
                begin = len(content)
                if section and section != previous_section:
                    content.append(("section", section, index))
                previous_section = section
                content.append(("label", row[0], index))
                detail = self.wrap(row[1], width - 8) if row[1] else []
                visible_detail = detail[:detail_limit]
                if len(detail) > detail_limit:
                    visible_detail[-1] = fit(visible_detail[-1] + " …", width - 8)
                content.extend(("detail", line, index) for line in visible_detail)
                bounds.append((begin, len(content)))
                content.append(("gap", "", index))
            if bounds:
                begin, end = bounds[selected]
                scroll = min(scroll, begin)
                if end > scroll + room:
                    scroll = max(begin, end - room) if end - begin > room else end - room
                scroll = max(0, min(scroll, max(0, len(content) - room)))
                # Start on a complete choice, never an orphaned description.
                scroll = next((row_begin for row_begin, _ in bounds if scroll <= row_begin <= begin), scroll)
            else:
                scroll = 0
            for offset, (kind, text, index) in enumerate(content[scroll:scroll + room]):
                if kind == "label":
                    text = re.sub(r"^\[[^]]+\]\s*", "", text)
                    shortcut = " [" + key_label(shortcuts[index]) + "]" if index in shortcuts else ""
                    label = ("› " if index == selected else "  ") + fit(text, width - 7 - len(shortcut)) + shortcut
                    attr = curses.A_BOLD
                    if index == selected:
                        label += " " * max(0, width - 5 - cell_width(label))
                        attr |= self.selection
                    self.put(y + offset, 2, label, attr)
                elif kind == "detail":
                    self.put(y + offset, 6, text, self.accent if index == selected else 0)
                elif kind == "section":
                    self.put(y + offset, 2, text, self.accent | curses.A_BOLD)
            if not filtered:
                self.put(y, 4, "No matches. Press / to change the search.")
            slots = max(1, sum(begin < scroll + room and end > scroll for begin, end in bounds))
            position = f"{selected + 1 if filtered else 0} of {len(filtered)}"
            if scroll:
                position += "   ↑ more"
            if scroll + room < len(content) - 1:
                position += "   ↓ more"
            self.put(bottom - 1, 4, position)
            self.screen.refresh()
            key = self.key()
            if key is None:
                continue
            if searching:
                if key in ("\n", "\r", 27, "\x1b"):
                    searching = False
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    query = query[:-1]
                    selected = 0
                elif key == "\x15":
                    query = ""
                elif isinstance(key, str) and key.isprintable():
                    query += key
                    selected = 0
                continue
            if key in ("\x1b", 27, "q"):
                raise Back()
            if key == "/":
                searching = True
            elif key == "?":
                current = filtered[selected] if filtered else None
                text = ((str(current[0]) + "\n\n" + str(current[1]) + "\n\n") if current else "")
                text += "Keyboard\n" + "\n".join("  " + hint for hint in self.key_hints(hints))
                text += "\n" + "\n".join("  [" + key_label(key) + "] " + filtered[index][0] for index, key in shortcuts.items())
                text += "\n  [PgUp/PgDn] page\n  [Home/End] first / last\n  Search: [Ctrl+U] clear, [Enter] finish"
                self.message("Menu help", text)
            elif key == curses.KEY_F2 and clipped_notes:
                self.message(title + " / summary", "\n\n".join(notes))
            elif isinstance(key, str) and key in actions:
                action = actions[key]
                if callable(action) and not filtered:
                    continue
                return action(filtered[selected][2]) if callable(action) else action
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(filtered) - 1, selected + 1)
            elif key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key == curses.KEY_NPAGE:
                selected = min(len(filtered) - 1, selected + slots)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - slots)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(filtered) - 1)
            elif key in ("\n", "\r", curses.KEY_ENTER) and filtered:
                return filtered[selected][2]
            elif isinstance(key, str) and key.lower() in actions:
                action = actions[key.lower()]
                if callable(action) and not filtered:
                    continue
                return action(filtered[selected][2]) if callable(action) else action
            elif isinstance(key, str):
                index = next((index for index, shortcut in shortcuts.items() if key == shortcut or key.lower() == shortcut), None)
                if index is not None:
                    return filtered[index][2]

    def edit(self, title, initial, *, notes=None, validate=None):
        value = initial
        cursor = len(value)
        issue = ""
        while True:
            bottom = self.frame(title, "Edit the proposal or press Enter to keep it", "Enter continue   Ctrl+U clear   ←→ move cursor   Esc back")
            self.put(7, 3, value[:cursor] + "▏" + value[cursor:], curses.A_REVERSE)
            y = 10
            for note in (notes or []) + ([issue] if issue else []):
                for line in self.wrap(note):
                    if y >= bottom:
                        break
                    self.put(y, 3, line, self.error if note == issue else 0)
                    y += 1
                y += 1
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", 27):
                raise Back()
            if key in ("\n", "\r", curses.KEY_ENTER):
                try:
                    return validate(value) if validate else value
                except core.NebiusError as error:
                    issue = str(error)
            elif key == "\x15":
                value, cursor = "", 0
            elif key in (curses.KEY_LEFT,):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_RIGHT,):
                cursor = min(len(value), cursor + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                cursor = len(value)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
                value = value[:cursor - 1] + value[cursor:]
                cursor -= 1
            elif key == curses.KEY_DC:
                value = value[:cursor] + value[cursor + 1:]
            elif isinstance(key, str) and key.isprintable() and len(value) < 80:
                value = value[:cursor] + key + value[cursor:]
                cursor += 1

    def message(self, title, text, *, details="", error=False):
        offset = 0
        show_details = False
        while True:
            bottom = self.frame(title, "", "Enter / Esc back   D details   ↑↓ scroll" if details else "Enter / Esc back   ↑↓ scroll")
            lines = self.wrap(text + ("\n\n" + details if show_details else ""))
            slots = max(1, bottom - 7)
            offset = min(offset, max(0, len(lines) - slots))
            for y, line in enumerate(lines[offset:offset + slots], 6):
                self.put(y, 2, line, self.error if error and y == 6 else 0)
            if len(lines) > slots:
                self.put(bottom - 1, 2, f"Lines {offset + 1}–{min(len(lines), offset + slots)} of {len(lines)}")
            self.screen.refresh()
            key = self.key()
            if key in ("\n", "\r", "\x1b", "q", curses.KEY_ENTER):
                return
            if key in ("d", "D"):
                show_details = not show_details
                offset = 0
            elif key in ("j", curses.KEY_DOWN):
                offset += 1
            elif key in ("k", curses.KEY_UP):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset += slots
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - slots)

    def show_error(self, error):
        explanation = core.explain_error(str(error))
        self.message("Could not finish", explanation["message"] + "\n\n" + explanation["recovery"],
                     details=explanation["details"], error=True)

    def confirm_launch(self, notes, details, *, action="create VM and start billing", title="Review VM", confirm_key=None):
        """Every billing term must be displayed before Enter can submit."""
        offset = 0
        while True:
            footer = (f"{confirm_key.upper()} {action} at end   Enter next page   I details   ↑↓ scroll   Esc cancel"
                      if confirm_key else "Enter next page / confirm at end   D details   ↑↓ scroll   Esc back")
            bottom = self.frame(title, "Review before confirming", footer)
            lines = self.wrap("\n\n".join(notes))
            slots = max(1, bottom - 8)
            offset = min(offset, max(0, len(lines) - slots))
            for y, line in enumerate(lines[offset:offset + slots], 5):
                self.put(y, 2, line)
            at_end = offset + slots >= len(lines)
            self.put(bottom - 2, 2, ((confirm_key.upper() if confirm_key else "Enter") + ": " + action)
                     if at_end else "Enter: read next page", self.accent | curses.A_BOLD)
            self.put(bottom - 1, 2, f"Lines {offset + 1}–{min(len(lines), offset + slots)} of {len(lines)}")
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", "q"):
                return False
            if confirm_key and isinstance(key, str) and key.lower() == confirm_key:
                if at_end:
                    return True
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                if at_end and not confirm_key:
                    return True
                if not at_end:
                    offset += slots
            elif key in (("i", "I") if confirm_key else ("d", "D")):
                self.message(title + " / details", details)
            elif key in ("j", curses.KEY_DOWN):
                offset += 1
            elif key in ("k", curses.KEY_UP):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset += slots
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - slots)

    def progress(self, title, started, *, mutation=False):
        elapsed = int(time.time() - started)
        operation = core._read_json(getattr(self, "watched_operation", core.OPERATION_FILE), {}) if mutation else {}
        spinner = "|/-\\"[elapsed % 4]
        bottom = self.frame(title, f"{spinner}  {elapsed // 60}:{elapsed % 60:02d} elapsed",
                            "Esc/B background (work continues)" if mutation else "Esc cancel this read")
        y = 7
        for line in self.wrap(operation.get("message") or title)[:2]:
            self.put(y, 3, line, self.accent | curses.A_BOLD)
            y += 1
        if mutation:
            self.put(5, 3, timing.headline(operation), self.accent | curses.A_BOLD)
            y = min(y + 1, 9)
            stage_lines = timing.lines(operation)
            slots = max(1, bottom - y + 1)
            if len(stage_lines) > slots:
                stage_lines = ["Earlier stages saved in Activity"] + stage_lines[-max(1, slots - 1):]
            for line in stage_lines[:slots]:
                self.put(y, 3, line)
                y += 1
        self.screen.refresh()

    def read(self, title, *arguments):
        started = time.time()
        with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen([sys.executable, str(Path(core.__file__)), *arguments],
                                       stdout=output, stderr=errors, start_new_session=True)
            while process.poll() is None:
                self.progress(title, started)
                key = self.key()
                if key in ("\x1b", 27):
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait()
                    raise Back()
            output.seek(0)
            errors.seek(0)
            if process.returncode:
                raise core.NebiusError(errors.read().strip() or f"Read failed: exit {process.returncode}")
            return json.load(output)

    def active_job(self):
        return next((job for job in jobs.jobs() if job.get("phase") in {"running", "queued"}), None)

    def watch(self, job_id, title, started=None):
        started = started or time.time()
        path = core.STATE_DIR / "jobs" / f"{job_id}.json"
        self.watched_operation = path.with_name(job_id + ".operation.json")
        while True:
            job = core._read_json(path, {})
            if job.get("phase") in {"ready", "error"}:
                if job["phase"] == "error":
                    operation = core._read_json(self.watched_operation, {}) if job.get("started_at") else {}
                    if job.get("command") in {"create", "start"} and operation.get("vm_id"):
                        self.operation_result({**job, "operation": operation})
                        raise Background()
                    self.message("Operation stopped", operation.get("message", job.get("error", "")) + "\n\n" + timing.text(operation) + "\n\n" + operation.get("recovery", ""),
                                 details=job.get("error", ""), error=True)
                    raise Back()
                return job.get("result", {})
            if job.get("pid"):
                try:
                    os.kill(int(job["pid"]), 0)
                except OSError:
                    raise core.NebiusError("The operation worker stopped. Refresh overview before retrying; the cloud request may still finish.")
            elif time.time() - started > 10:
                raise core.NebiusError("The operation worker could not start. No cloud request was confirmed.")
            self.progress(title, started, mutation=True)
            if self.key() in ("b", "B", "\x1b", 27):
                self.notice = "Operation continues. Press A to follow progress."
                raise Background()

    def mutate(self, title, *arguments):
        job_id = jobs.submit(arguments, title)["job_id"]
        if arguments[0] in {"start", "stop", "delete", "delete-disk"}:
            self.notice = title + " submitted. A opens Activity; other VMs remain available."
            raise Background()
        return self.watch(job_id, title)

    def load_capacity(self, refresh=False):
        if not self.capacity or refresh:
            self.capacity = self.read("Refreshing regional capacity" if refresh else "Loading capacity", "capacity", *(["--refresh"] if refresh else []))
        return self.capacity

    def snapshot_note(self, snapshot):
        source = snapshot.get("source")
        age = max(0, int(snapshot.get("cache_age_seconds") or 0)) // 60
        return "Live snapshot" if source == "live" else f"Saved snapshot · {age} min old" + (" · refresh failed" if source == "stale-cache" else "")

    def toggle_allocation(self):
        self.allocation = "preemptible" if self.allocation == "on_demand" else "on_demand"

    def capacity_flow(self, launch=False):
        self.load_capacity()
        while True:
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in self.capacity.get("offerings", []):
                groups.setdefault(catalog.gpu_name(row.get("platform", ""), row["gpu_label"]), []).append(row)
            rows = []
            for label, offerings in sorted(groups.items()):
                best = max(offerings, key=lambda item: core._availability_score(item[self.allocation]))
                shapes = sorted({int(item.get("gpu_count") or 0) for item in offerings})
                regions = len({item["region"] for item in offerings})
                rows.append((label, f"{available(best[self.allocation])} · {','.join(map(str, shapes))} GPU shapes · {regions} regions", label))
            try:
                chosen = self.menu("Get a GPU" if launch else "GPU capacity", rows,
                    subtitle=self.snapshot_note(self.capacity),
                    notes=["Preemptible costs less and may be stopped by Nebius."] if self.allocation == "preemptible" else ["On-demand uses regular PAYG capacity."],
                    actions={"p": "__allocation", "r": "__refresh"},
                    footer="↑↓ / j k move   Enter configurations   P allocation   R refresh   / search   Esc back")
                if chosen == "__allocation":
                    self.toggle_allocation()
                    continue
                if chosen == "__refresh":
                    self.load_capacity(True)
                    continue
                self.configuration(chosen)
            except Back:
                return

    def configuration(self, gpu_label):
        while True:
            offerings = catalog.configurations([row for row in self.capacity.get("offerings", [])
                        if catalog.gpu_name(row.get("platform", ""), row["gpu_label"]) == gpu_label], self.allocation)
            offerings.sort(key=lambda row: (int(row.get("gpu_count") or 0), -core._availability_score(row[self.allocation])[0], row["region"]))
            rows = [(f"{row['gpu_count']}× {gpu_label} · {row['region']}",
                     f"{row['vcpu_count']} vCPU · {row['memory_gib']} GiB RAM\n"
                     f"On-demand: {available(row['on_demand'])}\nPreemptible: {available(row['preemptible'])}", row) for row in offerings]
            try:
                row = self.menu("Configuration", rows, subtitle=self.snapshot_note(self.capacity),
                    notes=["Regional capacity is not project eligibility. Choose GPU and region; preflight checks your project before allocation."],
                    actions={"p": "__allocation", "r": "__refresh"},
                    footer="↑↓ / j k move   Enter continue   P allocation   R refresh   / search   Esc GPU types")
                if row == "__allocation":
                    self.toggle_allocation()
                    continue
                if row == "__refresh":
                    self.load_capacity(True)
                    continue
                if core._availability_score(row[self.allocation])[0] < 0:
                    self.message("Unavailable", f"{allocation_label(self.allocation)} capacity is unavailable for this configuration.\n\nPress P to switch allocation, or select another region.")
                    continue
                try:
                    self.launch_flow(row)
                    return
                except Back:
                    continue
            except Back:
                return

    def choose_project(self, offering):
        projects = offering.get("projects") or []
        while True:
            if len(projects) == 1:
                return projects[0]["project_id"]
            rows = [(project["project_name"], project["region"], project["project_id"], "Your projects") for project in projects]
            rows.append(("Create a project…", "Choose a name for your project in " + offering["region"], "__new", "New project"))
            choice = self.menu("Project", rows, subtitle=offering["region"],
                               notes=["Only your personal projects are shown."] if projects else ["No personal project is ready here. Create one to place your VM in this region."])
            if choice != "__new":
                return choice
            try:
                name = self.edit("Project name", "gpu-" + offering["region"], validate=core._safe_name,
                                 notes=["This project can hold any Nebius resources.", "A default network is included. The project remains if you cancel the VM later."])
                approved = self.menu("Create project", [("Create " + name, offering["region"], True), ("Back to project selection", "No changes", False)],
                                     subtitle="Review project", notes=[f"Name: {name}", f"Region: {offering['region']}"])
                if not approved:
                    continue
                result = self.mutate("Creating project", "create-project", "--region", offering["region"], "--name", name, "--confirmed")
                self.notice = f"Project {result['project_name']} created."
                self.load_capacity(True)
                refreshed = next((item for item in catalog.configurations(self.capacity["offerings"], self.allocation)
                                  if catalog.configuration_key(item) == catalog.configuration_key(offering)), None)
                if not refreshed or result["project_id"] not in {p["project_id"] for p in refreshed["projects"]}:
                    raise core.NebiusError("The project exists, but GPU placement is not ready yet. Refresh capacity in a moment.")
                offering.update(refreshed)
                return result["project_id"]
            except Back:
                continue
            except core.NebiusError as error:
                self.show_error(error)

    def choose_image(self, offering, project_id):
        arguments = ["images", "--project-id", project_id]
        for variant in offering.get("variants", [offering]):
            arguments += ["--offering-id", variant["offering_id"]]
        while True:
            result = self.read("Finding available images", *arguments)
            rows = [("Default Ubuntu / CUDA", core.IMAGE_FAMILY, None, "Default")]
            for image in result["images"]:
                detail = (f"{image['cpu_architecture']} · minimum {image['min_disk_gib']} GiB disk\n"
                          f"{image['image_id']}")
                if image.get("summary"):
                    detail = image["summary"] + "\n" + detail
                if image.get("description"):
                    detail += "\n" + image["description"]
                if image["warnings"]:
                    detail += "\n" + "; ".join(image["warnings"])
                rows.append((image["name"], detail, image, image["source"]))
            chosen = self.menu("Boot image", rows, subtitle=offering["region"],
                               notes=["Public and accessible project images. Known architecture and hardware mismatches are excluded.",
                                      "Custom images must support cloud-init to install your SSH key."] + result["warnings"],
                               actions={"r": "__refresh"},
                               footer="↑↓ / j k move   Enter select   R refresh   / search   Esc back")
            if chosen != "__refresh":
                return chosen

    def launch_flow(self, offering):
        # A new project needs a new disk. Existing projects may already have a
        # reusable disk, so defer their storage check until placement is known.
        if not offering.get("projects"):
            preflight = self.read("Checking regional quota", "preflight", "--region", offering["region"],
                                  "--allocation", self.allocation, "--platform", offering["platform"],
                                  "--gpu-count", str(offering["gpu_count"]))
            if not preflight["ready"]:
                self.message("Region needs quota", preflight["message"] + "\n\n" + preflight["recovery"] + "\n\nNo project or VM was created.",
                             details=json.dumps(preflight, indent=2), error=True)
                raise Back()
        project_id = self.choose_project(offering)
        suggested = offering["platform"].removeprefix("gpu-").split("-")[0] + "-" + dt.datetime.now().strftime("%m%d-%H%M")
        name = suggested
        image = None
        disk_gib = core.DEFAULT_DISK_GIB
        plan = None
        created_vm = None
        while True:
            action = self.menu("VM settings", [
                ("Name", name, "name", "Configuration"),
                ("Boot image", image["name"] if image else "Default Ubuntu / CUDA", "image", "Configuration"),
                ("Boot disk", f"{disk_gib} GiB Network SSD", "disk", "Configuration"),
                ("Allocation", allocation_label(self.allocation) + " · [P] switch", "allocation", "Configuration"),
                ("Project", next((p["project_name"] for p in self.capacity["projects"] if p["project_id"] == project_id), project_id), "project", "Configuration"),
                ("Review and create", "Run preflight checks and review cost before confirming", "review", "Next step"),
            ], subtitle=f"{offering['gpu_count']}× {offering['gpu_label']} · {offering['region']}",
               notes=[self.notice] if self.notice else [], actions={"p": "allocation"},
               footer="↑↓ / j k move   Enter edit / continue   P allocation   Esc configurations")
            if action == "name":
                try:
                    name = self.edit("VM name", name, validate=core._safe_name)
                except Back:
                    pass
            elif action == "image":
                try:
                    image = self.choose_image(offering, project_id)
                    disk_gib = max(disk_gib, image["min_disk_gib"]) if image else disk_gib
                except Back:
                    pass
                except core.NebiusError as error:
                    self.show_error(error)
            elif action == "disk":
                def validate_size(value):
                    minimum = max(50, image["min_disk_gib"] if image else 50)
                    if not value.isdigit() or not minimum <= int(value) <= 30720:
                        raise core.NebiusError(f"Choose a disk size between {minimum} and 30720 GiB")
                    return value
                try:
                    disk_gib = int(self.edit("Boot disk (GiB)", str(disk_gib), validate=validate_size,
                                            notes=["Storage remains billable while the VM is stopped."]))
                except Back:
                    pass
            elif action == "allocation":
                self.toggle_allocation()
            elif action == "project":
                projects = offering.get("projects", [])
                try:
                    project_id = self.menu("Place VM in", [(p["project_name"], p["region"], p["project_id"]) for p in projects])
                    image = None
                except Back:
                    pass
            else:
                try:
                    selected = catalog.resolve_variant(offering, project_id, self.allocation,
                                                       image["compatible_offering_ids"] if image else None)
                    plan = self.read("Running preflight checks", "plan", "--offering-id", selected["offering_id"],
                                     "--project-id", project_id, "--name", name, "--allocation", self.allocation,
                                     "--image-id", image["image_id"] if image else "", "--disk-gib", str(disk_gib))
                    check = plan["preflight"]
                    if not check["ready"]:
                        self.message("Preflight blocked creation", check["message"] + "\n\n" + check["recovery"] +
                                     "\n\nNo new disk or VM was created.", details=json.dumps(check, indent=2), error=True)
                        continue
                    price = f"~${plan['estimated_usd_per_hour']:.3f}/hour" if plan.get("estimated_usd_per_hour") is not None else "Not available — check Nebius pricing before creating"
                    notes = [
                        f"Virtual machine\n  Name:    {name}\n  GPU:     {plan['gpu_count']}× {offering['gpu_label']}\n"
                        f"  Region:  {offering['region']}\n  Project: {plan['project']['project_name']}",
                        f"Billing\n  Allocation: {allocation_label(self.allocation)}\n  Storage:    {plan['disk_gib']} GiB SSD\n"
                        f"  Estimate:   {price}",
                        "No automatic stop is scheduled. Stop the VM manually when finished. Disks remain billable until deleted.",
                    ]
                    source = plan.get("boot_image", {"label": core.IMAGE_FAMILY, "note": ""})
                    notes.insert(1, f"Boot image\n  {source['label']}\n  {plan.get('image_id') or plan.get('image_family', '')}\n  {source['note']}")
                    notes.append("Network: static public IP; inbound SSH (TCP 22) only. Use Ports for local application access.")
                    if plan.get("reusable_disk"):
                        notes.insert(1, f"Boot disk\n  Reuse boot disk: {plan['reusable_disk']['name']}\n  No new disk will be created.")
                    if self.allocation == "preemptible":
                        notes.append("Nebius may stop a preemptible VM when capacity is needed.")
                    notes.append("Preflight\n  Checks passed: " + ", ".join(dict.fromkeys(
                        row["name"] for row in check["checks"] if row["state"] == "ok")) +
                        ("\n  " + "\n  ".join(check["warnings"]) if check["warnings"] else ""))
                    if core._availability_score(plan["capacity"])[0] == 0:
                        notes.append("Capacity is unreported; Nebius will confirm on submission.")
                    if not self.confirm_launch(notes, json.dumps(plan, indent=2)):
                        continue
                    vm = self.mutate("Creating VM", "create", "--plan-id", plan["plan_id"])
                    created_vm = vm
                    self.notice = f"{vm['name']} is ready for SSH. " + timing.headline(vm.get("launch_timing", {}))
                    self.operation_result({"phase": "ready", "result": vm, "operation": vm.get("launch_timing", {})})
                    self.inventory = {}
                    raise Background()
                except Back:
                    if created_vm:
                        self.inventory = {}
                        raise Background()
                    continue
                except core.NebiusError as error:
                    self.show_error(error)
                    if created_vm:
                        self.inventory = {}
                        raise Background()

    def operation_result(self, job):
        """A saved launch report stays open across SSH attempts and session exits."""
        operation = job.get("operation") or {}
        vm = dict(job.get("result") or {})
        vm.setdefault("id", operation.get("vm_id"))
        vm.setdefault("name", operation.get("name") or vm.get("id") or "VM")
        vm.setdefault("ssh_user", operation.get("ssh_user"))
        title = ("SSH ready" if operation.get("ssh_ready") else "Launch report") + " · " + vm["name"]
        lines = timing.lines(operation) + ["", operation.get("message") or jobs.describe(job)["message"]]
        if operation.get("recovery"):
            lines += ["", operation["recovery"]]
        lines += ["", "Saved: Activity or VM actions → Launch timings."]
        offset = 0
        while True:
            footer = ("C SSH   " if vm.get("id") else "") + "↑↓ scroll   D details   Esc back"
            bottom = self.frame(title, timing.headline(operation), footer)
            content = self.wrap("\n".join(lines))
            slots = max(1, bottom - 5)
            offset = min(offset, max(0, len(content) - slots))
            for y, line in enumerate(content[offset:offset + slots], 6):
                self.put(y, 2, line)
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", 27, "v", "V"):
                return
            if key in ("c", "C") and vm.get("id"):
                try:
                    self.ssh(vm)
                except Back:
                    pass
            elif key in ("d", "D"):
                self.message("Launch details", json.dumps(job, indent=2))
            elif key in ("j", curses.KEY_DOWN):
                offset += 1
            elif key in ("k", curses.KEY_UP):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_NPAGE, "\n", "\r"):
                offset += slots
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - slots)

    def ssh(self, vm):
        launch = jobs.latest_vm_job(vm["id"])
        if launch and launch.get("phase") in {"queued", "running"}:
            self.watch(launch["id"], "Waiting for SSH readiness · " + vm["name"])
            self.operation_result(jobs.latest_vm_job(vm["id"]) or launch)
            return
        launch_operation = (launch or {}).get("operation") or vm.get("launch_timing") or {}
        username = vm.get("ssh_user") or "ubuntu"
        if not vm.get("ssh_user"):
            username = self.edit("SSH username", username,
                                 notes=["This VM already exists. Use its login user; SSH will use your configured keys or agent."])
        while True:
            try:
                connection = self.read("Preparing SSH", "connect", "--vm-id", vm["id"], "--no-launch", "--username", username)
                vm["name"] = connection.get("name") or vm["name"]
                def progress(message, elapsed):
                    bottom = self.frame("Connecting to " + vm["name"], f"Checking SSH login · {elapsed}s / 120s", "Esc back (VM stays running)")
                    for y, line in enumerate(self.wrap(message), 6):
                        if y < bottom:
                            self.put(y, 3, line)
                    self.screen.refresh()
                if ssh_client.wait_ready(connection, progress=progress, cancelled=lambda: self.key() in ("\x1b", 27)) is not True:
                    raise ssh_client.SSHError("SSH login has not been verified; no session was opened")
                curses.def_prog_mode()
                curses.endwin()
                started = time.monotonic()
                try:
                    print(f"\nConnecting to {vm['name']}… Exit SSH to return to Nebius.\n", flush=True)
                    with tempfile.TemporaryFile(mode="w+") as errors:
                        result = subprocess.run(connection["command"], check=False, stderr=errors)
                        errors.seek(0)
                        detail = errors.read()[-4000:].strip()
                finally:
                    curses.reset_prog_mode()
                    self.screen.clear()
                    self.screen.refresh()
                if result.returncode == 0 and time.monotonic() - started >= 3:
                    return
                issue = (f"SSH exited with status {result.returncode}." if result.returncode else "The SSH session closed immediately.")
                issue += "\n" + (detail or "No diagnostic was returned by SSH.")
            except ssh_client.SSHCancelled:
                raise Back()
            except (ssh_client.SSHError, core.NebiusError, OSError) as error:
                issue = str(error)
            core._atomic_json(core.STATE_DIR / "ssh-last-error.json", {"vm_id": vm["id"], "name": vm["name"], "error": issue,
                                                                             "launch_timing": launch_operation})
            while True:
                choice = self.menu("SSH did not connect", [("Retry connection", "Check login readiness and try again", "resume"),
                                    ("SSH username", username, "settings"), ("Launch timings", timing.headline(launch_operation), "timings"),
                                    ("Back to VMs", "The VM is unchanged", "overview")],
                                   subtitle=vm["name"], notes=[issue, timing.headline(launch_operation)])
                if choice == "overview":
                    return
                if choice == "timings":
                    self.message("Launch timings", timing.text(launch_operation))
                    continue
                if choice == "settings":
                    username = self.edit("SSH username", username)
                break

    def activity(self):
        def rows():
            entries = sorted(jobs.jobs(), key=lambda job: job.get("phase") not in {"running", "queued"})
            result = []
            for job in entries:
                view = jobs.describe(job)
                result.append((view["title"] + " · " + view["status"], view["message"], job, view["section"]))
            return result
        while True:
            if not jobs.jobs():
                self.message("Activity", "No operations yet.")
                return
            selected = self.menu("Activity", rows, subtitle="Updates every 2s · saved results",
                                 actions={"r": "refresh"}, footer="↑↓ / j k move   Enter details   R refresh   Esc back")
            if selected == "refresh":
                continue
            if selected["phase"] in {"running", "queued"}:
                self.watch(selected["id"], jobs.describe(selected)["title"])
                completed = next((job for job in jobs.jobs() if job["id"] == selected["id"]), selected)
                if completed.get("command") in {"create", "start"}:
                    self.operation_result(completed)
            else:
                operation = selected.get("operation", {})
                view = jobs.describe(selected)
                if selected.get("command") in {"create", "start"} and not jobs.can_resume(selected):
                    self.operation_result(selected)
                    continue
                if jobs.can_resume(selected):
                    try:
                        action = self.menu(view["title"], [("Details", view["message"], "details"),
                             ("Resume operation", "Reconcile the saved cloud request and finish remaining steps", "resume")], subtitle=view["status"])
                    except Back:
                        continue
                    if action == "resume":
                        jobs.submit(selected["arguments"], view["title"])
                        continue
                text = view["status"] + "\n" + view["message"] + "\n\n" + timing.text(operation)
                if operation.get("recovery"):
                    text += "\n\n" + operation["recovery"]
                if selected.get("finished_at"):
                    text += "\n\nFinished: " + selected["finished_at"]
                self.message(view["title"], text, details=json.dumps(selected, indent=2), error=selected["phase"] != "ready")

    def port_form(self, vm, remote_port=8000, local_port=None, *, error=""):
        """One form and one submission for the two ends of an SSH tunnel."""
        values = [str(remote_port), str(local_port if local_port is not None else remote_port if int(remote_port) >= 1024 else int(remote_port) + 8000)]
        cursors = [len(value) for value in values]
        active, local_edited, replace = 0, local_port is not None, True
        issue = error
        try:
            "──SSH──▶".encode(sys.stdout.encoding or "utf-8")
            arrow = " ──SSH──▶ "
        except UnicodeEncodeError:
            arrow = " --SSH--> "
        while True:
            height, width = self.screen.getmaxyx()
            if height < 20 or width < 44:
                self.frame("Add SSH port", "Enlarge this terminal to at least 44×20", "Esc cancel")
                self.screen.refresh()
                if self.key() in ("\x1b", 27):
                    raise Back()
                continue
            bottom = self.frame("Add SSH port", vm["name"], "Tab/↑↓ fields   ←→ cursor   Enter save   Esc cancel\nCtrl+U clear field")
            for index, label in enumerate(("Remote port · VM", "Local port · this PC")):
                y = 6 + index * 2
                self.put(y, 2, ("› " if index == active else "  ") + label, curses.A_BOLD)
                field = values[index]
                if index == active:
                    field = field[:cursors[index]] + "▏" + field[cursors[index]:]
                self.put(y, 26, "[ " + field.ljust(6) + " ]", self.selection if index == active else 0)
            self.put(10, 2, "This computer", self.accent)
            self.put(10, 27, "VM app", self.accent)
            self.put(11, 2, ("127.0.0.1:" + (values[1] or "?")).ljust(15) + arrow + "127.0.0.1:" + (values[0] or "?"))
            note = issue or "Local access only. SSH carries traffic to the app on your VM."
            for y, line in enumerate(self.wrap(note, width - 6), 13):
                if y <= bottom:
                    self.put(y, 2, line, self.error if issue else 0)
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", 27):
                raise Back()
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                active = 1 - active
                replace = True
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                for index, validate in enumerate((ports.port, ports.validate_local_port)):
                    try:
                        validate(values[index])
                    except core.NebiusError as failure:
                        active, replace = index, True
                        issue = ("Remote port: " if index == 0 else "Local port: ") + str(failure)
                        break
                else:
                    return int(values[0]), int(values[1])
                continue
            value, cursor = values[active], cursors[active]
            changed = False
            if key == "\x15":
                value, cursor, changed = "", 0, True
            elif key == curses.KEY_LEFT:
                cursor, replace = max(0, cursor - 1), False
            elif key == curses.KEY_RIGHT:
                cursor, replace = min(len(value), cursor + 1), False
            elif key in (curses.KEY_HOME, "\x01"):
                cursor, replace = 0, False
            elif key in (curses.KEY_END, "\x05"):
                cursor, replace = len(value), False
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if replace:
                    value, cursor = "", 0
                elif cursor:
                    value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
                changed = True
            elif key == curses.KEY_DC:
                value, changed = value[:cursor] + value[cursor + 1:], True
            elif isinstance(key, str) and key in "0123456789":
                if replace:
                    value, cursor = "", 0
                if len(value) < 5:
                    value, cursor, changed = value[:cursor] + key + value[cursor:], cursor + 1, True
            values[active], cursors[active] = value, cursor
            if changed:
                issue, replace = "", False
                if active == 1:
                    local_edited = True
                elif not local_edited:
                    port = int(value) if value else 0
                    values[1] = str(port if port >= 1024 else port + 8000) if port else ""
                    cursors[1] = len(values[1])

    def ports(self, vm=None):
        def rows():
            saved = [m for m in ports.listing() if vm is None or m["vm_id"] == vm["id"]]
            return [(f"127.0.0.1:{m['local_port']} → {m['vm_name']}:{m['remote_port']}", m["state"], m) for m in saved] + [
                ("Add port", "Choose a remote application port to forward over SSH", "add")]
        while True:
            choice = self.menu("SSH port forwarding" + (" · " + vm["name"] if vm else ""), rows,
                               subtitle="Updates every 2s · SSH tunnel status",
                               notes=["Only this laptop can access these addresses. Enabled ports reconnect after login."],
                               actions={"r": "refresh"}, footer="↑↓ / j k move   Enter actions   R refresh   Esc back")
            if choice == "refresh":
                continue
            if choice == "add":
                target = vm
                if target is None:
                    inventory = self.read("Loading VMs", "list")
                    candidates = [v for v in inventory.get("vms", []) if not v.get("instance_deleted")]
                    if not candidates:
                        self.message("No VMs", "Create a VM before adding a port.")
                        continue
                    target = self.menu("Choose VM", [(v["name"], v["state"], v) for v in candidates])
                remote, local, issue = 8000, None, ""
                while True:
                    try:
                        remote, local = self.port_form(target, remote, local, error=issue)
                        self.read("Saving SSH port forward", "ports", "--action", "add", "--vm-id", target["id"],
                                  "--remote-port", str(remote), "--local-port", str(local))
                        self.notice = f"Saved: 127.0.0.1:{local} → {target['name']}:{remote}"
                        break
                    except Back:
                        break
                    except core.NebiusError as failure:
                        issue = str(failure)
            else:
                actions = [("Open in browser", choice["url"], "open"), ("Copy address", f"127.0.0.1:{choice['local_port']}", "copy"),
                           ("Details", choice.get("detail", ""), "details")]
                if not choice.get("deleted"):
                    actions.append(("Pause" if choice["enabled"] else "Resume", "Keep this saved mapping", "pause" if choice["enabled"] else "resume"))
                actions.append(("Remove port", "Stop forwarding and forget this mapping", "remove"))
                action = self.menu("Port actions", actions, subtitle=choice["state"])
                if action == "open":
                    try:
                        subprocess.Popen(["xdg-open", choice["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except OSError as error:
                        raise core.NebiusError("Could not open browser: " + str(error)) from error
                elif action == "copy":
                    try:
                        result = subprocess.run(["wl-copy"], input=f"127.0.0.1:{choice['local_port']}", text=True, capture_output=True)
                    except OSError as error:
                        raise core.NebiusError("Could not copy address: " + str(error)) from error
                    if result.returncode:
                        raise core.NebiusError("Could not copy address: " + result.stderr)
                    self.notice = "Address copied"
                elif action == "details":
                    self.message("Port details", choice.get("detail", "Tunnel status only; application readiness is not checked."), details=json.dumps(choice, indent=2))
                else:
                    ports.change(choice["id"], action)

    def vm_actions(self, vm, action=None):
        if vm.get("recovery_id"):
            request = next((item for item in self.inventory.get("recovery", []) if item["plan_id"] == vm["recovery_id"]), None)
            if not request:
                raise core.NebiusError("This saved recovery state changed. Refresh Your VMs")
            self.recovery_actions(request)
            return
        rows = []
        if vm["state"] == "running":
            rows += [("Check SSH and connect", "Open a session only after login succeeds", "connect"), ("Stop VM", "Compute stops; disks remain billable", "stop")]
        elif vm["state"] == "stopped":
            rows += [("Start VM", "Resume billing and wait for its address", "start")]
        if not vm.get("instance_deleted"):
            rows += [("SSH port forwarding", "Open remote apps at localhost; manage saved ports", "ports")]
            rows += [("Connection settings", "Edit the saved SSH username", "settings")]
        rows = [(*row, "VM actions") for row in rows]
        if not vm.get("instance_deleted"):
            rows += [("Disks and storage", "Inspect attached disks and cleanup options", "storage", "Inspect")]
        rows += [("Launch timings", "Saved stages and total time to SSH ready", "timings", "Inspect")]
        rows += [("Full details", "Resource identifiers and state", "details", "Inspect")]
        if vm.get("can_delete"):
            rows += [("Delete remaining boot disk" if vm.get("instance_deleted") else "Delete VM and boot disk",
                      "Permanent deletion; disk charges continue until removed", "delete", "Delete")]
        if action is None:
            action = self.menu(vm["name"], rows, subtitle=f"{vm['state']} · {vm['region']} · {allocation_label(vm['allocation'])}",
                               notes=[vm["project_name"]])
        elif action not in {row[2] for row in rows}:
            self.message("Action unavailable", "This action is not available for " + vm["name"] + " in its current state.")
            return
        if action == "connect":
            self.ssh(vm)
        elif action == "ports":
            self.ports(vm)
        elif action == "timings":
            launch = jobs.latest_vm_job(vm["id"])
            if launch and launch.get("phase") in {"running", "queued"}:
                self.watch(launch["id"], "Launch progress · " + vm["name"])
                launch = jobs.latest_vm_job(vm["id"])
            self.operation_result(launch or {"operation": vm.get("launch_timing", {}), "result": vm})
        elif action == "details":
            self.message("VM details", json.dumps(vm, indent=2))
        elif action == "storage":
            storage = self.read("Inspecting attached disks", "storage", "--vm-id", vm["id"])
            self.message("Disks and storage", "\n\n".join(
                f"{disk['name']}\n  {disk['size_gib']} GiB · {disk['state']} · {'boot' if disk['boot'] else 'secondary'}\n  {disk['id']}"
                for disk in storage["disks"]) + "\n\n" + storage["note"], details=json.dumps(storage, indent=2))
        elif action == "settings":
            username = self.edit("SSH username", vm.get("ssh_user") or "ubuntu")
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
                raise core.NebiusError("Invalid SSH username")
            connections = core._read_json(core.CONNECTIONS_FILE, {})
            connections[vm["id"]] = {"username": username}
            core._atomic_json(core.CONNECTIONS_FILE, connections)
            vm["ssh_user"] = username
        else:
            label = {"start": "Start VM", "stop": "Stop VM", "delete": "Delete VM and boot disk"}[action]
            notes = [vm["name"] + "\nProject: " + vm["project_name"] + " · " + vm["region"]]
            if action == "delete":
                notes += ["VM: " + vm["id"] + "\nBoot disk: " + str(vm.get("disk_id") or "not recorded"),
                          "Permanently deletes the VM and boot disk, including all boot-disk data. Cannot be undone. Secondary disks are kept and remain billable."]
            else:
                notes += ["Starting resumes compute charges." if action == "start" else "Stops compute charges. Disks remain saved and billable."]
            if self.confirm_launch(notes, json.dumps(vm, indent=2), title=label,
                                   action="delete permanently" if action == "delete" else label.lower(),
                                   confirm_key={"delete": "d", "start": "t", "stop": "s"}[action]):
                self.mutate(label + " · " + vm["name"], action, "--vm-id", vm["id"],
                            *(["--confirmed", "--expected-disk-id", str(vm.get("disk_id") or "")] if action == "delete" else []))

    def recovery_actions(self, request):
        choice = self.menu("Interrupted launch", [
            ("Check for a rejected request", "Verify rejection; keep its disk for reuse or cleanup", "repair", "Recover"),
            ("Recheck and recover", "Find its VM; restore management and SSH access", "recover", "Recover"),
            ("Full details", "Saved request and any confirmed disk IDs", "details", "Inspect"),
            ("Archive request locally", "Only after checking the outcome in the Nebius console", "archive", "Advanced"),
        ], subtitle=request["name"], notes=[
            "This request may have completed and may be billing. Creating another VM in this project is paused.",
            "Recovery does not schedule a stop. Stop running VMs manually when finished.",
            "If no VM is found, inspect the project at console.nebius.com. Nothing is deleted automatically.",
        ])
        if choice == "details":
            self.message("Saved launch request", json.dumps(request, indent=2))
        elif choice == "repair":
            result = self.mutate("Checking rejected requests", "repair-rejected")
            self.notice = ("Rejected request cleared. Its boot disk will be offered on your next launch in the same project."
                           if result["resolved"] else "This request is still uncertain. Recheck and recover it; no resources were changed.")
        elif choice == "recover":
            self.mutate("Recovering interrupted launch", "recover", "--request-id", request["plan_id"])
        elif self.confirm_launch([
            "Archive " + request["name"] + " locally?",
            "Confirm only if you checked this request in the Nebius console. Any VM or disk remains billable until you stop/delete it there.",
            "This removes the duplicate-launch guard. No cloud resources will be changed. The request remains saved in archived-requests.",
        ], json.dumps(request, indent=2), action="archive request", title="Archive interrupted request"):
            self.mutate("Archiving request", "archive-request", "--request-id", request["plan_id"], "--confirmed")

    def disk_actions(self, disk):
        choice = self.menu(disk["name"], [
            ("Reuse on next launch", "How to use this boot disk without creating another", "reuse", "Saved boot disk"),
            ("Full details", "Ownership, project and resource ID", "details", "Saved boot disk"),
            ("Delete unused boot disk", "Permanent data deletion; ends this disk's storage charges", "delete", "Cleanup"),
        ], subtitle=f"{disk['disk_gib']} GiB · {disk['project']['region']}",
           notes=["No VM was created. This unused disk remains billable until deleted."])
        if choice == "details":
            self.message("Boot disk details", json.dumps(disk, indent=2))
        elif choice == "reuse":
            self.message("Reuse boot disk", f"Get a GPU and choose {disk['project']['project_name']} in {disk['project']['region']}. "
                         "The review will offer this compatible disk instead of allocating another.")
        else:
            if self.confirm_launch([
                "Permanently delete " + disk["name"] + "\nProject: " + disk["project"]["project_name"],
                "Disk: " + disk["disk_id"], f"{disk['disk_gib']} GiB. All data will be lost. This cannot be undone.",
                "Deletion is refused if the disk is attached, protected, or no longer verified as this plugin's saved disk.",
            ], json.dumps(disk, indent=2), title="Delete boot disk", action="delete permanently", confirm_key="d"):
                self.mutate("Deleting unused boot disk", "delete-disk", "--disk-id", disk["disk_id"], "--confirmed")

    def overview(self, jump=False):
        refresh = not self.inventory
        selected_id, selected_index = None, 0
        while True:
            if refresh:
                self.inventory = self.read("Discovering your VMs", "list", "--refresh")
                refresh = False
            vms = [vm for vm in self.inventory.get("vms", []) if not jump or vm["state"] == "running"]
            rows = [(f"{vm['name']} · {vm['state'].upper()}" + (" · saved state" if vm.get("stale") else ""),
                     f"{catalog.gpu_name(vm['platform'])} · {vm['region']} · {allocation_label(vm['allocation'])}\n"
                     f"Project: {vm['project_name']}", vm, "Virtual machines") for vm in vms]
            if not jump:
                rows += [(item["name"] + " · LAUNCH UNCONFIRMED", item["project"]["region"] + " · check this request; not a VM health status", {"recovery": item}, "Launch requests")
                         for item in self.inventory.get("recovery", [])]
                rows += [(item["name"] + " · BOOT DISK AVAILABLE", "No VM created · " + item["project"]["project_name"] + " · ready to reuse",
                          {"reusable_disk": item}, "Saved boot disks") for item in self.inventory.get("reusable_disks", [])]
            if not rows:
                rows = [("Get a GPU VM", "No running VMs in your projects" if jump else "No VMs found in your projects", "__get")]
            notes = [self.notice] if self.notice else []
            if self.active_job():
                notes += ["Operation in progress · A to follow it"]
            if self.inventory.get("errors"):
                notes += ["Some projects could not be read. R retries; D shows details."]
            try:
                selected_index = next((index for index, row in enumerate(rows) if isinstance(row[2], dict)
                                       and row[2].get("id") == selected_id), min(selected_index, len(rows) - 1)) if selected_id else selected_index
                choice = self.menu("Jump into a VM" if jump else "Your VMs", rows,
                    subtitle=f"{len(vms)} {'running ' if jump else ''}VMs · {self.snapshot_note(self.inventory)}",
                    notes=notes, selected=selected_index,
                    actions={"p": lambda vm: {"vm_action": "ports", "vm": vm},
                             "d": lambda vm: {"vm_action": "delete", "vm": vm},
                             "s": lambda vm: {"vm_action": "stop", "vm": vm},
                             "t": lambda vm: {"vm_action": "start", "vm": vm},
                             "c": lambda vm: {"vm_action": "connect", "vm": vm},
                             "a": "__activity", "r": "__refresh", "g": "__get", "i": "__errors", "m": lambda vm: {"manage": vm}},
                    footer="↑↓ / j k move   " + ("Enter SSH" if jump else "Enter actions") + "   P ports   D delete   S stop   T start   C SSH   A activity   R refresh   G GPU   I info   M actions   Esc home")
                target = choice.get("vm", choice.get("manage", choice)) if isinstance(choice, dict) else None
                if isinstance(target, dict) and target.get("id"):
                    selected_id = target["id"]
                    selected_index = next((index for index, row in enumerate(rows) if row[2] == target), selected_index)
                if choice == "__get":
                    self.capacity_flow(True)
                    refresh = True
                elif choice == "__capacity":
                    self.capacity_flow()
                elif choice == "__refresh":
                    refresh = True
                elif choice == "__activity":
                    self.activity()
                    refresh = True
                elif choice == "__errors":
                    self.message("Discovery details", json.dumps(self.inventory.get("errors") or ["All personal projects loaded."], indent=2))
                elif choice == "__overview":
                    jump = False
                elif isinstance(choice, dict) and choice.get("recovery"):
                    try:
                        self.recovery_actions(choice["recovery"])
                    except Back:
                        pass
                    refresh = True
                elif isinstance(choice, dict) and choice.get("reusable_disk"):
                    try:
                        self.disk_actions(choice["reusable_disk"])
                    except Back:
                        pass
                    refresh = True
                elif isinstance(choice, dict) and choice.get("manage"):
                    item = choice["manage"]
                    try:
                        if isinstance(item, dict) and item.get("id"):
                            self.vm_actions(item)
                        elif isinstance(item, dict) and item.get("reusable_disk"):
                            self.disk_actions(item["reusable_disk"])
                        elif isinstance(item, dict) and item.get("recovery"):
                            self.recovery_actions(item["recovery"])
                    except Back:
                        pass
                    refresh = True
                elif isinstance(choice, dict) and choice.get("vm_action"):
                    item = choice["vm"]
                    try:
                        if isinstance(item, dict) and item.get("id"):
                            self.vm_actions(item, action=choice["vm_action"])
                        elif choice["vm_action"] == "ports":
                            self.ports()
                        elif isinstance(item, dict) and item.get("reusable_disk") and choice["vm_action"] == "delete":
                            self.disk_actions(item["reusable_disk"])
                        else:
                            self.message("Choose a VM", "Highlight a VM to use this action.")
                    except Back:
                        pass
                    refresh = True
                elif jump:
                    try:
                        if choice.get("recovery_id"):
                            self.vm_actions(choice)
                            refresh = True
                        else:
                            self.ssh(choice)
                    except Back:
                        pass
                else:
                    try:
                        self.vm_actions(choice)
                    except Back:
                        pass
                    refresh = True
            except Back:
                return
            except Background:
                refresh = True
            except core.NebiusError as error:
                self.show_error(error)

    def shortcut_form(self, initial=None, *, error=""):
        # Extend the native two-field form: preview, modifiers, key, inline
        # conflict feedback. Keep arrows/Tab/Escape and the existing palette.
        parts = (initial or shortcuts.DEFAULT).split(" + ")
        modifiers, value = " + ".join(parts[:-1]), parts[-1]
        options = list(shortcuts.MODIFIERS)
        if modifiers not in options:
            options.append(modifiers)
        index, active, replace, issue = options.index(modifiers), 1, True, error
        while True:
            height, width = self.screen.getmaxyx()
            if height < 20 or width < 44:
                self.frame("Keyboard shortcut", "Enlarge the terminal to 44×20", "Esc cancel")
                self.screen.refresh()
                if self.key() in ("\x1b", 27):
                    raise Back()
                continue
            bottom = self.frame("Keyboard shortcut", "Open Nebius from anywhere",
                                "Tab/↑↓ fields   ←→ modifiers   Enter save   Esc cancel" + ("   F2 error details" if issue else ""))
            proposed = options[index] + " + " + value
            self.put(6, 2, shortcuts.label(proposed) + "  →  Nebius", self.accent | curses.A_BOLD)
            self.put(8, 2, ("› " if active == 0 else "  ") + "Modifiers", curses.A_BOLD)
            self.put(9, 6, "‹ " + shortcuts.label(options[index]) + " ›", self.selection if active == 0 else 0)
            self.put(11, 2, ("› " if active == 1 else "  ") + "Key", curses.A_BOLD)
            self.put(12, 6, "[ " + (value or " ") + " ]  Letter, number or F1–F24", self.selection if active == 1 else 0)
            note = issue or "Type a key. Existing shortcuts are kept; conflicts block saving."
            for y, line in enumerate(self.wrap(note, width - 6), 13 if issue else 14):
                if y <= bottom:
                    self.put(y, 2, line, self.error if issue else 0)
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", 27):
                raise Back()
            if key == curses.KEY_F2 and issue:
                self.message("Shortcut not saved", issue, error=True)
                continue
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                active, replace = 1 - active, True
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                if active == 0:
                    index = (index + (1 if key == curses.KEY_RIGHT else -1)) % len(options)
                    issue = ""
            elif key in ("\n", "\r", curses.KEY_ENTER):
                try:
                    chord = shortcuts.normalize(proposed)
                    self.frame("Saving shortcut", "Checking conflicts and applying your choice…", "Please wait")
                    self.screen.refresh()
                    return shortcuts.save(chord)
                except (core.NebiusError, OSError) as error:
                    issue = str(error)
            elif active == 1 and key in ("\x15", "\x7f", "\b", curses.KEY_BACKSPACE, curses.KEY_DC):
                value, issue, replace = "", "", True
            elif active == 1 and isinstance(key, str) and re.fullmatch("[A-Za-z0-9]", key):
                value = ("" if replace else value) + key.upper()
                value, issue, replace = value[-3:], "", False

    def keyboard_shortcuts(self):
        while True:
            current = shortcuts.configured()
            choice = self.menu("Shortcuts", [
                ("Change opening shortcut", shortcuts.label(current) + (" · opens the Nebius panel" if current else " · choose a key to open Nebius"), "change"),
                ("Disable opening shortcut" if current else "Keep shortcut disabled",
                 "Keep the bar icon; remove only the dedicated shortcut" if current else "Do not add a shortcut during setup or repair", "disable"),
            ], notes=["Super+Ctrl+1 follows the right-hand bar order. A dedicated key follows Nebius wherever you put it."],
                actions={"e": "change", "d": "disable"},
                footer="↑↓ / j k move   E change   D disable   Enter select   Esc back")
            try:
                if choice == "change":
                    result = self.shortcut_form(current)
                elif self.confirm_launch([("Remove " + shortcuts.label(current) + "?" if current else "Keep the dedicated shortcut disabled, including during setup?")
                                          + " The Nebius bar icon and all cloud resources stay unchanged."],
                                                     "", title="Disable shortcut", action="disable shortcut", confirm_key="d"):
                    result = shortcuts.save(None)
                else:
                    continue
                self.message("Shortcut saved", result)
            except Back:
                continue
            except (core.NebiusError, OSError) as error:
                self.message("Shortcut not saved", str(error), error=True)

    def account(self):
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.run([str(Path(__file__).parent.parent / "bin/nebius-setup")], check=False)
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
        self.capacity, self.inventory = {}, {}

    def run(self):
        entry = self.entry
        while True:
            try:
                if entry == "get":
                    self.capacity_flow(True)
                elif entry == "capacity":
                    self.capacity_flow()
                elif entry in {"overview", "jump"}:
                    self.overview(entry == "jump")
                elif entry == "activity":
                    self.activity()
                elif entry == "ports":
                    self.ports()
                elif entry == "connect":
                    self.ssh(self.entry_vm)
                elif entry == "account":
                    self.account()
                elif entry == "shortcuts":
                    self.keyboard_shortcuts()
            except Background:
                entry = "overview"
                continue
            except Back:
                pass
            except core.NebiusError as error:
                self.show_error(error)
            try:
                operation = core._read_json(core.OPERATION_FILE, {})
                note = "Operation running · A to follow progress" if self.active_job() else operation.get("message", "")
                if not self.active_job() and operation.get("phase") == "error":
                    note = core.explain_error(note)["message"] + " A opens details."
                entry = self.menu("GPU manager", [
                    ("[G] Get a GPU VM", "Choose a GPU, review the cost, then create and connect", "get", "Create & connect"),
                    ("[Shift+J] Jump into a VM", "Connect to a running machine over SSH", "jump", "Create & connect"),
                    ("[V] Your VMs", "View machines, connection settings and actions", "overview", "Manage"),
                    ("[C] GPU capacity", "Compare on-demand and preemptible availability", "capacity", "Manage"),
                    ("[P] SSH port forwarding", "Open remote apps locally; add, pause or remove ports", "ports", "Manage"),
                    ("[A] Activity", "Follow concurrent operations and inspect results", "activity", "Manage"),
                    ("[S] Account / reconnect", "Connect your Nebius account and tools", "account", "Account"),
                    ("[Shift+K] Shortcuts", "Choose the key that opens Nebius", "shortcuts", "Settings"),
                ], subtitle="Your projects · keyboard first", notes=[note] if note else [],
                    actions={"g": "get", "J": "jump", "v": "overview", "c": "capacity", "a": "activity", "s": "account", "p": "ports", "K": "shortcuts"},
                    footer="↑↓ / j k move   Enter select   G get GPU   Shift+J jump   V VMs   C capacity   Esc quit")
            except Back:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screen", nargs="?", default="home", choices=("home", "get", "capacity", "overview", "jump", "activity", "ports", "connect", "shortcuts"))
    parser.add_argument("--vm-id")
    parser.add_argument("--username")
    args = parser.parse_args()
    if args.screen == "connect" and not args.vm_id:
        parser.error("connect requires --vm-id")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda screen: App(screen, args.screen, args.vm_id, args.username).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
