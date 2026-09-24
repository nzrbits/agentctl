# Reliability notes

Upstream behaviour that agentctl works around. Each item says what happens and what the orchestrator does about it.

| Behaviour | Effect | Handling |
|---|---|---|
| `opencode` inherits an open stdin | the process blocks on a permission read and streams nothing | agents are spawned with `stdin=DEVNULL` |
| DeepSeek ends its turn after the tool calls, with no final text | no summary to parse | tool receipts are harvested from the event stream and become the deliverable |
| `opencode run --agent <subagent>` emits final text unreliably | empty answers from some profiles | DeepSeek only needs receipts; GPT-5.5 runs with `--model` |
| OpenCode's OAuth route to GPT-5.5 returns exit 0 with an empty body | looks like success | an empty review is marked `unavailable` and never counts as an accept |
| DeepSeek idle-stalls mid-run | the run hangs | idle timeout, then one bounded retry if the run budget allows it |
| Agents talk about "rate limit" or "401" in a normal answer | false limit detection | stdout is only scanned for limits when the call did not succeed cleanly; stderr is always scanned |

For a deterministic GPT-5.5 review, set the agent type to `http_openai` and provide `OPENAI_API_KEY`. That route is a
single HTTP request, so it cannot stall or return a half-finished stream.
