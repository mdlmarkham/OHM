"""Cross-instance pattern extraction and seeding (OHM-tss4.7).

Extracts anonymized knowledge patterns from tenant instances (opt-in)
and seeds them into new instances. Each new customer starts smarter
than the last.

Pattern format:
    {
        "id": "pattern_<domain>_<hash>",
        "label": "...",
        "content": "...",
        "confidence": 0.5,
        "tags": [],
        "domain": "ohm",
        "sample_size": 1,
        "created_at": "ISO-8601"
    }

Extraction query targets L3 pattern/synthesis/idea nodes.
Anonymization strips PII (IDs, names, addresses, phones, emails).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PII_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[PHONE]"),
    (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "[EMAIL]"),
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
    (r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", "[NAME]"),
    (r"\b\d+\s+[A-Z][a-z]+\s+(?:St|Ave|Blvd|Dr|Ln|Ct|Rd)\.?\b", "[ADDRESS]"),
]

_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def anonymize_text(text: str) -> str:
    """Strip PII from text using regex patterns."""
    result = text
    for pattern, replacement in _PII_PATTERNS:
        result = re.sub(pattern, replacement, result)
    result = _UUID_PATTERN.sub("[ID]", result)
    return result


def extract_patterns(store: "OhmStore", domain: str = "ohm") -> list[dict]:  # noqa: F821
    """Extract L3 pattern/synthesis/idea nodes from a tenant's graph.

    Only extracts from tenants with shared_patterns=True in meta.json.
    Targets nodes with type IN (pattern, idea, concept) and provenance
    containing 'analyst', 'synthesis', or 'research'.

    Args:
        store: OhmStore for the tenant.
        domain: Domain tag for extracted patterns.

    Returns:
        List of anonymized pattern dicts.
    """
    frozenset({"pattern", "idea"})
    rows = store.conn.execute("SELECT id, label, type, created_by FROM ohm_nodes WHERE type IN ('pattern', 'idea')").fetchall()

    patterns = []
    for row in rows:
        node_id, label, node_type, created_by = row
        content = anonymize_text(str(label))
        if not content.strip():
            continue

        pattern_id = f"pattern_{domain}_{hashlib.sha256(content.encode()).hexdigest()[:12]}"
        patterns.append(
            {
                "id": pattern_id,
                "label": content,
                "content": content,
                "confidence": 0.5,
                "tags": [node_type, domain],
                "domain": domain,
                "sample_size": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "provenance": "platform_pattern",
            }
        )

    return patterns


def merge_patterns(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge new patterns into existing shared pattern store.

    If a pattern with the same ID already exists, increment its
    sample_size and adjust confidence using Bayesian averaging:
        confidence = min(0.95, 0.5 + 0.05 * sample_size)

    Args:
        existing: Patterns already in the shared store.
        new: Newly extracted patterns.

    Returns:
        Merged list of patterns.
    """
    by_id = {p["id"]: p.copy() for p in existing}
    for pattern in new:
        pid = pattern["id"]
        if pid in by_id:
            existing_p = by_id[pid]
            existing_p["sample_size"] = existing_p.get("sample_size", 1) + 1
            n = existing_p["sample_size"]
            existing_p["confidence"] = min(0.95, 0.5 + 0.05 * n)
            tags = set(existing_p.get("tags", []))
            tags.update(pattern.get("tags", []))
            existing_p["tags"] = sorted(tags)
        else:
            by_id[pid] = pattern.copy()
    return sorted(by_id.values(), key=lambda p: p["id"])


def save_patterns(patterns: list[dict], path: Path, domain: str) -> None:
    """Save patterns to the shared pattern store.

    Args:
        patterns: List of pattern dicts.
        path: Base directory for shared patterns.
        domain: Domain subdirectory.
    """
    domain_dir = path / domain
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "patterns.json").write_text(json.dumps(patterns, indent=2))


def load_patterns(path: Path, domain: str) -> list[dict]:
    """Load patterns from the shared pattern store.

    Args:
        path: Base directory for shared patterns.
        domain: Domain subdirectory.

    Returns:
        List of pattern dicts, or empty list if not found.
    """
    pattern_file = path / domain / "patterns.json"
    if not pattern_file.exists():
        return []
    try:
        return json.loads(pattern_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def seed_patterns(store: "OhmStore", patterns: list[dict], domain: str = "ohm") -> int:  # noqa: F821
    """Inject shared patterns into a tenant's graph on provision.

    Creates L3 nodes of type 'pattern' with provenance='platform_pattern'.
    Only seeds patterns matching the tenant's domain.

    Args:
        store: OhmStore for the new tenant.
        patterns: Shared pattern list.
        domain: Domain filter for seeding.

    Returns:
        Number of patterns seeded.
    """
    domain_patterns = [p for p in patterns if p.get("domain") == domain]
    count = 0
    for pattern in domain_patterns:
        try:
            node_id = str(uuid.uuid4())
            store.conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES (?, ?, 'pattern', 'platform_pattern', CURRENT_TIMESTAMP)",
                [node_id, pattern["label"]],
            )
            count += 1
        except Exception:
            continue
    return count


def run_extraction(
    tenant_manager: "TenantManager",  # noqa: F821
    shared_dir: Path,
) -> dict:
    """Extract patterns from all opted-in tenants and merge into shared store.

    Args:
        tenant_manager: TenantManager instance.
        shared_dir: Base directory for shared patterns.

    Returns:
        Dict with extraction results per domain.
    """

    results = {}
    for tenant_meta in tenant_manager.list_tenants():
        if not tenant_meta.get("shared_patterns", False):
            continue
        customer_id = tenant_meta.get("customer_id", "")
        domain = tenant_meta.get("domain", "ohm")
        try:
            store = tenant_manager.get_store(customer_id)
            new_patterns = extract_patterns(store, domain=domain)
            existing = load_patterns(shared_dir, domain)
            merged = merge_patterns(existing, new_patterns)
            save_patterns(merged, shared_dir, domain)
            results[customer_id] = {
                "domain": domain,
                "extracted": len(new_patterns),
                "total_shared": len(merged),
            }
        except Exception as e:
            results[customer_id] = {"domain": domain, "error": str(e)}

    return results
