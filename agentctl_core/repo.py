"""Read-only repository snapshot: git state, structure, package manager, tests."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def is_git_repo(cwd: Path) -> bool:
    rc, out, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0 and out.strip() == "true"


def git_toplevel(cwd: Path) -> Path | None:
    rc, out, _ = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(out.strip()) if rc == 0 and out.strip() else None


def has_commits(cwd: Path) -> bool:
    rc, _, _ = _run(["git", "rev-parse", "HEAD"], cwd)
    return rc == 0


def detect_package_manager(cwd: Path) -> dict:
    """Best-effort detection of language + package manager + test command."""
    info: dict = {"language": None, "package_manager": None, "test_command": None,
                  "markers": []}
    files = {p.name for p in cwd.iterdir() if p.is_file()} if cwd.exists() else set()

    if "package.json" in files:
        info["language"] = "javascript/typescript"
        info["markers"].append("package.json")
        if (cwd / "pnpm-lock.yaml").exists():
            info["package_manager"] = "pnpm"
        elif (cwd / "yarn.lock").exists():
            info["package_manager"] = "yarn"
        elif (cwd / "bun.lockb").exists():
            info["package_manager"] = "bun"
        else:
            info["package_manager"] = "npm"
        # peek at scripts.test
        try:
            import json
            pkg = json.loads((cwd / "package.json").read_text())
            if isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("test"):
                info["test_command"] = f"{info['package_manager']} test"
        except Exception:
            pass
    elif "pyproject.toml" in files or "setup.py" in files or "requirements.txt" in files:
        info["language"] = "python"
        info["package_manager"] = "pip"
        for m in ("pyproject.toml", "setup.py", "requirements.txt"):
            if m in files:
                info["markers"].append(m)
        if (cwd / "tests").is_dir() or any(cwd.glob("test_*.py")):
            info["test_command"] = "python3 -m pytest -q || python3 -m unittest"
    elif "go.mod" in files:
        info["language"] = "go"
        info["package_manager"] = "go"
        info["test_command"] = "go test ./..."
        info["markers"].append("go.mod")
    elif "Cargo.toml" in files:
        info["language"] = "rust"
        info["package_manager"] = "cargo"
        info["test_command"] = "cargo test"
        info["markers"].append("Cargo.toml")

    return info


def file_tree(cwd: Path, max_files: int = 200) -> list[str]:
    """Tracked files if git, else a shallow filesystem walk. Skips noise dirs."""
    if is_git_repo(cwd):
        rc, out, _ = _run(["git", "ls-files"], cwd)
        if rc == 0:
            files = [l for l in out.splitlines() if l.strip()]
            if files:  # empty when repo has no commits / nothing staged
                return sorted(files)[:max_files]
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".agent-runs",
            "dist", "build", ".next", "target"}
    out_files: list[str] = []
    for p in sorted(cwd.rglob("*")):
        if any(part in skip for part in p.parts):
            continue
        if p.is_file():
            out_files.append(str(p.relative_to(cwd)))
        if len(out_files) >= max_files:
            break
    return out_files


def snapshot(cwd: Path) -> dict:
    """Full read-only repo snapshot used by the orchestrator and run log."""
    snap: dict = {"cwd": str(cwd), "is_git_repo": is_git_repo(cwd)}
    if snap["is_git_repo"]:
        snap["branch"] = _run(["git", "branch", "--show-current"], cwd)[1].strip()
        snap["has_commits"] = has_commits(cwd)
        snap["status"] = _run(["git", "status", "--short"], cwd)[1].strip().splitlines()
        snap["remotes"] = _run(["git", "remote", "-v"], cwd)[1].strip().splitlines()
        snap["recent_log"] = _run(
            ["git", "log", "--oneline", "-n", "10"], cwd
        )[1].strip().splitlines()
        snap["toplevel"] = str(git_toplevel(cwd) or cwd)
    snap["package"] = detect_package_manager(cwd)
    snap["files"] = file_tree(cwd)
    snap["file_count"] = len(snap["files"])
    return snap


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None
