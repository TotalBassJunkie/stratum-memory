# Stratum-Memory — Distribution & Installation

This document is the **contract** for every download/install channel. All build
artifacts are produced by GitHub Actions on a version tag and attached to a
GitHub Release, so every channel resolves to the same published files.

## Canonical identifiers

| Thing | Value |
|---|---|
| GitHub repo | `TotalBassJunkie/stratum-memory` |
| PyPI package | `stratum-memory` (CLI: `stratum`) |
| npm package | `stratum-memory` |
| Container image | `ghcr.io/totalbassjunkie/stratum-memory` |
| Homebrew tap | `TotalBassJunkie/homebrew-stratum` |
| Scoop bucket | `TotalBassJunkie/scoop-stratum` |
| winget id | `TotalBassJunkie.StratumMemory` |
| Version source | `src/stratum/__init__.py` (`__version__`) |

## Release assets (naming contract)

Produced on tag `v<version>` and attached to the GitHub Release:

| Asset | OS | Built by |
|---|---|---|
| `stratum_memory-<version>-py3-none-any.whl` | any | `python -m build` |
| `stratum_memory-<version>.tar.gz` | any | `python -m build` |
| `stratum_<version>_amd64.deb` | Linux | nfpm (ubuntu runner) |
| `stratum-<version>-1.x86_64.rpm` | Linux | nfpm (ubuntu runner) |
| `Stratum-<version>-x86_64.AppImage` | Linux | appimagetool (ubuntu runner) |
| `stratum_<version>_amd64.snap` | Linux | snapcraft (ubuntu runner) |
| `Stratum-<version>-x64.msi` | Windows | WiX v4 (windows runner) |
| `Stratum-<version>-setup.exe` | Windows | Inno Setup (windows runner) |
| `Stratum-<version>-x86_64.pkg` | macOS | pkgbuild/productbuild (macos runner) |
| `Stratum-<version>-x86_64.dmg` | macOS | hdiutil (macos runner) |
| `stratum.exe` | Windows | PyInstaller (frozen CLI, shipped inside the MSI/EXE) |
| `SHA256SUMS` | any | checksums for all of the above |

The two Python distributions carry the PEP 503-normalised project name:
`pyproject.toml` declares `name = "stratum-memory"`, and `python -m build`
rewrites the dash to an underscore before naming the files, so the wheel is
`stratum_memory-<version>-py3-none-any.whl` and the sdist is
`stratum_memory-<version>.tar.gz`. Every consumer (release workflow, Homebrew
formula, AppImage/Snap payload, PyPI publish) must use that spelling.

## Channel matrix

### Linux
1. **curl | bash** (primary, no package manager):
   `curl -fsSL https://get.stratum.dev/install.sh | bash` (and the GitHub raw URL).
2. **.deb** (`apt install ./stratum_*.deb`) — Debian/Ubuntu.
3. **.rpm** (`dnf install ./stratum-*.rpm`) — Fedora/RHEL/openSUSE.
4. **AppImage** (`chmod +x Stratum-*.AppImage && ./Stratum-*.AppImage`).
5. **Snap** (`snap install stratum-memory`) — needs the snap in the store.
6. **pipx / pip** (`pipx install stratum-memory`).
7. **Homebrew on Linux** (`brew install TotalBassJunkie/stratum/stratum-memory`).
8. **Docker** (`docker run ghcr.io/totalbassjunkie/stratum-memory`).

### Windows
1. **winget** (`winget install TotalBassJunkie.StratumMemory`).
2. **Scoop** (`scoop install stratum-memory`).
3. **npm** (`npm install -g stratum-memory`).
4. **PowerShell** (`irm https://get.stratum.dev/install.ps1 | iex`).
5. **MSI** (`msiexec /i Stratum-*.msi`) and **EXE** installer.
6. **pipx / pip** (`pipx install stratum-memory`).
7. **Chocolatey** (`choco install stratum-memory`).
8. **Docker Desktop** (same image).

### macOS
1. **Homebrew** (`brew install TotalBassJunkie/stratum/stratum-memory`).
2. **curl | bash** (same `install.sh`).
3. **.pkg** installer (double-click) and **.dmg** (drag to Applications).
4. **pipx / pip** (`pipx install stratum-memory`).
5. **npm** (`npm install -g stratum-memory`).
6. **Docker** (same image).

## The `stratum` CLI (what every channel installs)

```
stratum install      # bootstrap a local stack (writes compose + .env, pulls image)
stratum up           # start the stack (docker compose up -d)
stratum down         # stop the stack
stratum status       # container + health summary
stratum logs [svc]   # tail logs
stratum mcp          # run the MCP server (stdio / streamable-http)
stratum health       # readiness probe (JSON)
stratum version      # print the version
stratum doctor       # environment diagnostics (python, docker, ports)
```

`install`, `up`, `down`, `status`, `logs` shell out to `docker compose`; they
require Docker but not a local Postgres. `mcp`/`health` work against any
configured `STRATUM_DB_*` (defaults to the stack).

## What needs a one-time human step (documented, not code)

- **PyPI**: trusted publishing (OIDC) or an API token as repo secret `PYPI_TOKEN`.
- **npm**: `NPM_TOKEN` repo secret.
- **Homebrew tap**: the `homebrew-stratum` repo must exist; the workflow pushes
  the formula there (needs `TAP_GITHUB_TOKEN` with `repo`).
- **Scoop bucket**: the `scoop-stratum` repo must exist (same token).
- **winget**: `wingetcreate` opens a PR against `microsoft/winget-pkgs`
  (`WINGET_TOKEN` with `public_repo`).
- **Snap**: a snapcraft.io account + `SNAPCRAFT_STORE_CREDENTIALS`.
- **get.stratum.dev**: a CNAME to the GitHub raw `install.sh`/`install.ps1`
  (optional; the raw GitHub URLs always work).

All GitHub-native channels (Releases, GHCR container, `install.sh`/`install.ps1`
from raw) work with the default `GITHUB_TOKEN` and need no extra secret.
