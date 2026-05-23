# src/ohm/pert.py  (shim — actual code in inference/pert.py)
from ohm.inference.pert import *  # noqa: F401,F403

try:
    from ohm.inference.pert import __all__  # noqa: F401
except ImportError:
    pass
