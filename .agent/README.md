# .agent/ — agentctl assets

This directory ships the prompts and config that `agentctl` uses.

```
.agent/
  prompts/
    gpt55-orchestrator.md   # lead orchestrator: plan, route, final review, decide
    claude-reviewer.md      # architecture reviewer: infra risk and design validation
    deepseek-worker.md      # execution worker: token-efficient scout / executor
  opencode/
    deepseek-worker.md      # OpenCode execution worker definition
  config.example.json       # copy to config.json to customize
  config.json               # your local config (gitignored)
```

## Config resolution order

`agentctl` loads the **first** file it finds, merged over built-in defaults:

1. `<cwd>/.agent/config.json`  — per-repo override
2. `<install>/.agent/config.json` — your global override
3. `<install>/.agent/config.example.json` — shipped example

## Wiring DeepSeek (OpenCode)

The default deepseek command is:

```
opencode run --agent deepseek-worker --format json "<prompt>"
```

To use `--agent deepseek-worker`, install the agent definition:

```
mkdir -p ~/.config/opencode/agent
cp .agent/opencode/deepseek-worker.md ~/.config/opencode/agent/
```

If you don't install the agent, switch the deepseek args to the model form
(already present as `fallback_args` in `config.example.json`):

```
opencode run --model deepseek/deepseek-chat --format json "<prompt>"
```

## Wiring GPT-5.5

`config.example.json` ships `gpt55.command = "CONFIGURE_ME"`. Until you set a
real command, the orchestrator falls back to a local Claude/heuristic planner
and every run is flagged `GPT_LIMIT_ACTIVE`. Point `command`/`args` at your real
GPT-5.5 CLI or a thin API wrapper to enable the Lead Orchestrator.
