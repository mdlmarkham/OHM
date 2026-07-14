"""Graph handler mixin — node/edge CRUD, search, observations, webhooks, and agent state."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin, _resolve_type_field

import logging
import time
from typing import Any

from ohm.server import suggestions as _suggestions_module

logger = logging.getLogger(__name__)

from ohm.framework.exceptions import NodeNotFoundError, AuthenticationError
from ohm.server import server as _server_module
from ohm.server.nudges import generate_nudges, enrich_response

class GraphHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for graph CRUD endpoints (OHM-hpxa).

    Methods migrated from server.py: 38 handler methods covering node/edge
    read/write/delete, search, observations, agent registration, webhooks,
    and batch operations.
    """

    _challenge_ratio_cache: float = 0.0
    _challenge_ratio_cache_time: float = 0.0

    def _get_challenge_ratio(self) -> float:
        """Get the current graph challenge ratio, cached for 5 minutes."""
        import time

        now = time.time()
        if now - self._challenge_ratio_cache_time > 300:  # 5-minute cache
            try:
                row = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL").fetchone()
                challenged = row[0] if row else 0
                row2 = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()
                total_l3 = row2[0] if row2 else 1
                ratio = challenged / max(total_l3, 1)
                GraphHandlerMixin._challenge_ratio_cache = ratio
                GraphHandlerMixin._challenge_ratio_cache_time = now
            except Exception:
                ratio = GraphHandlerMixin._challenge_ratio_cache
        else:
            ratio = GraphHandlerMixin._challenge_ratio_cache
        return ratio

    def _get_listen(self, path: str, qs: dict) -> None:
        """GET /listen — poll change feed since last sync."""
        from ohm.exceptions import AuthenticationError
        from datetime import datetime, timedelta, timezone

        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required — provide Bearer token"
                )
            else:
                agent = "ohm"
        since = qs.get("since", [None])[0]
        agent_name = qs.get("agent", [agent or "ohm"])[0]
        enrich = qs.get("enrich", ["false"])[0].lower() == "true"
        if not since:
            state = self.current_store.get_agent_state(agent_name)
            if state and state.get("last_sync"):
                since = state["last_sync"]
                if isinstance(since, datetime):
                    since = since.isoformat()
            else:
                since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        from ohm.queries import query_change_feed

        results = query_change_feed(self.current_store.conn, since=since, agent_name=agent_name, enrich=enrich)
        self._json_response(200, results)

    def _post_scratch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /scratch — write an L0 thinking fragment (OHM-a5rz.4).

        Minimal write: just content. Auto-generates id, label (first 80 chars),
        type='fragment'. Extracts URLs from content. Returns 201.
        """
        from ohm.queries import scratch

        content = body.get("content", "").strip()
        if not content:
            self._json_response(400, {"error": "content is required and must be non-empty"})
            return

        try:
            node = scratch(
                self.current_store.conn,
                content=content,
                created_by=agent,
                tags=body.get("tags"),
                connects_to=body.get("connects_to"),
                metadata=body.get("metadata"),
            )
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        decorations = self._run_post_ingest_hooks(agent, "scratch", node)
        if decorations:
            node["hook_decorations"] = decorations

        # ADR-021: Proactive discoverability — suggestions for scratch
        # OHM-855: isolate suggestion failures from fragment writes
        if _suggestions_module._suggestions_enabled():
            deadline = time.time() + _suggestions_module.SUGGESTION_TIMEOUT_S
            try:
                sugg = _suggestions_module.generate_suggestions(
                    store=self.current_store,
                    node_id=node.get("id", ""),
                    content=content,
                    label=node.get("label"),
                    tags=body.get("tags"),
                    node_type="fragment",
                    has_edges=bool(body.get("connects_to")),
                    deadline=deadline,
                    use_store_conn=True,
                )
                node["suggestions"] = sugg
            except Exception as e:
                logger.debug("Edge suggestions failed: %s", e)

        self._json_response(201, node)

    def _post_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /outcome — record whether a source agent's claim was correct."""
        from ohm.exceptions import ValidationError

        source_agent = body.get("source_agent")
        claim_node = body.get("claim_node")
        outcome = body.get("outcome")
        notes = body.get("notes")
        if not source_agent or not claim_node or outcome is None:
            raise ValidationError("outcome requires source_agent, claim_node, and outcome fields")
        from ohm.queries import query_record_outcome

        result = query_record_outcome(
            self.current_store.conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=bool(outcome),
            recorded_by=agent,
            notes=notes,
        )
        self._json_response(201, result)

    def _post_skill(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /skill — Create a portable skill node (OHM-461f).

        Body:
            label (required): Human-readable skill name
            trigger (required): When this skill activates
            scope (optional): personal (default), project, or universal
            required_tools (optional): List of tool names
            boundaries (optional): Constraints on what the skill does
            output_format (optional): Expected output format
            verification_evidence (optional): List of evidence types
            connects_to (optional): List of existing node IDs to link
        """
        from ohm.queries import create_skill
        from ohm.exceptions import ValidationError

        label = body.get("label")
        trigger = body.get("trigger")
        if not label or not trigger:
            raise ValidationError("label and trigger are required")

        skill = create_skill(
            self.current_store.conn,
            label=label,
            trigger=trigger,
            scope=body.get("scope", "personal"),
            required_tools=body.get("required_tools", []),
            boundaries=body.get("boundaries"),
            output_format=body.get("output_format"),
            verification_evidence=body.get("verification_evidence", []),
            connects_to=body.get("connects_to", []),
            created_by=agent,
        )
        self._json_response(201, skill)

    def _post_runbook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /runbook — Create an ordered chain of skills (OHM-461f).

        Body:
            label (required): Human-readable runbook name
            skill_ids (required): Ordered list of existing skill node IDs
            description (optional): Free-text description
        """
        from ohm.queries import create_runbook
        from ohm.exceptions import ValidationError

        label = body.get("label")
        skill_ids = body.get("skill_ids", [])
        if not label:
            raise ValidationError("label is required")
        if not skill_ids or not isinstance(skill_ids, list):
            raise ValidationError("skill_ids must be a non-empty list")

        runbook = create_runbook(
            self.current_store.conn,
            label=label,
            skill_ids=skill_ids,
            description=body.get("description"),
            created_by=agent,
        )
        self._json_response(201, runbook)

    def _get_runbook_steps(self, path: str, qs: dict) -> None:
        """GET /runbook/{id}/steps — Get ordered skills in a runbook (OHM-461f)."""
        from ohm.queries import get_runbook_steps
        from ohm.exceptions import NodeNotFoundError, ValidationError

        prefix = "/runbook/"
        suffix = "/steps"
        if not path.endswith(suffix):
            raise ValidationError("Path must end with /steps")
        runbook_id = path[len(prefix) : -len(suffix)]
        if not runbook_id:
            raise ValidationError("runbook_id is required")

        try:
            result = get_runbook_steps(self.current_store.conn, runbook_id=runbook_id)
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _get_vault(self, path: str, qs: dict) -> None:
        """GET /vault — list vault contents for the authenticated agent (OHM-cuu0).

        Returns nodes with ``visibility='vault'`` created by the authenticated
        agent, plus any edges attached to those nodes.
        """
        agent = self._authenticate()
        if agent is None:
            if self.no_auth:
                agent = "ohm"
            else:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required"
                )
        nodes = self.current_store.execute(
            "SELECT * FROM ohm_nodes WHERE visibility = 'vault' AND created_by = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100",
            [agent],
        )
        node_ids = [n["id"] for n in nodes]
        edges: list = []
        if node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            edges = self.current_store.execute(
                f"SELECT * FROM ohm_edges WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders})) AND deleted_at IS NULL",
                node_ids + node_ids,
            )
        self._json_response(200, {"agent": agent, "nodes": nodes, "edges": edges, "count": len(nodes)})

    def _post_vault_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /vault/promote — promote a vault node to the shared graph (OHM-cuu0).

        Changes ``visibility`` from ``vault`` to ``team`` for the given node
        and its edges (if any). Only the owning agent can promote their own
        vault content.

        Body: {"node_id": "<node_id>"}
        """
        node_id = body.get("node_id", "")
        if not node_id:
            self._json_response(400, {"error": "validation_error", "message": "node_id is required"})
            return

        node = self.current_store.conn.execute(
            "SELECT id, visibility, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if not node:
            self._json_response(404, {"error": "not_found", "message": f"Node not found: {node_id}"})
            return
        nid, vis, creator = node

        if vis != "vault":
            self._json_response(400, {"error": "validation_error", "message": f"Node {node_id} has visibility '{vis}', not 'vault'"})
            return

        # OHM-tjzh: promotion requires at least one cross-link to shared graph
        from ohm.schema import requires_cross_link

        if requires_cross_link(node["type"] if len(node) > 3 else "concept"):
            edge_count = self.current_store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
                [node_id, node_id],
            ).fetchone()[0]
            if edge_count == 0:
                self._json_response(
                    422,
                    {
                        "error": "cross_link_required",
                        "message": f"Vault node '{node_id}' has no edges. Per ADR-018 / OHM-tjzh, nodes must have at least one edge before promotion to the shared graph.",
                        "hint": "Add an edge to an existing shared-graph node via POST /edge, then retry promotion.",
                    },
                )
                return

        now = self.current_store._now()
        self.current_store.conn.execute(
            "UPDATE ohm_nodes SET visibility = 'team', updated_at = ?, updated_by = ? WHERE id = ?",
            [now, agent, node_id],
        )
        # Also promote related edges
        self.current_store.conn.execute(
            "UPDATE ohm_edges SET updated_at = ?, updated_by = ? WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
            [now, agent, node_id, node_id],
        )

        self._json_response(
            200,
            {
                "promoted": node_id,
                "previous_visibility": "vault",
                "new_visibility": "team",
                "promoted_by": agent,
            },
        )

    def _post_ask_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /ask — conversational analytics: natural language question → synthesized insights.

        Converts OHM's AND-gate interface into an OR-gate by orchestrating
        search, neighborhood expansion, Bayesian inference, and challenge
        checking into a single structured response.

        Pipeline:
          1. Node search (text + semantic) to find relevant nodes
          2. Neighborhood expansion for top matches
          3. Bayesian inference on causal chains (optional)
          4. Challenge check for relevant edges
          5. Synthesis combining all results

        Input:
          question (required): Natural language question
          agent: Requesting agent name (defaults to authenticated agent)
          depth: Neighborhood depth, 1-3 (default 2)
          include_inference: Run Bayesian inference (default true)
          limit: Max search results per method (default 5)
        """
        from ohm.queries import search, semantic_search, query_neighborhood
        from ohm.bayesian import bayesian_inference, PGMPY_AVAILABLE
        from ohm.validation import validate_identifier

        question = body.get("question", "").strip()
        if not question:
            self._json_response(400, {"error": "missing_parameter", "message": "'question' is required"})
            return

        depth = min(max(int(body.get("depth", 2)), 1), 3)
        include_inference = body.get("include_inference", True)
        limit = min(max(int(body.get("limit", 5)), 1), 20)
        # Step 1: Node search — text + semantic
        matched_nodes = []
        search_errors = []

        # Direct node ID lookup — if the question contains a known node ID, use it
        question_lower = question.lower().replace(" ", "_").replace("-", "_")
        try:
            # Check if question matches an existing node ID directly
            direct_node = self.current_store.get_node(question_lower)
            if direct_node:
                matched_nodes.append(
                    {
                        "id": direct_node["id"],
                        "label": direct_node.get("label", ""),
                        "type": direct_node.get("type", ""),
                        "confidence": direct_node.get("confidence"),
                        "match_method": "direct_id",
                    }
                )
        except Exception:
            pass

        # Also try common variations (hormuz and gate → hormuz_and_gate)
        if not matched_nodes:
            for variant in [question_lower, question_lower.replace(" and ", "_and_").replace(" ", "_")]:
                try:
                    node = self.current_store.get_node(variant)
                    if node and node["id"] not in {n["id"] for n in matched_nodes}:
                        matched_nodes.append(
                            {
                                "id": node["id"],
                                "label": node.get("label", ""),
                                "type": node.get("type", ""),
                                "confidence": node.get("confidence"),
                                "match_method": "direct_id",
                            }
                        )
                        break
                except Exception:
                    pass

        # Text search
        try:
            text_results = search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            for r in text_results:
                matched_nodes.append(
                    {
                        "id": r.get("id", ""),
                        "label": r.get("label", ""),
                        "type": r.get("type", ""),
                        "confidence": r.get("confidence"),
                        "match_method": "text",
                    }
                )
        except Exception as e:
            search_errors.append(f"text_search: {e}")

        # Semantic search
        try:
            sem_results = semantic_search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            # Merge: add semantic results that aren't already in matched_nodes
            existing_ids = {n["id"] for n in matched_nodes}
            for r in sem_results:
                nid = r.get("node_id", r.get("id", ""))
                if nid and nid not in existing_ids:
                    matched_nodes.append(
                        {
                            "id": nid,
                            "label": r.get("label", ""),
                            "type": r.get("type", ""),
                            "confidence": r.get("confidence"),
                            "distance": r.get("distance"),
                            "match_method": "semantic",
                        }
                    )
                    existing_ids.add(nid)
        except Exception as e:
            # Semantic search may be unavailable (no Ollama)
            search_errors.append(f"semantic_search: {e}")

        # Fuzzy search fallback
        if not matched_nodes:
            try:
                from ohm.graph.queries import fuzzy_search

                fuzzy_results = fuzzy_search(
                    self.current_store.conn,
                    query=question,
                    limit=limit,
                )
                existing_ids = {n["id"] for n in matched_nodes}
                for r in fuzzy_results:
                    nid = r.get("id", "")
                    if nid and nid not in existing_ids:
                        matched_nodes.append(
                            {
                                "id": nid,
                                "label": r.get("label", ""),
                                "type": r.get("type", ""),
                                "confidence": r.get("confidence"),
                                "distance": r.get("distance"),
                                "match_method": r.get("match_type", "fuzzy"),
                            }
                        )
                        existing_ids.add(nid)
            except Exception as e:
                search_errors.append(f"fuzzy_search: {e}")

        # Step 2: Neighborhood expansion for top matches
        all_node_ids = set()
        all_edges = []
        node_details = []
        for node in matched_nodes[:limit]:
            nid = node["id"]
            if not nid:
                continue
            all_node_ids.add(nid)
            try:
                n_edges = query_neighborhood(
                    self.current_store.conn,
                    nid,
                    depth=depth,
                )
                for edge in n_edges:
                    all_node_ids.add(edge.get("from_node", edge.get("from", "")))
                    all_node_ids.add(edge.get("to_node", edge.get("to", "")))
                    all_edges.append(edge)
            except Exception:
                pass

        # Fetch node details for all discovered nodes
        if all_node_ids:
            placeholders = ",".join(["?"] * len(all_node_ids))
            node_details = self.current_store.execute(
                f"SELECT id, label, type, confidence, content, tags, created_by, provenance FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(all_node_ids),
            )

        # Step 3: Bayesian inference on causal chains
        inference_results = {}
        inference_errors = []
        if include_inference and PGMPY_AVAILABLE and matched_nodes:
            # Find nodes with causal edges (CAUSES, DEPENDS_ON, THREATENS, NEGATES)
            target_ids = [n["id"] for n in matched_nodes if n.get("id")]
            if target_ids:
                placeholders = ",".join(["?"] * len(target_ids))
                # Find causal edges involving our matched nodes
                causal_edges = self.current_store.execute(
                    f"""SELECT DISTINCT from_node, to_node, edge_type, confidence, probability
                       FROM ohm_edges
                       WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders}))
                         AND edge_type IN ('CAUSES', 'DEPENDS_ON', 'THREATENS', 'NEGATES')
                         AND deleted_at IS NULL
                       LIMIT 50""",
                    target_ids + target_ids,
                )

                if causal_edges:
                    # Build evidence from observed nodes (high-confidence observations)
                    # OHM-w1iv.2: batch the latest probability observation for all targets.
                    if target_ids:
                        placeholders = ",".join(["?"] * len(target_ids))
                        obs_rows = self.current_store.execute(
                            f"""SELECT node_id, value FROM (
                                SELECT node_id, value,
                                    ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY created_at DESC) AS rn
                                FROM ohm_observations
                                WHERE node_id IN ({placeholders})
                                  AND type = 'probability'
                                  AND deleted_at IS NULL
                            ) WHERE rn = 1""",
                            target_ids,
                        )
                        evidence = {}
                        for row in obs_rows:
                            try:
                                val = float(row["value"])
                                if 0.0 <= val <= 1.0:
                                    evidence[row["node_id"]] = 1 if val >= 0.5 else 0
                            except (ValueError, TypeError, KeyError):
                                pass

                    # Run inference on each matched node that has causal connections
                    for target_id in target_ids[:3]:  # Limit to top 3 to avoid timeouts
                        try:
                            target_safe = validate_identifier(target_id, name="target")
                            result = bayesian_inference(
                                self.current_store.conn,
                                target_safe,
                                evidence,
                                customer_id=self._customer_id,
                            )
                            if "error" not in result:
                                # ADR-025: Extract only posteriors, not full network info
                                posterior = result.get("posterior", result)
                                network_info = result.get("network_info", {})
                                inference_results[target_safe] = {
                                    "posterior": posterior,
                                    "n_nodes": network_info.get("n_nodes", 0),
                                    "n_edges": network_info.get("n_edges", 0),
                                    "method": result.get("method", "bayesian_variable_elimination"),
                                }
                        except Exception as e:
                            inference_errors.append(f"inference({target_id}): {e}")

        # Step 4: Challenge check for relevant edges
        challenges = []
        challenge_node_ids = list(all_node_ids)[:50]  # Limit to prevent runaway queries
        if challenge_node_ids:
            placeholders = ",".join(["?"] * len(challenge_node_ids))
            challenge_edges = self.current_store.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                          e.challenge_type, e.provenance, e.created_by,
                          n_from.label AS from_label, n_to.label AS to_label
                   FROM ohm_edges e
                   LEFT JOIN ohm_nodes n_from ON e.from_node = n_from.id
                   LEFT JOIN ohm_nodes n_to ON e.to_node = n_to.id
                   WHERE e.edge_type = 'CHALLENGED_BY'
                     AND (e.to_node IN ({placeholders}) OR e.from_node IN ({placeholders}))
                     AND e.deleted_at IS NULL
                   LIMIT 20""",
                challenge_node_ids + challenge_node_ids,
            )
            for ce in challenge_edges:
                challenges.append(
                    {
                        "edge_id": ce.get("id"),
                        "challenger_node": ce.get("from_node"),
                        "challenged_node": ce.get("to_node"),
                        "challenger_label": ce.get("from_label", ""),
                        "challenged_label": ce.get("to_label", ""),
                        "challenge_type": ce.get("challenge_type"),
                        "confidence": ce.get("confidence"),
                        "provenance": ce.get("provenance"),
                        "created_by": ce.get("created_by"),
                    }
                )

        # Step 5: Build synthesis
        # Confidence based on: search match quality + inference certainty + challenge coverage
        confidence = 0.5
        match_count = len(matched_nodes)
        if match_count >= 3:
            confidence += 0.15
        elif match_count >= 1:
            confidence += 0.1

        # Boost if semantic matches are close
        semantic_matches = [n for n in matched_nodes if n.get("match_method") == "semantic"]
        if semantic_matches:
            min_dist = min((n.get("distance", 1.0) for n in semantic_matches), default=1.0)
            if min_dist < 0.3:
                confidence += 0.1
            elif min_dist < 0.5:
                confidence += 0.05

        # Boost if inference converged
        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    max_prob = max(posterior.get("good", 0), posterior.get("bad", 0))
                    confidence += 0.1 * max_prob  # Higher certainty → more confidence

        # Reduce if challenges exist on key edges
        if challenges:
            challenge_count = len(challenges)
            confidence -= 0.05 * min(challenge_count, 3)

        confidence = max(0.1, min(1.0, round(confidence, 2)))

        # Build synthesis text from gathered context
        synthesis_parts = []

        if matched_nodes:
            node_labels = [f"{n['label']} ({n['id']})" for n in matched_nodes[:5] if n.get("label")]
            if node_labels:
                synthesis_parts.append(f"Relevant nodes: {', '.join(node_labels)}.")

        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    p_good = posterior.get("good", 0)
                    p_bad = posterior.get("bad", 0)
                    synthesis_parts.append(f"Bayesian inference on {target_id}: P(good)={p_good:.2%}, P(bad)={p_bad:.2%}.")

        if challenges:
            challenge_descs = []
            for c in challenges[:3]:
                cdesc = f"{c.get('challenger_label', c.get('challenger_node', '?'))} challenges {c.get('challenged_label', c.get('challenged_node', '?'))}"
                if c.get("challenge_type"):
                    cdesc += f" ({c['challenge_type']})"
                challenge_descs.append(cdesc)
            synthesis_parts.append(f"Active challenges: {'; '.join(challenge_descs)}.")

        if not synthesis_parts:
            synthesis_parts.append(f"No matching nodes or inference results found for '{question}'.")

        synthesis = " ".join(synthesis_parts)

        # Source node IDs for traceability
        sources = list({n["id"] for n in matched_nodes if n.get("id")})[:20]

        response = {
            "question": question,
            "matched_nodes": matched_nodes[:20],
            "neighborhood": {
                "nodes": node_details[:50],
                "edges": all_edges[:100],
            },
            "inference_results": inference_results,
            "challenges": challenges,
            "synthesis": synthesis,
            "confidence": confidence,
            "sources": sources,
        }

        if inference_errors:
            response["inference_errors"] = inference_errors
        if search_errors:
            response["search_errors"] = search_errors
        if not PGMPY_AVAILABLE:
            response["inference_skipped"] = True
            response["inference_reason"] = "pgmpy not available"
        if not include_inference:
            response["inference_skipped"] = True
            response["inference_reason"] = "include_inference=false"

        self._json_response(200, response)
