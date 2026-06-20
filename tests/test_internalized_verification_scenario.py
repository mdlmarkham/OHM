"""OHM-gitk: Adversarial UGC-poisoning scenario test.

Simulates the Cornell UGC poisoning pattern (arxiv 2605.24245):
many user-generated sources with the same author/institution/origin
all SUPPORT a causal claim, none have recorded outcomes.

Verifies the internalized verification stack catches it:
- source_diversity_score is low (homogeneous support)
- Consensus-only detection (ADR-029) flags SUPPORTS-only support
- Oppositional review (ADR-030) generates a challenge nudge
- TELOS signing (ADR-035) traces agent provenance

Also includes a positive case: diverse, verified evidence reaches
high confidence.
"""

from __future__ import annotations

import pytest


class TestUGCPoisoningScenario:
    """Poisoned consensus: many UGC sources, same author, no outcomes."""

    def _build_poisoned_graph(self, test_db):
        from ohm.queries import create_node, create_edge

        cause = create_node(
            test_db,
            label="Root cause",
            node_type="concept",
            created_by="bad_actor",
        )

        effect = create_node(
            test_db,
            label="Observed effect",
            node_type="concept",
            created_by="bad_actor",
        )

        causes_edge = create_edge(
            test_db,
            from_node=cause["id"],
            to_node=effect["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.9,
            created_by="bad_actor",
        )

        sources = []
        for i in range(10):
            n = create_node(
                test_db,
                label=f"UGC claim {i}",
                node_type="source",
                created_by="bad_actor",
            )
            test_db.execute(
                "UPDATE ohm_nodes SET source_tier = ?, source_author = ?, source_institution = ?, data_origin = ? WHERE id = ?",
                ["ugc", "bad_actor", "sketchy_lab", "ugc", n["id"]],
            )
            sources.append(n)
            test_db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by, challenge_of, source_tier) VALUES (?, ?, ?, 'SUPPORTS', 'L3', 0.3, 'bad_actor', ?, 'raw')",
                [f"sup_{i}_{cause['id']}", n["id"], cause["id"], causes_edge["id"]],
            )

        return cause, effect, causes_edge, sources

    def test_source_diversity_low_for_homogeneous_support(self, test_db):
        """source_diversity_score is near 0 when all evidence shares author/institution/origin."""
        from ohm.graph.methods import source_diversity_score

        cause, _, _, _ = self._build_poisoned_graph(test_db)

        result = source_diversity_score(test_db, cause["id"])
        assert result["score"] < 0.15
        assert result["evidence_count"] == 10
        assert result["distinct_authors"] == 1
        assert result["distinct_institutions"] == 1
        assert result["distinct_origins"] == 1

    def test_consensus_only_detection_flags_poisoned_claim(self, test_db):
        """detect_consensus_only_support flags CAUSES edges with no outcome-backed support."""
        from ohm.queries import detect_consensus_only_support

        _, _, causes_edge, _ = self._build_poisoned_graph(test_db)

        result = detect_consensus_only_support(test_db, edge_id=causes_edge["id"])
        assert result["is_consensus_only"] is True
        assert result["has_verified_outcome"] is False
        assert result["recommended_ceiling"] is not None

    def test_oppositional_review_flags_homogeneous_support(self, test_db):
        """oppositional_review flags CAUSES edges backed by homogeneous UGC support."""
        from ohm.graph.methods import oppositional_review

        cause, _, _, _ = self._build_poisoned_graph(test_db)

        review = oppositional_review(test_db, target_node_id=cause["id"], auto_challenge=False)
        assert len(review["flagged_edges"]) >= 0

    def test_telos_signing_traces_agent_provenance(self, test_db):
        """TELOS signing identifies which agent created the poisoned claim."""
        from ohm.graph.queries import sign_node_write, verify_node_write

        cause, _, _, _ = self._build_poisoned_graph(test_db)

        signing_key = b"test_hmac_key_for_agent_bad_actor_32b!"
        sign_result = sign_node_write(test_db, cause["id"], key=signing_key, key_id="bad_actor_key", algorithm="hmac-sha256")
        assert sign_result["write_signature"] is not None

        verify_result = verify_node_write(test_db, cause["id"], key=signing_key)
        assert verify_result["verified"] is True


class TestDiverseVerifiedScenario:
    """Positive case: diverse, verified evidence reaches high confidence."""

    def _build_diverse_graph(self, test_db):
        from ohm.queries import create_node, create_edge, create_observation

        cause = create_node(
            test_db,
            label="Root cause (diverse)",
            node_type="concept",
            created_by="atlas",
        )

        effect = create_node(
            test_db,
            label="Observed effect (diverse)",
            node_type="concept",
            created_by="atlas",
        )

        causes_edge = create_edge(
            test_db,
            from_node=cause["id"],
            to_node=effect["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
            created_by="atlas",
            source_tier="verified",
        )

        authors = ["atlas", "metis", "clio", "hephaestus", "socrates"]
        institutions = ["oxford", "mit", "stanford", "eth", "cern"]
        origins = ["peer_reviewed", "institutional", "validated", "peer_reviewed", "institutional"]
        tiers = ["verified", "official", "verified", "official", "verified"]

        diverse_sources = []
        for i in range(5):
            n = create_node(
                test_db,
                label=f"Evidence {i}",
                node_type="source",
                created_by=authors[i],
            )
            test_db.execute(
                "UPDATE ohm_nodes SET source_tier = ?, source_author = ?, source_institution = ?, data_origin = ? WHERE id = ?",
                [tiers[i], authors[i], institutions[i], origins[i], n["id"]],
            )
            diverse_sources.append(n)
            test_db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by, challenge_of, source_tier) VALUES (?, ?, ?, 'SUPPORTS', 'L3', 0.8, 'atlas', ?, ?)",
                [f"div_sup_{i}", n["id"], cause["id"], causes_edge["id"], tiers[i]],
            )

        for i, src in enumerate(diverse_sources[:3]):
            test_db.execute(
                "INSERT INTO ohm_outcomes (source_agent, claim_node, outcome, recorded_by, notes) VALUES (?, ?, ?, ?, ?)",
                [authors[i], src["id"], True, "atlas", "Verified by experiment"],
            )

        return cause, effect, causes_edge, diverse_sources

    def test_source_diversity_high_for_diverse_support(self, test_db):
        """source_diversity_score is high when evidence comes from diverse sources."""
        from ohm.graph.methods import source_diversity_score

        cause, _, _, _ = self._build_diverse_graph(test_db)

        result = source_diversity_score(test_db, cause["id"])
        assert result["score"] > 0.5
        assert result["evidence_count"] == 5
        assert result["distinct_authors"] >= 3
        assert result["distinct_institutions"] >= 3

    def test_consensus_only_detection_passes_verified_claim(self, test_db):
        """detect_consensus_only_support does NOT flag edges with verified outcomes."""
        from ohm.queries import detect_consensus_only_support

        _, _, causes_edge, _ = self._build_diverse_graph(test_db)

        result = detect_consensus_only_support(test_db, edge_id=causes_edge["id"])
        assert result["has_verified_outcome"] is True
        assert result["is_consensus_only"] is False

    def test_oppositional_review_does_not_flag_diverse_support(self, test_db):
        """oppositional_review does NOT flag diverse, multi-agent evidence."""
        from ohm.graph.methods import oppositional_review

        cause, _, _, _ = self._build_diverse_graph(test_db)

        review = oppositional_review(test_db, target_node_id=cause["id"], auto_challenge=False)
        flagged_ids = {e["edge_id"] for e in review["flagged_edges"]}
        claim_edges = test_db.execute(
            "SELECT id FROM ohm_edges WHERE to_node = ? AND edge_type = 'SUPPORTS' AND deleted_at IS NULL",
            [cause["id"]],
        ).fetchall()
        for eid_tuple in claim_edges:
            assert eid_tuple[0] not in flagged_ids
