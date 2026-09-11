#!/usr/bin/env python3
"""Nebius: capacity, GPU launch, VM overview and SSH in one keyboard interface.

THESIS: keep the resource and the next action visible from GPU choice to SSH.
OWN-WORLD: Nebius lime selected rows, blue ink, Omarchy's terminal font and ground.
STORY: choose GPU and allocation; name/place; review; follow progress; connect.
FIRST VIEWPORT: title and scope, searchable resource list, selection details,
and a persistent keyboard footer. No fixed-width content or floating overlays.
FORM: existing Omarchy terminal conventions, explicitly requested by the user.
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


class App:
    def __init__(self, screen, entry="overview"):
        self.screen = screen
        self.entry = entry
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
        while True:
            filtered = [row for row in rows if query.casefold() in f"{row[0]} {row[1]}".casefold()]
            selected = min(max(0, selected), max(0, len(filtered) - 1))
            hints = footer or "↑↓ / j k move   Enter select   / search   Esc back"
            if "?" not in hints:
                hints += "   ? help"
            escape_hint = next((part for part in re.split(r"\s{2,}", hints) if part.startswith("Esc ")), "Esc back")
            enter_hint = next((part for part in re.split(r"\s{2,}", hints) if part.startswith("Enter ")), "Enter select")
            compact = "↑↓ / j k move   " + enter_hint + "   " + escape_hint
            context_keys = (["P switch"] if "p" in actions else []) + (["G GPU"] if "g" in actions and title != "GPU manager" else [])
            context_keys += (["M actions"] if "m" in actions else []) + (["R refresh"] if "r" in actions else []) + ["/ search", "? help"]
            compact += "\n" + "   ".join(context_keys)
            bottom = self.frame(title, subtitle, compact, allocation=self.allocation if "p" in actions else None)
            height, width = self.screen.getmaxyx()
            y = 7 if "p" in actions else 6
            note_lines = [line for note in notes for line in self.wrap(note, width - 8)]
            note_slots = max(0, min(3, bottom - y - 7))
            clipped_notes = len(note_lines) > note_slots
            for line in note_lines[:max(0, note_slots - (1 if clipped_notes else 0))]:
                self.put(y, 4, line)
                y += 1
            if clipped_notes:
                self.put(y, 4, "[I] Read full summary", self.accent)
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
                    label = ("› " if index == selected else "  ") + fit(text, width - 7)
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
                text += "\n  [PgUp/PgDn] page\n  [Home/End] first / last\n  Search: [Ctrl+U] clear, [Enter] finish"
                self.message("Menu help", text)
            elif key in ("i", "I") and clipped_notes:
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

    def confirm_launch(self, notes, details, *, action="create VM and start billing", title="Review VM"):
        """Every billing term must be displayed before Enter can submit."""
        offset = 0
        while True:
            bottom = self.frame(title, "Review every page before confirming",
                                "Enter next page / confirm at end   D details   ↑↓ scroll   Esc back")
            lines = self.wrap("\n\n".join(notes))
            slots = max(1, bottom - 8)
            offset = min(offset, max(0, len(lines) - slots))
            for y, line in enumerate(lines[offset:offset + slots], 5):
                self.put(y, 2, line)
            at_end = offset + slots >= len(lines)
            self.put(bottom - 2, 2, "Enter: " + action if at_end else "Enter: read next page", self.accent | curses.A_BOLD)
            self.put(bottom - 1, 2, f"Lines {offset + 1}–{min(len(lines), offset + slots)} of {len(lines)}")
            self.screen.refresh()
            key = self.key()
            if key in ("\x1b", "q"):
                return False
            if key in ("\n", "\r", curses.KEY_ENTER):
                if at_end:
                    return True
                offset += slots
            elif key in ("d", "D"):
                self.message("Launch details", details)
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
        operation = core._read_json(core.OPERATION_FILE, {}) if mutation else {}
        spinner = "|/-\\"[elapsed % 4]
        bottom = self.frame(title, f"{spinner}  {elapsed // 60}:{elapsed % 60:02d} elapsed",
                            "B continue in background   This operation survives closing the window" if mutation else "Esc cancel this read")
        y = 7
        for line in self.wrap(operation.get("message") or title):
            self.put(y, 3, line, self.accent | curses.A_BOLD)
            y += 1
        if mutation:
            stages = {"checking": 0, "project": 1, "network": 2, "disk": 1, "instance": 2, "boot": 3, "done": 4}
            current = stages.get(operation.get("stage"), 0)
            labels = ["Check request", "Create boot disk", "Create VM", "Wait for address", "Ready to connect"]
            if operation.get("stage") in {"project", "network"}:
                labels = ["Check request", "Create project", "Wait for network", "Project ready"]
            if operation.get("stage") in {"start", "stop", "delete"}:
                labels = ["Send request", "Wait for Nebius", "Update overview"]
                current = 1
            y += 2
            for index, label in enumerate(labels):
                if y >= bottom:
                    break
                self.put(y, 3, ("✓ " if index < current else "› " if index == current else "  ") + label,
                         self.accent if index == current else 0)
                y += 2
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
        job = core._read_json(core.STATE_DIR / "active-job.json", {})
        if job.get("phase") != "running":
            return None
        try:
            os.kill(int(job["pid"]), 0)
        except (OSError, KeyError, ValueError):
            return None
        return job

    def watch(self, job_id, title, started=None):
        started = started or time.time()
        path = core.STATE_DIR / "jobs" / f"{job_id}.json"
        while True:
            job = core._read_json(path, {})
            if job.get("phase") in {"ready", "error"}:
                if job["phase"] == "error":
                    operation = core._read_json(core.OPERATION_FILE, {}) if job.get("started_at") else {}
                    self.message("Operation stopped", operation.get("message", job.get("error", "")) + "\n\n" + operation.get("recovery", ""),
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
            if self.key() in ("b", "B"):
                self.notice = "Operation continues. Press A to follow progress."
                raise Background()

    def mutate(self, title, *arguments):
        if self.active_job():
            raise core.NebiusError("Another operation is running. Press A in the overview to follow it.")
        job_id = secrets.token_hex(12)
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (core.STATE_DIR / "worker.log").open("a") as log:
            subprocess.Popen([sys.executable, str(Path(__file__).with_name("nebius_job.py")), job_id, *arguments],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
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
                groups.setdefault(row["gpu_label"], []).append(row)
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
            offerings = [row for row in self.capacity.get("offerings", []) if row["gpu_label"] == gpu_label]
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
                refreshed = next((item for item in self.capacity["offerings"] if item["offering_id"] == offering["offering_id"]), None)
                if not refreshed or result["project_id"] not in {p["project_id"] for p in refreshed["projects"]}:
                    raise core.NebiusError("The project exists, but GPU placement is not ready yet. Refresh capacity in a moment.")
                return result["project_id"]
            except Back:
                continue
            except core.NebiusError as error:
                self.show_error(error)

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
        plan = None
        created_vm = None
        while True:
            action = self.menu("VM settings", [
                ("Name", name, "name", "Configuration"),
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
            elif action == "allocation":
                self.toggle_allocation()
            elif action == "project":
                projects = next((item["projects"] for item in self.capacity["offerings"] if item["offering_id"] == offering["offering_id"]), [])
                try:
                    project_id = self.menu("Place VM in", [(p["project_name"], p["region"], p["project_id"]) for p in projects])
                except Back:
                    pass
            else:
                try:
                    plan = self.read("Running preflight checks", "plan", "--offering-id", offering["offering_id"],
                                     "--project-id", project_id, "--name", name, "--allocation", self.allocation)
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
                    self.notice = f"{vm['name']} is running."
                    result_notes = [f"Address: {vm.get('public_ip', '')}"]
                    choice = self.menu("VM is running", [("Connect now", vm.get("public_ip", ""), "connect"),
                                                         ("Back to overview", "Your VM is saved", "overview")],
                                       subtitle=vm["name"], notes=result_notes)
                    if choice == "connect":
                        self.ssh({**vm, "managed": True, "ssh_user": core.SSH_USER})
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

    def ssh(self, vm):
        username = vm.get("ssh_user") or "ubuntu"
        if not vm.get("ssh_user"):
            username = self.edit("SSH username", username,
                                 notes=["This VM already exists. Use its login user; SSH will use your configured keys or agent."])
        connection = self.read("Preparing SSH", "connect", "--vm-id", vm["id"], "--no-launch", "--username", username)
        curses.def_prog_mode()
        curses.endwin()
        try:
            print(f"\nConnecting to {vm['name']}… Exit SSH to return to Nebius.\n", flush=True)
            result = subprocess.run(connection["command"], check=False)
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()
        if result.returncode:
            self.message("SSH ended", f"SSH exited with status {result.returncode}.\n\nCheck the username, SSH key and network access. The VM is unchanged.")

    def activity(self):
        active = self.active_job()
        if active:
            started = dt.datetime.fromisoformat(active["started_at"]).timestamp()
            self.watch(active["id"], "Active operation", started)
        operation = core._read_json(core.OPERATION_FILE, {})
        summary = core.explain_error(operation.get("message", ""))["message"] if operation.get("phase") == "error" else operation.get("message", "No operations yet")
        self.message("Last operation", summary + "\n\n" + operation.get("recovery", ""),
                     details=operation.get("details") or json.dumps(operation, indent=2), error=operation.get("phase") == "error")

    def vm_actions(self, vm):
        if vm.get("recovery_id"):
            request = next((item for item in self.inventory.get("recovery", []) if item["plan_id"] == vm["recovery_id"]), None)
            if not request:
                raise core.NebiusError("This saved recovery state changed. Refresh Your VMs")
            self.recovery_actions(request)
            return
        rows = []
        if vm["state"] == "running":
            rows += [("Connect over SSH", "Enter your running VM", "connect"), ("Stop VM", "Compute stops; disks remain billable", "stop")]
        elif vm["state"] == "stopped":
            rows += [("Start VM", "Resume billing and wait for its address", "start")]
        if not vm.get("instance_deleted"):
            rows += [("Connection settings", "Edit the saved SSH username", "settings")]
        rows = [(*row, "VM actions") for row in rows]
        if not vm.get("instance_deleted"):
            rows += [("Disks and storage", "Inspect attached disks and cleanup options", "storage", "Inspect")]
        rows += [("Full details", "Resource identifiers and state", "details", "Inspect")]
        if vm.get("can_delete"):
            rows += [("Delete remaining boot disk" if vm.get("instance_deleted") else "Delete VM and boot disk",
                      "Permanent deletion; disk charges continue until removed", "delete", "Delete")]
        action = self.menu(vm["name"], rows, subtitle=f"{vm['state']} · {vm['region']} · {allocation_label(vm['allocation'])}",
                           notes=[vm["project_name"]])
        if action == "connect":
            self.ssh(vm)
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
            choice = self.menu(label, [(label, vm["name"], True), ("Cancel", "No changes", False)],
                               selected=1, subtitle=vm["name"], notes=["Deletion is permanent. The VM and its boot disk are removed; secondary disks are kept." if action == "delete" else "Starting incurs compute charges. Stopping leaves disks billable."])
            if choice:
                if action == "delete" and not self.confirm_launch([
                    "Permanently delete " + vm["name"],
                    "Project: " + vm["project_name"], "VM: " + vm["id"],
                    "Boot disk: " + str(vm.get("disk_id") or "not recorded"),
                    "All data on this boot disk will be lost. Secondary disks are kept and remain billable.",
                ], json.dumps(vm, indent=2), title="Review deletion", action="permanently delete VM and boot disk"):
                    return
                self.mutate(label, action, "--vm-id", vm["id"], *(["--confirmed"] if action == "delete" else []))
                self.inventory = self.read("Refreshing VMs", "list", "--refresh")

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
            approved = self.menu("Delete boot disk", [("Delete permanently", disk["name"], True), ("Cancel", "Keep the disk", False)], selected=1)
            if approved and self.confirm_launch([
                "Permanently delete " + disk["name"], "Project: " + disk["project"]["project_name"],
                "Disk: " + disk["disk_id"], f"{disk['disk_gib']} GiB. All data will be lost. This cannot be undone.",
                "Deletion is refused if the disk is attached, protected, or no longer verified as this plugin's saved disk.",
            ], json.dumps(disk, indent=2), title="Review disk deletion", action="permanently delete boot disk"):
                self.mutate("Deleting unused boot disk", "delete-disk", "--disk-id", disk["disk_id"], "--confirmed")

    def overview(self, jump=False):
        refresh = not self.inventory
        while True:
            if refresh:
                self.inventory = self.read("Discovering your VMs", "list", "--refresh")
                refresh = False
            vms = [vm for vm in self.inventory.get("vms", []) if not jump or vm["state"] == "running"]
            rows = [(f"{vm['name']} · {vm['state'].upper()}" + (" · saved state" if vm.get("stale") else ""),
                     f"{vm['platform'].removeprefix('gpu-').upper()} · {vm['region']} · {allocation_label(vm['allocation'])}\n"
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
                choice = self.menu("Jump into a VM" if jump else "Your VMs", rows,
                    subtitle=f"{len(vms)} {'running ' if jump else ''}VMs · {self.snapshot_note(self.inventory)}",
                    notes=notes, actions={"g": "__get", "c": "__capacity", "r": "__refresh", "a": "__activity", "d": "__errors", "v": "__overview",
                                          "m": lambda vm: {"manage": vm}},
                    footer="↑↓ / j k move   " + ("Enter SSH" if jump else "Enter actions") + "   M actions   / search   G get GPU   R refresh   A activity   Esc home")
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
                elif entry == "account":
                    self.account()
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
                    ("[A] Last operation", "Follow progress or inspect a result or error", "activity", "Manage"),
                    ("[S] Account / reconnect", "Connect your Nebius account and tools", "account", "Account"),
                ], subtitle="Your projects · keyboard first", notes=[note] if note else [],
                    actions={"g": "get", "J": "jump", "v": "overview", "c": "capacity", "a": "activity", "s": "account"},
                    footer="↑↓ / j k move   Enter select   G get GPU   Shift+J jump   V VMs   C capacity   Esc quit")
            except Back:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screen", nargs="?", default="home", choices=("home", "get", "capacity", "overview", "jump", "activity"))
    args = parser.parse_args()
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda screen: App(screen, args.screen).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
