# src/ohm/markov.py  (shim — actual code in inference/markov.py)
from ohm.inference.markov import *  # noqa: F401,F403
from ohm.inference.markov import (  # noqa: F401
    _require_numpy,
    _build_transition_matrix,
)
try:
    from ohm.inference.markov import __all__  # noqa: F401
except ImportError:
    pass
