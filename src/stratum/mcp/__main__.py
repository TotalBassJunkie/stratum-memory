"""``python -m stratum.mcp`` entry point (WP-11).

Running the package as a module starts the MCP server through
:func:`stratum.mcp.server.main`, so ``python -m stratum.mcp`` is equivalent to
the ``stratum mcp`` console command. The ``mcp`` dependency is only imported
here (via :mod:`stratum.mcp.server`), never by the pure tool layer.
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
