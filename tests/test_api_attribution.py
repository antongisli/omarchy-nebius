"""Plugin attribution stays in child processes, never in the user's shell."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libexec"))
import nebius_runtime as runtime
import nebius_core as core
import nebius_mcp_bridge as bridge

KEY = "NEBIUS_CLI_USER_AGENT_PREFIX"


class ApiAttributionTests(unittest.TestCase):
    def test_prefix_is_product_and_manifest_version_only(self):
        self.assertEqual(runtime.USER_AGENT_PREFIX, f"omarchy-nebius/{runtime.VERSION}")

    def test_child_environment_does_not_mutate_parent(self):
        with patch.dict(os.environ, {KEY: "other-client/1", "SYNTHETIC_SETTING": "keep"}):
            child = runtime.cli_environment()
            self.assertEqual(child[KEY], runtime.USER_AGENT_PREFIX)
            self.assertEqual(child["SYNTHETIC_SETTING"], "keep")
            self.assertEqual(os.environ[KEY], "other-client/1")

    def test_direct_cli_calls_receive_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "nebius"
            cli.write_text("#!/bin/sh\nprintf '{\"agent\":\"%s\"}' \"$NEBIUS_CLI_USER_AGENT_PREFIX\"\n")
            cli.chmod(0o700)
            with patch.object(core, "CLI", cli), patch.object(core, "_profile_credential_expiry", return_value=None):
                self.assertEqual(core.run_cli(["version"]), {"agent": runtime.USER_AGENT_PREFIX})

    def test_other_subprocesses_are_not_attributed(self):
        with patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            core._run(["ssh-keygen", "-h"])
            self.assertIsNone(run.call_args.kwargs["env"])

    def test_upstream_mcp_inherits_prefix_and_client_name(self):
        with patch.object(bridge.subprocess, "Popen") as popen, patch.object(bridge.threading, "Thread"), \
             patch.dict(os.environ, {KEY: "unrelated-client/1"}):
            client = bridge.McpClient()
            self.assertEqual(popen.call_args.kwargs["env"][KEY], runtime.USER_AGENT_PREFIX)
            self.assertEqual(os.environ[KEY], "unrelated-client/1")
            with patch.object(client, "request") as request, patch.object(client, "notify"):
                client.initialize()
                self.assertEqual(request.call_args.args[1]["clientInfo"],
                                 {"name": "omarchy-nebius", "version": runtime.VERSION})

    def test_regular_terminal_launcher_does_not_add_plugin_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "fake-cli"
            cli.write_text('#!/bin/sh\nprintf "%s" "${NEBIUS_CLI_USER_AGENT_PREFIX-unset}"\n')
            cli.chmod(0o700)
            environment = {"HOME": directory, "PATH": os.environ["PATH"], "NEBIUS_CLI_BIN": str(cli)}
            result = subprocess.run([str(ROOT / "bin/nebius-cli"), "version"], env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "unset")

    def test_setup_prefix_is_scoped_to_setup_process(self):
        source = (ROOT / "bin/nebius-setup").read_text()
        start = source.index('NEBIUS_CLI_USER_AGENT_PREFIX=$(python3 "$RUNTIME" user-agent)')
        end = source.index('\nCLI=', start)
        with patch.dict(os.environ, {KEY: "other-client/1"}):
            result = subprocess.run(["bash", "-ec", source[start:end] + '\nprintf "%s" "$NEBIUS_CLI_USER_AGENT_PREFIX"'],
                                    env={**os.environ, "RUNTIME": str(ROOT / "libexec/nebius_runtime.py")},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, runtime.USER_AGENT_PREFIX)
            self.assertEqual(os.environ[KEY], "other-client/1")

    def test_upgrade_keeps_old_private_cli_available_until_setup(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"HOME": directory, "XDG_DATA_HOME": directory}, clear=True):
            previous = runtime.private_cli_path(runtime.PREVIOUS_CLI_VERSION)
            previous.parent.mkdir(parents=True)
            previous.write_text("previous")
            self.assertEqual(runtime.cli_path(), previous)
            current = runtime.private_cli_path()
            current.parent.mkdir(parents=True)
            current.write_text("current")
            self.assertEqual(runtime.cli_path(), current)


if __name__ == "__main__":
    unittest.main()
