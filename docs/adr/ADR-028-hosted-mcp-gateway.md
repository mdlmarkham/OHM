# ADR-028: Hosted OHM MCP Gateway

**Date:** 2026-07-07
**Status:** Proposed
**Related issues:** OHM-yzyk.2, OHM-cjiw, OHM-ospu, OHM-b97p, OHM-ur0u, OHM-s139

## Context

OHM-yzyk.1 hardened the local `ohm-mcp` sidecar so agents on a user's machine can access OHM via stdio MCP. Many agents cannot run a local process at all: headless CI/CD, serverless functions, browser/mobile agents, third-party SaaS agents, Kubernetes jobs, and low-code tools. They need a hosted gateway that exposes OHM over HTTPS using standard MCP remote transports.

We researched FastMCP (`gofastmcp.com`) as a candidate framework for the gateway because it provides HTTP/SSE transports, OAuth 2.1 / CIMD helpers, server transforms (namespace, tool search, resources-as-tools), tool fingerprinting, and CLI utilities. The local `ohm-mcp` sidecar will remain on the raw `mcp` SDK stdio transport; the gateway is a separate component.

## Decision

Build `ohm-gateway`: a stateless hosted MCP gateway that routes remote agents to the correct OHM instance, injects identity, enforces guardrails, and adapts transport.

### 1. Foundation: FastMCP for the gateway

Use FastMCP as the gateway framework because:

- It implements MCP over SSE and Streamable HTTP transports.
- It provides client-auth helpers (Bearer, OAuth 2.1, CIMD) that we can adapt for server-side token validation.
- It gives us transforms we need: `Namespace` for multi-tenant composition safety, `RegexSearchTransform` for tool discovery at scale, `ResourcesAsTools` for clients without resource support.
- Tool fingerprinting helps detect schema drift between gateway deployments.

The existing local `ohm-mcp` sidecar stays on the raw `mcp` SDK. Do not migrate it to FastMCP; its `--config`, `allowed_tools`, and `read_only` logic is OHM-specific and not simplified by FastMCP.

### 2. Deployment targets (in order)

1. **Long-running container / sidecar** — primary, lowest latency, simplest operations.
2. **AWS Lambda + API Gateway** — required by OHM-yzyk.2; use Streamable HTTP transport because API Gateway's 29s timeout and request/response model do not suit SSE.
3. **`pip install ohm[gateway]` CLI** — convenience wrapper that runs the container service locally.

### 3. Authentication: token validation

Primary auth: API keys issued by the gateway.

- Each API key maps to exactly one tenant profile (see §4).
- The gateway validates the key and rejects anonymous/unknown requests before contacting OHM.
- After validation, the gateway forwards to OHM using the tenant's OHM token (`Authorization: Bearer <ohm_token>`) and injects `X-OHM-Agent: <agent_id>`.
- Key rotation via a management endpoint authenticated with a master/admin key.

Optional future: JWT validation against an external issuer (e.g., Entra, Clerk) and mTLS for enterprise.

OAuth 2.1 / CIMD are intentionally deferred: they are designed for external/untrusted clients, whereas the small-team mesh (OHM-yzyk) starts with internal agents and API keys.

### 4. Tenant profile

Each API key resolves to a profile stored in the gateway config or a small metadata store:

```json
{
  "key_hash": "sha256:<hash>",
  "ohm_url": "http://127.0.0.1:8710",
  "ohm_token": "ohm-cu...",
  "tenant_id": "devops",
  "agent_id": "ci-runner-1",
  "domain_config": "devsecops.json",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_neighborhood", "ohm_observe"],
  "read_only": false,
  "rate_limit": "100/min",
  "quota": {"writes_per_day": 1000},
  "high_blast_radius": ["ohm_delete", "ohm_mass_observe"]
}
```

A session/connection is pinned to one profile for its lifetime. No runtime tenant switching.

### 5. MCP transport

- **Primary remote transport:** MCP over SSE (`/mcp/sse`).
- **Serverless transport:** Streamable HTTP (`/mcp`). Preferred for Lambda because it is request/response and fits API Gateway's 29s limit.
- **Health endpoint:** `/health` checks gateway health and OHM backend reachability.
- **Management endpoint:** `/admin/keys` (admin-key only) for key CRUD.

### 6. Tool surface

The gateway re-exposes the tenant's OHM tools. For the small-team mesh, the initial tool count is ~16, so a full catalog is fine. As domains add tools, apply `RegexSearchTransform` to keep `list_tools` small and improve tool-selection accuracy.

Expose domain schema as:

- MCP resource: `ohm://domain-schema`
- MCP prompt: `ohm-domain-onboarding`
- Generated tools: `list_resources`, `read_resource` via `ResourcesAsTools` for clients that do not support resources.

Use `Namespace` transform only when mounting multiple tenant-specific servers inside one gateway process for internal composition; each client session still sees only its own tenant's prefixed tools.

### 7. Guardrails

Gateway-level enforcement before forwarding to OHM:

- Reject tools not in `allowed_tools`.
- Block all write-tier tools if `read_only` is true.
- Deny high-blast-radius tools unless the request carries an explicit approval claim (e.g., a signed `X-OHM-Approve: <tool>` header or a secondary approval token).
- Forward structured errors from OHM without leaking internal details (e.g., no stack traces, no token values).

This is the network-boundary implementation of `OHM-cjiw` (tool-discipline guardrails).

### 8. Observability

Log every request with:

- API key hash
- `agent_id`
- Tool name
- Request/response size
- HTTP/MCP status
- Latency
- Tenant ID

Storage: append to an `ohm_gateway_audit` table (optional DuckDB file at `OHM_GATEWAY_AUDIT_PATH`) or emit OpenTelemetry spans if `OTEL_EXPORTER_*` env vars are set.

Alert on: burst writes, denied tool attempts, repeated auth failures, backend errors.

This extends `OHM-ospu` (memory-operation observability) across the network boundary.

### 9. Backend handling

- `/health` probes `GET <ohm_url>/health` with the tenant token.
- Circuit breaker: if OHM is unreachable, fail fast with `503 Service Unavailable` and a clear error body.
- Retry idempotent reads (`ohm_search`, `ohm_get_node`) on transient errors with exponential backoff; do not retry writes.
- Paginate large responses; stream SSE chunks for long-running neighborhood/listen queries.

### 10. Packaging

Add optional dependency group `gateway` to `pyproject.toml`:

```toml
[project.optional-dependencies]
gateway = [
    "fastmcp>=3.0",
    "mcp>=1.6",
    "httpx>=0.27",
]
```

Core OHM keeps only `duckdb`, `click`, etc. The gateway is opt-in.

## Consequences

### Positive

- Remote agents can access OHM without installing anything locally.
- FastMCP reduces transport/auth/transform implementation cost.
- Tenant isolation is enforced at two layers: API key maps to one tenant; OHM customer token is tenant-scoped.
- Guardrails and audit logging make the network boundary explicit.

### Negative / Risks

- Adds a new runtime component with its own auth store and config.
- FastMCP is a moving target; auth patterns are noted as "rapidly evolving" in their docs.
- Lambda cold start + FastMCP dependency may be slow; need to measure and possibly trim the package.
- SSE transport is awkward behind API Gateway; we bias toward Streamable HTTP for Lambda.

## Alternatives considered

- **Raw `mcp` SDK only:** Rejected for the gateway because it lacks built-in HTTP/SSE/OAuth support; we'd reimplement what FastMCP provides.
- **Build gateway as an `ohmd` handler instead of a separate service:** Rejected because it couples remote-agent concerns to the core daemon and complicates multi-instance routing.
- **Use OAuth 2.1 / CIMD as primary auth:** Deferred. API keys are sufficient for the small-team mesh; OAuth/CIMD can be added later for external clients.

## Open questions

1. Should the gateway cache tenant schemas to reduce `/schema` calls, or fetch fresh each session?
2. Should high-blast-radius approval use a separate approval token or a time-limited signed URL?
3. How do we package the Lambda handler with minimal cold-start size? (Zipping deps, Docker image, or Lambda layer?)
4. Should the gateway support direct `OhmStore` paths in addition to `ohmd` HTTP URLs, or only HTTP?

## Acceptance criteria (from OHM-yzyk.2)

1. Remote agent calls `ohm_search` through the gateway with only an API key and HTTPS.
2. Gateway routes to the correct OHM instance and attributes writes to `agent_id`.
3. Gateway rejects invalid API keys before contacting OHM.
4. Gateway denies tools not in `allowed_tools`.
5. Domain schema available as MCP resource and prompt.
6. Audit log captures key hash, agent_id, tool, latency, outcome.
7. Container image and Lambda packaging both build and run.
8. End-to-end CI test: GitHub Actions connects to gateway and writes an observation.
9. ADR documents architecture, tenancy, and security boundaries. (This ADR.)
