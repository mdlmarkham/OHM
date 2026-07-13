# Decision Nodes in OHM

Decision nodes represent choices — what to do, which strategy to pursue, which
alternative to commit to. They are the entry point for strategic reasoning:
value-of-information, game theory, policy analysis, and PERT-based scheduling.

## Creating a Decision Node

A `decision`-type node requires three fields to be fully functional:

| Field | Type | Purpose |
|-------|------|---------|
| `utility_scale` | `FLOAT` | How good the current best outcome is: `1.0` (best), `0.5` (neutral), `0.0` (worst). Maps to `"best"/"neutral"/"worst"` in the recommendation engine for the three canonical values; non-canonical values pass through as floats. |
| `action_alternatives` | `TEXT` (JSON) | Array of alternative actions: `["do_nothing", "upgrade", "replace"]` |
| `current_best_action` | `VARCHAR` | Which alternative is currently selected: e.g. `"upgrade"` |

### float-precision caveat

`utility_scale` is a DuckDB `FLOAT` (32-bit). The recommendation engine
(`evaluate_decision()`) maps the exact values `1.0`, `0.5`, and `0.0` to
the strings `"best"`, `"neutral"`, and `"worst"`. Any other value passes
through raw as a `float32` — for example `0.8` becomes `0.800000011920929`.
If your SDK/MCP consumer needs clean string enums, use only the three
canonical values.

## Linking Hypotheses

Decision nodes derive their confidence from linked hypothesis nodes via the
`DECISION_DEPENDS_ON` edge type (L3):

```
DECISION_DEPENDS_ON  (L3)
  from: decision-node
  to:   hypothesis-node
```

`evaluate_decision()` reads `hypothesis_status` on each linked hypothesis
(`verified`, `pruned`, `tested`, `other`) and scores the decision accordingly.
More verified hypotheses → higher confidence in the current best action.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/decision/{id}/recommendation` | Decision recommendation: best action, alternatives, confidence, key assumptions |
| GET | `/voi?decision={id}` | Value-of-information: which observations would most improve the decision |
| GET | `/game` | Game theory analysis (requires utility_scale) |
| GET | `/policy` | Policy endpoint (requires utility_scale) |

## SDK

```python
import ohm.framework.sdk as ohm

with ohm.connect_http("http://127.0.0.1:8710", actor="metis", token="...") as g:
    rec = g.decision_recommend("decision-abc123")
    print(rec["current_best_action"])
    print(rec["confidence"])
    print(rec["action_alternatives"])
```

## MCP Tool

```json
{
  "tool": "ohm_decision_recommend",
  "arguments": {"node_id": "decision-abc123"}
}
```

Returns the same shape as `GET /decision/{id}/recommendation`.

## Cognitive Nudges

When an agent creates a bare `decision`-type node missing `utility_scale`,
`action_alternatives`, or `DECISION_DEPENDS_ON` edges, OHM returns a
`decision_node_incomplete` nudge suggesting the missing fields. This ensures
decision nodes are fully specified for downstream analysis even when the agent
isn't consciously trying to build a decision framework.

## Response Shape

```json
{
  "decision_id": "decision-abc123",
  "label": "Should we upgrade the turbine?",
  "current_best_action": "upgrade",
  "action_alternatives": ["do_nothing", "upgrade", "replace"],
  "confidence": 0.75,
  "key_assumptions": ["Turbine failure probability > 0.3", "Budget available"],
  "utility_scale": 1.0
}
```

`confidence` ranges from 0.0 to 1.0 and is derived from the proportion of
linked hypotheses that are `verified` or `tested`.
