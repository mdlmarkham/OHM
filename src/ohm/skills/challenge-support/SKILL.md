# Skill: Challenge and Support

## When to challenge
Challenge an edge when you have evidence that contradicts it. Use
`CHALLENGED_BY` edges with a `reason` and `confidence` reflecting your
certainty.

## When to support
Use `SUPPORTS` edges to add corroborating evidence. High-confidence
support edges increase the target edge's compound confidence.

## NEGATES vs CHALLENGED_BY (ADR-009)
- `CHALLENGED_BY`: Expresses doubt — the challenged edge may still be
  partially correct.
- `NEGATES`: Expresses contradiction — the negated edge is wrong.

## Oppositional review
The system automatically flags CAUSES edges with homogeneous source_tier
or agent support for oppositional review. Check `oppositional_review` in
synthesis responses.
