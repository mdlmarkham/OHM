# src/ohm/ingest.py  (shim — actual code in framework/ingest.py)
from ohm.framework.ingest import *  # noqa: F401,F403
try:
    from ohm.framework.ingest import __all__  # noqa: F401
except ImportError:
    pass
