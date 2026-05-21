# src/ohm/evidence.py  (shim — actual code in inference/evidence.py)
from ohm.inference.evidence import *  # noqa: F401,F403
from ohm.inference.evidence import (  # noqa: F401
    _norm_cdf,
    _sigmoid,
)
try:
    from ohm.inference.evidence import __all__  # noqa: F401
except ImportError:
    pass
