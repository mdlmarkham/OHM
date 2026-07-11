"""Tests for OHM-804/805/806/808: Generic domain tables (configuration-over-schema pattern)."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema, SCHEMA_VERSION


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestSchemaVersion:
    def test_version_bumped(self):
        assert SCHEMA_VERSION == "0.50.0"


class TestDomainAssumptions:
    """OHM-805: domain_assumptions table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM domain_assumptions").fetchone()
        assert result[0] == 0

    def test_insert(self, db):
        db.execute(
            "INSERT INTO domain_assumptions (node_id, domain, assumption_type, key, value, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            ["n1", "topo", "operational", "min_throughput", "100 tph", "agent"],
        )
        row = db.execute("SELECT * FROM domain_assumptions WHERE node_id = 'n1'").fetchone()
        assert row is not None

    def test_domain_filter(self, db):
        db.execute("INSERT INTO domain_assumptions (node_id, domain, assumption_type, key, created_by) VALUES ('n1', 'topo', 'op', 'k1', 'a')")
        db.execute("INSERT INTO domain_assumptions (node_id, domain, assumption_type, key, created_by) VALUES ('n2', 'trading', 'risk', 'k2', 'a')")
        topo = db.execute("SELECT COUNT(*) FROM domain_assumptions WHERE domain = 'topo'").fetchone()
        assert topo[0] == 1


class TestDomainExpectations:
    """OHM-805: domain_expectations table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM domain_expectations").fetchone()
        assert result[0] == 0

    def test_insert_with_status(self, db):
        db.execute(
            "INSERT INTO domain_expectations (node_id, domain, expectation_type, key, expected_value, status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["n1", "topo", "performance", "availability", "99.5%", "pending", "agent"],
        )
        row = db.execute("SELECT status FROM domain_expectations WHERE node_id = 'n1'").fetchone()
        assert row[0] == "pending"


class TestStalenessLog:
    """OHM-806: staleness_log table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM staleness_log").fetchone()
        assert result[0] == 0

    def test_insert(self, db):
        db.execute(
            "INSERT INTO staleness_log (node_id, domain, trigger_type, staleness_score) VALUES (?, ?, ?, ?)",
            ["n1", "topo", "no_observation", 0.8],
        )
        row = db.execute("SELECT staleness_score FROM staleness_log WHERE node_id = 'n1'").fetchone()
        assert abs(row[0] - 0.8) < 0.01

    def test_trigger_type_filter(self, db):
        db.execute("INSERT INTO staleness_log (node_id, domain, trigger_type) VALUES ('n1', 'topo', 'no_observation')")
        db.execute("INSERT INTO staleness_log (node_id, domain, trigger_type) VALUES ('n2', 'topo', 'stale_evidence')")
        stale = db.execute("SELECT COUNT(*) FROM staleness_log WHERE trigger_type = 'no_observation'").fetchone()
        assert stale[0] == 1


class TestMetricVersions:
    """OHM-808: metric_versions table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM metric_versions").fetchone()
        assert result[0] == 0

    def test_insert(self, db):
        db.execute(
            "INSERT INTO metric_versions (node_id, domain, version_number, formula_ref, formula_description, scope, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["n1", "topo", 2, "throughput_v2.py", "Revised throughput calculation", "plant=RCC", "agent"],
        )
        row = db.execute("SELECT version_number, formula_ref FROM metric_versions WHERE node_id = 'n1'").fetchone()
        assert row[0] == 2
        assert row[1] == "throughput_v2.py"

    def test_domain_filter(self, db):
        db.execute("INSERT INTO metric_versions (node_id, domain, version_number, created_by) VALUES ('n1', 'topo', 1, 'a')")
        db.execute("INSERT INTO metric_versions (node_id, domain, version_number, created_by) VALUES ('n2', 'trading', 1, 'a')")
        topo = db.execute("SELECT COUNT(*) FROM metric_versions WHERE domain = 'topo' AND deleted_at IS NULL").fetchone()
        assert topo[0] == 1


class TestDomainSimulationRuns:
    """OHM-804: domain_simulation_runs table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM domain_simulation_runs").fetchone()
        assert result[0] == 0

    def test_insert(self, db):
        db.execute(
            "INSERT INTO domain_simulation_runs (domain, simulation_type, status, started_by) VALUES (?, ?, ?, ?)",
            ["topo", "monte_carlo_cascade", "running", "agent"],
        )
        row = db.execute("SELECT status FROM domain_simulation_runs WHERE domain = 'topo'").fetchone()
        assert row[0] == "running"


class TestDomainSimulationResults:
    """OHM-804: domain_simulation_results table."""

    def test_table_exists(self, db):
        result = db.execute("SELECT COUNT(*) FROM domain_simulation_results").fetchone()
        assert result[0] == 0

    def test_insert(self, db):
        db.execute(
            "INSERT INTO domain_simulation_results (run_id, node_id, result_type, value) VALUES (?, ?, ?, ?)",
            ["run1", "n1", "p_failure", 0.15],
        )
        row = db.execute("SELECT value FROM domain_simulation_results WHERE run_id = 'run1'").fetchone()
        assert abs(row[0] - 0.15) < 0.01

    def test_join_with_runs(self, db):
        db.execute("INSERT INTO domain_simulation_runs (id, domain, simulation_type, status, started_by) VALUES ('run1', 'topo', 'mc', 'completed', 'a')")
        db.execute("INSERT INTO domain_simulation_results (run_id, node_id, value) VALUES ('run1', 'n1', 0.15)")
        result = db.execute("SELECT r.domain, res.value FROM domain_simulation_runs r JOIN domain_simulation_results res ON res.run_id = r.id WHERE r.domain = 'topo'").fetchone()
        assert result is not None
        assert result[0] == "topo"
        assert abs(result[1] - 0.15) < 0.01


class TestCrossDomainIsolation:
    """All generic tables support cross-domain usage without collision (OHM-811)."""

    def test_assumptions_two_domains(self, db):
        db.execute("INSERT INTO domain_assumptions (node_id, domain, assumption_type, key, created_by) VALUES ('n1', 'topo', 'op', 'k', 'a')")
        db.execute("INSERT INTO domain_assumptions (node_id, domain, assumption_type, key, created_by) VALUES ('n1', 'trading', 'risk', 'k', 'a')")
        topo = db.execute("SELECT COUNT(*) FROM domain_assumptions WHERE domain = 'topo'").fetchone()[0]
        trading = db.execute("SELECT COUNT(*) FROM domain_assumptions WHERE domain = 'trading'").fetchone()[0]
        assert topo == 1
        assert trading == 1

    def test_metric_versions_two_domains(self, db):
        db.execute("INSERT INTO metric_versions (node_id, domain, version_number, created_by) VALUES ('n1', 'topo', 1, 'a')")
        db.execute("INSERT INTO metric_versions (node_id, domain, version_number, created_by) VALUES ('n1', 'devsecops', 1, 'a')")
        topo = db.execute("SELECT COUNT(*) FROM metric_versions WHERE domain = 'topo' AND deleted_at IS NULL").fetchone()[0]
        devsecops = db.execute("SELECT COUNT(*) FROM metric_versions WHERE domain = 'devsecops' AND deleted_at IS NULL").fetchone()[0]
        assert topo == 1
        assert devsecops == 1
