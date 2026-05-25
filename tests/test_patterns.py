"""Tests for cross-instance pattern extraction and seeding (OHM-tss4.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ohm.patterns import (
    anonymize_text,
    extract_patterns,
    load_patterns,
    merge_patterns,
    run_extraction,
    save_patterns,
    seed_patterns,
)
from ohm.tenant import TenantManager


class TestAnonymizeText:
    def test_strips_phone_numbers(self):
        assert "[PHONE]" in anonymize_text("Call 555-123-4567 for help")

    def test_strips_email_addresses(self):
        assert "[EMAIL]" in anonymize_text("Contact user@example.com for info")

    def test_strips_ip_addresses(self):
        assert "[IP]" in anonymize_text("Server at 192.168.1.1 is down")

    def test_strips_uuids(self):
        assert "[ID]" in anonymize_text("Node a1b2c3d4-e5f6-7890-abcd-ef1234567890 failed")

    def test_preserves_non_pii(self):
        text = "AND-OR conversion pattern improves reliability"
        assert anonymize_text(text) == text

    def test_strips_names(self):
        assert "[NAME]" in anonymize_text("John Smith reported the issue")


class TestExtractPatterns:
    def test_extracts_l3_pattern_nodes(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("test_tenant")
        store = tm.get_store("test_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'AND-OR conversion', 'pattern', 'agent', CURRENT_TIMESTAMP)"
        )
        patterns = extract_patterns(store, domain="ohm")
        assert len(patterns) == 1
        assert patterns[0]["label"] == "AND-OR conversion"
        assert patterns[0]["domain"] == "ohm"
        assert patterns[0]["tags"] == ["pattern", "ohm"]
        tm.close()

    def test_extracts_idea_nodes(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("test_tenant")
        store = tm.get_store("test_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'Batch before peak', 'idea', 'agent', CURRENT_TIMESTAMP)"
        )
        patterns = extract_patterns(store, domain="ohm")
        assert len(patterns) == 1
        tm.close()

    def test_skips_non_pattern_nodes(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("test_tenant")
        store = tm.get_store("test_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'Equipment A', 'equipment', 'agent', CURRENT_TIMESTAMP)"
        )
        patterns = extract_patterns(store, domain="ohm")
        assert len(patterns) == 0
        tm.close()

    def test_anonymizes_pii_in_labels(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("test_tenant")
        store = tm.get_store("test_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'Pattern from John Smith about 555-123-4567', 'pattern', 'agent', CURRENT_TIMESTAMP)"
        )
        patterns = extract_patterns(store, domain="ohm")
        assert "[NAME]" in patterns[0]["label"]
        assert "[PHONE]" in patterns[0]["label"]
        assert "John Smith" not in patterns[0]["label"]
        tm.close()


class TestMergePatterns:
    def test_merge_new_pattern(self):
        existing = []
        new = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": ["ohm"], "domain": "ohm", "sample_size": 1}]
        merged = merge_patterns(existing, new)
        assert len(merged) == 1

    def test_merge_increments_sample_size(self):
        existing = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": ["ohm"], "domain": "ohm", "sample_size": 1}]
        new = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": ["ohm"], "domain": "ohm", "sample_size": 1}]
        merged = merge_patterns(existing, new)
        assert merged[0]["sample_size"] == 2
        assert merged[0]["confidence"] > 0.5

    def test_confidence_capped_at_095(self):
        existing = [{"id": "p1", "label": "A", "confidence": 0.9, "tags": [], "domain": "ohm", "sample_size": 9}]
        new = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": [], "domain": "ohm", "sample_size": 1}]
        merged = merge_patterns(existing, new)
        assert merged[0]["confidence"] == 0.95

    def test_merge_combines_tags(self):
        existing = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": ["ohm"], "domain": "ohm", "sample_size": 1}]
        new = [{"id": "p1", "label": "A", "confidence": 0.5, "tags": ["topo"], "domain": "ohm", "sample_size": 1}]
        merged = merge_patterns(existing, new)
        assert "ohm" in merged[0]["tags"]
        assert "topo" in merged[0]["tags"]


class TestSaveLoadPatterns:
    def test_save_and_load(self, tmp_path):
        patterns = [{"id": "p1", "label": "A", "domain": "ohm"}]
        save_patterns(patterns, tmp_path, "ohm")
        loaded = load_patterns(tmp_path, "ohm")
        assert len(loaded) == 1
        assert loaded[0]["id"] == "p1"

    def test_load_empty_when_no_file(self, tmp_path):
        patterns = load_patterns(tmp_path, "ohm")
        assert patterns == []


class TestSeedPatterns:
    def test_seeds_matching_domain(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("new_tenant")
        store = tm.get_store("new_tenant")
        patterns = [
            {"id": "p1", "label": "AND-OR conversion", "domain": "ohm", "tags": ["pattern"]},
            {"id": "p2", "label": "Topo pattern", "domain": "topo", "tags": ["pattern"]},
        ]
        count = seed_patterns(store, patterns, domain="ohm")
        assert count == 1
        nodes = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'pattern'").fetchone()[0]
        assert nodes == 1
        tm.close()

    def test_seeded_patterns_have_platform_provenance(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants")
        tm.provision("new_tenant")
        store = tm.get_store("new_tenant")
        patterns = [{"id": "p1", "label": "Test pattern", "domain": "ohm", "tags": []}]
        seed_patterns(store, patterns, domain="ohm")
        created_by = store.conn.execute("SELECT created_by FROM ohm_nodes WHERE type = 'pattern'").fetchone()[0]
        assert created_by == "platform_pattern"
        tm.close()


class TestRunExtraction:
    def test_skips_opted_out_tenants(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants", shared_patterns_dir=tmp_path / "shared")
        tm.provision("private_tenant")
        store = tm.get_store("private_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'Secret pattern', 'pattern', 'agent', CURRENT_TIMESTAMP)"
        )
        shared_dir = tmp_path / "shared"
        results = run_extraction(tm, shared_dir)
        assert "private_tenant" not in results
        tm.close()

    def test_extracts_from_opted_in_tenants(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants", shared_patterns_dir=tmp_path / "shared")
        tm.provision("sharing_tenant")
        meta = tm._read_meta("sharing_tenant")
        meta["shared_patterns"] = True
        tm._write_meta("sharing_tenant", meta)

        store = tm.get_store("sharing_tenant")
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
            "VALUES ('n1', 'Useful pattern', 'pattern', 'agent', CURRENT_TIMESTAMP)"
        )
        shared_dir = tmp_path / "shared"
        results = run_extraction(tm, shared_dir)
        assert "sharing_tenant" in results
        assert results["sharing_tenant"]["extracted"] == 1
        tm.close()
