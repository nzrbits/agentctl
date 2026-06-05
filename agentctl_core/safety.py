"""Detection of destructive / risky shell commands.

agentctl never runs these on its own. DeepSeek is not allowed to run them at
all; Claude only after explicit approval. The orchestrator must flag them.
"""
from __future__ import annotations

import re

# (compiled regex, human reason). Conservative, false-positive-tolerant:
# better to flag and ask than to silently run something destructive.
_DANGEROUS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.I), "recursive force delete (rm -rf)"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "git reset --hard discards work"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.I), "git clean -f deletes untracked files"),
    (re.compile(r"\bgit\s+push\s+.*(--force|-f)\b", re.I), "force push rewrites remote history"),
    (re.compile(r"\bgit\s+checkout\s+--\s+\.", re.I), "git checkout -- . discards local changes"),
    (re.compile(r"\b(drop|truncate)\s+(database|table|schema)\b", re.I), "destructive SQL"),
    (re.compile(r"\bterraform\s+(apply|destroy)\b", re.I), "terraform mutation"),
    (re.compile(r"\b(tofu)\s+(apply|destroy)\b", re.I), "opentofu mutation"),
    (re.compile(r"\bkubectl\s+(apply|delete|patch|edit|replace)\b", re.I), "kubectl mutation"),
    (re.compile(r"\bhelm\s+(install|upgrade|uninstall|delete)\b", re.I), "helm mutation"),
    (re.compile(r"\baz\s+\S+\s+(create|update|delete)\b", re.I), "azure mutation"),
    (re.compile(r"\b(qm|pvesh)\s+(set|create|delete|destroy|start|stop)\b", re.I), "proxmox mutation"),
    (re.compile(r"\b(shutdown|reboot|halt)\b", re.I), "host power state change"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=.*of=/dev/", re.I), "disk format / overwrite"),
    (re.compile(r":\(\)\s*\{.*\}\s*;\s*:", re.I), "fork bomb"),
    (re.compile(r">\s*/dev/(sd|disk|nvme)", re.I), "raw device write"),
]


def is_dangerous(cmd: str) -> tuple[bool, str]:
    """Return (True, reason) if the command looks destructive."""
    if not cmd:
        return (False, "")
    for pat, reason in _DANGEROUS:
        if pat.search(cmd):
            return (True, reason)
    return (False, "")


def scan_commands(cmds: list[str]) -> list[dict]:
    """Return a flag record for each dangerous command in the list."""
    out = []
    for c in cmds:
        bad, reason = is_dangerous(c)
        if bad:
            out.append({"cmd": c, "reason": reason})
    return out
