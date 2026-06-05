"""Interactive agentctl console (REPL).

Type a task and press Enter -> it runs through the orchestrator.
Lines starting with `/` are meta commands (`/help`, `/doctor`, `/cd`, ...).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:  # arrow-key history; libedit on macOS, fine either way
    import readline  # noqa: F401
except Exception:  # pragma: no cover
    pass

from . import __version__
from . import doctor as doctor_mod
from . import runner as runner_mod

BANNER = r"""
  agentctl {ver}  — interactive console
  GPT-5.5 (brain) -> Claude (builder) -> DeepSeek (cheap worker)

  Type a task and press Enter to run it.
  Meta commands start with '/'. Type /help for the list, /exit to quit.
""".strip("\n")

HELP = """
commands:
  <task text>          run a task through the orchestrator (DeepSeek first)
  /help                show this help
  /doctor              run environment diagnosis
  /status [N]          list recent runs (default 10)
  /review [run]        show last review report (or a specific run dir)
  /diff                show diffs from agent worktrees
  /cleanup [--yes]     remove worktrees (preview unless --yes)
  /cd <path>           change the target repo (where tasks run)
  /pwd                 show current target repo
  /edit on|off         toggle --allow-edit for following runs   (now: {edit})
  /dry on|off          toggle dry-run (plan only, no agents)     (now: {dry})
  /exit, /quit         leave the console
""".rstrip()


class Console:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.allow_edit = False
        self.dry_run = False

    # ---- meta state lines ----
    def _state(self) -> str:
        tags = []
        if self.allow_edit:
            tags.append("edit")
        if self.dry_run:
            tags.append("dry")
        suffix = (" [" + ",".join(tags) + "]") if tags else ""
        return f"{self.cwd.name or self.cwd}{suffix}"

    def _prompt(self) -> str:
        return f"agentctl:{self._state()}> "

    # ---- meta command handlers ----
    def _do_cd(self, arg: str):
        if not arg:
            print("usage: /cd <path>")
            return
        target = Path(os.path.expanduser(arg))
        if not target.is_absolute():
            target = (self.cwd / target)
        target = target.resolve()
        if target.is_dir():
            self.cwd = target
            print(f"target repo -> {self.cwd}")
        else:
            print(f"not a directory: {target}")

    def _toggle(self, name: str, arg: str):
        val = arg.strip().lower()
        if val not in ("on", "off"):
            print(f"usage: /{name} on|off")
            return
        on = val == "on"
        if name == "edit":
            self.allow_edit = on
        else:
            self.dry_run = on
        print(f"{name} = {'on' if on else 'off'}")

    def _passthru(self, args: list[str]):
        """Run another agentctl subcommand in the target repo via the CLI."""
        from .cli import main as cli_main
        old = Path.cwd()
        try:
            os.chdir(self.cwd)
            cli_main(args)
        except SystemExit:
            pass
        finally:
            os.chdir(old)

    # ---- main loop ----
    def run(self) -> int:
        print(BANNER.format(ver=__version__))
        print(f"  target repo: {self.cwd}\n")
        while True:
            try:
                line = input(self._prompt())
            except (EOFError, KeyboardInterrupt):
                print("\nbye.")
                return 0

            line = line.strip()
            if not line:
                continue

            if line.startswith("/"):
                parts = line[1:].split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd in ("exit", "quit", "q"):
                    print("bye.")
                    return 0
                elif cmd in ("help", "h", "?"):
                    print(HELP.format(edit="on" if self.allow_edit else "off",
                                      dry="on" if self.dry_run else "off"))
                elif cmd == "doctor":
                    self._passthru(["doctor"])
                elif cmd == "status":
                    self._passthru(["status"] + (["--limit", arg] if arg.isdigit() else []))
                elif cmd == "review":
                    self._passthru(["review"] + (["--run", arg] if arg else []))
                elif cmd == "diff":
                    self._passthru(["diff"])
                elif cmd == "cleanup":
                    self._passthru(["cleanup"] + (["--yes"] if "--yes" in arg or "-y" in arg else []))
                elif cmd == "cd":
                    self._do_cd(arg)
                elif cmd == "pwd":
                    print(self.cwd)
                elif cmd in ("edit", "dry"):
                    self._toggle(cmd, arg)
                else:
                    print(f"unknown command: /{cmd}  (try /help)")
                continue

            # otherwise: it's a task
            try:
                runner_mod.run(task=line, cwd=self.cwd,
                               dry_run=self.dry_run, allow_edit=self.allow_edit)
            except Exception as e:  # noqa: BLE001 - keep the REPL alive
                print(f"run failed: {e}")
        # unreachable


def start(cwd: Path) -> int:
    return Console(cwd).run()
