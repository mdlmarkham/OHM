# OHM Project Sub-Agents

This directory contains project-specific sub-agents for the OHM codebase. They are
defined as markdown files (per [opencode agents docs](https://opencode.ai/docs/agents/))
and routed onto **Ollama Cloud** models so the OpenCode-Go budget is reserved for the
primary agent.

## Provider strategy

| Provider | Used by | Why |
|---|---|---|
| **Ollama Cloud** (`ollama-cloud/*:cloud`) | All sub-agents | Hosted inference, no local GPU required. Pro plan gives 3 concurrent cloud models with 50× more usage than Free. Sub-agent dispatch is cheap, so the primary can delegate generously without burning the Go window. |
| **OpenCode Go** (`opencode-go/*`) | Primary agent only | Subscription with `$12/5hr`, `$30/week`, `$60/month` caps. Kept exclusively for the primary so session longevity is maximized. |

## Model routing

All sub-agents run on Ollama Cloud. Models are matched to each agent's task.

| Agent | Mode | Model | Purpose |
|---|---|---|---|
| `ohm-researcher` | subagent | `ollama-cloud/glm-5.2:cloud` | Deep codebase research and design exploration. Read-only; high-quality reasoning with 1M context. |
| `ohm-adr-writer` | subagent | `ollama-cloud/glm-4.7:cloud` | Writes ADR documents at `docs/adr/`. Quality prose, strong on tool-use. |
| `ohm-test-writer` | subagent | `ollama-cloud/minimax-m3:cloud` | Writes pytest test suites. Bulk, pattern-following code generation with 1M context. |
| `ohm-plumber` | subagent | `ollama-cloud/minimax-m3:cloud` | Wires features through queries → store → SDK → handler. Deep-context plumbing with multimodal. |
| `ohm-schemer` | subagent | `ollama-cloud/kimi-k2.7-code:cloud` | Schema migrations, validators, `VALID_*` frozensets. Code-focused, ~30% fewer thinking tokens than K2.6. |

> **Note:** `explore` and `general` built-in sub-agents inherit the primary agent's
> model (OpenCode Go) by default, unless explicitly overridden in
> `.opencode/opencode.json` under `agent.<name>.model`. No override is currently set
> for them in this project.

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

Before using Ollama Cloud in OpenCode:

1. Sign in at <https://ollama.com/> and create an API key under **Settings** > **Keys**.
2. Run `/connect` in opencode TUI, search for **Ollama Cloud**, paste the API key.
3. The first time you select a cloud model, opencode may need to pull the model
   metadata locally — this is automatic on first use.

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
