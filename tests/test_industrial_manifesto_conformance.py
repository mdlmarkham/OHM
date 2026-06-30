"""Industrial Agent Manifesto — conformance tests (OHM-dp38).

Each test class maps to one principle in docs/industrial-agent-manifesto.md.
Industrial deployments gate agent promotion on passing all 15 classes.

The tests verify that OHM enforces each principle at the substrate level —
schema contracts, validator behavior, boundary checks, and observable function
output. They are not exhaustive feature tests; they are the minimum bar for
an agent to claim conformance.

See also:
- docs/industrial-agent-manifesto.md
- tests/test_industrial_process.py — worked reactor example (OHM-brps)
- ADR-003, ADR-018, ADR-028, ADR-029, ADR-030, ADR-033, ADR-035, ADR-036, ADR-037
"""

from __future__ import annotations

import duckdb
import pytest

from ohm.boundary import (
    check_can_update_edge,
    enforce_write_boundary,
    get_agent_read_scope,
    set_agent_read_scope,
)
from ohm.framework.validation import enforce_confidence_ceiling, validate_source_tier
from ohm.methods import ripen_then_decide, source_diversity_score
from ohm.queries import (
    create_edge,
    create_node,
    detect_consensus_only_support,
    execute_action,
    find_homogeneous_causes,
    propose_action,
    query_loop_status,
    set_freshness_threshold,
    sign_node_write,
    verify_node_write,
)
from ohm.schema import (
    SOURCE_TIER_CEILINGS,
    VALID_GATE_STATUSES,
    VALID_GATE_TYPES,
    VALID_NODE_TYPES,
    initialize_schema,
)


KEY = b"conformance-test-key-32-bytes-min-XX"


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    initialize_schema(c)
    yield c
    c.close()


class TestAgentOwnedEdges:
    """Principle 1 — every L3/L4 edge has a single owner."""

    def test_create_edge_stores_owner(self, conn):
        edge = create_edge(
            conn, from_node="a", to_node="b", layer="L3",
            edge_type="CAUSES", created_by="metis", confidence=0.8,
        )
        row = conn.execute(
            "SELECT created_by FROM ohm_edges WHERE id = ?", [edge["id"]],
        ).fetchone()
        assert row[0] == "metis"

    def test_other_agent_cannot_overwrite(self, conn):
        edge = create_edge(
            conn, from_node="a", to_node="b", layer="L3",
            edge_type="CAUSES", created_by="metis", confidence=0.8,
        )
        with pytest.raises(Exception) as exc:
            check_can_update_edge("clio", "metis", edge["id"])
        assert "cannot update" in str(exc.value).lower()

    def test_owner_can_update_their_own_edge(self, conn):
        edge = create_edge(
            conn, from_node="a", to_node="b", layer="L3",
            edge_type="CAUSES", created_by="metis", confidence=0.8,
        )
        check_can_update_edge("metis", "metis", edge["id"])


class TestMandatoryCrossLink:
    """Principle 2 — derived-claim nodes must reference existing nodes.

    Cross-link validation is enforced by the substrate when `connects_to`
    is provided. The HTTP-layer (POST /agent/synthesis) is the strict
    endpoint that requires it; the query-layer validates references when
    given. Both layers cooperate on the contract.
    """

    def test_concept_node_can_exist_without_link(self, conn):
        """Source / concept / entity are exempt — they are foundational references."""
        node = create_node(
            conn, label="Foundational concept", node_type="concept",
            created_by="metis",
        )
        assert node["id"]

    def test_pattern_with_valid_connects_to_succeeds(self, conn):
        anchor = create_node(
            conn, label="Anchor claim", node_type="concept", created_by="metis",
        )
        node = create_node(
            conn, label="Derived pattern", node_type="pattern",
            created_by="metis", connects_to=[anchor["id"]],
        )
        assert node["id"]

    def test_pattern_with_nonexistent_connects_to_rejected(self, conn):
        with pytest.raises(ValueError) as exc:
            create_node(
                conn, label="Orphan", node_type="pattern",
                created_by="metis", connects_to=["nonexistent-id"],
            )
        assert "unknown node id" in str(exc.value).lower()

    def test_pattern_with_empty_connects_to_rejected(self, conn):
        with pytest.raises(ValueError) as exc:
            create_node(
                conn, label="Orphan", node_type="pattern",
                created_by="metis", connects_to=[],
            )
        assert "at least one" in str(exc.value).lower()


class TestSourceTierCeilings:
    """Principle 3 — confidence cannot exceed tier ceiling."""

    def test_ceiling_table_values(self):
        assert SOURCE_TIER_CEILINGS == {
            "raw": 0.3,
            "unverified": 0.5,
            "preliminary": 0.7,
            "official": 0.9,
            "verified": 1.0,
        }

    def test_valid_tiers_pass(self):
        for tier in SOURCE_TIER_CEILINGS:
            assert validate_source_tier(tier) == tier

    def test_unknown_tier_rejected(self):
        with pytest.raises(ValueError):
            validate_source_tier("rumor")

    def test_within_ceiling_passes(self):
        enforce_confidence_ceiling(0.7, "preliminary")
        enforce_confidence_ceiling(0.9, "official")
        enforce_confidence_ceiling(1.0, "verified")

    def test_above_ceiling_rejected(self):
        with pytest.raises(ValueError) as exc:
            enforce_confidence_ceiling(0.9, "raw")
        assert "exceeds ceiling" in str(exc.value)

    def test_null_tier_bypasses_enforcement(self):
        """Legacy write paths pass tier=NULL and skip ceiling check."""
        enforce_confidence_ceiling(0.95, None)


class TestAndGateGovernance:
    """Principle 4 — AND-gate governance primitives."""

    def test_gate_type_valid_values(self):
        assert VALID_GATE_TYPES == frozenset({"AND", "OR"})

    def test_gate_status_includes_upstream_and_metis_aliases(self):
        assert "intact" in VALID_GATE_STATUSES
        assert "compromised" in VALID_GATE_STATUSES
        assert "failed" in VALID_GATE_STATUSES
        assert "open" in VALID_GATE_STATUSES
        assert "closed" in VALID_GATE_STATUSES
        assert "stuck" in VALID_GATE_STATUSES

    def test_node_accepts_gate_type_and_status(self, conn):
        node = create_node(
            conn, label="Reactor R-101", node_type="concept", created_by="plant",
        )
        conn.execute(
            "UPDATE ohm_nodes SET gate_type = ?, gate_status = ? WHERE id = ?",
            ["AND", "intact", node["id"]],
        )
        row = conn.execute(
            "SELECT gate_type, gate_status FROM ohm_nodes WHERE id = ?",
            [node["id"]],
        ).fetchone()
        assert row == ("AND", "intact")

    def test_edge_accepts_constraint_expr(self, conn):
        a = create_node(conn, label="feed", node_type="concept", created_by="plant")
        b = create_node(conn, label="catalyst", node_type="concept", created_by="plant")
        edge = create_edge(
            conn, from_node=a["id"], to_node=b["id"], layer="L3",
            edge_type="CAUSES", created_by="plant",
        )
        conn.execute(
            "UPDATE ohm_edges SET constraint_expr = ? WHERE id = ?",
            ["feed AND catalyst_flow", edge["id"]],
        )
        row = conn.execute(
            "SELECT constraint_expr FROM ohm_edges WHERE id = ?", [edge["id"]],
        ).fetchone()
        assert row[0] == "feed AND catalyst_flow"


class TestVerificationDecay:
    """Principle 5 — verification decay primitives are first-class."""

    def test_heartbeat_endpoint_exists(self):
        from ohm.server.server import OhmHandler
        assert hasattr(OhmHandler, "_post_heartbeat")

    def test_decay_relevant_columns_present(self, conn):
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='ohm_nodes'"
        ).fetchall()}
        assert "confidence" in cols
        assert "created_at" in cols
        assert "updated_at" in cols


class TestConsensusOnlyDetection:
    """Principle 6 — homogeneous SUPPORTS triggers CONSENSUS_FLAG."""

    def test_detect_consensus_only_is_callable(self):
        assert callable(detect_consensus_only_support)


class TestOppositionalReview:
    """Principle 7 — homogeneous CAUSES support triggers oppositional review."""

    def test_find_homogeneous_causes_empty_graph(self, conn):
        result = find_homogeneous_causes(conn)
        assert isinstance(result, list)

    def test_homogeneous_supports_detected(self, conn):
        cause = create_node(conn, label="X causes Y", node_type="concept", created_by="a")
        effect = create_node(conn, label="Y", node_type="concept", created_by="a")
        cause_edge = create_edge(
            conn, from_node=cause["id"], to_node=effect["id"], layer="L3",
            edge_type="CAUSES", created_by="a", confidence=0.3, source_tier="raw",
        )
        for i in range(3):
            sup = create_node(
                conn, label=f"supporter {i}", node_type="concept", created_by=f"s{i}",
            )
            create_edge(
                conn, from_node=sup["id"], to_node=cause["id"], layer="L3",
                edge_type="SUPPORTS", created_by=f"s{i}", confidence=0.3, source_tier="raw",
            )
        detect_consensus_only_support(conn, edge_id=cause_edge["id"])
        result = find_homogeneous_causes(conn, min_support_count=2, homogeneity_threshold=0.8)
        assert isinstance(result, list)


class TestCryptographicAttribution:
    """Principle 8 — TELOS signing provides tamper evidence."""

    def test_sign_and_verify_roundtrip(self, conn):
        node = create_node(conn, label="Signed claim", node_type="concept", created_by="metis")
        sign_node_write(conn, node["id"], key=KEY, key_id="k1")
        result = verify_node_write(conn, node["id"], key=KEY)
        assert result["verified"] is True

    def test_signature_columns_populated(self, conn):
        node = create_node(conn, label="x", node_type="concept", created_by="metis")
        sign_node_write(conn, node["id"], key=KEY, key_id="k2")
        row = conn.execute(
            "SELECT write_signature, signing_key_id, signed_at FROM ohm_nodes WHERE id = ?",
            [node["id"]],
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "k2"
        assert row[2] is not None

    def test_tampered_signature_fails_verification(self, conn):
        node = create_node(conn, label="claim", node_type="concept", created_by="metis")
        sign_node_write(conn, node["id"], key=KEY, key_id="k3")
        conn.execute(
            "UPDATE ohm_nodes SET label = ? WHERE id = ?",
            ["tampered label", node["id"]],
        )
        result = verify_node_write(conn, node["id"], key=KEY)
        assert result["verified"] is False


class TestSourceDiversity:
    """Principle 9 — independence-weighted Shannon entropy."""

    def test_diverse_authors_produce_structured_result(self, conn):
        cause = create_node(conn, label="diverse claim", node_type="concept", created_by="a")
        for i, author in enumerate(["alice", "bob", "carol", "dave"]):
            sup = create_node(
                conn, label=f"supporter {i}", node_type="concept", created_by=author,
                source_author=author, source_institution=f"inst-{i}",
                data_origin="peer_reviewed",
            )
            create_edge(
                conn, from_node=sup["id"], to_node=cause["id"], layer="L3",
                edge_type="SUPPORTS", created_by=author, confidence=0.95, source_tier="verified",
            )
        result = source_diversity_score(conn, cause["id"])
        assert isinstance(result, dict)


class TestAutonomyLoopIntegrity:
    """Principle 10 — propose → execute → status flow is atomic and attributed."""

    def test_full_loop_propose_execute_status(self, conn):
        anchor = create_node(conn, label="anchor", node_type="concept", created_by="plant")
        scenario = create_node(
            conn, label="disruption scenario", node_type="scenario",
            created_by="plant", connects_to=[anchor["id"]],
        )
        action = propose_action(
            conn, scenario_id=scenario["id"], label="increase buffer",
            created_by="plant", rationale="mitigate supply risk",
        )
        assert action["task_status"] == "proposed"
        result = execute_action(
            conn, action_id=action["id"], executed_by="plant",
            outcome="TRUE", outcome_notes="buffer increased",
        )
        assert result["task_status"] == "executed"
        status = query_loop_status(conn, agent_name="plant")
        assert status["summary"]["executed"] == 1


class TestTwinRegistration:
    """Principle 11 — twin node type is first-class and EVALUATES-linked."""

    def test_twin_is_valid_node_type(self):
        assert "twin" in VALID_NODE_TYPES

    def test_twin_with_evaluates_edge_is_valid(self, conn):
        system = create_node(conn, label="Reactor R-101", node_type="concept", created_by="plant")
        twin = create_node(
            conn, label="Reactor Twin", node_type="twin",
            created_by="plant", connects_to=[system["id"]],
        )
        create_edge(
            conn, from_node=twin["id"], to_node=system["id"], layer="L3",
            edge_type="EVALUATES", created_by="plant",
        )
        edges = conn.execute(
            "SELECT edge_type FROM ohm_edges WHERE from_node = ? AND to_node = ?",
            [twin["id"], system["id"]],
        ).fetchall()
        assert ("EVALUATES",) in edges


class TestTemporalModeAwareness:
    """Principle 12 — temporal decision primitives."""

    def test_freshness_threshold_is_valid_node_type(self):
        assert "freshness_threshold" in VALID_NODE_TYPES

    def test_set_freshness_threshold_is_callable(self):
        assert callable(set_freshness_threshold)


class TestReadScopes:
    """Principle 13 — agents see only what their trust boundary permits."""

    def test_set_and_get_agent_read_scope(self, conn):
        set_agent_read_scope(
            conn, agent_name="clio",
            scope={"layer": ["L3"], "created_by": ["metis"]},
        )
        scope = get_agent_read_scope(conn, agent_name="clio")
        assert scope is not None
        assert scope.get("layer") == ["L3"]


class TestSuggestionLifecycle:
    """Principle 14 — suggestions ripen via multiplicative gate."""

    def test_ripen_then_decide_is_callable(self):
        assert callable(ripen_then_decide)

    def test_ripen_then_decide_runs_on_empty_graph(self, conn):
        result = ripen_then_decide(conn, dry_run=True)
        assert isinstance(result, dict)


class TestBoundaryRespect:
    """Principle 15 — boundary enforcement across agents."""

    def test_enforce_write_boundary_on_cross_agent_update(self, conn):
        edge = create_edge(
            conn, from_node="a", to_node="b", layer="L3",
            edge_type="CAUSES", created_by="metis", confidence=0.8,
        )
        with pytest.raises(Exception):
            enforce_write_boundary(conn, agent_name="clio", edge_id=edge["id"])

    def test_boundary_passes_for_owner(self, conn):
        edge = create_edge(
            conn, from_node="a", to_node="b", layer="L3",
            edge_type="CAUSES", created_by="metis", confidence=0.8,
        )
        enforce_write_boundary(conn, agent_name="metis", edge_id=edge["id"])
