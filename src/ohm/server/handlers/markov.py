"""Markov absorbing-state analysis endpoint handlers."""

from __future__ import annotations


from ohm.server.handlers._base import OhmHandlerBase

class MarkovHandlerMixin(OhmHandlerBase):
    """Markov endpoint handlers for OhmHandler.

    Provides GET /markov/absorbing and GET /markov/expected_steps.
    Requires numpy via the ohm.markov module; raises ConfigurationError (→ 501)
    when numpy is absent.
    """

    def _get_markov_absorbing(self, path: str, qs: dict) -> None:
        """GET /markov/absorbing — Markov absorbing-state risk."""
        from ohm.exceptions import ConfigurationError, ValidationError

        start_node = qs.get("start", [None])[0]
        if not start_node:
            raise ValidationError("?start=<node_id> is required")
        edge_types_str = qs.get("edge_types", [""])[0]
        markov_edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] or None
        try:
            from ohm.markov import markov_absorbing_risk
        except ImportError as exc:
            raise ConfigurationError(f"Markov analysis requires numpy: {exc}") from exc

        result = markov_absorbing_risk(
            self.current_store.conn,
            start_node,
            edge_types=markov_edge_types,
        )
        self._json_response(200, result)

    def _get_markov_expected_steps(self, path: str, qs: dict) -> None:
        """GET /markov/expected_steps — Markov expected steps to absorption."""
        from ohm.exceptions import ConfigurationError, ValidationError

        start_node = qs.get("start", [None])[0]
        if not start_node:
            raise ValidationError("?start=<node_id> is required")
        target_state = qs.get("target", [None])[0]
        edge_types_str = qs.get("edge_types", [""])[0]
        markov_edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] or None
        try:
            from ohm.markov import markov_expected_steps
        except ImportError as exc:
            raise ConfigurationError(f"Markov analysis requires numpy: {exc}") from exc

        result = markov_expected_steps(
            self.current_store.conn,
            start_node,
            target_state=target_state,
            edge_types=markov_edge_types,
        )
        self._json_response(200, result)
