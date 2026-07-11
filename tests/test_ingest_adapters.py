"""Tests for OHM-803: Plugin-based ingest adapter system."""

from __future__ import annotations

import json
import tempfile
import pathlib
import pytest

from ohm.framework.ingest import (
    IngestAdapter,
    IngestRecord,
    IngestResult,
    run_ingest,
    register_adapter,
    get_adapter,
    list_adapters,
    TagBatchAdapter,
)


class TestAdapterRegistry:
    def test_tags_adapter_registered(self):
        assert "tags" in list_adapters()

    def test_get_adapter_returns_class(self):
        cls = get_adapter("tags")
        assert cls is TagBatchAdapter

    def test_get_unknown_adapter_returns_none(self):
        assert get_adapter("nonexistent") is None

    def test_register_custom_adapter(self):
        class CustomAdapter:
            def source_id(self):
                return "custom"

            def read_batch(self):
                return []

        register_adapter("custom_test", CustomAdapter)
        assert "custom_test" in list_adapters()
        assert get_adapter("custom_test") is CustomAdapter


class TestTagBatchAdapter:
    def test_source_id(self):
        adapter = TagBatchAdapter(file_path="/dev/null", domain="topo")
        assert adapter.source_id() == "tag-batch-topo"

    def test_read_batch_yields_nodes(self, tmp_path):
        tags = [
            {"node_id": "tag_1", "label": "Temperature Sensor", "source_type": "opc_ua"},
            {"node_id": "tag_2", "label": "Pressure Gauge", "source_type": "opc_ua"},
        ]
        batch_file = tmp_path / "tags.json"
        batch_file.write_text(json.dumps(tags))

        adapter = TagBatchAdapter(file_path=str(batch_file), domain="topo")
        records = list(adapter.read_batch())

        assert len(records) == 2
        assert all(r.kind == "node" for r in records)
        assert records[0].id == "tag_1"
        assert records[0].label == "Temperature Sensor"

    def test_read_batch_skips_missing_ids(self, tmp_path):
        tags = [
            {"node_id": "tag_1", "label": "Valid"},
            {"label": "No ID"},  # should be skipped
        ]
        batch_file = tmp_path / "tags.json"
        batch_file.write_text(json.dumps(tags))

        adapter = TagBatchAdapter(file_path=str(batch_file))
        records = list(adapter.read_batch())
        assert len(records) == 1

    def test_read_batch_rejects_non_array(self, tmp_path):
        batch_file = tmp_path / "tags.json"
        batch_file.write_text('{"not": "an array"}')

        adapter = TagBatchAdapter(file_path=str(batch_file))
        with pytest.raises(ValueError, match="JSON array"):
            list(adapter.read_batch())


class TestRunIngest:
    def test_creates_nodes(self):
        class TestAdapter:
            def source_id(self):
                return "test-v1"

            def read_batch(self):
                yield IngestRecord(kind="node", id="n1", label="Node 1")
                yield IngestRecord(kind="node", id="n2", label="Node 2")

        class FakeClient:
            def __init__(self):
                self.nodes = []

            def create_node(self, label, **kwargs):
                self.nodes.append({"label": label, **kwargs})
                return {"created": True}

            def create_edge(self, from_node, to_node, **kwargs):
                return {"created": True}

        client = FakeClient()
        result = run_ingest(TestAdapter(), client)
        assert result.created == 2
        assert len(client.nodes) == 2

    def test_dry_run_doesnt_write(self):
        class TestAdapter:
            def source_id(self):
                return "test-v1"

            def read_batch(self):
                yield IngestRecord(kind="node", id="n1", label="Node 1")

        class FakeClient:
            def __init__(self):
                self.nodes = []

            def create_node(self, label, **kwargs):
                self.nodes.append({"label": label, **kwargs})
                return {"created": True}

            def create_edge(self, from_node, to_node, **kwargs):
                return {"created": True}

        client = FakeClient()
        result = run_ingest(TestAdapter(), client, dry_run=True)
        # dry_run counts records but doesn't write
        assert result.created == 1  # counted but not written
        assert len(client.nodes) == 0  # no actual writes

    def test_skips_missing_label(self):
        class TestAdapter:
            def source_id(self):
                return "test-v1"

            def read_batch(self):
                yield IngestRecord(kind="node", id="n1")  # no label
                yield IngestRecord(kind="node", id="n2", label="Has Label")

        class FakeClient:
            def __init__(self):
                self.nodes = []

            def create_node(self, label, **kwargs):
                self.nodes.append({"label": label, **kwargs})
                return {"created": True}

            def create_edge(self, from_node, to_node, **kwargs):
                return {"created": True}

        client = FakeClient()
        result = run_ingest(TestAdapter(), client)
        assert result.skipped == 1
        assert result.created == 1
