"""Command-line entry point for Stratum-Memory (``stratum`` console script).

Declared in ``pyproject.toml`` as ``stratum = "stratum.cli:main"``. It provides a
minimal, dependency-light dispatcher over the two operator commands the work
package requires:

* ``stratum mcp``    — start the MCP server (stdio / streamable-http), delegating
  to :func:`stratum.mcp.server.main`, so the ``mcp`` library is imported only
  when the server is actually started.
* ``stratum health`` — print the readiness probe (:func:`stratum.observability.health`)
  as JSON and exit non-zero when the status is not ``ok``.

The ``mcp`` sub-command forwards any extra arguments (e.g. ``--transport stdio``)
verbatim to the server's own argument parser.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

__all__ = ["main"]


def _cmd_mcp(argv: Sequence[str]) -> int:
    """Start the MCP server (imports the ``mcp`` library lazily)."""
    from .mcp.server import main as mcp_main

    return int(mcp_main(list(argv)))


def _cmd_health(_argv: Sequence[str]) -> int:
    """Print the readiness probe as JSON; non-zero exit when degraded."""
    from .observability import health

    report = health()
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") == "ok" else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the ``stratum`` CLI.

    Args:
        argv: Argument list (excluding the program name); defaults to
            :data:`sys.argv` via :mod:`argparse`.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="stratum", description="Stratum-Memory command-line interface."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subparsers.add_parser("mcp", help="run the MCP server")
    mcp_parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default=None
    )
    mcp_parser.set_defaults(func=_cmd_mcp)

    health_parser = subparsers.add_parser("health", help="print the readiness probe")
    health_parser.set_defaults(func=_cmd_health)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "mcp":
        forwarded: list[str] = []
        if args.transport is not None:
            forwarded += ["--transport", args.transport]
        return _cmd_mcp(forwarded)
    return args.func([])


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
