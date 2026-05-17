"""
OHM + Marimo Integration — query OHM graphs in reactive notebooks.

Usage:
    import marimo as mo
    from ohm.marimo_pair import OHMPair

    # In a marimo cell:
    ohm = OHMPair("/path/to/graph.duckdb", actor="analyst")
    ohm.stats()          # → mo.ui.table
    ohm.anomalies()      # → mo.ui.table
    ohm.search("Hormuz") # → mo.ui.table
    ohm.graph("node-id", depth=2)  # → mermaid diagram
"""

from __future__ import annotations

from typing import Any

from ohm.sdk import connect


class OHMPair:
    """Marimo-integrated OHM graph explorer.

    Wraps the OHM SDK and returns marimo-compatible objects (tables, diagrams).
    Requires marimo to be installed: pip install marimo
    """

    def __init__(self, db_path: str = ":memory:", *, actor: str = "analyst"):
        self.db_path = db_path
        self.actor = actor
        self._graph = connect(db_path, actor=actor)

    def _table(self, data: list[dict]) -> Any:
        """Convert list of dicts to a marimo table if marimo is available."""
        try:
            import marimo as mo
            return mo.ui.table(data)
        except ImportError:
            return data

    def _md(self, text: str) -> Any:
        """Convert text to marimo markdown if available."""
        try:
            import marimo as mo
            return mo.md(text)
        except ImportError:
            return text

    def stats(self) -> Any:
        """Graph statistics as a table."""
        return self._graph.stats()

    def search(self, query: str, *, node_type: str | None = None) -> Any:
        """Search nodes by label/content. Returns marimo table."""
        results = self._graph.search(query, node_type=node_type)
        return self._table(results)

    def anomalies(self, *, sigma: float = 2.0) -> Any:
        """Anomalous observations. Returns marimo table."""
        results = self._graph.anomalies(sigma_threshold=sigma)
        return self._table(results)

    def contradictions(self, *, confidence: float = 0.5) -> Any:
        """Contradictory observations. Returns marimo dict."""
        return self._graph.contradictions(confidence_threshold=confidence)

    def health(self) -> Any:
        """Graph health report."""
        return self._graph.health()

    def agent_health(self) -> Any:
        """Agent health as table."""
        results = self._graph.agent_health()
        return self._table(results)

    def neighborhood(self, node_id: str, *, depth: int = 2) -> Any:
        """Neighborhood query. Returns table."""
        result = self._graph.neighborhood(node_id, depth=depth)
        return self._table(result)

    def provenance(self, node_id: str, *, depth: int = 10) -> Any:
        """Provenance chain. Returns table."""
        result = self._graph.provenance(node_id, max_depth=depth)
        return self._table(result)

    def graph(self, node_id: str, *, depth: int = 2) -> Any:
        """Mermaid diagram of a node's neighborhood.

        Returns a marimo mermaid diagram if marimo is available,
        otherwise returns the raw mermaid string.
        """
        nbr = self._graph.neighborhood(node_id, depth=depth)
        if not nbr:
            return self._md(f"No neighborhood found for {node_id}")

        # Build mermaid graph
        lines = ["graph LR"]
        seen_nodes = set()
        seen_edges = set()

        for entry in nbr:
            if entry.get("from_node"):
                fn = entry["from_node"][:8]
                tn = entry["to_node"][:8]
                et = entry.get("edge_type", "?")
                fl = entry.get("from_label", fn)[:20]
                tl = entry.get("to_label", tn)[:20]

                if fn not in seen_nodes:
                    lines.append(f"    {fn}[\"{fl}\"]")
                    seen_nodes.add(fn)
                if tn not in seen_nodes:
                    lines.append(f"    {tn}[\"{tl}\"]")
                    seen_nodes.add(tn)

                edge_key = f"{fn}-{tn}"
                if edge_key not in seen_edges:
                    lines.append(f"    {fn} -->|{et}| {tn}")
                    seen_edges.add(edge_key)

        mermaid_str = "\n".join(lines)

        try:
            import marimo as mo
            return mo.mermaid(mermaid_str)
        except ImportError:
            return mermaid_str

    def stale(self, *, threshold: float = 0.1) -> Any:
        """Stale edges. Returns table."""
        result = self._graph.stale_edges(stale_threshold=threshold)
        return self._table(result)

    def agents(self) -> Any:
        """List registered agents."""
        result = self._graph.agent_state()
        return self._table(result)

    def listen(self, **kwargs) -> Any:
        """Change feed. Returns table."""
        result = self._graph.listen(**kwargs)
        return self._table(result)

    def close(self) -> None:
        self._graph.close()

    def __enter__(self) -> OHMPair:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
