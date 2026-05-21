# src/ohm/methods.py  (shim — actual code in graph/methods.py)
from ohm.graph.methods import *  # noqa: F401,F403
try:
    from ohm.graph.methods import __all__  # noqa: F401
except ImportError:
    pass
