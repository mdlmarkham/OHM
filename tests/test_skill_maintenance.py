"""Tests for OHM-854: Measurement-driven skill maintenance loop.

Covers signal detection, candidate generation, candidate writing,
evaluation, promotion, demotion, and the full round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ohm.mcp.skill_maintenance import (
    _skill_hash,
    detect_signals,
    generate_candidate,
    write_candidate,
    evaluate_candidate,
    promote_candidate,
    demote_candidate,
    run_skill_maintenance_round,
)


class TestSignalDetection:
    """Tests for detect_signals()."""

    def test_no_signals_on_empty_db(self, test_db):
        result = detect_signals(test_db)
        assert result == []

    def test_low_acceptance_signal_detected(self, test_db):
        for i in range(15):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, accepted) VALUES (?, 'a', 'node', 'source_citation', 'hint', false)",
                [f"sig_{i}"],
            )
        test_db.commit()

        signals = detect_signals(test_db)
        assert len(signals) == 1
        assert signals[0]["skill_name"] == "observation-recording"
        assert signals[0]["signal_type"] == "low_nudge_acceptance"
        assert signals[0]["acceptance_rate"] == 0.0

    def test_high_acceptance_no_signal(self, test_db):
        for i in range(15):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, accepted) VALUES (?, 'a', 'node', 'causal_edge_suggestion', 'info', true)",
                [f"ok_{i}"],
            )
        test_db.commit()

        signals = detect_signals(test_db)
        assert len(signals) == 0

    def test_unmapped_nudge_type_no_signal(self, test_db):
        for i in range(15):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, accepted) VALUES (?, 'a', 'node', 'unknown_nudge_type', 'info', false)",
                [f"unk_{i}"],
            )
        test_db.commit()

        signals = detect_signals(test_db)
        assert len(signals) == 0


class TestGenerateCandidate:
    """Tests for generate_candidate()."""

    def test_low_acceptance_adds_note(self):
        content = "# Skill: Test\n\nSome guidance."
        signal = {
            "signal_type": "low_nudge_acceptance",
            "suggestion": "Acceptance is low.",
            "nudge_type": "source_citation",
        }
        candidate = generate_candidate("observation-recording", content, signal)
        assert "Maintenance note" in candidate
        assert "Acceptance is low" in candidate
        assert candidate != content

    def test_unknown_signal_no_change(self):
        content = "# Skill: Test\n\nSome guidance."
        signal = {"signal_type": "unknown", "suggestion": "????"}
        candidate = generate_candidate("test", content, signal)
        assert candidate == content


class TestWriteCandidate:
    """Tests for write_candidate()."""

    def test_write_candidate(self, tmp_path):
        candidates_dir = tmp_path / "candidates"
        path = write_candidate("decision-node", "# Candidate", candidates_dir)
        assert path.exists()
        assert path.read_text() == "# Candidate"
        assert path.parent.name == "decision-node"


class TestEvaluateCandidate:
    """Tests for evaluate_candidate()."""

    def test_insufficient_data(self, test_db):
        result = evaluate_candidate(test_db, nudge_type="nonexistent")
        assert result["insufficient_data"] is True
        assert result["improved"] is False


class TestPromoteCandidate:
    """Tests for promote_candidate()."""

    def test_promote_replaces_default(self, tmp_path):
        default_dir = tmp_path / "default"
        skill_dir = default_dir / "decision-node"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Old content")

        candidate_dir = tmp_path / "candidates" / "decision-node"
        candidate_dir.mkdir(parents=True)
        candidate_path = candidate_dir / "SKILL.md"
        candidate_path.write_text("# New content")

        result = promote_candidate("decision-node", candidate_path, default_dir)
        assert result["status"] == "promoted"
        assert (skill_dir / "SKILL.md").read_text() == "# New content"
        assert not candidate_dir.exists()

    def test_promote_creates_skill_dir_if_missing(self, tmp_path):
        default_dir = tmp_path / "default"
        candidate_dir = tmp_path / "candidates" / "causal-edge"
        candidate_dir.mkdir(parents=True)
        candidate_path = candidate_dir / "SKILL.md"
        candidate_path.write_text("# New skill")

        result = promote_candidate("causal-edge", candidate_path, default_dir)
        assert result["status"] == "promoted"
        assert (default_dir / "causal-edge" / "SKILL.md").read_text() == "# New skill"


class TestDemoteCandidate:
    """Tests for demote_candidate()."""

    def test_demote_removes_candidate(self, tmp_path):
        candidates_dir = tmp_path / "candidates"
        candidate_dir = candidates_dir / "test-skill"
        candidate_dir.mkdir(parents=True)
        (candidate_dir / "SKILL.md").write_text("# Candidate")

        result = demote_candidate("test-skill", candidates_dir)
        assert result["status"] == "demoted"
        assert not candidate_dir.exists()

    def test_demote_not_found(self, tmp_path):
        candidates_dir = tmp_path / "candidates"
        result = demote_candidate("nonexistent", candidates_dir)
        assert result["status"] == "not_found"


class TestRunSkillMaintenanceRound:
    """Tests for run_skill_maintenance_round()."""

    def test_no_signals_returns_empty(self, test_db, tmp_path):
        result = run_skill_maintenance_round(
            test_db,
            default_skills_dir=tmp_path / "skills",
            candidates_dir=tmp_path / "candidates",
        )
        assert result["signals"] == []
        assert result["message"] == "No signals detected"

    def test_dry_run_generates_candidates(self, test_db, tmp_path):
        default_dir = tmp_path / "skills"
        skill_dir = default_dir / "observation-recording"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Observation Recording\n\nGuidance.")

        for i in range(15):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, accepted) VALUES (?, 'a', 'node', 'source_citation', 'hint', false)",
                [f"smr_{i}"],
            )
        test_db.commit()

        result = run_skill_maintenance_round(
            test_db,
            default_skills_dir=default_dir,
            candidates_dir=tmp_path / "candidates",
            dry_run=True,
        )
        assert len(result["signals"]) == 1
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["skill_name"] == "observation-recording"
        assert result["dry_run"] is True

    def test_skill_hash_is_deterministic(self):
        h1 = _skill_hash("content")
        h2 = _skill_hash("content")
        assert h1 == h2
        assert h1 != _skill_hash("different content")