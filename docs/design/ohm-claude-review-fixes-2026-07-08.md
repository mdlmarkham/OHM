# Claude review fixes — 2026-07-08

## Source

Independent reviewer (Claude) delivered four high-severity and three medium findings against the current OHM `main` after the MCP/agent-mesh batch landed.

## High-severity fixes

### 1. Read-scope enforcement inconsistent across read endpoints

**Findings**

- `GET /edges` only filtered `source_tier` on `from_node`, allowing edges pointing at restricted `to_node` values to leak.
- `GET /edge/<id>` called `enforce_read_scope(..., source_tier=edge.get("source_tier"))`, but `ohm_edges` has no `source_tier` column — the check was a permanent no-op.
- `GET /neighborhood/<id>` only checked the root node; every traversed neighbor and edge was returned unfiltered.

**Fix**

- Added `apply_read_scope_edge_filters()` to `src/ohm/server/boundary.py`: joins `ohm_nodes` for both endpoints and applies source_tier / created_by / node_id scope to each endpoint, plus layer / created_by scope to the edge itself.
- Replaced `_get_edges` scope logic with `apply_read_scope_edge_filters()`.
- Added `enforce_read_scope_for_edge()` and `filter_edges_by_read_scope()` helpers.
- `_get_edge` now enforces scope on both endpoint nodes.
- `_get_neighborhood` now post-filters nodes and edges by read scope (and drops edges whose endpoints were filtered out).
- `_get_deep` now enforces scope on the root node and filters connected edges.

### 2. SDK leaked `X-Tenant-ID` for customer-scoped tokens

**Finding**

`HttpGraph._http_request` sent `X-Tenant-ID` whenever `tenant_id` was set, regardless of token type. This contradicts ADR-043, which requires customer-scoped keys to omit the header in transit.

**Fix**

- Added optional `token_type` parameter to `connect_http()` and `HttpGraph`.
- When `token_type` is omitted, infer it from the token prefix (`ohm-cu…` → customer).
- Only send `X-Tenant-ID` when `token_type != "customer"`.

### 3. Instance monitoring gauges read from non-existent table

**Finding**

`GET /instance` and `GET /metrics` queried `ohm_sync_state`, which nothing writes to. The bare `except: pass` blocks meant `/instance` always reported `lag_seconds: null` and `/metrics` silently omitted the gauges.

**Fix**

- The daemon's sync heartbeat writes to `ohm_agent_state.last_sync` (via `OhmStore.sync_heartbeat`).
- Updated both endpoints to query `SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?` using the store's `agent_name` (default `ohmd`).
- Replaced silent `pass` with an explicit error note when sync state cannot be read.

### 4. `ohm profile show` printed tokens in plaintext

**Fix**

- Added `_mask_token()` helper (same convention used in `standup.py`: first 8 + "..." + last 4 chars).
- `_handle_profile` now masks the `token` field before printing.
- Added `.ohm/profiles.json` and `.ohm/active_profile` to `.gitignore`.

## Medium findings (acknowledged, partial fixes)

- **Profile catalog has no access control.** Single-user local deployments are the intended target; multi-user hosts should set `chmod 600` on the catalog. Added note to `.gitignore` and will revisit if the catalog is ever shared.
- **`tests/conftest.py` class-state fixture reset bypassed in two integration tests.** Not addressed in this batch; tracked as latent test-isolation risk.

## Tests added / extended

- `tests/test_read_scope_leakage.py`: new tests for edge list, single-edge, and neighborhood endpoint filtering.
- `tests/test_sdk_tenant_header.py`: new unit tests for customer vs agent token header behavior.
- `tests/test_profiles.py`: new tests for `ohm profile show` token masking and `_mask_token()`.

## Verification

- `mypy src/ohm/server --ignore-missing-imports`: pass
- `mypy src/ --ignore-missing-imports`: pass
- `tests/test_read_scope_leakage.py`: 19 passed
- `tests/test_sdk_tenant_header.py`: 4 passed
- `tests/test_profiles.py`: 19 passed
- `tests/test_instance_registry.py`: 6 passed
- Combined targeted suite: 74 passed
- MCP integration suites: 11 passed
