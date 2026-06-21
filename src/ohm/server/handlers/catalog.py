from __future__ import annotations


class CatalogHandlerMixin:
    """Handler mixin for BOS ODPS data product catalog endpoints (ADR-027).

    GET  /data-products          — list products (filterable)
    GET  /data-products/{id}     — get single product
    POST /data-products          — register a product (validates ODPS v4.1)
    """

    def _get_data_products(self, path: str, qs: dict) -> None:
        """GET /data-products — list data products with optional filters."""
        producer_agent = qs.get("producer_agent", [None])[0]
        product_type = qs.get("type", [None])[0]
        status = qs.get("status", [None])[0]
        limit = int(qs.get("limit", [100])[0])

        customer_id = self._customer_id if self.multi_tenant else None

        products = self.current_store.list_data_products(
            producer_agent=producer_agent,
            type=product_type,
            status=status,
            customer_id=customer_id,
            limit=limit,
        )
        self._json_response(200, {"products": products, "count": len(products)})

    def _get_data_product(self, path: str, qs: dict) -> None:
        """GET /data-products/{internal_id} — get a single data product."""
        prefix = "/data-products/"
        internal_id = path[len(prefix) :]
        if not internal_id:
            self._json_response(404, {"error": "not_found", "message": "internal_id required"})
            return
        product = self.current_store.get_data_product(internal_id)
        if product is None:
            self._json_response(404, {"error": "not_found", "message": f"Data product '{internal_id}' not found"})
            return
        self._json_response(200, product)

    def _post_data_product(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /data-products — register an ODPS v4.1 data product.

        Body must include either structured fields (product_id, name, type) or
        an odps_yaml document. If odps_yaml is provided, it's validated against
        the ODPS v4.1 schema + BOS constraints before registration.
        """
        from ohm.bos.odps_validation import validate_registration

        odps_yaml = body.get("odps_yaml")
        producer_agent = body.get("producer_agent", agent)
        result: dict | None = None

        if odps_yaml:
            result = validate_registration(odps_yaml, producer_agent=producer_agent)
            if not result["valid"]:
                self._json_response(
                    422,
                    {
                        "error": "validation_failed",
                        "message": "ODPS document failed validation",
                        "errors": result["errors"],
                        "odps_valid": result["odps_valid"],
                        "bos_valid": result["bos_valid"],
                    },
                )
                return

        product = self.current_store.register_data_product(
            product_id=body["product_id"],
            name=body["name"],
            type=body["type"],
            producer_agent=producer_agent,
            customer_id=self._customer_id if self.multi_tenant else None,
            visibility=body.get("visibility", "private"),
            status=body.get("status", "draft"),
            value_proposition=body.get("value_proposition"),
            description=body.get("description"),
            output_port_type=body.get("output_port_type"),
            access_format=body.get("access_format"),
            access_url=body.get("access_url"),
            authentication_method=body.get("authentication_method"),
            output_file_formats=body.get("output_file_formats"),
            confidence=body.get("confidence"),
            product_version=body.get("product_version"),
            odps_yaml=odps_yaml,
            consumers=body.get("consumers"),
            agent_name=agent,
        )

        if product is None:
            self._json_response(500, {"error": "internal_error", "message": "Registration failed"})
            return

        response = dict(product)
        if odps_yaml and result:
            response["compliance_level"] = result.get("compliance_level")
        self._json_response(201, response)
