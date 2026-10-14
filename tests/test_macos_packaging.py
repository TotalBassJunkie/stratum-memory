"""Tests for the macOS installer sources (``packaging/macos/``).

The CI builds the two macOS release assets on a ``macos-latest`` runner:

* ``Stratum-<version>-x86_64.pkg`` — ``pkgbuild`` + ``productbuild``
* ``Stratum-<version>-x86_64.dmg`` — ``hdiutil``

The contract (asset names, channels) lives in ``docs/distribution.md``; these
tests pin it as executable expectations **without** macOS tooling and without a
database or network:

* every source file exists and is non-empty,
* ``distribution.xml`` parses as XML (``xml.etree.ElementTree``),
* the shell scripts pass ``bash -n`` (skipped when no working bash is on PATH),
* the contract file names and the expected tools (``pkgbuild``, ``productbuild``,
  ``hdiutil``) are referenced,
* the version is never hardcoded (only the ``${VERSION}`` placeholder), and
* no secrets leak into the sources.

The suite is deliberately static: ``pkgbuild`` / ``productbuild`` / ``hdiutil``
are *not* required, so it runs on Linux and Windows too.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS = REPO_ROOT / "packaging" / "macos"

BUILD_PKG = MACOS / "build-pkg.sh"
BUILD_DMG = MACOS / "build-dmg.sh"
DISTRIBUTION = MACOS / "distribution.xml"
PREINSTALL = MACOS / "scripts" / "preinstall"
POSTINSTALL = MACOS / "scripts" / "postinstall"
LAUNCHER = MACOS / "stratum-launcher.sh"
README = MACOS / "README.md"
WELCOME = MACOS / "resources" / "welcome.html"
README_HTML = MACOS / "resources" / "readme.html"

ALL_FILES = (
    BUILD_PKG,
    BUILD_DMG,
    DISTRIBUTION,
    PREINSTALL,
    POSTINSTALL,
    LAUNCHER,
    README,
    WELCOME,
    README_HTML,
)

SHELL_SCRIPTS = (BUILD_PKG, BUILD_DMG, PREINSTALL, POSTINSTALL, LAUNCHER)

#: The two macOS asset names from the docs/distribution.md naming contract.
PKG_ASSET = "Stratum-${VERSION}-x86_64.pkg"
DMG_ASSET = "Stratum-${VERSION}-x86_64.dmg"


# --------------------------------------------------------------- bash discovery


def _find_working_bash() -> str | None:
    """Return a bash that actually executes, or ``None`` (mirrors test_deploy_pi)."""
    candidates: list[str] = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    candidates += [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        executable = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if not executable:
            continue
        try:
            probe = subprocess.run(
                [executable, "-c", "echo ok"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return executable
    return None


BASH = _find_working_bash()

TEXTS = {path: path.read_text(encoding="utf-8") for path in ALL_FILES}


# ============================================================== file presence


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_artifact_exists_and_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing artifact: {path}"
    assert path.stat().st_size > 0, f"empty artifact: {path}"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_artifacts_use_lf_line_endings(path: Path) -> None:
    data = path.read_bytes()
    assert b"\r\n" not in data, f"{path.name} must use LF line endings"


# ================================================================ distribution


def test_distribution_xml_parses() -> None:
    root = ET.parse(DISTRIBUTION).getroot()
    assert root.tag == "installer-gui-script", f"unexpected root: {root.tag!r}"


def test_distribution_has_contract_metadata() -> None:
    root = ET.parse(DISTRIBUTION).getroot()

    title = root.findtext("title")
    assert title == "Stratum-Memory", f"unexpected title: {title!r}"

    options = root.find("options")
    assert options is not None, "missing <options>"
    assert options.get("hostArchitectures") == "x86_64,arm64"
    assert options.get("rootVolumeOnly") == "true"

    # The component package is referenced by the identifier/version placeholders
    # (substituted by build-pkg.sh), never by a hardcoded version.
    pkg_refs = root.findall("pkg-ref")
    assert pkg_refs, "no <pkg-ref>"
    assert any(ref.get("id") == "__IDENTIFIER__" for ref in pkg_refs)
    assert any(ref.get("version") == "__VERSION__" for ref in pkg_refs)


def test_distribution_wires_welcome_and_readme() -> None:
    root = ET.parse(DISTRIBUTION).getroot()
    assert root.find("welcome") is not None, "missing <welcome>"
    assert root.find("readme") is not None, "missing <readme>"
    assert root.find("choices-outline") is not None, "missing <choices-outline>"

    welcome = root.find("welcome")
    readme = root.find("readme")
    assert welcome is not None and readme is not None
    assert (MACOS / "resources" / welcome.get("file", "")).is_file()
    assert (MACOS / "resources" / readme.get("file", "")).is_file()


# =============================================================== shell scripts


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_are_syntactically_valid(path: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"sh -n {path.name} failed:\n{result.stderr}"


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_use_strict_mode(path: Path) -> None:
    assert "set -eu" in TEXTS[path], f"{path.name} must use 'set -eu'"


@pytest.mark.parametrize("path", (PREINSTALL, POSTINSTALL), ids=lambda p: p.name)
def test_pkg_scripts_are_posix_sh(path: Path) -> None:
    assert TEXTS[path].startswith("#!/bin/sh\n"), f"{path.name} must be a POSIX sh script"


# ======================================================= contract & tool usage


def test_build_pkg_names_and_tools() -> None:
    text = TEXTS[BUILD_PKG]
    assert PKG_ASSET in text, "build-pkg.sh must derive the contract .pkg name"
    assert "pkgbuild" in text, "build-pkg.sh must call pkgbuild"
    assert "productbuild" in text, "build-pkg.sh must call productbuild"
    assert "distribution.xml" in text, "build-pkg.sh must use distribution.xml"
    # Payload + launcher locations required by the ticket.
    assert "/usr/local/lib/stratum" in text
    assert "/usr/local/bin/stratum" in text
    assert "scripts" in text, "build-pkg.sh must pass the pre/postinstall scripts"
    assert "stratum-launcher.sh" in text, "build-pkg.sh must install the launcher"


def test_build_dmg_names_and_tools() -> None:
    text = TEXTS[BUILD_DMG]
    assert DMG_ASSET in text, "build-dmg.sh must derive the contract .dmg name"
    assert PKG_ASSET in text, "build-dmg.sh must embed the contract .pkg name"
    assert "hdiutil" in text, "build-dmg.sh must call hdiutil"
    assert "hdiutil create" in text
    assert "Stratum-Memory" in text, "the volume must be named Stratum-Memory"
    assert "/Applications" in text, "the DMG must contain an Applications symlink"


def test_preinstall_checks_the_launcher() -> None:
    text = TEXTS[PREINSTALL]
    assert "/usr/local/bin/stratum" in text
    assert "3.12" in text, "preinstall must check the Python requirement"


def test_postinstall_links_the_launcher() -> None:
    text = TEXTS[POSTINSTALL]
    assert "/usr/local/bin/stratum" in text
    assert "/usr/local/lib/stratum/bin/stratum" in text
    assert "ln -s" in text, "postinstall must symlink the launcher"


def test_readme_documents_every_macos_channel() -> None:
    text = TEXTS[README]
    for needle in (
        "Stratum-<version>-x86_64.pkg",
        "Stratum-<version>-x86_64.dmg",
        "brew install TotalBassJunkie/stratum/stratum-memory",
        "curl -fsSL https://get.stratum.dev/install.sh | bash",
        "pipx install stratum-memory",
        "npm i -g stratum-memory",
    ):
        assert needle in text, f"README.md does not document: {needle}"


# ==================================================================== safety


def test_version_is_never_hardcoded() -> None:
    for path in (BUILD_PKG, BUILD_DMG):
        text = TEXTS[path]
        assert "${VERSION}" in text, f"{path.name} must use the ${{VERSION}} placeholder"
        # No literal version string may be baked into *executable* code (comments
        # legitimately show `VERSION=0.1.0` usage examples).
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert not re.search(r"\b\d+\.\d+\.\d+\b", code), (
            f"{path.name} appears to hardcode a version"
        )
    # distribution.xml carries only placeholders, substituted by build-pkg.sh.
    assert "__VERSION__" in TEXTS[DISTRIBUTION]
    assert not re.search(r"\b\d+\.\d+\.\d+\b", TEXTS[DISTRIBUTION])


#: Obvious secret shapes. The sources are public and must stay clean.
_SECRET_PATTERNS = (
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{8,}"),
    re.compile(r"(?i)aws_(access_key_id|secret_access_key)"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_secrets_in_sources(path: Path) -> None:
    text = TEXTS[path]
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"{path.name} looks like it contains a secret: {match.group(0)!r}"
