"""Bug OHM-y2i.20: POST /node upserts then raises ConflictError.

When store.write_node() is called with an existing ID, it performs an UPDATE
(upsert behavior) and returns {"created": False, ...}. But server.py checks
result.get("created", True) and raises ConflictError. The database is modified
but the client receives a 409 error response.
"""

import os
import pytest

from ohm.store import OhmStore
from ohm.exceptions import ConflictError


class TestNodeUpsertConflictBug:
    """Verify that write_node's upsert behavior conflicts with server.py's
    ConflictError handling.

    The bug: store.write_node() updates existing nodes (upsert), but server.py
    raises ConflictError when created=False. The node IS modified in the database,
    but the client is told it failed.
    """

    @pytest.fixture
    def store(self, tmp_path):
        """Create a fresh OhmStore for testing."""
        db_path = str(tmp_path / "test_upsert.duckdb")
        s = OhmStore(db_path, agent_name="test")
        yield s
        s.close()

    def test_write_node_upsert_returns_created_false(self, store):
        """When write_node is called with an existing ID, it returns created=False.

        This is the first half of the bug: the store does an UPDATE, not an INSERT.
        """
        # First write — node is created
        result1 = store.write_node(
            id="upsert_test", label="concept", type="note", content="original",
        )
        assert result1["created"] is True, "First write should create the node"

        # Second write with same ID — node is UPDATED (upsert behavior)
        result2 = store.write_node(
            id="upsert_test", label="concept", type="note", content="updated",
        )
        assert result2["created"] is False, "Second write should return created=False"
        assert result2["content"] == "updated", "Node content should be updated"

    def test_write_node_upsert_actually_modifies_database(self, store):
        """The upsert actually changes the database — data is modified before
        the server raises ConflictError.

        This proves that the ConflictError in server.py is misleading:
        the database was already modified, but the client is told it failed.
        """
        store.write_node(id="conflict_test", label="concept", type="note", content="v1")

        # Simulate what server.py does: call write_node, check created
        result = store.write_node(
            id="conflict_test", label="concept", type="note", content="v2",
        )

        # Server would raise ConflictError here, but the database already changed
        assert result["created"] is False

        # Verify the database WAS modified despite what the error would say
        node = store.get_node("conflict_test")
        assert node["content"] == "v2", (
            "Database was modified (content='v2'), but server.py would raise "
            "ConflictError telling the client the operation failed. "
            "This is a data integrity issue."
        )

    def test_server_logic_would_raise_conflict_after_upsert(self, store):
        """Simulate the server.py logic that raises ConflictError after upsert.

        This test demonstrates the exact bug path in server.py:
            result = self.store.write_node(...)
            if result.get("created", True):
                self._json_response(201, result)
            else:
                raise ConflictError(f"Node {body['id']} already exists")
        """
        store.write_node(id="server_sim", label="concept", type="note", content="first")

        result = store.write_node(
            id="server_sim", label="concept", type="note", content="second",
        )

        # This is what server.py checks:
        if result.get("created", True):
            # Would return 201 Created
            pass
        else:
            # Would raise ConflictError
            # But the node was ALREADY UPDATED in the database
            node = store.get_node("server_sim")
            assert node["content"] == "second", (
                "Node was updated to 'second' in DB, but server would return 409 Conflict. "
                "Client thinks the operation failed, but it actually succeeded."
            )

    def test_write_node_field_updates_on_upsert(self, store):
        """Verify which fields get updated during upsert.

        All mutable fields should be updated: label, type, content,
        confidence, visibility, provenance, tags, metadata.
        """
        store.write_node(
            id="field_test", label="original_label", type="note",
            content="original", confidence=0.5, visibility="private",
            provenance="test_v1",
        )

        result = store.write_node(
            id="field_test", label="updated_label", type="concept",
            content="updated", confidence=0.9, visibility="team",
            provenance="test_v2",
        )
        assert result["created"] is False
        assert result["label"] == "updated_label"
        assert result["content"] == "updated"
        assert abs(result["confidence"] - 0.9) < 0.001
        assert result["visibility"] == "team"
        assert result["provenance"] == "test_v2"

    def test_edge_upsert_similar_issue(self, store):
        """Edges also have upsert behavior — check if similar issue exists.

        Note: Edges use UUIDs by default, so upsert is less likely, but
        if challenge_of is specified, there could be similar issues.
        """
        store.write_node(id="e1", label="concept", type="note")
        store.write_node(id="e2", label="concept", type="note")

        edge1 = store.write_edge(
            from_node="e1", to_node="e2",
            edge_type="RELATES_TO", layer="L1", confidence=0.8,
        )
        assert edge1["created_by"] == "test"