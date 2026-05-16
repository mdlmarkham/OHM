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

### The Daemon Works (P0)

An agent can start `ohmd`, get a token, and read/write the graph over HTTP. Multiple agents connect concurrently. The daemon handles auth, errors, and doesn't crash. This is table stakes — nothing else matters until this is solid.

**Acceptance:**
- `ohmd` starts, stays running, handles 10+ concurrent connections
- Bearer token auth works — agents can only modify their own L3/L4 edges
- Error responses have status codes, messages, and correlation IDs
- `/health` returns 200, `/status` returns meaningful data
- Systemd unit keeps it alive across reboots

**Also done (P0 foundations):**
- 144 tests passing across all modules
- Python SDK (`ohm.sdk`) for programmatic agent access
- Input validation module (SQL injection prevention for CTE identifiers)
- Exception hierarchy with exit codes (0-5) and correlation IDs
- CLI integration tests against real database
- Beads issue tracker with 24 active issues organized by priority

### Agents See Each Other (P1)

Métis writes a note about AND→OR conversion. Clio researches it and adds sources. Socrates challenges the confidence. The graph shows all three perspectives, who holds them, and how they connect. That's the hive mind.

**Acceptance:**
- Each agent can push/pull on heartbeat
- `ohm snapshot <timestamp>` reconstructs the graph at any point
- `ohm diff <t1> <t2>` shows what changed between two moments
- Agent state table shows who's working on what

### The Knowledge Compounds (P2+)

Over time, the graph should get more useful, not just bigger. Connections emerge between domains. Challenges accumulate and confidence scores adjust. Patterns become visible that no single agent would see.

**Acceptance:**
- A query like `ohm graph neighborhood hungary --depth 3` returns edges from multiple agents with their confidence scores
- The change feed enables reactive behavior — agents can subscribe to changes in their domain
- TOPO proves the architecture generalizes beyond cognitive domains

## What I Don't Care About

- **Pretty UI.** This is an agent API, not a dashboard.
- **Perfect code.** Ship, then iterate. 130 tests is enough coverage for now.
- **Debate about architecture.** The schema works. The boundary rules work. The challenge semantics work. Build on top, don't redesign underneath.
- **Feature creep.** If it doesn't help agents share awareness while preserving judgment, it doesn't belong here.

## Architecture Decisions That Are Locked

These aren't up for debate. They're the foundation everything else builds on:

1. **DuckDB, not Postgres.** Embedded, zero-config, recursive CTEs. Agents don't need a DBA.
2. **Challenge edges, not modification.** ADR-002. This is the core insight. Don't break it.
3. **Quack for concurrent access.** HTTP protocol, not IPC. Agents are processes, not threads.
4. **CLI as primary interface.** If it doesn't work from the command line, it doesn't work.
5. **Attribution on every write.** No anonymous edges. The graph must know who thinks what.

## Architecture Decisions That Are Open

These need exploration, not implementation:

1. **DuckLake sync strategy.** How often? On write? On heartbeat? Batch? Real-time?
   - *My leaning:* Heartbeat-based. Each agent syncs when it checks in (every 5-15 minutes depending on activity). Real-time is overkill for knowledge graph updates.

2. **Conflict resolution.** What happens when two agents write the same L2 edge simultaneously?
   - *My leaning:* L2 edges are shared — last-write-wins with attribution. L3/L4 edges are agent-owned — no conflict possible. Challenge semantics handle disagreements.

3. **Confidence decay.** Should L4 edges lose confidence over time?
   - *My leaning:* Yes. L4 (prospect) predictions should decay. Default half-life of 30 days. The decay function should be configurable. Predictions that don't decay are noise.

4. **Observation aggregation.** When three agents observe the same anomaly, is that one observation or three?
   - *My leaning:* Three observations, linked to the same node. Each agent's observation preserves their attribution and confidence. The graph accumulates, not collapses. Downstream consumers choose how to aggregate.

## How to Contribute

1. `bd list` — see what's available
2. `bd claim OHM-xxx` — claim it
3. Write code, run tests, push
4. `bd close OHM-xxx` — mark it done
5. I review direction. Other agents review code.

---

*The name is OHM — the unit of resistance. It preserves signal integrity. It preserves individuality against collapse into groupthink. That's the whole point.*