"""Tests for GitHub #974: graceful 501 when numpy is unavailable.

ohm_markov, ohm_game/ohm_nash, and ohm_discover should return HTTP 501
(ConfigurationError → not_implemented) with a structured body mentioning
numpy, not an unhandled 500.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


@pytest.mark.xdist_group("server")
class TestNumpyUnavailable501:
    """All numpy-dependent endpoints return 501 when numpy is 'missing'."""

    @pytest.fixture(autouse=True)
    def _patch_numpy(self):
        with (
            patch("ohm.inference.game_theory.NUMPY_AVAILABLE", False),
            patch("ohm.inference.discovery.NUMPY_AVAILABLE", False),
            patch("ohm.inference.markov.NUMPY_AVAILABLE", False),
        ):
            yield

    def test_game_returns_501(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "g976", "label": "Game", "type": "concept"})
        status, data = _request("GET", port, "/game?target=g976")
        assert status == 501
        assert data["error"] == "not_implemented"
        assert "numpy" in data["message"].lower()
        assert data["status"] == 501

    def test_nash_returns_501(self, test_server):
        port, _ = test_server
        payoffs = json.dumps([[[1, 0], [0, 1]], [[0, 1], [1, 0]]])
        status, data = _request("GET", port, f"/nash?players=a,b&payoffs={payoffs}")
        assert status == 501
        assert data["error"] == "not_implemented"
        assert "numpy" in data["message"].lower()

    def test_discover_returns_501(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "d976", "label": "Discover", "type": "concept"})
        status, data = _request("GET", port, "/discover?method=pc")
        assert status == 501
        assert data["error"] == "not_implemented"
        assert "numpy" in data["message"].lower()

    def test_markov_absorbing_returns_501(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "m976", "label": "Markov", "type": "concept"})
        status, data = _request("GET", port, "/markov/absorbing?start=m976")
        assert status == 501
        assert data["error"] == "not_implemented"
        assert "numpy" in data["message"].lower()

    def test_markov_expected_steps_returns_501(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "me976", "label": "Markov E", "type": "concept"})
        status, data = _request("GET", port, "/markov/expected_steps?start=me976")
        assert status == 501
        assert data["error"] == "not_implemented"
        assert "numpy" in data["message"].lower()


@pytest.mark.xdist_group("server")
class TestNumpyAvailableHappyPath:
    """With numpy available (real default), endpoints don't 500/501 due to numpy."""

    def test_game_works_with_numpy(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "ok976", "label": "OK", "type": "concept"})
        status, data = _request("GET", port, "/game?target=ok976")
        assert status != 501
        assert status != 500

    def test_markov_absorbing_works_with_numpy(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "mk976", "label": "MK", "type": "concept"})
        status, _ = _request("GET", port, "/markov/absorbing?start=mk976")
        assert status != 501
        assert status != 500
