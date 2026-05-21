# src/ohm/semantic_roles.py  (shim — actual code in framework/semantic_roles.py)
from ohm.framework.semantic_roles import *  # noqa: F401,F403
try:
    from ohm.framework.semantic_roles import __all__  # noqa: F401
except ImportError:
    pass
