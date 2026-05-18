# OHM Vision — What We're Building and Why

## The Problem

Knowledge graphs for multi-agent systems either collapse into consensus (everyone sees the same truth) or fragment into silos (each agent has its own disconnected view). Neither works.

Consensus destroys the information that makes multi-agent systems valuable — the *disagreement* between perspectives. If Clio finds something with 0.6 confidence and Socrates challenges it, that tension is *the signal*, not noise. Flattening it is data destruction.

Silos are the other failure mode — agents can't coordinate because they can't see each other's work. Clio researches something Métis already knows. Hephaestus audits code that Socrates already challenged. Redundant, wasteful, and slow.

## The Solution

OHM preserves individual judgment while enabling shared awareness. The mechanism is simple:

1. **Layers of ownership.** L1/L2 are shared (facts, citations). L3/L4 are agent-owned (interpretations, predictions). Private stays private.
2. **Challenge, don't overwrite.** When Socrates disagrees with Clio, he doesn't erase her edge. He creates a CHALLENGED_BY edge. The graph accumulates perspectives. It never collapses.
3. **Attribution is identity.** Every edge says who created it. Confidence scores say how sure they are. The graph knows *who thinks what* and *how strongly*.

This is Popperian epistemology as database schema. Knowledge grows through refutation, not consensus. The hive mind doesn't think one thought — it holds many and records how they relate.

## What "Done" Looks Like

### Phase 0: Foundation ✅

- Schema, CLI, recursive CTEs, boundary enforcement
- Python SDK, input validation, exception hierarchy
- 528 tests passing across all modules

### Phase 1: Daemon + Multi-Agent ✅

- `ohmd` running with Bearer token auth, per-agent roles
- Health/ready/status endpoints with correlation IDs
- Error handling with exit codes
- Systemd unit file
- 9 agents registered with values, goals, and capabilities
- Server test coverage (17 HTTP endpoints)

### Phase 2: Domain Flexibility ✅

- Cattle + retail scenarios with domain-specific edge types
- Medical diagnosis (NEGATES, compound_confidence, differential_diagnosis)
- Cybersecurity incident response (source reliability, threat clustering, SSE batch)
- Supply chain disruption (probability-weighted edges, cascade simulation, what_if)
- Customer support (priority, handoff chains, resolution state machine)
- Urgency and priority as first-class schema fields
- Schema v0.5.0: urgency, priority, probability, NEGATES, scenario edge types
- 6 scenario docs with runnable code examples
- 603+ tests passing

### Phase 3: Agent Integration (next)

- Each Olympus agent uses OHM as its knowledge graph
- Métis zettelkasten → OHM nodes/edges
- Clio research findings → OHM L3 edges
- Hephaestus audit findings → OHM observations
- Socrates challenges → OHM CHALLENGED_BY edges
- Change feed enables reactive behavior

### Phase 4: DuckLake + Time Travel (later)

- Shared DuckLake backend
- `ohm snapshot <timestamp>` — historical graph state
- `ohm diff <t1> <t2>` — change comparison
- Agent heartbeat sync

### Phase 5: Advanced + TOPO

- Confidence decay for L4 edges
- Source reliability calibration
- Cascade simulation (Monte Carlo)
- TOPO industrial instantiation
- Shared ohmd/topod daemon codebase

## Agent Interface

Agents use the **Python SDK** (`ohm.sdk`) for regular graph operations. The CLI is for human diagnostics and ad-hoc exploration. The HTTP daemon (ohmd) is for shared access.

```
Agent code  → SDK → queries/ → DuckDB (local cache)
Human       → CLI → queries/ → DuckDB (one-shot)
HTTP clients → ohmd → store.py → DuckDB (shared daemon)
```

All three paths go through the same logic and boundary enforcement. The difference is interface: Python API vs text output vs JSON over HTTP.

## What I Don't Care About

- **Pretty UI.** This is an agent API, not a dashboard.
- **Perfect code.** Ship, then iterate. 528 tests is solid coverage.
- **Debate about architecture.** The schema works. The boundary rules work. The challenge semantics work. Build on top, don't redesign underneath.
- **Feature creep.** If it doesn't help agents share awareness while preserving judgment, it doesn't belong here.

## Architecture Decisions That Are Locked

1. **DuckDB, not Postgres.** Embedded, zero-config, recursive CTEs. Agents don't need a DBA.
2. **Challenge edges, not modification.** ADR-002. This is the core insight. Don't break it.
3. **Quack for concurrent access.** HTTP protocol, not IPC. Agents are processes, not threads.
4. **CLI as primary interface.** If it doesn't work from the command line, it doesn't work.
5. **Attribution on every write.** No anonymous edges. The graph must know who thinks what.
6. **Probability ≠ confidence.** Confidence is agent belief; probability is objective likelihood. Both are needed for risk modeling. (ADR-008)
7. **Urgency ≠ priority.** Urgency is time-sensitivity of information (edge). Priority is importance of an entity (node). Different dimensions. (ADR-009)
8. **NEGATES is not CHALLENGED_BY.** CHALLENGED_BY is subjective disagreement. NEGATES is objective ruling-out. Both preserve the original. (ADR-010)

## Architecture Decisions That Are Open

1. **Observation type extensibility.** Should `VALID_OBSERVATION_TYPES` be a frozenset (requiring schema.py changes), a SchemaConfig field (extensible per domain), or an open string with suggested types? Need ADR before medical/cyber scenarios.

2. **DuckLake sync strategy.** How often? On write? On heartbeat? Batch? Real-time?
   - *Leaning:* Heartbeat-based. Each agent syncs when it checks in (every 5-15 minutes).

3. **Conflict resolution.** What happens when two agents write the same L2 edge simultaneously?
   - *Leaning:* L2 edges are shared — last-write-wins with attribution. L3/L4 edges are agent-owned — no conflict possible.

## How to Contribute

1. `bd list` — see what's available
2. `bd claim OHM-xxx` — claim it
3. Write code, run tests, push
4. `bd close OHM-xxx` — mark it done
5. I review direction. Other agents review code.

---

*The name is OHM — the unit of resistance. It preserves signal integrity. It preserves individuality against collapse into groupthink. That's the whole point.*