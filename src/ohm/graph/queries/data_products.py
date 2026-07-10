"""data_products queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


# ── Data Products (ADR-027 / OHM-ksi0) ─────────────────────────────────────


def register_data_product(
    conn: DuckDBPyConnection,
    *,
    product_id: str,
    name: str,
    type: str,
    producer_agent: str,
    created_by: str,
    customer_id: str | None = None,
    language: str = "en",
    visibility: str = "private",
    status: str = "draft",
    value_proposition: str | None = None,
    description: str | None = None,
    output_port_type: str | None = None,
    access_format: str | None = None,
    access_url: str | None = None,
    authentication_method: str | None = None,
    output_file_formats: str | None = None,
    ohm_node_id: str | None = None,
    confidence: float | None = None,
    product_version: str | None = None,
    odps_yaml: str | None = None,
    consumers: list[str] | None = None,
    auto_link: bool = True,
) -> dict[str, Any]:
    """Insert or update an ODPS data product (ADR-027). Returns the full record.

    OHM-ovwq: When ``ohm_node_id`` is None and ``auto_link`` is True, auto-creates
    an OHM ``source`` node for the product, a ``PRODUCES`` L2 edge from the producer
    agent node, and ``CONSUMES`` L2 edges from each consumer agent node. Seeds
    ``source_reliability`` from the producer agent's outcome history.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    if ohm_node_id is None and auto_link:
        ohm_node_id, source_reliability = _link_provenance(
            conn,
            name=name,
            product_id=product_id,
            type=type,
            producer_agent=producer_agent,
            created_by=created_by,
            description=description,
            access_url=access_url,
            confidence=confidence,
            consumers=consumers,
        )
    elif ohm_node_id is None:
        source_reliability = None
    else:
        source_reliability = _seed_reliability(conn, producer_agent, created_by)

    existing = _rows_to_dicts(
        conn.execute(
            "SELECT internal_id FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )
    )
    if existing:
        internal_id = existing[0]["internal_id"]
        conn.execute(
            """UPDATE ohm_data_products SET
                   name = ?, type = ?, visibility = ?, status = ?,
                   value_proposition = ?, description = ?, producer_agent = ?,
                   output_port_type = ?, access_format = ?, access_url = ?,
                   authentication_method = ?, output_file_formats = ?,
                   ohm_node_id = ?, confidence = ?, source_reliability = ?,
                   product_version = ?,
                   odps_yaml = ?, updated = ?, updated_at = CURRENT_TIMESTAMP
               WHERE internal_id = ?""",
            [
                name,
                type,
                visibility,
                status,
                value_proposition,
                description,
                producer_agent,
                output_port_type,
                access_format,
                access_url,
                authentication_method,
                output_file_formats,
                ohm_node_id,
                confidence,
                source_reliability,
                product_version,
                odps_yaml,
                now,
                internal_id,
            ],
        )
        _log_change(conn, "ohm_data_products", internal_id, "UPDATE", created_by)
        rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
        return rows[0]

    internal_id = str(_uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_data_products
           (internal_id, customer_id, product_id, name, language, visibility, status, type,
            value_proposition, description, producer_agent, output_port_type, access_format,
            access_url, authentication_method, output_file_formats, ohm_node_id, confidence,
            source_reliability, product_version, odps_yaml, created_by, created_at, updated_at, created, updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)""",
        [
            internal_id,
            customer_id,
            product_id,
            name,
            language,
            visibility,
            status,
            type,
            value_proposition,
            description,
            producer_agent,
            output_port_type,
            access_format,
            access_url,
            authentication_method,
            output_file_formats,
            ohm_node_id,
            confidence,
            source_reliability,
            product_version,
            odps_yaml,
            created_by,
            now,
            now,
        ],
    )
    _log_change(conn, "ohm_data_products", internal_id, "INSERT", created_by)
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
    return rows[0]


def _seed_reliability(conn: DuckDBPyConnection, producer_agent: str, created_by: str) -> float | None:
    """Seed source_reliability from the producer agent's outcome history."""
    prod_node = find_or_create_node(conn, label=producer_agent, node_type="agent", created_by=created_by)
    try:
        reliability = query_source_reliability(conn, prod_node["id"])
        eff = reliability.get("effective_reliability")
        return eff if eff is not None else None
    except Exception:
        return None


def _link_provenance(
    conn: DuckDBPyConnection,
    *,
    name: str,
    product_id: str,
    type: str,
    producer_agent: str,
    created_by: str,
    description: str | None = None,
    access_url: str | None = None,
    confidence: float | None = None,
    consumers: list[str] | None = None,
) -> tuple[str | None, float | None]:
    """Auto-create OHM provenance node + edges for a data product (OHM-ovwq).

    Returns (ohm_node_id, source_reliability).
    """
    import json as _json

    product_node = find_or_create_node(
        conn,
        label=name,
        node_type="source",
        content=description,
        created_by=created_by,
        provenance="bos-data-product",
        confidence=confidence or 0.7,
        url=access_url,
    )
    ohm_node_id = product_node["id"]

    prod_node = find_or_create_node(conn, label=producer_agent, node_type="agent", created_by=created_by)

    _idempotent_edge(
        conn,
        from_node=prod_node["id"],
        to_node=ohm_node_id,
        layer="L2",
        edge_type="PRODUCES",
        created_by=created_by,
        confidence=confidence or 0.7,
        provenance="bos-data-product",
    )

    if consumers:
        for consumer_label in consumers:
            consumer_node = find_or_create_node(conn, label=consumer_label, node_type="agent", created_by=created_by)
            _idempotent_edge(
                conn,
                from_node=consumer_node["id"],
                to_node=ohm_node_id,
                layer="L2",
                edge_type="CONSUMES",
                created_by=created_by,
                confidence=0.5,
                provenance="bos-data-product",
            )

    source_reliability = _seed_reliability(conn, producer_agent, created_by)
    return ohm_node_id, source_reliability


def _idempotent_edge(
    conn: DuckDBPyConnection,
    *,
    from_node: str,
    to_node: str,
    layer: str,
    edge_type: str,
    created_by: str,
    confidence: float = 0.7,
    provenance: str | None = None,
) -> None:
    """Create an edge only if it doesn't already exist (idempotent)."""
    existing = conn.execute(
        "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = ? AND layer = ? AND created_by = ? AND deleted_at IS NULL",
        [from_node, to_node, edge_type, layer, created_by],
    ).fetchone()
    if not existing:
        create_edge(
            conn,
            from_node=from_node,
            to_node=to_node,
            layer=layer,
            edge_type=edge_type,
            created_by=created_by,
            confidence=confidence,
            provenance=provenance,
        )


def refresh_data_product_provenance(
    conn: DuckDBPyConnection,
    internal_id: str,
) -> dict[str, Any] | None:
    """Refresh source_reliability and confidence for a data product from outcomes (OHM-ovwq).

    Called after recording an outcome against a product's OHM node to update
    the catalog entry with the latest reliability score.
    """
    product = get_data_product(conn, internal_id)
    if not product or not product.get("ohm_node_id"):
        return None

    producer = product.get("producer_agent")
    if not producer:
        return None

    prod_node = _rows_to_dicts(conn.execute("SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent' AND deleted_at IS NULL", [producer]))
    if not prod_node:
        return None

    try:
        reliability = query_source_reliability(conn, prod_node[0]["id"])
        eff = reliability.get("effective_reliability")
    except Exception:
        eff = None

    conn.execute(
        "UPDATE ohm_data_products SET source_reliability = ?, updated_at = CURRENT_TIMESTAMP WHERE internal_id = ?",
        [eff, internal_id],
    )
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
    return rows[0] if rows else None


def get_data_product(conn: DuckDBPyConnection, internal_id: str) -> dict[str, Any] | None:
    """Get a data product by internal_id."""
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ? AND deleted_at IS NULL", [internal_id]))
    return rows[0] if rows else None


def get_data_product_by_odps_id(
    conn: DuckDBPyConnection,
    product_id: str,
    *,
    customer_id: str | None = None,
    language: str = "en",
) -> dict[str, Any] | None:
    """Get a data product by its ODPS product_id (+ tenant + language)."""
    rows = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )
    )
    return rows[0] if rows else None


def list_data_products(
    conn: DuckDBPyConnection,
    *,
    producer_agent: str | None = None,
    type: str | None = None,
    status: str | None = None,
    customer_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List data products with optional filters."""
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if producer_agent:
        conditions.append("producer_agent = ?")
        params.append(producer_agent)
    if type:
        conditions.append("type = ?")
        params.append(type)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if customer_id is not None:
        conditions.append("customer_id IS NOT DISTINCT FROM ?")
        params.append(customer_id)
    where = " WHERE " + " AND ".join(conditions)
    params.append(limit)
    return _rows_to_dicts(conn.execute(f"SELECT * FROM ohm_data_products{where} ORDER BY updated_at DESC LIMIT ?", params))

# OHM-447: Lazy cross-domain imports resolved at access time
_LAZY_IMPORTS = {
    "create_node",
    "create_edge",
    "find_or_create_node",
}

def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import ohm.graph.queries as _q
        return getattr(_q, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

