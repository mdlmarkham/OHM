"""Tenant handler mixin — tenant provisioning and management endpoints."""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from ohm.exceptions import (
    ConflictError,
    NodeNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ohm.server import server as _server_module


class TenantHandlerMixin:
    """Handler mixin for tenant provisioning and management (OHM-97q8)."""

    def _require_multi_tenant_active(self) -> None:
        """Raise ValidationError if multi-tenancy or TenantManager is not active."""
        if not self.multi_tenant or self.tenant_manager is None:
            raise ValidationError("Multi-tenancy is not enabled — start ohmd with --multi-tenant")

    def _post_tenant_provision(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tenant/provision — create a new tenant instance and generate an API key."""
        self._require_admin()
        self._require_multi_tenant_active()

        from ohm.tenant import TenantAlreadyExistsError

        customer_id = body.get("customer_id", "")
        if not customer_id:
            raise ValidationError("customer_id is required")
        from ohm.framework.validation import validate_customer_id as _validate_cid

        try:
            customer_id = _validate_cid(customer_id)
        except ValueError as exc:
            raise ValidationError(str(exc))
        domain = body.get("domain", "ohm")
        tier = body.get("tier", "starter")
        import re as _re

        if not _re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", domain):
            raise ValidationError(f"Invalid domain '{domain}' — must be lowercase alphanumeric/underscore/hyphen, 1-63 chars")

        try:
            meta = self.tenant_manager.provision(customer_id, domain=domain, tier=tier)
        except TenantAlreadyExistsError:
            raise ConflictError(f"Tenant '{customer_id}' already exists")

        token, token_hash = _server_module._generate_customer_token(customer_id)
        with _server_module._customer_tokens_lock:
            type(self).customer_tokens[token_hash] = customer_id

        self._json_response(
            201,
            {
                "customer_id": customer_id,
                "domain": domain,
                "tier": tier,
                "token": token,
                "meta": meta,
                "warning": "Store this token securely — it will not be shown again.",
            },
        )

    def _get_tenants(self, path: str, qs: dict) -> None:
        """GET /tenants — list all provisioned tenants."""
        self._require_admin()
        self._require_multi_tenant_active()
        tenants = self.tenant_manager.list_tenants()
        self._json_response(200, {"tenants": tenants, "count": len(tenants)})

    def _get_tenant_prefix(self, path: str, qs: dict) -> None:
        """GET /tenant/{id} or /tenant/{id}/schema — tenant status or domain schema."""
        self._require_admin()
        self._require_multi_tenant_active()

        tail = path[len("/tenant/") :]
        parts = tail.split("/", 1)
        raw_id = parts[0]
        sub = parts[1] if len(parts) > 1 else ""
        from ohm.framework.validation import validate_customer_id as _validate_cid

        try:
            customer_id = _validate_cid(raw_id)
        except ValueError as exc:
            raise ValidationError(str(exc))

        from ohm.tenant import TenantNotFoundError

        try:
            meta = self.tenant_manager.get_meta(customer_id)
        except TenantNotFoundError:
            raise NodeNotFoundError(f"Tenant '{customer_id}' not found")

        if sub == "schema":
            try:
                tenant_store = self.tenant_manager.get_store(customer_id)
                sc = tenant_store.schema
                schema_data = {
                    "customer_id": customer_id,
                    "domain": meta.get("domain", "ohm"),
                    "node_types": sorted(sc.node_types) if sc else [],
                    "edge_types": sorted(sc.all_edge_types) if sc else [],
                }
                self._json_response(200, schema_data)
            except PermissionDeniedError:
                raise
            except Exception as e:
                self._json_response(500, {"error": "schema_error", "message": str(e)})
        elif sub == "health":
            health = self.tenant_manager.tenant_health(customer_id)
            self._json_response(200, health)
        elif sub == "backups":
            backups = self.tenant_manager.list_backups(customer_id)
            self._json_response(200, {"customer_id": customer_id, "backups": backups})
        elif sub == "":
            self._json_response(200, {"tenant": meta})
        else:
            self._json_response(404, {"error": f"Unknown tenant sub-resource: {sub}"})

    def _delete_tenant_prefix(self, path: str, agent: str) -> None:
        """DELETE /tenant/{id} — deprovision a tenant."""
        self._require_admin()
        self._require_multi_tenant_active()

        qs = parse_qs(urlparse(self.path).query)
        confirm = qs.get("confirm", ["false"])[0].lower() in ("true", "1", "yes")
        if not confirm:
            raise ValidationError("Pass ?confirm=true to deprovision a tenant — this is irreversible")

        customer_id = path[len("/tenant/") :]
        try:
            from ohm.tenant import TenantNotFoundError, validate_customer_id

            customer_id = validate_customer_id(customer_id)
        except ValueError as exc:
            raise ValidationError(f"Invalid customer_id: {exc}")
        try:
            self.tenant_manager.deprovision(customer_id, confirm=True)
        except TenantNotFoundError:
            raise NodeNotFoundError(f"Tenant '{customer_id}' not found")

        with _server_module._customer_tokens_lock:
            revoked = [h for h, cid in list(type(self).customer_tokens.items()) if cid == customer_id]
            for h in revoked:
                type(self).customer_tokens.pop(h, None)

        self._json_response(200, {"status": "deprovisioned", "customer_id": customer_id})

    def _post_tenant_export(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tenant/{id}/export, /backup, /restore — tenant data operations."""
        self._require_admin()
        self._require_multi_tenant_active()

        from ohm.tenant import TenantNotFoundError

        tail = path[len("/tenant/") :]
        parts = tail.split("/", 1)
        raw_id = parts[0]
        sub = parts[1] if len(parts) > 1 else ""
        from ohm.framework.validation import validate_customer_id as _validate_cid

        try:
            customer_id = _validate_cid(raw_id)
        except ValueError as exc:
            raise ValidationError(str(exc))

        if sub == "export":
            try:
                tenant_store = self.tenant_manager.get_store(customer_id)
            except TenantNotFoundError:
                raise NodeNotFoundError(f"Tenant '{customer_id}' not found")

            nodes = tenant_store.execute("SELECT * FROM ohm_nodes WHERE deleted_at IS NULL ORDER BY id")
            edges = tenant_store.execute("SELECT * FROM ohm_edges WHERE deleted_at IS NULL ORDER BY id")
            self._json_response(
                200,
                {
                    "customer_id": customer_id,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "nodes": nodes,
                    "edges": edges,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                },
            )
        elif sub == "backup":
            reason = body.get("reason", "manual") if body else "manual"
            result = self.tenant_manager.backup_tenant(customer_id, reason=reason)
            self._json_response(201, result)
        elif sub == "restore":
            if not body or "backup_id" not in body:
                raise ValidationError("backup_id is required")
            from ohm.framework.validation import validate_backup_id

            backup_id = validate_backup_id(body["backup_id"])
            result = self.tenant_manager.restore_tenant(customer_id, backup_id)
            self._json_response(200, result)
        else:
            self._json_response(404, {"error": f"Unknown tenant endpoint: {path}"})
