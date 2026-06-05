"""Limit / quota / auth / billing / context detection.

This is intentionally dependency-free and string-based: agent CLIs report
problems very differently (plain text, JSON error blobs, HTTP status echoes),
so we scan raw stdout+stderr for known signals, classify the *kind* of
problem, and then map it to an agent-specific status flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Status flags surfaced to the orchestrator / user.
GPT_LIMIT_ACTIVE = "GPT_LIMIT_ACTIVE"
CLAUDE_LIMIT_ACTIVE = "CLAUDE_LIMIT_ACTIVE"
DEEPSEEK_LIMIT_ACTIVE = "DEEPSEEK_LIMIT_ACTIVE"
BRAIN_LIMIT_ACTIVE = "BRAIN_LIMIT_ACTIVE"  # gpt55 + claude both limited

# Problem *kinds* (independent of which agent hit them).
KIND_RATE = "rate_limit"
KIND_CONTEXT = "context_limit"
KIND_AUTH = "auth_error"
KIND_BILLING = "billing_error"
KIND_OVERLOADED = "overloaded"
KIND_TOOL = "unknown_tool_error"

# Final classification categories required by the spec.
CAT_GPT_LIMIT = "gpt_limit"
CAT_CLAUDE_LIMIT = "claude_limit"
CAT_DEEPSEEK_LIMIT = "deepseek_limit"
CAT_AUTH = "auth_error"
CAT_BILLING = "billing_error"
CAT_CONTEXT = "context_limit"
CAT_TOOL = "unknown_tool_error"

# (compiled regex, kind). Order matters: context/auth/billing are checked
# before the generic rate-limit bucket so a more specific kind wins.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # --- context / token window ---
    (re.compile(r"context[\s_]?length(\s+exceeded)?", re.I), KIND_CONTEXT),
    (re.compile(r"context_length_exceeded", re.I), KIND_CONTEXT),
    (re.compile(r"maximum\s+context", re.I), KIND_CONTEXT),
    (re.compile(r"token\s+limit", re.I), KIND_CONTEXT),
    (re.compile(r"prompt\s+is\s+too\s+long", re.I), KIND_CONTEXT),
    # --- auth ---
    (re.compile(r"unauthorized", re.I), KIND_AUTH),
    (re.compile(r"invalid\s+api[\s_]?key", re.I), KIND_AUTH),
    (re.compile(r"authentication", re.I), KIND_AUTH),
    (re.compile(r"permission\s+denied", re.I), KIND_AUTH),
    (re.compile(r"\b401\b", re.I), KIND_AUTH),
    (re.compile(r"\b403\b", re.I), KIND_AUTH),
    # --- billing / credits ---
    (re.compile(r"insufficient_quota", re.I), KIND_BILLING),
    (re.compile(r"billing", re.I), KIND_BILLING),
    (re.compile(r"credits?", re.I), KIND_BILLING),
    (re.compile(r"payment\s+required", re.I), KIND_BILLING),
    (re.compile(r"\b402\b", re.I), KIND_BILLING),
    # --- overloaded / transient ---
    (re.compile(r"overloaded", re.I), KIND_OVERLOADED),
    (re.compile(r"temporarily\s+unavailable", re.I), KIND_OVERLOADED),
    (re.compile(r"\b503\b", re.I), KIND_OVERLOADED),
    # --- rate / usage / quota ---
    (re.compile(r"rate[\s_]?limit(_exceeded)?", re.I), KIND_RATE),
    (re.compile(r"usage\s+limit", re.I), KIND_RATE),
    (re.compile(r"limit\s+reached", re.I), KIND_RATE),
    (re.compile(r"\bquota\b", re.I), KIND_RATE),
    (re.compile(r"too\s+many\s+requests", re.I), KIND_RATE),
    (re.compile(r"\b429\b", re.I), KIND_RATE),
    # --- tool / binary problems ---
    (re.compile(r"command\s+not\s+found", re.I), KIND_TOOL),
    (re.compile(r"no\s+such\s+file\s+or\s+directory", re.I), KIND_TOOL),
    (re.compile(r"is\s+not\s+recognized\s+as", re.I), KIND_TOOL),
]

# Kind -> precedence (higher wins when several kinds match).
_KIND_PRECEDENCE = {
    KIND_AUTH: 60,
    KIND_BILLING: 55,
    KIND_CONTEXT: 50,
    KIND_RATE: 40,
    KIND_OVERLOADED: 30,
    KIND_TOOL: 10,
}

_AGENT_TO_LIMIT_CAT = {
    "gpt55": CAT_GPT_LIMIT,
    "gpt": CAT_GPT_LIMIT,
    "claude": CAT_CLAUDE_LIMIT,
    "deepseek": CAT_DEEPSEEK_LIMIT,
}


@dataclass
class LimitFinding:
    detected: bool = False
    kind: Optional[str] = None          # rate_limit / context_limit / ...
    category: Optional[str] = None      # gpt_limit / claude_limit / ...
    matches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "detected": self.detected,
            "kind": self.kind,
            "category": self.category,
            "matches": self.matches,
        }


def detect(text: str) -> list[tuple[str, str]]:
    """Return every (matched_substring, kind) pair found in text."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for pat, kind in _PATTERNS:
        for m in pat.finditer(text):
            out.append((m.group(0), kind))
    return out


def classify(text: str, agent: Optional[str] = None) -> LimitFinding:
    """Scan text, pick the dominant problem kind, map it to a category.

    `agent` is the agent the text came from (gpt55 / claude / deepseek);
    rate/overload kinds become that agent's *_limit category.
    """
    hits = detect(text)
    if not hits:
        return LimitFinding(detected=False)

    # dominant kind by precedence
    kind = max((k for _, k in hits), key=lambda k: _KIND_PRECEDENCE.get(k, 0))
    matches = sorted({h[0].lower() for h in hits if h[1] == kind})

    if kind == KIND_CONTEXT:
        category = CAT_CONTEXT
    elif kind == KIND_AUTH:
        category = CAT_AUTH
    elif kind == KIND_BILLING:
        category = CAT_BILLING
    elif kind == KIND_TOOL:
        category = CAT_TOOL
    else:  # rate / overloaded -> agent-specific limit
        category = _AGENT_TO_LIMIT_CAT.get((agent or "").lower(), CAT_DEEPSEEK_LIMIT)

    return LimitFinding(detected=True, kind=kind, category=category, matches=matches)


def status_flags(active_categories: set[str]) -> list[str]:
    """Translate the set of categories seen this run into status flags.

    gpt55 + claude both limited -> BRAIN_LIMIT_ACTIVE (plus the individual flags).
    """
    flags: list[str] = []
    if CAT_GPT_LIMIT in active_categories:
        flags.append(GPT_LIMIT_ACTIVE)
    if CAT_CLAUDE_LIMIT in active_categories:
        flags.append(CLAUDE_LIMIT_ACTIVE)
    if CAT_DEEPSEEK_LIMIT in active_categories:
        flags.append(DEEPSEEK_LIMIT_ACTIVE)
    if CAT_GPT_LIMIT in active_categories and CAT_CLAUDE_LIMIT in active_categories:
        flags.append(BRAIN_LIMIT_ACTIVE)
    return flags
