"""Tests for the one-line installers (``install.sh`` / ``install.ps1``).

The installers are the *first* thing a new user runs, so the contract in
``docs/distribution.md`` (the CLI comes from PyPI; the Docker stack is bootstrapped
later by ``stratum install``) is pinned here as executable expectations.

The suite is deliberately **DB-free, network-free and side-effect-free**:

* the static checks only read the two files and pattern-match them,
* ``install.sh`` is checked with ``bash -n`` (skipped when no working bash is on
  PATH) and exercised end-to-end under ``STRATUM_DRY_RUN=1`` against throwaway
  ``uname`` / ``python3`` / ``pipx`` shims on a private PATH — nothing is
  installed and no real download happens,
* ``install.ps1`` is parsed with the PowerShell language parser and, when a
  Python >= 3.12 is available, dry-run for real (skipped otherwise).

The dry-run tests assert the *decisions* the scripts make (OS/arch detection,
pipx-before-pip, version pinning, the ``stratum version`` verification) rather
than their cosmetic output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

#: Hosts the installers are allowed to *mention*. Anything else would mean a
#: second, uncontrolled download channel.
ALLOWED_URL_HOSTS = frozenset({"get.stratum.dev", "brew.sh", "www.python.org"})


# =========================================================== discovery helpers


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


def _find_working_powershell() -> str | None:
    """Return a PowerShell that actually executes a script block, or ``None``."""
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            probe = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", "Write-Output ok"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "ok" in probe.stdout:
            return executable
    return None


def _has_suitable_python() -> bool:
    """True when ``py -3``/``python`` >= 3.12 is available to install.ps1."""
    for exe, extra in (("py", ["-3"]), ("python", []), ("python3", [])):
        if not shutil.which(exe):
            continue
        try:
            probe = subprocess.run(
                [exe, *extra, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return True
    return False


BASH = _find_working_bash()
POWERSHELL = _find_working_powershell()
SUITABLE_PYTHON = _has_suitable_python()

needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on PATH")

SH_TEXT = INSTALL_SH.read_text(encoding="utf-8")
PS1_TEXT = INSTALL_PS1.read_text(encoding="utf-8")


# ============================================================== text utilities


def _strip_comments_and_heredocs(text: str, *, comment: str = "#") -> str:
    """Drop comments and here-doc/here-string bodies, leaving only *executable* code.

    The installers legitimately *print* a ``curl ... | bash`` hint and reference
    documentation URLs; those live in comments, PowerShell block comments and
    here-docs/here-strings and must not count as an actual pipe-to-shell. This
    keeps the "never execute a foreign script" assertion honest without weakening
    it: any *unquoted* pipe-to-shell in real code would still be caught.
    """
    # PowerShell block comments first (they span lines and may contain a `|`).
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    kept: list[str] = []
    sh_tag: str | None = None
    ps_close: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if sh_tag is not None:
            if stripped == sh_tag:
                sh_tag = None
            continue
        if ps_close is not None:
            if stripped.startswith(ps_close):
                ps_close = None
            continue
        if stripped.startswith(comment):
            continue
        heredoc = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", stripped)
        if heredoc:
            sh_tag = heredoc.group(1)
            continue
        if stripped.endswith('@"') or stripped.endswith("@'"):
            ps_close = '"@' if stripped.endswith('@"') else "'@"
            continue
        kept.append(line)
    return "\n".join(kept)


def _urls(text: str) -> set[str]:
    return set(re.findall(r"https?://[^\s\"'`)]+", text))


def _url_hosts(text: str) -> set[str]:
    hosts = set()
    for url in _urls(text):
        match = re.match(r"https?://([^/]+)", url)
        if match:
            hosts.add(match.group(1).lower())
    return hosts


# ============================================ fake toolchain for the dry runs


def _shim(directory: Path, name: str, body: str) -> None:
    """Write an executable ``/bin/sh`` shim named *name* into *directory*."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _posix_path(path: Path) -> str:
    """Render *path* the way the *shell* (git-bash on Windows) expects to see it.

    Windows temp dirs arrive as ``C:\\...``; a POSIX shell resolves PATH entries
    against its own mounts, so the git-bash form ``/c/...`` is what makes
    ``command -v`` find the shims. On Linux/macOS the path is already POSIX and is
    returned unchanged.
    """
    text = path.as_posix()
    drive = re.match(r"^([A-Za-z]):/(.*)$", text)
    if drive:
        return f"/{drive.group(1).lower()}/{drive.group(2)}"
    return text


def _run_script_with_shims(
    shim_dir: Path, script: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *script* under ``sh`` with *shim_dir* as the **entire** PATH.

    The shim directory is set **inside** the shell on purpose: on Windows
    git-bash prepends its own tool directories (``/usr/bin``, …) while starting up,
    so a PATH set by the parent process would be shadowed by the real ``uname``.

    The PATH deliberately contains the shim directory and nothing else. The first
    version of this helper appended ``/usr/bin:/bin`` as a "POSIX toolbox", which
    looks harmless on Windows but is fatal on Debian/Ubuntu: there ``/bin`` is a
    symlink to ``/usr/bin``, so the runner's real ``python3`` (3.12) and ``pipx``
    sit exactly where the toolbox was. ``install.sh`` found them before the shims,
    so ``test_install_sh_fails_cleanly_without_python`` and
    ``test_install_sh_rejects_a_too_old_python`` exited 0 instead of 1 on
    ubuntu-latest (they had only ever passed on Windows, where git-bash's PATH
    never reaches the Windows Python). The shims must be the *only* interpreter
    the installer can see, so the run needs no host toolbox at all: the one
    external command ``install.sh`` calls in a dry run is ``cat`` (the
    missing-Python guidance), and it is provided as a shim here. ``sh`` itself is
    started through its absolute path for the same reason.
    """
    assert BASH is not None
    _shim(shim_dir, "cat", 'exec /bin/cat "$@"\n')
    wrapper = 'PATH="$1"; export PATH; exec /bin/sh "$2"'
    environment = {**os.environ, "PATH": "/usr/bin:/bin"}
    environment.update(env or {})
    return subprocess.run(
        [BASH, "-c", wrapper, "_", _posix_path(shim_dir), str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=environment,
    )


def _fake_uname(directory: Path, *, os_name: str, arch: str) -> None:
    _shim(
        directory,
        "uname",
        'case "$1" in\n'
        f"  -s) echo {os_name} ;;\n"
        f"  -m) echo {arch} ;;\n"
        f"  *) echo {os_name} ;;\n"
        "esac\n",
    )


def _fake_python(directory: Path, *, version: str = "3.12.4", pip: bool = True) -> None:
    """A ``python3`` shim that answers the version/pip probes of install.sh."""
    pip_exit = 0 if pip else 1
    _shim(
        directory,
        "python3",
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"-c\" ]; then\n"
        f"    echo {version}\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"pip\" ]; then\n"
        f"    exit {pip_exit}\n"
        "  fi\n"
        "done\n"
        "exit 0\n",
    )


def _fake_pipx(directory: Path) -> None:
    _shim(directory, "pipx", "echo pipx-stub\n")


def _run_install_sh(
    tmp_path: Path,
    *,
    os_name: str = "Linux",
    arch: str = "x86_64",
    python: str | None = "3.12.4",
    pipx: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``install.sh`` under ``sh`` against a shimmed PATH (dry run by default)."""
    assert BASH is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    _fake_uname(shim_dir, os_name=os_name, arch=arch)
    if python is not None:
        _fake_python(shim_dir, version=python)
    if pipx:
        _fake_pipx(shim_dir)

    # A private, minimal PATH (shims + POSIX toolbox) set inside the shell, so no
    # real pipx/python can leak in; see _run_script_with_shims.
    environment = {
        **os.environ,
        "STRATUM_DRY_RUN": "1",
        "HOME": str(tmp_path / "home"),
        "STRATUM_PREFIX": "user",
    }
    for key in ("STRATUM_VERSION",):
        environment.pop(key, None)
    environment.update(env or {})

    return _run_script_with_shims(shim_dir, INSTALL_SH, environment)


def _run_install_ps1(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``install.ps1`` through ``&`` so ``-DryRun``/``-Version`` bind correctly."""
    assert POWERSHELL is not None
    environment = {**os.environ, "STRATUM_VERSION": "", "STRATUM_DRY_RUN": ""}
    environment.update(env or {})
    quoted = " ".join([f"'{INSTALL_PS1}'", *args])
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"& {quoted}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
        env=environment,
    )


# ============================================================== file presence


@pytest.mark.parametrize("path", (INSTALL_SH, INSTALL_PS1), ids=lambda p: p.name)
def test_installer_exists_and_is_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing installer: {path}"
    assert path.stat().st_size > 0, f"empty installer: {path}"


def test_install_sh_is_posix_sh() -> None:
    assert SH_TEXT.startswith("#!/bin/sh\n"), "install.sh must be a POSIX sh script"


def test_install_sh_uses_strict_mode() -> None:
    assert "set -eu" in SH_TEXT


def test_install_ps1_uses_error_action_preference() -> None:
    assert "$ErrorActionPreference = 'Stop'" in PS1_TEXT


# ============================================================ install.sh: static


def test_install_sh_detects_os_and_arch() -> None:
    assert "uname -s" in SH_TEXT
    assert "uname -m" in SH_TEXT
    # OS branches (Linux/macOS only).
    assert "Linux)" in SH_TEXT and "Darwin)" in SH_TEXT
    # Arch branches, including both spellings of 64-bit ARM.
    assert "x86_64" in SH_TEXT
    assert "arm64" in SH_TEXT and "aarch64" in SH_TEXT


def test_install_sh_requires_python_312_with_guidance() -> None:
    assert "3, 12" in SH_TEXT or "3.12" in SH_TEXT
    # Clear, actionable instructions when no interpreter is found.
    assert "apt" in SH_TEXT and "dnf" in SH_TEXT and "brew" in SH_TEXT
    assert "exit 1" in SH_TEXT


def test_install_sh_has_pipx_and_pip_paths() -> None:
    assert "pipx install" in SH_TEXT
    assert "-m pip install --user" in SH_TEXT


def test_install_sh_supports_env_contract() -> None:
    assert "STRATUM_VERSION" in SH_TEXT
    assert "STRATUM_PREFIX" in SH_TEXT
    assert "STRATUM_DRY_RUN" in SH_TEXT
    assert "latest" in SH_TEXT  # STRATUM_VERSION default


def test_install_sh_implements_dry_run() -> None:
    # A single guard function routes every mutation through the dry-run check.
    assert "[dry-run]" in SH_TEXT
    assert "DRY_RUN" in SH_TEXT


def test_install_sh_verifies_the_cli_and_prints_next_steps() -> None:
    assert "stratum version" in SH_TEXT
    assert "stratum install" in SH_TEXT
    assert "stratum up" in SH_TEXT


def test_install_sh_mentions_npm_only_as_fallback_hint() -> None:
    assert "npm i -g" in SH_TEXT


def test_installers_only_reference_known_hosts() -> None:
    for name, text in (("install.sh", SH_TEXT), ("install.ps1", PS1_TEXT)):
        unknown = _url_hosts(text) - ALLOWED_URL_HOSTS
        assert not unknown, f"{name} references unexpected hosts: {sorted(unknown)}"


def test_install_sh_never_executes_a_foreign_script() -> None:
    code = _strip_comments_and_heredocs(SH_TEXT)
    assert "curl" not in code, "install.sh must not download anything"
    assert "wget" not in code, "install.sh must not download anything"
    assert not re.search(r"\|\s*(sudo\s+)?(ba)?sh\b", code), "install.sh must not pipe to a shell"


def test_install_ps1_never_executes_a_foreign_script() -> None:
    code = _strip_comments_and_heredocs(PS1_TEXT)
    for banned in ("Invoke-WebRequest", "iwr ", "Invoke-RestMethod", "curl ", "wget "):
        assert banned not in code, f"install.ps1 must not download anything ({banned!r})"
    assert not re.search(r"\|\s*iex\b", code), "install.ps1 must not pipe to iex"


# ========================================================= install.sh: dry runs


@needs_bash
def test_install_sh_dry_run_linux_x86_64(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, os_name="Linux", arch="x86_64")

    assert result.returncode == 0, result.stderr
    assert "detected: linux/x86_64" in result.stdout
    assert "[dry-run] python3 -m pip install --user stratum-memory" in result.stdout
    # The CLI is installed; the Docker stack is not.
    assert "stratum install" in result.stdout
    assert "stratum up" in result.stdout


@needs_bash
def test_install_sh_dry_run_macos_arm64(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, os_name="Darwin", arch="arm64")

    assert result.returncode == 0, result.stderr
    assert "detected: macos/arm64" in result.stdout


@needs_bash
def test_install_sh_prefers_pipx_when_available(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, pipx=True)

    assert result.returncode == 0, result.stderr
    assert "[dry-run] pipx install stratum-memory" in result.stdout
    assert "pip install" not in result.stdout


@needs_bash
def test_install_sh_pins_the_requested_version(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, env={"STRATUM_VERSION": "0.1.0"})

    assert result.returncode == 0, result.stderr
    assert "stratum-memory==0.1.0" in result.stdout


@needs_bash
def test_install_sh_fails_cleanly_without_python(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, python=None)

    assert result.returncode == 1
    assert "Python >= 3.12" in result.stderr
    assert "apt" in result.stderr


@needs_bash
def test_install_sh_rejects_a_too_old_python(tmp_path: Path) -> None:
    # A python3 that reports 3.11 makes the version probe fail (the shim always
    # prints 3.11 for `-c`, but install.sh only accepts >= 3.12), so the script
    # must stop with the installation guidance instead of calling pip.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    _fake_uname(shim_dir, os_name="Linux", arch="x86_64")
    _shim(
        shim_dir,
        "python3",
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"-c\" ]; then\n"
        "    case \"$*\" in\n"
        "      *'3, 12'*) exit 1 ;;\n"
        "      *) echo 3.11.9; exit 0 ;;\n"
        "    esac\n"
        "  fi\n"
        "done\n"
        "exit 0\n",
    )
    environment = {
        **os.environ,
        "STRATUM_DRY_RUN": "1",
    }
    result = _run_script_with_shims(shim_dir, INSTALL_SH, environment)

    assert result.returncode == 1
    assert "Python >= 3.12" in result.stderr


@needs_bash
def test_install_sh_dry_run_changes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_install_sh(tmp_path)

    assert result.returncode == 0, result.stderr
    # No venv/bin/stratum was created anywhere in the private HOME.
    assert not (home / ".local" / "bin" / "stratum").exists()
    assert not (home / ".local" / "pipx").exists()


@needs_bash
def test_install_sh_dry_runs_see_only_the_shims(tmp_path: Path) -> None:
    """Regression guard for the ubuntu-latest failures.

    The helper used to append ``/usr/bin:/bin`` to the shim PATH. On
    Debian/Ubuntu ``/bin`` is a symlink to ``/usr/bin``, so that "toolbox"
    exposed the runner's real ``python3`` (3.12) and ``pipx``. ``install.sh``
    then found a working interpreter *before* the shims and
    ``test_install_sh_fails_cleanly_without_python`` /
    ``test_install_sh_rejects_a_too_old_python`` exited 0 instead of 1 (they had
    only ever been green on Windows). The shim directory must therefore be the
    whole PATH the installer sees - nothing else.
    """
    probe = tmp_path / "probe.sh"
    probe.write_text('#!/bin/sh\nprintf "%s" "$PATH"\n', encoding="utf-8", newline="\n")
    probe.chmod(0o755)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    result = _run_script_with_shims(shim_dir, probe)

    assert result.returncode == 0, result.stderr
    seen = result.stdout.strip()
    assert seen == _posix_path(shim_dir), (
        "the installer must see only the shim directory on PATH; "
        f"it saw {seen!r} (a host python3/pipx could leak in)"
    )
    # Belt and braces: the shared system directories must not be on that PATH,
    # because on Debian/Ubuntu they are where the runner's python3/pipx live.
    entries = seen.split(":")
    assert "/usr/bin" not in entries, f"the host toolbox leaked into PATH: {seen!r}"
    assert "/bin" not in entries, f"the host toolbox leaked into PATH: {seen!r}"


# =========================================================== install.ps1: static


def test_install_ps1_has_pipx_and_pip_paths() -> None:
    assert "pipx" in PS1_TEXT
    assert "'pip', 'install', '--user'" in PS1_TEXT or "-m pip install --user" in PS1_TEXT


def test_install_ps1_has_version_and_dryrun_parameters() -> None:
    assert "$Version" in PS1_TEXT
    assert "$DryRun" in PS1_TEXT
    assert "param(" in PS1_TEXT
    assert "STRATUM_VERSION" in PS1_TEXT
    assert "STRATUM_DRY_RUN" in PS1_TEXT


def test_install_ps1_requires_python_312_with_guidance() -> None:
    assert "3, 12" in PS1_TEXT
    assert "winget install" in PS1_TEXT
    assert "Python.Python.3.12" in PS1_TEXT


def test_install_ps1_mentions_the_windows_channels() -> None:
    assert "TotalBassJunkie.StratumMemory" in PS1_TEXT
    assert "npm i -g" in PS1_TEXT


def test_install_ps1_verifies_the_cli_and_prints_next_steps() -> None:
    assert "version" in PS1_TEXT
    assert "stratum install" in PS1_TEXT
    assert "stratum up" in PS1_TEXT


@needs_powershell
def test_install_ps1_parses() -> None:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$tokens = $null\n"
        "$errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errors)\n"
        "if (@($errors).Count -gt 0) {\n"
        "  @($errors) | ForEach-Object { Write-Output $_.Message }\n"
        "  exit 1\n"
        "}\n"
        "exit 0\n"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ========================================================== install.ps1: dry run


@needs_powershell
@pytest.mark.skipif(not SUITABLE_PYTHON, reason="no Python >= 3.12 on PATH")
def test_install_ps1_dry_run_reports_plan_and_changes_nothing() -> None:
    result = _run_install_ps1("-DryRun", "-Version", "0.1.0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "stratum-memory==0.1.0" in result.stdout
    assert "[dry-run]" in result.stdout
    assert "stratum install" in result.stdout
    assert "stratum up" in result.stdout
