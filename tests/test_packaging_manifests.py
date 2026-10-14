"""Tests for the native package-manager manifests (``packaging/``).

``docs/distribution.md`` is the contract: every channel resolves to the same
artifacts built by GitHub Actions on a ``v<version>`` tag and attached to the
GitHub Release. The manifests in ``packaging/`` are the per-channel front doors
to those artifacts, so their identity (package id, version, locale, URLs,
checksums) is pinned here as executable expectations.

The suite is deliberately **database-free, network-free and side-effect-free**:
it only reads the shipped files, parses them (JSON/YAML/XML/Ruby) and
pattern-matches them. YAML parsing needs PyYAML (in ``[project.optional-
dependencies].dev``) and the Ruby syntax check needs a ``ruby`` on ``PATH``;
each is skipped - never failed - when the tool is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from stratum import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = REPO_ROOT / "packaging"

HOMEBREW_FORMULA = PACKAGING_DIR / "homebrew" / "stratum-memory.rb"
SCOOP_MANIFEST = PACKAGING_DIR / "scoop" / "stratum-memory.json"
WINGET_VERSION = PACKAGING_DIR / "winget" / "TotalBassJunkie.StratumMemory.yaml"
WINGET_INSTALLER = PACKAGING_DIR / "winget" / "TotalBassJunkie.StratumMemory.installer.yaml"
WINGET_LOCALE = PACKAGING_DIR / "winget" / "TotalBassJunkie.StratumMemory.locale.en-US.yaml"
CHOCO_NUSPEC = PACKAGING_DIR / "chocolatey" / "stratum-memory.nuspec"
CHOCO_INSTALL = PACKAGING_DIR / "chocolatey" / "chocolateyinstall.ps1"
PACKAGING_README = PACKAGING_DIR / "README.md"

#: Every file this suite owns (see the task's Dateihoheit).
EXPECTED_FILES = (
    HOMEBREW_FORMULA,
    SCOOP_MANIFEST,
    WINGET_VERSION,
    WINGET_INSTALLER,
    WINGET_LOCALE,
    CHOCO_NUSPEC,
    CHOCO_INSTALL,
    PACKAGING_README,
)

WINGET_ID = "TotalBassJunkie.StratumMemory"
WINGET_LOCALE_ID = "en-US"
MANIFEST_VERSION = "1.6.0"
GH_REPO_URL = "https://github.com/TotalBassJunkie/stratum-memory"
RELEASE_TAG = f"v{__version__}"
NUSPEC_NS = {"n": "http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd"}

#: Marker the release workflow replaces the placeholders in place.
CI_MARKER = "# updated by .github/workflows/release.yml"
#: The all-zero checksum/digest placeholder CI overwrites with a real hash.
PLACEHOLDER_SHA256 = "0" * 64

try:  # PyYAML is a dev extra; the YAML tests skip when it is absent.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    yaml = None  # type: ignore[assignment]

needs_yaml = pytest.mark.skipif(yaml is None, reason="PyYAML is not installed")
needs_ruby = pytest.mark.skipif(shutil.which("ruby") is None, reason="no ruby on PATH")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path) -> dict:
    assert yaml is not None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} must parse to a mapping"
    return data


# ============================================================== file presence


@pytest.mark.parametrize("path", EXPECTED_FILES, ids=lambda p: p.name)
def test_manifest_exists_and_is_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing manifest: {path}"
    assert path.stat().st_size > 0, f"empty manifest: {path}"


# ==================================================================== homebrew


def test_homebrew_formula_structure() -> None:
    text = _text(HOMEBREW_FORMULA)
    assert "class StratumMemory < Formula" in text
    assert "def install" in text
    assert "test do" in text
    # `test do` runs the CLI's version command through the installed bin shim.
    assert 'system bin/"stratum", "version"' in text
    assert re.search(r"^end\s*$", text, flags=re.MULTILINE), "formula must close with `end`"


def test_homebrew_formula_metadata() -> None:
    text = _text(HOMEBREW_FORMULA)
    assert re.search(r'^\s*desc ".+"$', text, flags=re.MULTILINE)
    assert 'homepage "https://github.com/TotalBassJunkie/stratum-memory"' in text
    assert 'license "Apache-2.0"' in text
    assert 'depends_on "python@3.12"' in text


def test_homebrew_formula_prefers_the_release_wheel() -> None:
    text = _text(HOMEBREW_FORMULA)
    assert f"stratum-{__version__}-py3-none-any.whl" in text
    assert GH_REPO_URL + f"/releases/download/{RELEASE_TAG}/" in text
    assert re.search(r'^\s*sha256 "[0-9a-f]{64}"$', text, flags=re.MULTILINE)


def test_homebrew_formula_has_the_ci_placeholder_marker() -> None:
    assert CI_MARKER in _text(HOMEBREW_FORMULA)


@needs_ruby
def test_homebrew_formula_is_valid_ruby() -> None:
    result = subprocess.run(
        ["ruby", "-c", str(HOMEBREW_FORMULA)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ======================================================================= scoop


def test_scoop_manifest_is_valid_json() -> None:
    data = json.loads(_text(SCOOP_MANIFEST))
    assert isinstance(data, dict)


def test_scoop_manifest_metadata() -> None:
    data = json.loads(_text(SCOOP_MANIFEST))
    assert data["version"] == __version__
    assert data["description"]
    assert data["homepage"] == GH_REPO_URL
    assert data["license"] == "Apache-2.0"


def test_scoop_manifest_points_at_the_release_installer() -> None:
    data = json.loads(_text(SCOOP_MANIFEST))
    url = data["architecture"]["64bit"]["url"]
    assert url.startswith(f"{GH_REPO_URL}/releases/download/{RELEASE_TAG}/")
    assert url.endswith(f"Stratum-{__version__}-setup.exe")
    assert re.fullmatch(r"[0-9a-f]{64}", data["architecture"]["64bit"]["hash"])


def test_scoop_manifest_exposes_the_cli() -> None:
    data = json.loads(_text(SCOOP_MANIFEST))
    assert data["bin"], "scoop manifest must add the CLI to PATH"
    assert any("stratum" in entry for entry in data["bin"])


def test_scoop_manifest_tracks_releases() -> None:
    data = json.loads(_text(SCOOP_MANIFEST))
    assert "github" in data["checkver"]
    assert GH_REPO_URL in data["checkver"]["github"]
    autoupdate_url = data["autoupdate"]["architecture"]["64bit"]["url"]
    assert "$version" in autoupdate_url
    assert autoupdate_url.startswith(f"{GH_REPO_URL}/releases/download/v$version/")


# ====================================================================== winget


@needs_yaml
def test_winget_manifests_parse() -> None:
    for path in (WINGET_VERSION, WINGET_INSTALLER, WINGET_LOCALE):
        assert _load_yaml(path)["ManifestVersion"] == MANIFEST_VERSION


@needs_yaml
def test_winget_identifier_and_version_are_consistent() -> None:
    for path in (WINGET_VERSION, WINGET_INSTALLER, WINGET_LOCALE):
        data = _load_yaml(path)
        assert data["PackageIdentifier"] == WINGET_ID, path.name
        assert data["PackageVersion"] == __version__, path.name


@needs_yaml
def test_winget_locale_is_consistent() -> None:
    version = _load_yaml(WINGET_VERSION)
    locale = _load_yaml(WINGET_LOCALE)
    assert version["DefaultLocale"] == WINGET_LOCALE_ID
    assert locale["PackageLocale"] == WINGET_LOCALE_ID


@needs_yaml
def test_winget_manifest_types_are_distinct_and_complete() -> None:
    types = {
        _load_yaml(path)["ManifestType"]
        for path in (WINGET_VERSION, WINGET_INSTALLER, WINGET_LOCALE)
    }
    assert types == {"version", "installer", "defaultLocale"}


@needs_yaml
def test_winget_installer_is_the_release_msi() -> None:
    data = _load_yaml(WINGET_INSTALLER)
    assert data["InstallerType"] == "msi"
    assert data["Scope"] == "machine"
    assert data["Installers"], "at least one installer entry is required"
    for entry in data["Installers"]:
        assert entry["Architecture"] == "x64"
        url = entry["InstallerUrl"]
        assert url.startswith(f"{GH_REPO_URL}/releases/download/{RELEASE_TAG}/")
        assert url.endswith(f"Stratum-{__version__}-x64.msi")
        assert re.fullmatch(r"[0-9a-f]{64}", entry["InstallerSha256"])
    assert data["ProductCode"].startswith("{") and data["ProductCode"].endswith("}")


@needs_yaml
def test_winget_locale_has_display_metadata() -> None:
    data = _load_yaml(WINGET_LOCALE)
    for key in ("Publisher", "PackageName", "PackageUrl", "License", "ShortDescription"):
        assert data.get(key), f"winget locale is missing {key}"
    assert data["PackageUrl"] == GH_REPO_URL
    assert data["Tags"], "winget locale must carry search tags"
    assert all(isinstance(tag, str) and tag for tag in data["Tags"])


def test_winget_installer_has_the_ci_placeholder_marker() -> None:
    assert CI_MARKER in _text(WINGET_INSTALLER)


# ================================================================== chocolatey


def test_chocolatey_nuspec_is_valid_xml() -> None:
    root = ET.fromstring(_text(CHOCO_NUSPEC))
    assert root.tag.endswith("package")
    metadata = root.find("n:metadata", NUSPEC_NS)
    assert metadata is not None, "nuspec must have a <metadata> element"
    assert metadata.findtext("n:id", namespaces=NUSPEC_NS) == "stratum-memory"
    assert metadata.findtext("n:version", namespaces=NUSPEC_NS) == __version__
    assert metadata.findtext("n:title", namespaces=NUSPEC_NS) == "Stratum-Memory"
    assert metadata.findtext("n:licenseUrl", namespaces=NUSPEC_NS)
    assert metadata.findtext("n:projectUrl", namespaces=NUSPEC_NS) == GH_REPO_URL


def test_chocolatey_install_script_installs_the_msi_silently() -> None:
    text = _text(CHOCO_INSTALL)
    assert "$ErrorActionPreference = 'Stop'" in text
    assert "fileType" in text and "'msi'" in text
    assert "Install-ChocolateyPackage" in text
    assert "/qn /norestart" in text
    assert "checksumType64" in text and "sha256" in text


def test_chocolatey_install_script_points_at_the_release_msi() -> None:
    text = _text(CHOCO_INSTALL)
    assert f"{GH_REPO_URL}/releases/download/{RELEASE_TAG}/Stratum-{__version__}-x64.msi" in text
    assert re.search(r"checksum64\s*=\s*'[0-9a-f]{64}'", text)


def test_chocolatey_install_script_has_the_ci_placeholder_marker() -> None:
    assert CI_MARKER in _text(CHOCO_INSTALL)


# =============================================================== cross-cutting


@pytest.mark.parametrize(
    "path",
    (SCOOP_MANIFEST, WINGET_INSTALLER, WINGET_LOCALE, CHOCO_NUSPEC, CHOCO_INSTALL),
    ids=lambda p: p.name,
)
def test_manifests_carry_the_current_version(path: Path) -> None:
    text = _text(path)
    assert __version__ in text
    # A stale, hard-coded older release would contradict the version source
    # (`src/stratum/__init__.py`); every release URL must use the current tag.
    for stale in re.findall(r"releases/download/v([0-9][^/\"'\s]*)", text):
        assert stale == __version__, f"{path.name} references stale tag v{stale}"


def test_packaging_readme_documents_every_channel() -> None:
    text = _text(PACKAGING_README)
    assert "brew tap TotalBassJunkie/stratum" in text
    assert "brew install stratum-memory" in text
    assert "scoop bucket add" in text
    assert "scoop install stratum-memory" in text
    assert f"winget install {WINGET_ID}" in text
    assert "choco install stratum-memory" in text
    # The CI-replaces-the-placeholders caveat must be stated for the user.
    assert "placeholder" in text.lower()
    assert ".github/workflows/release.yml" in text


def test_no_secrets_in_manifests() -> None:
    patterns = (
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+=_-]{12,}"
        ),
    )
    for path in EXPECTED_FILES:
        text = _text(path)
        for pattern in patterns:
            match = pattern.search(text)
            assert match is None, f"{path.name} looks like it contains a secret: {match.group(0)!r}"


def test_checksum_placeholders_are_zeroed() -> None:
    """The committed digests must be the CI placeholder, never a real hash."""
    for path in (HOMEBREW_FORMULA, WINGET_INSTALLER, CHOCO_INSTALL):
        digests = re.findall(r"\b[0-9a-f]{64}\b", _text(path))
        assert digests, f"{path.name} is missing its checksum placeholder"
        assert all(digest == PLACEHOLDER_SHA256 for digest in digests), path.name
