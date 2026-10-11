"""MCP server + agent tool API for Stratum-Memory (WP-11).

The package has two layers:

* :mod:`stratum.mcp.tools` — the **pure, testable** tool functions
  (:func:`stratum_write`, :func:`stratum_query`, ...). This module imports **no**
  MCP library and no database at import time: every collaborator
  (``llm``/``store``/``graph``/``fs``/``curator``/``query``/``auth``) is injected,
  so the tools are exercisable with in-memory fakes and without a running server.
* :mod:`stratum.mcp.server` — the thin FastMCP adapter that registers the ten
  tools with their signatures and docstrings and selects the transport
  (``stdio`` or ``streamable-http``) from :class:`stratum.config.Settings`.

Importing this package therefore never requires the ``mcp`` dependency; only
:mod:`stratum.mcp.server` does, and it fails loudly if ``mcp`` is missing.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Tool-API revision marker (independent of the package version).
__version__ = "0.1.0"
