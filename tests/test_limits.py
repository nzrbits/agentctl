"""Unit tests for the limit-detection engine."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from agentctl_core import limits  # noqa: E402


class TestDetect(unittest.TestCase):
    def test_no_signal(self):
        self.assertEqual(limits.detect("all good, build succeeded"), [])

    def test_rate_limit_variants(self):
        for s in ["Rate limit exceeded", "rate_limit_exceeded", "HTTP 429",
                  "Too Many Requests", "usage limit reached", "quota exceeded"]:
            self.assertTrue(limits.detect(s), f"should detect: {s}")


class TestClassify(unittest.TestCase):
    def test_rate_limit_is_agent_specific(self):
        f = limits.classify("Error: rate_limit_exceeded (429)", agent="gpt55")
        self.assertEqual(f.category, limits.CAT_GPT_LIMIT)
        f = limits.classify("429 too many requests", agent="claude")
        self.assertEqual(f.category, limits.CAT_CLAUDE_LIMIT)
        f = limits.classify("usage limit reached", agent="deepseek")
        self.assertEqual(f.category, limits.CAT_DEEPSEEK_LIMIT)

    def test_context_wins_over_rate(self):
        f = limits.classify("context_length_exceeded; also rate limit", agent="claude")
        self.assertEqual(f.category, limits.CAT_CONTEXT)
        self.assertEqual(f.kind, limits.KIND_CONTEXT)

    def test_auth(self):
        for s in ["401 Unauthorized", "invalid api key", "authentication failed",
                  "permission denied"]:
            f = limits.classify(s, agent="gpt55")
            self.assertEqual(f.category, limits.CAT_AUTH, s)

    def test_billing(self):
        for s in ["insufficient_quota", "billing hard limit reached",
                  "you have run out of credits", "402 payment required"]:
            f = limits.classify(s, agent="gpt55")
            self.assertEqual(f.category, limits.CAT_BILLING, s)

    def test_overloaded_is_limit(self):
        f = limits.classify("Error 503: Overloaded, temporarily unavailable", agent="claude")
        self.assertEqual(f.category, limits.CAT_CLAUDE_LIMIT)

    def test_tool_error(self):
        f = limits.classify("opencode: command not found", agent="deepseek")
        self.assertEqual(f.category, limits.CAT_TOOL)

    def test_clean_output(self):
        f = limits.classify('{"status":"done","summary":"ok"}', agent="deepseek")
        self.assertFalse(f.detected)


class TestStatusFlags(unittest.TestCase):
    def test_single_flags(self):
        self.assertIn(limits.GPT_LIMIT_ACTIVE,
                      limits.status_flags({limits.CAT_GPT_LIMIT}))
        self.assertIn(limits.DEEPSEEK_LIMIT_ACTIVE,
                      limits.status_flags({limits.CAT_DEEPSEEK_LIMIT}))

    def test_brain_limit(self):
        flags = limits.status_flags({limits.CAT_GPT_LIMIT, limits.CAT_CLAUDE_LIMIT})
        self.assertIn(limits.BRAIN_LIMIT_ACTIVE, flags)
        self.assertIn(limits.GPT_LIMIT_ACTIVE, flags)
        self.assertIn(limits.CLAUDE_LIMIT_ACTIVE, flags)

    def test_no_brain_limit_with_one(self):
        flags = limits.status_flags({limits.CAT_GPT_LIMIT})
        self.assertNotIn(limits.BRAIN_LIMIT_ACTIVE, flags)


if __name__ == "__main__":
    unittest.main()
