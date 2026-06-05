"""Agent adapters: turn a config entry + prompt into a real subprocess call.

Each adapter is deliberately thin and uniform. It:
  - builds the command line from config (with {{PROMPT}} substitution),
  - refuses to run if the agent is not configured / binary missing,
  - captures exit code + stdout + stderr,
  - runs limit detection on the combined output,
  - best-effort-parses a JSON report from stdout.

No secrets are ever placed on the command line or in env dumps here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import limits


@dataclass
class AgentResult:
    agent: str
    status: str                       # ok | not_configured | tool_missing | limit | error | timeout | dry_run
    command: list[str] = field(default_factory=list)
    command_display: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    limit: dict = field(default_factory=dict)
    report: Optional[dict] = None     # parsed JSON report from the agent
    message: str = ""

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        # truncate large blobs for the on-disk log readability
        d["stdout"] = self.stdout[-8000:]
        d["stderr"] = self.stderr[-8000:]
        return d


def build_command(agent_cfg: dict, prompt: str, use_fallback: bool = False) -> list[str]:
    cmd = agent_cfg.get("command", "")
    args_key = "fallback_args" if use_fallback and agent_cfg.get("fallback_args") else "args"
    args = agent_cfg.get(args_key, [])
    out = [cmd]
    for a in args:
        out.append(a.replace("{{PROMPT}}", prompt))
    return out


def _display(cmd: list[str], prompt: str) -> str:
    """Command line for logs, with the (possibly huge) prompt elided."""
    shown = []
    for part in cmd:
        if prompt and prompt in part and len(prompt) > 40:
            shown.append("<PROMPT>")
        else:
            shown.append(part if len(part) < 120 else part[:117] + "...")
    return " ".join(shown)


def _collect_stream_text(text: str) -> str:
    """Reassemble assistant text from an NDJSON event stream.

    OpenCode's `--format json` and Claude's `--output-format stream-json` emit
    one JSON object per line. The model's actual answer lives in text events.
    We concatenate those so the real report can be parsed out of it.
    Returns "" if the text is not a recognizable event stream.
    """
    chunks: list[str] = []
    saw_event = False
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "type" not in obj:
            continue
        saw_event = True
        # opencode: {"type":"text","part":{"type":"text","text":"..."}}
        part = obj.get("part")
        if obj.get("type") == "text" and isinstance(part, dict) and part.get("text"):
            chunks.append(part["text"])
        # claude stream-json: {"type":"assistant","message":{"content":[{"type":"text","text":...}]}}
        msg = obj.get("message")
        if isinstance(msg, dict):
            for c in msg.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    chunks.append(c["text"])
        # claude --output-format json: {"type":"result","result":"..."}
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            chunks.append(obj["result"])
    return "\n".join(chunks) if saw_event else ""


def _extract_json(text: str) -> Optional[dict]:
    """Find the most plausible JSON object in agent stdout.

    Handles: pure JSON, JSON embedded in chatter, NDJSON event streams
    (opencode / claude stream-json), and the last object carrying report keys.
    """
    if not text:
        return None
    # 0) if this is an event stream, parse the reassembled assistant text first
    streamed = _collect_stream_text(text)
    if streamed and streamed != text:
        inner = _extract_json(streamed)
        if inner is not None:
            return inner
    # 1) whole thing
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            # claude `--output-format json` wraps the answer in {"result": "..."}
            if isinstance(obj.get("result"), str):
                inner = _extract_json(obj["result"])
                if inner is not None:
                    return inner
            return obj
    except json.JSONDecodeError:
        pass
    # 2) scan for balanced {...} blocks, prefer the last one that has report keys
    candidates: list[dict] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            candidates.append(obj)
                    except json.JSONDecodeError:
                        pass
    report_keys = {"status", "summary", "action", "changed_files"}
    for obj in reversed(candidates):
        if report_keys & set(obj.keys()):
            return obj
    return candidates[-1] if candidates else None


def run_agent(
    name: str,
    agent_cfg: dict,
    prompt: str,
    cwd: Path,
    dry_run: bool = False,
    timeout: Optional[int] = None,
) -> AgentResult:
    cmd = build_command(agent_cfg, prompt)
    disp = _display(cmd, prompt)
    res = AgentResult(agent=name, status="error", command=cmd, command_display=disp)

    binary = cmd[0] if cmd else ""

    if not agent_cfg.get("enabled", True):
        res.status = "not_configured"
        res.message = f"agent '{name}' is disabled in config"
        return res

    if not binary or binary == "CONFIGURE_ME":
        res.status = "not_configured"
        res.message = (
            f"agent '{name}' has no real command (command='{binary}'). "
            f"Wire it up in .agent/config.json before it can run."
        )
        return res

    if shutil.which(binary) is None:
        res.status = "tool_missing"
        res.message = f"binary '{binary}' not found on PATH"
        res.limit = limits.LimitFinding(
            detected=True, kind=limits.KIND_TOOL, category=limits.CAT_TOOL,
            matches=[f"{binary}: not found"],
        ).as_dict()
        return res

    if dry_run:
        res.status = "dry_run"
        res.message = f"[dry-run] would execute: {disp}"
        return res

    to = timeout or int(agent_cfg.get("timeout_seconds", 300))
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=to
        )
        res.exit_code = p.returncode
        res.stdout = p.stdout or ""
        res.stderr = p.stderr or ""
    except subprocess.TimeoutExpired as e:
        res.status = "timeout"
        res.duration_s = round(time.time() - t0, 2)
        res.message = f"timed out after {to}s"
        res.stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        res.stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return res
    except Exception as e:  # noqa: BLE001 - surface anything as a clean error
        res.status = "error"
        res.duration_s = round(time.time() - t0, 2)
        res.message = f"failed to execute: {e}"
        return res

    res.duration_s = round(time.time() - t0, 2)

    # Parse the report first. A clean exit + a parseable report means the agent
    # SUCCEEDED, and its content may legitimately discuss "rate limit", "401",
    # etc. (e.g. summarizing code/tests). We must not let that trip detection.
    res.report = _extract_json(res.stdout)
    succeeded = res.exit_code == 0 and res.report is not None

    # stderr is where CLIs print real infra failures -> always scanned.
    # stdout is scanned only when the call did NOT cleanly succeed.
    scan_text = res.stderr if succeeded else f"{res.stdout}\n{res.stderr}"
    finding = limits.classify(scan_text, agent=name)
    res.limit = finding.as_dict()

    if finding.detected and finding.kind in (
        limits.KIND_RATE, limits.KIND_CONTEXT, limits.KIND_AUTH,
        limits.KIND_BILLING, limits.KIND_OVERLOADED,
    ):
        res.status = "limit"
        res.message = f"{finding.category} detected ({', '.join(finding.matches)})"
        return res

    if res.exit_code == 0:
        res.status = "ok"
    else:
        res.status = "error"
        res.message = f"exit code {res.exit_code}"
    return res
