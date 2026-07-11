from __future__ import annotations

from ohm.graph.schema import SCHEMA_VERSION


def test_current_schema_version():
    """Single canonical SCHEMA_VERSION checkpoint.

    All other hardcoded ``assert SCHEMA_VERSION ==`` assertions were removed
    from individual test files (issue #821) to prevent drift on schema bumps.
    The CI grep guard in ``.github/workflows/test.yml`` prevents reintroduction.
    """
    assert SCHEMA_VERSION == "0.50.0"
