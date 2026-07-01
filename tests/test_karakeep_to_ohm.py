"""Tests for Karakeep → OHM document bridge."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingestion.karakeep_to_ohm import (
    _extract_tags,
    _extract_url,
    _is_document_url,
    run_bridge,
)


class TestHelpers:
    """Tests for small helper functions."""

    def test_is_document_url_detects_extensions(self):
        assert _is_document_url("https://example.com/paper.pdf")
        assert _is_document_url("https://example.com/page.html")
        assert _is_document_url("https://example.com/page.htm")
        assert _is_document_url("https://example.com/readme.md")
        assert _is_document_url("https://example.com/notes.txt")
        assert not _is_document_url("https://example.com/article")
        assert not _is_document_url("https://example.com/article?format=pdf")

    def test_extract_url_prefers_content_url(self):
        bookmark = {
            "id": "bk-1",
            "title": "Test",
            "url": "https://fallback.example",
            "content": {"url": "https://primary.example/doc.pdf"},
        }
        assert _extract_url(bookmark) == "https://primary.example/doc.pdf"

    def test_extract_url_falls_back_to_top_level(self):
        bookmark = {"id": "bk-2", "title": "Test", "url": "https://fallback.example"}
        assert _extract_url(bookmark) == "https://fallback.example"

    def test_extract_tags(self):
        bookmark = {
            "id": "bk-3",
            "title": "Test",
            "tags": [{"name": "AI Agents"}, "security"],
            "theme": "AI/Security",
        }
        tags = _extract_tags(bookmark)
        assert "karakeep" in tags
        assert "ai_agents" in tags
        assert "security" in tags
        assert "ai_security" in tags


class TestRunBridge:
    """Tests for the main bridge run function."""

    def test_bridge_queues_pdf_bookmark(self, tmp_path):
        karakeep_queue = tmp_path / "bookmarks.json"
        ohm_queue_dir = tmp_path / "ohm_queue"
        state_file = tmp_path / "state.json"

        karakeep_queue.write_text(
            json.dumps(
                [
                    {
                        "id": "bk-pdf-1",
                        "received": "2026-06-21T20:00:00Z",
                        "bookmark": {
                            "id": "bk-pdf-1",
                            "title": "Important PDF",
                            "url": "https://example.com/report.pdf",
                            "tags": ["economics"],
                            "publisher": "Example",
                        },
                    }
                ]
            )
        )

        queued = run_bridge(
            karakeep_queue=karakeep_queue,
            ohm_queue_dir=ohm_queue_dir,
            state_file=state_file,
        )

        assert queued == 1

        items = [json.loads(p.read_text()) for p in (ohm_queue_dir / "triage_pass").glob("*.json")]
        assert len(items) == 1
        assert items[0]["kind"] == "document"
        assert items[0]["url"] == "https://example.com/report.pdf"
        assert items[0]["filename"] == "report.pdf"
        assert "economics" in items[0]["tags"]

        state = json.loads(state_file.read_text())
        assert "bk-pdf-1" in state["processed_ids"]

    def test_bridge_skips_non_document_urls(self, tmp_path):
        karakeep_queue = tmp_path / "bookmarks.json"
        ohm_queue_dir = tmp_path / "ohm_queue"
        state_file = tmp_path / "state.json"

        karakeep_queue.write_text(
            json.dumps(
                [
                    {
                        "id": "bk-article-1",
                        "bookmark": {
                            "id": "bk-article-1",
                            "title": "News Article",
                            "url": "https://example.com/article",
                        },
                    }
                ]
            )
        )

        queued = run_bridge(
            karakeep_queue=karakeep_queue,
            ohm_queue_dir=ohm_queue_dir,
            state_file=state_file,
        )

        assert queued == 0
        assert not (ohm_queue_dir / "triage_pass").exists() or not list((ohm_queue_dir / "triage_pass").glob("*.json"))

        state = json.loads(state_file.read_text())
        assert "bk-article-1" in state["processed_ids"]

    def test_bridge_does_not_requeue_processed_items(self, tmp_path):
        karakeep_queue = tmp_path / "bookmarks.json"
        ohm_queue_dir = tmp_path / "ohm_queue"
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"processed_ids": ["bk-pdf-2"]}))

        karakeep_queue.write_text(
            json.dumps(
                [
                    {
                        "id": "bk-pdf-2",
                        "bookmark": {
                            "id": "bk-pdf-2",
                            "title": "Already Done",
                            "url": "https://example.com/old.pdf",
                        },
                    }
                ]
            )
        )

        queued = run_bridge(
            karakeep_queue=karakeep_queue,
            ohm_queue_dir=ohm_queue_dir,
            state_file=state_file,
        )

        assert queued == 0
        assert not (ohm_queue_dir / "triage_pass").exists() or not list((ohm_queue_dir / "triage_pass").glob("*.json"))

    def test_bridge_handles_top_level_bookmarks_list(self, tmp_path):
        karakeep_queue = tmp_path / "bookmarks.json"
        ohm_queue_dir = tmp_path / "ohm_queue"
        state_file = tmp_path / "state.json"

        karakeep_queue.write_text(
            json.dumps(
                {
                    "bookmarks": [
                        {
                            "id": "bk-pdf-3",
                            "title": "Export PDF",
                            "url": "https://example.com/export.pdf",
                        }
                    ],
                    "lastSync": "2026-06-21T20:00:00Z",
                }
            )
        )

        queued = run_bridge(
            karakeep_queue=karakeep_queue,
            ohm_queue_dir=ohm_queue_dir,
            state_file=state_file,
        )

        assert queued == 1
        items = [json.loads(p.read_text()) for p in (ohm_queue_dir / "triage_pass").glob("*.json")]
        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/export.pdf"
