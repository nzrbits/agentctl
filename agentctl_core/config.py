"""Config + env loading. Secrets are referenced, never printed."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Mirrors .agent/config.example.json so agentctl works with zero config files.
DEFAULTS: dict[str, Any] = {
    "orchestrator": {
        "primary": "gpt55",
        "fallback": "claude",
        "review_required": True,
        "review_policy": "reciprocal",
        "default_caller": "gpt55",
        "run_timeout_seconds": 120,
        "agent_timeout_seconds": 90,
        "review_timeout_seconds": 45,
        "idle_timeout_seconds": 30,
        "role_policy": {
            "gpt55": "Lead Orchestrator: planning, routing, final review, and final decision authority",
            "claude": "Architecture Reviewer: infrastructure architecture, risk review, and lightweight code design validation",
            "deepseek": "Execution Worker: token-efficient repository inspection, tests, logs, context packs, and low-risk implementation",
        },
    },
    "agents": {
        "gpt55": {
            "enabled": True,
            "command": "CONFIGURE_ME",
            "args": ["{{PROMPT}}"],
            "env": ["OPENAI_API_KEY"],
            "timeout_seconds": 45,
            "idle_timeout_seconds": 30,
        },
        "claude": {
            "enabled": True,
            "command": "claude",
            "args": ["-p", "{{PROMPT}}", "--output-format", "json"],
            "env": ["ANTHROPIC_API_KEY"],
            "timeout_seconds": 45,
            "idle_timeout_seconds": 30,
        },
        "deepseek": {
            "enabled": True,
            "command": "opencode",
            "args": ["run", "--agent", "deepseek-worker", "--format", "json", "{{PROMPT}}"],
            "env": ["DEEPSEEK_API_KEY"],
            "timeout_seconds": 90,
            "idle_timeout_seconds": 30,
        },
    },
    "limits": {"detect_patterns": True, "fallback_on_limit": True},
    "worktrees": {"enabled": True, "base_dir": ".agent-runs/worktrees"},
    "safety": {
        "block_dangerous_commands": True,
        "deepseek_may_edit": False,
        "claude_may_edit": True,
    },
}


def install_dir() -> Path:
    """Directory the agentctl package is installed in (ships prompts/config)."""
    return Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_search_paths(cwd: Path) -> list[Path]:
    inst = install_dir()
    return [
        cwd / ".agent" / "config.json",
        inst / ".agent" / "config.json",
        inst / ".agent" / "config.example.json",
    ]


def load_config(cwd: Path) -> tuple[dict, list[Path]]:
    """Merge DEFAULTS with the first config file found. Returns (config, used_paths)."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    used: list[Path] = []
    for p in config_search_paths(cwd):
        if p.exists():
            try:
                cfg = _deep_merge(cfg, json.loads(p.read_text()))
                used.append(p)
                break
            except json.JSONDecodeError:
                continue
    return cfg, used


def load_dotenv(cwd: Path) -> dict[str, str]:
    """Read .env from cwd then install dir into os.environ (without overwriting)."""
    loaded: dict[str, str] = {}
    for envfile in (cwd / ".env", install_dir() / ".env"):
        if not envfile.exists():
            continue
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
                loaded[key] = val
    return loaded


def mask(value: str | None) -> str:
    """Mask a secret for display: keep nothing meaningful."""
    if not value:
        return "(unset)"
    n = len(value)
    if n <= 4:
        return "SET(****)"
    return f"SET(len={n}, …{value[-2:]})"


def env_status(var: str) -> str:
    return mask(os.environ.get(var))
