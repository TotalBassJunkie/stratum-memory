"""Tests for the end-user installation documentation (``docs/install.md``).

These tests are deliberately **database-free, Docker-free and network-free**:
they only read the shipped Markdown files and assert the *contract* of the
installation guide (``docs/distribution.md`` is the source of truth for the
package names and release assets):

* every channel/OS named in the task exists in ``docs/install.md``,
* every canonical identifier appears (winget id, GHCR image, PyPI/npm package,
  both short ``get.stratum.dev`` URLs),
* the channel × OS matrix exists and covers at least five channels per OS,
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
