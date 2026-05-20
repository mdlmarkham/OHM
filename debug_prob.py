"""Debug script for probability/confidence computation."""
import duckdb
from ohm.schema import initialize_schema
from ohm.bayesian import build_bayesian_network

conn = duckdb.connect(':memory:')
initialize_schema(conn)

# Test 1: Edge with confidence=0.9 but NO probability
conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('a', 'conf_cause', 'concept', 'test')")
conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('b', 'conf_effect', 'concept', 'test')")
conn.execute("INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by) VALUES ('e1', 'a', 'b', 'L3', 'CAUSES', 0.9, 'test')")

# Check SQL
rows = conn.execute("SELECT from_node, to_node, edge_type, probability as raw_probability, confidence as raw_confidence FROM ohm_edges WHERE edge_type IN ('CAUSES') AND deleted_at IS NULL").fetchall()
print('SQL rows:', rows)
print('raw_probability:', rows[0][3], 'is None:', rows[0][3] is None)
print('raw_confidence:', rows[0][4])

result = build_bayesian_network(conn, default_probability=0.5)
if result:
    for e in result['edges']:
        print(f"probability={e['probability']}, confidence={e['confidence']}, has_explicit_prob={e.get('has_explicit_probability')}")
else:
    print("Result is None")