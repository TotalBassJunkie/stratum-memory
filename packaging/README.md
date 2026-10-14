# Packaging manifests

Native package-manager manifests for Stratum-Memory, one directory per channel.
`docs/distribution.md` is the contract: every channel resolves to the same
artifacts built by GitHub Actions on a `v<version>` tag and attached to the
GitHub Release.

## Channels and install commands

| Channel | Manifest | Install |
|---|---|---|
| Homebrew (macOS / Linux) | `homebrew/stratum-memory.rb` | `brew tap TotalBassJunkie/stratum` then `brew install stratum-memory` |
| Scoop (Windows) | `scoop/stratum-memory.json` | `scoop bucket add stratum https://github.com/TotalBassJunkie/scoop-stratum` then `scoop install stratum-memory` |
| winget (Windows) | `winget/TotalBassJunkie.StratumMemory*.yaml` | `winget install TotalBassJunkie.StratumMemory` |
| Chocolatey (Windows) | `chocolatey/stratum-memory.nuspec` + `chocolatey/chocolateyinstall.ps1` | `choco install stratum-memory` |

The Homebrew formula installs the prebuilt pure-Python wheel from the release
(no compiler, no source build) and depends on `python@3.12`; its `test do` block
runs `stratum version`. Scoop, winget and Chocolatey all install the Windows
build: Scoop and winget use the WiX MSI/EXE from the release, Chocolatey runs
the MSI silently with `/qn /norestart`.

## Placeholders — replaced by CI

The version, download URLs and checksums in these files are **placeholders**.
`.github/workflows/release.yml` rewrites them on every tag:

- Homebrew: `url` + `sha256` (the wheel) — marked `# updated by .github/workflows/release.yml`.
- Scoop: `version`, `architecture.64bit.url`, `architecture.64bit.hash` — the
  `checkver`/`autoupdate` blocks let `scoop update` track new release tags.
- winget: `PackageVersion` in all three manifests, `InstallerUrl` and
  `InstallerSha256` (the MSI), and `ProductCode`.
- Chocolatey: `version` (nuspec), `url64bit` + `checksum64` (install script).

Do not hand-edit the placeholders in a tap/bucket checkout; edit the manifest
here and let the release workflow publish it.

## Human one-time steps

`docs/distribution.md` lists the required repos and tokens: the
`homebrew-stratum` and `scoop-stratum` repositories (`TAP_GITHUB_TOKEN` with
`repo`), and `WINGET_TOKEN` with `public_repo` for the `wingetcreate` PR against
`microsoft/winget-pkgs`. The Chocolatey package is published to the community
feed from the same MSI.

## Tests

`tests/test_packaging_manifests.py` pins the contract statically — it is
database-free and network-free, parses every JSON/YAML file, checks the winget
identifier/version/locale consistency, and asserts the version matches
`stratum.__version__`.
