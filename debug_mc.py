import sys
sys.path.insert(0, 'src')
import duckdb
conn = duckdb.connect(':memory:')
from ohm.schema import initialize_schema
initialize_schema(conn)
from ohm.queries import create_node, create_edge, _rows_to_dicts

a = create_node(conn, label='A', node_type='concept', created_by='test')
b = create_node(conn, label='B', node_type='concept', created_by='test')
c = create_node(conn, label='C', node_type='concept', created_by='test')
ab = create_edge(conn, from_node=a['id'], to_node=b['id'], layer='L3', edge_type='CAUSES', created_by='test', probability=0.5)
bc = create_edge(conn, from_node=b['id'], to_node=c['id'], layer='L3', edge_type='CAUSES', created_by='test', probability=0.5)

print('A:', a['id'], 'B:', b['id'], 'C:', c['id'])
print('AB edge:', ab['id'], ab['from_node'], '->', ab['to_node'])
print('BC edge:', bc['id'], bc['from_node'], '->', bc['to_node'])

# Run edges query manually
node_id = a['id']
max_depth = 10
edges_query = """
    WITH RECURSIVE cascade AS (
        SELECT
            ? AS node_id,
            0 AS depth,
            list_value(?) AS path
        UNION ALL
        SELECT
            e.to_node AS node_id,
            c.depth + 1 AS depth,
            list_concat(c.path, list_value(e.to_node)) AS path
        FROM cascade c
        JOIN ohm_edges e ON e.from_node = c.node_id
        WHERE c.depth < ?
          AND e.edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
          AND NOT list_contains(c.path, e.to_node)
    )
    SELECT DISTINCT
        c.node_id,
        c.depth,
        c.path,
        e.from_node,
        e.edge_type,
        e.probability,
        e.confidence
    FROM cascade c
    JOIN ohm_edges e ON e.from_node = c.node_id
    WHERE c.depth > 0
    ORDER BY c.depth, c.node_id
"""
edges_result = conn.execute(edges_query, [node_id, node_id, max_depth])
edges = _rows_to_dicts(edges_result)
print('Edges found:', len(edges))
for e in edges:
    print('  edge:', e)