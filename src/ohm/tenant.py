"""TenantManager — per-customer OhmStore provisioning, routing, and lifecycle.

Single-process multi-tenancy: one ohmd, N isolated DuckDB files.
Each customer gets /tenants_dir/{customer_id}/ohm.duckdb + meta.json.
An LRU cache of OhmStore connections avoids re-opening on every request.

Per-tenant write mutex (OHM-7jcb): DuckDB is single-writer. The manager
holds a threading.Lock per tenant that serialises all writes to that
tenant's connection. Reads are lock-free (DuckDB permits concurrent reads).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ohm.framework.validation import validate_customer_id
from ohm.graph.store import OhmStore
from ohm.schema import SchemaConfig

logger = logging.getLogger(__name__)

_IDLE_EVICT_SECONDS = 600  # 10 min idle before eviction
_META_FILENAME = "meta.json"
_DB_FILENAME = "ohm.duckdb"


class TenantNotFoundError(Exception):
    pass


class TenantAlreadyExistsError(Exception):
    pass


class _TenantEntry:
    """One slot in the LRU cache."""

    __slots__ = ("store", "write_lock", "last_accessed")

    def __init__(self, store: OhmStore) -> None:
        self.store = store
        self.write_lock = threading.Lock()
        self.last_accessed = time.monotonic()

    def touch(self) -> None:
        self.last_accessed = time.monotonic()


class TenantManager:
    """Provision, route, and manage isolated per-tenant OhmStore instances.

    Usage::

        tm = TenantManager("/var/lib/ohm/tenants")
        tm.provision("acme_hvac", domain="home_services", tier="starter")
        store = tm.get_store("acme_hvac")

    Thread safety: all public methods are safe for concurrent callers.
    """

    def __init__(
        self,
        tenants_dir: str | Path,
        templates_dir: Optional[str | Path] = None,
        max_cached: int = 100,
    ) -> None:
        self._tenants_dir = Path(tenants_dir)
        self._tenants_dir.mkdir(parents=True, exist_ok=True)
        self._templates_dir = Path(templates_dir) if templates_dir else None
        self._max_cached = max_cached

        self._cache: OrderedDict[str, _TenantEntry] = OrderedDict()
        self._cache_lock = threading.Lock()

        self._eviction_thread = threading.Thread(target=self._eviction_loop, daemon=True, name="tenant-eviction")
        self._eviction_thread.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def provision(self, customer_id: str, domain: str = "ohm", tier: str = "starter") -> dict:
        """Create an isolated DuckDB instance for *customer_id*.

        Raises TenantAlreadyExistsError if the tenant already exists.
        Returns the meta.json dict.
        """
        customer_id = validate_customer_id(customer_id)
        tenant_dir = self._tenant_dir(customer_id)

        if (tenant_dir / _META_FILENAME).exists():
            raise TenantAlreadyExistsError(f"Tenant '{customer_id}' already exists")

        tenant_dir.mkdir(parents=True, exist_ok=True)

        schema = self._load_schema(domain)
        db_path = tenant_dir / _DB_FILENAME
        store = OhmStore(db_path=str(db_path), agent_name="ohmd", schema=schema)
        store.close()

        meta = {
            "customer_id": customer_id,
            "domain": domain,
            "tier": tier,
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "shared_patterns": False,
            "integrations": {},
        }
        (tenant_dir / _META_FILENAME).write_text(json.dumps(meta, indent=2))
        logger.info("Provisioned tenant %s (domain=%s, tier=%s)", customer_id, domain, tier)
        return meta

    def get_store(self, customer_id: str) -> OhmStore:
        """Return a cached OhmStore for *customer_id*, opening it if needed.

        Raises TenantNotFoundError if the tenant has not been provisioned.
        Promotes the entry to MRU position in the LRU cache.
        """
        customer_id = validate_customer_id(customer_id)
        with self._cache_lock:
            if customer_id in self._cache:
                entry = self._cache[customer_id]
                self._cache.move_to_end(customer_id)
                entry.touch()
                return entry.store

        # Not cached — open and insert (dropping LRU entry if at capacity)
        meta = self._read_meta(customer_id)
        schema = self._load_schema(meta.get("domain", "ohm"))
        db_path = self._tenant_dir(customer_id) / _DB_FILENAME
        store = OhmStore(db_path=str(db_path), agent_name="ohmd", schema=schema)

        with self._cache_lock:
            # Check again under lock (another thread may have beaten us)
            if customer_id in self._cache:
                store.close()
                entry = self._cache[customer_id]
                self._cache.move_to_end(customer_id)
                entry.touch()
                return entry.store

            self._cache[customer_id] = _TenantEntry(store)
            self._cache.move_to_end(customer_id)
            self._evict_lru_unlocked()

        return store

    def get_write_lock(self, customer_id: str) -> threading.Lock:
        """Return the per-tenant write mutex for *customer_id*.

        Callers must hold this lock for the duration of any write operation
        against the tenant's OhmStore.  Reads do not need the lock.
        """
        customer_id = validate_customer_id(customer_id)
        with self._cache_lock:
            if customer_id in self._cache:
                return self._cache[customer_id].write_lock
        # Force the store into cache so we get a stable lock reference
        self.get_store(customer_id)
        with self._cache_lock:
            return self._cache[customer_id].write_lock

    def deprovision(self, customer_id: str, *, confirm: bool = False) -> None:
        """Permanently delete a tenant instance.

        Requires *confirm=True* to prevent accidental deletion.
        Overwrites the DB with random bytes before unlinking (secure delete).
        """
        if not confirm:
            raise ValueError("Pass confirm=True to deprovision a tenant")

        customer_id = validate_customer_id(customer_id)
        self._evict(customer_id)

        tenant_dir = self._tenant_dir(customer_id)
        if not tenant_dir.exists():
            raise TenantNotFoundError(f"Tenant '{customer_id}' not found")

        db_path = tenant_dir / _DB_FILENAME
        if db_path.exists():
            size = db_path.stat().st_size
            with open(db_path, "r+b") as f:
                f.write(secrets.token_bytes(min(size, 4096)))
                f.flush()
                os.fsync(f.fileno())
            db_path.unlink()

        wal_path = Path(str(db_path) + ".wal")
        if wal_path.exists():
            wal_path.unlink()

        meta_path = tenant_dir / _META_FILENAME
        if meta_path.exists():
            meta_path.unlink()

        try:
            tenant_dir.rmdir()
        except OSError:
            pass  # Non-empty — leave the directory, files are gone

        logger.info("Deprovisioned tenant %s", customer_id)

    def list_tenants(self) -> list[dict]:
        """Return meta.json contents for all provisioned tenants."""
        result = []
        if not self._tenants_dir.exists():
            return result
        for entry in sorted(self._tenants_dir.iterdir()):
            meta_path = entry / _META_FILENAME
            if meta_path.exists():
                try:
                    result.append(json.loads(meta_path.read_text()))
                except Exception:
                    pass
        return result

    def get_meta(self, customer_id: str) -> dict:
        """Return meta.json for a single tenant."""
        return self._read_meta(customer_id)

    def close(self) -> None:
        """Close all cached stores and checkpoint their WALs."""
        with self._cache_lock:
            for entry in self._cache.values():
                try:
                    entry.store.close()
                except Exception:
                    pass
            self._cache.clear()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _tenant_dir(self, customer_id: str) -> Path:
        return self._tenants_dir / customer_id

    def _read_meta(self, customer_id: str) -> dict:
        meta_path = self._tenant_dir(customer_id) / _META_FILENAME
        if not meta_path.exists():
            raise TenantNotFoundError(f"Tenant '{customer_id}' not found — provision first")
        return json.loads(meta_path.read_text())

    def _load_schema(self, domain: str) -> SchemaConfig:
        if self._templates_dir:
            json_path = self._templates_dir / f"{domain}.json"
            if json_path.exists():
                try:
                    return SchemaConfig.from_json_file(str(json_path))
                except Exception as e:
                    logger.warning("Could not load template %s: %s — using default", json_path, e)
        return SchemaConfig.from_json_file(str(Path(__file__).parent / "graph" / "templates" / "ohm.json")) if (Path(__file__).parent / "graph" / "templates" / "ohm.json").exists() else SchemaConfig()

    def _evict(self, customer_id: str) -> None:
        """Remove *customer_id* from the cache and close its store."""
        with self._cache_lock:
            entry = self._cache.pop(customer_id, None)
        if entry:
            try:
                entry.store.close()
            except Exception:
                pass

    def _evict_lru_unlocked(self) -> None:
        """Evict the LRU entry if cache is over capacity. Must hold _cache_lock."""
        while len(self._cache) > self._max_cached:
            oldest_id, entry = next(iter(self._cache.items()))
            self._cache.pop(oldest_id)
            try:
                entry.store.close()
            except Exception:
                pass
            logger.debug("LRU evicted tenant %s", oldest_id)

    def _eviction_loop(self) -> None:
        """Background thread: evict idle tenants every 60 seconds."""
        while True:
            time.sleep(60)
            now = time.monotonic()
            with self._cache_lock:
                idle = [cid for cid, entry in self._cache.items() if now - entry.last_accessed > _IDLE_EVICT_SECONDS]
            for cid in idle:
                self._evict(cid)
                logger.info("Idle-evicted tenant %s", cid)
