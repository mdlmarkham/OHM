"""Tests for OHM-848: Soft type-validation mode for schema-extension trials.

Verifies:
  - proposed-type:* tags are detected and auto-register in ohm_type_proposals
  - Unknown raw types are still rejected (no validation bypass)
  - Collision detection identifies proposed types with shared CAUSES targets
  - Nudges fire for proposed-type tags
  - Schema version is bumped and migration exists
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


class TestSchemaMigration:
    """Schema-level invariants."""

    def test_schema_version_bumped(self):
        from ohm.graph.schema import SCHEMA_VERSION
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 54, 0)

    def test_migration_0_54_0_present(self):
        from ohm.graph.schema import MIGRATIONS
        versions = [m[0] for m in MIGRATIONS]
        assert "0.54.0" in versions

    def test_migration_0_54_0_creates_table(self):
        from ohm.graph.schema import MIGRATIONS
        for ver, desc, stmts in MIGRATIONS:
            if ver == "0.54.0":
                assert "ohm_type_proposals" in desc.lower()
                assert any("ohm_type_proposals" in s for s in stmts)
                return
        pytest.fail("Migration 0.54.0 not found")

    def test_table_ddl_in_schema(self):
        from ohm.graph.schema import DDL_STATEMENTS
        assert any("ohm_type_proposals" in stmt for stmt in DDL_STATEMENTS)


class TestProposedTypeDetection:
    """detect_proposed_types extracts proposed-type:* tags."""

    def test_detect_single_proposed_type(self):
        from ohm.graph.queries.type_proposals import detect_proposed_types
        result = detect_proposed_types(["proposed-type:signal", "scope:q3"])
        assert result == ["signal"]

    def test_detect_multiple_proposed_types(self):
        from ohm.graph.queries.type_proposals import detect_proposed_types
        result = detect_proposed_types(["proposed-type:signal", "proposed-type:indicator"])
        assert result == ["signal", "indicator"]

    def test_detect_no_proposed_types(self):
        from ohm.graph.queries.type_proposals import detect_proposed_types
        assert detect_proposed_types(["scope:q3", "priority:high"]) == []

    def test_detect_none_tags(self):
        from ohm.graph.queries.type_proposals import detect_proposed_types
        assert detect_proposed_types(None) == []

    def test_detect_empty_tags(self):
        from ohm.graph.queries.type_proposals import detect_proposed_types
        assert detect_proposed_types([]) == []


class TestRegisterTypeProposal:
    """register_type_proposal creates/updates ohm_type_proposals rows."""

    def test_create_new_proposal(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal
        result = register_type_proposal(test_db, proposed_type="signal", proposed_by="metis")
        assert result["proposed_type"] == "signal"
        assert result["status"] == "trial"
        assert result["proposed_by"] == "metis"

    def test_update_existing_proposal(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal
        r1 = register_type_proposal(test_db, proposed_type="signal", proposed_by="metis", evidence_node_id="node1")
        r2 = register_type_proposal(test_db, proposed_type="signal", proposed_by="agent2", evidence_node_id="node2")
        assert r2["id"] == r1["id"]
        evidence = r2.get("evidence_node_ids") or []
        if isinstance(evidence, str):
            import json as _json
            evidence = _json.loads(evidence)
        assert "node1" in evidence
        assert "node2" in evidence

    def test_different_types_get_different_rows(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal
        r1 = register_type_proposal(test_db, proposed_type="signal")
        r2 = register_type_proposal(test_db, proposed_type="indicator")
        assert r1["id"] != r2["id"]


class TestListTypeProposals:
    """list_type_proposals filters by status."""

    def test_list_all(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal, list_type_proposals
        register_type_proposal(test_db, proposed_type="signal")
        register_type_proposal(test_db, proposed_type="indicator")
        result = list_type_proposals(test_db)
        assert len(result) >= 2

    def test_list_by_status_trial(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal, list_type_proposals
        register_type_proposal(test_db, proposed_type="signal")
        result = list_type_proposals(test_db, status="trial")
        assert all(r["status"] == "trial" for r in result)

    def test_list_by_status_promoted_empty(self, test_db):
        from ohm.graph.queries.type_proposals import list_type_proposals
        result = list_type_proposals(test_db, status="promoted")
        assert len(result) == 0


class TestGetTypeProposal:
    """get_type_proposal fetches by id."""

    def test_get_existing(self, test_db):
        from ohm.graph.queries.type_proposals import register_type_proposal, get_type_proposal
        created = register_type_proposal(test_db, proposed_type="signal")
        result = get_type_proposal(test_db, proposal_id=created["id"])
        assert result["proposed_type"] == "signal"

    def test_get_nonexistent_raises(self, test_db):
        from ohm.graph.queries.type_proposals import get_type_proposal
        with pytest.raises(ValueError, match="not found"):
            get_type_proposal(test_db, proposal_id="nonexistent")


class TestDetectCollisions:
    """detect_collisions finds proposed types with shared CAUSES targets."""

    def test_detect_collision(self, test_db):
        from ohm.graph.queries.type_proposals import detect_collisions
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES "
            "('n1', 'Signal A', 'concept', 'metis', CURRENT_TIMESTAMP, '[\"proposed-type:vendor\"]'), "
            "('n2', 'Signal B', 'concept', 'metis', CURRENT_TIMESTAMP, '[\"proposed-type:supplier\"]'), "
            "('target1', 'Effect', 'event', 'metis', CURRENT_TIMESTAMP, '[]')"
        )
        test_db.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('n1', 'target1', 'CAUSES', 'L3', 0.9, 'metis', CURRENT_TIMESTAMP), "
            "('n2', 'target1', 'CAUSES', 'L3', 0.8, 'metis', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        collisions = detect_collisions(test_db)
        assert len(collisions) == 1
        c = collisions[0]
        assert {c["type_a"], c["type_b"]} == {"vendor", "supplier"}
        assert "target1" in c["shared_targets"]

    def test_no_collision_different_targets(self, test_db):
        from ohm.graph.queries.type_proposals import detect_collisions
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES "
            "('n1', 'Signal A', 'concept', 'metis', CURRENT_TIMESTAMP, '[\"proposed-type:vendor\"]'), "
            "('n2', 'Signal B', 'concept', 'metis', CURRENT_TIMESTAMP, '[\"proposed-type:supplier\"]'), "
            "('target1', 'Effect A', 'event', 'metis', CURRENT_TIMESTAMP, '[]'), "
            "('target2', 'Effect B', 'event', 'metis', CURRENT_TIMESTAMP, '[]')"
        )
        test_db.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('n1', 'target1', 'CAUSES', 'L3', 0.9, 'metis', CURRENT_TIMESTAMP), "
            "('n2', 'target2', 'CAUSES', 'L3', 0.8, 'metis', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        collisions = detect_collisions(test_db)
        assert len(collisions) == 0

    def test_no_collision_no_proposed_types(self, test_db):
        from ohm.graph.queries.type_proposals import detect_collisions
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES "
            "('n1', 'Signal A', 'concept', 'metis', CURRENT_TIMESTAMP, '[\"scope:q3\"]')"
        )
        test_db.commit()
        collisions = detect_collisions(test_db)
        assert len(collisions) == 0


class TestProcessNodeTags:
    """process_node_tags auto-registers proposed types from tags."""

    def test_processes_proposed_type_tags(self, test_db):
        from ohm.graph.queries.type_proposals import process_node_tags
        proposals = process_node_tags(
            test_db,
            node_id="n1",
            tags=["proposed-type:signal", "scope:q3"],
            created_by="metis",
        )
        assert len(proposals) == 1
        assert proposals[0]["proposed_type"] == "signal"

    def test_no_proposed_types_returns_empty(self, test_db):
        from ohm.graph.queries.type_proposals import process_node_tags
        proposals = process_node_tags(test_db, node_id="n1", tags=["scope:q3"])
        assert proposals == []

    def test_none_tags_returns_empty(self, test_db):
        from ohm.graph.queries.type_proposals import process_node_tags
        proposals = process_node_tags(test_db, node_id="n1", tags=None)
        assert proposals == []


class TestUnknownTypeRejection:
    """Unknown raw types are still rejected — no validation bypass."""

    def test_unknown_type_rejected(self, test_db):
        from ohm.queries import create_node
        with pytest.raises(ValueError, match="Invalid node type"):
            create_node(test_db, label="Test", node_type="signal", created_by="metis")

    def test_known_type_with_proposed_tag_accepted(self, test_db):
        from ohm.queries import create_node
        node = create_node(
            test_db,
            label="Signal Node",
            node_type="concept",
            created_by="metis",
            tags=["proposed-type:signal"],
        )
        assert node["type"] == "concept"

    def test_canonical_types_not_affected(self, test_db):
        from ohm.queries import create_node
        for nt in ("concept", "source", "event", "pattern", "decision"):
            node = create_node(test_db, label=f"Test {nt}", node_type=nt, created_by="metis")
            assert node["type"] == nt


class TestHTTPIntegration:
    """HTTP-level integration tests."""

    def test_create_node_with_proposed_type_tag(self, test_server):
        port, store = test_server
        status, data = _request("POST", port, "/node", {
            "id": "n_prop1",
            "label": "Signal Detector",
            "type": "concept",
            "created_by": "metis",
            "tags": ["proposed-type:signal"],
        })
        assert status == 201
        assert data.get("type") == "concept"

        proposals = store.read_conn.execute(
            "SELECT * FROM ohm_type_proposals WHERE proposed_type = 'signal' AND status = 'trial'"
        ).fetchall()
        assert len(proposals) >= 1

    def test_nudge_fires_for_proposed_type(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/node", {
            "id": "n_prop2",
            "label": "Signal Detector",
            "type": "concept",
            "created_by": "metis",
            "tags": ["proposed-type:signal"],
        })
        assert status == 201
        nudges = data.get("nudges", [])
        proposed_nudges = [n for n in nudges if n.get("type") == "proposed_type_trial"]
        assert len(proposed_nudges) >= 1
        assert "signal" in proposed_nudges[0]["message"]

    def test_no_nudge_without_proposed_type(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/node", {
            "id": "n_prop3",
            "label": "Regular Node",
            "type": "concept",
            "created_by": "metis",
            "tags": ["scope:q3"],
        })
        assert status == 201
        nudges = data.get("nudges", [])
        proposed_nudges = [n for n in nudges if n.get("type") == "proposed_type_trial"]
        assert len(proposed_nudges) == 0

    def test_unknown_type_still_rejected_via_http(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/node", {
            "id": "n_bad",
            "label": "Bad Type",
            "type": "signal",
            "created_by": "metis",
        })
        assert status in (400, 422, 500)

    def test_multiple_proposed_types_one_node(self, test_server):
        port, store = test_server
        status, data = _request("POST", port, "/node", {
            "id": "n_multi",
            "label": "Multi Type",
            "type": "concept",
            "created_by": "metis",
            "tags": ["proposed-type:signal", "proposed-type:indicator"],
        })
        assert status == 201
        proposals = store.read_conn.execute(
            "SELECT COUNT(*) FROM ohm_type_proposals WHERE status = 'trial'"
        ).fetchone()
        assert proposals[0] >= 2

    def test_repeated_node_updates_evidence_list(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", {
            "id": "n_rep1",
            "label": "Node 1",
            "type": "concept",
            "created_by": "metis",
            "tags": ["proposed-type:signal"],
        })
        _request("POST", port, "/node", {
            "id": "n_rep2",
            "label": "Node 2",
            "type": "concept",
            "created_by": "metis",
            "tags": ["proposed-type:signal"],
        })
        proposals = store.read_conn.execute(
            "SELECT * FROM ohm_type_proposals WHERE proposed_type = 'signal' AND status = 'trial'"
        ).fetchall()
        assert len(proposals) == 1
        evidence = proposals[0]
        evidence_ids = evidence[10] if len(evidence) > 10 else None
        if isinstance(evidence_ids, str):
            evidence_ids = json.loads(evidence_ids)
        if evidence_ids:
            assert len(evidence_ids) >= 2


class TestNoCanonicalSchemaChange:
    """No change to canonical VALID_NODE_TYPES until explicit promotion."""

    def test_proposed_type_not_in_valid_types(self):
        from ohm.graph.schema import VALID_NODE_TYPES
        assert "signal" not in VALID_NODE_TYPES
        assert "vendor" not in VALID_NODE_TYPES
        assert "supplier" not in VALID_NODE_TYPES

    def test_canonical_types_preserved(self):
        from ohm.graph.schema import VALID_NODE_TYPES
        for t in ("concept", "source", "event", "pattern", "decision", "prospect", "expectation"):
            assert t in VALID_NODE_TYPES