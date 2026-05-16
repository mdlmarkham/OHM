"""
OHM Query — natural language query interface.

Translates natural language queries into parameterized SQL.
Uses pattern matching for common query types.
"""

from typing import Optional

from .graph import build_neighborhood_query, build_path_query, build_impact_query


# Pattern matching for natural language queries
QUERY_PATTERNS = [
    # Agent state (most specific patterns first)
    (r"(?:who is working on|who\'s researching|who\'s thinking about)\s+(.+)", "who_working"),
    (r"(?:what is|what are)\s+(\S+)\s+(?:working on|thinking about|researching)", "agent_focus"),
    # Confidence queries
    (r"(?:confidence of|audit|trust|verify)\s+(\S+)", "confidence"),
    # Impact queries
    (r"(?:impact of|what depends on|downstream from|what if)\s+(\S+)", "impact"),
    # Path queries
    (r"(?:path from|how does|route from|connection between)\s+(\S+)\s+(?:to|and)\s+(\S+)", "path"),
    # Neighborhood queries
    (r"(?:what connects to|neighborhood of|around|near)\s+(\S+)", "neighborhood"),
    # Lookup (catch-all for short phrases)
    (r"(?:what is|who is|tell me about)\s+(\S+)", "lookup"),
]


def parse_query(query: str) -> Optional[dict]:
    """Parse a natural language query into a structured query object.

    Returns a dict with:
        - type: 'neighborhood', 'path', 'impact', 'confidence', 'who_working', 'agent_focus', 'unknown'
        - params: extracted parameters
    """
    import re

    query_lower = query.lower().strip()

    for pattern, qtype in QUERY_PATTERNS:
        match = re.search(pattern, query_lower)
        if match:
            return {
                "type": qtype,
                "params": match.groups(),
                "original": query,
            }

    # Fallback: try as a node ID lookup
    # If it's a single word or short phrase, treat as neighborhood
    if len(query_lower.split()) <= 3:
        return {
            "type": "neighborhood",
            "params": (query_lower.replace(" ", "_"),),
            "original": query,
        }

    return {
        "type": "unknown",
        "params": (),
        "original": query,
    }


def execute_parsed_query(store, parsed: dict, agent_name: str = "ohm"):
    """Execute a parsed query against the store.

    Args:
        store: OhmStore instance
        parsed: Result from parse_query()
        agent_name: Calling agent name

    Returns:
        List of result dicts
    """
    qtype = parsed["type"]
    params = parsed["params"]

    if qtype == "neighborhood":
        node_id = params[0]
        sql, sql_params = build_neighborhood_query(node_id, depth=3)
        return store.execute(sql, sql_params)

    elif qtype == "path":
        from_node, to_node = params[0], params[1]
        sql, sql_params = build_path_query(from_node, to_node, max_depth=5)
        return store.execute(sql, sql_params)

    elif qtype == "impact":
        node_id = params[0]
        sql, sql_params = build_impact_query(node_id, depth=5)
        return store.execute(sql, sql_params)

    elif qtype == "confidence":
        edge_id = params[0]
        from .graph import build_confidence_audit_query
        sql, sql_params = build_confidence_audit_query(edge_id)
        return store.execute(sql, sql_params)

    elif qtype == "who_working":
        topic = params[0]
        return store.who_is_working_on(topic)

    elif qtype == "agent_focus":
        agent_name_query = params[0]
        state = store.get_agent_state(agent_name_query)
        return [state] if state else []

    elif qtype == "lookup":
        node_id = params[0]
        node = store.get_node(node_id)
        if node:
            # Also get neighborhood
            sql, sql_params = build_neighborhood_query(node_id, depth=1)
            neighbors = store.execute(sql, sql_params)
            return {"node": node, "neighbors": neighbors}
        return []

    else:
        # Unknown query type - try full-text search on nodes
        return store.execute(
            "SELECT * FROM ohm_nodes WHERE label ILIKE ? OR content ILIKE ? LIMIT 10",
            [f"%{parsed['original']}%", f"%{parsed['original']}%"],
        )
