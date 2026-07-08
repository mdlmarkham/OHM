"""Benchmark TOON vs JSON token efficiency for OHM MCP tool results.

Run from repo root:
    python3 scripts/spikes/ohm_toon_benchmark_spike.py

Requirements: python-toon, tiktoken (optional). Falls back to byte-length if either
is missing.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from ohm.mcp.encoding import encode_payload

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
    _HAS_TIKTOKEN = True
except Exception:  # pragma: no cover
    _ENC = None
    _HAS_TIKTOKEN = False


def token_count(text: str) -> int:
    if _HAS_TIKTOKEN and _ENC is not None:
        return len(_ENC.encode(text))
    # Crude fallback
    return len(text.split())


# ── Realistic OHM result payloads ───────────────────────────────────────────


def payload_stats() -> dict:
    return {
        "nodes": 3721,
        "edges": 5433,
        "observations": 1963,
        "agents": 11,
        "challenge_ratio": 0.0396,
        "health_score": 0.7155,
        "edge_types_by_layer": {
            "L1": {"VALUES": 12, "GOALS": 8, "CAPABLE_OF": 15},
            "L2": {"REFERENCES": 412, "DERIVES_FROM": 89},
            "L3": {"CAUSES": 340, "SUPPORTS": 210, "CHALLENGED_BY": 178, "RELATED_TO": 1205},
            "L4": {"PREDICTS": 45, "THREATENS": 67},
        },
    }


def payload_search_results(n: int = 20) -> dict:
    nodes = [
        {
            "id": f"concept-and-or-{i:03d}",
            "label": f"AND-OR conversion example {i}",
            "type": "concept",
            "content": "Every infrastructure OR-gate creates a hidden AND-gate at a different layer.",
            "confidence": 0.85,
            "created_by": "metis",
            "created_at": "2026-07-07T12:00:00Z",
            "tags": ["and-or", "infrastructure", "gates"],
        }
        for i in range(n)
    ]
    return {"results": nodes, "total": n, "query": "AND-OR conversion"}


def payload_neighborhood() -> dict:
    edges = [
        {
            "id": f"edge-{i:04d}",
            "from_node": "concept-and-or-control-plane",
            "to_node": f"concept-demand-rationing-{i:03d}",
            "edge_type": "CAUSES" if i % 3 == 0 else "RELATED_TO",
            "layer": "L3",
            "confidence": 0.8 - (i % 5) * 0.05,
            "provenance": "pattern_analysis",
            "created_by": "metis",
        }
        for i in range(50)
    ]
    return {"center": "concept-and-or-control-plane", "depth": 1, "edges": edges}


def payload_listen(n: int = 30) -> dict:
    events = [
        {
            "id": f"event-{i:04d}",
            "type": "edge.created" if i % 2 == 0 else "node.created",
            "actor": ["metis", "socrates", "hephaestus"][i % 3],
            "node_id": f"concept-{i:03d}",
            "edge_id": f"edge-{i:04d}" if i % 2 == 0 else None,
            "timestamp": f"2026-07-07T{i:02d}:00:00Z",
        }
        for i in range(n)
    ]
    return {"events": events, "since": "2026-07-07T00:00:00Z"}


def payload_confidence() -> dict:
    return {
        "edge_id": "edge-hormuz-and-gate",
        "original_confidence": 0.82,
        "current_confidence": 0.7935,
        "challenges": [
            {"id": "ch-001", "reason": "Israel non-compliance", "confidence": 0.7, "created_by": "socrates"},
            {"id": "ch-002", "reason": "Toll dispute", "confidence": 0.6, "created_by": "metis"},
        ],
        "supports": [
            {"id": "sup-001", "reason": "MoU text leaked", "confidence": 0.85, "created_by": "deepthought"},
        ],
    }


# ── Benchmark harness ─────────────────────────────────────────────────────────


def benchmark(name: str, payload: dict) -> dict:
    json_text = json.dumps(payload, indent=2)
    toon_text = encode_payload(payload, "toon")
    json_tokens = token_count(json_text)
    toon_tokens = token_count(toon_text)
    savings = json_tokens - toon_tokens
    ratio = savings / json_tokens if json_tokens else 0.0
    return {
        "name": name,
        "json_tokens": json_tokens,
        "toon_tokens": toon_tokens,
        "savings": savings,
        "ratio": ratio,
        "json_chars": len(json_text),
        "toon_chars": len(toon_text),
    }


def main() -> None:
    results = [
        benchmark("stats", payload_stats()),
        benchmark("search_20", payload_search_results(20)),
        benchmark("search_100", payload_search_results(100)),
        benchmark("neighborhood_50", payload_neighborhood()),
        benchmark("listen_30", payload_listen(30)),
        benchmark("listen_100", payload_listen(100)),
        benchmark("confidence", payload_confidence()),
    ]

    print("TOON vs JSON token efficiency (cl100k_base tokenizer)\n")
    print(f"{'Payload':<18} {'JSON tok':>10} {'TOON tok':>10} {'Saved':>10} {'%':>8} {'JSON chars':>12} {'TOON chars':>12}")
    print("-" * 92)
    for r in results:
        print(f"{r['name']:<18} {r['json_tokens']:>10} {r['toon_tokens']:>10} {r['savings']:>10} {r['ratio'] * 100:>7.1f}% {r['json_chars']:>12} {r['toon_chars']:>12}")
    print("-" * 92)
    avg_ratio = statistics.mean(r["ratio"] for r in results)
    print(f"{'Average':<18} {'':>10} {'':>10} {'':>10} {avg_ratio * 100:>7.1f}%")


if __name__ == "__main__":
    main()
