"""Tests for the end-user installation documentation (``docs/install.md``).

These tests are deliberately **database-free, Docker-free and network-free**:
they only read the shipped Markdown files and assert the *contract* of the
installation guide (``docs/distribution.md`` is the source of truth for the
package names and release assets):

* every channel/OS named in the task exists in ``docs/install.md``,
* every canonical identifier appears (winget id, GHCR image, PyPI/npm package,
  both short ``get.stratum.dev`` URLs),
* the channel × OS matrix exists and covers at least five channels per OS,
* the runtime prerequisite is stated **correctly**: only MSI/EXE (PyInstaller
  freeze) and Docker are self-contained; ``.deb``/``.rpm``, Snap, AppImage,
  ``.pkg``/``.dmg``, Homebrew, curl, PowerShell, pipx/pip and npm all need a
  Python >= 3.12 on the host,
* the deinstallation table names a real Inno Setup uninstaller (no invented
  ``/UNINSTALL`` switch),
* the elevated-rights row includes the ``.pkg`` installer,
* the winget section admits the one-time ``wingetcreate`` submission,
* every referenced release asset matches the naming contract of
  ``docs/distribution.md`` (no invented artifact names),
* ``README.md`` links to ``docs/install.md`` and carries the three one-liners,
* neither file leaks a secret.

The repository root is derived from ``__file__`` so the tests run regardless of
the working directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = REPO_ROOT / "docs" / "install.md"
README_PATH = REPO_ROOT / "README.md"

#: The three one-liners that must be prominent in both files.
CURL_ONE_LINER = "curl -fsSL https://get.stratum.dev/install.sh | bash"
PS_ONE_LINER = "irm https://get.stratum.dev/install.ps1 | iex"
PIPX_ONE_LINER = "pipx install stratum-memory"

#: Canonical identifiers from ``docs/distribution.md`` (naming contract).
WINGET_ID = "TotalBassJunkie.StratumMemory"
GHCR_IMAGE = "ghcr.io/totalbassjunkie/stratum-memory"
PYPI_PACKAGE = "stratum-memory"
SHORT_SH = "get.stratum.dev/install.sh"
SHORT_PS1 = "get.stratum.dev/install.ps1"

#: Every channel/OS keyword the guide must mention at least once. The match is
#: case-insensitive so ``Snap``/``snap`` and ``Homebrew``/``brew`` both count.
REQUIRED_CHANNEL_KEYWORDS: tuple[str, ...] = (
    "winget",
    "scoop",
    "npm",
    "chocolatey",
    "curl",
    ".deb",
    ".rpm",
    "appimage",
    "snap",
    "homebrew",
    "brew",
    ".pkg",
    ".dmg",
    "pipx",
    "docker",
)

#: The OS columns of the channel matrix.
OS_HEADERS: tuple[str, ...] = ("Windows", "Linux", "macOS")
MIN_CHANNELS_PER_OS = 5

#: Every channel of the "Braucht der Kanal ein eigenes Python >= 3.12?" table,
#: as ``name: (regex that finds the row, required verdict)``. The regex is
#: matched case-insensitively against the row label (Markdown stripped). Only
#: MSI/EXE (PyInstaller freeze) and Docker may answer ``nein``.
RUNTIME_TABLE_EXPECTATIONS: dict[str, tuple[str, str]] = {
    "deb/rpm": (r"\.deb\s*/\s*\.rpm", "ja"),
    "Snap": (r"snap", "ja"),
    "AppImage": (r"appimage", "ja"),
    "pkg/dmg": (r"\.pkg\s*/\s*\.dmg", "ja"),
    "Homebrew": (r"homebrew", "ja"),
    "curl/install.sh": (r"curl\s*/\s*install\.sh", "ja"),
    "PowerShell/install.ps1": (r"powershell\s*/\s*install\.ps1", "ja"),
    "pipx/pip": (r"pipx\s*/\s*pip", "ja"),
    "npm": (r"^npm$", "ja"),
    "MSI": (r"^msi$", "nein"),
    "EXE": (r"^exe\b", "nein"),
    "Docker": (r"^docker$", "nein"),
}

#: Release assets named in ``docs/install.md``; every ``Stratum-<version>-…``
#: / ``stratum_<version>…`` / ``stratum-<version>…`` token in the file must match
#: one of these, i.e. must not invent an artifact name.
CONTRACT_ASSET_SUFFIXES: tuple[str, ...] = (
    "x64.msi",
    "setup.exe",
    "x86_64.pkg",
    "x86_64.dmg",
    "x86_64.AppImage",
    "_amd64.deb",
    "-1.x86_64.rpm",
)

#: Version placeholders the guide uses inside an asset name.
_VERSION_TOKEN = r"(?:<version>|\d+\.\d+\.\d+)"
_ASSET_RE = re.compile(
    rf"(?:Stratum-|stratum[_-]){_VERSION_TOKEN}"
    r"(?:[A-Za-z0-9._-]*\.(?:msi|exe|pkg|dmg|AppImage|deb|rpm|snap))?"
)

#: A table row: the leading label and the remaining cells.
_TABLE_ROW_RE = re.compile(r"^\|\s*(?P<label>[^|]+?)\s*\|(?P<rest>.*)\|\s*$")


# ============================================================ fixtures


@pytest.fixture(scope="module")
def install_text() -> str:
    assert INSTALL_PATH.is_file(), f"{INSTALL_PATH} does not exist"
    return INSTALL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README_PATH.is_file(), f"{README_PATH} does not exist"
    return README_PATH.read_text(encoding="utf-8")


# ============================================================ docs/install.md


def test_install_doc_exists_and_is_german(install_text: str) -> None:
    """The guide exists and is written in German (the user's language)."""
    assert install_text.startswith("# "), "install.md should start with an H1 heading"
    assert "Installation" in install_text
    # A few German words that would not survive a careless English rewrite.
    for word in ("Voraussetzungen", "Deinstallation", "Betriebssystem"):
        assert word in install_text, f"expected the German heading {word!r}"


@pytest.mark.parametrize("keyword", REQUIRED_CHANNEL_KEYWORDS)
def test_install_doc_mentions_every_channel(install_text: str, keyword: str) -> None:
    assert re.search(re.escape(keyword), install_text, re.IGNORECASE), (
        f"docs/install.md does not mention the channel {keyword!r}"
    )


def test_install_doc_has_all_three_os_sections(install_text: str) -> None:
    for heading in ("## Windows", "## Linux", "## macOS"):
        assert heading in install_text, f"missing OS section {heading!r}"


@pytest.mark.parametrize(
    "identifier",
    [WINGET_ID, GHCR_IMAGE, PYPI_PACKAGE, SHORT_SH, SHORT_PS1],
)
def test_install_doc_contains_contract_identifiers(install_text: str, identifier: str) -> None:
    assert identifier in install_text, (
        f"docs/install.md must contain the contract identifier {identifier!r}"
    )


def test_install_doc_has_copy_paste_one_liners(install_text: str) -> None:
    assert CURL_ONE_LINER in install_text
    assert PS_ONE_LINER in install_text
    assert PIPX_ONE_LINER in install_text


def test_install_doc_commands_are_in_code_blocks(install_text: str) -> None:
    """The three one-liners must live inside fenced code blocks, not prose."""
    blocks = re.findall(r"```[a-z]*\n(.*?)```", install_text, re.DOTALL)
    joined = "\n".join(blocks)
    for one_liner in (CURL_ONE_LINER, PS_ONE_LINER, PIPX_ONE_LINER):
        assert one_liner in joined, f"{one_liner!r} is not inside a code block"


def test_install_doc_documents_the_post_install_flow(install_text: str) -> None:
    """`stratum install` → `stratum up` → `stratum status`, with the Docker caveat."""
    for command in ("stratum install", "stratum up", "stratum status"):
        assert command in install_text, f"missing the post-install step {command!r}"
    assert re.search(r"(?i)docker", install_text), "the Docker prerequisite is not explained"


def test_install_doc_explains_the_required_python_version(install_text: str) -> None:
    assert "3.12" in install_text, "the Python >= 3.12 requirement is not stated"


def test_install_doc_explains_the_cname_hint(install_text: str) -> None:
    """`get.stratum.dev` is only a CNAME; the raw GitHub URLs always work."""
    assert "CNAME" in install_text
    assert "raw.githubusercontent.com" in install_text


def test_install_doc_has_a_deinstallation_section(install_text: str) -> None:
    assert "## Deinstallation" in install_text
    # Spot-check a few channels rather than every row.
    for snippet in (
        "winget uninstall",
        "pipx uninstall stratum-memory",
        "sudo snap remove stratum-memory",
        "brew uninstall stratum-memory",
        "npm uninstall -g stratum-memory",
    ):
        assert snippet in install_text, f"deinstallation is missing {snippet!r}"


# ============================================== markdown section / table helpers


def _section(install_text: str, heading: str) -> str:
    """Return the body of the ``## <heading>`` section (up to the next ``## ``)."""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", install_text, re.MULTILINE)
    assert match, f"no '## {heading}' section in docs/install.md"
    rest = install_text[match.end() :]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _table_row(section_text: str, keyword: str) -> str:
    """Return the first table row whose *label* cell contains ``keyword``."""
    for line in section_text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if match and keyword.lower() in match.group("label").lower():
            return line
    raise AssertionError(f"no table row labelled like {keyword!r}")


def _python_runtime_table(install_text: str) -> dict[str, str]:
    """Parse the "Braucht der Kanal ein eigenes Python >= 3.12?" table.

    Returns ``{label: "ja"|"nein"}``; the label is lowercased and stripped of
    Markdown noise (backticks, bold markers, collapsed whitespace) so lookups do
    not depend on cosmetic formatting.
    """
    rows: dict[str, str] = {}
    for line in install_text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("rest").split("|")]
        if len(cells) < 2:
            continue
        verdict = cells[0].strip().strip("*").strip().lower()
        if verdict not in ("ja", "nein"):
            continue
        label = re.sub(r"[`*\s]+", " ", match.group("label")).strip().lower()
        rows[label] = verdict
    return rows


# ============================================== runtime prerequisite (finding 1)


def test_prerequisites_do_not_claim_the_native_installers_bundle_python(
    install_text: str,
) -> None:
    """The guide must not repeat the old "every native installer is self-contained"."""
    for pattern in (
        # "Die nativen Installer … bringen ihre eigene Laufzeit mit."
        r"(?:nativen|native|alle)\s+Installer[^.\n]{0,140}(?:eigene\s+Laufzeit|Laufzeit\s+mit)",
        # "… .deb/.rpm, Snap, AppImage … bringen ihre eigene Laufzeit mit."
        r"(?:\.deb|\.rpm|Snap|AppImage|\.pkg|\.dmg|Homebrew)[^.\n]{0,140}"
        r"bring(?:en|t)\s+ihre\s+eigene\s+Laufzeit\s+mit",
    ):
        match = re.search(pattern, install_text)
        assert match is None, (
            "docs/install.md still claims the native packages ship their own "
            f"runtime: {match.group(0)!r}"
        )


def test_prerequisites_row_does_not_limit_python_to_pipx_and_npm(install_text: str) -> None:
    """Python is needed by every channel but MSI/EXE/Docker, not only pipx/npm."""
    row = _table_row(_section(install_text, "Voraussetzungen"), "Python")
    assert re.search(r"(?i)nur\s+für", row) is None, (
        f"the Python prerequisite must not be limited to a few channels: {row!r}"
    )
    for channel in ("MSI", "EXE", "Docker"):
        assert channel in row, f"the Python prerequisite row must name {channel} as the exception"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    list(RUNTIME_TABLE_EXPECTATIONS.values()),
    ids=list(RUNTIME_TABLE_EXPECTATIONS),
)
def test_runtime_python_table_maps_every_channel_correctly(
    install_text: str, pattern: str, expected: str
) -> None:
    rows = _python_runtime_table(install_text)
    matches = [label for label in rows if re.search(pattern, label, re.IGNORECASE)]
    assert matches, f"the runtime table has no row for {pattern!r}"
    for label in matches:
        assert rows[label] == expected, (
            f"{label!r} is marked {rows[label]!r} but must be {expected!r} "
            "(only MSI/EXE/Docker ship their own runtime)"
        )


def test_runtime_python_table_has_no_undocumented_rows(install_text: str) -> None:
    """Every row of the runtime table must be a channel this contract knows."""
    rows = _python_runtime_table(install_text)
    assert rows, "the 'Braucht der Kanal ein eigenes Python >= 3.12?' table is missing"
    unknown = [
        label
        for label in rows
        if not any(
            re.search(pattern, label, re.IGNORECASE)
            for pattern, _ in RUNTIME_TABLE_EXPECTATIONS.values()
        )
    ]
    assert not unknown, f"runtime table rows without an expectation: {unknown}"


def test_runtime_section_names_the_concrete_symptoms(install_text: str) -> None:
    """Ubuntu 22.04 (Python 3.10) must be the worked example, with both symptoms."""
    section = _section(install_text, "Voraussetzungen")
    assert "Ubuntu 22.04" in section
    assert "3.10" in section
    assert re.search(r"Depends:\s*python3\s*\(>=\s*3\.12\)", section), (
        "the .deb dependency failure is not shown"
    )
    assert "ModuleNotFoundError" in section, "the AppImage import failure is not shown"


def test_elevated_rights_row_includes_the_pkg_installer(install_text: str) -> None:
    """The .pkg runs as root (postinstall writes /usr/local/bin/stratum)."""
    row = _table_row(_section(install_text, "Voraussetzungen"), "sudo")
    for channel in (".deb", ".rpm", "Snap", ".pkg", "MSI", "EXE"):
        assert channel in row, f"the elevated-rights row must name {channel}"
    assert "root" in row, "the elevated-rights row must explain why the .pkg needs root"


def test_pkg_sections_document_the_root_install(install_text: str) -> None:
    """`sudo installer -pkg …` is the real command; the payload is /usr/local/lib/stratum."""
    macos = _section(install_text, "macOS (Apple)")
    assert "sudo installer -pkg" in macos
    assert "/usr/local/lib/stratum" in macos or "/usr/local/bin/stratum" in macos


# ============================================== Inno Setup uninstall (finding 2)


def test_deinstallation_does_not_invent_an_inno_uninstall_switch(install_text: str) -> None:
    """Inno Setup has no `/UNINSTALL` switch — it would just re-run Setup."""
    assert "/UNINSTALL" not in install_text, (
        "docs/install.md documents a non-existent Inno Setup switch"
    )


def test_inno_uninstall_names_the_real_uninstaller(install_text: str) -> None:
    row = _table_row(_section(install_text, "Deinstallation"), "EXE")
    assert "Apps & Features" in row, "the GUI path is missing"
    assert re.search(r"unins\d+\.exe", row), (
        "the documented uninstaller must be the unins000.exe placed by Inno Setup"
    )
    assert "%LOCALAPPDATA%\\Programs\\Stratum" in row, "the default install directory is missing"
    assert "/VERYSILENT" in row, "the silent flag for the uninstaller is missing"


# ============================================== winget submission (finding 4)


def test_winget_section_documents_the_one_time_submission(install_text: str) -> None:
    """`winget install` only works after the package landed in microsoft/winget-pkgs."""
    windows = _section(install_text, "Windows")
    start = windows.index("### 1. winget")
    end = windows.index("### 2.")
    winget = windows[start:end]

    assert "wingetcreate" in winget, "the one-time submission tool is not named"
    assert "microsoft/winget-pkgs" in winget, "the community repository is not named"
    assert re.search(r"(?i)nicht\s+automatisierbar", winget), (
        "the guide must admit the submission is a manual, one-time step"
    )
    assert "No package found" in winget, "the symptom before the submission is not shown"
    for alternative in ("MSI", "EXE", "Scoop", "npm", "PowerShell"):
        assert alternative in winget, f"the winget hint must point at the {alternative} fallback"


# ============================================== asset names (naming contract)


def test_install_doc_only_names_contract_artifacts(install_text: str) -> None:
    """Every release asset named here must exist in docs/distribution.md."""
    assets = {match.group(0) for match in _ASSET_RE.finditer(install_text)}
    assert assets, "no release asset is named in docs/install.md"
    invented = sorted(asset for asset in assets if not asset.endswith(CONTRACT_ASSET_SUFFIXES))
    assert not invented, (
        f"artifact names outside the docs/distribution.md contract: {invented}"
    )


# ============================================================ channel × OS matrix


def _matrix_rows(install_text: str) -> list[tuple[str, tuple[bool, ...]]]:
    """Parse the channel × OS table into ``(label, per-OS checks)`` rows.

    A row counts as covering an OS when its cell is *not* a dash (``-``/``–``).
    """
    rows: list[tuple[str, tuple[bool, ...]]] = []
    for line in install_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(OS_HEADERS) + 1:
            continue
        # Skip the header row and the ``|---|---|`` separator.
        if cells[0] in ("Kanal", "") or set(cells[0]) <= set("-: "):
            continue
        marks = tuple(cell not in ("-", "–", "", ":---:") for cell in cells[1:])
        rows.append((cells[0], marks))
    return rows


def test_install_doc_has_a_channel_by_os_matrix(install_text: str) -> None:
    assert "Kanal" in install_text and "Betriebssystem" in install_text
    header = re.search(r"^\|\s*Kanal\s*\|.*\|.*\|.*\|$", install_text, re.MULTILINE)
    assert header, "no 'Kanal × Betriebssystem' table header found"
    for os_name in OS_HEADERS:
        assert os_name in header.group(0), f"matrix header is missing the {os_name} column"


@pytest.mark.parametrize("os_index,os_name", list(enumerate(OS_HEADERS)))
def test_matrix_covers_at_least_five_channels_per_os(
    install_text: str, os_index: int, os_name: str
) -> None:
    rows = _matrix_rows(install_text)
    covered = [label for label, marks in rows if marks[os_index]]
    assert len(covered) >= MIN_CHANNELS_PER_OS, (
        f"only {len(covered)} channels for {os_name}: {covered}"
    )


def test_matrix_lists_every_required_channel(install_text: str) -> None:
    labels = " ".join(label.lower() for label, _ in _matrix_rows(install_text))
    for keyword in REQUIRED_CHANNEL_KEYWORDS:
        assert keyword in labels, f"the channel matrix does not list {keyword!r}"


# ============================================================ README.md


def test_readme_links_to_the_install_doc(readme_text: str) -> None:
    assert "docs/install.md" in readme_text, "README.md must link to docs/install.md"


def test_readme_has_an_installation_section(readme_text: str) -> None:
    assert "## Installation" in readme_text
    # ...and the pointer near the intro.
    head = readme_text[: readme_text.index("## Architecture at a glance")]
    assert "docs/install.md" in head, "the intro should point at docs/install.md"


def test_readme_has_the_three_one_liners(readme_text: str) -> None:
    for one_liner in (CURL_ONE_LINER, PS_ONE_LINER, PIPX_ONE_LINER):
        assert one_liner in readme_text, f"README.md is missing the one-liner {one_liner!r}"


def test_readme_states_the_python_prerequisite(readme_text: str) -> None:
    """The install section must not imply that only pipx needs Python."""
    section = readme_text[
        readme_text.index("## Installation") : readme_text.index("## Architecture at a glance")
    ]
    assert "3.12" in section, "the README install section omits the Python >= 3.12 requirement"
    assert re.search(r"(?i)MSI", section) and re.search(r"(?i)EXE", section), (
        "the README must name MSI/EXE as the self-contained exception"
    )


# ============================================================ secrets


#: Patterns that would indicate a leaked credential (not a *path* to one).
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{30,}"),
    ("PyPI token", r"\bpypi-[A-Za-z0-9_-]{30,}"),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    ("private key header", r"\bBEGIN OPENSSH PRIVATE KEY\b"),
    # A literal password assignment (as opposed to a *_FILE pointer).
    (
        "literal password",
        r"(?im)^\s*(?:POSTGRES_PASSWORD|STRATUM_DB_PASSWORD|DB_PASSWORD|PGPASSWORD)\s*[:=]\s*[^\s]",
    ),
    # A high-entropy blob (hex/base64) of 40+ chars — typical of leaked keys.
    ("long random token", r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)


@pytest.mark.parametrize(
    "path",
    [INSTALL_PATH, README_PATH],
    ids=["install", "readme"],
)
def test_no_secrets_in_docs(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for label, pattern in _SECRET_PATTERNS:
        match = re.search(pattern, text)
        assert match is None, f"possible {label} in {path.name}: {match.group(0)!r}"
