# ADR-017: Encryption at Rest for Tenant DuckDB Files

**Status:** Accepted
**Created:** 2026-05-25
**Owner:** Matt Markham

---

## Context

OHM's multi-tenant architecture stores each customer's knowledge graph in an isolated DuckDB file at `/var/lib/ohm/tenants/{customer_id}/ohm.duckdb`. The healthcare domain template (OHM-tss4.13) implies PHI, and the Teams HIPAA mode (OHM-tss4.18) is meaningless if the underlying data layer is unencrypted. Financial and PII data across all verticals requires encryption at rest to be commercially viable.

**Constraints discovered during design:**
- DuckDB Community Edition has **no native encryption at rest** — confirmed via DuckDB documentation and ADR-016 notes
- DuckDB Pro/Enterprise has encryption but requires licensing and is not available in all deployment environments
- OHM's architecture requires **per-tenant isolation** — a single DEK per tenant is insufficient for blast-radius containment if the DEK is shared

**Goals:**
1. Protect tenant data at rest from physical media compromise
2. Maintain per-tenant isolation guarantees (no cross-tenant key sharing)
3. Support airgapped environments without external key management services
4. Meet HIPAA/PII requirements for healthcare and financial verticals

---

## Decision

Adopt a **layered encryption strategy** with two independent mechanisms:

### Layer 1: Platform-Level Volume Encryption (Required for HIPAA/PII)

Deploy tenant volumes with LUKS/dm-crypt at the OS level. This is a platform deployment requirement, not OHM application code.

```
/var/lib/ohm/tenants/{customer_id}/  →  Encrypted volume (LUKS/dm-crypt)
```

**Why not application-level encryption:**
- DuckDB files are memory-mapped — encrypting individual columns would require significant OHM code changes and performance overhead
- Volume-level encryption is handled by the OS with hardware acceleration (AES-NI)
- Compliance auditors recognize LUKS/dm-crypt as equivalent to "encrypted at rest" for HIPAA purposes

**Implementation:**
- Document LUKS/dm-crypt as a **deployment requirement** for all tenant volumes
- Use a per-volume DM key with key derivation from a platform KEK (Key Encryption Key)
- Platform KEK stored in HSM or secure key management service (AWS KMS, Azure Key Vault, etc.)
- DEK rotation: automatic on volume remount or manual via operator command

**Trade-offs:**
- Requires OS-level access controls — OHM cannot enforce this in code
- Not portable across platforms without matching volume encryption setup

### Layer 2: Application-Level Field Encryption (Supplementary for PHI)

For sensitive node content (e.g., patient names, SSNs in healthcare domain), OHM provides **field-level encryption** via an optional encrypt flag on node content.

```python
store.write_node(
    "patient-001",
    "John Smith",  # encrypted if sensitive_content=True
    "patient",
    sensitive_content=True,
    encryption_key=customer_dek
)
```

**Implementation:**
- Add `sensitive_content: bool` and `content_encryption_key_id: str` fields to `ohm_nodes` schema
- Content encrypted using AES-256-GCM before storage
- Encryption key derived from tenant's per-tenant DEK
- Decryption happens at read time — plain text never written to disk unencrypted

**Trade-offs:**
- Adds latency: encryption/decryption on every sensitive node read/write
- Requires key management infrastructure (OHM does not implement key storage)

---

## Alternatives Considered

### Option 1: DuckDB Pro with built-in encryption

DuckDB Pro provides `PRAGMA set_key` for database encryption. This was rejected because:
- Requires DuckDB Pro license (not in OHM's current commercial model)
- Single key for entire DB — no per-tenant isolation
- Not available in airgapped environments without license server access

### Option 2: SQL-level column encryption via custom functions

Implement encryption/decryption in Python and store as encrypted BLOBs. Rejected because:
- OHM's schema does not anticipate encrypted columns — would require DDL migration
- Query capability lost on encrypted fields (can't search encrypted content)
- Performance overhead unacceptable for real-time workloads

### Option 3: Application-level full-database encryption

Encrypt entire DuckDB file with a per-tenant key using a library like `cryptography.Fernet`. Rejected because:
- DuckDB does memory-mapping — encrypted pages must be decrypted in memory anyway
- Cannot use DuckDB's native features (VSS, indexes) on encrypted data without decryption layer
- Complexity and maintenance burden too high

---

## Security Considerations

### Key Hierarchy

```
Platform KEK (HSM/KMS)
  └── Per-tenant DEK (derived, stored encrypted in tenant meta.json)
        └── Per-field KEK (derived for sensitive content only)
```

### Key Rotation

- Platform KEK: Annual rotation with HSM-managed lifecycle
- Per-tenant DEK: Rotation on tenant deprovision or explicit operator command
- Per-field KEK: Derived from DEK, no separate rotation needed

### Key Storage

- Platform KEK: HSM or cloud KMS — never in code or config files
- Per-tenant DEK: Stored encrypted in tenant's `meta.json`, decrypted in memory at startup
- Field encryption keys: Derived on-demand, never stored

### Blast Radius Containment

If a single tenant's DEK is compromised, only that tenant's data is exposed. Other tenants' DEKs are independent — derived from different salts under the same platform KEK.

---

## Consequences

### What this ADR enables:
- Healthcare domain (OHM-tss4.13) can be marketed as HIPAA-ready once LUKS is deployed
- Financial/PII tenants have a path to compliance
- OHM does not implement key management (delegated to platform)

### What this ADR does NOT enable:
- This ADR does not implement LUKS setup or volume management — that's platform/deployment
- This ADR does not implement key management service integration — that's future work (OHM-yl1f follow-on)
- This ADR does not make current deployments HIPAA-compliant — only new deployments with proper volume encryption

### Implementation Notes

The following code changes are needed (tracked separately):

1. **Schema migration**: Add `sensitive_content` and `content_encryption_key_id` columns to `ohm_nodes`
2. **Store integration**: `write_node()` accepts `sensitive_content` flag and encrypts content if set
3. **Read integration**: `get_node()` decrypts content if `content_encryption_key_id` is set
4. **Key derivation**: Add `derive_field_key(tenant_dek, content_salt)` utility
5. **CLI commands**: `ohm tenant encrypt-volume` (informational — volume encryption is platform-side)

---

## References

- OHM-yl1f: Encryption at rest for tenant DuckDB files (HIPAA/PII requirement)
- OHM-tss4.13: Healthcare Domain Template
- OHM-tss4.18: Teams Channel (HIPAA mode)
- ADR-015: Multi-Tenancy Architecture
- ADR-016: Quack Production Readiness (notes DuckDB CE has no native encryption)
- HIPAA Security Rule: 45 CFR 164.312(a)(1) — access control (encryption is addressable)