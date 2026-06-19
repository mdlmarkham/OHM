# src/ohm/hd.py  (shim — actual code in inference/hd.py)
from ohm.inference.hd import *  # noqa: F401,F403

try:
    from ohm.inference.hd import __all__  # noqa: F401
except ImportError:
    pass
