# OHM Agent Profiles — Design Note

**Date:** 2026-07-08
**Author:** Métis
**Scope:** Let a single local `ohm-mcp` sidecar switch between multiple OHM instances/tenants at runtime.
**Parent bead:** `OHM-yzyk.3` — Agent Profiles: multi-instance access for a single agent

## Background

Today `~/.ohm/mcp.json` describes exactly one backend: one `ohm_url`, one `token`, one `tenant_id`. A single agent (e.g., metis) often needs access to more than one OHM instance — personal knowledge graph, a devops tenant, a trading-research tenant — without launching multiple sidecars or editing the config file between tool calls.

## Goal

Add a `profiles` list to the MCP config. Each profile carries its own backend credentials and policy. Two new read-tier tools let the agent manage the active profile:

- `ohm_list_profiles` — list available profiles and show the active one.
- `ohm_select_profile` — switch the active profile by name.

After a switch, every subsequent tool call uses that profile's `ohm_url`, token, tenant, agent ID, `allowed_tools`, and `read_only` flag.

## Config schema (backwards compatible)

The existing flat keys remain valid and define a single implicit profile named `"default"`:

```json
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "ohm-...",
  "agent_id": "metis",
  "allowed_tools": ["*"],
  "read_only": false
}
```

New optional `profiles` array overrides the flat keys and allows many profiles:

```json
{
  "profiles": [
    {
      "name": "personal",
      "ohm_url": "http://127.0.0.1:8710",
      "token": "ohm-metis-...",
      "agent_id": "metis",
      "allowed_tools": ["*"],
      "read_only": false
    },
    {
      "name": "devops",
      "ohm_url": "http://127.0.0.1:8710",
      "token": "ohm-cu-...",
      "tenant_id": "devops",
      "agent_id": "metis",
      "allowed_tools": ["ohm_search", "ohm_get_node"],
      "read_only": true,
      "token_type": "customer"
    }
  ],
  "active_profile": "personal"
}
```

Fields per profile reuse the same semantics as the top-level config plus:

- `name` — unique identifier used by `ohm_select_profile`.
- `token_type` — `"agent"` (send `X-Tenant-ID`) or `"customer"` (tenant scoped by key).
- `default` — optional boolean; the first profile is the default if `active_profile` is absent.

## Tools added

### `ohm_list_profiles`

Read-tier. Returns the list of profile names and the active profile. No backend call required.

Input schema: empty object.

Output example:

```json
{
  "profiles": ["personal", "devops"],
  "active": "personal"
}
```

### `ohm_select_profile`

Read-tier (it changes sidecar state, not the daemon). Selects the active profile for subsequent tool calls.

Input schema:

```json
{
  "name": "string"
}
```

Output:

```json
{
  "active": "devops",
  "ohm_url": "http://127.0.0.1:8710",
  "tenant_id": "devops",
  "read_only": true
}
```

Errors:

```json
{"error": "profile_not_found", "available": ["personal", "devops"]}
```

## Server changes

1. **`src/ohm/mcp/config.py`**
   - Normalize config at load time: if `profiles` is missing, synthesize a single profile from flat keys named `"default"`.
   - Provide `get_profiles()`, `get_active_profile()`, `set_active_profile(name)`.
   - Move `make_headers()` to accept a profile dict rather than global `config`.
   - Keep `is_tool_allowed()` as a free function taking a profile dict.

2. **`src/ohm/mcp/server.py`**
   - `_headers()` and `_ohm_get/_ohm_post` use `get_active_profile()`.
   - `call_tool()` adds branches for `ohm_list_profiles` and `ohm_select_profile`.
   - All existing tools continue to use the active profile.

3. **`src/ohm/mcp/tools.py`**
   - Add `Tool` definitions for the two new tools to the shared registry.

4. **`src/ohm/mcp/dispatch.py`**
   - `build_request()` raises `NotImplementedError` for the local-only tools, since the gateway resolves profiles per HTTP request and does not need them.

## Read-only and allowed_tools semantics

The active profile's policy is enforced on every tool call, including `ohm_select_profile`? `ohm_select_profile` itself is harmless, so it is always allowed. The target profile's policy is what matters for subsequent calls.

## Gateway interaction

The FastMCP gateway (`src/ohm/mcp/gateway.py`) already resolves a profile per request from the `Authorization` header. It will not expose `ohm_list_profiles`/`ohm_select_profile`; those are local-only. The shared tool registry still includes them, so the gateway's `_register_tools()` should skip them along with `ohm_list_instances`.

## Backwards compatibility

- Flat config files without `profiles` keep working unchanged.
- New config files with `profiles` but no `active_profile` default to the first profile.
- The existing `OHM_URL`, `OHM_TOKEN`, etc. env vars still seed the implicit default profile.

## Tests

- `tests/test_mcp_config.py`: multi-profile loading, active selection, policy enforcement per profile.
- `tests/test_mcp_e2e.py`: provision two tenants, switch profile, assert calls hit different tenants.
- `tests/test_mcp_gateway.py`: confirm gateway tool list skips local-only profile tools.

## Commit message target

```
feat(mcp): agent profiles — multi-instance sidecar support (OHM-yzyk.3)

- Add profiles list to MCP config with backwards-compatible flat fallback.
- Add ohm_list_profiles and ohm_select_profile tools.
- Enforce allowed_tools/read_only per active profile.
- Skip profile tools in FastMCP gateway (per-request profile resolution).
```
