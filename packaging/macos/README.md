# Stratum-Memory — macOS installers

Sources for the two macOS release assets built by the CI on a `macos-latest`
runner (see [`docs/distribution.md`](../../docs/distribution.md) for the naming
contract):

| Asset | Built by |
|---|---|
| `Stratum-<version>-x86_64.pkg` | `pkgbuild` + `productbuild` (`build-pkg.sh`) |
| `Stratum-<version>-x86_64.dmg` | `hdiutil` (`build-dmg.sh`) |

The version is **never** hardcoded here: the CI reads it from
`src/stratum/__init__.py` (`__version__`) and exports `VERSION`, which the
scripts require (`VERSION="${VERSION:?...}"`).

## Build (CI, macOS only)

```sh
VERSION=0.1.0 sh packaging/macos/build-pkg.sh   # -> dist/Stratum-0.1.0-x86_64.pkg
VERSION=0.1.0 sh packaging/macos/build-dmg.sh   # -> dist/Stratum-0.1.0-x86_64.dmg
```

`build-pkg.sh` installs the CLI and its dependencies into
`/usr/local/lib/stratum` (via `pip install --target`, falling back to the source
tree), ships a launcher at `/usr/local/lib/stratum/bin/stratum`, and links it as
`/usr/local/bin/stratum` in `scripts/postinstall`. `build-dmg.sh` puts that
`.pkg` on a volume named `Stratum-Memory` next to an `Applications` symlink.

## Installation channels (macOS)

1. **`.pkg` — double-click.** Run `Stratum-<version>-x86_64.pkg`; the installer
   places `stratum` on your `PATH`. Non-interactive:
   `sudo installer -pkg Stratum-<version>-x86_64.pkg -target /`.
2. **`.dmg` — double-click.** Open `Stratum-<version>-x86_64.dmg`, then run the
   `.pkg` inside it (or drag the package out first).
3. **Homebrew:**
   `brew install TotalBassJunkie/stratum/stratum-memory`.
4. **curl | bash:**
   `curl -fsSL https://get.stratum.dev/install.sh | bash`.
5. **pipx:** `pipx install stratum-memory`.
6. **npm:** `npm i -g stratum-memory`.
7. **Docker:** `docker run ghcr.io/totalbassjunkie/stratum-memory`.

The `.pkg`/`.dmg` and `pipx`/`npm` channels all install the same `stratum` CLI.
The Docker stack (Postgres + pgvector, Caddy, worker) is **not** installed by any
of them — run `stratum install` afterwards to bootstrap it.

## Requirements

- macOS on Intel (x86_64) or Apple silicon (arm64); the distribution declares
  `hostArchitectures="x86_64,arm64"`.
- Python 3.12 or newer at runtime (`brew install python@3.12`). `preinstall`
  warns (but does not fail) when it is missing.
- Docker Desktop — only for `stratum install` / `stratum up`.

## Layout

```
packaging/macos/
  build-pkg.sh            # pkgbuild + productbuild -> Stratum-<version>-x86_64.pkg
  build-dmg.sh            # hdiutil -> Stratum-<version>-x86_64.dmg
  distribution.xml        # productbuild distribution (__VERSION__/__IDENTIFIER__ placeholders)
  stratum-launcher.sh     # POSIX-sh launcher installed as /usr/local/bin/stratum
  resources/
    welcome.html          # shown by Installer.app (welcome page)
    readme.html           # shown by Installer.app (read-me page)
  scripts/
    preinstall            # POSIX sh: writable volume + Python >= 3.12 check
    postinstall           # POSIX sh: link /usr/local/bin/stratum, verify offline
```

## Tests

`tests/test_macos_packaging.py` checks the sources without macOS tooling
(`pkgbuild`/`productbuild`/`hdiutil` are not required): file presence, that
`distribution.xml` parses as XML, `sh -n` syntax of the shell scripts, that the
contract file names and the expected tools are referenced, and that no secrets
leak in.

```sh
python -m pytest tests/test_macos_packaging.py -q
```
