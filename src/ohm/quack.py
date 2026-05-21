# src/ohm/quack.py  (transparent alias — redirects to ohm.graph.quack)
# Uses sys.modules replacement so module-level state (e.g. _quack_available)
# is shared correctly when accessed via either import path.
import sys as _sys
import ohm.graph.quack as _real
_sys.modules[__name__] = _real
