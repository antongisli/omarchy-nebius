#!/usr/bin/env python3
"""Render the production terminal menu with synthetic capacity; no cloud access.

Run from the repo root: python3 tools/render_preview.py
The SVG is a cell-for-cell rendering of App.capacity_flow(), not a UI mockup.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import time
from html import escape
from pathlib import Path
import sys
from unittest.mock import patch
import xml.etree.ElementTree as ET

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


def render(width=80, height=30, allocation="on_demand", surface="capacity"):
    if surface == "cover":
        return render_cover()
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
         patch.object(app, "mutate", side_effect=AssertionError("Preview cannot mutate cloud")), \
         patch.object(ui.inventory_view.Poller, "poll", side_effect=lambda snapshot, entries, **kwargs: snapshot):
        try:
            if surface == "port-form":
                app.port_form({"name": "inference · H100"}, remote_port=8000, local_port=18000)
            elif surface in {"shortcut-form", "shortcut-error"}:
                app.shortcut_form("SUPER + CTRL + 1" if surface == "shortcut-error" else None,
                                  error="Super+Ctrl+1 is already used by Bar panel 1. Choose another key; nothing was changed."
                                  if surface == "shortcut-error" else "")
            elif surface in {"shortcuts", "shortcuts-unset"}:
                with patch.object(ui.shortcuts, "configured", return_value=None if surface == "shortcuts-unset" else ui.shortcuts.DEFAULT):
                    app.keyboard_shortcuts()
            elif surface == "ports":
                with patch.object(ui.ports, "listing", return_value=[
                    {"id": "demo1", "vm_id": "computeinstance-demo", "vm_name": "comfyui", "enabled": True,
                     "local_port": 8188, "remote_port": 8188, "state": "Connected", "url": "http://127.0.0.1:8188"},
                    {"id": "demo2", "vm_id": "computeinstance-demo2", "vm_name": "inference", "enabled": True,
                     "local_port": 18000, "remote_port": 8000, "state": "Reconnecting", "url": "http://127.0.0.1:18000"},
                ]):
                    app.ports()
            elif surface in {"launch-ready", "launch-progress"}:
                operation = {}
                base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=118.2)
                for phase, stage, elapsed in [("running", "checking", 0), ("running", "preflight", 0.5),
                        ("running", "disk", 4), ("running", "instance", 30), ("running", "boot", 80),
                        ("running", "ssh", 100), ("ready", "done", 118.2)]:
                    if surface == "launch-progress" and phase == "ready":
                        break
                    operation = {**ui.timing.advance(operation, phase, stage, (base + dt.timedelta(seconds=elapsed)).isoformat()),
                                 "phase": phase, "stage": stage}
                operation.update(name="training", vm_id="computeinstance-example",
                                 message="SSH login verified" if surface == "launch-ready" else "Waiting for authenticated SSH login")
                if surface == "launch-ready":
                    operation["ssh_ready"] = True
                    app.operation_result({"operation": operation, "result": {"id": "computeinstance-example", "name": "training"}})
                else:
                    with patch.object(ui.core, "_read_json", return_value=operation):
                        app.progress("Creating VM · training", time.time() - 118.2, mutation=True)
            elif surface == "activity":
                with patch.object(ui.jobs, "jobs", return_value=[
                    {"id": "demo1", "command": "delete", "title": "Delete VM · comfyui", "phase": "running",
                     "operation": {"message": "VM deleted; deleting its boot disk"}},
                    {"id": "demo2", "command": "create", "phase": "ready", "result": {"name": "inference"}},
                    {"id": "demo3", "command": "stop", "phase": "error", "title": "Stop VM · experiment",
                     "error": "Connection timed out. Check the VM state before retrying."},
                ]):
                    app.activity()
            elif surface in {"overview", "overview-deleting", "vm-actions", "delete-review", "home"}:
                vm = {"id": "computeinstance-example", "name": "comfyui", "state": "running", "region": "eu-north1",
                      "allocation": "on_demand", "project_name": "personal", "platform": "gpu-h100-sxm",
                      "managed": True, "can_delete": True, "ssh_user": "dev", "disk_id": "computedisk-example"}
                app.inventory = {"vms": [vm, {**vm, "id": "computeinstance-second", "name": "inference", "state": "stopped"}], "source": "live"}
                with patch.object(ui.jobs, "jobs", return_value=[{"id": "a" * 24, "command": "delete", "phase": "running",
                        "arguments": ["delete", "--vm-id", vm["id"]]}] if surface == "overview-deleting" else []), \
                     patch.object(ui.core, "_read_json", return_value={}):
                    if surface in {"overview", "overview-deleting"}:
                        app.overview()
                    elif surface == "home":
                        app.entry = "home"
                        app.run()
                    else:
                        app.vm_actions(vm, action="delete" if surface == "delete-review" else None)
            else:
                app.capacity_flow(launch=True)
        except FrameReady:
            pass
    cell_w, cell_h = 10, 22
    content = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * cell_w}" height="{height * cell_h}" viewBox="0 0 {width * cell_w} {height * cell_h}">',
               '<title>Nebius: production menu, synthetic example data</title>',
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


def render_cover():
    """Keep the branded cover, with two current production terminal screens."""
    def panel(width, height, surface, x, y):
        # Qt's SVG Tiny renderer silently drops nested <svg> elements. Flatten
        # the terminal viewport into a translated group, keeping its cell sizes.
        document = ET.fromstring(render(width, height, surface=surface))
        namespace = "http://www.w3.org/2000/svg"
        ET.register_namespace("", namespace)
        group = ET.Element(f"{{{namespace}}}g", {"transform": f"translate({x} {y})"})
        for child in document:
            # Percentages would otherwise refer to the entire cover viewport.
            if child.tag == f"{{{namespace}}}rect" and child.get("width") == "100%":
                child.set("width", document.attrib["width"])
                child.set("height", document.attrib["height"])
            group.append(child)
        return ET.tostring(group, encoding="unicode")

    capacity = panel(80, 28, "capacity", 32, 196)
    form = panel(52, 28, "port-form", 872, 196)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="896" viewBox="0 0 1440 896">'
            '<title>Nebius GPU: capacity and SSH ports, production interface with example data</title>'
            '<rect width="1440" height="896" fill="#E0FF4F"/>'
            '<text x="40" y="94" font-family="sans-serif" font-size="62" font-weight="700" fill="#052B42">Need a bigger GPU? Hit a key.</text>'
            '<text x="42" y="143" font-family="sans-serif" font-size="27" fill="#052B42">Simple launch. Simple SSH. Simple port forwarding.</text>'
            '<rect x="24" y="181" width="1392" height="649" fill="#101820"/>'
            + capacity + form +
            '<path d="M848 207V804" stroke="#35454E"/>'
            '<text x="40" y="871" font-family="monospace" font-size="17" fill="#052B42">NEBIUS GPU / OMARCHY</text>'
            '<text x="900" y="871" font-family="monospace" font-size="17" fill="#052B42">REAL INTERFACE · EXAMPLE DATA</text></svg>\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=30)
    parser.add_argument("--allocation", choices=("on_demand", "preemptible"), default="on_demand")
    parser.add_argument("--surface", choices=("cover", "capacity", "ports", "port-form", "launch-ready", "launch-progress", "activity", "overview", "overview-deleting", "vm-actions", "delete-review", "home", "shortcuts", "shortcuts-unset", "shortcut-form", "shortcut-error"), default="capacity")
    parser.add_argument("--output", type=Path, default=ROOT / "assets/terminal-preview.svg")
    args = parser.parse_args()
    target = args.output
    drawing = render(args.width, args.height, args.allocation, args.surface)
    if target.suffix.lower() == ".png":
        import cairosvg  # Optional build dependency, never needed by the plugin.
        cairosvg.svg2png(bytestring=drawing.encode("utf-8"), write_to=str(target))
    else:
        target.write_text(drawing, encoding="utf-8")
    print(target)
