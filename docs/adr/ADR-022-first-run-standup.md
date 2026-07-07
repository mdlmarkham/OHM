# ADR-022: First-Run / Standup CLI for OHM

**Date:** 2026-07-07
**Status:** Proposed

## Context

OHM currently requires a new user to read and execute three documents (`docs/bootstrap.md`,
`docs/deployment.md`, `docs/deployment/local-copilot-ohm.md`) plus several manual commands
before the graph is usable. On a cold start there are no nodes, no `/orient` signal, and no
`/suggest` hits. The activation energy is too high for adoption.

There are also two overlapping but distinct needs:
1. A user with an existing `ohmd` backend wants to connect an agent or MCP sidecar.
2. A user with nothing wants to stand up a new OHM instance aligned to a purpose.

This ADR proposes a single CLI command, `ohm standup`, that detects the environment and adapts
to either case (and intermediate variants) without separate subcommands.

## Decision

Introduce `ohm standup` as a consolidated, adaptive first-run command.

It shall:
1. Detect whether an `ohmd` backend is already reachable (default probe: `http://127.0.0.1:8710/health`).
2. Detect the host OS and available service manager (`systemd`, `launchd`, Windows Services, Docker, or foreground).
3. Detect installed local agent hosts (VS Code, Cursor, Claude Code, OpenCode) by inspecting their known config locations.
4. Branch based on what it finds and what the user confirms:
   - **Connect mode**: backend exists; authenticate, list/select/provision tenants, emit MCP configs, patch agent hosts if requested.
   - **Greenfield mode**: no backend; install/init `ohmd`, create admin + agent tokens, start daemon via OS-appropriate service, provision tenant(s), seed a purpose-aligned domain template.
   - **SDK-only mode**: skip MCP entirely; write a local agent config for direct SDK usage.
5. Verify end-to-end before exiting: `/health`, `/orient`, tenant schema, `ohm-mcp tools/list`, minimum viable graph metrics.

## Consequences

- Lower activation energy for new OHM deployments.
- Fewer manual steps and fewer topology-specific setup docs.
- The CLI becomes a first-class product surface; it must be tested across OS/service/agent combinations.
- We need domain templates as machine-readable data, not only markdown.

## Alternatives Considered

- **Separate commands (`ohm standup` vs `ohm onboard`)**: Rejected. The distinction is not meaningful to users; a single adaptive command is simpler.
- **Pure documentation**: Current state. Rejected because it leaves the cold-start problem unsolved.
- **Installer script only**: Rejected. Installation is only half the problem; purpose-aligned seeding and verification are equally important.

## Related Documents

- `docs/bootstrap.md` — seeding protocol
- `docs/deployment.md` — deployment topologies
- `docs/deployment/local-copilot-ohm.md` — daemon + MCP sidecar topology
- `reseed_ohm.py` — existing seed data
- `domain-configs.md` — domain template definitions
