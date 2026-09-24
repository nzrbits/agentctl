# .agent/

Prompts and config that `agentctl` ships with.

```
.agent/
  prompts/
    deepseek-worker.md          # retrieval worker: read-only tools, returns receipts
    gpt55-reviewer.md           # optional second opinion from GPT-5.5
    gpt55-orchestrator.md       # fallback template for the GPT-5.5 reviewer
    claude-reviewer.md          # optional architecture and risk review
    gpt55-operator-onboarding.md  # paste-in briefing for driving agentctl from a ChatGPT chat
  opencode/
    deepseek-worker.md          # OpenCode agent definition for the worker
  config.example.json           # shipped defaults, copy to config.json to change them
  config.json                   # your local config (gitignored)
```

## Config resolution

`agentctl` merges the first file it finds over the built-in defaults:

1. `<cwd>/.agent/config.json` for a per-repo override
2. `<install>/.agent/config.json` for your global override
3. `<install>/.agent/config.example.json`, the shipped example

## DeepSeek via OpenCode

Default command:

```
opencode run --agent deepseek-worker --format json "<prompt>"
```

Install the agent definition once:

```
mkdir -p ~/.config/opencode/agent
cp .agent/opencode/deepseek-worker.md ~/.config/opencode/agent/
```

Without it, switch the deepseek `args` to the model form (already in `fallback_args` in the example):

```
opencode run --model deepseek/deepseek-chat --format json "<prompt>"
```

## GPT-5.5

The example config uses `"type": "http_openai"`: one HTTP request to the OpenAI API, key from `OPENAI_API_KEY`. Without
a key the review is skipped and the run flags `GPT_LIMIT_ACTIVE`. The run itself still completes.

No API key? Use `--route deepseek-chatgpt`. agentctl writes a prompt to paste into ChatGPT, and `agentctl ingest <run>`
adds the answer to the report.

## Claude

`claude -p "<prompt>" --output-format json`, using the Claude Code login. Used for `--route deepseek-claude`.
