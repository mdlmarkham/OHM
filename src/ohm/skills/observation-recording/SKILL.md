# Skill: Observation Recording

## When to use
Record observations on nodes or edges to capture measurements, outcomes,
or assessments. Observations feed the verification and confidence decay
systems.

## Required fields
- `source_url`: Provenance URL for the observation (ADR-013).
- `sigma`: Uncertainty/standard deviation of the measurement.

## Observation types
- `measurement`: A quantitative reading.
- `experiment_result`: Outcome of a simulation or experiment.
- `assessment`: A qualitative evaluation.
- `forecast`: A forward-looking prediction (OHM-841).
- `pattern`: A detected pattern in data.

## Verification
Unverified edges decay with a 30-day half-life. Verified edges (with
recorded outcomes) decay with a 365-day half-life. Record outcomes via
`record_outcome(source_agent, claim_node, outcome)`.
