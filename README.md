# agentctl

A local, real CLI that orchestrates three coding agents on your Mac:

- **GPT-5.5** — lead orchestrator: plan, route, review, final decision (brain, expensive)
- **Claude** — senior builder: complex implementation (expensive)
- **DeepSeek** — cheap worker via [OpenCode]: scouting, logs, tests, boilerplate, context packs

The whole point: **burn cheap DeepSeek tokens first**, reserve the expensive
brains for architecture, hard implementation and review. If a brain hits a
rate/usage/quota limit, agentctl detects it and degrades safely instead of
crashing.

This is not a markdown concept and not a blind prompt pipeline. It runs real
subprocesses (`opencode`, `claude`), isolates agent edits in **git worktrees**,
parses each agent's JSON report, scans for **dangerous commands**, and writes a
full audit trail per run.

## Requirements

- macOS, `python3` (3.9+), `git`
- [OpenCode](https://opencode.ai) (`opencode`) for the DeepSeek worker
- [Claude Code](https://claude.com/claude-code) (`claude`) for the builder
- A GPT-5.5 CLI/API of your choice for the orchestrator (optional; see below)

## Install

```bash
# the tool lives in ~/agentctl; put it on your PATH via a symlink
ln -s ~/agentctl/agentctl ~/.local/bin/agentctl   # or anywhere on PATH
agentctl doctor
```

You run `agentctl` from inside **any target repo** — that repo becomes the
working context, and run artifacts land in that repo's `.agent-runs/`.

## Commands

| command | what it does |
|---|---|
| `agentctl doctor` | check macOS, git, toolchains, opencode/claude, config, env vars (masked) |
| `agentctl run "<task>"` | snapshot repo → plan → DeepSeek first → escalate to Claude if needed → review |
| `agentctl run "<task>" --dry-run` | do everything except executing the agents (safe, no tokens spent) |
| `agentctl run "<task>" --allow-edit` | let agents edit files, in isolated worktrees |
| `agentctl status` | list recent runs and their flags |
| `agentctl diff` | show diffs from agent worktrees |
| `agentctl review [--run DIR]` | print a run's review report (latest by default) |
| `agentctl cleanup [--yes] [--runs]` | remove worktrees (and optionally run dirs) |
| `agentctl selftest` | run the bundled unit tests |

## How a run works

1. **Snapshot** the repo (git state, structure, package manager, test command) — read-only.
2. **Plan**: GPT-5.5 if wired, else a local heuristic orchestrator (flagged `GPT_LIMIT_ACTIVE`).
3. **DeepSeek first**: builds a context pack and solves low-risk work cheaply.
4. **Escalate to Claude** only if the task is complex/security-sensitive or DeepSeek returns `needs_claude`.
5. **Review**: synthesize results, scan for dangerous commands, accept/hold/retry.
6. **Persist** everything under `.agent-runs/<timestamp>/`:
   `task.txt`, `snapshot.json`, `plan.json`, `prompt-*.md`,
   `agent-*/` (stdout, stderr, parsed `report.json`), `review-report.md`, `log.json`.

Agents that edit run inside a dedicated `git worktree` on a throwaway
`agentctl/...` branch, so your real working tree is never touched. Inspect with
`agentctl diff`, then `agentctl cleanup --yes`.

## Limit / failure handling

agentctl scans every agent's stdout+stderr for limit/quota/auth/billing/context
signals (`rate limit`, `429`, `insufficient_quota`, `context_length_exceeded`,
`unauthorized`, `overloaded`, …) and reacts per the contract:

| situation | behavior | flag |
|---|---|---|
| GPT-5.5 limited | Claude/heuristic plans + reviews temporarily | `GPT_LIMIT_ACTIVE` |
| Claude limited | GPT-5.5 plans; DeepSeek does only safe work | `CLAUDE_LIMIT_ACTIVE` |
| DeepSeek limited | brains used only for important work | `DEEPSEEK_LIMIT_ACTIVE` |
| GPT-5.5 + Claude limited | no complex/risky changes; diagnosis only | `BRAIN_LIMIT_ACTIVE` |
| context too long | compress / shrink context pack, retry | `context_limit` |
| auth / billing | **block immediately** with concrete diagnosis | `auth_error` / `billing_error` |

## Configuration

See [`.agent/README.md`](.agent/README.md). Copy `.agent/config.example.json` to
`.agent/config.json` and edit. Every agent's command line is config-driven, not
hardcoded.

## Secrets

- No secrets are hardcoded. Copy `.env.example` → `.env` (gitignored) for keys.
- `doctor` only ever prints **masked** env values (`SET(len=…)` / `(unset)`).
- Command lines elide long prompts in logs; secrets are never put on argv here.

## Safety

agentctl is read-only by default. `--allow-edit` is required for changes, and
even then DeepSeek edits are off unless `safety.deepseek_may_edit` is enabled.
Dangerous commands (`rm -rf`, `git reset --hard`, `git clean -fd`, force push,
`DROP TABLE`, `terraform apply`, `kubectl delete`, …) are flagged in every review.
