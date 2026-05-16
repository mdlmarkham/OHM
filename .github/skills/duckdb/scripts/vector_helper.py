#!/usr/bin/env python3
"""
vector_helper.py — VectorStore: embedding storage, HNSW index lifecycle,
RAG retrieval, hybrid search, and TOON-formatted output.

Wraps DuckDBSession's VSS helpers with a higher-level API suitable for
drop-in use in RAG pipelines.

Usage:
    from scripts.vector_helper import VectorStore

    vs = VectorStore("rag.duckdb", dim=384, table="embeddings")
    vs.upsert("doc-1", embedding=[...], content="Pump seal inspection guide")
    results = vs.search(query_embedding=[...], top_k=5)
    print(vs.results_to_toon(results, "rag_hits"))
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from scripts.duckdb_helper import DuckDBSession, DuckDBConfig, df_to_toon, _require_identifier

logger = logging.getLogger(__name__)


class VectorStore:
    """
    Persistent vector store backed by DuckDB + HNSW index.

    Table schema:
        id        VARCHAR PRIMARY KEY
        embedding FLOAT[dim]
        content   VARCHAR
        metadata  JSON       ← arbitrary filter attributes for hybrid search

    Index is created once (after first bulk load) or on demand via build_index().
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        dim: int = 384,
        table: str = "embeddings",
        metric: str = "cosine",
        config: DuckDBConfig | None = None,
    ) -> None:
        self.db_path = db_path
        self.dim = dim
        self.table = _require_identifier(table, "table")
        self.metric = metric
        self._session: DuckDBSession | None = None
        self._config = config or DuckDBConfig()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "VectorStore":
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def open(self) -> None:
        if self._session is not None:
            return
        self._session = DuckDBSession(self.db_path, config=self._config)
        self._session.connect()
        self._session.create_vector_table(
            self.table,
            dim=self.dim,
            extra_cols={"content": "VARCHAR", "metadata": "JSON"},
        )

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None

    @property
    def _db(self) -> DuckDBSession:
        if self._session is None:
            raise RuntimeError("VectorStore not open. Use as context manager or call open().")
        return self._session

    # ------------------------------------------------------------------
    # Data operations
    # ------------------------------------------------------------------

    def upsert(
        self,
        id: str,
        embedding: list[float] | "np.ndarray",
        content: str = "",
        metadata: dict | None = None,
    ) -> None:
        """
        Insert or replace a single embedding row.

        embedding can be a Python list or a NumPy array — it is passed as a
        DuckDB parameter (not interpolated into SQL).
        """
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if len(embedding) != self.dim:
            raise ValueError(f"Embedding dim {len(embedding)} ≠ expected {self.dim}")

        import json
        meta_str = json.dumps(metadata or {})
        self._db.execute(
            f"INSERT OR REPLACE INTO {self.table} (id, embedding, content, metadata) "
            f"VALUES (?, ?::FLOAT[{self.dim}], ?, ?::JSON)",
            [id, embedding, content, meta_str],
        )

    def upsert_batch(self, rows: list[dict]) -> int:
        """
        Bulk upsert from a list of dicts with keys: id, embedding, content, metadata.

        Uses a DataFrame → DuckDB relation for efficient bulk load.
        Returns number of rows upserted.
        """
        import json
        import pandas as pd

        records = []
        for r in rows:
            emb = r["embedding"]
            if hasattr(emb, "tolist"):
                emb = emb.tolist()
            if len(emb) != self.dim:
                raise ValueError(f"Row {r['id']!r}: dim {len(emb)} ≠ {self.dim}")
            records.append({
                "id": r["id"],
                "embedding": emb,
                "content": r.get("content", ""),
                "metadata": json.dumps(r.get("metadata", {})),
            })

        df = pd.DataFrame(records)
        # Register as DuckDB relation then INSERT OR REPLACE
        self._db.con.register("__upsert_df", df)
        self._db.execute(
            f"INSERT OR REPLACE INTO {self.table} "
            f"SELECT id, embedding::FLOAT[{self.dim}], content, metadata::JSON "
            f"FROM __upsert_df"
        )
        self._db.con.unregister("__upsert_df")
        logger.info("Upserted %d rows into %s", len(records), self.table)
        return len(records)

    def build_index(self) -> None:
        """
        Build (or rebuild) the HNSW index.

        Call after initial bulk load for best performance.
        Safe to call multiple times — uses CREATE INDEX IF NOT EXISTS.
        """
        self._db.build_hnsw_index(
            table=self.table,
            embedding_col="embedding",
            index_name=f"{self.table}_hnsw",
            metric=self.metric,
        )

    def count(self) -> int:
        return self._db.scalar(f"SELECT COUNT(*) FROM {self.table}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query_embedding: list[float] | "np.ndarray",
        top_k: int = 5,
        where: str | None = None,
        return_cols: list[str] | None = None,
    ) -> pd.DataFrame:
        """
        Semantic nearest-neighbour search.

        Args:
            query_embedding: Query vector (same dim as stored embeddings)
            top_k: Number of results to return
            where: Optional SQL boolean expression for metadata filtering,
                   e.g. "json_extract_string(metadata,'$.category') = 'maintenance'"
                   Applied post-retrieval (over-fetch then filter).
            return_cols: Subset of columns to return (None = all)

        Returns:
            DataFrame with result rows + 'dist' column (lower = more similar).
        """
        if hasattr(query_embedding, "tolist"):
            query_embedding = query_embedding.tolist()
        return self._db.vector_search(
            table=self.table,
            query_vector=query_embedding,
            top_k=top_k,
            metric=self.metric,
            embedding_col="embedding",
            return_cols=return_cols,
            where=where,
        )

    def hybrid_search(
        self,
        query_embedding: list[float] | "np.ndarray",
        fts_query: str | None = None,
        top_k: int = 5,
        vector_weight: float = 0.7,
        fts_weight: float = 0.3,
    ) -> pd.DataFrame:
        """
        Reciprocal Rank Fusion of VSS + BM25 full-text search.

        Requires the FTS extension to be loaded and an FTS index on `content`.
        Scores are fused as: combined = vector_weight/rank_v + fts_weight/rank_f

        Falls back to pure vector search if fts_query is None.
        """
        if fts_query is None:
            return self.search(query_embedding, top_k=top_k)

        if hasattr(query_embedding, "tolist"):
            query_embedding = query_embedding.tolist()

        dim = len(query_embedding)
        vw, fw = float(vector_weight), float(fts_weight)

        # Ensure FTS index exists
        self._db.load_extensions("fts")
        try:
            self._db.execute(f"PRAGMA create_fts_index('{self.table}', 'id', 'content');")
        except Exception:
            pass  # Already exists

        sql = f"""
            WITH vec_hits AS (
                SELECT id, content,
                       ROW_NUMBER() OVER (ORDER BY array_cosine_distance(embedding, $1::FLOAT[{dim}])) AS rank_v
                FROM {self.table}
                LIMIT {int(top_k) * 10}
            ),
            fts_hits AS (
                SELECT id, content,
                       ROW_NUMBER() OVER (ORDER BY fts_main_{self.table}.match_bm25(id, $2) DESC NULLS LAST) AS rank_f
                FROM {self.table}
                WHERE fts_main_{self.table}.match_bm25(id, $2) IS NOT NULL
                LIMIT {int(top_k) * 10}
            ),
            fused AS (
                SELECT COALESCE(v.id, f.id) AS id,
                       COALESCE(v.content, f.content) AS content,
                       COALESCE({vw} / NULLIF(v.rank_v, 0), 0)
                       + COALESCE({fw} / NULLIF(f.rank_f, 0), 0) AS rrf_score
                FROM vec_hits v
                FULL OUTER JOIN fts_hits f USING (id)
            )
            SELECT id, content, rrf_score
            FROM fused
            ORDER BY rrf_score DESC
            LIMIT {int(top_k)};
        """
        return self._db.query(sql, [query_embedding, fts_query])

    # ------------------------------------------------------------------
    # TOON output
    # ------------------------------------------------------------------

    def results_to_toon(self, df: pd.DataFrame, label: str, max_rows: int = 20) -> str:
        """Serialise search results as TOON (omit the embedding column)."""
        display_cols = [c for c in df.columns if c != "embedding"]
        return df_to_toon(df[display_cols], label, max_rows=max_rows)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def export_parquet(self, output_path: str, compression: str = "zstd") -> int:
        """Export all embeddings to Parquet (without the index)."""
        return self._db.writeback_parquet(
            source_table_or_query=self.table,
            output_path=output_path,
            compression=compression,
            dry_run=False,
        )

    def stats(self) -> str:
        """Return TOON-formatted store statistics."""
        n = self.count()
        idx_exists = self._db.scalar(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE table_name = ? AND index_name = ?",
            [self.table, f"{self.table}_hnsw"],
        ) > 0
        return df_to_toon(
            pd.DataFrame([{
                "table": self.table,
                "dim": self.dim,
                "metric": self.metric,
                "rows": n,
                "hnsw_index": "yes" if idx_exists else "no (call build_index())",
            }]),
            "vector_store_stats",
        )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    DIM = 8
    with VectorStore(":memory:", dim=DIM, table="test_emb") as vs:
        print(f"Empty store: {vs.count()} rows")

        # Bulk upsert
        rows = [
            {"id": f"doc{i}", "embedding": [random.random() for _ in range(DIM)],
             "content": f"Document {i} about topic {i%3}", "metadata": {"topic": i % 3}}
            for i in range(50)
        ]
        vs.upsert_batch(rows)
        vs.build_index()
        print(f"After upsert: {vs.count()} rows")
        print(vs.stats())

        # Search
        q = [random.random() for _ in range(DIM)]
        results = vs.search(q, top_k=3)
        print("\n--- Search results ---")
        print(vs.results_to_toon(results, "results"))
