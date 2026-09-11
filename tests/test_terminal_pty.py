"""Real curses/key decoding in a private PTY; never touches a desktop terminal."""

import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest


class NativeTerminalTests(unittest.TestCase):
    def exercise(self, scene, keys, width, height):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        process = subprocess.Popen([sys.executable, __file__, "--scene", scene], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm-256color", "LINES": str(height), "COLUMNS": str(width)},
                                   start_new_session=True)
        os.close(slave)
        output = b""
        sent = False
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    if not sent and b"PTY-test" in output:
                        os.write(master, keys)
                        sent = True
                if process.poll() is not None:
                    # Drain the PTY's final result after curses restores modes.
                    if not select.select([master], [], [], 0)[0]:
                        break
            if process.poll() is None:
                process.wait(timeout=1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            os.close(master)
        self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
        return output.decode(errors="replace")

    def test_real_curses_decodes_arrows_in_private_terminal(self):
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            with self.subTest(width=width):
                output = self.exercise("arrows", b"\x1bOB\n", width, height)
                self.assertIn("RESULT:two", output)

    def test_real_curses_delete_cancel_never_calls_mutation(self):
        output = self.exercise("delete-cancel", b"\x1bOF\n\n", 48, 20)
        self.assertIn("RESULT:cancelled", output)
        self.assertIn("Cancel", output)


def scene(name):
    import curses
    from unittest.mock import patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
    import nebius_ui as ui

    def run(screen):
        app = ui.App(screen)
        with patch.object(app, "mutate", side_effect=AssertionError("Cloud mutation is forbidden in a PTY test")), \
             patch.object(app, "read", side_effect=AssertionError("Cloud reads are forbidden in a PTY test")):
            if name == "arrows":
                return app.menu("PTY-test", [("One", "First VM", "one"), ("Two", "Second VM", "two")])
            vm = {"id": "computeinstance-synthetic", "name": "PTY-test", "state": "running", "region": "eu-north1",
                  "allocation": "preemptible", "project_name": "synthetic", "managed": True, "can_delete": True}
            app.vm_actions(vm)
            return "cancelled"
    result = curses.wrapper(run)
    print("RESULT:" + str(result), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--scene":
        scene(sys.argv[2])
    else:
        unittest.main()
