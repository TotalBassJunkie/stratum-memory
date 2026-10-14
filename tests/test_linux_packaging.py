"""Tests for the Linux native packaging sources (``packaging/linux/``).

These files are the *source* of the four Linux release assets named in the
``docs/distribution.md`` contract and are built by CI on ``ubuntu-latest``:

* ``stratum_<version>_amd64.deb`` and ``stratum-<version>-1.x86_64.rpm``
  (nFPM: ``nfpm.yaml`` + ``build-deb-rpm.sh``),
* ``Stratum-<version>-x86_64.AppImage``
  (``AppRun`` + ``stratum.desktop`` + ``build-appimage.sh``),
* ``stratum_<version>_amd64.snap`` (``snapcraft.yaml``).

The suite is deliberately **database-free, network-free and side-effect-free**:
nothing is built, downloaded or installed. It pins the parts of the contract
that can be checked statically:

* every declared file exists and is non-empty and LF-only (a stray CR breaks
  ``set -eu`` on Linux and corrupts the packaged payload),
* ``nfpm.yaml`` and ``snapcraft.yaml`` parse as YAML (skipped when PyYAML is not
  importable — see ``pyproject.toml``: it is a *dev* extra, never a runtime dep),
* every shell artefact is syntactically valid under ``bash -n`` / ``sh -n``
  (skipped when no working shell is on PATH),
* the launcher really runs ``python3 -m stratum.cli "$@"``, the way
  ``pyproject.toml`` declares ``stratum = "stratum.cli:main"``,
* ``stratum.desktop`` carries the freedesktop required keys,
* the build scripts produce exactly the contract file names, and
* no packaged file contains a secret.

The repository root is derived from ``__file__`` so the tests run regardless of
the working directory.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LINUX_DIR = REPO_ROOT / "packaging" / "linux"

NFPM_YAML = LINUX_DIR / "nfpm.yaml"
LAUNCHER_SH = LINUX_DIR / "stratum-launcher.sh"
BUILD_DEB_RPM_SH = LINUX_DIR / "build-deb-rpm.sh"
APPRUN = LINUX_DIR / "AppRun"
DESKTOP = LINUX_DIR / "stratum.desktop"
BUILD_APPIMAGE_SH = LINUX_DIR / "build-appimage.sh"
SNAPCRAFT_YAML = LINUX_DIR / "snapcraft.yaml"
README_MD = LINUX_DIR / "README.md"

#: Every file the task contract requires, in a stable order.
ALL_FILES: tuple[Path, ...] = (
    NFPM_YAML,
    LAUNCHER_SH,
    BUILD_DEB_RPM_SH,
    APPRUN,
    DESKTOP,
    BUILD_APPIMAGE_SH,
    SNAPCRAFT_YAML,
    README_MD,
)

#: The exact asset names from ``docs/distribution.md`` (the naming contract),
#: with ``<version>`` standing in for the CI-provided ``${VERSION}``.
DEB_ASSET = "stratum_${VERSION}_amd64.deb"
RPM_ASSET = "stratum-${VERSION}-1.x86_64.rpm"
APPIMAGE_ASSET = "Stratum-${VERSION}-x86_64.AppImage"


# =========================================================== shell discovery


def _find_working_shell(candidates: list[str]) -> str | None:
    """Return the first candidate that actually executes, or ``None``."""
    for candidate in candidates:
        executable = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if not executable:
            continue
        try:
            probe = subprocess.run(
                [executable, "-c", "echo ok"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return executable
    return None


BASH = _find_working_shell(
    [
        *([shutil.which("bash")] if shutil.which("bash") else []),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
)
SH = _find_working_shell(
    [
        *([shutil.which("sh")] if shutil.which("sh") else []),
        r"C:\Program Files\Git\usr\bin\sh.exe",
        "/bin/sh",
        "/usr/bin/sh",
    ]
)

needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
needs_sh = pytest.mark.skipif(SH is None, reason="no working sh on PATH")


def _syntax_check(shell: str, path: Path) -> None:
    result = subprocess.run(
        [shell, "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{path.name} is not valid {Path(shell).name} syntax:\n{result.stdout}{result.stderr}"
    )


# ============================================================ file presence


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_packaging_file_exists_and_is_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing packaging file: {path}"
    assert path.stat().st_size > 0, f"empty packaging file: {path}"


# ============================================================ line endings


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_packaging_files_use_lf_line_endings(path: Path) -> None:
    """LF only: a CR makes ``set -eu`` fail on Linux and corrupts the payload."""
    data = path.read_bytes()
    assert b"\r\n" not in data, f"{path.name} has CRLF line endings; use LF"


# ============================================================== YAML parsing


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # noqa: PLC0415  (dev-only dependency, probed at call time)
    except ModuleNotFoundError:
        pytest.skip("PyYAML is not installed; cannot parse YAML")
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_nfpm_yaml_parses() -> None:
    config = _load_yaml(NFPM_YAML)
    assert isinstance(config, dict), "nfpm.yaml must parse to a mapping"


def test_snapcraft_yaml_parses() -> None:
    config = _load_yaml(SNAPCRAFT_YAML)
    assert isinstance(config, dict), "snapcraft.yaml must parse to a mapping"


def test_nfpm_yaml_declares_the_package_identity() -> None:
    config = _load_yaml(NFPM_YAML)
    assert config["name"] == "stratum-memory"
    assert config["license"] == "Apache-2.0"
    assert config["maintainer"], "nfpm.yaml must name a maintainer"
    assert config["description"], "nfpm.yaml must carry a description"
    assert "stratum-memory" in str(config.get("homepage", "")), (
        "nfpm.yaml homepage must point at the project"
    )


def test_nfpm_yaml_takes_the_version_from_the_environment() -> None:
    """The version is a CI-set placeholder, never a hard-coded literal."""
    text = NFPM_YAML.read_text(encoding="utf-8")
    assert "${VERSION}" in text, "nfpm.yaml must read the version from ${VERSION}"
    config = _load_yaml(NFPM_YAML)
    assert config["version"] == "${VERSION}", (
        "nfpm.yaml version must be the ${VERSION} placeholder, not a literal"
    )
    assert config["arch"] == "${ARCH}", (
        "nfpm.yaml arch must be the ${ARCH} placeholder (amd64/x86_64 per packager)"
    )


def test_nfpm_yaml_requires_python_312() -> None:
    """The dependency must match ``pyproject.toml`` (``requires-python = ">=3.12"``)."""
    text = NFPM_YAML.read_text(encoding="utf-8")
    assert "python3 >= 3.12" in text, "nfpm.yaml must depend on python3 >= 3.12"
    assert "python3 (>= 3.12)" in text, "the Debian override needs the parenthesised form"


def test_nfpm_yaml_installs_the_cli_and_the_docs() -> None:
    text = NFPM_YAML.read_text(encoding="utf-8")
    assert "/usr/bin/stratum" in text, "nfpm.yaml must install the CLI as /usr/bin/stratum"
    assert "/usr/share/doc/stratum-memory/" in text, (
        "nfpm.yaml must install documentation under /usr/share/doc/stratum-memory/"
    )
    assert "packaging/linux/stratum-launcher.sh" in text, (
        "nfpm.yaml must install the launcher script as the CLI"
    )


def test_snapcraft_yaml_identity_and_confinement() -> None:
    config = _load_yaml(SNAPCRAFT_YAML)
    assert config["name"] == "stratum-memory"
    assert config["confinement"] == "strict", "the snap must be strictly confined"
    assert config["version"] == "${VERSION}", "snapcraft.yaml version is a CI placeholder"


def test_snapcraft_yaml_defines_the_stratum_app() -> None:
    config = _load_yaml(SNAPCRAFT_YAML)
    apps = config.get("apps") or {}
    assert "stratum" in apps, "snapcraft.yaml must define apps.stratum"
    assert apps["stratum"].get("command"), "apps.stratum.command must be set"


def test_snapcraft_yaml_stages_python3() -> None:
    config = _load_yaml(SNAPCRAFT_YAML)
    parts = config.get("parts") or {}
    assert parts, "snapcraft.yaml must define at least one part"
    stage = {package for part in parts.values() for package in (part.get("stage-packages") or [])}
    assert "python3" in stage, f"a part must stage python3; got {sorted(stage)}"


def test_snapcraft_yaml_uses_core24_for_python_312() -> None:
    """The project needs >= 3.12; core22 would ship Python 3.10 and break it."""
    text = SNAPCRAFT_YAML.read_text(encoding="utf-8")
    config = _load_yaml(SNAPCRAFT_YAML)
    assert config["base"] == "core24", "the snap must build on core24 (Ubuntu 24.04)"
    assert "core22" not in text, "core22 only carries Python 3.10 and must not come back"
    assert "python3.10" not in text, "a Python 3.10 prime path is a hard-coded bug"


def test_snapcraft_yaml_primes_the_real_interpreter() -> None:
    config = _load_yaml(SNAPCRAFT_YAML)
    parts = config.get("parts") or {}
    primed = {entry for part in parts.values() for entry in (part.get("prime") or [])}
    assert "usr/lib/python3.12" in primed, (
        f"the prime list must name the real interpreter (usr/lib/python3.12); got {sorted(primed)}"
    )
    assert "bin/stratum" in primed, "the CLI launcher must be primed"
    assert not any(entry.startswith("usr/lib/python3.1") and entry != "usr/lib/python3.12" for entry in primed), (
        "only the python3.12 tree may be primed"
    )


def test_snapcraft_yaml_uses_platforms_not_architectures() -> None:
    """core24 rejects `architectures:` and needs the `platforms:` key.

    snapcraft aborts with "architectures key is not supported for base 'core24';
    use 'platforms' key instead", which silently cost the snap asset in the
    first v0.1.0 release (the step is continue-on-error, so the release still
    succeeded without the snap).
    """
    text = SNAPCRAFT_YAML.read_text(encoding="utf-8")
    config = _load_yaml(SNAPCRAFT_YAML)
    assert "architectures" not in config, (
        "core24 does not accept the legacy `architectures:` key; use `platforms:`"
    )
    # Only flag a *top-level key*, not the explanatory comment that names the
    # rejected value (the comment is the reason the key is absent).
    assert not re.search(r"^architectures\s*:", text, flags=re.MULTILINE), (
        "no architectures block may remain in the YAML"
    )
    platforms = config.get("platforms") or {}
    assert "amd64" in platforms, "the amd64 platform must be declared for the contract snap"
    amd64 = platforms["amd64"]
    assert "amd64" in (amd64.get("build-on") or []), "the amd64 platform must build on amd64"
    assert "amd64" in (amd64.get("build-for") or []), "the amd64 platform must build for amd64"


def test_snapcraft_pip_install_ignores_preinstalled_deb_packages() -> None:
    """The core24 build env ships Debian-managed wheels pip cannot uninstall.

    A plain `pip install --prefix` aborts with "Cannot uninstall
    typing_extensions 4.10.0, RECORD file not found. Hint: The package was
    installed by debian", which cost the snap asset in the v0.1.0 run.
    """
    text = SNAPCRAFT_YAML.read_text(encoding="utf-8")
    build = text.split("override-build:", 1)[1]
    assert "pip install" in build, "the snap must install the built wheel"
    assert "--ignore-installed" in build, (
        "the core24 build env has debian-managed wheels pip cannot uninstall; "
        "the install needs --ignore-installed"
    )


def test_snapcraft_yaml_keeps_build_and_stage_packages_consistent() -> None:
    """Both lists must exist and must not contradict the core24 choice."""
    config = _load_yaml(SNAPCRAFT_YAML)
    for part in (config.get("parts") or {}).values():
        for package in (part.get("build-packages") or []) + (part.get("stage-packages") or []):
            assert not re.search(r"python3\.\d", str(package)), (
                f"a versioned python package ({package!r}) pins the interpreter; use python3"
            )
        assert "python3-pip" in (part.get("build-packages") or []), (
            "the wheel is installed with pip, so python3-pip must be a build package"
        )


# ============================================================== shell syntax


@needs_bash
@pytest.mark.parametrize("path", (BUILD_DEB_RPM_SH, BUILD_APPIMAGE_SH), ids=lambda p: p.name)
def test_bash_scripts_are_syntactically_valid(path: Path) -> None:
    assert BASH is not None
    _syntax_check(BASH, path)


@needs_sh
@pytest.mark.parametrize("path", (LAUNCHER_SH, APPRUN), ids=lambda p: p.name)
def test_posix_sh_scripts_are_syntactically_valid(path: Path) -> None:
    assert SH is not None
    _syntax_check(SH, path)


def test_launcher_is_a_posix_sh_wrapper_around_the_cli_module() -> None:
    text = LAUNCHER_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n"), "the launcher must be a POSIX sh script"
    assert "set -eu" in text, "the launcher must use strict mode"
    assert 'exec "$PYTHON" -m stratum.cli "$@"' in text, (
        'the launcher must exec `python3 -m stratum.cli "$@"`'
    )
    assert "python3" in text, "the launcher must default to python3"


def test_apprun_uses_strict_mode() -> None:
    text = APPRUN.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n"), "AppRun must be a POSIX sh script"
    assert "set -eu" in text
    assert "APPDIR" in text, "AppRun must resolve the payload through $APPDIR"


@pytest.mark.parametrize("path", (BUILD_DEB_RPM_SH, BUILD_APPIMAGE_SH), ids=lambda p: p.name)
def test_build_scripts_are_bash_with_strict_mode(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n"), f"{path.name} must be a bash script"
    assert "set -eu" in text, f"{path.name} must use `set -eu`"


# ============================================================= desktop entry


def test_desktop_entry_has_the_required_keys() -> None:
    text = DESKTOP.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "[Desktop Entry]" in lines, "stratum.desktop needs a [Desktop Entry] group"
    keys = {
        line.split("=", 1)[0].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    for required in ("Name", "Exec", "Categories"):
        assert required in keys, f"stratum.desktop is missing the {required!r} key"
    assert "Type=Application" in text, "stratum.desktop must be Type=Application"


def test_desktop_entry_exec_is_the_cli() -> None:
    text = DESKTOP.read_text(encoding="utf-8")
    match = re.search(r"(?m)^Exec=(.+)$", text)
    assert match, "stratum.desktop has no Exec line"
    assert match.group(1).strip().split()[0] == "stratum", (
        "the desktop entry must launch the `stratum` CLI"
    )


# ============================================= contract names in build scripts


def test_build_deb_rpm_writes_the_contract_asset_names() -> None:
    text = BUILD_DEB_RPM_SH.read_text(encoding="utf-8")
    assert DEB_ASSET in text, f"build-deb-rpm.sh must produce {DEB_ASSET}"
    assert RPM_ASSET in text, f"build-deb-rpm.sh must produce {RPM_ASSET}"
    assert "python -m build --wheel" in text, "the wheel must be built before packaging"
    assert "nfpm package" in text, "build-deb-rpm.sh must call `nfpm package`"
    assert "--packager deb" in text and "--packager rpm" in text, (
        "build-deb-rpm.sh must build both the deb and the rpm"
    )
    assert "VERSION" in text, "build-deb-rpm.sh must take the version from the environment"
    assert "dist" in text, "build-deb-rpm.sh must write its artefacts into dist/"


def test_build_appimage_writes_the_contract_asset_name() -> None:
    text = BUILD_APPIMAGE_SH.read_text(encoding="utf-8")
    assert APPIMAGE_ASSET in text, f"build-appimage.sh must produce {APPIMAGE_ASSET}"
    assert "appimagetool" in text, "build-appimage.sh must use appimagetool"
    assert "AppRun" in text, "build-appimage.sh must place AppRun in the AppDir"
    assert "VERSION" in text, "build-appimage.sh must take the version from the environment"


def test_build_appimage_builds_the_wheel_first() -> None:
    text = BUILD_APPIMAGE_SH.read_text(encoding="utf-8")
    assert "python -m build --wheel" in text, "build-appimage.sh must build the wheel it bundles"


@pytest.mark.parametrize("path", (BUILD_DEB_RPM_SH, BUILD_APPIMAGE_SH), ids=lambda p: p.name)
def test_build_scripts_require_the_version_instead_of_defaulting(path: Path) -> None:
    """A run without VERSION must stop, not silently build 0.1.0."""
    text = path.read_text(encoding="utf-8")
    assert re.search(r'VERSION="\$\{VERSION:\?', text), (
        f"{path.name} must require VERSION (VERSION=${{VERSION:?set VERSION ...}})"
    )
    assert not re.search(r'VERSION="\$\{VERSION:-', text), (
        f"{path.name} must not fall back to a hard-coded version"
    )
    assert not re.search(r'VERSION="\$\{VERSION:-0\.1\.0\}"', text), (
        f"{path.name} must not hard-code the 0.1.0 fallback"
    )
    # The only mention of a concrete version left may be the usage example.
    for line in text.splitlines():
        if "0.1.0" in line:
            assert line.lstrip().startswith("#"), (
                f"{path.name} mentions a hard-coded version outside a comment: {line!r}"
            )


def test_build_scripts_share_the_macos_version_rule() -> None:
    """The same `${VERSION:?}` guard the macOS scripts use (packaging/macos/README.md)."""
    for path in (BUILD_DEB_RPM_SH, BUILD_APPIMAGE_SH):
        text = path.read_text(encoding="utf-8")
        assert "see release.yml" in text, (
            f"{path.name} must point the reader at the workflow that sets VERSION"
        )


def test_readme_documents_every_linux_channel() -> None:
    text = README_MD.read_text(encoding="utf-8")
    for command in ("apt install", "dnf install", "chmod +x", "snap install"):
        assert command in text, f"packaging/linux/README.md must document {command!r}"
    for asset in (".deb", ".rpm", ".AppImage", ".snap"):
        assert asset in text, f"README.md must mention the {asset} channel"


# ================================================================== secrets


#: Patterns that would indicate a leaked credential (mirrors test_deploy_server).
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    (
        "literal password",
        r"(?im)^\s*(?:POSTGRES_PASSWORD|STRATUM_DB_PASSWORD|DB_PASSWORD|PGPASSWORD)\s*[:=]\s*[^\s]",
    ),
    # A high-entropy blob (hex/base64url) of 40+ chars — typical of a leaked key.
    # `/` is excluded from the charset on purpose: every packaged path contains
    # long slash-joined segments (`usr/share/icons/hicolor/256x256/apps`), and a
    # leaked hex/base64url token never contains one, so splitting on `/` keeps
    # the check honest instead of noisy.
    (
        "long random token",
        r"(?<![A-Za-z0-9+=])[A-Za-z0-9+=]{40,}={0,2}(?![A-Za-z0-9+=])",
    ),
)


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_secrets_in_packaging_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    # URLs legitimately contain long slash-joined path segments (e.g. the
    # appimagetool release URL) which the entropy pattern would flag. Strip the
    # URLs first; a credential would still have to appear *outside* a URL.
    scanned = re.sub(r"https?://\S+", "", text)
    for label, pattern in _SECRET_PATTERNS:
        match = re.search(pattern, scanned)
        assert match is None, f"possible {label} in {path.name}: {match.group(0)!r}"
