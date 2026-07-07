# ADR-029: Optional TOON Transport Encoding for OHM MCP

## Status

Proposed

## Context

OHM exposes its knowledge graph through an MCP server (`ohm-mcp`) and, in the future, through a hosted `ohm-gateway`. The MCP protocol itself is JSON-based, but the **text payloads** inside MCP tool results — lists of nodes, edges, observations, confidence audits — are verbose. For agents with limited context windows or token budgets, this verbosity is a real cost.

[TOON](https://github.com/toon-format/toon) (Token-Oriented Object Notation) is a line-oriented, indentation-based encoding of the JSON data model that is optimized for LLM prompts. It declares array shapes once and uses compact tabular rows for uniform arrays of objects, often using ~30–40% fewer tokens than JSON for the same data.

We want to offer TOON as an **optional, opt-in response encoding** for MCP read results without changing OHM's HTTP API, storage layer, or default behavior.

## Decision

Add an optional TOON encoder/decoder to the MCP layer:

1. **Default stays JSON.** All existing MCP tools continue to return pretty-printed JSON unless the agent explicitly requests TOON.
2. **Opt-in per request.** Agents request TOON via a `format: "toon"` tool argument. We also support `Accept: text/toon` for HTTP-based transports, but the primary MCP path is the tool argument because MCP stdio does not expose HTTP headers cleanly.
3. **Scope: read-heavy tools first.** The following tools gain a `format` option: `ohm_stats`, `ohm_search`, `ohm_get_node`, `ohm_neighborhood`, `ohm_listen`, `ohm_confidence`, `ohm_path`, `ohm_agents`, `ohm_list_nodes`, `ohm_domain_onboarding`.
4. **TOON is optional dependency.** The MCP server imports `toon` lazily. If unavailable, TOON requests gracefully fall back to JSON.
5. **Internal API unchanged.** The `ohm-mcp` sidecar still talks to `ohmd` via JSON HTTP. Translation to TOON happens only when building the MCP `TextContent` response.
6. **Future gateway parity.** The same `ohm.mcp.encoding` module will be reused by `ohm-gateway` so hosted remote agents can also request TOON.

## Consequences

### Positive

- Reduced token consumption for agents reading large OHM result sets.
- No breaking change to existing MCP clients.
- Minimal internal surface area: one small translation module and a `format` parameter.
- Keeps OHM HTTP API and storage layer simple and standard.

### Negative / Risks

- Agents must be able to parse TOON. We rely on the model's ability to read TOON natively or on a client-side decoder.
- TOON is less efficient for deeply nested or non-uniform structures; in those cases JSON may still be smaller.
- Adds one external dependency (`python-toon`) as an optional extra.

## Implementation

- `src/ohm/mcp/encoding.py`: encode/decode helpers, MIME negotiation, fallback behavior.
- `src/ohm/mcp/server.py`: consume `format` argument, pass format to `_text()`, add `format` to read-heavy tool schemas.
- `tests/test_mcp_toon.py`: regression tests for negotiation, round-trip, fallback.
- `pyproject.toml`: add `[toon]` optional extra mapping to `python-toon`.

## Related

- ADR-022 First-Run / Standup CLI for OHM
- ADR-028 Hosted OHM MCP Gateway
- `OHM-yzyk.2` hosted MCP gateway
