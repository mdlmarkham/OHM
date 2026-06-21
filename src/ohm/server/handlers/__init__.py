"""OHM server handler mixins — domain-grouped endpoint methods for OhmHandler.

Each module in this package defines a mixin class whose methods are endpoint
handlers for a specific domain area. OhmHandler inherits all of them:

    class OhmHandler(
        TenantHandlerMixin,
        AdminHandlerMixin,
        InfraHandlerMixin,
        MarkovHandlerMixin,
        InferenceHandlerMixin,
        AnalysisHandlerMixin,
        GraphHandlerMixin,
        BaseHTTPRequestHandler,
    ): ...

Mixin methods access shared state (current_store, schema_config, _json_response,
etc.) through self, which resolves to OhmHandler at runtime. Mixins define no
class-level state of their own.

See OHM-xily for the full migration plan and design rationale.
"""

from ohm.server.handlers.admin import AdminHandlerMixin
from ohm.server.handlers.decision import DecisionHandlerMixin
from ohm.server.handlers.documents import DocumentHandlerMixin
from ohm.server.handlers.graph import GraphHandlerMixin
from ohm.server.handlers.infra import InfraHandlerMixin
from ohm.server.handlers.tenant import TenantHandlerMixin

__all__ = [
    "AdminHandlerMixin",
    "AnalysisHandlerMixin",
    "CatalogHandlerMixin",
    "DecisionHandlerMixin",
    "DocumentHandlerMixin",
    "GraphHandlerMixin",
    "InfraHandlerMixin",
    "InferenceHandlerMixin",
    "MarkovHandlerMixin",
    "TenantHandlerMixin",
]

# Keep existing class definition in server.py functional
# (DecisionHandlerMixin is added there).
