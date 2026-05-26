# src/ohm/game.py  (shim — actual code in inference/game_theory.py)
from ohm.inference.game_theory import *  # noqa: F401,F403

try:
    from ohm.inference.game_theory import __all__  # noqa: F401
except ImportError:
    pass
