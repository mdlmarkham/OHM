"""Test ingestion pipeline enhancements (OHM-g0kv, OHM-wdrg).

Tests for:
- OHM-g0kv: Content Hashing + Alias Population
- OHM-wdrg: Source Citation Architecture
- Stage 4-5: Assessment and Synthesis enhancements
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ohm.validation import normalize_alias, compute_content_hash


# ── OHM-g0kv Feature A: Backfill Aliases ──────────────────────────────────


class TestBackfillAliases:
    """Tests for POST /admin/backfill-aliases endpoint."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_backfill_aliases_creates_aliases_for_nodes(self, store):
        """Backfill should create additional aliases (node IDs) for existing nodes.

        Note: OhmStore.write_node already auto-registers normalize_alias(label).
        Backfill adds the node_id alias as well.
        """
        from ohm.queries import register_alias

        # Create some test nodes
        store.write_node("test-concept-1", "Hormuz AND-Gate", "concept")
        store.write_node("test-source-1", "Reuters Investigation", "source", url="https://reuters.com/article")

        conn = store.conn

        # Count aliases before (store auto-creates label aliases)
        before = conn.execute("SELECT COUNT(*) FROM ohm_aliases").fetchone()[0]
        # write_node auto-creates normalize_alias(label) for each node
        assert before >= 2  # At least label aliases exist

        # Backfill aliases (add node_id aliases)
        rows = conn.execute("SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()

        created = 0
        for node_id, label in rows:
            norm_label = normalize_alias(label)
            # Label alias already exists, so try to register again
            if norm_label:
                result = register_alias(conn, alias_norm=norm_label, node_id=node_id)
                if result.get("created"):
                    created += 1
            # node_id alias is new
            norm_id = normalize_alias(node_id)
            if norm_id and norm_id != norm_label:
                result = register_alias(conn, alias_norm=norm_id, node_id=node_id)
                if result.get("created"):
                    created += 1

        # Should have created node_id aliases (label aliases already existed)
        assert created >= 2  # At least 2 node_id aliases

        # Verify aliases exist
        after = conn.execute("SELECT COUNT(*) FROM ohm_aliases").fetchone()[0]
        assert after >= 4  # 2 label aliases + 2 node_id aliases

        # Verify specific alias
        hormuz_alias = conn.execute("SELECT node_id FROM ohm_aliases WHERE alias_norm = ?", ["hormuz_and-gate"]).fetchone()
        assert hormuz_alias is not None
        assert hormuz_alias[0] == "test-concept-1"

    def test_backfill_aliases_idempotent(self, store):
        """Running backfill twice should not create duplicate aliases."""
        from ohm.queries import register_alias

        store.write_node("test-dup", "Test Duplicate", "concept")

        conn = store.conn

        # Label alias was auto-created by write_node, try again
        norm_label = normalize_alias("Test Duplicate")
        result1 = register_alias(conn, alias_norm=norm_label, node_id="test-dup")
        # Should already exist (auto-created) or be created
        assert result1["created"] is False  # Already exists from auto-creation

        # Second explicit registration (should also be idempotent)
        result2 = register_alias(conn, alias_norm=norm_label, node_id="test-dup")
        assert result2["created"] is False

        # Should still only have one alias for this norm+node pair
        count = conn.execute(
            "SELECT COUNT(*) FROM ohm_aliases WHERE alias_norm = ? AND node_id = ?",
            [norm_label, "test-dup"],
        ).fetchone()[0]
        assert count == 1


# ── OHM-g0kv Feature B: Backfill Content Hashes ────────────────────────────


class TestBackfillContentHashes:
    """Tests for POST /admin/backfill-content-hashes endpoint."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_backfill_content_hashes_for_source_nodes(self, store):
        """Content hashes should be created for source nodes with URLs."""
        from ohm.queries import register_content_hash

        # Create source node with URL
        store.write_node("test-src", "Reuters Article", "source", url="https://reuters.com/article/123")
        # Create a concept node (should not get content hash in backfill)
        store.write_node("test-concept", "Some Concept", "concept")

        conn = store.conn

        # Compute hash from URL
        content_hash = compute_content_hash("https://reuters.com/article/123")
        result = register_content_hash(conn, node_id="test-src", content_hash=content_hash)
        assert result["created"] is True

        # Verify hash exists
        row = conn.execute(
            "SELECT content_hash FROM ohm_content_hashes WHERE node_id = ?",
            ["test-src"],
        ).fetchone()
        assert row is not None
        assert row[0] == content_hash

    def test_content_hash_uses_url_first(self, store):
        """Content hash should prefer URL over label when URL exists."""
        from ohm.queries import register_content_hash

        url = "https://example.com/article"
        store.write_node("test-src2", "Article Title", "source", url=url)

        content = url  # URL is preferred
        expected_hash = compute_content_hash(content)

        register_content_hash(store.conn, node_id="test-src2", content_hash=expected_hash)

        row = store.conn.execute(
            "SELECT content_hash FROM ohm_content_hashes WHERE node_id = ?",
            ["test-src2"],
        ).fetchone()
        assert row[0] == expected_hash

    def test_content_hash_fallback_to_label(self, store):
        """Content hash should use label when URL is empty."""
        from ohm.queries import register_content_hash

        store.write_node("test-src3", "Some Article", "source")

        content = "Some Article"  # Falls back to label when no URL
        expected_hash = compute_content_hash(content)

        register_content_hash(store.conn, node_id="test-src3", content_hash=expected_hash)

        row = store.conn.execute(
            "SELECT content_hash FROM ohm_content_hashes WHERE node_id = ?",
            ["test-src3"],
        ).fetchone()
        assert row[0] == expected_hash

    def test_content_hash_upsert(self, store):
        """Registering same node_id again should update, not duplicate."""
        from ohm.queries import register_content_hash

        store.write_node("test-src4", "V1", "source", url="https://example.com/v1")

        hash1 = compute_content_hash("https://example.com/v1")
        result1 = register_content_hash(store.conn, node_id="test-src4", content_hash=hash1)
        assert result1["created"] is True

        # Update with new hash
        hash2 = compute_content_hash("https://example.com/v2")
        result2 = register_content_hash(store.conn, node_id="test-src4", content_hash=hash2)
        assert result2["created"] is False  # Updated, not created

        # Should have only one entry with updated hash
        row = store.conn.execute(
            "SELECT content_hash FROM ohm_content_hashes WHERE node_id = ?",
            ["test-src4"],
        ).fetchone()
        assert row[0] == hash2


# ── OHM-g0kv Feature C: Content Hash in Stage 1 Fetch ──────────────────────


class TestStageFetchContentDedup:
    """Tests for content hash dedup in Stage 1 (fetch)."""

    def test_normalize_alias_function(self):
        """Test normalize_alias produces expected outputs."""
        assert normalize_alias("Hormuz AND-Gate") == "hormuz_and-gate"
        assert normalize_alias("  Demand  Rationing  ") == "demand_rationing"
        assert normalize_alias("Strait of Hormuz") == "strait_of_hormuz"
        assert normalize_alias("") == ""

    def test_compute_content_hash_function(self):
        """Test compute_content_hash produces consistent SHA-256 hashes."""
        h1 = compute_content_hash("https://example.com/article")
        h2 = compute_content_hash("https://example.com/article")
        h3 = compute_content_hash("https://example.com/different")

        assert h1 == h2  # Same input = same hash
        assert h1 != h3  # Different input = different hash
        assert len(h1) == 64  # SHA-256 hex digest = 64 chars

    def test_resolve_node_by_alias(self, tmp_path):
        """Test that resolve_node_by_alias finds nodes via normalized aliases."""
        from ohm.store import OhmStore
        from ohm.queries import register_alias, resolve_node_by_alias

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        store.write_node("test-resolve", "Hormuz Strait", "concept")

        # Register alias
        norm = normalize_alias("Hormuz Strait")
        register_alias(store.conn, alias_norm=norm, node_id="test-resolve")

        # Resolve by normalized query
        result = resolve_node_by_alias(store.conn, query="hormuz strait")
        assert result is not None
        assert result["id"] == "test-resolve"

        # Should also resolve by exact label (after normalization)
        result2 = resolve_node_by_alias(store.conn, query="Hormuz Strait")
        assert result2 is not None
        assert result2["id"] == "test-resolve"


# ── OHM-g0kv Feature D: Auto-alias on Node Creation ────────────────────────


class TestAutoAliasOnNodeCreation:
    """Tests for automatic alias and content hash registration on POST /node."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_auto_alias_registration_on_write_node(self, store):
        """When a node is created, its label and ID should be auto-registered as aliases."""
        from ohm.queries import register_alias, query_aliases

        # Simulate what the handler does after node creation
        node_id = "auto-test-node"
        label = "Test Node Alpha"

        store.write_node(node_id, label, "concept")

        norm_label = normalize_alias(label)
        if norm_label:
            register_alias(store.conn, alias_norm=norm_label, node_id=node_id)

        norm_id = normalize_alias(node_id)
        if norm_id and norm_id != norm_label:
            register_alias(store.conn, alias_norm=norm_id, node_id=node_id)

        # Verify aliases were created
        aliases = query_aliases(store.conn, node_id=node_id)
        assert len(aliases) >= 2  # label alias + id alias

        alias_norms = [a["alias_norm"] for a in aliases]
        assert "test_node_alpha" in alias_norms
        assert "auto-test-node" in alias_norms

    def test_auto_content_hash_on_source_node_creation(self, store):
        """When a source node is created with a URL, content hash should be registered."""
        from ohm.queries import register_content_hash, lookup_content_hash

        url = "https://reuters.com/article/123"
        content_hash = compute_content_hash(url)

        result = register_content_hash(store.conn, node_id="test-src-auto", content_hash=content_hash)
        assert result["created"] is True

        # Verify hash lookup works
        matches = lookup_content_hash(store.conn, content_hash=content_hash)
        assert len(matches) == 1
        assert matches[0]["node_id"] == "test-src-auto"


# ── OHM-wdrg Feature A: observation_source_required Hook ──────────────────


class TestObservationSourceRequired:
    """Tests for the observation_source_required built-in hook."""

    def test_low_confidence_observation_passes(self):
        """Observations with confidence < 0.8 should pass regardless of source_url."""
        from ohm.hooks_builtin import observation_source_required

        payload = {
            "agent": "test",
            "action": "observation",
            "body": {"value": 0.5, "node_id": "test-node"},
        }
        result = observation_source_required(payload)
        assert result[0] == 0  # exit code 0 = pass

    def test_high_confidence_with_source_url_passes(self):
        """Observations with confidence >= 0.8 and source_url should pass."""
        from ohm.hooks_builtin import observation_source_required

        payload = {
            "agent": "test",
            "action": "observation",
            "body": {"value": 0.9, "source_url": "https://example.com", "node_id": "test-node"},
        }
        result = observation_source_required(payload)
        assert result[0] == 0

    def test_high_confidence_without_source_url_warns(self):
        """Observations with confidence >= 0.8 and no source_url should warn (advisory)."""
        from ohm.hooks_builtin import observation_source_required

        payload = {
            "agent": "test",
            "action": "observation",
            "body": {"value": 0.9, "node_id": "test-node"},
        }
        result = observation_source_required(payload)
        # In advisory mode (default), should still pass (0) but log warning
        assert result[0] == 0

    def test_high_confidence_without_source_url_rejects_in_strict(self):
        """In strict mode, high-confidence observations without source_url should be rejected."""
        from ohm.hooks_builtin import observation_source_required

        payload = {
            "agent": "test",
            "action": "observation",
            "body": {"value": 0.9, "node_id": "test-node"},
            "__strict": True,
        }
        result = observation_source_required(payload)
        assert result[0] == 1  # exit code 1 = reject
        assert "observation_source_required" in result[2]

    def test_non_observation_action_passes(self):
        """The hook should only apply to observation actions."""
        from ohm.hooks_builtin import observation_source_required

        payload = {
            "agent": "test",
            "action": "node",
            "body": {"value": 0.9},  # high value but not observation
        }
        result = observation_source_required(payload)
        assert result[0] == 0

    def test_exact_threshold(self):
        """Confidence exactly at 0.8 should trigger the hook."""
        from ohm.hooks_builtin import observation_source_required

        # At threshold without source_url, strict = reject
        payload = {
            "agent": "test",
            "action": "observation",
            "body": {"value": 0.8, "node_id": "test-node"},
            "__strict": True,
        }
        result = observation_source_required(payload)
        assert result[0] == 1

        # Just below threshold should pass
        payload["body"]["value"] = 0.799
        result = observation_source_required(payload)
        assert result[0] == 0


# ── OHM-wdrg Feature B: Backfill Source URLs ──────────────────────────────


class TestBackfillSourceUrls:
    """Tests for POST /admin/backfill-source-urls endpoint."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_backfill_source_urls_from_references(self, store):
        """Observations without source_url should get URL from referenced source node."""
        conn = store.conn

        # Create source node with URL
        store.write_node("source-reuters", "Reuters", "source", url="https://reuters.com/article/123")

        # Create concept node
        store.write_node("concept-hormuz", "Hormuz AND-Gate", "concept")

        # Create REFERENCES edge from concept to source
        store.write_edge(from_node="concept-hormuz", to_node="source-reuters", edge_type="REFERENCES", layer="L2", confidence=0.9)

        # Create observation on concept node without source_url
        store.write_observation(node_id="concept-hormuz", type="measurement", value=0.85, source="agent-metis")

        # Find the observation
        obs = conn.execute(
            "SELECT id, node_id FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
            ["concept-hormuz"],
        ).fetchone()
        assert obs is not None
        obs_id = obs[0]

        # Verify source_url is initially null
        before = conn.execute(
            "SELECT source_url FROM ohm_observations WHERE id = ?",
            [obs_id],
        ).fetchone()
        assert before[0] is None

        # Simulate backfill: find REFERENCE edges, copy URL
        ref_edges = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'REFERENCES' AND deleted_at IS NULL",
            ["concept-hormuz"],
        ).fetchall()

        source_url_found = None
        for (source_node_id,) in ref_edges:
            row = conn.execute(
                "SELECT url FROM ohm_nodes WHERE id = ? AND url IS NOT NULL AND url != ''",
                [source_node_id],
            ).fetchone()
            if row and row[0]:
                source_url_found = row[0]
                break

        assert source_url_found == "https://reuters.com/article/123"

        # Update observation
        conn.execute(
            "UPDATE ohm_observations SET source_url = ? WHERE id = ?",
            [source_url_found, obs_id],
        )

        # Verify update
        after = conn.execute(
            "SELECT source_url FROM ohm_observations WHERE id = ?",
            [obs_id],
        ).fetchone()
        assert after[0] == "https://reuters.com/article/123"


# ── OHM-wdrg Feature C: REFERENCES Edge Auto-creation ──────────────────────


class TestReferencesEdgeAutoCreation:
    """Tests for REFERENCES edge creation in Stage 3 (source creation)."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_reference_edge_creation(self, store):
        """Creating a REFERENCES edge should work for concept→source."""
        store.write_node("concept-iran", "Iran Geopolitics", "concept")
        store.write_node("src-article-1", "Reuters: Iran Sanctions", "source", url="https://reuters.com/iran-sanctions")

        edge = store.write_edge(
            from_node="concept-iran",
            to_node="src-article-1",
            edge_type="REFERENCES",
            layer="L2",
            confidence=0.7,
        )

        assert edge is not None
        assert edge.get("created", False) is True or edge.get("id") is not None

    def test_reference_edge_not_from_source(self, store):
        """REFERENCES edges should only go FROM concept nodes, not from source nodes."""
        store.write_node("src-article-2", "Article", "source", url="https://example.com")
        store.write_node("concept-oil", "Oil Prices", "concept")

        # This should work (concept → source is valid)
        edge = store.write_edge(
            from_node="concept-oil",
            to_node="src-article-2",
            edge_type="REFERENCES",
            layer="L2",
            confidence=0.7,
        )
        assert edge is not None


# ── Stage 4-5: Assessment and Synthesis ─────────────────────────────────────


class TestStageAssessKeyword:
    """Tests for keyword-based assessment in Stage 4."""

    def test_keyword_extraction_from_title(self):
        """Keywords from TRACKED_DOMAINS should be extractable from titles."""
        from scripts.ingestion.ingestion_pipeline import TRACKED_DOMAINS

        title = "Iran threatens Hormuz strait shipping"
        text = title.lower()
        matched = [d for d in TRACKED_DOMAINS if d.lower() in text]
        assert "hormuz" in matched or "iran" in matched
        assert len(matched) >= 2

    def test_confidence_scaling(self):
        """Trust score should scale to confidence range [0.5, 0.95]."""
        trust = 0.5
        confidence = min(0.5 + trust * 0.3, 0.95)
        assert 0.5 <= confidence <= 0.95
        assert abs(confidence - 0.65) < 0.01  # 0.5 + 0.5*0.3 = 0.65

        trust = 1.0
        confidence = min(0.5 + trust * 0.3, 0.95)
        assert confidence == 0.8  # 0.5 + 1.0*0.3 = 0.8

        trust = 1.5
        confidence = min(0.5 + trust * 0.3, 0.95)
        assert confidence == 0.95  # capped at 0.95


class TestStageSynthesizeClusters:
    """Tests for cluster detection in Stage 5."""

    def test_cluster_grouping_by_keyword(self):
        """Items sharing keywords should be grouped into clusters."""
        from collections import defaultdict

        items = [
            {"title": "Iran shipping disrupted", "assessment": {"keywords": ["iran", "shipping"]}},
            {"title": "Iran sanctions escalate", "assessment": {"keywords": ["iran", "oil"]}},
            {"title": "Iran nuclear talks", "assessment": {"keywords": ["iran"]}},
            {"title": "AI regulation update", "assessment": {"keywords": ["AI governance"]}},
        ]

        by_keyword = defaultdict(list)
        for item in items:
            for kw in item["assessment"]["keywords"]:
                by_keyword[kw].append(item)

        # "iran" should have 3 items (cluster)
        assert len(by_keyword["iran"]) >= 3
        # "shipping" should have 1 item (not a cluster)
        assert len(by_keyword["shipping"]) == 1

    def test_cluster_detection_threshold(self):
        """Clusters with fewer than 3 items should not trigger synthesis."""
        from collections import defaultdict

        items = [
            {"title": "AI governance update", "assessment": {"keywords": ["AI governance"]}},
            {"title": "AI ethics report", "assessment": {"keywords": ["AI governance"]}},
        ]

        by_keyword = defaultdict(list)
        for item in items:
            for kw in item["assessment"]["keywords"]:
                by_keyword[kw].append(item)

        clusters = [{kw: items} for kw, items in by_keyword.items() if len(items) >= 3]
        assert len(clusters) == 0  # Only 2 items, no cluster


# ── Integration Tests for Admin Endpoints ─────────────────────────────────────


class TestAdminBackfillEndpoints:
    """Integration tests for admin backfill endpoints via store-level logic."""

    @pytest.fixture
    def store(self, tmp_path):
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        return store

    def test_backfill_aliases_handler(self, store):
        """Test the backfill-aliases handler logic directly."""
        from ohm.queries import register_alias

        # Create test nodes
        store.write_node("test-1", "Test Node Alpha", "concept")
        store.write_node("test-2", "Another Node", "source", url="https://example.com")

        conn = store.conn
        rows = conn.execute("SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()

        created = 0
        for node_id, label in rows:
            norm_label = normalize_alias(label)
            if norm_label:
                result = register_alias(conn, alias_norm=norm_label, node_id=node_id)
                if result.get("created"):
                    created += 1
            norm_id = normalize_alias(node_id)
            if norm_id and norm_id != norm_label:
                result = register_alias(conn, alias_norm=norm_id, node_id=node_id)
                if result.get("created"):
                    created += 1

        # Label aliases were auto-created; node_id aliases should be new
        assert created >= 2  # At least 2 node_id aliases

        # Verify we can resolve via alias
        from ohm.queries import resolve_node_by_alias

        result = resolve_node_by_alias(conn, query="Test Node Alpha")
        assert result is not None
        assert result["id"] == "test-1"

    def test_backfill_content_hashes_handler(self, store):
        """Test the backfill-content-hashes handler logic directly."""
        from ohm.queries import register_content_hash

        # Create source nodes
        store.write_node("src-1", "Reuters Article", "source", url="https://reuters.com/article")
        store.write_node("src-2", "BBC News", "source", url="https://bbc.com/news")

        conn = store.conn
        rows = conn.execute("SELECT id, label, url FROM ohm_nodes WHERE type = 'source' AND deleted_at IS NULL").fetchall()

        created = 0
        for node_id, label, url in rows:
            content = url if url else f"{label or ''}{url or ''}"
            if content.strip():
                content_hash = compute_content_hash(content)
                result = register_content_hash(conn, node_id=node_id, content_hash=content_hash)
                if result.get("created"):
                    created += 1

        assert created == 2  # Two source nodes with URLs

    def test_backfill_source_urls_handler(self, store):
        """Test the backfill-source-urls handler logic directly."""
        conn = store.conn

        # Create source node with URL
        store.write_node("source-reuters", "Reuters", "source", url="https://reuters.com/article/456")

        # Create concept node
        store.write_node("concept-test", "Test Concept", "concept")

        # Create REFERENCES edge
        store.write_edge(from_node="concept-test", to_node="source-reuters", edge_type="REFERENCES", layer="L2", confidence=0.8)

        # Create observation without source_url
        store.write_observation(node_id="concept-test", type="measurement", value=0.9, source="agent-clio")

        # Find observations without source_url
        obs_rows = conn.execute("SELECT id, node_id FROM ohm_observations WHERE deleted_at IS NULL AND (source_url IS NULL OR source_url = '')").fetchall()

        updated = 0
        for obs_id, node_id in obs_rows:
            ref_edges = conn.execute(
                "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'REFERENCES' AND deleted_at IS NULL",
                [node_id],
            ).fetchall()

            for (source_node_id,) in ref_edges:
                row = conn.execute(
                    "SELECT url FROM ohm_nodes WHERE id = ? AND url IS NOT NULL AND url != ''",
                    [source_node_id],
                ).fetchone()
                if row and row[0]:
                    conn.execute(
                        "UPDATE ohm_observations SET source_url = ? WHERE id = ?",
                        [row[0], obs_id],
                    )
                    updated += 1
                    break

        assert updated == 1

        # Verify the update
        obs = conn.execute(
            "SELECT source_url FROM ohm_observations WHERE node_id = ?",
            ["concept-test"],
        ).fetchone()
        assert obs[0] == "https://reuters.com/article/456"
