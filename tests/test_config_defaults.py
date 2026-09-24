"""Built-in defaults and the shipped example config must agree with the documented behaviour."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from agentctl_core import config as cfg_mod, runner  # noqa: E402

EXAMPLE = cfg_mod.install_dir() / ".agent" / "config.example.json"


class TestConfigDefaults(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("AGENTCTL_CALLER", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["AGENTCTL_CALLER"] = self._env

    def test_builtin_default_caller_is_local(self):
        self.assertEqual(runner._resolve_caller(cfg_mod.DEFAULTS, "auto"), "local")

    def test_example_default_caller_is_local(self):
        example = json.loads(EXAMPLE.read_text())
        self.assertEqual(runner._resolve_caller(example, "auto"), "local")

    def test_default_route_is_deepseek_only(self):
        for cfg in (cfg_mod.DEFAULTS, json.loads(EXAMPLE.read_text())):
            self.assertEqual(runner._resolve_route(cfg, "auto", "local"), "deepseek")

    def test_run_context_defaults_to_local(self):
        ctx = runner.RunContext("x", Path("."), Path("."), {}, False, False)
        self.assertEqual(ctx.caller, "local")

    def test_chatgpt_route_uses_manual_handoff(self):
        cfg = {"orchestrator": {"review_required": True}}
        ctx = runner.RunContext("x", Path("."), Path("."), cfg, False, False, route="deepseek-chatgpt")
        self.assertEqual(runner._optional_reviewer(ctx), "chatgpt")


if __name__ == "__main__":
    unittest.main()
