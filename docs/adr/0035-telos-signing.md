# ADR-035: TELOS Signing — Cryptographic Audit Trail for Agent Writes

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-enwb (this ADR), ADR-003 (agent-owned edges), ADR-017 (encryption at rest), ADR-028 (source tier provenance)

## Context

OHM's `created_by` column attributes writes to agents, but attribution is a plain string — any agent can set `created_by="metis"` and the graph has no way to detect forgery. ADR-003's agent-owned edge model depends on `created_by` for boundary enforcement; if `created_by` is spoofed, the boundary is meaningless. ADR-017's encryption at rest protects data confidentiality but not integrity — a compromised file can be silently modified without detection.

The graph needs a cryptographic audit trail: a signature over each write that can be verified later to prove the write came from a holder of a specific key. This is not access control (ADR-003 handles that) — it is tamper evidence. If a node's content changes after signing, verification fails.

Constraints:
- Must work with stdlib only (no hard dependency on third-party crypto libraries)
- Must not break existing write paths — unsigned writes remain valid
- Must be deterministic — same record + same key = same signature (reproducible verification)
- Must cover both nodes and edges

## Decision

### 1. Three new columns on `ohm_nodes` and `ohm_edges`

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `write_signature` | VARCHAR | Yes | NULL | Algorithm-prefixed hex signature (e.g., `hmac-sha256:abcdef...`) |
| `signing_key_id` | VARCHAR | Yes | NULL | Logical key identifier (e.g., `"metis-v1"`, `"default"`) |
| `signed_at` | TIMESTAMP | Yes | NULL | ISO-8601 timestamp of when the signature was applied |

All three are nullable with NULL defaults — existing rows and unsigned writes are unaffected.

```sql
ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS write_signature VARCHAR;
ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS signing_key_id VARCHAR;
ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP;
ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS write_signature VARCHAR;
ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS signing_key_id VARCHAR;
ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP;
```

### 2. Schema version

`SCHEMA_VERSION = "0.33.0"` — idempotent ALTER TABLE migration in `src/ohm/graph/schema.py:1462`. Partial indexes on `signing_key_id` for efficient key-based lookups:

```sql
CREATE INDEX IF NOT EXISTS idx_ohm_nodes_signing_key_id
    ON ohm_nodes(signing_key_id) WHERE signing_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ohm_edges_signing_key_id
    ON ohm_edges(signing_key_id) WHERE signing_key_id IS NOT NULL;
```

### 3. Canonical payload — sorted-key JSON of whitelisted fields

`canonical_payload(record, kind="node"|"edge")` in `src/ohm/graph/crypto.py:22` produces a deterministic byte representation:

```python
NODE_FIELDS = ("id", "label", "type", "content", "created_by",
               "confidence", "visibility", "provenance", "source_tier")
EDGE_FIELDS = ("id", "from_node", "to_node", "layer", "edge_type",
               "created_by", "confidence", "probability", "source_tier")
```

- Only whitelisted fields are included — extra fields are ignored (prevents signature breakage from schema evolution)
- NULL values are excluded from the payload
- `connects_to` on nodes is included (sorted) when present and non-empty
- JSON serialization: `sort_keys=True, separators=(",", ":")` — no whitespace, key-ordered

This ensures: (a) same logical content always produces the same bytes, and (b) adding new columns to the table does not invalidate existing signatures unless those columns are added to the whitelist.

### 4. HMAC-SHA256 default algorithm (stdlib only)

`sign_hmac(payload, key)` in `src/ohm/graph/crypto.py:35` uses `hmac.new(key, payload, hashlib.sha256).hexdigest()`. Verification uses `hmac.compare_digest` for constant-time comparison.

Signature format: `hmac-sha256:<hex_digest>`

No third-party dependencies. Works in any Python 3.12+ environment.

### 5. Ed25519 optional algorithm (pynacl)

`sign_write(..., algorithm="ed25519")` in `src/ohm/graph/crypto.py:60` uses `nacl.signing.SigningKey` for signing and `nacl.signing.VerifyKey` for verification. Raises `ImportError` with install instructions if `pynacl` is not installed.

Signature format: `ed25519:<hex_signature>`

Ed25519 provides non-repudiation (public-key signature) — any party with the public key can verify, not just key holders. Useful for multi-agent verification where HMAC keys cannot be shared.

### 6. NULL defaults = unsigned writes valid (flag, not reject)

Unsigned writes (all three columns NULL) are valid. Verification of an unsigned record returns `{"verified": False, "reason": "unsigned"}` — it does not raise an error. This is a **flag model**, not a **reject model**:

- Phase 1 (current): signing is opt-in. Agents sign when they have a key; unsigned writes pass through.
- Phase 2 (future): `require_signatures` flag in `ohm_agent_config` can enforce signing per agent.
- Phase 3 (future): unsigned writes rejected at the HTTP boundary for agents with the flag set.

This graduated enforcement follows ADR-006 (advisory → lenient → strict).

### 7. Query functions in `src/ohm/graph/queries/__init__.py`

| Function | Line | Purpose |
|----------|------|---------|
| `sign_node_write(conn, node_id, *, key, algorithm, key_id)` | :5202 | Fetch node, compute signature, UPDATE columns |
| `sign_edge_write(conn, edge_id, *, key, algorithm, key_id)` | :5230 | Fetch edge, compute signature, UPDATE columns |
| `verify_node_write(conn, node_id, *, key)` | :5258 | Fetch node + signature, verify, return `{"verified": bool}` |
| `verify_edge_write(conn, edge_id, *, key)` | :5279 | Fetch edge + signature, verify, return `{"verified": bool}` |

All four use `validate_identifier` from `src/ohm/validation.py` for SQL injection prevention.

### 8. SDK integration in `src/ohm/framework/sdk.py`

`Graph` class (line 40) has `_signing_key: bytes | None = None` property. Four methods:

| Method | Line | Behavior |
|--------|------|----------|
| `sign_node(node_id, *, key, algorithm, key_id)` | :830 | Signs node; uses `_signing_key` if no explicit key |
| `sign_edge(edge_id, *, key, algorithm, key_id)` | :838 | Signs edge; uses `_signing_key` if no explicit key |
| `verify_node(node_id, *, key)` | :846 | Verifies node signature |
| `verify_edge(edge_id, *, key)` | :854 | Verifies edge signature |

All raise `ValueError("No signing key available")` when neither explicit key nor `_signing_key` is set.

## Mapping to existing concepts

| Existing concept | TELOS signing mapping |
|------------------|----------------------|
| `created_by` (ADR-003) | Complements — `created_by` is attribution (string), `write_signature` is proof (crypto). Boundary enforcement uses `created_by`; signing proves `created_by` is authentic. |
| `source_tier` (ADR-028) | Orthogonal — tier is quality ceiling; signature is integrity proof. A `verified` tier claim with a valid signature has both quality and provenance. |
| Encryption at rest (ADR-017) | Complements — encryption protects confidentiality; signing protects integrity. A compromised encrypted file can be modified; signing detects modification. |
| Advisory schema (ADR-006) | Follows graduated enforcement — Phase 1 is advisory (signing opt-in), Phase 2 adds per-agent flag, Phase 3 enforces at boundary. |
| `ohm_change_feed` | Future: signed change feed entries enable tamper-evident audit logs (deferred). |

## Consequences

**Positive:**
- Tamper evidence — any post-signature modification to whitelisted fields invalidates the signature
- Non-repudiation with Ed25519 — public-key verification without sharing secret keys
- Zero-dependency default — HMAC-SHA256 uses only stdlib (`hmac`, `hashlib`)
- Backward compatible — NULL defaults mean existing writes and callers are unaffected
- Deterministic — same record + same key always produces the same signature
- Partial indexes keep key-based lookups efficient without penalizing unsigned rows

**Negative:**
- Signing is post-hoc, not write-time — `create_node`/`create_edge` do not sign automatically. A window exists between creation and signing where the record is unsigned. Mitigated by Phase 2 `require_signatures` flag.
- Canonical payload whitelist must be maintained — adding a column to `ohm_nodes` does not automatically include it in signatures. If a security-relevant column is added later (e.g., `data_origin` from ADR-033), it must be added to `NODE_FIELDS` manually, which invalidates existing signatures.
- HMAC key management is out of scope — agents must store keys securely. Compromised HMAC key = undetectable forgery. Ed25519 mitigates this (private key stays with signer, public key shared for verification).
- `signed_at` is set by the signer, not the server — a malicious signer can backdate. Server-side timestamping is deferred.

## Alternatives considered

- **Sign at write-time (auto-sign in `create_node`/`create_edge`)** — rejected: would require every caller to provide a signing key, breaking backward compatibility. Post-hoc signing with opt-in is the correct Phase 1 approach.
- **Merkle tree / hash chain for batch integrity** — rejected: OHM's graph is not append-only (soft deletes, updates). A Merkle tree over mutable records requires re-computation on every mutation. Per-record signatures are simpler and sufficient.
- **SHA-256 hash only (no key)** — rejected: a bare hash detects accidental corruption but not forgery — anyone who can modify the record can recompute the hash. HMAC or Ed25519 requires key possession, making forgery detectable.

## References

- ADR-003 — Agent-Owned Edges with Challenge Semantics (`created_by` boundary enforcement)
- ADR-006 — Advisory Schema with Graduated Enforcement (signing follows advisory → lenient → strict)
- ADR-017 — Encryption at Rest (complementary confidentiality; signing adds integrity)
- ADR-028 — Source Tier Architecture (orthogonal quality dimension)
- OHM-enwb — TELOS signing issue
