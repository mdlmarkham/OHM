"""Search handler mixin."""

from __future__ import annotations

import logging

from ohm.server.handlers._base import OhmHandlerBase

logger = logging.getLogger(__name__)


class SearchHandlerMixin(OhmHandlerBase):
    """Handler mixin for search handler mixin."""

    def _get_search(self, path: str, qs: dict) -> None:
        """GET /search — text search over nodes.

        OHM-a5rz.7: supports ``?since=`` and ``?until=`` ISO 8601 timestamp
        filters to constrain search by ``created_at`` range.

        OHM-a5rz.18: L0 fragments are excluded by default.
        Pass ``?include_l0=true`` to include fragment-type nodes.

        OHM-842: supports ``?tags=`` for AND-semantics tag filtering.
        Pass multiple ``?tags=`` params — all must be present.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        since = qs.get("since", [None])[0]
        until = qs.get("until", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        limit = int(qs.get("limit", [20])[0])
        tags = qs.get("tags", [])
        if not query_text:
            raise ValidationError("Search requires ?q=QUERY")
        conditions = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
        params = [f"%{query_text}%", f"%{query_text}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        elif not include_l0:
            # OHM-a5rz.18: exclude L0 fragments from default search results
            conditions.append("type != 'fragment'")
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if since:
            conditions.append("created_at >= ?::TIMESTAMP")
            params.append(since)
        if until:
            conditions.append("created_at <= ?::TIMESTAMP")
            params.append(until)
        # OHM-842: AND-semantics tag filtering — each tag must be present
        for tag in tags:
            conditions.append("json_contains(tags, ?)")
            params.append(f'"{tag}"')
        # OHM-oqyc: enforce read scope at SQL level
        from ohm.server.boundary import apply_read_scope_filters

        agent = getattr(self, "_current_agent", "ohm")
        scope_conds, scope_params = apply_read_scope_filters(self.current_store.conn, agent)
        conditions.extend(scope_conds)
        params.extend(scope_params)
        params.append(limit)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)

        # OHM-tr71.8: Automatic semantic fallback on empty text search
        # When text search returns 0 results, try semantic search automatically.
        # OHM-738: pass node_type through to fallbacks so a typed query can
        # still benefit from semantic/fuzzy matching instead of returning 0.
        # OHM-842: skip fallbacks when tags are specified — fallbacks don't
        # support tag filtering and would bypass the user's explicit constraint.
        if not results and not tags:
            try:
                from ohm.graph.queries import semantic_search

                semantic_results = semantic_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    node_type=node_type,
                    include_l0=include_l0,
                )
                if semantic_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("node_id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": round(r.get("distance", 1.0), 4),
                                    "match_method": "semantic",
                                }
                                for r in semantic_results
                            ],
                            "count": len(semantic_results),
                            "fallback": "semantic",
                            "tip": f"No exact text matches for '{query_text}'. Showing semantic matches instead. Use /semantic_search?q={query_text} for more options.",
                        },
                    )
                    return
            except (ValueError, ImportError, Exception) as e:
                logger.debug(f"Semantic fallback failed: {e}")

            # OHM-tr71.9: Fuzzy matching fallback — try DuckDB jaro_winkler_similarity
            try:
                from ohm.graph.queries import fuzzy_search as _fuzzy_search

                fuzzy_results = _fuzzy_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    include_l0=include_l0,
                )
                if node_type:
                    fuzzy_results = [r for r in fuzzy_results if r.get("type") == node_type]
                if fuzzy_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": r.get("distance", 0.0),
                                    "match_method": r.get("match_type", "fuzzy"),
                                }
                                for r in fuzzy_results
                            ],
                            "count": len(fuzzy_results),
                            "fallback": "fuzzy",
                            "tip": f"No exact matches for '{query_text}'. Showing fuzzy label matches instead.",
                        },
                    )
                    return
            except Exception as e:
                logger.debug(f"Fuzzy fallback failed: {e}")

            self._json_response(
                200,
                {
                    "results": [],
                    "count": 0,
                    "tip": f"No results for '{query_text}' via text, semantic, or fuzzy search. Try a different query.",
                },
            )
            return

        self._json_response(200, results)

    def _get_semantic_search(self, path: str, qs: dict) -> None:
        """GET /semantic_search — vector similarity search.

        OHM-a5rz.20: L0 fragments excluded by default. Pass ``?include_l0=true`` to include.
        OHM-xuf4: Pass ``?membership_weight=0.3`` to blend HD Hamming similarity
        alongside cosine similarity. Results then carry ``cosine_similarity``,
        ``hd_similarity``, and ``blended_score`` fields.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        membership_weight_raw = qs.get("membership_weight", [None])[0]
        membership_weight: float | None = None
        if membership_weight_raw is not None:
            try:
                membership_weight = float(membership_weight_raw)
            except ValueError:
                raise ValidationError("?membership_weight must be a number in [0, 1]")
            if not 0.0 <= membership_weight <= 1.0:
                raise ValidationError("?membership_weight must be in [0, 1]")
        if min_confidence is not None:
            try:
                min_confidence = float(min_confidence)
            except ValueError:
                raise ValidationError("?min_confidence must be a number")
        try:
            from ohm.queries import semantic_search

            results = semantic_search(
                self.current_store.conn,
                query=query_text,
                limit=limit,
                node_type=node_type,
                min_confidence=min_confidence,
                include_l0=include_l0,
                membership_weight=membership_weight,
            )
            # OHM-oqyc: post-filter results by read scope
            from ohm.server.boundary import filter_results_by_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            results = filter_results_by_read_scope(
                self.current_store.conn,
                agent,
                results,
                id_field="node_id",
            )
            self._json_response(200, {"results": results, "count": len(results)})
        except ValueError as e:
            self._json_response(
                503,
                {
                    "error": "service_unavailable",
                    "message": str(e),
                },
            )

