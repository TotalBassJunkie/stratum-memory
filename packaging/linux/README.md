# Linux packaging — Stratum-Memory

Sources for the four Linux native channels in `docs/distribution.md`. All of
them are built by GitHub Actions on `ubuntu-latest` on a `v<version>` tag and
attached to the GitHub Release, so every channel resolves to the same published
files:

| Asset | Built by | Source here |
|---|---|---|
| `stratum_<version>_amd64.deb` | nFPM | `nfpm.yaml`, `build-deb-rpm.sh` |
| `stratum-<version>-1.x86_64.rpm` | nFPM | `nfpm.yaml`, `build-deb-rpm.sh` |
| `Stratum-<version>-x86_64.AppImage` | appimagetool | `AppRun`, `stratum.desktop`, `build-appimage.sh` |
| `stratum_<version>_amd64.snap` | snapcraft | `snapcraft.yaml` |

`${VERSION}` is a **placeholder set by the CI** (from `src/stratum/__init__.py`,
the version source named in the contract) and exported into the build scripts.

## Install

### Debian / Ubuntu (`.deb`)

```bash
sudo apt install ./stratum_<version>_amd64.deb
stratum version
```

`apt` resolves the `python3 (>= 3.12)` dependency; the package puts the CLI on
`PATH` as `/usr/bin/stratum` and docs under `/usr/share/doc/stratum-memory/`.
The Python package itself comes from PyPI (`pipx install stratum-memory`), so
the CLI is installed on first use.

### Fedora / RHEL / openSUSE (`.rpm`)

```bash
sudo dnf install ./stratum-<version>-1.x86_64.rpm
stratum version
```

### AppImage (any distribution, no package manager)

```bash
chmod +x Stratum-<version>-x86_64.AppImage
./Stratum-<version>-x86_64.AppImage version
```

The AppImage bundles the launcher; the CLI's Docker stack commands still need a
`docker` binary on the host.

### Snap

```bash
sudo snap install stratum-memory
stratum version
```

Needs the snap published to the store (`SNAPCRAFT_STORE_CREDENTIALS`, see
`docs/distribution.md`). `stratum install`/`up` require the `docker` plug to be
connected:

```bash
sudo snap connect stratum-memory:docker
```

## Building locally

Requires `python -m build`, [nFPM](https://github.com/goreleaser/nfpm/releases)
on `PATH`, and (for the AppImage) `curl`.

```bash
VERSION=0.1.0 bash packaging/linux/build-deb-rpm.sh     # → dist/*.deb, dist/*.rpm
VERSION=0.1.0 bash packaging/linux/build-appimage.sh    # → dist/Stratum-0.1.0-x86_64.AppImage
snapcraft --destructive-mode                            # → *.snap (rename to the contract name)
```

## Next steps after installing the CLI

```bash
stratum install   # write docker-compose.yml + .env into ~/.stratum
stratum up        # start the stack
stratum status    # container + health summary
stratum doctor    # environment diagnostics
```
