"""Graph handler mixin — placeholder until full extraction (OHM-xily).

Currently, all graph-related handler methods remain inline in server.py.
This mixin is empty so that OhmHandler can inherit from it without error.
As handler methods are extracted from server.py, they will be moved here.
"""


class GraphHandlerMixin:
    """Placeholder mixin for graph endpoint handlers.

    All graph-related handlers are still defined in server.py inline.
    This class exists solely to satisfy the import in OhmHandler.__bases__.
    Methods will be migrated here as part of the handler extraction (OHM-xily).
    """
    pass
