# src/ohm/bayesian.py  (shim — actual code in inference/bayesian.py)
from ohm.inference.bayesian import *  # noqa: F401,F403
from ohm.inference.bayesian import (  # noqa: F401
    _bayesian_network_cache,
    _safe_node_id,
    _find_acyclic_subgraph,
    _max_edge_pert_variance_toward,
)
try:
    from ohm.inference.bayesian import __all__  # noqa: F401
except ImportError:
    pass
