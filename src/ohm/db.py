# src/ohm/db.py  (shim — actual code in graph/db.py)
from ohm.graph.db import *  # noqa: F401,F403
from ohm.graph.db import (  # noqa: F401
    _load_extensions,
    _get_ducklake_path,
    _try_ducklake_recovery,
    _auto_restore_if_empty,
    _create_ducklake_tables,
)
try:
    from ohm.graph.db import __all__  # noqa: F401
except ImportError:
    pass
