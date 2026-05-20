# Markov Chains Feasibility Analysis

**Date**: 2026-05-20  
**Issue**: OHM-1jh  
**Status**: Research Spike Complete

## 1. Use Case Analysis

### 1.1 Applicable OHM Scenarios

| Domain | State Transitions | Markov Fit | Notes |
|--------|------------------|------------|-------|
| **Supply Chain** | operational → degraded → disrupted → recovered | High | Absorbing states at disruption, recovery cycles |
| **Medical/Disease** | healthy → symptomatic → critical → deceased | High | Natural absorbing states, progressive progression |
| **Cybersecurity** | recon → exploit → persist → exfiltrate | High | Attack chain progression, absorbing at exfil |
| **Cattle Health** | healthy → stressed → ill → deceased | High | Herd health state modeling |
| **Project Risk** | planning → execution → delayed → failed | Medium | Depends on whether delays are recoverable |
| **Ecosystem** | stable → stressed → collapsed | Medium | May need continuous-time variant |

### 1.2 Key Insight

OHM already has:
- **Bayesian Networks**: P(effect|cause) — conditional, static reasoning
- **Monte Carlo**: Stochastic one-shot propagation with variance
- **Temporal Decay**: Time-based confidence degradation

Markov Chains fill a specific gap: **sequential multi-step state evolution with absorption analysis**

### 1.3 Why Not Bayesian or Monte Carlo?

- **Bayesian**: Conditions on evidence but doesn't model sequential transitions
- **Monte Carlo**: One-shot propagation, no tracking of absorption over infinite steps
- **Markov**: Computes P(eventually reach absorbing state | start) for unbounded time steps

## 2. Technical Feasibility

### 2.1 DuckDB Linear Algebra

DuckDB has **limited** native linear algebra:
- No built-in matrix multiplication
- No matrix inverse operation
- Python UDFs can bridge but lose efficiency

### 2.2 Recommended Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   DuckDB    │────▶│   NumPy     │────▶│   DuckDB    │
│  (storage)  │     │  (compute)  │     │  (results)  │
└─────────────┘     └─────────────┘     └─────────────┘
     │                    │                    │
  Edges →          Transition Matrix     Absorption
  Node states         computation          probabilities
```

### 2.3 Absorbing Markov Chain Mathematics

For a transition matrix P partitioned as:

```
P = [Q  R]
    [0  I]
```

Where:
- Q = transient-to-transient probabilities (n×n)
- R = transient-to-absorbing probabilities (n×m)
- I = identity matrix (absorbing states)

**Fundamental Matrix**: N = (I - Q)^(-1)
- N[i,j] = expected number of visits to state j starting from state i

**Expected Steps**: t = N × 1 (vector of expected steps from each state)

**Absorption Probabilities**: B = N × R
- B[i,j] = probability of eventually reaching absorbing state j from state i

### 2.4 Implementation Requirements

1. **Transition Matrix Construction**: NumPy 2D array from OHM edges
2. **Matrix Inverse**: `numpy.linalg.inv()` or `numpy.linalg.solve()`
3. **Absorption Calculation**: NumPy matrix operations
4. **DuckDB Bridge**: Store results back to DuckDB

### 2.5 Complexity Considerations

- Matrix size = number of transient states
- Dense matrix operations: O(n³) for inverse
- With OHM graph limits, likely manageable for n < 1000

## 3. API Design Sketch

### 3.1 Primary Function: markov_absorbing_risk()

```python
def markov_absorbing_risk(
    conn: DuckDBPyConnection,
    start_node: str,
    *,
    edge_types: list[str] = ['CAUSES', 'TRANSITIONS_TO'],
    state_column: str = 'node_state',  # optional node attribute
) -> dict[str, float]:
    """
    Compute absorption probabilities from start_node.
    
    Returns:
        Dict mapping each absorbing state to P(absorb | start)
    """
    # 1. Extract transition matrix from edges
    # 2. Identify absorbing states (no outgoing edges in edge_types)
    # 3. Compute N = (I - Q)^(-1) using NumPy
    # 4. Compute B = N × R
    # 5. Return absorption probabilities
```

### 3.2 Secondary Function: markov_expected_steps()

```python
def markov_expected_steps(
    conn: DuckDBPyConnection,
    start_node: str,
    *,
    target_state: str | None = None,  # None = all states
    edge_types: list[str] = ['CAUSES', 'TRANSITIONS_TO'],
) -> float | dict[str, float]:
    """
    Compute expected steps to absorption or target state.
    
    Returns:
        Float if target_state specified, else dict of expected steps per state
    """
```

### 3.3 Steady-State Analysis (Future)

```python
def markov_steady_state(
    conn: DuckDBPyConnection,
    node_type: str = 'process',
    edge_types: list[str] = ['TRANSITIONS_TO'],
) -> dict[str, float]:
    """
    Compute long-run steady-state probabilities.
    Uses power iteration: π = π × P^∞
    """
```

## 4. Comparison: Markov vs Bayesian vs Monte Carlo

| Aspect | Bayesian Networks | Monte Carlo | Markov Chains |
|--------|-------------------|--------------|---------------|
| **Purpose** | Conditional inference | One-shot propagation | Sequential state evolution |
| **Time** | Static | One-shot | Infinite horizon |
| **Output** | P(node|evidence) | Distribution of outcomes | P(eventually absorb) |
| **Use Case** | "What's likely given what I know?" | "What's the distribution after N steps?" | "Will it eventually reach failure?" |
| **Absorption** | No | No | Yes |
| **Variance** | Yes (conditional) | Yes (sampling) | Yes (via N) |
| **Computation** | Variable elimination | N trials | Matrix operations |

## 5. Recommendation

### ✅ **GO: Implement as Substrate Method**

**Rationale**:
1. **Fills genuine gap**: Sequential multi-step analysis with absorption is not covered by existing methods
2. **Clear use cases**: Risk analysis, failure prediction, long-horizon planning
3. **Feasible architecture**: NumPy bridge is straightforward and performant
4. **Coherent with OHM**: Edges naturally represent state transitions

### 5.1 Implementation Plan

| Phase | File | Function | Dependencies |
|-------|------|----------|---------------|
| 1 | `src/ohm/markov.py` (new) | `markov_absorbing_risk()` | numpy |
| 2 | `src/ohm/markov.py` | `markov_expected_steps()` | numpy |
| 3 | `src/ohm/sdk.py` | `Graph.absorbing_risk()` wrapper | markov.py |
| 4 | `tests/test_markov.py` (new) | Full test suite | pytest |

### 5.2 DuckDB Integration

```python
# In methods.py or new markov.py
def markov_absorbing_risk(conn, start_node, *, edge_types=None):
    # Get edges as DataFrame
    edges_df = conn.execute("""
        SELECT from_node, to_node, probability, confidence
        FROM ohm_edges
        WHERE edge_type IN ('CAUSES', 'TRANSITIONS_TO')
          AND deleted_at IS NULL
    """).df()
    
    # Build transition matrix in NumPy
    nodes = unique list of all nodes in edges
    matrix = zeros((n, n))
    for _, row in edges_df.iterrows():
        i = node_to_idx[row['from_node']]
        j = node_to_idx[row['to_node']]
        p = row['probability'] or row['confidence'] or 0.5
        matrix[i, j] = p
    
    # Compute absorption via NumPy
    # ...
    
    # Store results
    result_df = DataFrame(absorption_probs)
    conn.execute("INSERT INTO ohm_markov_results ...")
```

### 5.3 Edge Cases to Handle

1. **No outgoing edges**: Node is absorbing (probability = 1.0)
2. **Disconnected subgraphs**: Analyze each connected component
3. **Cyclic transitions**: Valid for transient states, must have absorbing exit
4. **Missing probabilities**: Use confidence as fallback (current OHM pattern)

## 6. No-Go Alternative

If NumPy integration is not acceptable for OHM:
- **Alternative**: Agent-level implementation using external scipy
- **Tradeoff**: Less tightly integrated, requires agent to manage scipy lifecycle
- **Gap**: Sequential state evolution analysis would remain unimplemented

---

**Conclusion**: Markov Chains provide unique capability for OHM. Recommend implementation using NumPy bridge with results stored back to DuckDB. Phase 1 can begin immediately.