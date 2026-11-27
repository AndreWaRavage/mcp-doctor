#!/usr/bin/env python3
"""
Test suite for mcp-doctor. Standard library only:

    python tests.py            # or: python -m unittest -v tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mcp_doctor as md  # noqa: E402


def write_temp(body: str, name: str = "mcp.json") -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=name + "-", delete=False, encoding="utf-8"
    )
    handle.write(textwrap.dedent(body))
    handle.close()
    return handle.name


def write_script(body: str, name: str = "server.py") -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".py", prefix=name + "-", delete=False, encoding="utf-8"
    )
    handle.write(textwrap.dedent(body))
    handle.close()
    return handle.name


class LintTests(unittest.TestCase):
    def lint(self, config: str):
        parser = md.build_parser()
        args = parser.parse_args(["lint", config, "--no-color"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = md.cmd_lint(args)
        return code, buffer.getvalue()

    def codes(self, config: str):
        data, findings = md.load_config(config)
        specs = md.to_specs(md.find_config_wrapper(data)[0] or {}, findings) if data else []
        md.lint(specs, findings)
        return {f.code for f in findings}, findings

    def test_demo_config_is_clean(self):
        code, output = self.lint(os.path.join(HERE, "demo_config.json"))
        self.assertEqual(code, 0, output)
        self.assertIn("findings (0)", output)
        self.assertIn("servers (1)", output)

    def test_broken_json(self):
        path = write_temp('{"mcpServers": {"a": {},}}')
        code, output = self.lint(path)
        self.assertEqual(code, 1)
        self.assertIn("MD002", output)

    def test_missing_wrapper(self):
        path = write_temp('{"other": 1}')
        code, output = self.lint(path)
        self.assertEqual(code, 1)
        self.assertIn("MD003", output)

    def test_command_not_on_path(self):
        path = write_temp(
            '{"mcpServers": {"ghost": {"command": "definitely-not-a-real-binary-xyz"}}}'
        )
        codes, findings = self.codes(path)
        self.assertIn("MD009", codes)
        self.assertTrue(any(f.severity == "error" for f in findings))

    def test_missing_path_argument(self):
        path = write_temp(
            '{"mcpServers": {"fs": {"command": "python", '
            '"args": ["-u", "no/such/file.py"]}}}'
        )
        codes, _ = self.codes(path)
        self.assertIn("MD012", codes)

    def test_npx_without_yes(self):
        path = write_temp(
            '{"mcpServers": {"fs": {"command": "npx", '
            '"args": ["@modelcontextprotocol/server-filesystem", "/tmp"]}}}'
        )
        codes, _ = self.codes(path)
        self.assertIn("MD015", codes)

    def test_npx_with_yes_is_fine(self):
        path = write_temp(
            '{"mcpServers": {"fs": {"command": "npx", '
            '"args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}}}'
        )
        codes, _ = self.codes(path)
        self.assertNotIn("MD015", codes)

    def test_unset_env_placeholder(self):
        path = write_temp(
            '{"mcpServers": {"api": {"command": "python", '
            '"env": {"TOKEN": "${MCP_DOCTOR_UNSET_VAR}"}}}}'
        )
        codes, _ = self.codes(path)
        self.assertIn("MD013", codes)

    def test_command_and_url_conflict(self):
        path = write_temp(
            '{"mcpServers": {"both": {"command": "python", "url": "https://example.com/mcp"}}}'
        )
        codes, _ = self.codes(path)
        self.assertIn("MD005", codes)

    def test_remote_url_is_accepted(self):
        path = write_temp('{"servers": {"remote": {"url": "https://example.com/mcp"}}}')
        data, findings = md.load_config(path)
        specs = md.to_specs(md.find_config_wrapper(data)[0], findings)
        md.lint(specs, findings)
        self.assertEqual(specs[0].transport, "http")
        self.assertEqual([f for f in findings if f.severity == "error"], [])

    def test_duplicate_launch_line(self):
        path = write_temp(
            '{"mcpServers": {'
            '"one": {"command": "python", "args": ["-u", "x.py"]},'
            '"two": {"command": "python", "args": ["-u", "x.py"]}}}'
        )
        codes, _ = self.codes(path)
        self.assertIn("MD016", codes)

    def test_json_output_is_parseable(self):
        path = write_temp('{"mcpServers": {"ghost": {"command": "nope-xyz-binary"}}}')
        parser = md.build_parser()
        args = parser.parse_args(["lint", path, "--format", "json"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            md.cmd_lint(args)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["summary"]["errors"], 1)
        self.assertEqual(payload["findings"][0]["code"], "MD009")

    def test_markdown_output(self):
        parser = md.build_parser()
        args = parser.parse_args(["lint", os.path.join(HERE, "demo_config.json"), "--format", "markdown"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            md.cmd_lint(args)
        self.assertIn("| server | transport | launch |", buffer.getvalue())


class ProbeTests(unittest.TestCase):
    def probe(self, config: str, timeout: float = 15.0):
        parser = md.build_parser()
        args = parser.parse_args(
            ["probe", config, "--timeout", str(timeout), "--no-color", "--format", "json"]
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = md.cmd_probe(args)
        return code, json.loads(buffer.getvalue())

    def test_probe_demo_server(self):
        code, payload = self.probe(os.path.join(HERE, "demo_config.json"))
        self.assertEqual(code, 0, payload)
        probe = payload["probes"][0]
        self.assertTrue(probe["ok"], probe)
        self.assertEqual(probe["protocol_version"], md.PROTOCOL_VERSION)
        names = sorted(t["name"] for t in probe["tools"])
        self.assertEqual(names, ["add", "echo"])
        steps = {s["name"]: s for s in probe["steps"]}
        self.assertTrue(steps["spawn"]["ok"])
        self.assertTrue(steps["initialize"]["ok"])
        self.assertTrue(steps["tools/list"]["ok"])
        self.assertGreater(steps["initialize"]["elapsed_ms"], 0)

    def test_probe_times_out_on_silent_server(self):
        script = write_script(
            """
            import sys, time
            # Read everything, answer nothing: the classic "hung server" shape.
            sys.stdin.read()
            """
        )
        config = write_temp(
            json.dumps(
                {"mcpServers": {"silent": {"command": sys.executable, "args": ["-u", script]}}}
            )
        )
        code, payload = self.probe(config, timeout=2.0)
        self.assertEqual(code, 1)
        probe = payload["probes"][0]
        self.assertFalse(probe["ok"])
        self.assertIn("timed out", probe["error"])

    def test_probe_reports_missing_binary(self):
        config = write_temp('{"mcpServers": {"ghost": {"command": "nope-xyz-binary"}}}')
        code, payload = self.probe(config)
        self.assertEqual(code, 1)
        self.assertIn("MD009", {f["code"] for f in payload["findings"]})

    def test_probe_validates_tool_call(self):
        spec = md.ServerSpec(
            name="demo",
            raw={},
            command=sys.executable,
            args=["-u", os.path.join(HERE, "demo_server.py")],
        )
        client = md.StdioClient(spec, timeout=10)
        client.start()
        try:
            client.request(
                "initialize",
                {"protocolVersion": md.PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {}},
            )
            client.notify("notifications/initialized")
            result = client.request(
                "tools/call", {"name": "add", "arguments": {"a": 19, "b": 23}}
            )
            self.assertEqual(result["result"]["content"][0]["text"], "42.0")
        finally:
            client.stop()


class CliTests(unittest.TestCase):
    def test_no_subcommand_prints_help(self):
        code = md.main([])
        self.assertEqual(code, 2)

    def test_strict_mode_promotes_warnings(self):
        path = write_temp(
            '{"mcpServers": {"fs": {"command": "npx", "args": ["some-package"]}}}'
        )
        self.assertEqual(md.main(["lint", path, "--no-color"]), 0)
        self.assertEqual(md.main(["lint", path, "--no-color", "--strict"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
