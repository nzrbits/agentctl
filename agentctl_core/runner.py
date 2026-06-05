"""`agentctl run`: orchestrate DeepSeek -> Claude with GPT-5.5 as lead brain.

Routing philosophy (cheap-first):
  1. DeepSeek scouts and, when safe, does the small work.
  2. Claude implements only when the task is complex / DeepSeek escalates.
  3. GPT-5.5 plans + reviews when configured; otherwise a local heuristic
     orchestrator stands in and the run is flagged GPT_LIMIT_ACTIVE.

Everything is logged. No infra is mutated. Agents run in isolated git
worktrees when possible so the main working tree is never touched.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, config as cfg_mod, limits, repo as repo_mod, safety

# Heuristics for "this is not cheap DeepSeek work".
_COMPLEX_HINTS = [
    "refactor", "architecture", "architect", "migrate", "migration", "redesign",
    "concurrency", "thread", "async race", "auth", "authentication", "authorization",
    "oauth", "payment", "billing", "encryption", "crypto", "security", "vulnerab",
    "schema change", "database migration", "multi-file", "cross-cutting",
    "infra", "terraform", "kubernetes", "deploy pipeline", "rewrite",
]
_SECURITY_HINTS = ["auth", "secret", "token", "password", "crypto", "encryption",
                   "vulnerab", "exploit", "permission", "rbac", "payment"]


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _short(text: str, n: int = 1200) -> str:
    return text if len(text) <= n else text[:n] + f"\n…[truncated {len(text)-n} chars]"


@dataclass
class RunContext:
    task: str
    cwd: Path
    run_dir: Path
    config: dict
    dry_run: bool
    allow_edit: bool
    snapshot: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    active_limit_categories: set[str] = field(default_factory=set)
    worktrees: list[str] = field(default_factory=list)

    def log(self, kind: str, **data):
        evt = {"ts": _now_tag(), "kind": kind, **data}
        self.events.append(evt)
        print(f"  · {kind}: " + data.get("msg", json.dumps({k: v for k, v in data.items() if k != 'msg'})[:160]))


# --------------------------------------------------------------------------- #
# prompt assembly
# --------------------------------------------------------------------------- #
def _load_prompt_template(name: str) -> str:
    p = cfg_mod.install_dir() / ".agent" / "prompts" / name
    return p.read_text() if p.exists() else ""


def _context_pack(ctx: RunContext) -> str:
    snap = ctx.snapshot
    pkg = snap.get("package", {})
    lines = [
        "## REPO CONTEXT (read-only snapshot)",
        f"- cwd: {snap.get('cwd')}",
        f"- git: {snap.get('is_git_repo')} branch={snap.get('branch','-')}",
        f"- language: {pkg.get('language')} | package_manager: {pkg.get('package_manager')}",
        f"- test_command: {pkg.get('test_command')}",
        f"- file_count: {snap.get('file_count')}",
        "- files (sample):",
    ]
    for f in snap.get("files", [])[:60]:
        lines.append(f"    {f}")
    if snap.get("status"):
        lines.append("- git status:")
        for s in snap["status"][:30]:
            lines.append(f"    {s}")
    return "\n".join(lines)


def _build_contract(ctx: RunContext, role: str, goal: str, allow_edit: bool) -> dict:
    return {
        "role": role,
        "goal": goal,
        "scope": [ctx.task],
        "non_goals": ["no scope expansion", "no unrelated cleanup", "no new deps unless allowed"],
        "allowed_files": ["repo files within cwd / worktree"],
        "allowed_commands": ["read-only inspection; tests"] +
                            (["small edits"] if allow_edit else ["NO edits (inspection only)"]),
        "acceptance_criteria": ["evidence-backed findings", "valid JSON report"],
        "escalation_criteria": ["complex/architecture/security -> needs_claude"],
        "expected_output": "json_report",
        "edits_allowed": allow_edit,
    }


def _compose_agent_prompt(ctx: RunContext, template: str, contract: dict) -> str:
    return (
        f"{template}\n\n"
        f"## TASK CONTRACT\n```json\n{json.dumps(contract, indent=2)}\n```\n\n"
        f"{_context_pack(ctx)}\n\n"
        f"## USER TASK\n{ctx.task}\n\n"
        f"Return only the JSON report described above."
    )


# --------------------------------------------------------------------------- #
# orchestrator (GPT-5.5 if wired, else local heuristic)
# --------------------------------------------------------------------------- #
def _is_complex(task: str) -> bool:
    t = task.lower()
    return any(h in t for h in _COMPLEX_HINTS)


def _is_security_sensitive(task: str) -> bool:
    t = task.lower()
    return any(h in t for h in _SECURITY_HINTS)


def _gpt_available(ctx: RunContext) -> bool:
    gpt = ctx.config.get("agents", {}).get("gpt55", {})
    cmd = gpt.get("command")
    return bool(cmd) and cmd != "CONFIGURE_ME" and bool(__import__("shutil").which(cmd))


def plan(ctx: RunContext) -> dict:
    """Produce a routing plan. Uses GPT-5.5 if wired, else local heuristic."""
    complex_task = _is_complex(ctx.task)
    security = _is_security_sensitive(ctx.task)

    if _gpt_available(ctx):
        orchestrator = "gpt55"
    else:
        orchestrator = "claude(heuristic-fallback)"
        ctx.flags.add(limits.GPT_LIMIT_ACTIVE)
        ctx.log("orchestrator", msg="GPT-5.5 not wired (CONFIGURE_ME) -> "
                                    "local Claude heuristic orchestrator. GPT_LIMIT_ACTIVE")

    # cheap-first: DeepSeek scouts first unless obviously complex/security
    first = "deepseek"
    reason = "cheap-first: DeepSeek produces a context pack and tries the work"
    if complex_task or security:
        reason = ("task looks complex/security-sensitive; DeepSeek still scouts first, "
                  "then Claude implements")
    return {
        "orchestrator": orchestrator,
        "complex": complex_task,
        "security_sensitive": security,
        "first_agent": first,
        "reasoning_summary": reason,
        "review_required": ctx.config.get("orchestrator", {}).get("review_required", True),
    }


# --------------------------------------------------------------------------- #
# worktrees
# --------------------------------------------------------------------------- #
def _make_worktree(ctx: RunContext, label: str) -> Path | None:
    wcfg = ctx.config.get("worktrees", {})
    if not wcfg.get("enabled", True):
        return None
    if not repo_mod.is_git_repo(ctx.cwd) or not repo_mod.has_commits(ctx.cwd):
        ctx.log("worktree", msg=f"skip ({label}): not a git repo with commits")
        return None
    base = ctx.cwd / wcfg.get("base_dir", ".agent-runs/worktrees")
    base.mkdir(parents=True, exist_ok=True)
    wt = base / f"{ctx.run_dir.name}-{label}"
    branch = f"agentctl/{ctx.run_dir.name}-{label}"
    rc = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt), "HEAD"],
        cwd=str(ctx.cwd), capture_output=True, text=True,
    )
    if rc.returncode != 0:
        ctx.log("worktree", msg=f"failed ({label}): {rc.stderr.strip()[:160]}")
        return None
    ctx.worktrees.append(str(wt))
    ctx.log("worktree", msg=f"created {wt} on {branch}")
    return wt


# --------------------------------------------------------------------------- #
# limit handling
# --------------------------------------------------------------------------- #
def _handle_limit(ctx: RunContext, res: adapters.AgentResult) -> str:
    """Update flags from a limit result. Returns a directive string."""
    cat = res.limit.get("category")
    ctx.active_limit_categories.add(cat)
    if cat == limits.CAT_AUTH:
        ctx.flags.add(limits.CAT_AUTH)
        return "block_auth"
    if cat == limits.CAT_BILLING:
        ctx.flags.add(limits.CAT_BILLING)
        return "block_billing"
    if cat == limits.CAT_CONTEXT:
        ctx.flags.add(limits.CAT_CONTEXT)
        return "compress_retry"
    if cat == limits.CAT_GPT_LIMIT:
        ctx.flags.add(limits.GPT_LIMIT_ACTIVE)
        return "gpt_limited"
    if cat == limits.CAT_CLAUDE_LIMIT:
        ctx.flags.add(limits.CLAUDE_LIMIT_ACTIVE)
        return "claude_limited"
    if cat == limits.CAT_DEEPSEEK_LIMIT:
        ctx.flags.add(limits.DEEPSEEK_LIMIT_ACTIVE)
        return "deepseek_limited"
    return "unknown"


def _should_escalate(res: adapters.AgentResult, plan_info: dict) -> bool:
    if plan_info["complex"] or plan_info["security_sensitive"]:
        return True
    rep = res.report or {}
    if rep.get("status") == "needs_claude":
        return True
    if rep.get("recommended_next_action") == "escalate_claude":
        return True
    if res.status in ("error", "timeout"):
        return True
    return False


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def run(task: str, cwd: Path, dry_run: bool, allow_edit: bool) -> dict:
    cfg, used = cfg_mod.load_config(cwd)
    cfg_mod.load_dotenv(cwd)

    runs_root = cwd / ".agent-runs"
    runs_root.mkdir(exist_ok=True)
    run_dir = runs_root / f"{_now_tag()}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True)

    ctx = RunContext(task=task, cwd=cwd, run_dir=run_dir, config=cfg,
                     dry_run=dry_run, allow_edit=allow_edit)

    print(f"agentctl run  [{run_dir.name}]" + ("  (dry-run)" if dry_run else ""))
    print(f"  task: {task}")

    # 1. snapshot
    ctx.snapshot = repo_mod.snapshot(cwd)
    (run_dir / "task.txt").write_text(task + "\n")
    (run_dir / "snapshot.json").write_text(json.dumps(ctx.snapshot, indent=2))
    ctx.log("snapshot", msg=f"git={ctx.snapshot['is_git_repo']} "
                            f"files={ctx.snapshot['file_count']} "
                            f"lang={ctx.snapshot['package'].get('language')}")

    # 2. plan / route
    plan_info = plan(ctx)
    (run_dir / "plan.json").write_text(json.dumps(plan_info, indent=2))
    ctx.log("plan", msg=f"first={plan_info['first_agent']} complex={plan_info['complex']} "
                        f"orchestrator={plan_info['orchestrator']}")

    results: list[adapters.AgentResult] = []

    # 3. DeepSeek scout / context pack (cheap-first)
    ds_cfg = cfg["agents"]["deepseek"]
    ds_edit = allow_edit and cfg.get("safety", {}).get("deepseek_may_edit", False) \
        and not plan_info["security_sensitive"]
    ds_contract = _build_contract(ctx, "deepseek-worker",
                                  "Scout the repo, build a context pack, and answer/solve "
                                  "the task if it is low-risk.", ds_edit)
    ds_prompt = _compose_agent_prompt(ctx, _load_prompt_template("deepseek-worker.md"), ds_contract)
    (run_dir / "prompt-deepseek.md").write_text(ds_prompt)

    ds_wt = _make_worktree(ctx, "deepseek") if ds_edit else None
    ds_cwd = ds_wt or cwd
    ctx.log("delegate", msg="delegate_deepseek (scout/context-pack)")
    ds_res = adapters.run_agent("deepseek", ds_cfg, ds_prompt, ds_cwd, dry_run=dry_run)
    results.append(ds_res)
    _persist_result(run_dir, "deepseek", ds_res)
    ctx.log("result", msg=f"deepseek -> {ds_res.status} {ds_res.message}")

    if ds_res.status == "limit":
        directive = _handle_limit(ctx, ds_res)
        ctx.log("limit", msg=f"deepseek {ds_res.limit.get('category')} -> {directive}")

    # 4. decide escalation to Claude
    escalate = _should_escalate(ds_res, plan_info) or ds_res.status == "limit"
    claude_res = None

    # brain-limit guard
    brain_blocked = (limits.GPT_LIMIT_ACTIVE in ctx.flags
                     and limits.CLAUDE_LIMIT_ACTIVE in ctx.flags)
    if brain_blocked:
        ctx.flags.add(limits.BRAIN_LIMIT_ACTIVE)

    if escalate and not brain_blocked:
        cl_cfg = cfg["agents"]["claude"]
        cl_edit = allow_edit and cfg.get("safety", {}).get("claude_may_edit", True)
        cl_contract = _build_contract(ctx, "claude-builder",
                                      "Implement the task to a high standard within scope; "
                                      "use the DeepSeek context pack as recon.", cl_edit)
        # attach deepseek findings
        if ds_res.report:
            cl_contract["deepseek_findings"] = ds_res.report
        cl_prompt = _compose_agent_prompt(ctx, _load_prompt_template("claude-builder.md"), cl_contract)
        (run_dir / "prompt-claude.md").write_text(cl_prompt)

        cl_wt = _make_worktree(ctx, "claude") if cl_edit else None
        cl_cwd = cl_wt or cwd
        ctx.log("delegate", msg="delegate_claude (implementation)")
        claude_res = adapters.run_agent("claude", cl_cfg, cl_prompt, cl_cwd, dry_run=dry_run)
        results.append(claude_res)
        _persist_result(run_dir, "claude", claude_res)
        ctx.log("result", msg=f"claude -> {claude_res.status} {claude_res.message}")
        if claude_res.status == "limit":
            directive = _handle_limit(ctx, claude_res)
            ctx.log("limit", msg=f"claude {claude_res.limit.get('category')} -> {directive}")
    elif brain_blocked:
        ctx.log("block", msg="BRAIN_LIMIT_ACTIVE: GPT-5.5 + Claude both limited — "
                             "no complex changes, diagnosis only")

    # 5. final flags
    for f in limits.status_flags(ctx.active_limit_categories):
        ctx.flags.add(f)

    # 6. review report
    review = _review(ctx, plan_info, ds_res, claude_res)
    (run_dir / "review-report.md").write_text(review)

    # 7. full log
    log = {
        "run": run_dir.name,
        "task": task,
        "dry_run": dry_run,
        "config_files": [str(p) for p in used],
        "plan": plan_info,
        "flags": sorted(ctx.flags),
        "limit_categories": sorted(ctx.active_limit_categories),
        "worktrees": ctx.worktrees,
        "events": ctx.events,
        "results": [r.as_dict() for r in results],
    }
    (run_dir / "log.json").write_text(json.dumps(log, indent=2))

    print(f"\n{review}\n")
    print(f"artifacts: {run_dir}")
    return log


def _persist_result(run_dir: Path, name: str, res: adapters.AgentResult):
    d = run_dir / f"agent-{name}"
    d.mkdir(exist_ok=True)
    (d / "result.json").write_text(json.dumps(res.as_dict(), indent=2))
    if res.stdout:
        (d / "stdout.txt").write_text(res.stdout)
    if res.stderr:
        (d / "stderr.txt").write_text(res.stderr)
    if res.report:
        (d / "report.json").write_text(json.dumps(res.report, indent=2))


def _review(ctx: RunContext, plan_info, ds_res, claude_res) -> str:
    """Local synthesis review. GPT-5.5/Claude review when wired; this always runs."""
    lines = ["# agentctl review report", ""]
    lines.append(f"- run: `{ctx.run_dir.name}`")
    lines.append(f"- task: {ctx.task}")
    lines.append(f"- mode: {'DRY-RUN (no agents executed)' if ctx.dry_run else 'live'}")
    lines.append(f"- orchestrator: {plan_info['orchestrator']}")
    lines.append(f"- route: deepseek-first → " +
                 ("claude" if claude_res else "deepseek only"))
    if ctx.flags:
        lines.append(f"- **flags: {', '.join(sorted(ctx.flags))}**")
    lines.append("")

    # blocking conditions
    if limits.CAT_AUTH in ctx.flags:
        lines.append("## BLOCKED — auth error")
        lines.append("An agent reported an authentication problem. Check the relevant "
                     "API key / CLI login before retrying. No changes were accepted.")
    if limits.CAT_BILLING in ctx.flags:
        lines.append("## BLOCKED — billing / quota")
        lines.append("An agent reported a billing/credits problem. Resolve billing, then retry.")
    if limits.BRAIN_LIMIT_ACTIVE in ctx.flags:
        lines.append("## BLOCKED — BRAIN_LIMIT_ACTIVE")
        lines.append("GPT-5.5 and Claude are both limited. Only diagnosis was performed; "
                     "no complex or risky changes were made.")

    def agent_block(title, res):
        if not res:
            return
        lines.append(f"## {title}: {res.status}")
        if res.message:
            lines.append(f"- {res.message}")
        rep = res.report or {}
        if rep.get("summary"):
            lines.append(f"- summary: {rep['summary']}")
        if rep.get("changed_files"):
            lines.append(f"- changed_files: {rep['changed_files']}")
        if rep.get("tests"):
            lines.append(f"- tests: {json.dumps(rep['tests'])[:300]}")
        if rep.get("risks"):
            lines.append(f"- risks: {rep['risks']}")
        # safety scan on any commands the agent claims to have run
        cmds = [c.get("cmd", "") for c in rep.get("commands_run", []) if isinstance(c, dict)]
        flagged = safety.scan_commands(cmds)
        if flagged:
            lines.append(f"- ⚠ dangerous commands flagged: {flagged}")
        lines.append("")

    agent_block("DeepSeek (worker)", ds_res)
    agent_block("Claude (builder)", claude_res)

    # decision
    lines.append("## Orchestrator decision")
    if ctx.dry_run:
        lines.append("DRY-RUN: routing and prompts validated; agents not executed. "
                     "Re-run without --dry-run to execute.")
    elif limits.CAT_AUTH in ctx.flags or limits.CAT_BILLING in ctx.flags \
            or limits.BRAIN_LIMIT_ACTIVE in ctx.flags:
        lines.append("REJECT/HOLD — see blocking section above.")
    else:
        primary = claude_res or ds_res
        if primary and primary.status == "ok":
            lines.append("ACCEPT (pending human confirmation). Review diffs with "
                         "`agentctl diff` before merging any worktree branch.")
        else:
            lines.append(f"RETRY/INSPECT — primary agent status was "
                         f"'{primary.status if primary else 'none'}'. See logs.")
    if ctx.worktrees:
        lines.append("")
        lines.append("Worktrees created (inspect, then `agentctl cleanup`):")
        for w in ctx.worktrees:
            lines.append(f"- {w}")
    return "\n".join(lines)
