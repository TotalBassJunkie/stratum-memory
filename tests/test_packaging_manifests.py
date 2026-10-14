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
#: `choco pack` copies only what the nuspec matches, so the script must sit in
#: the `tools/` directory the nuspec globs - not next to the nuspec.
CHOCO_INSTALL = PACKAGING_DIR / "chocolatey" / "tools" / "chocolateyinstall.ps1"
CHOCO_FLAT_INSTALL = PACKAGING_DIR / "chocolatey" / "chocolateyinstall.ps1"
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


@needs_yaml
def test_winget_installer_does_not_claim_the_rewriter_fills_productcode() -> None:
    """The comment in the manifest must attribute ProductCode to wingetcreate."""
    text = _text(WINGET_INSTALLER)
    assert "ProductCode" in text
    collapsed = " ".join(text.split())
    for match in re.finditer("ProductCode", collapsed):
        window = collapsed[max(0, match.start() - 250) : match.start() + 150]
        assert "wingetcreate" in window or "does not touch" in window, (
            f"the manifest credits the release workflow with ProductCode: {window!r}"
        )


@needs_yaml
def test_winget_locale_release_notes_point_at_the_current_release() -> None:
    """A stale ReleaseNotesUrl would advertise the wrong release for ever."""
    data = _load_yaml(WINGET_LOCALE)
    assert data["ReleaseNotesUrl"] == f"{GH_REPO_URL}/releases/tag/{RELEASE_TAG}", (
        "ReleaseNotesUrl must point at the tag of the current __version__"
    )


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


def test_chocolatey_nuspec_file_glob_matches_the_real_layout() -> None:
    """`choco pack` only copies what `<file src=...>` matches.

    A `.nuspec` that globs `tools\\**` while the script sits flat packs a
    package with no install script at all.
    """
    root = ET.fromstring(_text(CHOCO_NUSPEC))
    files = root.find("n:files", NUSPEC_NS)
    assert files is not None, "the nuspec must declare a <files> section"
    sources = [element.get("src") for element in files]
    assert sources, "the nuspec must declare at least one <file>"
    assert any(source and source.replace("\\", "/").startswith("tools/") for source in sources), (
        f"the nuspec must pack tools/**, got {sources}"
    )
    # ...and the script really lives where that glob looks for it.
    assert CHOCO_INSTALL.is_file(), f"missing install script: {CHOCO_INSTALL}"
    assert not CHOCO_FLAT_INSTALL.is_file(), (
        "chocolateyinstall.ps1 must live in tools/, not next to the nuspec"
    )
    for source in sources:
        assert source, "an empty <file src> matches nothing"
        assert "*" not in source.replace("**", ""), f"unexpected glob: {source}"


def test_chocolatey_tools_dir_holds_the_install_script() -> None:
    tools = CHOCO_INSTALL.parent
    assert tools.name == "tools"
    assert sorted(path.name for path in tools.iterdir()) == ["chocolateyinstall.ps1"]


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
    # The nuspec globs tools/, so the README must name the real path.
    assert "chocolatey/tools/chocolateyinstall.ps1" in text


def test_packaging_readme_documents_the_winget_first_submission() -> None:
    """`wingetcreate update` fails until the package exists in winget-pkgs."""
    text = _text(PACKAGING_README)
    assert "wingetcreate new" in text, "the one-time first submission must be documented"
    assert "once" in text.lower(), "it must be marked as a one-time step"


def test_packaging_readme_does_not_claim_productcode_is_rewritten() -> None:
    """`wingetcreate` fills ProductCode in; the rewriter must not be credited."""
    text = _text(PACKAGING_README)
    assert "ProductCode" in text, "the ProductCode caveat must be stated"
    # The claim wraps across lines, so check the window after each mention. Every
    # mention must attribute it to wingetcreate; the old wording listed
    # ProductCode as one of the rewriter's fields.
    collapsed = " ".join(text.split())
    for match in re.finditer("ProductCode", collapsed):
        window = collapsed[match.start() : match.start() + 200]
        assert "wingetcreate" in window, (
            f"the README must attribute ProductCode to wingetcreate: {window!r}"
        )
    # The rewriter's own docstring must not credit itself either.
    rewriter = (REPO_ROOT / ".github" / "workflows" / "scripts" / "update-manifests.py").read_text(
        encoding="utf-8"
    )
    assert "ProductCode" in rewriter, "the rewriter must explain why it skips ProductCode"
    collapsed_script = " ".join(rewriter.split())
    for match in re.finditer("ProductCode", collapsed_script):
        window = collapsed_script[match.start() : match.start() + 200]
        assert "wingetcreate" in window or "not" in window, (
            f"the rewriter docstring claims it writes ProductCode: {window!r}"
        )


def test_packaging_readme_explains_the_manifest_handoff() -> None:
    """The rewritten tree must reach the publish jobs (see test_workflows.py)."""
    text = _text(PACKAGING_README)
    assert "packaging-manifests" in text, (
        "the README must explain that the publish jobs consume the rewritten artifact"
    )


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
