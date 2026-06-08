"""Unit tests for safety scanning and adapter command building / parsing."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from agentctl_core import safety, adapters, config as cfg_mod, repo  # noqa: E402


class TestSafety(unittest.TestCase):
    def test_dangerous(self):
        for c in ["rm -rf /", "git reset --hard origin/main", "git clean -fd",
                  "git push --force", "DROP TABLE users", "terraform apply",
                  "kubectl delete ns prod", "helm uninstall x", "shutdown -h now"]:
            bad, reason = safety.is_dangerous(c)
            self.assertTrue(bad, f"should flag: {c}")
            self.assertTrue(reason)

    def test_safe(self):
        for c in ["git status", "rg TODO", "python3 -m pytest", "ls -la",
                  "git diff", "npm test"]:
            bad, _ = safety.is_dangerous(c)
            self.assertFalse(bad, f"should NOT flag: {c}")


class TestAdapterBuild(unittest.TestCase):
    def test_prompt_substitution(self):
        cfg = {"command": "claude", "args": ["-p", "{{PROMPT}}", "--output-format", "json"]}
        cmd = adapters.build_command(cfg, "hello world")
        self.assertEqual(cmd, ["claude", "-p", "hello world", "--output-format", "json"])

    def test_not_configured(self):
        cfg = {"command": "CONFIGURE_ME", "args": ["{{PROMPT}}"], "enabled": True}
        res = adapters.run_agent("gpt55", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "not_configured")

    def test_tool_missing(self):
        cfg = {"command": "definitely_not_a_real_binary_xyz", "args": ["{{PROMPT}}"],
               "enabled": True}
        res = adapters.run_agent("deepseek", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "tool_missing")

    def test_dry_run(self):
        cfg = {"command": "echo", "args": ["{{PROMPT}}"], "enabled": True}
        res = adapters.run_agent("deepseek", cfg, "hi", Path("."), dry_run=True)
        self.assertEqual(res.status, "dry_run")

    def test_timeout_returns_clean_result(self):
        cfg = {"command": "sh", "args": ["-c", "sleep 2"], "enabled": True,
               "timeout_seconds": 1}
        res = adapters.run_agent("deepseek", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "timeout")
        self.assertIn("timed out", res.message)

    def test_idle_timeout_returns_clean_result(self):
        cfg = {"command": "sh", "args": ["-c", "sleep 2"], "enabled": True,
               "timeout_seconds": 10, "idle_timeout_seconds": 1}
        res = adapters.run_agent("deepseek", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "timeout")
        self.assertIn("idle timeout", res.message)

    def test_as_dict_elides_long_command_args(self):
        res = adapters.AgentResult(agent="deepseek", status="dry_run",
                                   command=["opencode", "x" * 1000])
        logged = res.as_dict()
        self.assertEqual(logged["command"][0], "opencode")
        self.assertTrue(logged["command"][1].startswith("<ELIDED_LONG_ARG"))

    def test_extract_json_embedded(self):
        text = 'chatter before\n{"status":"done","summary":"ok"}\ntrailing'
        obj = adapters._extract_json(text)
        self.assertEqual(obj.get("status"), "done")

    def test_extract_json_picks_report(self):
        text = '{"noise":1} ... {"action":"final_answer","reasoning_summary":"x"}'
        obj = adapters._extract_json(text)
        self.assertEqual(obj.get("action"), "final_answer")

    def test_extract_from_opencode_stream(self):
        stream = (
            '{"type":"step_start","part":{"type":"step-start"}}\n'
            '{"type":"text","part":{"type":"text","text":"{\\"status\\":\\"done\\",'
            '\\"summary\\":\\"ok\\"}"}}\n'
            '{"type":"step_finish","part":{"type":"step-finish"}}'
        )
        obj = adapters._extract_json(stream)
        self.assertEqual(obj.get("status"), "done")

    def test_extract_from_claude_result_wrapper(self):
        wrapped = ('{"type":"result","result":"text {\\"status\\":\\"done\\",'
                   '\\"changed_files\\":[\\"a.py\\"]}"}')
        obj = adapters._extract_json(wrapped)
        self.assertEqual(obj.get("changed_files"), ["a.py"])


class TestLimitScoping(unittest.TestCase):
    """A successful agent whose CONTENT mentions limit words must not be flagged."""

    def test_success_with_limit_words_in_payload_is_ok(self):
        payload = ('{"status":"done","summary":"tests cover 401 unauthorized, '
                   'invalid api key and rate limit cases"}')
        cfg = {"command": "printf", "args": ["%s", payload], "enabled": True}
        res = adapters.run_agent("deepseek", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "ok", res.message)
        self.assertFalse(res.limit.get("detected"))

    def test_real_failure_in_stderr_is_flagged(self):
        cfg = {"command": "sh",
               "args": ["-c", "echo 'Error 429 rate_limit_exceeded' >&2; exit 1"],
               "enabled": True}
        res = adapters.run_agent("claude", cfg, "x", Path("."), dry_run=False)
        self.assertEqual(res.status, "limit")
        self.assertEqual(res.limit.get("category"), "claude_limit")


class TestConfig(unittest.TestCase):
    def test_defaults_load(self):
        cfg, used = cfg_mod.load_config(cfg_mod.install_dir())
        self.assertIn("agents", cfg)
        self.assertIn("deepseek", cfg["agents"])

    def test_mask(self):
        self.assertEqual(cfg_mod.mask(None), "(unset)")
        self.assertTrue(cfg_mod.mask("sk-1234567890").startswith("SET("))


class TestRepoSnapshot(unittest.TestCase):
    def test_file_tree_stops_at_limit_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(20):
                (root / f"f{i:02d}.txt").write_text("x")
            files = repo.file_tree(root, max_files=5)
            self.assertEqual(len(files), 5)


if __name__ == "__main__":
    unittest.main()
