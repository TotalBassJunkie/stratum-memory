# Windows installers (MSI + EXE)

Sources for the two Windows installer assets fixed by
[`docs/distribution.md`](../../docs/distribution.md):

| Asset | Built by | Source |
|---|---|---|
| `Stratum-<version>-x64.msi` | WiX v4 (`wix build`) | [`stratum.wxs`](stratum.wxs) |
| `Stratum-<version>-setup.exe` | Inno Setup 6 (`ISCC.exe`) | [`stratum.iss`](stratum.iss) |

Both are built on the `windows-latest` CI runner from the same PyInstaller
freeze, so the MSI and the EXE ship the identical `stratum.exe`.

## What gets installed

One file, one PATH entry, one shortcut:

* `stratum.exe` — the one-file PyInstaller build of the `stratum` CLI
  (equivalent to `stratum = "stratum.cli:main"` in `pyproject.toml`).
* The install directory is appended to `PATH` (`%ProgramFiles%\Stratum` for the
  MSI, `{autopf}\Stratum` for the EXE installer), and removed again on uninstall.
* A Start-menu shortcut.

The installers do **not** touch Docker, Postgres or `~/.stratum`. After
installing, run `stratum install` to bootstrap the local stack — exactly as the
`install.ps1` one-liner does.

## Install

### MSI

```powershell
# double-click, or:
msiexec /i .\Stratum-0.1.0-x64.msi

# silent, per-machine (prompts for elevation):
msiexec /i .\Stratum-0.1.0-x64.msi /qn

# log to a file when troubleshooting:
msiexec /i .\Stratum-0.1.0-x64.msi /l*v msi.log
```

Uninstall via **Settings → Apps**, or `msiexec /x .\Stratum-0.1.0-x64.msi`.

The MSI is **per-machine**: it installs to `%ProgramFiles%\Stratum` and needs
elevation. Upgrades replace the previous version (fixed `UpgradeCode`, see the
comment in `stratum.wxs`); downgrades are refused.

### EXE (Inno Setup)

Double-click `Stratum-0.1.0-setup.exe`, or:

```powershell
.\Stratum-0.1.0-setup.exe            # interactive
.\Stratum-0.1.0-setup.exe /VERYSILENT # unattended
```

By default the EXE installer is **per-user** (`PrivilegesRequired=lowest`), so it
needs no UAC prompt and installs under `%LocalAppData%\Programs\Stratum`. Pass
`/ALLUSERS` (or pick the mode in the dialog) for a per-machine install under
`%ProgramFiles%\Stratum`.

## Build

Run from the repository root; both scripts resolve paths relative to themselves.

```powershell
# MSI -> dist\Stratum-<version>-x64.msi
.\packaging\windows\build-msi.ps1 -Version 0.1.0

# EXE -> dist\Stratum-<version>-setup.exe
.\packaging\windows\build-exe.ps1 -Version 0.1.0
```

Omit `-Version` and the scripts read `__version__` from
`src/stratum/__init__.py` (the single source of truth). The version is never
hard-coded in a file name: the scripts compose it, and WiX/Inno receive it as a
build variable (`-d Version=` / `/DVersion=`). Both fail loudly when it is
missing.

What the scripts do:

1. install the toolchain if absent — `dotnet tool install --global wix` for the
   MSI, `winget install JRSoftware.InnoSetup` (or `choco install innosetup`) for
   the EXE;
2. freeze the CLI:
   `python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/windows/stratum.spec`
   → `dist\stratum.exe`;
3. compile the installer into `dist\` under the contract name.

### Toolchain requirements

* **WiX v4+** (`wix.exe`) and the .NET SDK 6+ — `dotnet tool install --global wix`
  puts `wix.exe` in `%USERPROFILE%\.dotnet\tools`.
* **Inno Setup 6** — `ISCC.exe` in `Program Files (x86)\Inno Setup 6`.
* Python 3.12+ with the project's dev extras (`pip install -e ".[dev]"`) plus
  `pyinstaller`.

### The `.spec` file

[`stratum.spec`](stratum.spec) builds a **one-file**, **console** executable
named `stratum` from [`stratum_entry.py`](stratum_entry.py) (a two-line launcher
around `stratum.cli:main`, because PyInstaller wants a script, not a console-script
entry point). It collects all `stratum.*` submodules explicitly — the CLI imports
its collaborators lazily — and adds `mcp` as a hidden import, since
`stratum.mcp` shadows that package name.

## Manual equivalent

If you would rather run the tools yourself:

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\windows\stratum.spec

# MSI
wix build -arch x64 `
  -d Version=0.1.0 `
  -d ExePath="$PWD\dist\stratum.exe" `
  -d IconPath="$PWD\assets\logo\favicon.ico" `
  -o "dist\Stratum-0.1.0-x64.msi" `
  packaging\windows\stratum.wxs

# EXE
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" `
  /DVersion=0.1.0 `
  /DExePath="$PWD\dist\stratum.exe" `
  /DIconPath="$PWD\assets\logo\favicon.ico" `
  /O"$PWD\dist" /FStratum-0.1.0-setup `
  packaging\windows\stratum.iss
```

## Notes

* Neither installer embeds a secret; nothing here reads credentials or the
  environment of the build machine.
* The `SHA256SUMS` asset listed in `docs/distribution.md` is produced by the CI
  workflow from whatever is in `dist/`, so these artefacts are covered
  automatically.
* Windows code signing is not wired up yet: the artefacts are unsigned, so
  SmartScreen will warn on first run.
