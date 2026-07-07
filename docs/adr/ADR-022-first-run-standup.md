# ADR-022: First-Run / Standup CLI for OHM

**Date:** 2026-07-07
**Status:** Implemented (MVP)

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

### Domain seed templates

Purpose-aligned seeding is implemented as JSON templates under `src/ohm/templates/seeds/`.
Each template defines:
- `domain_schema`: the tenant schema template to use (e.g., `ohm`, `devsecops`).
- `agents`, `values`, `capabilities`, `concepts`, `sources`: initial node populations.
- `edges`: cross-links that ensure `/suggest` and `/orient` return signal immediately.

Initial templates:
- `personal-knowledge` — external cognition, Zettelkasten, AND→OR, context gates.
- `devsecops` — agent authorization gap, CI/CD AND-gate, incident response.
- `trading-research` — source reliability, evaluation trap, autocatalytic systems.
- `data-pipelines` — lineage, schema drift, pipeline observability.

The loader module `ohm.templates` provides `list_templates()`, `load_template(name)`,
and `seed_payload(name)` for use by the standup CLI and tests.

## Consequences

- Lower activation energy for new OHM deployments.
- Fewer manual steps and fewer topology-specific setup docs.
- The CLI becomes a first-class product surface; it must be tested across OS/service/agent combinations.
- We need domain templates as machine-readable data, not only markdown.

## Alternatives Considered

- **Separate commands (`ohm standup` vs `ohm onboard`)**: Rejected. The distinction is not meaningful to users; a single adaptive command is simpler.
- **Pure documentation**: Current state. Rejected because it leaves the cold-start problem unsolved.
- **Installer script only**: Rejected. Installation is only half the problem; purpose-aligned seeding and verification are equally important.

## Implementation Status

- CLI entry point: `src/ohm/cli/standup.py` registered as `ohm standup`.
- Auto-detection: OS, service manager, existing `ohmd`, local agent hosts.
- Connect mode: probes `/health`, lists/provisions tenant, emits MCP config, patches
  detected agent hosts, verifies `tools/list`.
- Greenfield mode: writes default config with an `admin` agent, starts `ohmd` via
  selected service adapter, waits for health, provisions tenant, seeds from domain
  template, generates agent tokens, emits MCP config, starts sidecar, verifies.
- Service adapters: systemd unit install/enablement; launchd plist install/load;
  Windows `New-Service` wrapper; foreground fallback.
- Seed templates: `src/ohm/templates/seeds/` + loader + tests.
- Tests: `tests/test_templates.py` (11), `tests/test_standup.py` (10),
  `tests/test_cli.py` still passes (133 total in combined run).
- End-to-end greenfield test: succeeded on port 18710 with temp config/DB,
  producing an orient response with 11 nodes and 12 edges.

## Known Gaps

- Windows service adapter is best-effort (`New-Service`); production may prefer nssm.
- Agent host patching for Cursor / Claude Code / OpenCode paths are best-effort
  and may need adjustment as those products evolve.
- Remote HTTPS/Caddy deployments need `--url` and token handling tested.
- MCP sidecar in greenfield foreground mode returns 0 tools in the test; may be a
  config/env issue in the sidecar rather than the standup script.

## Related Documents

- `docs/bootstrap.md` — seeding protocol
- `docs/deployment.md` — deployment topologies
- `docs/deployment/local-copilot-ohm.md` — daemon + MCP sidecar topology
- `src/ohm/templates/` — domain seed templates and loader
- `reseed_ohm.py` — existing seed data
- `domain-configs.md` — domain schema definitions

## Implementation Notes

- Spike: `scripts/spikes/ohm_standup_spike.py` — connect-to-existing-daemon path.
- Spike: `scripts/spikes/ohm_seed_template_spike.py` — greenfield seeding path.
- Tests: `tests/test_templates.py` — template loading and validation.
- Current local deployments use agent tokens without an explicit admin role, so
  tenant provisioning requires either `ohmd --init` in greenfield mode or a
  pre-existing customer API key in connect mode.
