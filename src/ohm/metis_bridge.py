# src/ohm/metis_bridge.py  (shim — actual code in framework/metis_bridge.py)
from ohm.framework.metis_bridge import *  # noqa: F401,F403
from ohm.framework.metis_bridge import (  # noqa: F401
    _extract_wikilinks,
    _derive_edge_type,
    _get_zettelkasten_notes,
)

try:
    from ohm.framework.metis_bridge import __all__  # noqa: F401
except ImportError:
    pass
