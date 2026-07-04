# OHM Project Sub-Agents

This directory contains project-specific sub-agents for the OHM codebase. They are
defined as markdown files (per [opencode agents docs](https://opencode.ai/docs/agents/))
and routed onto **Synthetic** models so the OpenCode-Go budget is reserved for the
primary agent.

## Provider strategy

| Provider | Used by | Why |
|---|---|---|
| **Synthetic** (`synthetic/hf:*`) | All sub-agents | Quota-based, not per-token. Dispatching sub-agents is effectively free here, so the primary can delegate generously without burning the Go window. |
| **OpenCode Go** (`opencode-go/*`) | Primary agent only | Subscription with `$12/5hr`, `$30/week`, `$60/month` caps. Kept exclusively for the primary so session longevity is maximized. |

## Model routing

All sub-agents run on Synthetic. Models are matched to each agent's task and spread
across six distinct endpoints to avoid any single model's rate limits.

| Agent | Mode | Model | Purpose |
|---|---|---|---|
| `explore` (built-in override) | subagent | `synthetic/hf:Qwen/Qwen3.6-27B` | Fast read-only codebase search. Coder model = quick pattern/keyword hits. |
| `general` (built-in override) | subagent | `synthetic/hf:zai-org/GLM-5.2` | General multi-step research/execution. Capable model for code-capable background work. |
| `ohm-researcher` | subagent | `synthetic/hf:zai-org/GLM-5.2` | Deep codebase research and design exploration. Read-only; high-quality reasoning. |
| `ohm-adr-writer` | subagent | `synthetic/hf:zai-org/GLM-4.7-Flash` | Writes ADR documents at `docs/adr/`. Quality prose. |
| `ohm-test-writer` | subagent | `synthetic/hf:deepseek-ai/DeepSeek-V3.2` | Writes pytest test suites. Bulk, pattern-following code generation. |
| `ohm-plumber` | subagent | `synthetic/hf:MiniMaxAI/MiniMax-M3` | Wires features through queries → store → SDK → handler. Deep-context plumbing. |
| `ohm-schemer` | subagent | `synthetic/hf:moonshotai/Kimi-K2.6` | Schema migrations, validators, `VALID_*` frozensets. Code-focused. |

> **Routing audit (OHM-7jj2 followup):** Models are pinned to the Synthetic
> provider. `ohm-researcher`, `ohm-adr-writer`, `ohm-plumber`, `explore`, and
> `general` were repointed from stale model IDs (`GLM-5.1`, `Qwen2.5-Coder-32B`,
> `Qwen3-Coder-480B`) that no longer exist on Synthetic to confirmed-available
> models. `ohm-test-writer` (`DeepSeek-V3.2`) and `ohm-schemer` (`Kimi-K2.6`)
> are unverified — dispatch once and confirm, or repoint to a confirmed model
> (`GLM-5.2`, `GLM-4.7-Flash`, `MiniMax-M3`, `Qwen3.6-27B`, `Kimi-K2.7-Code`).
> **Restart opencode after editing routing** — config loads once at startup.

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
is the only thing to throttle; sub-agents keep running on Synthetic.

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
