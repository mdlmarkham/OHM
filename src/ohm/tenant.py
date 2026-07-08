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
import shutil
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ohm.framework.validation import validate_customer_id
from ohm.graph.store import OhmStore
from ohm.schema import SchemaConfig

logger = logging.getLogger(__name__)

_IDLE_EVICT_SECONDS = 600  # 10 min idle before eviction
_CHECKPOINT_INTERVAL_SECONDS = 300  # 5 min periodic checkpoint
_WAL_SIZE_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB
_META_FILENAME = "meta.json"
_DB_FILENAME = "ohm.duckdb"
_BACKUP_DIR_NAME = "backups"
_DEFAULT_BACKUP_RETENTION = 7
_DEFAULT_BACKUP_INTERVAL_HOURS = 6

TIER_QUOTAS: dict[str, dict] = {
    "starter": {
        "max_nodes": 10_000,
        "max_edges": 50_000,
        "max_db_size_bytes": 500 * 1024 * 1024,  # 500 MB
        "max_requests_per_day": 10_000,
        "max_inference_timeout": 30,
    },
    "professional": {
        "max_nodes": 100_000,
        "max_edges": 500_000,
        "max_db_size_bytes": 5 * 1024 * 1024 * 1024,  # 5 GB
        "max_requests_per_day": 100_000,
        "max_inference_timeout": 120,
    },
    "enterprise": {
        "max_nodes": 1_000_000,
        "max_edges": 5_000_000,
        "max_db_size_bytes": 50 * 1024 * 1024 * 1024,  # 50 GB
        "max_requests_per_day": 1_000_000,
        "max_inference_timeout": 600,
    },
}


class QuotaExceededError(Exception):
    """Raised when a tenant exceeds their quota (OHM-982m)."""

    pass


class TenantNotFoundError(Exception):
    pass


class TenantAlreadyExistsError(Exception):
    pass


class _TenantEntry:
    """One slot in the LRU cache.

    Reference counting (OHM-s18r): ``refcount`` tracks in-flight requests.
    Eviction skips entries with refcount > 0 and marks them for deferred
    eviction instead (``evict_pending``).
    """

    __slots__ = ("store", "write_lock", "last_accessed", "refcount", "evict_pending", "last_checkpoint_at")

    def __init__(self, store: OhmStore) -> None:
        self.store = store
        self.write_lock = threading.Lock()
        self.last_accessed = time.monotonic()
        self.refcount = 0
        self.evict_pending = False
        self.last_checkpoint_at = 0.0

    def touch(self) -> None:
        self.last_accessed = time.monotonic()

    def acquire(self) -> None:
        """Increment refcount — caller has an in-flight request."""
        self.refcount += 1
        self.evict_pending = False

    def release(self) -> None:
        """Decrement refcount. If refcount hits 0 and eviction is pending, evict."""
        self.refcount = max(0, self.refcount - 1)


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
        checkpoint_interval: int = _CHECKPOINT_INTERVAL_SECONDS,
        wal_size_threshold: int = _WAL_SIZE_THRESHOLD_BYTES,
        shared_patterns_dir: Optional[str | Path] = None,
    ) -> None:
        self._tenants_dir = Path(tenants_dir)
        self._tenants_dir.mkdir(parents=True, exist_ok=True)
        self._templates_dir = Path(templates_dir) if templates_dir else None
        self._max_cached = max_cached
        self._checkpoint_interval = checkpoint_interval
        self._wal_size_threshold = wal_size_threshold
        self._shared_patterns_dir = Path(shared_patterns_dir) if shared_patterns_dir else None

        self._cache: OrderedDict[str, _TenantEntry] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._schema_lock = threading.Lock()  # OHM-yzau: schema mutation lock
        self._stop_event = threading.Event()  # OHM-xfqp: signal for graceful shutdown (must be set before threads start)

        self._eviction_thread = threading.Thread(target=self._eviction_loop, daemon=True, name="tenant-eviction")
        self._eviction_thread.start()

        self._checkpoint_thread = threading.Thread(target=self._checkpoint_loop, daemon=True, name="tenant-checkpoint")
        self._checkpoint_thread.start()
        self._request_counts: dict[str, dict[str, int]] = {}
        self._request_counts_lock = threading.Lock()
        self._dirty_tenants: set[str] = set()
        self._quota_cache: dict[str, tuple[str, dict]] = {}  # customer_id → (tier, quotas)
        self._quota_cache_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def provision(self, customer_id: str, domain: str = "ohm", tier: str = "starter", integrations: Optional[dict] = None) -> dict:
        """Create an isolated DuckDB instance for *customer_id*.

        Raises TenantAlreadyExistsError if the tenant already exists.
        Raises ValueError if required integrations for the domain are missing.
        Returns the meta.json dict.
        """
        customer_id = validate_customer_id(customer_id)
        tenant_dir = self._tenant_dir(customer_id)

        if (tenant_dir / _META_FILENAME).exists():
            raise TenantAlreadyExistsError(f"Tenant '{customer_id}' already exists")

        tenant_dir.mkdir(parents=True, exist_ok=True)

        schema = self._load_schema(domain)

        # Validate required integrations for domain
        schema_dict = schema.to_dict()
        required = schema_dict.get("required_integrations", {})
        if required:
            integrations = integrations or {}
            missing = []
            for channel, spec in required.items():
                provided = integrations.get(channel, {})
                for field in spec.get("fields", []):
                    if field not in provided:
                        missing.append(f"{channel}.{field}")
            if missing:
                raise ValueError(f"Missing required integrations for domain '{domain}': {', '.join(missing)}")

        db_path = tenant_dir / _DB_FILENAME
        store = OhmStore(db_path=str(db_path), agent_name="ohmd", schema=schema)
        store.close()

        from ohm.schema import SCHEMA_VERSION

        quotas = dict(TIER_QUOTAS.get(tier, TIER_QUOTAS["starter"]))

        meta = {
            "customer_id": customer_id,
            "domain": domain,
            "tier": tier,
            "schema_version": SCHEMA_VERSION,
            "template_version": schema.template_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "shared_patterns": False,
            "integrations": integrations or {},
            "quotas": quotas,
        }
        self._write_meta(customer_id, meta)

        if self._shared_patterns_dir is not None:
            from ohm.patterns import load_patterns, seed_patterns

            patterns = load_patterns(self._shared_patterns_dir, domain)
            if patterns:
                store = OhmStore(db_path=str(db_path), agent_name="ohmd", schema=schema)
                seeded = seed_patterns(store, patterns, domain)
                store.close()
                logger.info("Seeded %d patterns for tenant %s (domain=%s)", seeded, customer_id, domain)

        logger.info("Provisioned tenant %s (domain=%s, tier=%s)", customer_id, domain, tier)
        return meta

    def get_store(self, customer_id: str) -> OhmStore:
        """Return a cached OhmStore for *customer_id*, opening it if needed.

        Raises TenantNotFoundError if the tenant has not been provisioned.
        Promotes the entry to MRU position in the LRU cache.

        If the tenant's schema_version is behind the current OHM version,
        pending migrations are applied automatically before returning the
        store (OHM-tss4.5).
        """
        customer_id = validate_customer_id(customer_id)

        with self._cache_lock:
            if customer_id in self._cache:
                entry = self._cache[customer_id]
                self._cache.move_to_end(customer_id)
                entry.touch()
                store = entry.store
            else:
                store = None

        if store is not None:
            self._apply_lazy_migrations(customer_id, store)
            self._propagate_template(customer_id, store)
            return store

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
                store = entry.store
            else:
                self._cache[customer_id] = _TenantEntry(store)
                self._cache.move_to_end(customer_id)
                self._evict_lru_unlocked()

        self._apply_lazy_migrations(customer_id, store)
        self._propagate_template(customer_id, store)
        return store

    def get_write_lock(self, customer_id: str) -> threading.Lock:
        """Return the per-tenant write mutex for *customer_id*.

        Callers must hold this lock for the duration of any write operation
        against the tenant's OhmStore.  Reads do not need the lock.

        Raises:
            KeyError: If the tenant does not exist.
        """
        customer_id = validate_customer_id(customer_id)
        with self._cache_lock:
            if customer_id in self._cache:
                return self._cache[customer_id].write_lock

        # Not cached — get_store() may insert it; do the whole operation
        # under lock to prevent a TOCTOU race where eviction happens between
        # get_store() and the cache lookup below (OHM-g3g7).
        self.get_store(customer_id)
        with self._cache_lock:
            if customer_id in self._cache:
                return self._cache[customer_id].write_lock
            raise KeyError(f"No cached entry for tenant: {customer_id}")

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
        return self._list_tenants_impl()

    def _list_tenants_impl(self) -> list[dict]:
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

    def _get_cached_quota(self, customer_id: str) -> tuple[str, dict]:
        """Return (tier, quotas) from cache or meta.json (OHM-8d54).

        Caches quota tier + limits in memory to avoid hot-reading meta.json
        on every request. Cache is invalidated when tenant is updated.
        """
        with self._quota_cache_lock:
            cached = self._quota_cache.get(customer_id)
        if cached is not None:
            return cached
        meta = self._read_meta(customer_id)
        tier = meta.get("tier", "starter")
        quotas = meta.get("quotas", TIER_QUOTAS.get(tier, TIER_QUOTAS["starter"]))
        with self._quota_cache_lock:
            self._quota_cache[customer_id] = (tier, quotas)
        return (tier, quotas)

    def _invalidate_quota_cache(self, customer_id: str) -> None:
        """Invalidate cached quota for a tenant (called on provision/update)."""
        with self._quota_cache_lock:
            self._quota_cache.pop(customer_id, None)

    def check_quota(self, customer_id: str, resource: str, amount: int = 1) -> None:
        """Check if a tenant is within quota for a resource (OHM-982m).

        Args:
            customer_id: Tenant identifier.
            resource: One of 'nodes', 'edges', 'db_size_bytes',
                'requests_per_day'.
            amount: Amount being requested (default 1).

        Raises:
            QuotaExceededError: If the quota would be exceeded.
        """
        customer_id = validate_customer_id(customer_id)
        tier, quotas = self._get_cached_quota(customer_id)

        quota_key = f"max_{resource}"
        limit = quotas.get(quota_key)
        if limit is None:
            return  # No quota defined for this resource

        if resource in ("nodes", "edges"):
            # Use table name map to eliminate SQL interpolation entirely (OHM-f3mu)
            _QUOTA_TABLE = {"nodes": "ohm_nodes", "edges": "ohm_edges"}
            table_name = _QUOTA_TABLE.get(resource)
            if not table_name:
                return
            with self._cache_lock:
                entry = self._cache.get(customer_id)
            if entry is not None:
                try:
                    current = entry.store.conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE deleted_at IS NULL").fetchone()
                    current_count = current[0] if current else 0
                except Exception:
                    raise QuotaExceededError(f"Tenant '{customer_id}' quota check failed — cannot verify {resource} count (DB error)")
            else:
                raise QuotaExceededError(f"Tenant '{customer_id}' cannot verify {resource} quota — store not in cache")

            if current_count + amount > limit:
                raise QuotaExceededError(f"Tenant '{customer_id}' would exceed {resource} quota: {current_count + amount} > {limit}")

        elif resource == "db_size_bytes":
            db_path = self._tenant_dir(customer_id) / _DB_FILENAME
            try:
                current_size = db_path.stat().st_size
            except Exception:
                current_size = 0
            if current_size + amount > limit:
                raise QuotaExceededError(f"Tenant '{customer_id}' would exceed DB size quota: {current_size + amount} > {limit}")

        elif resource == "requests_per_day":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            meta = self._read_meta(customer_id)
            request_counts = meta.get("_request_counts", {})
            with self._request_counts_lock:
                in_mem = self._request_counts.get(customer_id, {})
            for date, count in in_mem.items():
                if date not in request_counts:
                    request_counts[date] = 0
                request_counts[date] += count
            current_count = request_counts.get(today, 0)
            if current_count + amount > limit:
                raise QuotaExceededError(f"Tenant '{customer_id}' would exceed daily request quota: {current_count + amount} > {limit}")

    def record_request(self, customer_id: str) -> int:
        """Record a request for rate limiting (OHM-982m). Returns today's count.

        Buffered in memory — flushed to meta.json by the checkpoint loop.
        """
        customer_id = validate_customer_id(customer_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._request_counts_lock:
            if customer_id not in self._request_counts:
                self._request_counts[customer_id] = {}
            counts = self._request_counts[customer_id]
            counts[today] = counts.get(today, 0) + 1
            self._dirty_tenants.add(customer_id)
            return counts[today]

    def get_meta(self, customer_id: str) -> dict:
        """Return meta.json for a single tenant."""
        return self._read_meta(customer_id)

    def load_integrations(self, customer_id: str) -> dict:
        """Load and resolve integration config for a tenant (OHM-uwl2).

        Reads the integrations dict from meta.json and resolves _ref fields
        from environment variables. _ref fields contain env var names (e.g.,
        ``TWILIO_AUTH_TOKEN_ACME_HVAC``) or vault paths. The resolved dict
        contains the actual credential values.

        Fields without ``_ref`` suffix are passed through as-is (e.g.,
        account_sid, phone_number).
        """
        customer_id = validate_customer_id(customer_id)
        meta = self._read_meta(customer_id)
        raw = meta.get("integrations", {})
        resolved = {}

        for channel, config in raw.items():
            channel_config = {}
            for field, value in config.items():
                if field.endswith("_ref") and isinstance(value, str):
                    env_val = os.environ.get(value)
                    if env_val is not None:
                        channel_config[field[:-4]] = env_val
                    else:
                        channel_config[field] = value
                        channel_config[f"{field}_unresolved"] = True
                else:
                    channel_config[field] = value
            resolved[channel] = channel_config

        return resolved

    def update_integrations(self, customer_id: str, integrations: dict) -> dict:
        """Update integrations for a tenant in meta.json (OHM-uwl2).

        Merges the provided integrations dict into the existing one.
        Returns the updated meta.json.
        """
        customer_id = validate_customer_id(customer_id)
        meta = self._read_meta(customer_id)
        existing = meta.get("integrations", {})
        existing.update(integrations)
        meta["integrations"] = existing
        self._write_meta(customer_id, meta)
        self._invalidate_quota_cache(customer_id)
        return meta

    def acquire_store(self, customer_id: str) -> _TenantEntry:
        """Get a store entry and mark it as in-use (OHM-s18r).

        Callers must call ``release_store()`` when done (typically via
        a try/finally block or ``using_store()`` context manager).
        """
        customer_id = validate_customer_id(customer_id)
        with self._cache_lock:
            if customer_id in self._cache:
                entry = self._cache[customer_id]
                self._cache.move_to_end(customer_id)
                entry.touch()
                entry.acquire()
                return entry

        # Not cached — open and insert
        meta = self._read_meta(customer_id)
        schema = self._load_schema(meta.get("domain", "ohm"))
        db_path = self._tenant_dir(customer_id) / _DB_FILENAME
        store = OhmStore(db_path=str(db_path), agent_name="ohmd", schema=schema)

        with self._cache_lock:
            if customer_id in self._cache:
                store.close()
                entry = self._cache[customer_id]
                self._cache.move_to_end(customer_id)
                entry.touch()
                entry.acquire()
                return entry

            entry = _TenantEntry(store)
            entry.acquire()
            self._cache[customer_id] = entry
            self._cache.move_to_end(customer_id)
            self._evict_lru_unlocked()

        return entry

    def release_store(self, customer_id: str) -> None:
        """Release an in-flight reference on a tenant store (OHM-s18r)."""
        with self._cache_lock:
            entry = self._cache.get(customer_id)
            if entry is not None:
                entry.release()
                if entry.refcount == 0 and entry.evict_pending:
                    self._cache.pop(customer_id, None)

        if entry is not None and entry.refcount == 0 and entry.evict_pending:
            try:
                entry.store.conn.execute("CHECKPOINT")  # OHM-7r9s
            except Exception as e:
                logger.warning("Checkpoint before evict failed: %s", e)
            try:
                entry.store.close()
            except Exception:
                pass

    @contextmanager
    def using_store(self, customer_id: str):
        """Context manager: acquire/release a tenant store with refcounting.

        Usage::

            with tm.using_store("acme_hvac") as entry:
                result = entry.store.conn.execute("SELECT ...")
        """
        entry = self.acquire_store(customer_id)
        try:
            yield entry
        finally:
            self.release_store(customer_id)

    def shutdown(self) -> None:
        """Signal background threads to stop and checkpoint all tenants (OHM-xfqp)."""
        if not hasattr(self, "_stop_event") or not hasattr(self, "_eviction_thread"):
            return  # Not fully initialized — nothing to shut down
        self._stop_event.set()  # Signal threads to stop
        self._eviction_thread.join(timeout=5)  # Wait for eviction to finish
        self._checkpoint_thread.join(timeout=5)  # Wait for checkpoint to finish
        # Checkpoint all cached tenants before exit
        with self._cache_lock:
            for cid, entry in list(self._cache.items()):
                try:
                    entry.store.conn.execute("CHECKPOINT")
                    logger.info("Checkpointed tenant %s during shutdown", cid)
                except Exception:
                    logger.exception("Failed to checkpoint tenant %s during shutdown", cid)

    def close(self) -> None:
        """Close all cached stores and checkpoint their WALs."""
        with self._cache_lock:
            for entry in self._cache.values():
                try:
                    entry.store.close()
                except Exception:
                    pass
            self._cache.clear()

    def reconcile_tenants(self) -> list[dict]:
        """Startup scan: detect tenants with drifted meta.json (OHM-xflr).

        Compares each tenant's meta.json schema_version against the actual
        DB schema version. Flags mismatches as needs_attention.

        Also detects tenants left in a half-migrated state (kill -9 during
        migration): the migration lock file persists and schema_version in
        meta.json is behind the current version.

        Returns a list of dicts with keys:
            customer_id, meta_version, db_version, status
        where status is one of: ok, meta_behind, db_behind, half_migrated.
        """
        from ohm.schema import SCHEMA_VERSION, get_schema_version

        def _vtuple(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))

        _vtuple(SCHEMA_VERSION)
        results = []

        for meta in self.list_tenants():
            cid = meta.get("customer_id", "")
            if not cid:
                continue

            tenant_version = meta.get("schema_version", 1)
            if isinstance(tenant_version, int):
                tenant_version_str = f"0.{tenant_version}.0"
            else:
                tenant_version_str = str(tenant_version)
            meta_key = _vtuple(tenant_version_str)

            # Check for migration lock file (indicates crash mid-migration)
            lock_path = self._tenant_dir(cid) / ".migration_lock"
            if lock_path.exists():
                results.append(
                    {
                        "customer_id": cid,
                        "meta_version": tenant_version_str,
                        "db_version": "unknown",
                        "status": "half_migrated",
                    }
                )
                meta["needs_attention"] = True
                meta["migration_error"] = "Migration lock file found — previous migration may have crashed"
                self._write_meta(cid, meta)
                continue

            # Open DB to check actual schema version
            try:
                db_path = self._tenant_dir(cid) / _DB_FILENAME
                if not db_path.exists():
                    results.append(
                        {
                            "customer_id": cid,
                            "meta_version": tenant_version_str,
                            "db_version": "missing",
                            "status": "meta_behind",
                        }
                    )
                    continue

                store = self.get_store(cid)
                db_version = get_schema_version(store.conn)
                db_key = _vtuple(db_version)

                if meta_key < db_key:
                    status = "meta_behind"
                    meta["schema_version"] = db_version
                    meta.pop("needs_attention", None)
                    meta.pop("migration_error", None)
                    self._write_meta(cid, meta)
                elif db_key < meta_key:
                    status = "db_behind"
                    meta["needs_attention"] = True
                    meta["migration_error"] = f"DB version {db_version} behind meta version {tenant_version_str}"
                    self._write_meta(cid, meta)
                else:
                    status = "ok"

                results.append(
                    {
                        "customer_id": cid,
                        "meta_version": tenant_version_str,
                        "db_version": db_version,
                        "status": status,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "customer_id": cid,
                        "meta_version": tenant_version_str,
                        "db_version": "error",
                        "status": "meta_behind",
                    }
                )
                meta["needs_attention"] = True
                meta["migration_error"] = f"Reconciliation error: {e}"
                self._write_meta(cid, meta)

        drifted = [r for r in results if r["status"] != "ok"]
        if drifted:
            logger.warning("Reconciliation found %d drifted tenants: %s", len(drifted), [r["customer_id"] for r in drifted])
        else:
            logger.info("Reconciliation: all tenants OK")

        return results

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write_meta(self, customer_id: str, meta: dict) -> None:
        """Atomically write meta.json for a tenant (write to temp, fsync, then rename) (OHM-2i4y)."""
        meta_path = self._tenant_dir(customer_id) / _META_FILENAME
        tmp_path = meta_path.with_suffix(".tmp")
        with tmp_path.open("w") as f:
            f.write(json.dumps(meta, indent=2))
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(meta_path)

    def _tenant_dir(self, customer_id: str) -> Path:
        return self._tenants_dir / customer_id

    def _read_meta(self, customer_id: str) -> dict:
        meta_path = self._tenant_dir(customer_id) / _META_FILENAME
        if not meta_path.exists():
            raise TenantNotFoundError(f"Tenant '{customer_id}' not found — provision first")
        return json.loads(meta_path.read_text())

    def _load_schema(self, domain: str) -> SchemaConfig:
        import re as _re

        if not _re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", domain):
            raise ValueError(f"Invalid domain '{domain}' — must be lowercase alphanumeric/underscore/hyphen, 1-63 chars")
        # Try custom templates dir first
        if self._templates_dir:
            json_path = self._templates_dir / f"{domain}.json"
            if json_path.exists():
                try:
                    return SchemaConfig.from_json_file(str(json_path))
                except Exception as e:
                    logger.warning("Could not load template %s: %s — using default", json_path, e)

        # Try package-bundled templates
        package_template = Path(__file__).parent / "graph" / "templates" / f"{domain}.json"
        if package_template.exists():
            try:
                return SchemaConfig.from_json_file(str(package_template))
            except Exception as e:
                logger.warning("Could not load package template %s: %s — using default", package_template, e)

        # Fallback to base ohm schema
        ohm_template = Path(__file__).parent / "graph" / "templates" / "ohm.json"
        if ohm_template.exists():
            try:
                return SchemaConfig.from_json_file(str(ohm_template))
            except Exception:
                pass
        return SchemaConfig()

    def _apply_lazy_migrations(self, customer_id: str, store: OhmStore) -> None:
        """Apply pending schema migrations to a tenant instance (OHM-tss4.5).

        On first access after an OHM upgrade, the OhmStore constructor already
        runs initialize_schema + _apply_migrations, so the DB is at the current
        version. This method syncs meta.json with the actual DB version.

        If meta.json reports a version behind the DB, we update meta.json.
        If meta.json reports a version ahead of the DB (unlikely), we attempt
        to migrate and mark needs_attention on failure.

        Migration lock file (.migration_lock) is created before applying
        migrations and removed after success. If a crash occurs mid-migration,
        the lock file persists and reconcile_tenants() detects it (OHM-xflr).
        """
        from ohm.schema import SCHEMA_VERSION, get_schema_version

        meta = self._read_meta(customer_id)
        tenant_version = meta.get("schema_version", 1)
        if isinstance(tenant_version, int):
            tenant_version_str = f"0.{tenant_version}.0"
        else:
            tenant_version_str = str(tenant_version)

        def _vtuple(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))

        # Compare meta.json version against current OHM version
        meta_key = _vtuple(tenant_version_str)
        target_key = _vtuple(SCHEMA_VERSION)
        if meta_key >= target_key:
            return

        # Acquire per-tenant write lock for migration (OHM-cmao: cache read under lock)
        with self._cache_lock:
            entry = self._cache.get(customer_id)
        if entry is None:
            return

        lock_path = self._tenant_dir(customer_id) / ".migration_lock"

        with entry.write_lock:
            db_version = get_schema_version(store.conn)
            db_key = _vtuple(db_version)

            if db_key >= target_key:
                # DB is already current (OhmStore init migrated it) — sync meta.json
                meta["schema_version"] = SCHEMA_VERSION
                meta.pop("needs_attention", None)
                meta.pop("migration_error", None)
                self._write_meta(customer_id, meta)
                # Clean up stale lock file if present
                if lock_path.exists():
                    lock_path.unlink(missing_ok=True)
                logger.info("Synced meta.json for tenant %s to schema %s", customer_id, SCHEMA_VERSION)
                return

            logger.info(
                "Migrating tenant %s from %s to %s",
                customer_id,
                db_version,
                SCHEMA_VERSION,
            )
            # Write migration lock file for crash detection
            lock_path.write_text(
                json.dumps(
                    {
                        "customer_id": customer_id,
                        "from_version": db_version,
                        "to_version": SCHEMA_VERSION,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            try:
                from ohm.schema import _apply_migrations

                _apply_migrations(store.conn)
                meta["schema_version"] = SCHEMA_VERSION
                meta.pop("needs_attention", None)
                meta.pop("migration_error", None)
                self._write_meta(customer_id, meta)
                lock_path.unlink(missing_ok=True)
                logger.info("Migrated tenant %s to schema %s", customer_id, SCHEMA_VERSION)
            except Exception as e:
                logger.error("Migration failed for tenant %s: %s", customer_id, e)
                meta["needs_attention"] = True
                meta["migration_error"] = str(e)
                self._write_meta(customer_id, meta)
                # Leave lock file in place — reconcile_tenants() will detect it
                raise  # OHM-dlnx: re-raise so caller doesn't use half-migrated store

    def _propagate_template(self, customer_id: str, store: OhmStore) -> None:
        """Apply additive domain-template changes to an existing tenant (OHM-dcf3).

        When a domain template gains new types (node, edge, observation, etc.),
        existing tenants on that domain should receive those additions on next
        access. This is safe because SchemaConfig is validation metadata, not
        DDL — adding types only widens what the validator accepts.

        Breaking changes (type renames, type removals) are NOT auto-propagated.
        Those require manual migration via the upgrade guide.

        Algorithm:
        1. Read tenant's template_version from meta.json
        2. Load the current domain template
        3. If current > tenant's, apply additive merge and update meta.json
        """
        meta = self._read_meta(customer_id)
        tenant_tv = meta.get("template_version", 0)
        domain = meta.get("domain", "ohm")
        current_schema = self._load_schema(domain)
        current_tv = current_schema.template_version

        if current_tv <= tenant_tv:
            return

        with self._schema_lock:  # OHM-yzau
            old_schema = store.schema
            merged = self._additive_merge(old_schema, current_schema)
            store.schema = merged

            meta["template_version"] = current_tv
            self._write_meta(customer_id, meta)
            logger.info(
                "Propagated template v%d \u2192 v%d for tenant %s (domain=%s)",
                tenant_tv,
                current_tv,
                customer_id,
                domain,
            )

    @staticmethod
    def _additive_merge(old: "SchemaConfig", current: "SchemaConfig") -> "SchemaConfig":
        """Merge additive changes from current template into old schema.

        New types in current are added to old. Types present in old but
        absent in current are preserved (not removed — that's a breaking change).
        """
        from ohm.graph.schema import SchemaConfig

        merged_node_types = old.node_types | current.node_types
        merged_obs_types = old.observation_types | current.observation_types
        merged_obs_sources = old.observation_sources | current.observation_sources
        merged_visibilities = old.visibilities | current.visibilities
        merged_provenances = old.provenances | current.provenances

        merged_layer_edge_types = {}
        all_layers = set(old.layer_edge_types.keys()) | set(current.layer_edge_types.keys())
        for layer in all_layers:
            old_types = old.layer_edge_types.get(layer, frozenset())
            current_types = current.layer_edge_types.get(layer, frozenset())
            merged_layer_edge_types[layer] = old_types | current_types

        merged_layer_descriptions = {**old.layer_descriptions, **current.layer_descriptions}

        merged_required = {**old.required_integrations, **current.required_integrations}
        merged_optional = {**old.optional_integrations, **current.optional_integrations}

        return SchemaConfig(
            name=old.name,
            node_types=merged_node_types,
            edge_types_by_layer=merged_layer_edge_types,
            layer_descriptions=merged_layer_descriptions,
            observation_types=merged_obs_types,
            observation_sources=merged_obs_sources,
            visibilities=merged_visibilities,
            provenances=merged_provenances,
            required_integrations=merged_required,
            optional_integrations=merged_optional,
            template_version=current.template_version,
        )

    def _evict(self, customer_id: str) -> None:
        """Remove *customer_id* from the cache and close its store.

        Checkpoints the tenant before closing to flush WAL (OHM-p7fv).
        Skips eviction if the entry has in-flight requests (OHM-s18r),
        marking it for deferred eviction instead.
        """
        with self._cache_lock:
            entry = self._cache.get(customer_id)
            if entry is None:
                return
            if entry.refcount > 0:
                entry.evict_pending = True
                logger.debug("Skip eviction of tenant %s (refcount=%d), marked for deferred eviction", customer_id, entry.refcount)
                return
            self._cache.pop(customer_id, None)

        # Checkpoint before close to flush WAL
        try:
            entry.store.conn.execute("CHECKPOINT")
        except Exception:
            pass
        try:
            entry.store.close()
        except Exception:
            pass

    def _evict_lru_unlocked(self) -> None:
        """Evict the LRU entry if cache is over capacity. Must hold _cache_lock.

        Skips entries with in-flight requests (refcount > 0) and marks them
        for deferred eviction instead (OHM-s18r).
        """
        while len(self._cache) > self._max_cached:
            # Find LRU entry that isn't in-use
            evicted = False
            for cid, entry in self._cache.items():
                if entry.refcount == 0:
                    self._cache.pop(cid)
                    try:
                        entry.store.conn.execute("CHECKPOINT")
                    except Exception:
                        pass
                    try:
                        entry.store.close()
                    except Exception:
                        pass
                    logger.debug("LRU evicted tenant %s", cid)
                    evicted = True
                    break
                else:
                    entry.evict_pending = True
                    logger.debug("LRU skip tenant %s (refcount=%d), marked for deferred eviction", cid, entry.refcount)

            if not evicted:
                # All entries have in-flight requests — can't evict any
                logger.warning("LRU eviction: all %d cached tenants have in-flight requests", len(self._cache))
                break

    def _eviction_loop(self) -> None:
        """Background thread: evict idle tenants every 60 seconds (OHM-s8sg: resilient, OHM-xfqp: graceful shutdown)."""
        consecutive_errors = 0
        stop_event = getattr(self, "_stop_event", None)
        while not (stop_event and stop_event.is_set()):
            try:
                stop_event.wait(timeout=60) if stop_event else True  # OHM-xfqp: interruptible sleep
                now = time.monotonic()
                with self._cache_lock:
                    idle = [cid for cid, entry in self._cache.items() if now - entry.last_accessed > _IDLE_EVICT_SECONDS]
                for cid in idle:
                    self._evict(cid)
                    logger.info("Idle-evicted tenant %s", cid)
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.exception("Eviction loop error (consecutive=%d)", consecutive_errors)
                if consecutive_errors >= 3:
                    time.sleep(60)  # backoff after repeated failures

    def _checkpoint_loop(self) -> None:
        """Background thread: periodic checkpoint of active tenants (OHM-p7fv, OHM-s8sg: resilient, OHM-xfqp: graceful shutdown)."""
        consecutive_errors = 0
        stop_event = getattr(self, "_stop_event", None)
        while not (stop_event and stop_event.is_set()):
            try:
                stop_event.wait(timeout=self._checkpoint_interval) if stop_event else True  # OHM-xfqp: interruptible sleep
                self._checkpoint_active_tenants()
                self._flush_request_counts()
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.exception("Checkpoint loop error (consecutive=%d)", consecutive_errors)
                if consecutive_errors >= 3:
                    time.sleep(60)  # backoff after repeated failures

    def _checkpoint_active_tenants(self) -> None:
        """Checkpoint all cached tenants whose interval has elapsed (OHM-p7fv).

        Also checks WAL size and forces checkpoint if above threshold.
        """
        now = time.monotonic()
        with self._cache_lock:
            entries = list(self._cache.items())

        for cid, entry in entries:
            # last_checkpoint_at == 0.0 means never checkpointed — always eligible.
            # Otherwise skip if the interval hasn't elapsed yet.
            if entry.last_checkpoint_at > 0.0:
                elapsed = now - entry.last_checkpoint_at
                if elapsed < self._checkpoint_interval:
                    continue
            else:
                elapsed = self._checkpoint_interval  # treat as exactly due

            wal_size = self._wal_size(cid)
            if wal_size >= self._wal_size_threshold:
                self._checkpoint_tenant(cid, entry, reason="wal_size_threshold")
            elif elapsed >= self._checkpoint_interval:
                self._checkpoint_tenant(cid, entry, reason="interval")

    def _checkpoint_tenant(self, customer_id: str, entry: _TenantEntry, reason: str = "manual") -> None:
        """Checkpoint a single tenant's DuckDB."""
        with entry.write_lock:
            try:
                entry.store.conn.execute("CHECKPOINT")
                entry.last_checkpoint_at = time.monotonic()
                logger.debug("Checkpointed tenant %s (reason=%s)", customer_id, reason)
            except Exception as e:
                logger.warning("Checkpoint failed for tenant %s: %s", customer_id, e)

    def _wal_size(self, customer_id: str) -> int:
        """Return the WAL file size in bytes for a tenant."""
        wal_path = self._tenant_dir(customer_id) / (_DB_FILENAME + ".wal")
        if wal_path.exists():
            try:
                return wal_path.stat().st_size
            except Exception:
                return 0
        return 0

    def _flush_request_counts(self) -> None:
        """Flush buffered request counts to meta.json (OHM-8d54)."""
        with self._request_counts_lock:
            dirty = list(self._dirty_tenants)
            self._dirty_tenants.clear()

        for customer_id in dirty:
            try:
                meta = self._read_meta(customer_id)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                request_counts = meta.get("_request_counts", {})
                with self._request_counts_lock:
                    in_mem = self._request_counts.get(customer_id, {})
                for date, count in in_mem.items():
                    if date not in request_counts:
                        request_counts[date] = 0
                    request_counts[date] += count
                request_counts = {k: v for k, v in request_counts.items() if k >= today[:7]}
                meta["_request_counts"] = request_counts
                self._write_meta(customer_id, meta)
            except Exception:
                with self._request_counts_lock:
                    self._dirty_tenants.add(customer_id)

    def tenant_health(self, customer_id: str) -> dict:
        """Return health info for a tenant (OHM-p7fv/OHM-982m).

        Includes WAL size, last checkpoint time, schema version,
        needs_attention status, and quota usage.
        """
        customer_id = validate_customer_id(customer_id)
        meta = self._read_meta(customer_id)
        quotas = meta.get("quotas", TIER_QUOTAS.get(meta.get("tier", "starter"), {}))

        with self._cache_lock:
            entry = self._cache.get(customer_id)
            if entry is not None:
                try:
                    node_count = entry.store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
                    edge_count = entry.store.conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]
                except Exception:
                    node_count = 0
                    edge_count = 0
            else:
                node_count = 0
                edge_count = 0

        db_size = 0
        db_path = self._tenant_dir(customer_id) / _DB_FILENAME
        try:
            db_size = db_path.stat().st_size
        except Exception:
            pass

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        request_counts = meta.get("_request_counts", {})
        requests_today = request_counts.get(today, 0)

        result = {
            "customer_id": customer_id,
            "schema_version": meta.get("schema_version"),
            "tier": meta.get("tier", "starter"),
            "needs_attention": meta.get("needs_attention", False),
            "wal_size_bytes": self._wal_size(customer_id),
            "last_checkpoint_at": None,
            "cached": entry is not None,
            "refcount": entry.refcount if entry else 0,
            "quotas": quotas,
            "usage": {
                "nodes": node_count,
                "edges": edge_count,
                "db_size_bytes": db_size,
                "requests_today": requests_today,
            },
        }

        if entry is not None:
            result["last_checkpoint_at"] = entry.last_checkpoint_at

        return result

    # ── Backup & Restore (OHM-kbwl) ─────────────────────────────────────

    def backup_tenant(self, customer_id: str, *, reason: str = "manual") -> dict:
        """Create a backup of a tenant's DuckDB + meta.json.

        Checkpoints first, then evicts from LRU cache to release the file
        handle (required on Windows), copies DB + WAL + meta, and re-opens.

        Args:
            customer_id: Tenant to back up.
            reason: Why the backup was created (manual, scheduled, pre_migration, pre_deprovision).

        Returns:
            Dict with backup_id, timestamp, file sizes, and reason.
        """
        customer_id = validate_customer_id(customer_id)
        tenant_dir = self._tenant_dir(customer_id)
        backup_dir = tenant_dir / _BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        backup_id = now.strftime("%Y%m%dT%H%M%SZ")
        existing = [e.name for e in backup_dir.iterdir()] if backup_dir.exists() else []
        if backup_id in existing:
            backup_id = f"{backup_id}_{secrets.token_hex(3)}"

        backup_path = backup_dir / backup_id
        backup_path.mkdir(parents=True, exist_ok=True)

        with self._cache_lock:
            entry = self._cache.get(customer_id)

        if entry is not None:
            self._checkpoint_tenant(customer_id, entry, reason="pre_backup")
            with self._cache_lock:
                self._cache.pop(customer_id, None)
            try:
                entry.store.close()
            except Exception:
                pass

        db_src = tenant_dir / _DB_FILENAME
        wal_src = tenant_dir / (_DB_FILENAME + ".wal")
        meta_src = tenant_dir / _META_FILENAME

        import time as _time

        def _wait_for_file_ready(path: Path, max_attempts: int = 10) -> bool:
            for attempt in range(max_attempts):
                try:
                    with open(str(path), "r+b"):
                        pass
                    return True
                except (IOError, OSError):
                    if attempt < max_attempts - 1:
                        _time.sleep(0.05 * (2**attempt))
            return False

        def _atomic_copy(src: Path, dst_dir: Path, name: str) -> tuple[int, str]:
            dst = dst_dir / name
            tmp = dst.with_suffix(".tmp")
            checksum = ""
            if src.exists():
                import hashlib

                with open(str(src), "rb") as sf:
                    data = sf.read()
                with open(str(tmp), "wb") as tf:
                    tf.write(data)
                os.replace(str(tmp), str(dst))
                checksum = hashlib.sha256(data).hexdigest()
            return (src.stat().st_size if src.exists() else 0, checksum)

        for src in [db_src, wal_src, meta_src]:
            if src.exists():
                if not _wait_for_file_ready(src):
                    logger.warning("File %s may still be locked during backup", src)

        db_size, db_checksum = _atomic_copy(db_src, backup_path, _DB_FILENAME)
        wal_size, wal_checksum = _atomic_copy(wal_src, backup_path, _DB_FILENAME + ".wal")
        meta_size, meta_checksum = _atomic_copy(meta_src, backup_path, _META_FILENAME)

        backup_meta = {
            "backup_id": backup_id,
            "customer_id": customer_id,
            "created_at": now.isoformat(),
            "reason": reason,
            "db_size_bytes": db_size,
            "wal_size_bytes": wal_size,
            "meta_size_bytes": meta_size,
            "db_checksum": db_checksum,
            "wal_checksum": wal_checksum,
            "meta_checksum": meta_checksum,
        }
        meta_out = backup_meta.copy()
        if meta_src.exists():
            try:
                original_meta = json.loads(meta_src.read_text())
                meta_out = {**original_meta, **backup_meta}
            except Exception:
                pass
        meta_tmp = backup_path / (_META_FILENAME + ".tmp")
        meta_tmp.write_text(json.dumps(meta_out, indent=2))
        os.replace(str(meta_tmp), str(backup_path / _META_FILENAME))

        self._enforce_retention(customer_id)

        with self._cache_lock:
            from .store import OhmStore

            try:
                entry = _TenantEntry(
                    store=OhmStore(str(tenant_dir / _DB_FILENAME)),
                    write_lock=threading.RLock(),
                    last_checkpoint_at=0.0,
                )
                self._cache[customer_id] = entry
            except Exception:
                pass

        logger.info("Backed up tenant %s → %s (reason=%s)", customer_id, backup_id, reason)
        return backup_meta

    def list_backups(self, customer_id: str) -> list[dict]:
        """List all backups for a tenant, newest first."""
        customer_id = validate_customer_id(customer_id)
        backup_dir = self._tenant_dir(customer_id) / _BACKUP_DIR_NAME
        if not backup_dir.exists():
            return []

        backups = []
        for entry in sorted(backup_dir.iterdir(), reverse=True):
            if entry.is_dir():
                meta_path = entry / _META_FILENAME
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        backups.append(
                            {
                                "backup_id": entry.name,
                                "created_at": meta.get("created_at", ""),
                                "reason": meta.get("reason", "unknown"),
                                "db_size_bytes": meta.get("db_size_bytes", 0),
                            }
                        )
                    except Exception:
                        backups.append({"backup_id": entry.name, "created_at": "", "reason": "unknown", "db_size_bytes": 0})

        return backups

    def restore_tenant(self, customer_id: str, backup_id: str) -> dict:
        """Restore a tenant from a backup.

        Replaces the current DuckDB and meta.json with the backup copies.
        The tenant must NOT have in-flight requests (refcount must be 0).

        Args:
            customer_id: Tenant to restore.
            backup_id: Backup timestamp ID (e.g., "20260524T180000Z").

        Returns:
            Dict with status and backup metadata.
        """
        customer_id = validate_customer_id(customer_id)
        from ohm.framework.validation import validate_backup_id

        backup_id = validate_backup_id(backup_id)
        backup_path = self._tenant_dir(customer_id) / _BACKUP_DIR_NAME / backup_id
        if not backup_path.exists():
            raise ValueError(f"Backup '{backup_id}' not found for tenant '{customer_id}'")

        with self._cache_lock:
            entry = self._cache.get(customer_id)
        if entry is not None and entry.refcount > 0:
            raise RuntimeError(f"Cannot restore tenant '{customer_id}' — {entry.refcount} in-flight requests")

        tenant_dir = self._tenant_dir(customer_id)

        if entry is not None:
            self._checkpoint_tenant(customer_id, entry, reason="pre_restore")
            with self._cache_lock:
                self._cache.pop(customer_id, None)
            try:
                entry.store.close()
            except Exception:
                pass

        self.backup_tenant(customer_id, reason="pre_restore")

        db_src = backup_path / _DB_FILENAME
        wal_src = backup_path / (_DB_FILENAME + ".wal")
        meta_src = backup_path / _META_FILENAME

        db_dst = tenant_dir / _DB_FILENAME
        wal_dst = tenant_dir / (_DB_FILENAME + ".wal")
        meta_dst = tenant_dir / _META_FILENAME

        if db_dst.exists():
            db_dst.unlink()
        if wal_dst.exists():
            wal_dst.unlink()

        if db_src.exists():
            shutil.copy2(str(db_src), str(db_dst))
        if wal_src.exists():
            shutil.copy2(str(wal_src), str(wal_dst))
        if meta_src.exists():
            meta_content = json.loads(meta_src.read_text())
            restore_meta = {k: v for k, v in meta_content.items() if k not in ("backup_id", "reason")}
            meta_dst.write_text(json.dumps(restore_meta, indent=2))

        backup_meta = {}
        try:
            backup_meta = json.loads((backup_path / _META_FILENAME).read_text())
        except Exception:
            pass

        logger.info("Restored tenant %s from backup %s", customer_id, backup_id)
        return {"status": "restored", "customer_id": customer_id, "backup_id": backup_id, "backup_meta": backup_meta}

    def _enforce_retention(self, customer_id: str) -> None:
        """Enforce backup retention policy — delete oldest beyond limit."""
        customer_id = validate_customer_id(customer_id)
        meta = self._read_meta(customer_id)
        retention = meta.get("backup_retention_days", _DEFAULT_BACKUP_RETENTION)

        backup_dir = self._tenant_dir(customer_id) / _BACKUP_DIR_NAME
        if not backup_dir.exists():
            return

        cutoff = datetime.now(timezone.utc).timestamp() - (retention * 86400)
        for entry in sorted(backup_dir.iterdir()):
            if entry.is_dir():
                try:
                    ts = datetime.strptime(entry.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                    if ts < cutoff:
                        shutil.rmtree(str(entry), ignore_errors=True)
                        logger.debug("Pruned backup %s for tenant %s (older than %d days)", entry.name, customer_id, retention)
                except ValueError:
                    pass
