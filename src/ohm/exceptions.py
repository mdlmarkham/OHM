# src/ohm/exceptions.py  (shim — actual code in framework/exceptions.py)
# CRITICAL: Must re-export ALL exception classes so isinstance() checks work
# with classes from EITHER ohm.exceptions OR ohm.framework.exceptions.
from ohm.framework.exceptions import *  # noqa: F401,F403
try:
    from ohm.framework.exceptions import __all__  # noqa: F401
except ImportError:
    pass
