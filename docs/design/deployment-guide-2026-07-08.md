# Deployment guide update — 2026-07-08 (OHM-yzyk.4)

## Goal

Bring `docs/deployment/local-copilot-ohm.md` up to date with the three MCP/agent-mesh features that landed in July 2026:

1. Hosted FastMCP gateway (`OHM-yzyk.2` / ADR-028).
2. Agent profiles — one sidecar, multiple tenants (`OHM-yzyk.3` / ADR-043).
3. Instance registry and monitoring (`OHM-yzyk.5` / ADR-042).

## What changed

- Rewrote the topology section to show two supported deployment shapes:
  - **Shape A**: one `ohm-mcp` sidecar per tenant.
  - **Shape B**: one `ohm-mcp` sidecar carrying multiple profiles.
- Added a dedicated "Agent profiles" section covering:
  - When to use profiles vs separate sidecars.
  - `ohm_list_profiles` and `ohm_select_profile` semantics.
  - Local-only restriction in the FastMCP gateway.
  - Backwards compatibility with flat legacy configs.
- Added an "Instance registry and monitoring" section referencing ADR-042.
- Added a single sidecar profile reference config and VS Code examples.
- Updated the "Next steps" section to show the hosted gateway as available and point to the instance registry.
- Updated `docs/deployment/per-agent-ohm.md` growth-stage table to include the profile row and mark the gateway as available.

## Decisions

- The guide stays focused on **local agents** (Copilot, Cursor, Claude Code, OpenCode). SaaS/browser/CI flows are referenced but not duplicated here.
- The new profile shape is presented as an alternative, not a replacement. Both shapes are valid depending on whether the agent benefits from simultaneous MCP servers or a single switchable one.
- Profile tools are documented as local-only, matching the implementation: the gateway resolves profile identity per request and therefore does not expose `ohm_list_profiles` / `ohm_select_profile`.

## Status

- `docs/deployment/local-copilot-ohm.md` updated.
- `docs/deployment/per-agent-ohm.md` growth-stage table updated.
- No code changes; no live-daemon restart required.
- Bead `OHM-yzyk.4` closed.
