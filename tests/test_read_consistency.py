"""Read-consistency probe harness and canary tests (issue #903).

Production observed intermittent 404-on-existing-node ("get_node flicker")
on the live OHM instance on 2026-07-14: ``GET /node/<id>`` returned 404 on a
first call then 200 on the next three identical calls for a node that
exists, and one node (``assessment-ohm-mesh-investor-objections``) was
apparently lost entirely — 404 on every read and absent from search.

The live flicker cannot be reproduced against the in-memory / single-file
DuckDB test harness. ``OhmStore.get_node`` reads through the single write
connection (``self.conn``) under the write lock (``src/ohm/graph/store.py``),
and the HTTP handler ``_get_node`` calls ``self.current_store.get_node``
(``src/ohm/server/handlers/nodes.py``) on that same connection, so a
just-committed node is always visible to the next read — read-your-writes
is automatic within one process. The separate read-only connection
(``read_conn``) is only ever used by ``read_execute`` and the read-scope
helpers, not by ``get_node``; and when DuckLake is attached the read-only
connection cannot even be opened (``_ensure_read_conn`` falls back to the
write connection). The flicker is therefore consistent with a multi-replica
deployment where reads round-robin onto a stale/lagging replica that has
not yet received (or has lost) the write. See the follow-up issues filed
under #903 for the architectural fix, WAL checkpoint verification, and lost
node restoration — all of which need live operational access.

This module provides the deterministic probe and canary requested by the
issue's acceptance criteria so the defect moves from "observed live" to
"reliably reproducible" (here: reliably *passing* in the single-replica
test env, and tripping the moment a read path can miss a committed node):

* ``test_get_node_probe_hammer`` — the probe harness. Hammers
  ``GET /node/<id>`` in a tight sequential loop and records 404/200/500
  counts plus any ``correlation_id`` values returned on misses. Against
  the single-connection test server every read must return 200; any miss
  is a regression introduced by caching, replica routing, or async writes.

* ``test_known_node_ids_readback_canary`` — the canary. Seeds a set of
  known-good node IDs (an anchor concept plus derived-claim nodes that
  cross-link to it, mirroring the real arch-/assessment- prefixed nodes)
  and reads each back repeatedly, asserting that none ever 404. This is
  the regression guard: if anyone introduces a read path that can miss a
  committed node, this trips.

* ``test_write_then_immediate_read_consistency`` — pins the in-session
  read-your-writes invariant that the issue confirmed still holds.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.xdist_group("server")

from tests.conftest import _request


# Number of identical reads fired by the probe harness. High enough that an
# intermittent flicker (the live symptom was 1-in-4) would surface reliably,
# low enough to stay well under the fast-suite budget on a single-threaded
# test server (~0.5s for the full loop).
_PROBE_ITERATIONS = 200
_CANARY_READS_PER_NODE = 20
_RYW_ITERATIONS = 20


@pytest.mark.xdist_group("server")
class TestReadConsistencyProbe:
    """Probe harness + canary for the get_node read-consistency defect (#903).

    These tests run in the fast suite (no ``integration`` mark) because the
    probe must run on every change: it is the regression guard for the
    read-your-writes invariant. They are grouped with ``xdist_group("server")``
    because the ``test_server`` fixture mutates class-level ``OhmHandler``
    state, the same as the rest of the server test family.
    """

    def test_get_node_probe_hammer(self, test_server):
        """Hammer GET /node/<id> in a tight loop; assert zero 404s/500s.

        Records 404/200/500 counts and any ``correlation_id`` values on
        misses so a future regression that reintroduces the flicker leaves
        a forensic trail matching the live symptom (correlation IDs were
        the only handle for tracing the production incident).
        """
        port, _store = test_server
        node_id = "probe-target-903"
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": node_id, "label": "Probe Target", "type": "concept"},
        )
        assert status in (200, 201), f"seed write failed: {status} {data}"

        counts: dict[int, int] = {200: 0, 404: 0, 500: 0}
        miss_correlations: list[str] = []

        for _ in range(_PROBE_ITERATIONS):
            status, data = _request("GET", port, f"/node/{node_id}")
            counts[status] = counts.get(status, 0) + 1
            if status != 200:
                corr = data.get("correlation_id") if isinstance(data, dict) else None
                if corr:
                    miss_correlations.append(corr)

        # In the single-connection test harness every read of a committed
        # node MUST succeed. A 404 here means the read path can miss a node
        # that exists — the exact defect from #903. A 500 means the read
        # path raised (e.g. the `'int' object has no attribute 'get'`
        # sighting folded in from #904).
        assert counts[404] == 0, (
            f"get_node flicker reproduced in test env: {counts[404]} 404s "
            f"out of {_PROBE_ITERATIONS} reads; "
            f"status distribution={counts}; correlations={miss_correlations}"
        )
        assert counts[500] == 0, (
            f"server errors during probe: {counts[500]} 500s; "
            f"status distribution={counts}; correlations={miss_correlations}"
        )
        assert counts[200] == _PROBE_ITERATIONS, (
            f"unexpected read status distribution: {counts}"
        )

    def test_known_node_ids_readback_canary(self, test_server):
        """Canary: read a set of known-good node IDs repeatedly; zero 404s.

        Seeds an anchor concept plus derived-claim nodes (pattern / idea /
        decision) that cross-link to the anchor per ADR-018 — mirroring the
        real lost/flickering nodes which were derived claims (``arch-`` /
        ``assessment-`` prefixed), not bare concepts. A committed node must
        never 404 on read; this is the regression guard for any caching or
        replica-routing change that breaks read-your-writes semantics.
        """
        port, _store = test_server
        anchor = "canary-anchor-903"
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": anchor, "label": "Canary Anchor", "type": "concept"},
        )
        assert status in (200, 201), f"anchor seed write failed: {status} {data}"

        known_nodes = [
            ("canary-concept-903", "concept"),
            ("canary-pattern-903", "pattern"),
            ("canary-idea-903", "idea"),
            ("canary-decision-903", "decision"),
        ]
        for node_id, node_type in known_nodes:
            body: dict = {"id": node_id, "label": node_id, "type": node_type}
            if node_type != "concept":
                body["connects_to"] = [anchor]
            status, data = _request("POST", port, "/node", body=body)
            assert status in (200, 201), (
                f"seed write failed for {node_id} ({node_type}): {status} {data}"
            )

        all_ids = [anchor] + [nid for nid, _ in known_nodes]
        misses: list[tuple[str, int, object]] = []
        total = 0
        for node_id in all_ids:
            for _ in range(_CANARY_READS_PER_NODE):
                status, data = _request("GET", port, f"/node/{node_id}")
                total += 1
                if status != 200:
                    misses.append((node_id, status, data))

        assert not misses, (
            f"canary readback failures: {len(misses)}/{total} misses — "
            f"first 5: {misses[:5]}"
        )

    def test_write_then_immediate_read_consistency(self, test_server):
        """Pin the in-session read-your-writes invariant.

        The issue confirmed freshly created nodes are visible on the
        immediately-following read with a matching timestamp. This test
        pins that: write a node, read it back at once, assert 200 + matching
        id. Repeated to guard against any async-write / deferred-flush
        regression that would delay visibility of a just-committed node.
        """
        port, _store = test_server
        for i in range(_RYW_ITERATIONS):
            node_id = f"ryw-903-{i}"
            status, data = _request(
                "POST",
                port,
                "/node",
                body={"id": node_id, "label": f"RYW {i}", "type": "concept"},
            )
            assert status in (200, 201), f"write {i} failed: {status} {data}"
            status, data = _request("GET", port, f"/node/{node_id}")
            assert status == 200, f"immediate read {i} returned {status}: {data}"
            assert isinstance(data, dict) and data.get("id") == node_id, (
                f"immediate read {i} returned wrong body: {data}"
            )
