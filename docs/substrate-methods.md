# OHM Substrate Methods — Validated Computation in the Cognition Substrate

## Design Principle

OHM is a cognition substrate, not just a knowledge graph. The substrate stores agent reasoning and makes it visible. But it can also *compute* — using validated methods that produce the same result regardless of which agent calls them.

**Test:** If a method produces the same output regardless of which agent calls it, it belongs in the substrate. If it requires domain judgment, it stays with the agent.

## Substrate-Owned Methods

These are validated once, available to all agents. They take graph state as input and produce deterministic or well-characterized probabilistic output.

### 1. Confidence Aggregation

**Problem:** Three agents observe the same node with different values and confidences. How do you combine them?

**Method:** Multiple aggregation strategies:
- `mean` — simple average of values, weighted by confidence
- `bayesian` — inverse-variance weighting (σ² = 1/confidence)
- `max` — take the highest-confidence observation
- `consensus` — weighted average, but only if agreement exceeds threshold

**API:** `ohm graph aggregate <node_id> --method bayesian`
**Returns:** Combined value, combined confidence, number of observations, method used

**Why substrate:** Every agent needs this. If each implements it differently, the same graph produces different "truth" depending on who reads it.

### 2. Monte Carlo Impact Analysis

**Problem:** A node's impact depends on confidence propagation through the graph. What's the probability distribution of downstream effects?

**Method:** Monte Carlo simulation across the dependency chain:
1. Sample each edge's confidence from a distribution (beta, based on confidence value)
2. Propagate through recursive CTE traversal
3. Record impact magnitude for each iteration
4. Return distribution: mean, p5, p50, p95, max

**API:** `ohm graph impact <node_id> --method monte-carlo --iterations 10000`
**Returns:** Impact distribution, confidence bounds, key paths, simulation metadata

**Why substrate:** Given the same graph and same parameters, any agent should get the same probability distribution. This is math, not judgment.

### 3. Anomaly Detection

**Problem:** Which observations are surprising? Which nodes have observations far from baseline?

**Method:** Sigma-based flagging:
- For each observation, compute (value - baseline) / sigma
- Flag observations where sigma > threshold
- Flag nodes with high variance across observations
- Flag edges where confidence is unusually low for their layer

**API:** `ohm graph anomalies --sigma 2.0 --layer L3`
**Returns:** List of anomalous observations, ranked by surprise

**Why substrate:** Statistical anomaly detection is deterministic given the same data and threshold.

### 4. Change Feed with Relevance Scoring

**Problem:** An agent wants to know what changed, but only what's relevant to their domain.

**Method:** 
- Agent registers values and goals (OHM-a35.10)
- Change feed filters by: layers the agent writes to, nodes they've observed, edge types they care about
- Relevance score: how connected is the change to the agent's existing graph neighborhood?

**API:** `ohm graph listen --agent metis --min-relevance 0.5`
**Returns:** Changes ranked by relevance, with relevance scores

**Why substrate:** Filtering and scoring is mechanical once agent values are registered.

### 5. Graph Health Metrics

**Problem:** Is the graph healthy? Are there orphans, unchallenged low-confidence edges, or dense clusters worth synthesizing?

**Method:**
- Orphans: nodes with < 2 edges
- Unchallenged low-confidence: L3/L4 edges with confidence < 0.5 and no CHALLENGED_BY
- Dense clusters: nodes with > 5 edges (potential synthesis candidates)
- Stale observations: not updated in > 30 days (confidence decay)

**API:** `ohm graph health`
**Returns:** Orphan count, low-confidence count, cluster count, staleness metrics

**Why substrate:** Health checks are objective measurements of graph structure.

## Agent-Owned Computation

These require domain judgment and stay with agents:

- **Research methodology** — Clio decides how to investigate
- **Challenge strategy** — Socrates decides what to challenge and why
- **Synthesis judgment** — Métis decides what patterns mean
- **Trading signals** — Signal agent decides what to buy/sell
- **Priority ranking** — Each agent values different things differently

The substrate provides data (observations, confidence, impact). Agents provide judgment.

## Implementation Path

These methods should be implemented as the SDK matures:

1. **P1:** Confidence aggregation (needed for team testing)
2. **P2:** Anomaly detection and graph health (needed for monitoring agents)
3. **P2:** Monte Carlo impact (needed for trading/TOPO scenarios)
4. **P2:** Relevance-scoring change feed (needed for agent integration)

Each method gets its own module in `ohm/methods/` with:
- Pure function that takes graph state and returns results
- SDK wrapper for agent access
- CLI command for human diagnostics
- Test suite with known inputs and expected outputs