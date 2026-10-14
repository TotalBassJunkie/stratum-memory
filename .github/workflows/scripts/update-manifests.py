#!/usr/bin/env python3
"""Rewrite the packaging-manifest placeholders for a release.

Called by ``.github/workflows/release.yml`` in the ``release`` job, after every
installer has been built and ``dist/SHA256SUMS`` exists. It is the single place
that turns the committed placeholders (version, download URL, SHA256) into the
real values of the tag being released:

* ``packaging/homebrew/stratum-memory.rb`` - the wheel ``url`` + ``sha256``
  (marked ``# updated by .github/workflows/release.yml``),
* ``packaging/scoop/stratum-memory.json`` - ``version``, the installer ``url``
  and its ``hash`` (the setup.exe),
* ``packaging/winget/TotalBassJunkie.StratumMemory*.yaml`` - ``PackageVersion``
  in all three manifests, plus ``InstallerUrl``/``InstallerSha256``/``ProductCode``,
* ``packaging/chocolatey/stratum-memory.nuspec`` - ``version`` and
  ``releaseNotes``, and ``chocolateyinstall.ps1`` - ``url64bit`` + ``checksum64``.

The digest is read from ``dist/SHA256SUMS`` (the file the release job just
generated and attached to the release), so every manifest points at exactly the
byte-for-byte artifact that was published.

Inputs (environment, all required):

* ``VERSION``              - the version, without the leading ``v``
* ``GITHUB_REPOSITORY``    - ``owner/name``, used to build the download URLs
* ``DIST_DIR``             - where the assets + ``SHA256SUMS`` live (default ``dist``)
* ``PACKAGING_DIR``        - the manifest tree (default ``packaging``)

Only the standard library is used, so the job needs no extra dependency.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

#: The winget package id (docs/distribution.md).
WINGET_ID = "TotalBassJunkie.StratumMemory"

#: Where each winget manifest lives, keyed by the file name suffix.
WINGET_MANIFESTS = (
    "TotalBassJunkie.StratumMemory.yaml",
    "TotalBassJunkie.StratumMemory.installer.yaml",
    "TotalBassJunkie.StratumMemory.locale.en-US.yaml",
)


def fail(message: str) -> None:
    sys.exit(f"update-manifests: {message}")


def load_checksums(dist: pathlib.Path) -> dict[str, str]:
    """Parse ``SHA256SUMS`` into ``{file name: lowercase digest}``."""
    sums = dist / "SHA256SUMS"
    if not sums.is_file():
        fail(f"{sums} is missing; run sha256sum before this script")
    table: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # `sha256sum` writes "<64 hex><two spaces><name>"; be lenient about the
        # separator and about a leading "./" or "*" (binary mode).
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if not match:
            fail(f"unparsable SHA256SUMS line: {line!r}")
        table[match.group(2).lstrip("./")] = match.group(1).lower()
    if not table:
        fail("SHA256SUMS is empty")
    return table


def digest_for(table: dict[str, str], name: str) -> str:
    try:
        return table[name]
    except KeyError:
        fail(f"{name} is not listed in SHA256SUMS (have: {sorted(table)})")


def replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    """Substitute *pattern* exactly once; a miss or a double match is fatal.

    Failing loudly is the point: a silent no-op would publish a manifest that
    still carries the placeholder version/URL/digest.
    """
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        fail(f"expected exactly one {label} match, found {count}")
    return updated


def write(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"update-manifests: rewrote {path}")


def download_url(repo: str, version: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}/{asset}"


def release_url(repo: str, version: str) -> str:
    return f"https://github.com/{repo}/releases/tag/v{version}"


# --------------------------------------------------------------------------- #
# Per-channel rewriters
# --------------------------------------------------------------------------- #


def update_homebrew(
    path: pathlib.Path, *, repo: str, version: str, wheel: str, sha: str
) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        r'^(\s*url\s+")https://github\.com/[^"]+(")',
        lambda m: f"{m.group(1)}{download_url(repo, version, wheel)}{m.group(2)}",
        "homebrew url",
    )
    text = replace_once(
        text,
        r'^(\s*sha256\s+")[0-9a-f]{64}(")',
        lambda m: f"{m.group(1)}{sha}{m.group(2)}",
        "homebrew sha256",
    )
    write(path, text)


def update_scoop(
    path: pathlib.Path, *, repo: str, version: str, setup: str, sha: str
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    data["architecture"]["64bit"]["url"] = download_url(repo, version, setup)
    data["architecture"]["64bit"]["hash"] = sha
    write(path, json.dumps(data, indent=4, ensure_ascii=False) + "\n")


def update_winget(
    directory: pathlib.Path,
    *,
    repo: str,
    version: str,
    msi: str,
    sha: str,
) -> None:
    for name in WINGET_MANIFESTS:
        path = directory / name
        text = path.read_text(encoding="utf-8")
        text = replace_once(
            text,
            r"^(PackageVersion:\s*).+$",
            lambda m: f"{m.group(1)}{version}",
            f"winget PackageVersion in {name}",
        )
        if name.endswith(".installer.yaml"):
            text = replace_once(
                text,
                r"^(\s*InstallerUrl:\s*).+$",
                lambda m: f"{m.group(1)}{download_url(repo, version, msi)}",
                "winget InstallerUrl",
            )
            text = replace_once(
                text,
                r'^(\s*InstallerSha256:\s*")[0-9a-fA-F]{64}(")',
                lambda m: f"{m.group(1)}{sha}{m.group(2)}",
                "winget InstallerSha256",
            )
        write(path, text)


def update_chocolatey(
    directory: pathlib.Path,
    *,
    repo: str,
    version: str,
    msi: str,
    sha: str,
) -> None:
    nuspec = directory / "stratum-memory.nuspec"
    text = nuspec.read_text(encoding="utf-8")
    text = replace_once(
        text,
        r"^(\s*<version>)[^<]*(</version>)$",
        lambda m: f"{m.group(1)}{version}{m.group(2)}",
        "chocolatey nuspec version",
    )
    text = replace_once(
        text,
        r"(<releaseNotes>)[^<]*(</releaseNotes>)",
        lambda m: f"{m.group(1)}{release_url(repo, version)}{m.group(2)}",
        "chocolatey releaseNotes",
    )
    write(nuspec, text)

    install = directory / "chocolateyinstall.ps1"
    text = install.read_text(encoding="utf-8")
    text = replace_once(
        text,
        r"^(\s*url64bit\s*=\s*')[^']*(')",
        lambda m: f"{m.group(1)}{download_url(repo, version, msi)}{m.group(2)}",
        "chocolatey url64bit",
    )
    text = replace_once(
        text,
        r"^(\s*checksum64\s*=\s*')[0-9a-fA-F]*(')",
        lambda m: f"{m.group(1)}{sha}{m.group(2)}",
        "chocolatey checksum64",
    )
    write(install, text)


def main() -> None:
    version = os.environ.get("VERSION", "").strip().lstrip("v")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not version:
        fail("VERSION is not set")
    if "/" not in repo:
        fail(f"GITHUB_REPOSITORY looks wrong: {repo!r}")

    dist = pathlib.Path(os.environ.get("DIST_DIR", "dist"))
    packaging = pathlib.Path(os.environ.get("PACKAGING_DIR", "packaging"))
    table = load_checksums(dist)

    wheel = f"stratum-{version}-py3-none-any.whl"
    setup = f"Stratum-{version}-setup.exe"
    msi = f"Stratum-{version}-x64.msi"

    update_homebrew(
        packaging / "homebrew" / "stratum-memory.rb",
        repo=repo,
        version=version,
        wheel=wheel,
        sha=digest_for(table, wheel),
    )
    update_scoop(
        packaging / "scoop" / "stratum-memory.json",
        repo=repo,
        version=version,
        setup=setup,
        sha=digest_for(table, setup),
    )
    update_winget(
        packaging / "winget",
        repo=repo,
        version=version,
        msi=msi,
        sha=digest_for(table, msi),
    )
    update_chocolatey(
        packaging / "chocolatey",
        repo=repo,
        version=version,
        msi=msi,
        sha=digest_for(table, msi),
    )
    print(f"update-manifests: {WINGET_ID} {version} manifests are ready")


if __name__ == "__main__":
    main()
