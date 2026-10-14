# ruff: noqa: F821, UP009 -- PyInstaller injects Analysis/PYZ/EXE/SPECPATH;
# the emacs-style coding line is PyInstaller's own spec header convention.
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the one-file Windows build of the ``stratum`` CLI.

Used by ``packaging/windows/build-msi.ps1`` and ``build-exe.ps1``:

    pyinstaller --noconfirm --clean --distpath dist --workpath build \
        packaging/windows/stratum.spec

Result: ``dist/stratum.exe`` - a single, console-mode executable with the same
behaviour as the ``stratum = "stratum.cli:main"`` console script declared in
``pyproject.toml`` (see ``packaging/windows/stratum_entry.py``).

Two things are deliberately explicit:

* ``stratum.*`` submodules are collected wholesale. ``stratum.cli`` imports
  most collaborators lazily (``from .mcp.server import main``, ``from
  .observability import health``), so static analysis alone would drop them.
* the ``mcp`` library is a hidden import. It is the only third-party package
  the CLI imports at call time rather than at start-up, and ``stratum.mcp`` is
  a package *name* that shadows it, which makes the dependency easy to miss.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

#: Repository root is two levels up from this spec (``packaging/windows/``).
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))

hiddenimports = collect_submodules("stratum") + [
    # Imported lazily by stratum.cli._cmd_mcp -> stratum.mcp.server.
    "mcp",
]

a = Analysis(
    [os.path.join(SPECPATH, "stratum_entry.py")],
    pathex=[os.path.join(REPO_ROOT, "src")],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="stratum",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(REPO_ROOT, "assets", "logo", "favicon.ico"),
)
