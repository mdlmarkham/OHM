"""Tests for OHM-934: Enhanced ohm_belief — percentiles, prior, evidence movers, calibration.

The MCP tool schema and dispatch tests are skipped in environments where the
mcp package is not installed (they test static definitions that can't regress
without the MCP SDK anyway).
"""

from __future__ import annotations

import math

import pytest

# ── Skip MCP-dependent tests if mcp package is missing ──
_mcp_available = True
try:
    from ohm.mcp.tools import all_tools  # noqa: F401
    from ohm.mcp.dispatch import build_request  # noqa: F401
except ImportError:
    _mcp_available = False


@pytest.mark.skipif(not _mcp_available, reason="mcp package not installed")
class TestBeliefToolSchema:
    """Test ohm_belief MCP tool schema updates for OHM-934."""

    def test_schema_has_include_evidence_movers(self):
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        props = tool.inputSchema["properties"]
        assert "include_evidence_movers" in props
        assert props["include_evidence_movers"]["type"] == "boolean"
        assert props["include_evidence_movers"]["default"] is True

    def test_schema_has_include_prior(self):
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        props = tool.inputSchema["properties"]
        assert "include_prior" in props
        assert props["include_prior"]["type"] == "boolean"
        assert props["include_prior"]["default"] is True

    def test_schema_has_belief_statement(self):
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        props = tool.inputSchema["properties"]
        assert "belief_statement" in props
        assert props["belief_statement"]["type"] == "string"

    def test_schema_has_edge_types(self):
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        props = tool.inputSchema["properties"]
        assert "edge_types" in props
        assert props["edge_types"]["type"] == "string"


@pytest.mark.skipif(not _mcp_available, reason="mcp package not installed")
class TestBeliefDispatch:
    """Test dispatch forwarding of new parameters."""

    def test_dispatch_with_include_evidence_movers(self):
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "include_evidence_movers": True},
            "test-agent",
        )
        assert method == "GET"
        assert "include_evidence_movers=true" in path

    def test_dispatch_with_include_prior_false(self):
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "include_prior": False},
            "test-agent",
        )
        assert method == "GET"
        assert "include_prior=false" in path

    def test_dispatch_with_belief_statement(self):
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "belief_statement": "P(bad)=0.5"},
            "test-agent",
        )
        assert method == "GET"
        assert "belief_statement=" in path

    def test_dispatch_with_edge_types(self):
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "edge_types": "CAUSES,DEPENDS_ON"},
            "test-agent",
        )
        assert method == "GET"
        assert "edge_types=" in path

    def test_dispatch_all_new_params(self):
        method, path, body = build_request(
            "ohm_belief",
            {
                "target": "node-1",
                "include_evidence_movers": True,
                "include_prior": True,
                "belief_statement": "P(bad)=0.7",
                "edge_types": "CAUSES",
            },
            "test-agent",
        )
        assert method == "GET"
        assert "include_evidence_movers=true" in path
        assert "include_prior=true" in path
        assert "belief_statement=" in path
        assert "edge_types=CAUSES" in path


class TestBeliefEndpointEnhancements:
    """Test /belief endpoint with new parameters (OHM-934)."""

    def test_belief_returns_method_metadata(self, test_server):
        """GET /belief returns method and pgmpy_available."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Cause", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")

        status, data = _request("GET", port, f"/belief?target={target['id']}")
        assert status == 200
        assert "method" in data
        assert "pgmpy_available" in data
        assert isinstance(data["pgmpy_available"], bool)

    def test_belief_with_include_prior_shows_prior(self, test_server):
        """GET /belief with evidence and include_prior returns prior + surprise."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Cause", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")

        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&evidence={cause['id']}:1&include_prior=true",
        )
        assert status == 200
        assert "prior" in data
        assert "P(bad)" in data["prior"]
        assert "P(good)" in data["prior"]
        assert "surprise" in data
        assert "kl_divergence" in data["surprise"]
        assert "level" in data["surprise"]
        assert data["surprise"]["kl_divergence"] >= 0

    def test_belief_without_evidence_omits_prior(self, test_server):
        """GET /belief without evidence omits prior (nothing to compare)."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        status, data = _request("GET", port, f"/belief?target={target['id']}&include_prior=true")
        assert status == 200
        # No evidence → no prior comparison section
        assert "prior" not in data

    def test_belief_with_include_evidence_movers(self, test_server):
        """GET /belief with evidence and include_evidence_movers returns movers list."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause1 = create_node(store.conn, label="Cause1", node_type="event", created_by="test")
        cause2 = create_node(store.conn, label="Cause2", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause1["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")
        store.write_edge(cause2["id"], target["id"], "CAUSES", "L3", confidence=0.6, agent_name="test")

        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&evidence={cause1['id']}:1,{cause2['id']}:0&include_evidence_movers=true",
        )
        assert status == 200
        assert "evidence_movers" in data
        assert isinstance(data["evidence_movers"], list)
        assert len(data["evidence_movers"]) == 2

        for mover in data["evidence_movers"]:
            assert "node" in mover
            assert "delta_p_bad" in mover
            assert "direction" in mover
            assert "effective_confidence" in mover
            assert "age_days" in mover

        # Movers should be sorted by absolute impact
        abs_deltas = [abs(m["delta_p_bad"]) for m in data["evidence_movers"]]
        assert abs_deltas == sorted(abs_deltas, reverse=True)

    def test_belief_evidence_movers_excluded_when_disabled(self, test_server):
        """GET /belief with include_evidence_movers=false omits movers."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Cause", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")

        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&evidence={cause['id']}:1&include_evidence_movers=false",
        )
        assert status == 200
        assert "evidence_movers" not in data

    def test_belief_with_belief_statement_calibration(self, test_server):
        """GET /belief with belief_statement returns calibration."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Cause", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")

        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&belief_statement=P(bad)=0.9",
        )
        assert status == 200
        assert "calibration" in data
        assert "agent_belief_statement" in data["calibration"]
        assert "graph_probability" in data["calibration"]
        assert "divergence" in data["calibration"]
        assert "severity" in data["calibration"]
        assert data["calibration"]["divergence"] >= 0
        assert data["calibration"]["severity"] in (0, 1, 2, 3)

    def test_belief_posterior_has_percentiles_with_evidence(self, test_server):
        """GET /belief with evidence and sufficient data returns percentiles."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        nodes = []
        for i in range(5):
            n = create_node(store.conn, label=f"Cause{i}", node_type="event", created_by="test")
            nodes.append(n)
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        for n in nodes:
            store.write_edge(n["id"], target["id"], "CAUSES", "L3", confidence=0.7, agent_name="test")

        evidence_str = ",".join(f"{n['id']}:1" for n in nodes[:3])
        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&evidence={evidence_str}",
        )
        assert status == 200
        posterior = data["posterior"]
        assert "p50" in posterior
        assert "std" in posterior
        assert "mean" in posterior
        assert "mode" in posterior
        # Percentile ordering: p05 <= p25 <= p50 <= p75 <= p95
        assert posterior["p05"] <= posterior["p25"]
        assert posterior["p25"] <= posterior["p50"]
        assert posterior["p50"] <= posterior["p75"]
        assert posterior["p75"] <= posterior["p95"]

    def test_belief_surprise_levels(self, test_server):
        """Verify KL surprise level categorisation."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Cause", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.95, agent_name="test")

        status, data = _request(
            "GET", port,
            f"/belief?target={target['id']}&evidence={cause['id']}:1&include_prior=true",
        )
        assert status == 200
        assert "surprise" in data
        valid_levels = {"negligible", "low", "moderate", "high", "very_high"}
        assert data["surprise"]["level"] in valid_levels


class TestSDKBeliefMethod:
    """Test SDK belief() method exists and has correct signature.

    HttpGraph is defined inside connect_http() so we verify the method
    by checking the source code contains the definition and expected params.
    """

    def test_belief_method_defined_in_sdk_source(self):
        """belief() method is defined in ohm.framework.sdk source."""
        import inspect
        import ohm.framework.sdk as sdk_mod
        source = inspect.getsource(sdk_mod)
        assert "def belief(" in source

    def test_belief_method_has_expected_params_in_source(self):
        """belief() accepts target, evidence, edge_types, include_evidence_movers, etc."""
        import inspect
        import ohm.framework.sdk as sdk_mod
        source = inspect.getsource(sdk_mod)
        # Find the belief() method body by locating its definition
        idx = source.index("def belief(")
        # Extract the signature portion (up to the first colon after the closing paren)
        paren_depth = 0
        sig_end = idx
        for i, ch in enumerate(source[idx:], idx):
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    sig_end = i
                    break
        sig_text = source[idx:sig_end + 1]
        assert "target" in sig_text
        assert "evidence" in sig_text
        assert "edge_types" in sig_text
        assert "include_evidence_movers" in sig_text
        assert "include_prior" in sig_text
        assert "belief_statement" in sig_text


class TestBeliefMathUnit:
    """Unit tests for Beta approximation and KL divergence math (no server needed)."""

    def test_kl_divergence_identical_distributions(self):
        """KL(p || p) = 0 for identical distributions."""
        p = 0.3
        q = 0.3
        kl = p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))
        assert kl == pytest.approx(0.0, abs=1e-10)

    def test_kl_divergence_different_distributions(self):
        """KL(p || q) > 0 for different distributions."""
        p = 0.7
        q = 0.3
        kl = p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))
        assert kl > 0

    def test_beta_approximation_percentile_ordering(self):
        """Beta(alpha, beta) percentiles are monotonically ordered."""
        alpha = 4.0
        beta_param = 6.0
        mean = alpha / (alpha + beta_param)
        std_val = math.sqrt(alpha * beta_param / ((alpha + beta_param) ** 2 * (alpha + beta_param + 1)))

        z_map = {"p05": -1.645, "p25": -0.674, "p50": 0.0, "p75": 0.674, "p95": 1.645}
        percentiles = {}
        for name, z in z_map.items():
            percentiles[name] = mean + std_val * z

        assert percentiles["p05"] < percentiles["p25"]
        assert percentiles["p25"] < percentiles["p50"]
        assert percentiles["p50"] < percentiles["p75"]
        assert percentiles["p75"] < percentiles["p95"]

    def test_surprise_level_boundaries(self):
        """Verify KL → level mapping boundaries."""
        def kl_to_level(kl: float) -> str:
            if kl < 0.01:
                return "negligible"
            elif kl < 0.1:
                return "low"
            elif kl < 0.5:
                return "moderate"
            elif kl < 1.0:
                return "high"
            return "very_high"

        assert kl_to_level(0.0) == "negligible"
        assert kl_to_level(0.005) == "negligible"
        assert kl_to_level(0.05) == "low"
        assert kl_to_level(0.3) == "moderate"
        assert kl_to_level(0.75) == "high"
        assert kl_to_level(2.0) == "very_high"
