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
import os
import re
import selectors
import shutil
import signal
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
        d["command"] = [
            part if len(part) <= 500 else f"<ELIDED_LONG_ARG len={len(part)}>"
            for part in self.command
        ]
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


_SESSION_RE = re.compile(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9]+)"')


def session_id_from_stream(text: str) -> str:
    """Return the first opencode session id found in an NDJSON event stream, or ""."""
    m = _SESSION_RE.search(text or "")
    return m.group(1) if m else ""


def stream_has_tool_use(text: str) -> bool:
    """True if the event stream contains at least one tool_use event."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"type":"tool_use"' in line.replace(" ", ""):
            return True
    return False


def collect_tool_receipts(text: str, max_output: int = 8000,
                          max_receipts: int = 40) -> list[dict]:
    """Extract completed tool calls (the raw evidence) from an opencode event stream.

    Each tool_use event carries part.state.input (args) and part.state.output
    (result/file contents). When a worker gathers evidence but emits no final
    text, these receipts ARE the deliverable — hand them to the synthesizer.
    """
    out: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or '"type":"tool_use"' not in line.replace(" ", ""):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = (obj.get("part") or {}).get("state") or {}
        if state.get("status") != "completed":
            continue
        output = state.get("output")
        if isinstance(output, str) and len(output) > max_output:
            output = output[:max_output] + f" …[+{len(output) - max_output} chars]"
        out.append({
            "tool": (obj.get("part") or {}).get("tool"),
            "input": state.get("input"),
            "output": output,
        })
        if len(out) >= max_receipts:
            break
    return out


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
    idle_timeout: Optional[int] = None,
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

    to = timeout or int(agent_cfg.get("timeout_seconds", 90))
    idle_to = idle_timeout or int(agent_cfg.get("idle_timeout_seconds", 45))
    t0 = time.time()
    p = None
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    timed_out_reason = ""
    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        sel = selectors.DefaultSelector()
        if p.stdout:
            sel.register(p.stdout, selectors.EVENT_READ, "stdout")
        if p.stderr:
            sel.register(p.stderr, selectors.EVENT_READ, "stderr")
        deadline = t0 + to
        idle_deadline = time.time() + idle_to
        while p.poll() is None:
            now = time.time()
            if now >= deadline:
                timed_out_reason = f"timed out after {to}s"
                break
            if now >= idle_deadline:
                timed_out_reason = f"idle timeout after {idle_to}s without output"
                break
            wait_for = max(0.1, min(1.0, deadline - now, idle_deadline - now))
            for key, _ in sel.select(timeout=wait_for):
                chunk = key.fileobj.readline()
                if not chunk:
                    try:
                        sel.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                if key.data == "stdout":
                    stdout_parts.append(chunk)
                else:
                    stderr_parts.append(chunk)
                idle_deadline = time.time() + idle_to
        if timed_out_reason:
            raise subprocess.TimeoutExpired(cmd, to)
        stdout, stderr = p.communicate(timeout=2)
        stdout = "".join(stdout_parts) + (stdout or "")
        stderr = "".join(stderr_parts) + (stderr or "")
        res.exit_code = p.returncode
        res.stdout = stdout or ""
        res.stderr = stderr or ""
    except subprocess.TimeoutExpired as e:
        stdout = "".join(stdout_parts) + (e.stdout or "")
        stderr = "".join(stderr_parts) + (e.stderr or "")
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        res.status = "timeout"
        res.duration_s = round(time.time() - t0, 2)
        res.message = timed_out_reason or f"timed out after {to}s"
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            try:
                killed_stdout, killed_stderr = p.communicate(timeout=2)
                stdout = (stdout or "") + (killed_stdout or "")
                stderr = (stderr or "") + (killed_stderr or "")
            except subprocess.TimeoutExpired:
                pass
        res.stdout = stdout
        res.stderr = stderr
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
