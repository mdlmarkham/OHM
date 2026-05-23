# src/ohm/marimo_pair.py  (shim — actual code in framework/marimo_pair.py)
from ohm.framework.marimo_pair import *  # noqa: F401,F403

try:
    from ohm.framework.marimo_pair import __all__  # noqa: F401
except ImportError:
    pass
