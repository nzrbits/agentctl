"""`agentctl run`: orchestrate DeepSeek retrieval with optional GPT-5.5 review.

Routing philosophy:
  1. DeepSeek carries token-heavy retrieval and emits raw receipts.
  2. Local synthesis produces the stable decision from those receipts.
  3. GPT-5.5 review is optional best-effort only. The free opencode/OAuth route
     can return empty output, so it must never block, retry-loop, or false-accept.

Everything is logged. No infra is mutated. Agents run in isolated git
worktrees when possible so the main working tree is never touched.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
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
    caller: str = "local"
    timeout_seconds: int | None = None
    started_at: float = field(default_factory=time.time)
    snapshot: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    active_limit_categories: set[str] = field(default_factory=set)
    worktrees: list[str] = field(default_factory=list)
    route: str = "deepseek"

    def log(self, kind: str, **data):
        evt = {"ts": _now_tag(), "kind": kind, **data}
        self.events.append(evt)
        if self.config.get("ui", {}).get("show_internal_events", False):
            print(f"  [{kind}] " + data.get("msg", json.dumps({k: v for k, v in data.items() if k != 'msg'})[:160]))

    def live(self, msg: str):
        if self.config.get("ui", {}).get("quiet", False):
            return
        print(f"agentctl: {msg}", flush=True)

    def remaining_seconds(self) -> int | None:
        if self.timeout_seconds is None:
            return None
        elapsed = time.time() - self.started_at
        return max(1, int(self.timeout_seconds - elapsed))


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
        "escalation_criteria": ["insufficient evidence -> retry_deepseek", "unsafe/unclear -> ask_user"],
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
# routing plan
# --------------------------------------------------------------------------- #
def _is_complex(task: str) -> bool:
    t = task.lower()
    return any(h in t for h in _COMPLEX_HINTS)


def _is_security_sensitive(task: str) -> bool:
    t = task.lower()
    return any(h in t for h in _SECURITY_HINTS)


def _gpt_available(ctx: RunContext) -> bool:
    gpt = ctx.config.get("agents", {}).get("gpt55", {})
    if not gpt.get("enabled", True):
        return False
    if gpt.get("type") == "http_openai":
        # deterministic route: available iff the API key is set
        return bool(os.environ.get(gpt.get("api_key_env", "OPENAI_API_KEY")))
    cmd = gpt.get("command")
    return bool(cmd) and cmd != "CONFIGURE_ME" and bool(shutil.which(cmd))


def _resolve_caller(config: dict, caller: str) -> str:
    if caller != "auto":
        return caller
    env_caller = os.environ.get("AGENTCTL_CALLER", "").strip().lower()
    if env_caller in ("gpt55", "local"):
        return env_caller
    configured = config.get("orchestrator", {}).get("default_caller", "local")
    return configured if configured in ("gpt55", "local") else "local"


def _optional_reviewer(ctx: RunContext) -> str | None:
    if not ctx.config.get("orchestrator", {}).get("review_required", True):
        return None
    if ctx.route == "deepseek":
        return None
    if ctx.route == "deepseek-gpt55":
        return "gpt55" if _gpt_available(ctx) else None
    if ctx.route == "deepseek-claude":
        return "claude"
    if ctx.route == "deepseek-chatgpt":
        return "chatgpt"  # manual ChatGPT handoff — always available, no agent tokens
    policy = ctx.config.get("orchestrator", {}).get("review_policy", "best_effort_gpt55")
    if policy == "none":
        return None
    if policy in ("best_effort_gpt55", "gpt55_optional", "reciprocal") and _gpt_available(ctx):
        return "gpt55"
    return None


def plan(ctx: RunContext) -> dict:
    """Produce a stable DeepSeek-first routing plan."""
    complex_task = _is_complex(ctx.task)
    security = _is_security_sensitive(ctx.task)

    if _gpt_available(ctx):
        orchestrator = "local+gpt55_optional"
    else:
        orchestrator = "local"
        ctx.flags.add(limits.GPT_LIMIT_ACTIVE)
        ctx.log("orchestrator", msg="GPT-5.5 not wired/available -> local synthesis only. GPT_LIMIT_ACTIVE")

    ocfg = ctx.config.get("orchestrator", {})

    # Token-efficient fixed role order: DeepSeek retrieves evidence first;
    # local synthesis is stable; GPT-5.5 may review if the route works.
    first = "deepseek"
    reviewer = _optional_reviewer(ctx)
    reason = "role policy: DeepSeek retrieves receipts, local synthesis decides"
    if complex_task or security:
        reason = ("task is complex/security-sensitive; DeepSeek still prepares evidence first; "
                  "local synthesis must avoid risky acceptance without enough evidence")
    return {
        "orchestrator": orchestrator,
        "complex": complex_task,
        "security_sensitive": security,
        "first_agent": first,
        "reasoning_summary": reason,
        "review_required": ocfg.get("review_required", True),
        "review_policy": ocfg.get("review_policy", "best_effort_gpt55"),
        "caller": ctx.caller,
        "reviewer": reviewer,
        "role_policy": ocfg.get("role_policy", {}),
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


def _needs_review(res: adapters.AgentResult, plan_info: dict, allow_edit: bool) -> bool:
    """Run only the optional configured reviewer; never escalate to Claude implicitly."""
    if not plan_info.get("review_required", True):
        return False
    if not plan_info.get("reviewer"):
        return False
    if plan_info.get("review_policy") in ("best_effort_gpt55", "gpt55_optional", "reciprocal"):
        return True
    if allow_edit or plan_info["complex"] or plan_info["security_sensitive"]:
        return True
    rep = res.report or {}
    if rep.get("status") in ("needs_claude", "needs_reviewer"):
        return True
    if rep.get("recommended_next_action") in ("escalate_claude", "review"):
        return True
    if res.status in ("error", "timeout"):
        return True
    return False


def _cap_timeout(ctx: RunContext, cap_key: str, fallback: int) -> int:
    cap = int(ctx.config.get("orchestrator", {}).get(cap_key, fallback))
    remaining = ctx.remaining_seconds()
    if remaining is not None:
        cap = min(cap, remaining)
    return max(1, cap)


def _idle_timeout(ctx: RunContext) -> int:
    return max(1, int(ctx.config.get("orchestrator", {}).get("idle_timeout_seconds", 30)))


def _harvest_deepseek_evidence(ctx: RunContext, ds_res: adapters.AgentResult, dry_run: bool) -> None:
    """Use DeepSeek as a RETRIEVAL worker, never as a reasoner.

    Top priority of this orchestrator: push the token-heavy retrieval (file reads,
    greps, log dumps) onto DeepSeek, but rely on local synthesis / optional GPT-5.5
    review for ALL logic.
    So we always replace DeepSeek's deliverable with the raw tool receipts (each
    tool call's input + output) extracted from its event stream, and demote any
    prose it produced to an untrusted note. The reviewer synthesizes from the
    receipts. This also makes empty/tool-only DeepSeek turns harmless.
    """
    if dry_run or ds_res.status != "ok":
        return
    receipts = adapters.collect_tool_receipts(ds_res.stdout or "")
    note = adapters.collect_stream_text(ds_res.stdout or "").strip()
    if not receipts and not note:
        return  # nothing gathered at all
    report = {
        "status": "evidence",
        "role": "retrieval_only",
        "summary": (f"DeepSeek retrieval: {len(receipts)} tool receipts "
                    "(raw file/log/command evidence). Reasoning delegated to local synthesis."),
        "receipt_count": len(receipts),
        "tool_receipts": receipts,
    }
    if note:
        # kept for traceability only; reviewer is told NOT to trust it
        report["deepseek_note_untrusted"] = note[:1500]
    ds_res.report = report
    ds_res.message = (ds_res.message + " " if ds_res.message else "") + \
        f"[retrieval: {len(receipts)} receipts; reasoning → local synthesis]"
    ctx.log("evidence", msg=f"deepseek retrieval: {len(receipts)} receipts → local synthesis")
    ctx.live(f"primary analysis: {len(receipts)} receipts gathered → local synthesis")


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def _resolve_route(config: dict, route: str, caller: str) -> str:
    if route != "auto":
        return route
    configured = config.get("orchestrator", {}).get("default_route")
    if configured in ("deepseek", "deepseek-gpt55", "deepseek-claude", "deepseek-chatgpt"):
        return configured
    if caller == "claude":
        return "deepseek-gpt55"
    if caller == "gpt55":
        return "deepseek-claude"
    return "deepseek"


def run(task: str, cwd: Path, dry_run: bool, allow_edit: bool,
        timeout_seconds: int | None = None, caller: str = "auto", route: str = "auto") -> dict:
    cfg, used = cfg_mod.load_config(cwd)
    cfg_mod.load_dotenv(cwd)
    resolved_caller = _resolve_caller(cfg, caller)
    resolved_route = _resolve_route(cfg, route, resolved_caller)
    if timeout_seconds is None:
        timeout_seconds = cfg.get("orchestrator", {}).get("run_timeout_seconds")

    runs_root = cwd / ".agent-runs"
    runs_root.mkdir(exist_ok=True)
    run_dir = runs_root / f"{_now_tag()}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True)

    ctx = RunContext(task=task, cwd=cwd, run_dir=run_dir, config=cfg,
                     dry_run=dry_run, allow_edit=allow_edit,
                     caller=resolved_caller,
                     timeout_seconds=timeout_seconds,
                     route=resolved_route)

    ctx.live(f"run {run_dir.name}" + (" (dry-run)" if dry_run else ""))
    if timeout_seconds is not None:
        ctx.live(f"timeout budget: {timeout_seconds}s")
    ctx.live(f"task: {_short(task, 180)}")

    # 1. snapshot
    ctx.snapshot = repo_mod.snapshot(cwd)
    (run_dir / "task.txt").write_text(task + "\n")
    (run_dir / "snapshot.json").write_text(json.dumps(ctx.snapshot, indent=2))
    ctx.log("snapshot", msg=f"git={ctx.snapshot['is_git_repo']} "
                            f"files={ctx.snapshot['file_count']} "
                            f"lang={ctx.snapshot['package'].get('language')}")
    ctx.live(f"snapshot ready: {ctx.snapshot['file_count']} files, git={ctx.snapshot['is_git_repo']}")

    # 2. plan / route
    plan_info = plan(ctx)
    (run_dir / "plan.json").write_text(json.dumps(plan_info, indent=2))
    ctx.log("plan", msg=f"first={plan_info['first_agent']} complex={plan_info['complex']} "
                        f"orchestrator={plan_info['orchestrator']}")
    reviewer_name = plan_info.get("reviewer") or "none"
    ctx.live(f"plan: route={resolved_route}, DeepSeek retrieves, optional review={reviewer_name}")

    results: list[adapters.AgentResult] = []
    ds_res = None
    review_res = None

    def checkpoint(stage: str):
        _persist_run_log(ctx, used, plan_info, results, stage=stage)
        (run_dir / "review-report.md").write_text(
            _review(ctx, plan_info, ds_res, review_res, stage=stage)
        )

    checkpoint("planned")

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
    ctx.live("working: primary analysis started")
    ds_res = adapters.run_agent("deepseek", ds_cfg, ds_prompt, ds_cwd,
                                dry_run=dry_run, timeout=_cap_timeout(ctx, "agent_timeout_seconds", 90),
                                idle_timeout=_idle_timeout(ctx))
    # DeepSeek via opencode occasionally idle-stalls mid-run. One bounded retry
    # recovers most of these (the stall is intermittent, not deterministic).
    if not dry_run and ds_res.status == "timeout" and (ctx.remaining_seconds() or 999) > 30:
        ctx.log("retry", msg="deepseek idle-stalled; one bounded retry")
        ctx.live("primary analysis: retry after timeout")
        ds_res = adapters.run_agent("deepseek", ds_cfg, ds_prompt, ds_cwd,
                                    dry_run=dry_run, timeout=_cap_timeout(ctx, "agent_timeout_seconds", 90),
                                    idle_timeout=_idle_timeout(ctx))
    _harvest_deepseek_evidence(ctx, ds_res, dry_run)
    results.append(ds_res)
    _persist_result(run_dir, "deepseek", ds_res)
    ctx.log("result", msg=f"deepseek -> {ds_res.status} {ds_res.message}")
    ctx.live(f"primary analysis: {ds_res.status}{_agent_hint(ds_res)}")
    checkpoint("deepseek_done")

    if ds_res.status == "limit":
        directive = _handle_limit(ctx, ds_res)
        ctx.log("limit", msg=f"deepseek {ds_res.limit.get('category')} -> {directive}")

    # 4. decide whether the optional GPT-5.5 reviewer should validate DeepSeek's output
    needs_review = _needs_review(ds_res, plan_info, allow_edit)
    if timeout_seconds is not None and ds_res.status == "timeout":
        needs_review = False
        ctx.log("timeout", msg="skip review: DeepSeek consumed run timeout budget")
    if timeout_seconds is not None and needs_review:
        remaining = ctx.remaining_seconds() or 0
        min_review_seconds = min(10, max(1, timeout_seconds // 3))
        if remaining < min_review_seconds:
            needs_review = False
            ctx.log("timeout", msg=f"skip review: only {remaining}s left in run budget")

    if needs_review:
        reviewer = plan_info.get("reviewer")
        review_res = _run_review_agent(ctx, cfg, run_dir, reviewer, ds_res, dry_run, cwd)
        if review_res:
            results.append(review_res)
            ctx.log("result", msg=f"{reviewer} -> {review_res.status} {review_res.message}")
            ctx.live(f"validation: {reviewer} {review_res.status}{_agent_hint(review_res)}")
            checkpoint(f"{reviewer}_review_done")
            if review_res.status == "limit":
                directive = _handle_limit(ctx, review_res)
                ctx.log("limit", msg=f"{reviewer} {review_res.limit.get('category')} -> {directive}")

    # 5. final flags
    for f in limits.status_flags(ctx.active_limit_categories):
        ctx.flags.add(f)

    # 6. review report
    review = _review(ctx, plan_info, ds_res, review_res)
    (run_dir / "review-report.md").write_text(review)

    # 7. full log
    log = _persist_run_log(ctx, used, plan_info, results, stage="complete")

    print(_console_summary(ctx, ds_res, review_res, run_dir))
    return log


def _run_review_agent(ctx: RunContext, cfg: dict, run_dir: Path, reviewer: str | None,
                      ds_res: adapters.AgentResult, dry_run: bool, cwd: Path) -> adapters.AgentResult | None:
    if not reviewer:
        return None
    agent_cfg = cfg.get("agents", {}).get(reviewer, {})
    if reviewer in ("gpt55", "chatgpt"):
        role = "gpt55-reviewer"
        goal = (
            "You are an optional best-effort reviewer. DeepSeek is a RETRIEVAL worker only: its raw tool "
            "receipts (file/log/command evidence) are in `deepseek_findings`. Do NOT trust "
            "any DeepSeek prose, summary, or conclusion — derive everything yourself from the "
            "raw evidence. Produce synthesis: correctness, scope, acceptance criteria, risks, "
            "and an accept/retry_deepseek/ask_user decision, citing the evidence. Do NOT call retrieval "
            "tools yourself. If evidence is insufficient, return retry_deepseek naming exactly what to retrieve."
        )
        template = _load_prompt_template("gpt55-reviewer.md") or _load_prompt_template("gpt55-orchestrator.md")
    elif reviewer == "claude":
        role = "claude-reviewer"
        goal = (
            "You are an optional reviewer. DeepSeek is a RETRIEVAL worker only: its raw tool receipts "
            "are in `deepseek_findings`. Review architecture/design/risk from that evidence. "
            "Do not implement. Return accept, retry_deepseek, ask_user, or escalate with evidence."
        )
        template = _load_prompt_template("claude-reviewer.md")
    else:
        return None
    contract = _build_contract(ctx, role, goal, False)
    if ds_res.report:
        contract["deepseek_findings"] = ds_res.report
    prompt = _compose_agent_prompt(ctx, template, contract)
    (run_dir / f"prompt-{reviewer}.md").write_text(prompt)
    if reviewer == "chatgpt":
        return _chatgpt_handoff(ctx, run_dir, prompt)
    ctx.log("delegate", msg=f"delegate_{reviewer} (review-only)")
    ctx.live(f"validation: {reviewer} review started")
    timeout = _cap_timeout(ctx, "review_timeout_seconds", 45)
    res = adapters.run_agent(reviewer, agent_cfg, prompt, cwd, dry_run=dry_run,
                             timeout=timeout, idle_timeout=_idle_timeout(ctx))
    # The opencode/OAuth route to GPT-5.5 can return an empty stream (exit 0, no text).
    # So the GPT-5.5 review is best-effort: an empty result is marked unavailable and
    # never blocks, retries or false-accepts. Local synthesis owns the decision.
    if reviewer == "gpt55" and res.status in ("ok", "timeout"):
        if agent_cfg.get("type") == "http_openai":
            # deterministic route returns plain text, not an NDJSON event stream
            empty = not (res.stdout or "").strip() and not res.report
        else:
            empty = not adapters.collect_stream_text(res.stdout or "").strip() and not res.report
        if empty:
            res.status = "unavailable"
            res.message = ("GPT-5.5 review returned no usable output — local synthesis used "
                           "(opencode/OAuth route is flaky; for a deterministic route set "
                           "OPENAI_API_KEY with API billing)")
    _persist_result(run_dir, reviewer, res)
    return res


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


def _chatgpt_handoff(ctx: RunContext, run_dir: Path, prompt: str) -> adapters.AgentResult:
    """GPT-5.5 logic via the ChatGPT subscription — deterministic, no agent tokens.

    Writes a paste-ready prompt and returns a pending handoff. The user pastes the
    prompt into ChatGPT, saves the reply, and runs `agentctl ingest <run>`.
    """
    pf = run_dir / "chatgpt-prompt.md"
    pf.write_text(prompt)
    reply = run_dir / "chatgpt-reply.md"
    res = adapters.AgentResult(agent="chatgpt", status="handoff_pending",
                               command_display="manual ChatGPT handoff")
    res.message = (f"GPT-5.5 via ChatGPT — paste {pf.name} into ChatGPT, save the reply to "
                   f"{reply.name}, then run: agentctl ingest {run_dir.name}")
    ctx.log("handoff", msg="chatgpt handoff prompt written")
    ctx.live("validation: GPT-5.5 via ChatGPT — manual handoff")
    ctx.live(f"  1) paste: {pf}")
    ctx.live(f"  2) save reply to: {reply}")
    ctx.live(f"  3) run: agentctl ingest {run_dir.name}")
    _persist_result(run_dir, "chatgpt", res)
    return res


def ingest(run_dir: Path) -> int:
    """Ingest a pasted ChatGPT reply (GPT-5.5 logic) into an existing run."""
    if not run_dir.exists():
        print(f"no such run: {run_dir}")
        return 1
    reply = run_dir / "chatgpt-reply.md"
    if not reply.exists() or not reply.read_text().strip():
        print(f"paste ChatGPT's answer into {reply} first, then re-run ingest.")
        return 1
    text = reply.read_text()
    d = run_dir / "agent-chatgpt"
    d.mkdir(exist_ok=True)
    (d / "stdout.txt").write_text(text)
    report = adapters.extract_json(text)
    if report:
        (d / "report.json").write_text(json.dumps(report, indent=2))
    (d / "result.json").write_text(json.dumps(
        {"agent": "chatgpt", "status": "ok", "message": "ingested from ChatGPT",
         "report": report}, indent=2))
    rr = run_dir / "review-report.md"
    decision = (report or {}).get("recommended_next_action")
    section = ("\n\n## GPT-5.5 via ChatGPT — ingested\n\n"
               + (f"- decision: {decision}\n\n" if decision else "")
               + "<details><summary>answer</summary>\n\n"
               + "\n".join(f"> {ln}" for ln in text.splitlines())
               + "\n\n</details>\n")
    rr.write_text((rr.read_text() if rr.exists() else "") + section)
    print(f"ingested ChatGPT reply -> {d}")
    if decision:
        print(f"decision: {decision}")
    return 0


def _persist_run_log(ctx: RunContext, used: list[Path], plan_info: dict,
                     results: list[adapters.AgentResult], stage: str) -> dict:
    log = {
        "run": ctx.run_dir.name,
        "task": ctx.task,
        "stage": stage,
        "dry_run": ctx.dry_run,
        "config_files": [str(p) for p in used],
        "plan": plan_info,
        "flags": sorted(ctx.flags),
        "limit_categories": sorted(ctx.active_limit_categories),
        "worktrees": ctx.worktrees,
        "events": ctx.events,
        "results": [r.as_dict() for r in results],
    }
    (ctx.run_dir / "log.json").write_text(json.dumps(log, indent=2))
    return log


def _agent_hint(res: adapters.AgentResult) -> str:
    if res.message:
        return f" ({_short(res.message, 90)})"
    rep = res.report or {}
    if rep.get("summary"):
        return f" ({_short(str(rep['summary']), 90)})"
    return ""


def _recommendation(ds_res: adapters.AgentResult | None,
                    review_res: adapters.AgentResult | None) -> str:
    primary = _primary_result(ds_res, review_res)
    if not primary:
        return "inspect"
    rep = primary.report or {}
    return rep.get("recommended_next_action") or ("accept" if primary.status == "ok" else "inspect")


def _primary_result(ds_res: adapters.AgentResult | None,
                    review_res: adapters.AgentResult | None) -> adapters.AgentResult | None:
    if review_res and review_res.status not in ("unavailable", "not_configured",
                                                "tool_missing", "handoff_pending"):
        return review_res
    return ds_res


def _console_summary(ctx: RunContext, ds_res: adapters.AgentResult | None,
                     review_res: adapters.AgentResult | None, run_dir: Path) -> str:
    primary = _primary_result(ds_res, review_res)
    status = primary.status if primary else "none"
    lines = [
        "",
        "agentctl: complete",
        f"agentctl: status: {status}",
        f"agentctl: recommendation: {_recommendation(ds_res, review_res)}",
    ]
    if ctx.flags:
        lines.append(f"agentctl: flags: {', '.join(sorted(ctx.flags))}")
    if primary and primary.report and primary.report.get("summary"):
        lines.append(f"agentctl: summary: {_short(str(primary.report['summary']), 240)}")
    if primary and primary.report and primary.report.get("risks"):
        lines.append(f"agentctl: risks: {_short(str(primary.report['risks']), 240)}")
    lines.append(f"agentctl: artifacts: {run_dir}")
    lines.append(f"agentctl: review: {run_dir / 'review-report.md'}")
    return "\n".join(lines)


def _review(ctx: RunContext, plan_info: dict, ds_res: adapters.AgentResult | None,
            review_res: adapters.AgentResult | None, stage: str = "complete") -> str:
    """Local synthesis review. Optional GPT-5.5 review may add evidence; this always runs."""
    lines = ["# agentctl review report", ""]
    lines.append(f"- run: `{ctx.run_dir.name}`")
    lines.append(f"- task: {ctx.task}")
    lines.append(f"- mode: {'DRY-RUN (no agents executed)' if ctx.dry_run else 'live'}")
    lines.append(f"- stage: {stage}")
    lines.append(f"- orchestrator: {plan_info['orchestrator']}")
    lines.append(f"- caller: {plan_info.get('caller')}")
    lines.append(f"- route: deepseek retrieval → local synthesis" +
                 (f" + {plan_info.get('reviewer')} optional review" if review_res else ""))
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
        lines.append("External reasoning routes are limited. Only diagnosis was performed; "
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
        # Surface the agent's actual prose answer, not just the parsed report.
        # opencode/claude emit NDJSON event streams; the real findings live in the
        # reassembled text, which is dropped by _extract_report when it is not JSON.
        answer = adapters.collect_stream_text(res.stdout or "").strip()
        if answer:
            if len(answer) > 2000:
                answer = answer[:2000] + " …[truncated — full output in the agent's stdout.txt]"
            lines.append("")
            lines.append("<details><summary>findings</summary>")
            lines.append("")
            for ln in answer.splitlines():
                lines.append(f"> {ln}")
            lines.append("")
            lines.append("</details>")
        # safety scan on any commands the agent claims to have run
        cmds = [c.get("cmd", "") for c in rep.get("commands_run", []) if isinstance(c, dict)]
        flagged = safety.scan_commands(cmds)
        if flagged:
            lines.append(f"- ⚠ dangerous commands flagged: {flagged}")
        lines.append("")

    agent_block("DeepSeek Execution Worker", ds_res)
    if plan_info.get("reviewer") == "gpt55":
        agent_block("GPT-5.5 Lead Reviewer", review_res)
    elif plan_info.get("reviewer") == "chatgpt":
        agent_block("GPT-5.5 via ChatGPT (manual handoff)", review_res)
    elif plan_info.get("reviewer") == "claude":
        agent_block("Claude Reviewer", review_res)

    # decision
    lines.append("## Orchestrator decision")
    if ctx.dry_run:
        lines.append("DRY-RUN: routing and prompts validated; agents not executed. "
                     "Re-run without --dry-run to execute.")
    elif limits.CAT_AUTH in ctx.flags or limits.CAT_BILLING in ctx.flags \
            or limits.BRAIN_LIMIT_ACTIVE in ctx.flags:
        lines.append("REJECT/HOLD — see blocking section above.")
    else:
        primary = _primary_result(ds_res, review_res)
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
