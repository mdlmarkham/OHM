# OHM Project Sub-Agents

This directory contains project-specific sub-agents for the OHM codebase. They are
defined as markdown files (per [opencode agents docs](https://opencode.ai/docs/agents/))
and routed onto **Synthetic** models (HuggingFace-hosted via the Synthetic provider) so
the OpenCode-Go budget is reserved for the primary agent.

## Provider strategy

| Provider | Used by | Why |
|---|---|---|
| **Synthetic** (`synthetic/hf:*`) | All sub-agents | HuggingFace-hosted inference via the Synthetic provider. Sub-agent dispatch is cheap, so the primary can delegate generously without burning the Go window. |
| **OpenCode Go** (`opencode-go/*`) | Primary agent only | Subscription with `$12/5hr`, `$30/week`, `$60/month` caps. Kept exclusively for the primary so session longevity is maximized. |

## Model routing

All sub-agents run on Synthetic (HuggingFace-hosted). Models are matched to each agent's task.

| Agent | Mode | Model | Purpose |
|---|---|---|---|
| `ohm-researcher` | subagent | `synthetic/hf:zai-org/GLM-5.2` | Deep codebase research and design exploration. Read-only; high-quality reasoning with 1M context. |
| `ohm-adr-writer` | subagent | `synthetic/hf:zai-org/GLM-5.2` | Writes ADR documents at `docs/adr/`. Quality prose, strong on tool-use. |
| `ohm-test-writer` | subagent | `synthetic/hf:MiniMaxAI/MiniMax-M3` | Writes pytest test suites. Bulk, pattern-following code generation with 1M context. |
| `ohm-plumber` | subagent | `synthetic/hf:MiniMaxAI/MiniMax-M3` | Wires features through queries → store → SDK → handler. Deep-context plumbing. |
| `ohm-schemer` | subagent | `synthetic/hf:zai-org/GLM-5.2` | Schema migrations, validators, `VALID_*` frozensets. Code-focused reasoning. |

> **Note:** `explore` and `general` built-in sub-agents are overridden in
> `.opencode/opencode.json` to use `synthetic/hf:Qwen/Qwen3.6-27B` and
> `synthetic/hf:zai-org/GLM-5.2` respectively.

## Built-in agents NOT overridden

- `build` (primary) — uses the global `model` setting (OpenCode Go).
- `plan` (primary) — uses the global `model` setting. Plan mode is permission-restricted, not model-restricted.
- `scout` (subagent) — uses the primary's model. Used rarely enough that the default is fine.

## Usage

The primary agent (Build) invokes these via the Task tool. The model routing is
automatic — the primary agent just specifies `subagent_type: "ohm-researcher"` and the
configured model is used.

Users can also invoke any subagent directly via `@ohm-researcher`, `@ohm-plumber`, etc.

## Token budget tracking

Go usage can be tracked at <https://opencode.ai/auth>. Because **only the primary**
draws from Go, the `$12/5hr` window depletes at the primary's rate alone — sub-agent
dispatch adds no Go cost. If a long session still exhausts the Go window, the primary
is the only thing to throttle; sub-agents keep running on Ollama Cloud.

Ollama Cloud usage is metered by GPU time, not tokens. Each cloud model has a usage
level (1-4) shown on its library page. Pro plan gives 50× more cloud usage than Free;
Max plan adds another 5×. Check the [Ollama Cloud pricing page](https://ollama.com/pricing)
for the per-model usage levels and concurrency limits.

## Setup

Sub-agents use the Synthetic provider (HuggingFace-hosted models). No additional
setup is needed beyond having opencode configured with the Synthetic provider.
The `synthetic/hf:*` model IDs are resolved automatically.

## Sub-agent output contract

Every project sub-agent has a **mandatory verification protocol** that requires it to
paste actual command output before reporting success. The primary agent treats any
dispatch missing these elements as failed and re-does the work inline. The contract:

| Agent | Must paste |
|---|---|
| `ohm-plumber` | `ls` of new files, `git diff --stat`, pytest tail of new test file, no-regression pytest summary, function `file:line` locations |
| `ohm-test-writer` | `ls` of new test file, `git diff --stat`, pytest tail of new file, no-regression pytest summary, per-class test counts |
| `ohm-researcher` | `rg` / `cat` / `ls` command output for every claim, file:line locations, "what does NOT exist" section |
| `ohm-schemer` | `rg` for SCHEMA_VERSION + MIGRATIONS, `python -c` schema init check, `python -c` validator check, schema pytest tail |
| `ohm-adr-writer` | `ls` of new ADR, `rg` for index entry, `head -10` of new ADR |

The contract exists because sub-agents historically claimed "all tests pass" without
running them or writing the test file. The verification protocol makes those claims
verifiable.

## Changing the routing

To switch an agent's model, either:
1. Edit the markdown file's frontmatter `model:` field (for project agents), or
2. Override inline in `.opencode/opencode.json` under `agent.<name>.model` (works for
   built-ins like `explore`/`general` too).

After any change, **quit and restart opencode** — config is loaded once at startup.
