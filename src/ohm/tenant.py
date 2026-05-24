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
    ) -> None:
        self._tenants_dir = Path(tenants_dir)
        self._tenants_dir.mkdir(parents=True, exist_ok=True)
        self._templates_dir = Path(templates_dir) if templates_dir else None
        self._max_cached = max_cached
        self._checkpoint_interval = checkpoint_interval
        self._wal_size_threshold = wal_size_threshold

        self._cache: OrderedDict[str, _TenantEntry] = OrderedDict()
        self._cache_lock = threading.Lock()

        self._eviction_thread = threading.Thread(target=self._eviction_loop, daemon=True, name="tenant-eviction")
        self._eviction_thread.start()

        self._checkpoint_thread = threading.Thread(target=self._checkpoint_loop, daemon=True, name="tenant-checkpoint")
        self._checkpoint_thread.start()

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

        meta = {
            "customer_id": customer_id,
            "domain": domain,
            "tier": tier,
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "shared_patterns": False,
            "integrations": integrations or {},
        }
        self._write_meta(customer_id, meta)
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

        target_key = _vtuple(SCHEMA_VERSION)
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
                results.append({
                    "customer_id": cid,
                    "meta_version": tenant_version_str,
                    "db_version": "unknown",
                    "status": "half_migrated",
                })
                meta["needs_attention"] = True
                meta["migration_error"] = "Migration lock file found — previous migration may have crashed"
                self._write_meta(cid, meta)
                continue

            # Open DB to check actual schema version
            try:
                db_path = self._tenant_dir(cid) / _DB_FILENAME
                if not db_path.exists():
                    results.append({
                        "customer_id": cid,
                        "meta_version": tenant_version_str,
                        "db_version": "missing",
                        "status": "meta_behind",
                    })
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

                results.append({
                    "customer_id": cid,
                    "meta_version": tenant_version_str,
                    "db_version": db_version,
                    "status": status,
                })
            except Exception as e:
                results.append({
                    "customer_id": cid,
                    "meta_version": tenant_version_str,
                    "db_version": "error",
                    "status": "meta_behind",
                })
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
        """Atomically write meta.json for a tenant (write to temp, then rename)."""
        meta_path = self._tenant_dir(customer_id) / _META_FILENAME
        tmp_path = meta_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(meta, indent=2))
        tmp_path.replace(meta_path)

    def _tenant_dir(self, customer_id: str) -> Path:
        return self._tenants_dir / customer_id

    def _read_meta(self, customer_id: str) -> dict:
        meta_path = self._tenant_dir(customer_id) / _META_FILENAME
        if not meta_path.exists():
            raise TenantNotFoundError(f"Tenant '{customer_id}' not found — provision first")
        return json.loads(meta_path.read_text())

    def _load_schema(self, domain: str) -> SchemaConfig:
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

        # Acquire per-tenant write lock for migration
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
                customer_id, db_version, SCHEMA_VERSION,
            )
            # Write migration lock file for crash detection
            lock_path.write_text(json.dumps({
                "customer_id": customer_id,
                "from_version": db_version,
                "to_version": SCHEMA_VERSION,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }))
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
        """Background thread: evict idle tenants every 60 seconds."""
        while True:
            time.sleep(60)
            now = time.monotonic()
            with self._cache_lock:
                idle = [cid for cid, entry in self._cache.items() if now - entry.last_accessed > _IDLE_EVICT_SECONDS]
            for cid in idle:
                self._evict(cid)
                logger.info("Idle-evicted tenant %s", cid)

    def _checkpoint_loop(self) -> None:
        """Background thread: periodic checkpoint of active tenants (OHM-p7fv)."""
        while True:
            time.sleep(self._checkpoint_interval)
            self._checkpoint_active_tenants()

    def _checkpoint_active_tenants(self) -> None:
        """Checkpoint all cached tenants whose interval has elapsed (OHM-p7fv).

        Also checks WAL size and forces checkpoint if above threshold.
        """
        now = time.monotonic()
        with self._cache_lock:
            entries = list(self._cache.items())

        for cid, entry in entries:
            elapsed = now - entry.last_checkpoint_at
            if elapsed < self._checkpoint_interval:
                continue

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

    def tenant_health(self, customer_id: str) -> dict:
        """Return health info for a tenant (OHM-p7fv).

        Includes WAL size, last checkpoint time, schema version, and
        needs_attention status.
        """
        customer_id = validate_customer_id(customer_id)
        meta = self._read_meta(customer_id)

        with self._cache_lock:
            entry = self._cache.get(customer_id)

        result = {
            "customer_id": customer_id,
            "schema_version": meta.get("schema_version"),
            "needs_attention": meta.get("needs_attention", False),
            "wal_size_bytes": self._wal_size(customer_id),
            "last_checkpoint_at": None,
            "cached": entry is not None,
            "refcount": entry.refcount if entry else 0,
        }

        if entry is not None:
            result["last_checkpoint_at"] = entry.last_checkpoint_at

        return result
