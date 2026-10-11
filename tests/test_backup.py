"""Tests for the WP-15 backup/restore tooling.

These tests are deliberately **DB-free and side-effect-free**: nothing connects to
Postgres and nothing executes a real ``pg_dump``/``pg_restore``/``docker``. They
cover

* the pure retention rule (:mod:`scripts.backup_lib`) exhaustively, and
* the operational scripts statically and through a *dry run*:
  - shell scripts pass ``bash -n`` (skipped when no working bash is on PATH),
  - PowerShell scripts parse (skipped when neither ``pwsh`` nor Windows
    PowerShell is present),
  - the shell dry run (:env:`STRATUM_BACKUP_DRY_RUN=1`) exits 0 without writing
    anything,
  - the dump-file-name contract ``stratum-\\d{8}-\\d{6}\\.dump`` holds in every
    script.

The repository root is added to ``sys.path`` explicitly so ``import backup_lib``
works regardless of the pytest ``pythonpath`` setting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Make `import backup_lib` work no matter what `pythonpath` pytest was started with.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backup_lib  # noqa: E402  (import after sys.path manipulation)

SHELL_SCRIPTS = ("backup.sh", "restore.sh")
POWERSHELL_SCRIPTS = ("backup.ps1", "restore.ps1")
ALL_SCRIPTS = SHELL_SCRIPTS + POWERSHELL_SCRIPTS
PY_SCRIPTS = ("backup_lib.py",)
#: The documented dump-file-name contract.
DUMP_NAME_PATTERN = r"stratum-\d{8}-\d{6}\.dump"
#: The vault-archive contract.
VAULT_NAME_PATTERN = r"vault-\d{8}-\d{6}\.tar\.gz"


# --------------------------------------------------------------- shell discovery


def _find_working_bash() -> str | None:
    """Return a bash that actually runs, or ``None``.

    ``shutil.which('bash')`` may point at the WSL stub on Windows, which fails
    at runtime; every candidate is therefore smoke-tested with ``echo ok``.
    """
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


# --------------------------------------------------------- powershell discovery


def _find_powershell() -> str | None:
    """Return a PowerShell host that can parse a file, or ``None``."""
    candidates = [shutil.which("pwsh"), shutil.which("powershell")]
    candidates += [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "/usr/bin/pwsh",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


POWERSHELL = _find_powershell()


# ================================================================== file presence


@pytest.mark.parametrize("name", ALL_SCRIPTS + PY_SCRIPTS)
def test_script_files_exist_and_nonempty(name: str) -> None:
    path = SCRIPTS_DIR / name
    assert path.is_file(), f"missing script: {path}"
    assert path.stat().st_size > 0, f"empty script: {path}"


def test_backup_restore_doc_exists_and_states_slo() -> None:
    doc = REPO_ROOT / "docs" / "backup-restore.md"
    assert doc.is_file(), f"missing documentation: {doc}"
    text = doc.read_text(encoding="utf-8")
    # RTO/RPO targets must be documented (MASTER-PLAN §15).
    assert "RPO" in text and "24" in text
    assert "RTO" in text and "1 h" in text
    assert "cron" in text.lower() or "systemd" in text.lower()
    assert "Restore-Drill" in text
    assert "Offsite" in text


# ====================================================== shell syntax + dry run


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_are_syntactically_valid(name: str) -> None:
    result = subprocess.run(
        [BASH, "-n", str(SCRIPTS_DIR / name)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"bash -n {name} failed:\n{result.stderr}"


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_dry_run_exits_clean_and_writes_nothing(tmp_path) -> None:
    target = tmp_path / "backups"
    env = {
        "STRATUM_BACKUP_DIR": str(target),
        "STRATUM_BACKUP_DRY_RUN": "1",
        # Keep the script off docker/pg_dump entirely; dry-run must not need them.
        "PATH": "",
    }
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", "7"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, (
        f"dry run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Dry run: nothing on disk, but the plan is logged.
    assert list(target.glob("*")) == [] if target.exists() else True
    assert "Stratum backup" in result.stderr


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_dry_run_prunes_oldest_per_family(tmp_path) -> None:
    # Exercises the shell-side retention (prune_family) end-to-end in dry-run:
    # 5 dumps + 1 vault, keep=2 -> the 3 oldest dumps are scheduled for removal,
    # the vault is untouched. Nothing is actually deleted (dry run).
    target = tmp_path / "backups"
    target.mkdir()
    dumps = [f"stratum-2026010{i}-000000.dump" for i in range(1, 6)]
    vaults = ["vault-20260101-000000.tar.gz"]
    for name in dumps + vaults:
        (target / name).write_bytes(b"x")

    env = {"PATH": ""}
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", "2"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log = result.stderr
    # The three oldest dumps are slated for deletion; the two newest survive.
    for old in dumps[:3]:
        assert f"rm -f {target / old}" in log or old in log
    assert dumps[3] not in log and dumps[4] not in log
    assert "retention stratum-*.dump: removed 3, kept 2" in log
    assert vaults[0] not in log  # vault family under budget
    # Dry run: every file still present.
    assert all((target / name).is_file() for name in dumps + vaults)


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_restore_sh_usage_without_args_fails_with_usage_code() -> None:
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "restore.sh")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # No arguments -> usage error (exit 1); docker/pg are never reached.
    assert result.returncode == 1
    assert "Usage" in result.stderr or "Usage" in result.stdout


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_restore_sh_missing_dump_is_rejected() -> None:
    result = subprocess.run(
        [
            BASH,
            str(SCRIPTS_DIR / "restore.sh"),
            "--dump",
            "/nonexistent/stratum-20260101-000000.dump",
            "--target-db",
            "whatever",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Missing archive -> dedicated exit code 2, before any docker call.
    assert result.returncode == 2
    assert "not found" in result.stderr


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_restore_sh_verify_requires_no_real_docker(tmp_path) -> None:
    # A non-empty placeholder is enough: --verify with a fake `docker` shim on
    # PATH (which succeeds) proves the verify path shells out to `docker exec`
    # `pg_restore --list` and reports success without touching a real DB.
    dump = tmp_path / "stratum-20260101-000000.dump"
    dump.write_bytes(b"PGDMP fake payload")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if sys.platform == "win32":
        # git-bash is a POSIX shell: the shim must be a POSIX script named
        # `docker` (a Windows `docker.cmd` would not be executable from bash).
        docker_stub = fake_bin / "docker"
        docker_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    else:
        docker_stub = fake_bin / "docker"
        docker_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    docker_stub.chmod(0o755)

    # Locate a POSIX helper shell to convert the path for git-bash on Windows.
    helper = BASH if sys.platform == "win32" else "/bin/sh"
    posix_bin = subprocess.run(
        [helper, "-c", f"cygpath -u '{fake_bin}'"]
        if sys.platform == "win32"
        else [helper, "-c", f"printf '%s' '{fake_bin}'"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()

    env = dict(os.environ)
    env["PATH"] = posix_bin
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "restore.sh"), "--dump", str(dump), "--verify"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, f"verify failed:\n{result.stderr}"
    assert "verify: OK" in result.stderr


# =================================================== prune hardening (F1/F5)
# Regression coverage for the MAJOR finding: foreign files and minted-but-
# impossible stamps must never displace a real backup from a keep slot, and the
# shell rule must agree with the normative Python reference.


def _invalid_stamp_names() -> list[str]:
    """Names that look like backups but carry no usable stamp."""
    return [
        "stratum-manual.dump",            # foreign / hand-made dump
        "stratum-20261332-250000.dump",   # month 13, day 32, hour 25 -> impossible
        "stratum-20260101-000000.dump.bak",  # trailing junk after the suffix
        "stratum-2026-01-01-000000.dump",    # wrong stamp width
        "vault-manual.tar.gz",
        "vault-20260230-000000.tar.gz",   # Feb 30 does not exist
        "README.md",
        "backup.log",
    ]


def _stamped(count: int, *, prefix: str = "stratum-", suffix: str = ".dump",
             start_day: int = 1) -> list[str]:
    # Deterministic ascending stamps: day 01..NN at midnight.
    return [f"{prefix}202601{start_day + i:02d}-000000{suffix}" for i in range(count)]


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_prune_ignores_foreign_and_invalid_names(tmp_path) -> None:
    # Five valid dumps plus a pile of foreign / invalid-stamp names, keep=2.
    # Exactly the three oldest *valid* dumps may be pruned; every invalid name
    # must be left untouched, and the newest two valid dumps must survive.
    target = tmp_path / "backups"
    target.mkdir()
    valid = _stamped(5)
    invalid = _invalid_stamp_names()
    for name in valid + invalid:
        (target / name).write_bytes(b"x")

    env = {"PATH": ""}
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", "2"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log = result.stderr

    # The three oldest valid dumps are planned for removal.
    assert "retention stratum-*.dump: removed 3, kept 2" in log, log
    # Neither the newest valid dumps nor any invalid name is ever scheduled.
    for kept in valid[3:]:
        assert kept not in log, f"newest valid dump scheduled for removal: {kept}"
    for junk in invalid:
        assert junk not in log, f"foreign/invalid name scheduled for removal: {junk}"
    # Dry run: nothing was actually deleted.
    assert all((target / name).is_file() for name in valid + invalid)


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_prune_parity_with_python_reference(tmp_path) -> None:
    # The shell prune plan (dry run) must equal the normative Python selection
    # for names the two contracts can agree on.
    target = tmp_path / "backups"
    target.mkdir()
    valid = _stamped(6)
    invalid = _invalid_stamp_names()
    for name in valid + invalid:
        (target / name).write_bytes(b"x")

    keep = 2
    # The shell contract is *anchored* (the whole name must be a canonical
    # backup), so e.g. `stratum-20260101-000000.dump.bak` is intentionally not a
    # prune candidate even though backup_lib's unanchored stamp search would
    # match it. Parity is therefore asserted on canonical names; the deliberate
    # stricter-suffix behaviour is covered by its own test below.
    canonical = re.compile(r"^(?:stratum-\d{8}-\d{6}\.dump|vault-\d{8}-\d{6}\.tar\.gz)$")
    agreed = [n for n in valid + invalid if canonical.match(n)]
    expected = backup_lib.select_backups_to_prune(agreed, keep=keep)

    env = {"PATH": ""}
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", str(keep)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log = result.stderr

    scheduled = sorted(
        n for n in agreed
        if any("rm -f " in line and line.rstrip().endswith(n) for line in log.splitlines())
    )
    assert scheduled == sorted(expected), (
        f"shell plan {scheduled} != python plan {sorted(expected)}\n{log}"
    )
    # And the reference itself never returns a foreign / invalid-stamp name here.
    for junk in ("stratum-manual.dump", "stratum-20261332-250000.dump",
                 "vault-manual.tar.gz", "vault-20260230-000000.tar.gz",
                 "README.md", "backup.log"):
        assert junk not in expected


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_prune_suffix_is_anchored(tmp_path) -> None:
    # A file whose stamp is valid but whose name is not a canonical backup
    # (`...dump.bak`, `...dump.tmp`) is never pruned by the shell: the archived
    # name must end in exactly the documented suffix. Stricter than the
    # unanchored reference, and therefore data-safe.
    target = tmp_path / "backups"
    target.mkdir()
    names = [
        "stratum-20260101-000000.dump.bak",
        "stratum-20260102-000000.dump.tmp",
        "stratum-20260103-000000.dump",
    ]
    for name in names:
        (target / name).write_bytes(b"x")

    env = {"PATH": ""}
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", "0"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log = result.stderr
    # Only the one canonical dump is pruned; the .bak/.tmp near-misses survive.
    assert "retention stratum-*.dump: removed 1, kept 0" in log, log
    assert "stratum-20260101-000000.dump.bak" not in log
    assert "stratum-20260102-000000.dump.tmp" not in log


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_backup_sh_keep_zero_prunes_only_valid_stamped(tmp_path) -> None:
    # keep=0 must delete every *valid* archive but never a foreign/invalid name.
    target = tmp_path / "backups"
    target.mkdir()
    valid = _stamped(2)
    invalid = _invalid_stamp_names()
    for name in valid + invalid:
        (target / name).write_bytes(b"x")

    env = {"PATH": ""}
    result = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "backup.sh"), "--dry-run", "--dir", str(target), "--keep", "0"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log = result.stderr
    assert f"retention stratum-*.dump: removed {len(valid)}, kept 0" in log, log
    for junk in invalid:
        assert junk not in log


def test_backup_sh_dump_magic_guard_present() -> None:
    # F2: the dump step verifies the pg_dump custom-format magic instead of
    # trusting a merely non-empty file, so a truncated dump cannot be finalised.
    text = (SCRIPTS_DIR / "backup.sh").read_text(encoding="utf-8")
    assert "PGDMP" in text, "backup.sh must check the pg_dump custom-format magic"


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
def test_restore_sh_refuses_force_into_production(tmp_path) -> None:
    # F4: force-restoring into the production db 'stratum' is refused unless
    # STRATUM_RESTORE_ALLOW_PROD=1 is set.
    dump = tmp_path / "stratum-20260101-000000.dump"
    dump.write_bytes(b"PGDMP fake payload")

    env = dict(os.environ)
    env.pop("STRATUM_RESTORE_ALLOW_PROD", None)
    refused = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "restore.sh"), "--dump", str(dump),
         "--target-db", "stratum", "--force"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert refused.returncode == 1, refused.stderr
    assert "REFUSING" in refused.stderr

    # A non-production target database is not blocked (proceeds to the verify/
    # docker stage, i.e. it is not rejected with a usage error here).
    other = subprocess.run(
        [BASH, str(SCRIPTS_DIR / "restore.sh"), "--dump", str(dump),
         "--target-db", "stratum_drill", "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert "REFUSING" not in other.stderr


# ============================================================ powershell parsing

@pytest.mark.skipif(POWERSHELL is None, reason="no pwsh / Windows PowerShell")
@pytest.mark.parametrize("name", POWERSHELL_SCRIPTS)
def test_powershell_scripts_parse(name: str) -> None:
    path = SCRIPTS_DIR / name
    script = (
        "$ErrorActionPreference='Stop';"
        f"$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{path}',$([ref]$tokens),$([ref]$errors));"
        "if($errors.Count -gt 0){$errors|ForEach-Object{Write-Error $_.Message};exit 1};"
        "exit 0"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"parse {name} failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.skipif(POWERSHELL is None, reason="no pwsh / Windows PowerShell")
def test_backup_ps1_dry_run_exits_zero(tmp_path) -> None:
    target = tmp_path / "backups"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPTS_DIR / "backup.ps1"),
            "-Dir",
            str(target),
            "-Keep",
            "5",
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"ps1 dry run failed:\n{result.stdout}\n{result.stderr}"
    assert "Stratum backup" in result.stdout or "Stratum backup" in result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="no pwsh / Windows PowerShell")
def test_restore_ps1_requires_target_db_without_verify() -> None:
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPTS_DIR / "restore.ps1"),
            "-Dump",
            "whatever.dump",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "TargetDb" in combined


# ====================================================== static name-contract scan


@pytest.mark.parametrize("name", ("backup.sh", "restore.sh", "backup.ps1", "restore.ps1"))
def test_dump_name_pattern_present_in_scripts(name: str) -> None:
    # The scripts document the plain-regex contract verbatim, so a literal
    # substring check (not a regex search) is the right assertion here.
    text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert DUMP_NAME_PATTERN in text, f"{name} must reference {DUMP_NAME_PATTERN}"


def test_vault_name_pattern_present_in_scripts() -> None:
    for name in ("backup.sh", "backup.ps1"):
        text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        assert VAULT_NAME_PATTERN in text, f"{name} must reference {VAULT_NAME_PATTERN}"


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_are_strict(name: str) -> None:
    text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, f"{name} must use strict shell mode"


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_scripts_never_embed_secrets(name: str) -> None:
    text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    for marker in ("password=", "PASSWORD=", "pg_password", "api_key="):
        assert marker not in text, f"{name} must not embed secret material ({marker!r})"


# ================================================================ retention lib


class TestRetentionRule:
    """Exhaustive coverage of `backup_lib.select_backups_to_prune`."""

    @staticmethod
    def _stamped(prefix: str, suffix: str, count: int) -> list[str]:
        # Deterministic, ascending timestamps: 2026-01-01 00:00:01 .. 00:00:0N.
        return [f"{prefix}20260101-0000{i:02d}{suffix}" for i in range(1, count + 1)]

    def test_keeps_newest_n(self) -> None:
        names = self._stamped("stratum-", ".dump", 20)
        prune = backup_lib.select_backups_to_prune(names, keep=14)
        assert len(prune) == 6
        # The six oldest are pruned, oldest first.
        assert prune == names[:6]

    def test_nothing_pruned_when_at_or_below_keep(self) -> None:
        names = self._stamped("stratum-", ".dump", 14)
        assert backup_lib.select_backups_to_prune(names, keep=14) == []
        assert backup_lib.select_backups_to_prune(names[:3], keep=14) == []

    def test_keep_zero_prunes_all_stamped(self) -> None:
        names = self._stamped("vault-", ".tar.gz", 5)
        prune = backup_lib.select_backups_to_prune(names, keep=0)
        assert prune == names  # oldest first

    def test_order_is_oldest_first(self) -> None:
        names = self._stamped("stratum-", ".dump", 10)
        prune = backup_lib.select_backups_to_prune(names, keep=3)
        assert prune == names[:7]
        # Strictly ascending timestamps.
        stamps = [backup_lib.backup_timestamp(n) for n in prune]
        assert stamps == sorted(stamps)

    def test_ignores_names_without_valid_stamp(self) -> None:
        names = [
            "README.md",
            "backup.log",
            "stratum-manual.dump",
            # Syntactically matching but impossible dates -> never pruned.
            "stratum-20261332-250000.dump",
            "vault-20260101-000000.tar.gz",
        ]
        assert backup_lib.select_backups_to_prune(names, keep=0) == ["vault-20260101-000000.tar.gz"]

    def test_unsorted_input_is_handled_by_timestamp_not_order(self) -> None:
        names = [
            "stratum-20260103-000000.dump",
            "stratum-20260101-000000.dump",
            "stratum-20260102-000000.dump",
        ]
        prune = backup_lib.select_backups_to_prune(names, keep=1)
        assert prune == ["stratum-20260101-000000.dump", "stratum-20260102-000000.dump"]

    def test_duplicate_names_are_not_double_counted(self) -> None:
        names = ["stratum-20260101-000000.dump"] * 3 + ["stratum-20260102-000000.dump"]
        # Two distinct archives, keep one -> exactly the older one is pruned.
        assert backup_lib.select_backups_to_prune(names, keep=1) == ["stratum-20260101-000000.dump"]

    def test_empty_input(self) -> None:
        assert backup_lib.select_backups_to_prune([], keep=14) == []

    def test_per_family_invocation_keeps_n_of_each(self) -> None:
        # The scripts count per family: each call sees only its own family's
        # names (backup.sh `prune_family`, backup.ps1 `Invoke-Retention`).
        dumps = self._stamped("stratum-", ".dump", 4)
        vaults = self._stamped("vault-", ".tar.gz", 2)
        prune = backup_lib.select_backups_to_prune(
            dumps, keep=2
        ) + backup_lib.select_backups_to_prune(vaults, keep=2)
        # Only the two oldest dumps exceed the budget of 2; the two vaults fit.
        assert prune == dumps[:2]
        assert all(p.startswith("stratum-") for p in prune)

    def test_global_budget_when_families_are_combined(self) -> None:
        # If a caller passes one combined listing, the budget is global (this is
        # the function's contract; the scripts deliberately call it per family).
        dumps = self._stamped("stratum-", ".dump", 3)
        prune = backup_lib.select_backups_to_prune(dumps, keep=2)
        assert len(prune) == 1
        assert prune == dumps[:1]

    def test_negative_keep_rejected(self) -> None:
        with pytest.raises(ValueError):
            backup_lib.select_backups_to_prune(["stratum-20260101-000000.dump"], keep=-1)

    def test_non_int_keep_rejected(self) -> None:
        with pytest.raises(TypeError):
            backup_lib.select_backups_to_prune([], keep="14")  # type: ignore[arg-type]

    def test_backup_timestamp_parses_and_rejects(self) -> None:
        from datetime import datetime

        assert backup_lib.backup_timestamp("stratum-20260228-235959.dump") == datetime(
            2026, 2, 28, 23, 59, 59
        )
        # Leap day is valid in 2024...
        assert backup_lib.backup_timestamp("vault-20240229-010101.tar.gz") is not None
        # ...but 2023 is not a leap year.
        assert backup_lib.backup_timestamp("vault-20230229-010101.tar.gz") is None
        assert backup_lib.backup_timestamp("no-stamp-here") is None


def test_dump_name_regex_matches_generated_stamp() -> None:
    # The stamp format the scripts use (strftime %Y%m%d-%H%M%S) must satisfy the
    # documented dump-name contract.
    from datetime import datetime

    stamp = datetime(2026, 10, 11, 3, 4, 5).strftime("%Y%m%d-%H%M%S")
    assert re.fullmatch(DUMP_NAME_PATTERN, f"stratum-{stamp}.dump")
    assert re.fullmatch(VAULT_NAME_PATTERN, f"vault-{stamp}.tar.gz")
