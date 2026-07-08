# OHM Capability-Exposure Batch — Design Note

**Date:** 2026-07-08  
**Author:** Métis (subagent)  
**Scope:** Expose OHM’s existing inference/analysis/discovery HTTP endpoints through the MCP server, document them in `GET /schema`, and bring the SDK `HttpGraph` class to parity with the local `Graph` class.

> **Status:** Design-only deliverable. No production code changes.

---

## 1. Background & Goal

OHM already implements a rich set of Bayesian/causal/game-theoretic endpoints in `src/ohm/server/handlers/inference.py`. They are wired into the daemon route table and have unit tests, but they are not yet discoverable or callable by agents that connect through the MCP sidecar or the `connect_http()` SDK. This batch closes that gap without changing the underlying math or the HTTP contract.

The three deliverables are:

1. **MCP tools** for `ohm_inference`, `ohm_intervene`, `ohm_voi`, `ohm_refute`, `ohm_discover` (plus related endpoints that are already tested).
2. **A new `analysis` / `inference` section** in the `GET /schema` guide so agents can discover these capabilities.
3. **SDK parity**: add the missing wrappers to the `HttpGraph` class inside `connect_http()`.

---

## 2. Inspection Summary

### 2.1 Files and line ranges inspected

| File | Lines | What it contains |
|------|-------|------------------|
| `src/ohm/mcp/server.py` | 1–728 | MCP server implementation. `list_tools()` at 107–355, `call_tool()` at 359–572. |
| `src/ohm/mcp/config.py` | 1–109 | Tool-filtering config (`WRITE_TOOLS`, `is_tool_allowed`). |
| `src/ohm/server/server.py` | 2600–2664, 1015–1033, 792–829 | Daemon route table; `OhmHandler` mixin inheritance; `_build_router()` registering `/inference`, `/intervene`, `/voi`, `/refute`, `/game`, `/nash`, `/policy`, `/discover`, etc. |
| `src/ohm/server/handlers/inference.py` | 1–492 | Inference handler mixin (`_get_inference`, `_get_intervene`, `_get_ate`, `_get_sensitivity`, `_get_adjustment`, `_get_voi`, `_get_voi_tasks`, `_get_suggest_causes`, `_get_refute`, `_get_regime`, `_get_game`, `_get_nash`, `_get_policy`, `_get_discover`, `_get_discovery_queue`, `_post_discovery_review`). |
| `src/ohm/server/handlers/graph.py` | 248–418 | `GET /schema` handler (`_get_schema`) returning the `guide` object. |
| `src/ohm/framework/sdk.py` | 28–5962 (`Graph`), 6096–7005 (`connect_http` / `HttpGraph`) | Local `Graph` class and HTTP subclass. |
| `src/ohm/inference/bayesian.py` | 864–2497 | Core inference functions (`bayesian_inference`, `causal_intervention`, `compute_ate`, `compute_sensitivity`, `find_adjustment_sets`, `suggest_causes`, `compute_voi`, `generate_voi_tasks`). |
| `src/ohm/inference/discovery.py` | 272–388 | `discover_causal` function. |
| `src/ohm/inference/pomdp.py` | 27–295 | `compute_policy` function. |
| `src/ohm/inference/game_theory.py` | 42–493 | `extract_game` and `compute_nash` functions. |
| `tests/test_mcp_e2e.py` | 1–235 | Existing MCP e2e tests. |
| `tests/test_server.py` | 208, 1398–1430, 1783–1836 | Schema endpoint test and VoI/policy endpoint tests. |
| `tests/test_sdk.py` | 1–1773+ | SDK unit tests; no `HttpGraph`-specific tests today. |
| `tests/test_bayesian.py`, `tests/test_bayesian_sensitivity.py`, `tests/test_game.py`, `tests/test_policy.py`, `tests/test_discovery.py` | Various | Unit tests for the underlying math. |

### 2.2 Existing HTTP endpoints to wrap

The following endpoints are live, have handler implementations, and are included in `OhmHandler._GET_EXACT`:

- `GET /inference?target=X&evidence=a:1,b:0&leak=0.15&...`
- `GET /intervene?target=X&state=0|1&query=a,b&leak=0.15&...`
- `GET /ate?cause=X&effect=Y&leak=0.15`
- `GET /sensitivity?cause=X&effect=Y&leak=0.15`
- `GET /adjustment?cause=X&effect=Y&leak=0.15`
- `GET /voi?decision=a,b&top=10&leak=0.15&root_prior=0.3&...`
- `GET /voi/tasks?agent=metis&decision=a,b&top=5&...`
- `GET /refute?cause=X&effect=Y&n_samples=1000&seed=42&methods=...`
- `GET /regime?target=X&evidence=...&window_days=30`
- `GET /game?target=X&players=a,b&layers=L3`
- `GET /nash?players=a,b&payoffs=[[...],[...]]`
- `GET /policy?target=X&observation_cost=0.1&horizon=1&...`
- `GET /discover?nodes=a,b,c&method=pc|ges|both&alpha=0.05&min_observations=5&...`
- `GET /discover/queue?status=pending&method=pc&limit=100`
- `POST /discover/queue/review` (`_post_discovery_review`)

---

## 3. MCP Tool Design

### 3.1 Tool naming convention

All new tools keep the `ohm_` prefix and snake-case HTTP endpoint names:

- `ohm_inference`
- `ohm_intervene`
- `ohm_ate`
- `ohm_sensitivity`
- `ohm_adjustment`
- `ohm_voi`
- `ohm_voi_tasks`
- `ohm_refute`
- `ohm_regime`
- `ohm_game`
- `ohm_nash`
- `ohm_policy`
- `ohm_discover`
- `ohm_discovery_queue`
- `ohm_review_discovery` (POST)

### 3.2 Tier classification

- **Read tier:** all tools above except `ohm_review_discovery`.
- **Write tier:** `ohm_review_discovery` only (it mutates the discovery queue and may create edges).

Update `src/ohm/mcp/config.py::WRITE_TOOLS` to include `ohm_review_discovery`.

### 3.3 Input schemas

Below are the proposed tool definitions to insert into `list_tools()` in `src/ohm/mcp/server.py` (after `ohm_path` / before `ohm_agents`, or grouped in a new “Analysis / Inference” section).

#### `ohm_inference`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "target": {"type": "string", "description": "Target node ID to compute posterior for"},
    "evidence": {"type": "string", "description": "Comma-separated node:state pairs, e.g. 'risk_a:1,risk_b:0'. State 0=bad, 1=good. Floats allowed for soft evidence."},
    "layers": {"type": "string", "description": "Comma-separated layers to include, e.g. 'L2,L3'"},
    "leak": {"type": "number", "description": "Baseline probability of bad outcome when all parents are good", "default": 0.15},
    "half_life": {"type": "number", "description": "Half-life in days for temporal decay", "default": 0.0},
    "observation_window": {"type": "number", "description": "Optional observation window in days"},
    "soft_evidence": {"type": "boolean", "description": "Include soft evidence edges", "default": false},
    "soft_edges": {"type": "string", "description": "Comma-separated edge types to treat as soft evidence"}
  },
  "required": ["target"]
}
```

HTTP call: `GET /inference?target={target}&evidence={evidence}&leak={leak}&...`

#### `ohm_intervene`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "target": {"type": "string", "description": "Node ID to intervene on"},
    "state": {"type": "integer", "description": "Intervention state: 0=bad, 1=good", "enum": [0, 1]},
    "query": {"type": "string", "description": "Comma-separated downstream node IDs to query (optional)"},
    "layers": {"type": "string", "description": "Comma-separated layers to include"},
    "leak": {"type": "number", "default": 0.15},
    "soft_evidence": {"type": "boolean", "default": false},
    "soft_edges": {"type": "string"},
    "preferred_edges": {"type": "string", "description": "Comma-separated from:to pairs of edges to preserve during cycle breaking, e.g. 'a:b,c:d'"}
  },
  "required": ["target", "state"]
}
```

HTTP call: `GET /intervene?target={target}&state={state}&query={query}&leak={leak}&...`

#### `ohm_ate`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "cause": {"type": "string"},
    "effect": {"type": "string"},
    "layers": {"type": "string"},
    "leak": {"type": "number", "default": 0.15}
  },
  "required": ["cause", "effect"]
}
```

HTTP call: `GET /ate?cause={cause}&effect={effect}&leak={leak}&layers={layers}`

#### `ohm_sensitivity`

Same schema as `ohm_ate`; HTTP call to `/sensitivity`.

#### `ohm_adjustment`

Same schema as `ohm_ate`; HTTP call to `/adjustment`.

#### `ohm_voi`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "decision": {"type": "string", "description": "Comma-separated decision node IDs (optional — auto-detected if omitted)"},
    "top": {"type": "integer", "description": "Number of candidates to return", "default": 10},
    "leak": {"type": "number", "default": 0.15},
    "root_prior": {"type": "number", "default": 0.3},
    "layers": {"type": "string"},
    "edge_types": {"type": "string", "description": "Comma-separated edge types to include"},
    "soft_evidence": {"type": "boolean", "default": false},
    "soft_edges": {"type": "string"},
    "timeout": {"type": "number", "description": "Optional timeout in seconds"},
    "min_observations": {"type": "integer", "default": 0, "description": "Minimum observation count before low_data_warning is raised"}
  },
  "required": []
}
```

HTTP call: `GET /voi?decision={decision}&top={top}&leak={leak}&root_prior={root_prior}&...`

#### `ohm_voi_tasks`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "agent": {"type": "string", "description": "Filter tasks for a specific agent"},
    "decision": {"type": "string"},
    "top": {"type": "integer", "default": 5},
    "leak": {"type": "number", "default": 0.15},
    "root_prior": {"type": "number", "default": 0.3},
    "layers": {"type": "string"}
  },
  "required": []
}
```

HTTP call: `GET /voi/tasks?agent={agent}&decision={decision}&top={top}&...`

#### `ohm_refute`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "cause": {"type": "string"},
    "effect": {"type": "string"},
    "n_samples": {"type": "integer", "default": 1000},
    "seed": {"type": "integer", "default": 42},
    "methods": {"type": "string", "description": "Comma-separated refutation methods, e.g. 'random_common_cause,placebo_treatment,data_subset'"}
  },
  "required": ["cause", "effect"]
}
```

HTTP call: `GET /refute?cause={cause}&effect={effect}&n_samples={n_samples}&seed={seed}&methods={methods}`

#### `ohm_regime`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "target": {"type": "string"},
    "evidence": {"type": "string"},
    "layers": {"type": "string"},
    "leak": {"type": "number", "default": 0.15},
    "window_days": {"type": "number", "default": 30.0}
  },
  "required": ["target"]
}
```

HTTP call: `GET /regime?target={target}&evidence={evidence}&leak={leak}&window_days={window_days}&...`

#### `ohm_game`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "target": {"type": "string", "description": "Decision node that roots the game"},
    "players": {"type": "string", "description": "Comma-separated player agent names (optional)"},
    "layers": {"type": "string"}
  },
  "required": ["target"]
}
```

HTTP call: `GET /game?target={target}&players={players}&layers={layers}`

#### `ohm_nash`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "players": {"type": "string", "description": "Comma-separated player names, e.g. 'alice,bob'"},
    "payoffs": {"type": "string", "description": "JSON array of payoff matrices (output from /game)"}
  },
  "required": ["players", "payoffs"]
}
```

HTTP call: `GET /nash?players={players}&payoffs={payoffs}`

#### `ohm_policy`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "target": {"type": "string"},
    "observation_cost": {"type": "number", "description": "Cost of observing (optional)"},
    "horizon": {"type": "integer", "default": 1},
    "layers": {"type": "string"},
    "leak": {"type": "number", "default": 0.15}
  },
  "required": ["target"]
}
```

HTTP call: `GET /policy?target={target}&observation_cost={observation_cost}&horizon={horizon}&leak={leak}&...`

#### `ohm_discover`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "nodes": {"type": "string", "description": "Comma-separated node IDs to include (optional — auto-selected if omitted)"},
    "method": {"type": "string", "enum": ["pc", "ges", "both"], "default": "pc"},
    "alpha": {"type": "number", "default": 0.05},
    "min_observations": {"type": "integer", "default": 5},
    "indep_test": {"type": "string", "default": "fisherz"},
    "score_class": {"type": "string", "default": "local_score_BIC"},
    "queue": {"type": "boolean", "default": false, "description": "If true, queue discovered candidates for agent review"}
  },
  "required": []
}
```

HTTP call: `GET /discover?nodes={nodes}&method={method}&alpha={alpha}&min_observations={min_observations}&queue={queue}&...`

#### `ohm_discovery_queue`

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "status": {"type": "string", "description": "Filter by status (pending, accepted, rejected)"},
    "method": {"type": "string"},
    "limit": {"type": "integer", "default": 100}
  },
  "required": []
}
```

HTTP call: `GET /discover/queue?status={status}&method={method}&limit={limit}`

#### `ohm_review_discovery` (write-tier)

```json
{
  "type": "object",
  "properties": {
    "format": {"type": "string", "enum": ["json", "toon"], "default": "json"},
    "queue_id": {"type": "string"},
    "action": {"type": "string", "enum": ["accept", "reject"]},
    "reviewed_by": {"type": "string"},
    "review_notes": {"type": "string"},
    "edge_layer": {"type": "string", "default": "L3"}
  },
  "required": ["queue_id", "action"]
}
```

HTTP call: `POST /discover/queue/review` with JSON body.

### 3.4 `call_tool()` additions

In `src/ohm/mcp/server.py` the `elif name == "..."` chain needs a branch for each new tool. Each branch builds the query/body exactly like the existing handlers, calls `_ohm_get` or `_ohm_post`, and returns `_text(data, fmt)`. The `format` argument is consumed here (as it is today) and not forwarded to OHM.

For string-list parameters the pattern is:

```python
params: dict[str, str] = {}
if arguments.get("evidence"):
    params["evidence"] = arguments["evidence"]
if arguments.get("layers"):
    params["layers"] = arguments["layers"]
params["leak"] = str(arguments.get("leak", 0.15))
path = f"/inference?target={arguments['target']}"
if params:
    path += "&" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
```

The `urllib.parse` import is not currently in `server.py`; add it at the top of the file.

---

## 4. `/schema` Guide Section

### 4.1 Location

Modify `src/ohm/server/handlers/graph.py` inside `_get_schema()` (lines 248–418). Add a new top-level key to the `guide` dict:

```python
"analysis": { ... }
```

### 4.2 Proposed content

```python
"analysis": {
    "overview": (
        "OHM can reason over the causal graph using Bayesian networks, interventions, "
        "value-of-information, game theory, and causal discovery. These endpoints are read-only "
        "unless noted. They all consume the same causal edges (CAUSES, DEPENDS_ON, THREATENS, "
        "INFLUENCES, etc.) that the graph already contains."
    ),
    "inference": (
        "GET /inference?target=NODE\u0026evidence=a:1,b:0 — Compute P(target | evidence). "
        "State 0 = bad/closed/negative, 1 = good/open/positive. Use &layers=L2,L3 to scope edges. "
        "Use &soft_evidence=1 and &soft_edges=INFLUENCES to include non-causal soft evidence."
    ),
    "intervention": (
        "GET /intervene?target=NODE\u0026state=1 — Pearl do-operator. Sever incoming edges to target, "
        "force it to state 0 or 1, and propagate. Add &query=x,y to limit posteriors to specific "
        "downstream nodes. Compare with /inference to quantify confounding bias."
    ),
    "ate": "GET /ate?cause=X\u0026effect=Y — Average Treatment Effect from the Bayesian model.",
    "sensitivity": "GET /sensitivity?cause=X\u0026effect=Y — E-value: how much unmeasured confounding would overturn the conclusion?",
    "adjustment": "GET /adjustment?cause=X\u0026effect=Y — Valid backdoor/frontdoor adjustment sets for causal identification.",
    "voi": (
        "GET /voi?decision=a,b\u0026top=10 — Rank ancestors of decision nodes by expected value of "
        "perfect information. Returns per-candidate EVPI, downstream decisions, and low-data warnings."
    ),
    "voi_tasks": (
        "GET /voi/tasks?agent=metis\u0026decision=x — Turn VoI output into agent task assignments "
        "(research, observation, challenge)."
    ),
    "refutation": (
        "GET /refute?cause=X\u0026effect=Y — DoWhy refutation tests on synthetic data generated from "
        "the Bayesian network. Methods: random_common_cause, placebo_treatment, data_subset, "
        "unobserved_confounder."
    ),
    "regime": "GET /regime?target=X\u0026window_days=30 — Compare full-history vs. windowed posteriors to detect regime shifts.",
    "game_theory": {
        "extract": "GET /game?target=DECISION — Extract a normal-form game from the causal graph around a decision node.",
        "nash": "GET /nash?players=a,b\u0026payoffs=JSON — Compute Nash equilibrium for extracted payoff matrices.",
    },
    "policy": (
        "GET /policy?target=DECISION\u0026observation_cost=0.1 — Belief-state POMDP recommendation: "
        "observe vs. act. Returns EVPI, current belief, confidence, and top observation candidates."
    ),
    "discovery": {
        "run": "GET /discover?nodes=a,b,c\u0026method=pc — Causal structure discovery from observation data using PC or GES.",
        "queue": "GET /discover/queue — List pending discovery candidates for review.",
        "review": "POST /discover/queue/review — Accept or reject a candidate (write-tier). Body: queue_id, action, review_notes, edge_layer.",
    },
    "common_parameters": {
        "leak": "Baseline probability of bad outcome when all parents are good (default 0.15).",
        "layers": "Comma-separated layer filter, e.g. L2,L3.",
        "soft_evidence": "Include INFLUENCES/EXPECTED_LIKELIHOOD edges as soft evidence.",
    },
    "sdk_methods": "See SDK: Graph.bayesian_inference(), .causal_intervention(), .ate(), .sensitivity(), .adjustment(), .value_of_information(), .voi_tasks(), .refute(), .extract_game(), .compute_nash(), .compute_policy(), .discover_causal()."
}
```

### 4.3 Test update

`tests/test_server.py::TestSchemaEndpoints::test_schema_returns_types` currently only asserts the existence of `node_types`, `edge_types`, and `layers`. Add:

```python
assert "guide" in data
assert "analysis" in data["guide"]
assert "inference" in data["guide"]["analysis"]
```

---

## 5. SDK `HttpGraph` Parity

### 5.1 Current gap

`HttpGraph` (defined inside `connect_http()` at `src/ohm/framework/sdk.py:6142`) overrides only a subset of `Graph` methods. The local `Graph` class has many inference/analysis methods that are missing from `HttpGraph`. In addition, `Graph` defines methods like `value_of_information()`, `discover_causal()`, `extract_game()`, `compute_nash()`, `compute_policy()`, `regime_detection()` that do not exist at all.

Inspection shows:

- **In `Graph` only (not in `HttpGraph`):** 150+ methods including `anomalies`, `calibration`, `cascade_scenario`, `deterministic_cascade`, `monte_carlo_cascade`, `what_if`, `suggest_connections`, `contradictions`, `differential_diagnosis`, `near_duplicates`, `discover_peers`, `markov_absorbing_risk`, etc.
- **In `HttpGraph` only:** `adjustment`, `ate`, `bayesian_inference`, `causal_intervention`, `lint`, `refute`, `sensitivity`, `suggest_causes`.

### 5.2 Proposed additions to `HttpGraph`

All new methods follow the existing HTTP-GET pattern (build query string, call `_http_request("GET", path)`, return result).

#### Missing inference/analysis wrappers

```python
def value_of_information(
    self,
    decision_nodes: list[str] | None = None,
    *,
    top: int = 10,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    layers: list[str] | None = None,
    edge_types: list[str] | None = None,
    timeout: float | None = None,
    min_observations: int = 0,
) -> dict[str, Any]:
    """Rank ancestors of decision nodes by expected value of perfect information."""
    import urllib.parse
    params = [f"top={top}", f"leak={leak_probability}", f"root_prior={root_prior}"]
    if decision_nodes:
        params.append(f"decision={urllib.parse.quote(','.join(decision_nodes))}")
    if layers:
        params.append(f"layers={urllib.parse.quote(','.join(layers))}")
    if edge_types:
        params.append(f"edge_types={urllib.parse.quote(','.join(edge_types))}")
    if timeout is not None:
        params.append(f"timeout={timeout}")
    if min_observations:
        params.append(f"min_observations={min_observations}")
    path = "/voi?" + "&".join(params)
    return self._http_request("GET", path)

# Alias for naming consistency with Graph/HttpGraph style
voi = value_of_information


def voi_tasks(
    self,
    *,
    agent: str | None = None,
    decision_nodes: list[str] | None = None,
    top: int = 5,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """Generate agent task assignments from VoI output."""
    import urllib.parse
    params = [f"top={top}", f"leak={leak_probability}", f"root_prior={root_prior}"]
    if agent:
        params.append(f"agent={urllib.parse.quote(agent)}")
    if decision_nodes:
        params.append(f"decision={urllib.parse.quote(','.join(decision_nodes))}")
    if layers:
        params.append(f"layers={urllib.parse.quote(','.join(layers))}")
    return self._http_request("GET", "/voi/tasks?" + "&".join(params))


def discover_causal(
    self,
    *,
    node_ids: list[str] | None = None,
    method: str = "pc",
    alpha: float = 0.05,
    min_observations: int = 5,
    indep_test: str = "fisherz",
    score_class: str = "local_score_BIC",
    queue: bool = False,
) -> dict[str, Any]:
    """Run causal structure discovery on observation data."""
    import urllib.parse
    params = [
        f"method={urllib.parse.quote(method)}",
        f"alpha={alpha}",
        f"min_observations={min_observations}",
        f"indep_test={urllib.parse.quote(indep_test)}",
        f"score_class={urllib.parse.quote(score_class)}",
        f"queue={str(queue).lower()}",
    ]
    if node_ids:
        params.append(f"nodes={urllib.parse.quote(','.join(node_ids))}")
    return self._http_request("GET", "/discover?" + "&".join(params))


def discovery_queue(
    self,
    *,
    status: str | None = None,
    method: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List pending discovery candidates."""
    import urllib.parse
    params = [f"limit={limit}"]
    if status:
        params.append(f"status={urllib.parse.quote(status)}")
    if method:
        params.append(f"method={urllib.parse.quote(method)}")
    return self._http_request("GET", "/discover/queue?" + "&".join(params))


def review_discovery_candidate(
    self,
    *,
    queue_id: str,
    action: str,  # "accept" or "reject"
    reviewed_by: str | None = None,
    review_notes: str | None = None,
    edge_layer: str = "L3",
) -> dict[str, Any]:
    """Accept or reject a discovery candidate."""
    body = {"queue_id": queue_id, "action": action, "edge_layer": edge_layer}
    if reviewed_by:
        body["reviewed_by"] = reviewed_by
    if review_notes:
        body["review_notes"] = review_notes
    return self._http_request("POST", "/discover/queue/review", body)


def extract_game(
    self,
    target: str,
    *,
    players: list[str] | None = None,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """Extract a normal-form game from the causal graph around a decision node."""
    import urllib.parse
    params = [f"target={urllib.parse.quote(target)}"]
    if players:
        params.append(f"players={urllib.parse.quote(','.join(players))}")
    if layers:
        params.append(f"layers={urllib.parse.quote(','.join(layers))}")
    return self._http_request("GET", "/game?" + "&".join(params))


def compute_nash(
    self,
    payoff_matrices: list[Any],
    players: list[str],
) -> dict[str, Any]:
    """Compute Nash equilibrium for extracted payoff matrices."""
    import json
    import urllib.parse
    params = [
        f"players={urllib.parse.quote(','.join(players))}",
        f"payoffs={urllib.parse.quote(json.dumps(payoff_matrices))}",
    ]
    return self._http_request("GET", "/nash?" + "&".join(params))


def compute_policy(
    self,
    target: str,
    *,
    observation_cost: float | None = None,
    horizon: int = 1,
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Belief-state POMDP: observe vs. act recommendation."""
    import urllib.parse
    params = [f"target={urllib.parse.quote(target)}", f"horizon={horizon}", f"leak={leak_probability}"]
    if observation_cost is not None:
        params.append(f"observation_cost={observation_cost}")
    if layers:
        params.append(f"layers={urllib.parse.quote(','.join(layers))}")
    return self._http_request("GET", "/policy?" + "&".join(params))


def regime_detection(
    self,
    target: str,
    *,
    evidence: dict[str, int | float] | None = None,
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    window_days: float = 30.0,
) -> dict[str, Any]:
    """Compare full-history vs. windowed posteriors to detect regime shifts."""
    import urllib.parse
    params = [
        f"target={urllib.parse.quote(target)}",
        f"leak={leak_probability}",
        f"window_days={window_days}",
    ]
    if evidence:
        ev = ",".join(f"{k}:{v}" for k, v in evidence.items())
        params.append(f"evidence={urllib.parse.quote(ev)}")
    if layers:
        params.append(f"layers={urllib.parse.quote(','.join(layers))}")
    return self._http_request("GET", "/regime?" + "&".join(params))
```

#### Updates to existing `HttpGraph` methods

- `bayesian_inference()` already exists. Extend it to accept the newer parameters (`layers`, `half_life`, `observation_window`, `soft_evidence`, `soft_edges`) so it matches the server handler.
- `causal_intervention()` already exists. Extend it to accept `layers`, `preferred_edges`, `soft_evidence`, `soft_edges`.

#### Naming note

`HttpGraph` currently uses `bayesian_inference`, `causal_intervention`, `ate`, etc. The new methods should use the same noun/verb style. We will keep `value_of_information` as the canonical long name and add `voi` as an alias (matching the `/voi` endpoint). `extract_game`, `compute_nash`, `compute_policy`, and `discover_causal` are already the names used by the underlying library functions.

### 5.3 Optional parity candidates (out of scope for this batch, listed for tracking)

The following local `Graph` methods are useful but do not have existing `/` HTTP endpoints or are not in the requested batch. They should be handled in follow-up issues:

- `anomalies`, `contradictions`, `near_duplicates` — have `/anomalies`, `/contradictions`, `/duplicates` endpoints but not full wrapper parity.
- `deterministic_cascade`, `monte_carlo_cascade`, `what_if` — have `/impact/`, `/monte-carlo/`, `/impact/` endpoints; mapping is non-trivial.
- `markov_absorbing_risk`, `markov_expected_steps` — have `/markov/*` endpoints.
- `suggest_connections`, `discover_peers` — have `/suggest` and `/agents` endpoints.

---

## 6. Test Plan

### 6.1 Existing tests to run

Run the full relevant test matrix before and after implementation:

```bash
cd /root/olympus/OHM
python3 -m pytest tests/test_mcp_e2e.py tests/test_server.py::TestSchemaEndpoints tests/test_server.py::TestVoI tests/test_server.py::TestPolicyEndpoint tests/test_bayesian.py tests/test_bayesian_sensitivity.py tests/test_game.py tests/test_policy.py tests/test_discovery.py tests/test_sdk.py -q
```

(Marker names are illustrative; actual test classes/methods should be discovered with `pytest --collect-only`.)

### 6.2 New tests to add

#### MCP layer

In `tests/test_mcp_e2e.py`, extend the e2e fixture to build a small causal graph and add:

- `test_mcp_inference`: call `ohm_inference` against a graph with a `CAUSES` edge and evidence, assert `posterior` is present.
- `test_mcp_intervene`: call `ohm_intervene` with `state=1`, assert `posteriors` and `confounding_bias` present.
- `test_mcp_voi`: call `ohm_voi` with a decision node, assert `rankings` list present.
- `test_mcp_refute`: call `ohm_refute`, assert refutation method keys present.
- `test_mcp_discover`: seed observations, call `ohm_discover`, assert `candidate_edges` present.
- `test_mcp_game_and_nash`: create decision/utility nodes, call `ohm_game` then `ohm_nash` with returned payoffs, assert equilibrium present.
- `test_mcp_policy`: create a decision node, call `ohm_policy`, assert `recommendation` in (`observe`, `act`).
- `test_mcp_schema_guide_has_analysis`: call `ohm_domain_onboarding`, assert `guide.analysis.inference` exists.

Also add unit-style tests in a new `tests/test_mcp_inference.py` that mock `_ohm_get` / `_ohm_post` and assert the correct paths/query strings are built.

#### SDK layer

In `tests/test_sdk.py` (or a new `tests/test_sdk_http_parity.py`):

- Add a `TestHttpGraphInference` class using a mocked `urllib.request.urlopen` (similar to existing HTTP-less SDK tests where possible, or using a real `ohmd` subprocess).
- Assert that each new `HttpGraph` method builds the expected URL and returns the mocked response.
- Add a parameterized test that verifies every new method name also exists on the base `Graph` class or has a documented local equivalent.

#### Server/schema layer

In `tests/test_server.py::TestSchemaEndpoints`:

- `test_schema_guide_has_analysis_section`: assert the new `guide["analysis"]` block and key strings.

### 6.3 Backwards-compatibility test

- Run `tests/test_mcp_e2e.py` with `allowed_tools=["ohm_stats"]` to ensure new tools are filtered correctly and read-only still blocks writes.
- Verify `--dump-tools` output includes the new tools with `allowed` flags.

---

## 7. Proposed Beads Issues

### New issues

| ID proposal | Title | Priority | Parent | Notes |
|-------------|-------|----------|--------|-------|
| `OHM-yzyk.1.6` | MCP tools for inference, intervention, VoI, refute, discovery | P1 | `OHM-yzyk.1` | This batch. Covers all tools in §3. |
| `OHM-rx7h.1` | Add `analysis` section to `GET /schema` guide | P2 | `OHM-rx7h` | Schema documentation only; low risk. |
| `OHM-azn.5` | SDK `HttpGraph` parity: VoI, discovery, game theory, policy | P1 | `OHM-azn` | Closes the SDK/HTTP gap. |

### Issues to update

- `OHM-yzyk.1.2` — MCP server: implement --config file and allowed_tools enforcement  
  Update: mention that new inference tools are governed by the same `allowed_tools` / `read_only` filtering.
- `OHM-461f.1` — Open Skills needs schema guide + template/query endpoints  
  Update: the new `guide.analysis` block partially satisfies schema discoverability; note overlap.

### Suggested commit message

```
feat(OHM-yzyk.1.6, OHM-rx7h.1, OHM-azn.5): expose inference/discovery capabilities

- Add MCP tools: ohm_inference, ohm_intervene, ohm_ate, ohm_sensitivity,
  ohm_adjustment, ohm_voi, ohm_voi_tasks, ohm_refute, ohm_regime,
  ohm_game, ohm_nash, ohm_policy, ohm_discover, ohm_discovery_queue,
  ohm_review_discovery.
- Document analysis/inference endpoints in GET /schema guide.
- Bring SDK HttpGraph to parity: value_of_information, voi_tasks,
  discover_causal, discovery_queue, review_discovery_candidate,
  extract_game, compute_nash, compute_policy, regime_detection.
- Extend existing HttpGraph bayesian_inference/causal_intervention to
  pass layers, soft_evidence, preferred_edges, etc.
- Add tests in tests/test_mcp_e2e.py, tests/test_mcp_inference.py,
  tests/test_sdk_http_parity.py, and update TestSchemaEndpoints.
```

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| URL query encoding issues (node IDs with spaces, commas, colons) | Medium | Medium | Re-use `urllib.parse.quote` pattern already used in existing `HttpGraph` methods; add unit tests for exotic node IDs. |
| New MCP tools increase the default tool list, possibly overwhelming clients | Low | Low | New tools are still filtered by `allowed_tools`; default `["*"]` unchanged. `--dump-tools` lets operators audit. |
| `HttpGraph` method names collide with future local `Graph` methods | Low | Medium | Use the same canonical names as underlying functions (`extract_game`, `compute_nash`, etc.). |
| Discovery (`/discover`) depends on `pgmpy`/observation tables; may 500 on sparse graphs | Medium | Low | Handler already catches `ValueError`/`TypeError` and returns 400. MCP layer should propagate HTTP errors unchanged. |
| `/nash` requires a valid JSON payoff matrix from `/game`; easy to misuse | Medium | Low | Clear docstrings and schema descriptions; examples in `/schema` guide. |
| Schema guide bloat | Low | Low | Keep `analysis` section concise; heavy detail stays in endpoint docstrings and SDK docs. |

### Reversibility

- **MCP tools:** removing a tool is a single deletion from `list_tools()` and `call_tool()`. No DB schema changes.
- **Schema guide:** removing the `analysis` key is a single edit in `_get_schema()`.
- **SDK methods:** removing `HttpGraph` methods does not affect the local `Graph` class or the daemon. The methods are purely client-side wrappers.
- No new migrations, no new tables, no new CLI commands.

---

## 9. Implementation Order

1. Add SDK `HttpGraph` methods first — they can be tested with mocked HTTP and provide the canonical query-string building logic that the MCP layer can mirror.
2. Add MCP tool definitions and `call_tool()` branches — reuse the query-building logic from step 1, possibly extracting a small helper.
3. Update `WRITE_TOOLS` in `src/ohm/mcp/config.py`.
4. Add `analysis` section to `GET /schema`.
5. Write tests: MCP unit, MCP e2e, SDK parity, schema guide.
6. Run the full test matrix from §6.1.

---

## 10. Three-Bullet Summary

- **MCP:** Add 15 read-tier tools (`ohm_inference`, `ohm_intervene`, `ohm_voi`, `ohm_refute`, `ohm_discover`, etc.) plus one write-tier `ohm_review_discovery`, all wired to existing daemon endpoints in `src/ohm/server/handlers/inference.py`.
- **Schema:** Add an `analysis` section to the `GET /schema` guide (`src/ohm/server/handlers/graph.py:248`) so agents can discover Bayesian inference, interventions, VoI, refutation, game theory, policy, and causal discovery capabilities.
- **SDK parity:** Extend the `HttpGraph` class inside `connect_http()` (`src/ohm/framework/sdk.py:6142`) with `value_of_information`, `voi_tasks`, `discover_causal`, `discovery_queue`, `review_discovery_candidate`, `extract_game`, `compute_nash`, `compute_policy`, and `regime_detection`, while strengthening existing `bayesian_inference` / `causal_intervention` wrappers.

## 11. FastMCP gateway impact

This batch is the most directly relevant to the FastMCP gateway (`ohm-gateway`) defined in ADR-028:

- **Shared tool surface:** The new MCP tools in the local sidecar become the canonical set that the gateway re-exposes. To avoid schema drift between the raw stdio sidecar and the FastMCP gateway, move tool definitions (names, descriptions, input schemas, HTTP endpoint mappings) into a shared module such as `src/ohm/mcp/tools.py` or `src/ohm/mcp/tool_registry.py`. Both `src/ohm/mcp/server.py` (raw SDK) and `ohm-gateway` (FastMCP) should import from it.
- **Long-running tools:** `ohm_discover`, deep `ohm_refute`, and large `ohm_listen` are good candidates for FastMCP `@mcp.tool(task=True)` Background Tasks on Lambda/Streamable HTTP to avoid the 29s API Gateway limit. The efficiency batch raises the threshold for needing Background Tasks but does not eliminate it.
- **Schema guide as MCP resource/prompt:** The new `analysis` section maps cleanly to the gateway's planned `ohm://domain-schema` resource and `ohm-domain-onboarding` prompt. Reuse the same JSON content so local sidecar, gateway, and SDK clients all see identical capability documentation.
- **SDK parity:** Typed `HttpGraph` methods give the gateway an alternative to raw HTTP forwarding, and they can be wrapped directly as FastMCP tools if we later build a Python-based gateway implementation.
- **Guardrails:** New inference tools are read-tier except `ohm_review_discovery`. The gateway's `allowed_tools` and `read_only` filters should classify the new tools accordingly; update the default tenant profile in ADR-028 §4.

**Recommendation:** add a design task to the capability batch: extract a transport-agnostic tool registry and make both local MCP and `ohm-gateway` consume it.
