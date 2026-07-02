import duckdb

conn = duckdb.connect(":memory:")
conn.execute("CREATE TABLE t (id VARCHAR PRIMARY KEY)")
conn.execute("INSERT INTO t VALUES ('x')")
try:
    conn.execute("INSERT INTO t VALUES ('x')")
    print("NO ERROR - duplicate accepted!")
except Exception as e:
    print(f"ERROR: {e}")
