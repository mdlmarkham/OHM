"""Entry point for ``python -m ohm.server``.

Mirrors the ``ohmd`` console script so operators can launch the
daemon without installing the package (CI, ephemeral containers,
``ohm serve start``'s underlying subprocess). Adding this module
enables ``python -m ohm.server --port N --db PATH`` as a working
invocation alongside the registered ``ohmd`` entry point.
"""

from __future__ import annotations

from ohm.server.server import main


if __name__ == "__main__":
    main()
