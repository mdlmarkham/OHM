"""Tests for boundary enforcement rules (ADR-003)."""

import pytest

from ohm.exceptions import EdgeNotFoundError, PermissionDeniedError


class TestBoundaryChecks:
    """Tests for boundary enforcement functions."""

    def test_can_write_layer_always_allowed(self):
        """All layers are writable for new edges."""
        from ohm.boundary import check_can_write_layer

        # Should not raise
        check_can_write_layer("agent_a", "L1")
        check_can_write_layer("agent_a", "L2")
        check_can_write_layer("agent_a", "L3")
        check_can_write_layer("agent_a", "L4")

    def test_can_update_own_edge(self):
        """Agent can update its own edge."""
        from ohm.boundary import check_can_update_edge

        # Should not raise
        check_can_update_edge("agent_a", "agent_a", "edge_1")

    def test_can_update_other_agent_edge_raises(self):
        """Agent cannot update another agent's edge."""
        from ohm.boundary import check_can_update_edge

        with pytest.raises(PermissionDeniedError) as exc:
            check_can_update_edge("agent_b", "agent_a", "edge_1")
        assert "agent_b" in str(exc.value)
        assert "agent_a" in str(exc.value)
        assert "edge_1" in str(exc.value)

    def test_can_delete_own_edge(self):
        """Agent can delete its own edge."""
        from ohm.boundary import check_can_delete_edge

        # Should not raise
        check_can_delete_edge("agent_a", "agent_a", "edge_1")

    def test_can_delete_other_agent_edge_raises(self):
        """Agent cannot delete another agent's edge."""
        from ohm.boundary import check_can_delete_edge

        with pytest.raises(PermissionDeniedError) as exc:
            check_can_delete_edge("agent_b", "agent_a", "edge_1")
        assert "agent_b" in str(exc.value)

    def test_can_challenge_l3(self):
        """Any agent can challenge L3 edges."""
        from ohm.boundary import check_can_challenge

        # Should not raise
        check_can_challenge("agent_a", "L3")

    def test_can_challenge_l4(self):
        """Any agent can challenge L4 edges."""
        from ohm.boundary import check_can_challenge

        # Should not raise
        check_can_challenge("agent_a", "L4")

    def test_cannot_challenge_l1(self):
        """L1 edges cannot be challenged."""
        from ohm.boundary import check_can_challenge

        with pytest.raises(PermissionDeniedError, match="L1"):
            check_can_challenge("agent_a", "L1")

    def test_cannot_challenge_l2(self):
        """L2 edges cannot be challenged."""
        from ohm.boundary import check_can_challenge

        with pytest.raises(PermissionDeniedError, match="L2"):
            check_can_challenge("agent_a", "L2")

    def test_can_support_l3(self):
        """Any agent can support L3 edges."""
        from ohm.boundary import check_can_support

        # Should not raise
        check_can_support("agent_a", "L3")

    def test_cannot_support_l1(self):
        """L1 edges cannot be supported."""
        from ohm.boundary import check_can_support

        with pytest.raises(PermissionDeniedError, match="L1"):
            check_can_support("agent_a", "L1")


class TestBoundaryEnforcementWithDB:
    """Tests for boundary enforcement against real database state."""

    def test_get_edge_owner(self, test_db, sample_graph_small):
        """get_edge_owner returns the correct owner."""
        from ohm.boundary import get_edge_owner

        edge_ab = sample_graph_small["edges"]["ab"]
        owner = get_edge_owner(test_db, edge_ab)
        assert owner == "test_agent"

    def test_get_edge_owner_nonexistent(self, test_db):
        """get_edge_owner raises EdgeNotFoundError for missing edge."""
        from ohm.boundary import get_edge_owner

        with pytest.raises(EdgeNotFoundError):
            get_edge_owner(test_db, "nonexistent_edge")

    def test_get_edge_layer(self, test_db, sample_graph_small):
        """get_edge_layer returns the correct layer."""
        from ohm.boundary import get_edge_layer

        edge_ab = sample_graph_small["edges"]["ab"]
        layer = get_edge_layer(test_db, edge_ab)
        assert layer == "L3"

    def test_enforce_write_boundary_owner(self, test_db, sample_graph_small):
        """Owner can update their own edge."""
        from ohm.boundary import enforce_write_boundary

        edge_ab = sample_graph_small["edges"]["ab"]
        # Should not raise — test_agent owns the edge
        enforce_write_boundary(test_db, "test_agent", edge_ab)

    def test_enforce_write_boundary_non_owner(self, test_db, sample_graph_small):
        """Non-owner cannot update another agent's edge."""
        from ohm.boundary import enforce_write_boundary

        edge_ab = sample_graph_small["edges"]["ab"]
        with pytest.raises(PermissionDeniedError):
            enforce_write_boundary(test_db, "other_agent", edge_ab)

    def test_enforce_challenge_boundary_l3(self, test_db, sample_graph_small):
        """Can challenge L3 edges."""
        from ohm.boundary import enforce_challenge_boundary

        edge_ab = sample_graph_small["edges"]["ab"]
        # Should not raise
        enforce_challenge_boundary(test_db, "challenger", edge_ab)

    def test_enforce_challenge_boundary_l2(self, test_db, sample_graph_small):
        """Cannot challenge L2 edges."""
        from ohm.boundary import enforce_challenge_boundary

        edge_bc = sample_graph_small["edges"]["bc"]
        with pytest.raises(PermissionDeniedError, match="L2"):
            enforce_challenge_boundary(test_db, "challenger", edge_bc)

    def test_enforce_support_boundary_l3(self, test_db, sample_graph_small):
        """Can support L3 edges."""
        from ohm.boundary import enforce_support_boundary

        edge_ab = sample_graph_small["edges"]["ab"]
        # Should not raise
        enforce_support_boundary(test_db, "supporter", edge_ab)

    def test_enforce_support_boundary_l2(self, test_db, sample_graph_small):
        """Cannot support L2 edges."""
        from ohm.boundary import enforce_support_boundary

        edge_bc = sample_graph_small["edges"]["bc"]
        with pytest.raises(PermissionDeniedError, match="L2"):
            enforce_support_boundary(test_db, "supporter", edge_bc)


class TestMutationOperations:
    """Tests for create_node, create_edge, create_challenge, create_support."""

    def test_create_node(self, test_db):
        """create_node inserts a node and returns its full record."""
        from ohm.queries import create_node

        result = create_node(test_db, label="Test Node", created_by="agent_x")
        assert isinstance(result, dict)
        assert result["label"] == "Test Node"
        assert result["created_by"] == "agent_x"
        assert "test_node_" in result["id"]

        # Verify it exists
        node_result = test_db.execute(
            "SELECT label, created_by FROM ohm_nodes WHERE id = ?",
            [result["id"]],
        ).fetchone()
        assert node_result[0] == "Test Node"
        assert node_result[1] == "agent_x"

    def test_create_node_invalid_type(self, test_db):
        """create_node rejects invalid node types."""
        from ohm.queries import create_node

        with pytest.raises(ValueError, match="Invalid node type"):
            create_node(test_db, label="Bad", node_type="invalid_type", created_by="agent_x")

    def test_create_edge(self, test_db):
        """create_edge inserts an edge and returns its full record."""
        from ohm.queries import create_edge, create_node

        a = create_node(test_db, label="A", created_by="agent_x")
        b = create_node(test_db, label="B", created_by="agent_x")

        result = create_edge(
            test_db,
            from_node=a["id"],
            to_node=b["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="agent_x",
            confidence=0.9,
        )
        assert isinstance(result, dict)
        assert result["from_node"] == a["id"]
        assert result["to_node"] == b["id"]
        assert result["layer"] == "L3"
        assert result["edge_type"] == "CAUSES"
        assert result["confidence"] == pytest.approx(0.9)

    def test_create_edge_invalid_type_for_layer(self, test_db):
        """create_edge rejects edge types not valid for the layer."""
        from ohm.queries import create_edge, create_node

        a = create_node(test_db, label="A", created_by="agent_x")
        b = create_node(test_db, label="B", created_by="agent_x")

        with pytest.raises(ValueError, match="Invalid edge type"):
            create_edge(
                test_db,
                from_node=a,
                to_node=b,
                layer="L1",
                edge_type="CAUSES",
                created_by="agent_x",
            )

    def test_create_challenge(self, test_db, sample_graph_small):
        """create_challenge creates a CHALLENGED_BY edge and returns its full record."""
        from ohm.queries import create_challenge

        edge_ab = sample_graph_small["edges"]["ab"]
        result = create_challenge(
            test_db,
            edge_id=edge_ab,
            reason="weak evidence",
            created_by="critic",
            confidence=0.3,
        )
        assert isinstance(result, dict)
        assert result["edge_type"] == "CHALLENGED_BY"
        assert result["challenge_of"] == edge_ab
        assert result["challenge_type"] == "CHALLENGED_BY"
        assert result["condition"] == "weak evidence"
        assert result["created_by"] == "critic"
        assert result["confidence"] == pytest.approx(0.3)

    def test_create_challenge_on_l2_raises(self, test_db, sample_graph_small):
        """Cannot challenge L2 edges."""
        from ohm.queries import create_challenge

        edge_bc = sample_graph_small["edges"]["bc"]  # L2 edge
        with pytest.raises(PermissionDeniedError, match="L2"):
            create_challenge(
                test_db,
                edge_id=edge_bc,
                reason="test",
                created_by="critic",
            )

    def test_create_support(self, test_db, sample_graph_small):
        """create_support creates a SUPPORTS edge."""
        from ohm.queries import create_support

        edge_ab = sample_graph_small["edges"]["ab"]
        support = create_support(
            test_db,
            edge_id=edge_ab,
            reason="additional evidence",
            created_by="supporter",
            confidence=0.85,
        )
        assert support["id"]
        assert support["edge_type"] == "SUPPORTS"

        result = test_db.execute(
            "SELECT edge_type, challenge_of, challenge_type, condition, created_by FROM ohm_edges WHERE id = ?",
            [support["id"]],
        ).fetchone()
        assert result[0] == "SUPPORTS"
        assert result[1] == edge_ab
        assert result[2] == "SUPPORTS"

    def test_create_support_on_l2_raises(self, test_db, sample_graph_small):
        """Cannot support L2 edges."""
        from ohm.queries import create_support

        edge_bc = sample_graph_small["edges"]["bc"]  # L2 edge
        with pytest.raises(PermissionDeniedError, match="L2"):
            create_support(
                test_db,
                edge_id=edge_bc,
                reason="test",
                created_by="supporter",
            )

    def test_set_agent_state_new(self, test_db):
        """set_agent_state creates a new agent state record."""
        from ohm.queries import query_agent_state, set_agent_state

        set_agent_state(test_db, agent_name="metis", focus="researching patterns")
        results = query_agent_state(test_db, agent_name="metis")
        assert len(results) == 1
        assert results[0]["current_focus"] == "researching patterns"

    def test_set_agent_state_update(self, test_db):
        """set_agent_state updates an existing agent state record."""
        from ohm.queries import query_agent_state, set_agent_state

        set_agent_state(test_db, agent_name="clio", focus="initial focus")
        set_agent_state(test_db, agent_name="clio", focus="updated focus")
        results = query_agent_state(test_db, agent_name="clio")
        assert len(results) == 1
        assert results[0]["current_focus"] == "updated focus"

    def test_node_exists(self, test_db, sample_graph_small):
        """node_exists returns True for existing nodes."""
        from ohm.queries import node_exists

        node_a = sample_graph_small["nodes"]["a"]
        assert node_exists(test_db, node_a) is True
        assert node_exists(test_db, "nonexistent") is False

    def test_edge_exists(self, test_db, sample_graph_small):
        """edge_exists returns True for existing edges."""
        from ohm.queries import edge_exists

        edge_ab = sample_graph_small["edges"]["ab"]
        assert edge_exists(test_db, edge_ab) is True
        assert edge_exists(test_db, "nonexistent") is False


class TestCustomerIdentityBoundary:
    """Tests for customer API identity boundary rules (OHM-l1vs)."""

    def test_is_customer_identity(self):
        from ohm.boundary import is_customer_identity

        assert is_customer_identity("customer:acme_hvac") is True
        assert is_customer_identity("metis") is False
        assert is_customer_identity("customer:") is True
        assert is_customer_identity("clio") is False

    def test_customer_id_from_identity(self):
        from ohm.boundary import customer_id_from_identity

        assert customer_id_from_identity("customer:acme_hvac") == "acme_hvac"
        assert customer_id_from_identity("customer:") == ""
        assert customer_id_from_identity("metis") is None

    def test_customer_can_update_own_edge(self):
        from ohm.boundary import check_can_update_edge

        check_can_update_edge("customer:acme_hvac", "customer:acme_hvac", "edge_1")

    def test_customer_cannot_update_agent_edge(self):
        from ohm.boundary import check_can_update_edge

        with pytest.raises(PermissionDeniedError, match="customer:acme_hvac"):
            check_can_update_edge("customer:acme_hvac", "metis", "edge_1")

    def test_agent_cannot_update_customer_edge(self):
        from ohm.boundary import check_can_update_edge

        with pytest.raises(PermissionDeniedError, match="metis"):
            check_can_update_edge("metis", "customer:acme_hvac", "edge_1")

    def test_customer_can_delete_own_edge(self):
        from ohm.boundary import check_can_delete_edge

        check_can_delete_edge("customer:acme_hvac", "customer:acme_hvac", "edge_1")

    def test_customer_can_challenge_l3(self):
        from ohm.boundary import check_can_challenge

        check_can_challenge("customer:acme_hvac", "L3")

    def test_customer_can_challenge_l4(self):
        from ohm.boundary import check_can_challenge

        check_can_challenge("customer:acme_hvac", "L4")

    def test_customer_cannot_challenge_l1(self):
        from ohm.boundary import check_can_challenge

        with pytest.raises(PermissionDeniedError, match="L1"):
            check_can_challenge("customer:acme_hvac", "L1")

    def test_customer_can_support_l3(self):
        from ohm.boundary import check_can_support

        check_can_support("customer:acme_hvac", "L3")

    def test_customer_cannot_support_l2(self):
        from ohm.boundary import check_can_support

        with pytest.raises(PermissionDeniedError, match="L2"):
            check_can_support("customer:acme_hvac", "L2")

    def test_different_customers_cannot_update_each_others_edges(self):
        from ohm.boundary import check_can_update_edge

        with pytest.raises(PermissionDeniedError):
            check_can_update_edge("customer:acme_hvac", "customer:wayne_mfg", "edge_1")
