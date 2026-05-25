"""OHM server handler mixins — domain-grouped endpoint methods for OhmHandler.

Each module in this package defines a mixin class whose methods are endpoint
handlers for a specific domain area. OhmHandler inherits all of them:

    class OhmHandler(
        TenantHandlerMixin,
        AdminHandlerMixin,
        MarkovHandlerMixin,
        InferenceHandlerMixin,
        AnalysisHandlerMixin,
        GraphHandlerMixin,
        InfraHandlerMixin,
        BaseHTTPRequestHandler,
    ): ...

Mixin methods access shared state (current_store, schema_config, _json_response,
etc.) through self, which resolves to OhmHandler at runtime. Mixins define no
class-level state of their own.

See OHM-xily for the full migration plan and design rationale.
"""

from ohm.server.handlers.admin import AdminHandlerMixin
from ohm.server.handlers.tenant import TenantHandlerMixin
