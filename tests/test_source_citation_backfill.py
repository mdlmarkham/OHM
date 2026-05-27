"""Test source citation backfill logic (OHM-wdrg.3)."""

from __future__ import annotations

import pytest

from scripts.source_citation_backfill import (
    CANONICAL_SOURCES,
    AGENT_SOURCES,
    GENERIC_SOURCES,
    parse_compound_source,
)


class TestParseCompoundSource:
    """Tests for compound source string parsing."""

    def test_single_known_source(self):
        assert parse_compound_source("guardian") == ["guardian"]
        assert parse_compound_source("reuters") == ["reuters"]
        assert parse_compound_source("ap") == ["ap"]

    def test_compound_sources(self):
        assert "guardian" in parse_compound_source("guardian_reuters_ap")
        assert "reuters" in parse_compound_source("guardian_reuters_ap")
        assert "ap" in parse_compound_source("guardian_reuters_ap")

    def test_hyphenated_compound(self):
        result = parse_compound_source("al-monitor_reuters")
        assert "al-monitor" in result
        assert "reuters" in result

    def test_with_date_suffix_stripped(self):
        result = parse_compound_source("guardian_reuters_ap_2026_05_27")
        assert "guardian" in result
        assert "reuters" in result
        assert "ap" in result

    def test_with_may_suffix(self):
        result = parse_compound_source("guardian_may2026")
        assert "guardian" in result

    def test_compound_with_polymarket(self):
        result = parse_compound_source("polymarket_reuters")
        assert "polymarket" in result
        assert "reuters" in result

    def test_empty_source(self):
        assert parse_compound_source("") == []

    def test_no_match_returns_empty(self):
        result = parse_compound_source("unknown_source_xyz")
        assert result == []

    def test_source_with_underscore_day_suffix(self):
        result = parse_compound_source("guardian_day5")
        assert "guardian" in result

    def test_canonical_sources_complete(self):
        """Verify all canonical sources are in the mapping."""
        assert "reuters" in CANONICAL_SOURCES
        assert "guardian" in CANONICAL_SOURCES
        assert "ap" in CANONICAL_SOURCES
        assert "al-monitor" in CANONICAL_SOURCES
        assert "polymarket" in CANONICAL_SOURCES

    def test_agent_source_keys_covered(self):
        """Agent sources should be unique strings in the agent mapping."""
        for key in AGENT_SOURCES:
            assert isinstance(key, str)
            assert len(key) > 0

    def test_generic_sources_are_strings(self):
        """Generic sources should be a set of strings."""
        assert isinstance(GENERIC_SOURCES, set)
        for item in GENERIC_SOURCES:
            assert isinstance(item, str)

    def test_all_sources_are_distinct(self):
        """Canonical, agent, and generic source sets should not overlap."""
        for key in CANONICAL_SOURCES:
            assert key not in AGENT_SOURCES, f"Overlap: {key}"
            assert key not in GENERIC_SOURCES, f"Overlap: {key}"
