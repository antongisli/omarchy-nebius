"""A position-independent Nebius panel shortcut in Omarchy's user bindings.

Only our marked block is edited. Existing bindings are never replaced implicitly.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import nebius_core as core

START = "-- >>> independent Nebius plugin shortcuts >>>"
END = "-- <<< independent Nebius plugin shortcuts <<<"
TAG = "-- Nebius panel shortcut: "
DESCRIPTION = "Nebius: open panel"
DEFAULT = "SUPER + CTRL + M"
MODIFIERS = ("SUPER + CTRL", "SUPER + SHIFT", "SUPER + ALT", "SUPER + CTRL + SHIFT", "SUPER + CTRL + ALT", "SUPER")
MASKS = {"SUPER": 64, "CTRL": 4, "SHIFT": 1, "ALT": 8}


class ShortcutError(core.NebiusError):
    pass


def normalize(value):
    parts = [part.strip().upper() for part in str(value).split("+")]
    key = parts[-1]
    modifiers = parts[:-1]
    if (not modifiers or len(set(modifiers)) != len(modifiers) or "SUPER" not in modifiers
            or any(part not in MASKS for part in modifiers)
            or not re.fullmatch(r"[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])", key)):
        raise ShortcutError("Choose Super with optional Ctrl, Shift or Alt, and a letter, number or F1–F24.")
    return " + ".join([part for part in MASKS if part in modifiers] + [key])


def label(value):
    return value.title().replace(" + ", "+") if value else "Not set"


def path():
    return Path.home() / ".config/hypr/bindings.lua"


def read(*, uninstall=False):
    file = path()
    try:
        text = file.read_text() if file.exists() else ""
    except OSError as error:
        raise ShortcutError("Cannot read your user bindings. Check bindings.lua and retry.") from error
    if file.is_symlink() or file.parent.is_symlink():
        if uninstall and START not in text and END not in text:
            return ""  # An unrelated dotfile symlink does not block removal.
        raise ShortcutError("Your bindings file is symlinked. Manage its shortcut manually; it was not changed.")
    return text


def split(text):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == START]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if not starts and not ends:
        return text, "", ""
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ShortcutError("The marked Nebius shortcut block is incomplete or duplicated. Repair it before changing shortcuts.")
    return "".join(lines[:starts[0]]), "".join(lines[starts[0]:ends[0] + 1]), "".join(lines[ends[0] + 1:])


def configured(text=None):
    block = split(read() if text is None else text)[1]
    if not block:
        return None
    values = [line[len(TAG):] for line in block.splitlines() if line.startswith(TAG)]
    if len(values) != 1:
        raise ShortcutError("Existing Nebius shortcuts were added manually. Keep or remove that marked block in bindings.lua first.")
    chord = normalize(values[0]) if values[0] != "disabled" else None
    if block != render(chord):
        raise ShortcutError("The Nebius shortcut block was edited manually. Keep or remove that block before using this editor.")
    return chord


def render(chord):
    block = START + "\n" + TAG + (chord or "disabled") + "\n"
    if chord:
        block += f'o.bind({json.dumps(chord)}, {json.dumps(DESCRIPTION)}, "omarchy-shell shell toggle nebius")\n'
    return block + END + "\n"


def environment():
    env = dict(os.environ)
    env.setdefault("OMARCHY_PATH", "/usr/share/omarchy")
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        session = run(["systemctl", "--user", "show-environment"], env)
        for line in session.splitlines():
            name, _, value = line.partition("=")
            if name in {"HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"}:
                env[name] = value
    return env


def run(args, env, *, input=None):
    try:
        result = subprocess.run(args, input=input, env=env, text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShortcutError(f"Cannot run {args[0]}. Check your Omarchy desktop session and retry.") from error
    if result.returncode:
        raise ShortcutError(f"{args[0]} failed: " + (result.stderr or result.stdout).strip()[:500])
    return result.stdout


def live(env):
    try:
        rows = json.loads(run(["hyprctl", "-j", "binds"], env))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError()
        return rows
    except ValueError as error:
        raise ShortcutError("Cannot read live keyboard bindings. Run from your logged-in Omarchy desktop.") from error


def matching(chord, rows, menu=""):
    parts = normalize(chord).split(" + ")
    mask, key = sum(MASKS[part] for part in parts[:-1]), parts[-1]
    result = []
    for row in rows:
        if row.get("modmask") != mask:
            continue
        actual = str(row.get("key", "")).upper()
        code = row.get("keycode", 0)
        if code:
            # Physical number-row keys used by Omarchy's numbered panels.
            actual = str(code - 9) if 10 <= code <= 18 else "0" if code == 19 else actual
        if not actual:
            # Lua code: bindings currently omit their key in hyprctl. Omarchy's
            # own menu resolves their source, using description + modifiers.
            candidates = []
            for line in menu.splitlines():
                lhs, separator, description = line.partition("→")
                if separator and description.strip() == row.get("description"):
                    fields = lhs.replace("+", " ").split()
                    if fields and sum(MASKS.get(part, 0) for part in fields[:-1]) == mask:
                        candidates.append(fields[-1].upper())
            if not candidates:
                raise ShortcutError("Cannot identify a live shortcut with these modifiers. Check Omarchy keybindings before saving.")
            if key in candidates:
                result.append(row)
        elif actual == key:
            result.append(row)
        elif code and not (10 <= code <= 19):
            raise ShortcutError("A physical-key shortcut could conflict. Choose different modifiers or inspect your bindings manually.")
    return result


def atomic_write(text):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(file.stat().st_mode) if file.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".nebius-shortcut-", dir=file.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(name, mode)
        os.replace(name, file)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def config_ok(env):
    try:
        errors = json.loads(run(["hyprctl", "-j", "configerrors"], env))
    except ValueError as error:
        raise ShortcutError("Cannot check Hyprland configuration errors. Nothing was saved.") from error
    if not isinstance(errors, list) or any(str(error).strip() for error in errors):
        raise ShortcutError("Hyprland reports configuration errors. Fix those before changing shortcuts.")


def change(chord, *, uninstall=False):
    chord = normalize(chord) if chord else None
    original = read(uninstall=uninstall)
    before, block, after = split(original)
    if uninstall and not block:
        return "No plugin shortcuts to remove."
    old = None if uninstall else configured(original)
    env = environment()
    config_ok(env)
    rows = live(env)
    if chord:
        menu = run(["omarchy", "menu", "keybindings", "--print"], env)
        conflicts = matching(chord, rows, menu)
        if old == chord:
            own = next((row for row in conflicts if row.get("description") == DESCRIPTION), None)
            if own:
                conflicts.remove(own)
        if conflicts:
            names = ", ".join(row.get("description") or "another action" for row in conflicts)
            raise ShortcutError(f"{label(chord)} is already used by {names}. Choose another key; nothing was changed.")
    # An explicit disabled marker prevents setup from re-enabling a user's choice.
    new_block = "" if uninstall else render(chord)
    updated = before + ("\n" if before and not before.endswith("\n") and new_block else "") + new_block + after
    run(["luac", "-p", "-"], env, input=updated)
    if read() != original:
        raise ShortcutError("Your bindings changed while saving. Reopen Shortcuts and retry.")
    existed = path().exists()
    atomic_write(updated)
    try:
        run(["hyprctl", "reload"], env)
        config_ok(env)
        current = live(env)
        own = [row for row in current if row.get("description") == DESCRIPTION]
        if chord:
            if len(own) != 1 or len(matching(chord, own)) != 1:
                raise ShortcutError("The new shortcut did not become active.")
        elif own:
            raise ShortcutError("The removed shortcut is still active.")
    except ShortcutError as error:
        if read() != updated:
            raise ShortcutError("Verification failed and another edit appeared. Your newer file was preserved; inspect bindings.lua.") from error
        if existed:
            atomic_write(original)
        else:
            path().unlink()
        run(["hyprctl", "reload"], env)
        raise ShortcutError(f"{error} Previous bindings were restored; retry or choose another key.") from error
    return f"{label(chord)} opens Nebius, wherever its icon is placed." if chord else "Dedicated Nebius shortcut is disabled. The bar icon still works."


def save(chord):
    with core.installation_guard():
        with (core.STATE_DIR / "shortcuts.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ShortcutError("Another shortcut change is running. Try again in a moment.") from error
            return change(chord)


def main():
    try:
        args = sys.argv[1:]
        if args == ["ensure-default"]:
            if split(read())[1]:
                print("Kept your existing shortcut choice. Shift+K in Nebius changes it.")
            else:
                print(save(DEFAULT))
        elif args == ["check-uninstall"]:
            if split(read(uninstall=True))[1]:
                config_ok(environment())
        elif args == ["remove-for-uninstall"]:
            print(change(None, uninstall=True))
        elif len(args) == 2 and args[0] == "set":
            print(save(args[1]))
        else:
            raise ShortcutError("Expected ensure-default, set <shortcut>, check-uninstall or remove-for-uninstall.")
    except (core.NebiusError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
