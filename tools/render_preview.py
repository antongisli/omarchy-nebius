#!/usr/bin/env python3
"""Render the production terminal menu with synthetic capacity; no cloud access.

Run from the repo root: python3 tools/render_preview.py
The SVG is a cell-for-cell rendering of App.capacity_flow(), not a UI mockup.
"""

from __future__ import annotations

import argparse
import curses
from html import escape
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libexec"))
import nebius_ui as ui


class FrameReady(Exception):
    pass


class PreviewScreen:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.draws = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, _value):
        pass

    def timeout(self, _value):
        pass

    def erase(self):
        self.draws = []

    def addstr(self, y, x, text, attr=0):
        self.draws.append((y, x, text, attr))

    def refresh(self):
        pass

    def get_wch(self):
        raise FrameReady()


def render(width=80, height=30, allocation="on_demand"):
    screen = PreviewScreen(width, height)
    with patch.object(curses, "has_colors", return_value=False), patch.object(curses, "curs_set"):
        app = ui.App(screen)
    # Distinct attributes let the renderer preserve the production color roles.
    app.accent = 1 << 40
    app.selection = 1 << 41
    app.allocation = allocation
    app.capacity = {"source": "live", "offerings": [
        {"gpu_label": label, "gpu_count": count, "region": region,
         "on_demand": {"available": available}, "preemptible": {"available": preemptible}}
        for label, count, region, available, preemptible in [
            ("NVIDIA H100", 1, "eu-north1", 8, 1),
            ("NVIDIA H100", 8, "eu-west1", 2, 0),
            ("NVIDIA H200", 1, "eu-north1", 4, 0),
            ("NVIDIA H200", 8, "eu-west1", 1, 2),
            ("NVIDIA RTX 6000 Ada", 1, "uk-south2", 0, 0),
        ]
    ]}
    with patch.object(app, "read", side_effect=AssertionError("Preview cannot query cloud")), \
         patch.object(app, "mutate", side_effect=AssertionError("Preview cannot mutate cloud")):
        try:
            app.capacity_flow(launch=True)
        except FrameReady:
            pass
    cell_w, cell_h = 10, 22
    content = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * cell_w}" height="{height * cell_h}" viewBox="0 0 {width * cell_w} {height * cell_h}">',
               '<title>Nebius GPU picker: production menu, synthetic example data</title>',
               '<rect width="100%" height="100%" fill="#101820"/>']
    for y, x, text, attr in screen.draws:
        selected = bool(attr & app.selection)
        color = "#052B42" if selected else "#E0FF4F" if attr & app.accent else "#EDF2F5"
        if selected:
            content.append(f'<rect x="{x * cell_w}" y="{y * cell_h}" width="{ui.cell_width(text) * cell_w}" height="{cell_h}" fill="#E0FF4F"/>')
        weight = "700" if attr & curses.A_BOLD else "400"
        # Position each glyph to keep terminal cell geometry independent of font metrics.
        cursor = x * cell_w
        for char in text:
            if char != " ":
                content.append(f'<text x="{cursor}" y="{y * cell_h + 16}" font-family="monospace" font-size="16" font-weight="{weight}" fill="{color}">{escape(char)}</text>')
            cursor += ui.cell_width(char) * cell_w
    content.append('</svg>')
    return "\n".join(content) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=30)
    parser.add_argument("--allocation", choices=("on_demand", "preemptible"), default="on_demand")
    parser.add_argument("--output", type=Path, default=ROOT / "assets/terminal-preview.svg")
    args = parser.parse_args()
    target = args.output
    target.write_text(render(args.width, args.height, args.allocation), encoding="utf-8")
    print(target)
