import duckdb
conn = duckdb.connect(':memory:')
conn.execute('CREATE TABLE t (id TEXT PRIMARY KEY, val TEXT)')
conn.execute("INSERT INTO t VALUES ('a', 'old')")
conn.execute("INSERT INTO t VALUES ('b', 'old')")
# Update both
r = conn.execute('UPDATE t SET val = ? WHERE id = ?', ['new', 'a'])
print('rows_updated:', r.fetchone())
# Update none
r2 = conn.execute('UPDATE t SET val = ? WHERE id = ?', ['new', 'c'])
print('rows_updated_none:', r2.fetchone())
# Verify the value
rows = conn.execute('SELECT * FROM t').fetchall()
print('table:', rows)