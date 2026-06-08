"""`agentctl doctor`: environment + tooling diagnosis. No secrets printed."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import config as cfg_mod
from . import repo as repo_mod

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

_SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


def _ver(binary: str, args=("--version",)) -> str:
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True, timeout=10)
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else ""
    except Exception:
        return ""


def collect(cwd: Path) -> list[dict]:
    checks: list[dict] = []

    def add(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": detail})

    # --- system ---
    add("macOS", INFO, f"{platform.mac_ver()[0] or platform.platform()}")
    add("arch", INFO, platform.machine())
    add("shell", INFO, os.environ.get("SHELL", "unknown"))

    # --- git + repo ---
    if shutil.which("git"):
        add("git", OK, _ver("git"))
        if repo_mod.is_git_repo(cwd):
            branch = subprocess.run(["git", "branch", "--show-current"],
                                    cwd=str(cwd), capture_output=True, text=True).stdout.strip()
            dirty = subprocess.run(["git", "status", "--short"],
                                   cwd=str(cwd), capture_output=True, text=True).stdout.strip()
            add("git repo", OK, f"branch={branch or '(none)'}, "
                                f"{'dirty' if dirty else 'clean'}, "
                                f"{'has commits' if repo_mod.has_commits(cwd) else 'NO commits yet'}")
        else:
            add("git repo", WARN, f"{cwd} is not a git repo — `run` worktrees disabled here")
    else:
        add("git", FAIL, "git not found — required")

    # --- language toolchains ---
    for name, binary in (("python3", "python3"), ("node", "node"), ("npm", "npm"),
                         ("pnpm", "pnpm"), ("yarn", "yarn"), ("bun", "bun")):
        if shutil.which(binary):
            add(name, OK, _ver(binary))
        else:
            sev = FAIL if binary == "python3" else INFO
            add(name, sev, "not installed")

    # --- agent CLIs ---
    if shutil.which("opencode"):
        add("opencode (DeepSeek)", OK, _ver("opencode"))
    else:
        add("opencode (DeepSeek)", WARN, "not found — DeepSeek worker unavailable")
    if shutil.which("claude"):
        add("claude (Builder)", OK, _ver("claude"))
    else:
        add("claude (Builder)", WARN, "not found — Claude builder unavailable")

    # --- config ---
    cfg, used = cfg_mod.load_config(cwd)
    if used:
        add("config", OK, f"loaded {used[0]}")
    else:
        add("config", WARN, "no config file found — using built-in DEFAULTS")

    # --- .env presence ---
    env_local = (cwd / ".env").exists() or (cfg_mod.install_dir() / ".env").exists()
    add(".env", OK if env_local else INFO,
        "found" if env_local else "absent (fine — CLIs may use their own auth)")

    # --- env vars (masked, never printed in clear) ---
    cfg_mod.load_dotenv(cwd)
    for label, var in (("OPENAI_API_KEY (GPT-5.5)", "OPENAI_API_KEY"),
                       ("ANTHROPIC_API_KEY (Claude)", "ANTHROPIC_API_KEY"),
                       ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")):
        val = os.environ.get(var)
        add(f"env {label}", INFO, cfg_mod.mask(val))

    # --- per-agent readiness from config ---
    agents = cfg.get("agents", {})
    # gpt55
    gpt = agents.get("gpt55", {})
    if gpt.get("type") == "http_openai":
        key_env = gpt.get("api_key_env", "OPENAI_API_KEY")
        if os.environ.get(key_env):
            add("orchestrator gpt55", OK,
                f"http_openai model={gpt.get('model','gpt-5.5')} ({key_env} set)")
        else:
            add("orchestrator gpt55", WARN,
                f"http_openai model={gpt.get('model','gpt-5.5')} — {key_env} unset; "
                "add API key + billing (platform.openai.com). Until then GPT-5.5 review is skipped.")
    elif gpt.get("command") in (None, "", "CONFIGURE_ME"):
        add("orchestrator gpt55", WARN,
            "command=CONFIGURE_ME → not wired. Local synthesis only.")
    elif shutil.which(gpt.get("command", "")):
        add("orchestrator gpt55", OK, f"command={gpt['command']}")
    else:
        add("orchestrator gpt55", WARN, f"command '{gpt.get('command')}' not on PATH")
    # claude
    cl = agents.get("claude", {})
    add("agent claude", OK if shutil.which(cl.get("command", "")) else WARN,
        f"command={cl.get('command')}")
    # deepseek
    ds = agents.get("deepseek", {})
    if shutil.which(ds.get("command", "")):
        # check whether the configured opencode agent exists
        agent_flag = "deepseek-worker" in " ".join(ds.get("args", []))
        detail = f"command={ds.get('command')}"
        if agent_flag and shutil.which("opencode"):
            try:
                out = subprocess.run(["opencode", "agent", "list"],
                                     capture_output=True, text=True, timeout=15).stdout
                if "deepseek-worker" in out:
                    detail += ", opencode agent 'deepseek-worker' present"
                else:
                    detail += ", opencode agent 'deepseek-worker' MISSING " \
                              "(install .agent/opencode/deepseek-worker.md or use fallback_args)"
            except Exception:
                pass
        add("agent deepseek", OK, detail)
    else:
        add("agent deepseek", WARN, f"command '{ds.get('command')}' not on PATH")

    return checks


def render(checks: list[dict]) -> str:
    lines = ["agentctl doctor", "=" * 60]
    for c in checks:
        sym = _SYMBOL.get(c["status"], "?")
        lines.append(f" [{sym}] {c['name']:<28} {c['detail']}")
    fails = [c for c in checks if c["status"] == FAIL]
    warns = [c for c in checks if c["status"] == WARN]
    lines.append("=" * 60)
    lines.append(f" {len(fails)} fail · {len(warns)} warn · "
                 f"{sum(1 for c in checks if c['status']==OK)} ok")
    if fails:
        lines.append(" BLOCKER: fix FAIL items before running `agentctl run`.")
    return "\n".join(lines)


def exit_code(checks: list[dict]) -> int:
    return 1 if any(c["status"] == FAIL for c in checks) else 0
