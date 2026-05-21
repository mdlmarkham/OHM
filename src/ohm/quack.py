# src/ohm/quack.py  (shim — actual code in graph/quack.py)
from ohm.graph.quack import *  # noqa: F401,F403
try:
    from ohm.graph.quack import __all__  # noqa: F401
except ImportError:
    pass
