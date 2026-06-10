"""Tests for ADR-022 Layer Promotion Constraints."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Helpers ─────────────────────────────────────────────────────────────────


def _create_node(
    conn: DuckDBPyConnection,
    *,
    label: str = "test_node",
    node_type: str = "concept",
    created_by: str = "agent_a",
    layer: str = "L0",
    url: str | None = None,
) -> str:
    node_id = f"{label.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence, url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [node_id, label, node_type, created_by, "team", "test", 1.0, url],
    )
    return node_id


def _create_edge(
    conn: DuckDBPyConnection,
    *,
    from_node: str,
    to_node: str,
    layer: str = "L3",
    edge_type: str = "CAUSES",
    created_by: str = "agent_a",
    confidence: float = 0.9,
    challenge_type: str | None = None,
) -> str:
    edge_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence, challenge_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_node, to_node, layer, edge_type, created_by, confidence, challenge_type],
    )
    return edge_id


def _create_observation(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    value: float = 0.8,
    created_by: str = "agent_a",
) -> str:
    obs_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_observations (id, node_id, type, value, source, created_by, scale) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [obs_id, node_id, "measurement", value, "analysis", created_by, "probability"],
    )
    return obs_id


def _create_outcome(
    conn: DuckDBPyConnection,
    *,
    claim_node: str,
    outcome: bool = True,
    source_agent: str = "agent_a",
) -> str:
    out_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_outcomes (id, source_agent, claim_node, outcome, recorded_by) VALUES (?, ?, ?, ?, ?)",
        [out_id, source_agent, claim_node, outcome, "test_runner"],
    )
    return out_id


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(test_db):
    return test_db


# ── L0 → L1 Tests ──────────────────────────────────────────────────────────


class TestL0toL1:
    def test_fragment_with_context_link_satisfies_constraints(self, db):
        fragment_id = _create_node(db, label="hunch", node_type="fragment", layer="L0")
        target_id = _create_node(db, label="context", node_type="concept")
        _create_edge(db, from_node=fragment_id, to_node=target_id, layer="L0", edge_type="CONTEXT_OF")

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(fragment_id, "L0", "L1", db)
        assert valid
        assert len(errors) == 0

    def test_fragment_without_context_link_gets_warning(self, db):
        fragment_id = _create_node(db, label="orphan_hunch", node_type="fragment", layer="L0")

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(fragment_id, "L0", "L1", db)
        assert valid  # advisory mode: still valid with warnings
        assert len(warnings) > 0
        assert any("min_context_links" in w for w in warnings)

    def test_fragment_without_context_link_rejected_in_strict(self, db):
        fragment_id = _create_node(db, label="orphan_hunch", node_type="fragment", layer="L0")

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(fragment_id, "L0", "L1", db, enforce=True)
        assert not valid
        assert len(errors) > 0
        assert any("min_context_links" in e for e in errors)

    def test_count_context_links(self, db):
        fragment_id = _create_node(db, label="linked_frag", node_type="fragment", layer="L0")
        t1 = _create_node(db, label="target1")
        t2 = _create_node(db, label="target2")
        _create_edge(db, from_node=fragment_id, to_node=t1, layer="L0", edge_type="CONTEXT_OF")
        _create_edge(db, from_node=fragment_id, to_node=t2, layer="L0", edge_type="INSPIRED_BY")

        from ohm.graph.constraints import count_context_links

        assert count_context_links(db, fragment_id) == 2


# ── L1 → L2 Tests ──────────────────────────────────────────────────────────


class TestL1toL2:
    def test_node_with_source_and_observation_passes(self, db):
        node_id = _create_node(db, label="claim", node_type="idea", layer="L1")
        source_id = _create_node(db, label="source", node_type="source", url="https://example.com")
        _create_edge(db, from_node=node_id, to_node=source_id, layer="L2", edge_type="REFERENCES")
        _create_observation(db, node_id=node_id)

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L1", "L2", db)
        assert valid
        assert len(errors) == 0

    def test_node_without_source_gets_warning(self, db):
        node_id = _create_node(db, label="unsourced_claim", node_type="idea", layer="L1")
        _create_observation(db, node_id=node_id)

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L1", "L2", db)
        assert valid
        assert any("min_sources" in w for w in warnings)

    def test_count_sources(self, db):
        node_id = _create_node(db, label="sourced_claim", node_type="idea", layer="L1")
        s1 = _create_node(db, label="src1", node_type="source", url="https://a.com")
        s2 = _create_node(db, label="src2", node_type="source", url="https://b.com")
        _create_edge(db, from_node=node_id, to_node=s1, layer="L2", edge_type="REFERENCES")
        _create_edge(db, from_node=node_id, to_node=s2, layer="L2", edge_type="REFERENCES")

        from ohm.graph.constraints import count_sources

        assert count_sources(db, node_id) == 2


# ── L2 → L3 Tests ──────────────────────────────────────────────────────────


class TestL2toL3:
    def test_node_with_diverse_sources_and_outcomes_passes(self, db):
        node_id = _create_node(db, label="knowledge", node_type="pattern", layer="L2")
        s1 = _create_node(db, label="src1", node_type="source", url="https://a.com")
        s2 = _create_node(db, label="src2", node_type="source", url="https://b.com")
        _create_edge(db, from_node=node_id, to_node=s1, layer="L2", edge_type="REFERENCES", created_by="agent_a")
        _create_edge(db, from_node=node_id, to_node=s2, layer="L2", edge_type="REFERENCES", created_by="agent_b")
        _create_observation(db, node_id=node_id, value=0.8)
        _create_observation(db, node_id=node_id, value=0.7)
        _create_outcome(db, claim_node=node_id, outcome=True)
        _create_edge(db, from_node=node_id, to_node=s1, layer="L2", edge_type="REFERENCES")

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L2", "L3", db)
        assert valid
        assert len(errors) == 0

    def test_node_without_outcomes_gets_warning(self, db):
        node_id = _create_node(db, label="unverified_knowledge", node_type="pattern", layer="L2")
        s1 = _create_node(db, label="src1", node_type="source", url="https://a.com")
        _create_edge(db, from_node=node_id, to_node=s1, layer="L2", edge_type="REFERENCES")
        _create_observation(db, node_id=node_id, value=0.8)

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L2", "L3", db)
        assert valid
        assert any("min_outcomes" in w for w in warnings)

    def test_chain_validity(self, db):
        node_id = _create_node(db, label="weak_claim", node_type="pattern", layer="L2")
        _create_observation(db, node_id=node_id, value=0.5)
        _create_observation(db, node_id=node_id, value=0.2)

        from ohm.graph.constraints import chain_validity

        cv = chain_validity(db, node_id)
        assert cv == pytest.approx(0.2, abs=0.01)


# ── L3 → L4 Tests ──────────────────────────────────────────────────────────


class TestL3toL4:
    def test_node_with_strong_support_satisfies_l4(self, db):
        node_id = _create_node(db, label="prospect", node_type="pattern", layer="L3")

        for i in range(3):
            supporter = _create_node(db, label=f"supporter_{i}", node_type="concept")
            _create_edge(db, from_node=supporter, to_node=node_id, layer="L3", edge_type="SUPPORTS")
            _create_outcome(db, claim_node=node_id, outcome=True)

        _create_observation(db, node_id=node_id, value=0.9)
        _create_observation(db, node_id=node_id, value=0.8)

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L3", "L4", db)
        assert valid
        assert len(errors) == 0

    def test_node_with_open_challenge_blocked(self, db):
        node_id = _create_node(db, label="challenged_claim", node_type="pattern", layer="L3")
        challenger = _create_node(db, label="challenger", node_type="agent")
        _create_edge(db, from_node=challenger, to_node=node_id, layer="L3", edge_type="CHALLENGED_BY", confidence=0.3)

        from ohm.graph.constraints import validate_layer_promotion

        valid, warnings, errors = validate_layer_promotion(node_id, "L3", "L4", db, enforce=True)
        assert not valid
        assert any("no_open_challenges" in e for e in errors)


# ── Edge Constraint Tests ──────────────────────────────────────────────────


class TestEdgeConstraints:
    def test_causes_at_l1_rejected(self, db):
        from_node = _create_node(db, label="source", node_type="concept")
        to_node = _create_node(db, label="target", node_type="concept")
        ref_node = _create_node(db, label="ref", node_type="source", url="https://ref.com")
        _create_edge(db, from_node=from_node, to_node=ref_node, layer="L2", edge_type="REFERENCES")

        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints(
            "CAUSES",
            "L1",
            db,
            from_node=from_node,
            enforce=True,
        )
        assert not valid
        assert any("L2" in e for e in errors)

    def test_causes_at_l2_with_references_passes(self, db):
        from_node = _create_node(db, label="source", node_type="concept")
        to_node = _create_node(db, label="target", node_type="concept")
        ref_node = _create_node(db, label="ref", node_type="source", url="https://ref.com")
        _create_edge(db, from_node=from_node, to_node=ref_node, layer="L2", edge_type="REFERENCES")

        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints(
            "CAUSES",
            "L2",
            db,
            from_node=from_node,
            enforce=True,
        )
        assert valid

    def test_causes_without_references_gets_warning(self, db):
        from_node = _create_node(db, label="source", node_type="concept")
        to_node = _create_node(db, label="target", node_type="concept")

        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints(
            "CAUSES",
            "L2",
            db,
            from_node=from_node,
            enforce=False,
        )
        assert valid
        assert any("REFERENCES" in w for w in warnings)

    def test_challenged_by_requires_confidence(self, db):
        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints(
            "CHALLENGED_BY",
            "L3",
            db,
            confidence=None,
            enforce=True,
        )
        assert not valid
        assert any("confidence" in e for e in errors)

    def test_supports_at_l1_passes(self, db):
        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints("SUPPORTS", "L1", db, enforce=True)
        assert valid

    def test_predicts_at_l2_rejected(self, db):
        from ohm.graph.constraints import validate_edge_constraints

        valid, warnings, errors = validate_edge_constraints(
            "PREDICTS",
            "L2",
            db,
            from_node="test_node",
            enforce=True,
        )
        assert not valid
        assert any("L3" in e for e in errors)


# ── Effective Layer Tests ──────────────────────────────────────────────────


class TestEffectiveLayer:
    def test_l0_node_effective_unchanged(self, db):
        node_id = _create_node(db, label="fragment", node_type="fragment", layer="L0")
        from ohm.graph.constraints import effective_layer

        eff, _ = effective_layer(db, node_id)
        assert eff == "L0"

    def test_l3_node_with_decayed_evidence_demoted(self, db):
        node_id = _create_node(db, label="decayed_claim", node_type="pattern")
        other = _create_node(db, label="other")
        _create_edge(db, from_node=node_id, to_node=other, layer="L3", edge_type="CAUSES")
        _create_observation(db, node_id=node_id, value=0.05)

        from ohm.graph.constraints import effective_layer

        eff, status = effective_layer(db, node_id)
        assert eff in ("L1", "L2"), f"Expected demotion, got {eff}"

    def test_l4_node_with_strong_support_stays_l4(self, db):
        node_id = _create_node(db, label="strong_prospect", node_type="pattern")
        anchor = _create_node(db, label="anchor")
        _create_edge(db, from_node=node_id, to_node=anchor, layer="L4", edge_type="EXPECTS")
        _create_observation(db, node_id=node_id, value=0.9)
        for i in range(3):
            supporter = _create_node(db, label=f"sp_{i}")
            _create_edge(db, from_node=supporter, to_node=node_id, layer="L3", edge_type="SUPPORTS")
        for _ in range(2):
            _create_outcome(db, claim_node=node_id, outcome=True)

        from ohm.graph.constraints import effective_layer

        eff, status = effective_layer(db, node_id)
        assert eff == "L4", f"Expected L4, got {eff}"

    def test_constraint_status_includes_requirements(self, db):
        node_id = _create_node(db, label="status_check", node_type="idea", layer="L3")
        s1 = _create_node(db, label="src1", node_type="source", url="https://a.com")
        s2 = _create_node(db, label="src2", node_type="source", url="https://b.com")
        _create_edge(db, from_node=node_id, to_node=s1, layer="L2", edge_type="REFERENCES", created_by="agent_a")
        _create_edge(db, from_node=node_id, to_node=s2, layer="L2", edge_type="REFERENCES", created_by="agent_b")
        _create_observation(db, node_id=node_id, value=0.8)
        _create_observation(db, node_id=node_id, value=0.7)
        _create_outcome(db, claim_node=node_id, outcome=True)

        from ohm.graph.constraints import effective_layer

        eff, status = effective_layer(db, node_id)
        assert "L2_requirements" in status
        assert "min_sources" in status["L2_requirements"]


# ── promote_fragment Integration Test ───────────────────────────────────────


class TestPromoteFragmentConstraints:
    def test_promote_fragment_without_context_links_raises(self, db):
        fragment_id = _create_node(db, label="orphan", node_type="fragment", layer="L0")

        from ohm.queries import promote_fragment
        from ohm.exceptions import ConstraintViolationError

        with pytest.raises(ConstraintViolationError):
            promote_fragment(db, fragment_id=fragment_id, promoted_by="test_agent")

    def test_promote_fragment_with_context_links_succeeds(self, db):
        fragment_id = _create_node(db, label="anchored", node_type="fragment", layer="L0")
        context = _create_node(db, label="context")
        _create_edge(db, from_node=fragment_id, to_node=context, layer="L0", edge_type="CONTEXT_OF")

        from ohm.queries import promote_fragment

        result = promote_fragment(db, fragment_id=fragment_id, promoted_by="test_agent")
        assert "concept" in result
        assert result["concept"]["type"] == "concept"


# ── Admin Constraint Report ────────────────────────────────────────────────


class TestConstraintReport:
    def test_report_contains_all_layers(self, db):
        from ohm.graph.constraints import PROMOTION_CONSTRAINTS

        # Create a node and run report
        node_id = _create_node(db, label="test", node_type="concept", layer="L2")
        _create_observation(db, node_id=node_id, value=0.8)

        from ohm.graph.constraints import (
            count_sources,
            count_observations,
            count_outcomes,
            count_verified_outcomes,
            count_open_challenges,
            count_L3_supporting_nodes,
            chain_validity,
            count_context_links,
        )

        assert count_observations(db, node_id) >= 1
        assert count_sources(db, node_id) >= 0
        assert count_outcomes(db, node_id) == 0
        assert count_verified_outcomes(db, node_id) == 0
        assert count_open_challenges(db, node_id) == 0
        assert count_context_links(db, node_id) == 0

    def test_constraint_dispatch_all_keys(self, db):
        from ohm.graph.constraints import CONSTRAINT_DISPATCH

        assert "min_context_links" in CONSTRAINT_DISPATCH
        assert "min_sources" in CONSTRAINT_DISPATCH
        assert "min_observations" in CONSTRAINT_DISPATCH
        assert "min_chain_validity" in CONSTRAINT_DISPATCH
        assert "no_open_challenges" in CONSTRAINT_DISPATCH
        assert "require_references_edge" in CONSTRAINT_DISPATCH
        assert "min_L3_support" in CONSTRAINT_DISPATCH
        assert "min_outcomes" in CONSTRAINT_DISPATCH
        assert "min_verified_outcomes" in CONSTRAINT_DISPATCH
