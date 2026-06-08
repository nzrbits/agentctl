# agentctl

A local multi-agent orchestrator CLI for macOS. It splits work along one rule:

> **Push the token-heavy retrieval onto DeepSeek. Keep the logic on a reliable reasoner.**

- **DeepSeek = workhorse (retrieval).** Runs read-only tools (read/grep/glob) via OpenCode and
  returns raw **tool receipts** — file/log/command evidence. Its prose conclusions are **not trusted**.
- **Local / Claude = logic.** The stable reasoner. Synthesizes the answer and the decision from
  DeepSeek's receipts. This is the default and it never depends on a flaky channel.
- **GPT-5.5 = optional best-effort review.** A second opinion *when it has a working route*. The free
  OpenCode/OAuth route to GPT-5.5 is unreliable (can return empty), and a deterministic route needs
  OpenAI API billing — so GPT-5.5 is **never in the critical path**: if it returns nothing the run
  still completes on local synthesis.

It runs real subprocesses (`opencode`, `claude`), isolates edits in **git worktrees**, parses each
agent's output, scans for **dangerous commands**, and writes a full audit trail per run.

## Why this split

DeepSeek tokens are cheap, so it does the bulk file/log reading. The reasoner only sees the distilled
receipts, so it spends few tokens on logic. Crucially, model-behaviour quirks (DeepSeek ending a turn
without a final message, GPT-5.5's free route returning empty) are handled by the **orchestrator**, not
by trusting the model: receipts are harvested deterministically, empty reviews are marked `unavailable`,
and idle stalls get one bounded retry. The orchestrator is deterministic; the agents are probabilistic
inside it.

## Requirements

- macOS, `python3` (3.9+), `git`
- [OpenCode](https://opencode.ai) (`opencode`) — runs the DeepSeek worker (and GPT-5.5 when used).
  Needs OpenCode auth configured (DeepSeek API key; OpenAI via OAuth or API key).
- [Claude Code](https://claude.com/claude-code) (`claude`) — used when Claude runs as a review agent.

## Install

```bash
ln -s ~/dev/agentctl/agentctl ~/.local/bin/agentctl   # put on PATH (adjust to your location)
agentctl doctor
```

Run `agentctl` from inside any target repo — that repo is the working context, and run artifacts land
in that repo's `.agent-runs/`.

## Commands

| command | what it does |
|---|---|
| `agentctl` | bare invocation starts the interactive console (`shell`) |
| `agentctl doctor` | check macOS, git, toolchains, opencode/claude, config, env (masked) |
| `agentctl run "<task>"` | snapshot → DeepSeek retrieval → local synthesis (+ optional review) |
| `agentctl run "<task>" --dry-run` | plan + write prompts/run-dir, do NOT execute agents |
| `agentctl run "<task>" --allow-edit` | let agents edit files, in isolated worktrees |
| `agentctl run "<task>" --caller local` | Claude/local owns the logic (default, stable) |
| `agentctl run "<task>" --route deepseek-gpt55` | also attempt a best-effort GPT-5.5 review |
| `agentctl status` | list recent runs |
| `agentctl review [--run DIR]` | print a run's review report (latest by default) |
| `agentctl diff` | show diffs from agent worktrees (or the working tree) |
| `agentctl cleanup [--yes] [--runs]` | remove worktrees (and optionally run dirs) |
| `agentctl selftest` | run the bundled unit tests |

### Callers and routes

- `--caller local` / `--caller claude` — Claude/local reasons from receipts (default, no flaky channel).
- `--caller gpt55` — GPT-5.5 leads (only when it has a working route).
- `--route deepseek` — DeepSeek retrieval + local synthesis only (default).
- `--route deepseek-gpt55` — also run a best-effort GPT-5.5 review (non-blocking).
- `--route deepseek-claude` — also run a `claude` CLI review.

The default route comes from `.agent/config.json` (`orchestrator.default_route`, normally `deepseek`).

## How a run works

1. **Snapshot** the repo (git state, structure, package manager, test command) — read-only.
2. **Plan / route**: pure-local routing. DeepSeek is always first; the reviewer is optional.
3. **DeepSeek retrieval**: gathers evidence with read-only tools. If it idle-stalls, one bounded retry.
4. **Harvest receipts**: the orchestrator extracts DeepSeek's tool calls (input + output) as the
   deliverable, regardless of whether DeepSeek emitted a final summary. Its prose is kept only as an
   untrusted note.
5. **Local synthesis** produces the stable decision from the receipts.
6. **Optional review** (only on `deepseek-gpt55` / `deepseek-claude`): a second opinion. An empty
   GPT-5.5 result is marked `unavailable` and never blocks or false-accepts.
7. **Persist** under `.agent-runs/<run-id>/`: `task.txt`, `snapshot.json`, `plan.json`, `prompt-*.md`,
   `agent-*/` (stdout, stderr, parsed `report.json`), `review-report.md`, `log.json`.

## Reliability notes (known upstream behaviour)

- **opencode subprocess + stdin**: opencode blocked when it inherited an open stdin; agentctl spawns it
  with `stdin=DEVNULL`. Required for complex tasks to stream at all.
- **DeepSeek often ends after tool calls with no final text** → handled by harvesting tool receipts.
- **opencode `run --agent <subagent>` emits final text unreliably** → DeepSeek is fine (we only need
  receipts); GPT-5.5 runs on `--model`, and an empty result is treated as `unavailable`.
- **Free GPT-5.5 (OpenCode OAuth) can return empty (exit 0, no output)** → best-effort only; a
  deterministic GPT-5.5 needs OpenAI API billing.

## Configuration

See [`.agent/README.md`](.agent/README.md). Copy `.agent/config.example.json` to `.agent/config.json`
and edit. Every agent's command line is config-driven. `.agent/config.json` is gitignored.

## Secrets

- No secrets are hardcoded. Copy `.env.example` → `.env` (gitignored) for keys.
- `doctor` only prints **masked** env values.

## Safety

Read-only by default. `--allow-edit` is required for changes, and DeepSeek edits stay off unless
`safety.deepseek_may_edit` is enabled. Dangerous commands (`rm -rf`, `git reset --hard`, force push,
`terraform apply`, `kubectl delete`, …) are flagged in every review. DeepSeek receipts are treated as
untrusted **data**, never as instructions.
