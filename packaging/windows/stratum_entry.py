"""PyInstaller entry point for the Windows ``stratum.exe`` build.

PyInstaller needs a *script* file, not a console-script entry point, so this
tiny launcher stands in for the ``stratum = "stratum.cli:main"`` mapping in
``pyproject.toml``: it imports the real CLI and returns its exit code. Keep it
minimal - every import here is analysed and pulled into the frozen executable.
"""

from __future__ import annotations

import sys

from stratum.cli import main

if __name__ == "__main__":
    sys.exit(main())
