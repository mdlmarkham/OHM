# Skill: Decision Node

## When to use
Create a `decision` node when you need to choose between actions and the
choice depends on uncertain hypotheses.

## Required fields
- `utility_scale`: One of `best` (1.0), `neutral` (0.5), `worst` (0.0),
  or a numeric value 0-1.
- `action_alternatives`: JSON array of action names (e.g. `["build", "wait"]`).
- `current_best_action`: The currently recommended action.

## Linking hypotheses
Use `DECISION_DEPENDS_ON` edges (L3) to link the decision to hypothesis
nodes. The recommendation engine reads these edges to compute confidence
and suggest the best action.

## Autoresearch
Run `POST /decision/{id}/autoresearch` to automatically discover and
evaluate candidate hypothesis edges.

## Verification
Record outcomes on hypotheses via `record_outcome()`. The recommendation
engine weights verified hypotheses higher than untested ones.
