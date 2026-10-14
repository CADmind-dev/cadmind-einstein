"""The reference agent miner's command line: Claude Code's CLI restricted to the Watt MCP server by
default, or any agent CLI through an argv template."""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from cadmind_einstein.agent_miner import PROMPT, agent_argv


def ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "agent_command": "",
        "agent_cli": "claude",
        "model": "",
        "agent_arg": [],
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestAgentArgv(unittest.TestCase):
    def call(self, a: argparse.Namespace) -> list[str]:
        return agent_argv(
            a,
            prompt="P",
            prompt_file=Path("/s/prompt.txt"),
            mcp_file=Path("/s/watt-mcp.json"),
            ws="ws-1",
            job="job-1",
        )

    def test_default_is_restricted_to_the_mcp_tools_and_names_no_model(self) -> None:
        argv = self.call(ns())
        self.assertEqual(argv[:3], ["claude", "-p", "P"])
        self.assertIn("--strict-mcp-config", argv)
        # the Watt MCP server's tools and the one local fetch tool (tests/test_fetch_mcp.py)
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1],
            "mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace",
        )
        self.assertNotIn("--model", argv)

    def test_model_and_extra_arguments_are_the_operators(self) -> None:
        argv = self.call(ns(agent_cli="/opt/bin/claude", model="m-1", agent_arg=["--verbose"]))
        self.assertEqual(argv[0], "/opt/bin/claude")
        self.assertEqual(argv[argv.index("--model") + 1], "m-1")
        self.assertEqual(argv[-1], "--verbose")

    def test_any_agent_cli_through_a_template(self) -> None:
        argv = self.call(
            ns(
                agent_command=(
                    "agent --mcp {mcp_config} --in {prompt_file} --ws {workspace} -m {model}"
                ),
                model="x",
            )
        )
        self.assertEqual(
            argv,
            [
                "agent",
                "--mcp",
                "/s/watt-mcp.json",
                "--in",
                "/s/prompt.txt",
                "--ws",
                "ws-1",
                "-m",
                "x",
            ],
        )

    def test_the_prompt_template_fills(self) -> None:
        text = PROMPT.format(ws="w", job="j", brief="A {braced} brief", minutes=30, server="s")
        self.assertIn("A {braced} brief", text)
        self.assertIn("DONE <digest>", text)


if __name__ == "__main__":
    unittest.main()
