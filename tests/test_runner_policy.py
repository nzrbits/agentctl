"""Unit tests for fixed multi-agent role/routing policy."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from agentctl_core import adapters, runner  # noqa: E402


class TestRunnerPolicy(unittest.TestCase):
    def test_deepseek_route_does_not_review_successful_deepseek(self):
        res = adapters.AgentResult(agent="deepseek", status="ok", report={"status": "done"})
        plan = {
            "review_required": True,
            "review_policy": "best_effort_gpt55",
            "reviewer": None,
            "complex": False,
            "security_sensitive": False,
        }
        self.assertFalse(runner._needs_review(res, plan, allow_edit=False))

    def test_review_required_can_disable_claude_review(self):
        res = adapters.AgentResult(agent="deepseek", status="ok", report={"status": "done"})
        plan = {
            "review_required": False,
            "review_policy": "reciprocal",
            "reviewer": "claude",
            "complex": True,
            "security_sensitive": True,
        }
        self.assertFalse(runner._needs_review(res, plan, allow_edit=True))

    def test_deepseek_route_has_no_reviewer(self):
        cfg = {"orchestrator": {"review_required": True, "review_policy": "reciprocal"}}
        ctx = runner.RunContext("x", Path("."), Path("."), cfg, False, False, caller="gpt55", route="deepseek")
        self.assertIsNone(runner._optional_reviewer(ctx))

    def test_claude_route_uses_claude_reviewer(self):
        cfg = {"orchestrator": {"review_required": True, "review_policy": "best_effort_gpt55"}}
        ctx = runner.RunContext("x", Path("."), Path("."), cfg, False, False, caller="local", route="deepseek-claude")
        self.assertEqual(runner._optional_reviewer(ctx), "claude")


if __name__ == "__main__":
    unittest.main()
