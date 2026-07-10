# src/ohm/schema.py  (shim — actual code in graph/schema.py)
from ohm.graph.schema import *  # noqa: F401,F403
from ohm.graph.schema import (  # noqa: F401
    _ensure_meta_table,
    _apply_migrations,
    _apply_migrations_ducklake,
    _create_hnsw_index,
    _seed_agent_configs,
    initialize_schema_ducklake,
)

try:
    from ohm.graph.schema import __all__  # noqa: F401
except ImportError:
    pass
