# OHM Project Sub-Agents

This directory contains project-specific sub-agents for the OHM codebase. They are
defined as markdown files (per [opencode agents docs](https://opencode.ai/docs/agents/))
and routed across two providers to spread token usage and avoid exhausting either.

## Provider strategy

| Provider | Model(s) used | Why |
|---|---|---|
| **Synthetic** (`synthetic/hf:MiniMaxAI/MiniMax-M3`) | `ohm-plumber` | Heavy project context, low request count. Same model as the primary agent so it can re-use the in-session context efficiently. |
| **OpenCode Go** (`opencode-go/*`) | `mimo-v2.5`, `kimi-k2.7`, `glm-5.2`, `deepseek-v4-flash` | Subscription plan with $12/5hr, $30/week, $60/month caps. Spreading across models with different price points maximizes the request budget. |

## Model selection rationale

| Model | Monthly requests | Price profile | Best fit |
|---|---|---|---|
| `opencode-go/glm-5.2` | 4,300 | Premium ($1.40/$4.40 per 1M) | High-quality reasoning where request count is low (research, ADR writing) |
| `opencode-go/kimi-k2.7` | 9,250 | Code-focused ($0.95/$4.00) | Code-heavy multi-step work (general subagent, schemer) |
| `opencode-go/mimo-v2.5` | 150,400 | Cheap ($0.14/$0.28) | Bulk read-only codebase search where volume matters more than nuance |
| `opencode-go/deepseek-v4-flash` | 158,150 | Cheap ($0.14/$0.28) | Bulk test writing — repetitive, pattern-following work |
| `synthetic/hf:MiniMaxAI/MiniMax-M3` | (Synthetic quota) | (Synthetic quota) | Deep-context plumbing where the primary agent's session context helps |

## Agent inventory

| Agent | Mode | Model | Purpose |
|---|---|---|---|
| `explore` (built-in override) | subagent | `opencode-go/mimo-v2.5` | Fast read-only codebase search. Cheap, high-volume — moves off the primary's Synthetic quota. |
| `general` (built-in override) | subagent | `opencode-go/kimi-k2.7` | General multi-step research/execution. Code-capable, mid-tier volume. |
| `ohm-researcher` | subagent | `opencode-go/glm-5.2` | Deep codebase research and design exploration. Read-only. Premium model for high-quality findings. |
| `ohm-adr-writer` | subagent | `opencode-go/glm-5.2` | Writes ADR documents at `docs/adr/`. Premium model for quality prose. |
| `ohm-test-writer` | subagent | `opencode-go/deepseek-v4-flash` | Writes pytest test suites. Cheap model for high-volume pattern-following work. |
| `ohm-plumber` | subagent | `synthetic/hf:MiniMaxAI/MiniMax-M3` | Wires features through queries → store → SDK → handler. Uses Synthetic to leverage primary's context. |
| `ohm-schemer` | subagent | `opencode-go/kimi-k2.7` | Schema migrations, validators, `VALID_*` frozensets. Code-focused mid-tier. |

## Built-in agents NOT overridden

- `build` (primary) — uses the global `model` setting (currently Synthetic MiniMax M3).
- `plan` (primary) — uses the global `model` setting. Plan mode is permission-restricted, not model-restricted.
- `scout` (subagent) — uses the primary's model. Used rarely enough that the default is fine.

## Usage

The primary agent (me, Build) invokes these via the Task tool. The model routing is
automatic — the primary agent just specifies `subagent_type: "ohm-researcher"` and the
configured model is used.

Users can also invoke any subagent directly via `@ohm-researcher`, `@ohm-plumber`, etc.

## Token budget tracking

GO usage can be tracked at <https://opencode.ai/auth>. The `$12/5hr` window is the
tightest constraint — if a long session exhausts it, the cheap models
(`mimo-v2.5`, `deepseek-v4-flash`) are the highest-volume paths and will deplete slowest.

If one GO model hits its limit, the others are independent quotas — switch the agent
config to a different GO model until the window resets.

## Changing the routing

To switch an agent's model, either:
1. Edit the markdown file's frontmatter `model:` field, or
2. Override inline in `.opencode/opencode.json` under `agent.<name>.model`.

After any change, **quit and restart opencode** — config is loaded once at startup.
