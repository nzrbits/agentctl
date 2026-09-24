"""agentctl command-line interface."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import config as cfg_mod
from . import doctor as doctor_mod
from . import runner as runner_mod


def _runs_root(cwd: Path) -> Path:
    return cwd / ".agent-runs"


def _list_runs(cwd: Path) -> list[Path]:
    root = _runs_root(cwd)
    if not root.exists():
        return []
    runs = [p for p in root.iterdir() if p.is_dir() and p.name != "worktrees"]
    return sorted(runs, key=lambda p: p.name)


def cmd_doctor(args) -> int:
    cwd = Path.cwd()
    checks = doctor_mod.collect(cwd)
    print(doctor_mod.render(checks))
    return doctor_mod.exit_code(checks)


def cmd_run(args) -> int:
    cwd = Path.cwd()
    runner_mod.run(
        task=args.task,
        cwd=cwd,
        dry_run=args.dry_run,
        allow_edit=args.allow_edit,
        timeout_seconds=args.timeout_seconds,
        caller=args.caller,
        route=args.route,
    )
    return 0


def cmd_status(args) -> int:
    cwd = Path.cwd()
    runs = _list_runs(cwd)
    if not runs:
        print("no runs yet (.agent-runs/ is empty)")
        return 0
    print(f"{len(runs)} run(s) in {_runs_root(cwd)}:\n")
    for r in runs[-args.limit:]:
        log_f = r / "log.json"
        flags, task, route = "", "", ""
        if log_f.exists():
            try:
                log = json.loads(log_f.read_text())
                task = log.get("task", "")[:60]
                flags = ",".join(log.get("flags", [])) or "-"
                agents = [res.get("agent") for res in log.get("results", [])]
                if "gpt55" in agents:
                    route = "gpt55-review"
                elif "claude" in agents:
                    route = "claude-review"
                elif "chatgpt" in agents:
                    route = "chatgpt-handoff"
                else:
                    route = "deepseek"
            except json.JSONDecodeError:
                pass
        print(f"  {r.name}")
        print(f"      task : {task}")
        print(f"      route: {route}   flags: {flags}")
    return 0


def _latest_run(cwd: Path) -> Path | None:
    runs = _list_runs(cwd)
    return runs[-1] if runs else None


def cmd_review(args) -> int:
    cwd = Path.cwd()
    run = Path(args.run) if args.run else _latest_run(cwd)
    if not run or not run.exists():
        print("no run found. Run `agentctl run \"...\"` first.")
        return 1
    report = run / "review-report.md"
    if report.exists():
        print(report.read_text())
        return 0
    print(f"no review-report.md in {run}")
    return 1


def cmd_diff(args) -> int:
    cwd = Path.cwd()
    wt_root = cwd / ".agent-runs" / "worktrees"
    found = False
    if wt_root.exists():
        for wt in sorted(wt_root.iterdir()):
            if not wt.is_dir():
                continue
            found = True
            print(f"=== diff for worktree {wt.name} ===")
            r = subprocess.run(["git", "diff", "HEAD"], cwd=str(wt),
                               capture_output=True, text=True)
            print(r.stdout or "(no committed-base diff)")
            st = subprocess.run(["git", "status", "--short"], cwd=str(wt),
                                capture_output=True, text=True)
            if st.stdout.strip():
                print("--- uncommitted in worktree ---")
                print(st.stdout)
    if not found:
        # fall back to main working tree diff
        print("=== diff for current working tree ===")
        r = subprocess.run(["git", "diff"], cwd=str(cwd), capture_output=True, text=True)
        print(r.stdout or "(no changes / not a git repo)")
    return 0


def cmd_cleanup(args) -> int:
    cwd = Path.cwd()
    wt_root = cwd / ".agent-runs" / "worktrees"
    removed = []
    if wt_root.exists():
        for wt in sorted(wt_root.iterdir()):
            if not wt.is_dir():
                continue
            if not args.yes:
                print(f"would remove worktree: {wt}  (re-run with --yes)")
                continue
            r = subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                               cwd=str(cwd), capture_output=True, text=True)
            if r.returncode == 0:
                removed.append(str(wt))
            else:
                print(f"  failed to remove {wt}: {r.stderr.strip()}")
        if args.yes:
            subprocess.run(["git", "worktree", "prune"], cwd=str(cwd),
                           capture_output=True, text=True)
            # delete the throwaway agentctl/* branches left behind by worktrees
            branches = subprocess.run(["git", "branch", "--list", "agentctl/*"],
                                      cwd=str(cwd), capture_output=True, text=True).stdout
            for b in (x.strip().lstrip("* ").strip() for x in branches.splitlines()):
                if b:
                    subprocess.run(["git", "branch", "-D", b], cwd=str(cwd),
                                   capture_output=True, text=True)
                    removed.append(f"branch {b}")
    if args.runs and args.yes:
        for r in _list_runs(cwd):
            shutil.rmtree(r, ignore_errors=True)
            removed.append(str(r))
    if removed:
        print("removed:")
        for x in removed:
            print(f"  {x}")
    elif args.yes:
        print("nothing to clean.")
    return 0


def cmd_ingest(args) -> int:
    cwd = Path.cwd()
    run = Path(args.run)
    if not run.exists():
        run = cwd / ".agent-runs" / args.run
    return runner_mod.ingest(run)


def cmd_shell(args) -> int:
    from . import shell as shell_mod
    return shell_mod.start(Path.cwd())


def cmd_selftest(args) -> int:
    """Run the bundled unit tests (limit detection + doctor)."""
    inst = cfg_mod.install_dir()
    print(f"running self-tests from {inst/'tests'} ...")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                        str(inst / "tests"), "-v"], cwd=str(inst))
    return r.returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentctl",
        description="Local multi-agent orchestrator with DeepSeek-first routes.",
    )
    p.add_argument("--version", action="version", version=f"agentctl {__version__}")
    # no subcommand -> interactive console
    sub = p.add_subparsers(dest="command", required=False)

    sp = sub.add_parser("shell", help="start the interactive console (default if no command)")
    sp.set_defaults(func=cmd_shell)

    sp = sub.add_parser("doctor", help="diagnose environment, tools, config, env vars")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("run", help="orchestrate a task across the agents")
    sp.add_argument("task", help="the user task, in quotes")
    sp.add_argument("--dry-run", action="store_true",
                    help="plan + write prompts/run-dir but do NOT execute agents")
    sp.add_argument("--allow-edit", action="store_true",
                    help="permit agents to edit files (in isolated worktrees)")
    sp.add_argument("--timeout-seconds", type=int,
                    help="overall run budget; writes timeout artifacts instead of relying on shell kill")
    sp.add_argument("--caller", choices=["local", "claude", "gpt55", "auto"], default="auto",
                    help="who owns the LOGIC: local/claude = Claude reasons from DeepSeek's "
                         "receipts (default, stable); gpt55 = optional GPT-5.5 review when it has tokens")
    sp.add_argument("--route",
                    choices=["auto", "deepseek", "deepseek-gpt55", "deepseek-claude", "deepseek-chatgpt"],
                    default="auto",
                    help="agent route; default from config = deepseek (workhorse=DeepSeek, logic=local/Claude). "
                         "deepseek-chatgpt = write a paste-ready GPT-5.5 prompt for ChatGPT (no agent tokens)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("ingest",
                        help="ingest a pasted ChatGPT reply (GPT-5.5 logic) into a run")
    sp.add_argument("run", help="run id or path under .agent-runs/")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("status", help="list recent runs")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("diff", help="show diffs from agent worktrees")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("review", help="print the review report of a run (latest by default)")
    sp.add_argument("--run", help="path to a specific run dir")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("cleanup", help="remove agent worktrees (and optionally run dirs)")
    sp.add_argument("--yes", action="store_true", help="actually remove (default: dry preview)")
    sp.add_argument("--runs", action="store_true", help="also delete .agent-runs/* run dirs")
    sp.set_defaults(func=cmd_cleanup)

    sp = sub.add_parser("selftest", help="run bundled unit tests")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):  # bare `agentctl` -> interactive console
        return cmd_shell(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
