# src/ohm/contract.py  (shim — actual code in server/contract.py)
from ohm.server.contract import *  # noqa: F401,F403
try:
    from ohm.server.contract import __all__  # noqa: F401
except ImportError:
    pass
