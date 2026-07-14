"""Data-products Graph mixin (ADR-027 / OHM-ksi0)."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class DataProductsGraphMixin(GraphMixinBase):
    """Register/list data products."""

    def register_data_product(
        self,
        product_id: str,
        name: str,
        type: str,
        *,
        producer_agent: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Register or update an ODPS data product. Returns the full record.

        Upserts on the (customer_id, product_id, language) unique key.
        ``created_by`` is set to this graph's actor.

        Args:
            product_id: ODPS product identifier (unique per customer + language).
            name: Human-readable product name.
            type: Product type (e.g., 'dataset', 'model', 'service').
            producer_agent: Agent that produces this data product.
            **kwargs: Optional fields — customer_id, language, visibility, status,
                value_proposition, description, output_port_type, access_format,
                access_url, authentication_method, output_file_formats, ohm_node_id,
                confidence, product_version, odps_yaml.

        Returns:
            The full data product record.
        """
        from ohm.queries import register_data_product

        return register_data_product(
            self._conn,
            product_id=product_id,
            name=name,
            type=type,
            producer_agent=producer_agent,
            created_by=self.actor,
            **kwargs,
        )

    def get_data_product(self, internal_id: str) -> dict[str, Any] | None:
        """Retrieve a single data product by internal_id, or None if not found."""
        from ohm.queries import get_data_product

        return get_data_product(self._conn, internal_id)

    def list_data_products(self, **kwargs: Any) -> list[dict[str, Any]]:
        """List data products with optional filters.

        Accepts producer_agent, type, status, customer_id, and limit.
        """
        from ohm.queries import list_data_products

        return list_data_products(self._conn, **kwargs)
