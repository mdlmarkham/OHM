# src/ohm/graph_reader.py  (shim — actual code in framework/graph_reader.py)
from ohm.framework.graph_reader import *  # noqa: F401,F403
try:
    from ohm.framework.graph_reader import __all__  # noqa: F401
except ImportError:
    pass
