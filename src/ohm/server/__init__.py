"""OHM server layer — HTTP daemon, boundary enforcement, contracts, visualization."""
import sys
import types

from .boundary import enforce_challenge_boundary, enforce_support_boundary, enforce_write_boundary
from .contract import ContractConfig, lint_graph
from .visualization import to_mermaid
from . import server as _server_module


class _ServerPackage(types.ModuleType):
    """Module proxy that forwards attribute reads/writes to server.server.

    This ensures that tests which do ``import ohm.server as srv`` and then
    mutate module-level constants (``srv.RATE_LIMIT_MAX_REQUESTS = 5``) are
    reflected in the ``server/server.py`` module that OhmHandler actually reads.

    The ``__file__`` attribute points to ``server/server.py`` so that
    ``inspect.getsource(ohm.server)`` returns the main server implementation.
    """

    def __getattr__(self, name: str):
        # Fall through to server.server module
        try:
            return getattr(_server_module, name)
        except AttributeError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        _INTERNAL = frozenset({
            "__spec__", "__loader__", "__package__", "__path__",
            "__file__", "__cached__", "__builtins__", "__doc__", "__name__",
        })
        if name in _INTERNAL:
            super().__setattr__(name, value)
        else:
            object.__setattr__(self, name, value)
            setattr(_server_module, name, value)


_current_module = sys.modules[__name__]
_proxy = _ServerPackage(__name__)
_proxy.__dict__.update(_current_module.__dict__)
_proxy.__file__ = _server_module.__file__  # Critical: makes inspect.getsource work
_proxy.__spec__ = _current_module.__spec__
_proxy.__loader__ = _current_module.__loader__
_proxy.__package__ = _current_module.__package__
_proxy.__path__ = _current_module.__path__
sys.modules[__name__] = _proxy
