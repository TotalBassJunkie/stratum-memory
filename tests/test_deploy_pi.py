"""Tests for the WP-14 Raspberry Pi deployment profile.

These tests are deliberately **DB-free and side-effect-free**: nothing connects to
Postgres, nothing runs ``docker`` and nothing writes to ``/etc``. They validate the
operational artifacts in :mod:`deploy.pi`:

* the Compose override parses and carries the exact §10/ADR-0009 RAM limits,
* ``stratum-db`` is genuinely swap-free (``memswap_limit == mem_limit``) while
  core/worker may swap,
* the SSD bind mounts override the *same container targets* the base compose
  declares as named volumes (Compose merge semantics),
* the shell scripts pass ``bash -n`` (skipped when no working bash is on PATH),
* the sysctl / zram values are present and correct,
* **no microSD swap** is configured anywhere (boot medium only).

The override is parsed with a tiny, dependency-free YAML subset parser because the
CI venv does not ship PyYAML; the Compose override uses only that subset (nested
mappings + scalar lists).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "pi"
OVERRIDE = DEPLOY_DIR / "docker-compose.override.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
SWAP_SETUP = DEPLOY_DIR / "swap-setup.sh"
SETUP_PI = DEPLOY_DIR / "setup-pi.sh"
ZRAM_CONF = DEPLOY_DIR / "zram-config.conf"
SYSCTL_CONF = DEPLOY_DIR / "sysctl-stratum.conf"
SYSTEMD_UNIT = DEPLOY_DIR / "stratum-host-bootstrap.service"
README = DEPLOY_DIR / "README.md"

SHELL_SCRIPTS = (SWAP_SETUP, SETUP_PI)


# --------------------------------------------------------- minimal YAML subset


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing `` # ...`` comment from an unquoted scalar."""
    if value[:1] in ("'", '"'):
        return value
    marker = value.find(" #")
    return value[:marker].rstrip() if marker != -1 else value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _parse(lines: list[str], start: int, min_indent: int):
    """Parse one block starting at/after *start*; return ``(value, next_index)``."""
    idx = start
    while idx < len(lines):
        raw = lines[idx]
        if raw.strip() and not raw.lstrip().startswith("#"):
            break
        idx += 1
    if idx >= len(lines):
        return None, idx

    block_indent = _indent(lines[idx])
    if block_indent < min_indent:
        return None, idx
    if lines[idx].strip().startswith("- "):
        return _parse_list(lines, idx, block_indent)
    return _parse_map(lines, idx, block_indent)


def _parse_map(lines: list[str], idx: int, indent: int):
    result: dict[str, object] = {}
    while idx < len(lines):
        raw = lines[idx]
        if not raw.strip() or raw.lstrip().startswith("#"):
            idx += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        if cur > indent:
            idx += 1
            continue
        content = raw.strip()
        key, sep, val = content.partition(":")
        if not sep:
            idx += 1
            continue
        key = key.strip()
        val = _strip_inline_comment(val.strip())
        if val == "":
            child, nxt = _parse(lines, idx + 1, cur + 1)
            result[key] = child if child is not None else {}
            idx = nxt
        else:
            result[key] = _scalar(val)
            idx += 1
    return result, idx


def _parse_list(lines: list[str], idx: int, indent: int):
    result: list[str] = []
    while idx < len(lines):
        raw = lines[idx]
        if not raw.strip() or raw.lstrip().startswith("#"):
            idx += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        content = raw.strip()
        if not content.startswith("- "):
            break
        result.append(_scalar(_strip_inline_comment(content[2:].strip())))
        idx += 1
    return result, idx


def parse_yaml(text: str):
    value, _ = _parse(text.splitlines(), 0, 0)
    return value


def _resolve_vars(entry: str) -> str:
    """Replace ``${VAR:-default}`` (which embeds a ``:``) with its default."""
    return re.sub(r"\$\{[A-Za-z_]+:-([^}]*)\}", r"\1", entry)


def volume_target(entry: str) -> str:
    """Container path a ``src:dst[:opts]`` mount targets."""
    return _resolve_vars(entry).split(":")[1]


def volume_source(entry: str) -> str:
    """Host path of a ``src:dst[:opts]`` mount, resolving ``${VAR:-default}``."""
    return _resolve_vars(entry).split(":")[0]


def parse_mem(value: str) -> int:
    """Parse a Compose memory string (``2g``/``1.5g``/``512mb``) to bytes."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]b?)?\s*", value, re.IGNORECASE)
    assert match, f"unparseable memory value: {value!r}"
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2,
               "g": 1024**3, "gb": 1024**3}
    return int(number * factors[unit])


# --------------------------------------------------------------- bash discovery


def _find_working_bash() -> str | None:
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


# ================================================================= file presence


@pytest.mark.parametrize(
    "path",
    (OVERRIDE, SWAP_SETUP, SETUP_PI, ZRAM_CONF, SYSCTL_CONF, SYSTEMD_UNIT, README),
)
def test_artifact_exists_and_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing artifact: {path}"
    assert path.stat().st_size > 0, f"empty artifact: {path}"


# ============================================================ compose override


def test_override_parses_and_has_expected_services() -> None:
    doc = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "override did not parse to a mapping"
    services = doc.get("services")
    assert isinstance(services, dict)
    # Three always-on services plus the profile-gated bulk importer.
    assert set(services) == {
        "stratum-db",
        "stratum-core",
        "stratum-worker",
        "stratum-import",
    }
    # The minimal YAML subset parser keeps inline lists as raw strings; accept
    # both a parsed list and the raw "[...]" form.
    profiles = services["stratum-import"].get("profiles")
    assert profiles in (["import"], '["import"]')


def test_override_has_no_legacy_version_key() -> None:
    # The Compose spec (docker compose v2) deprecates a top-level `version:`.
    doc = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))
    assert "version" not in doc


def test_db_is_swap_free_memswap_equals_memlimit() -> None:
    services = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))["services"]
    db = services["stratum-db"]
    assert parse_mem(db["mem_limit"]) == 2 * 1024**3, "db mem_limit must be 2g"
    assert parse_mem(db["memswap_limit"]) == parse_mem(db["mem_limit"]), (
        "stratum-db must have memswap_limit == mem_limit (no swap for Postgres)"
    )


def test_core_and_worker_limits_match_spec() -> None:
    services = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))["services"]
    core = services["stratum-core"]
    worker = services["stratum-worker"]
    assert parse_mem(core["mem_limit"]) == 1024**3, "core mem_limit must be 1g"
    assert parse_mem(worker["mem_limit"]) == int(1.5 * 1024**3), "worker mem_limit must be 1.5g"
    # Core and worker MAY swap -> their swap ceiling exceeds the memory limit.
    assert parse_mem(core["memswap_limit"]) > parse_mem(core["mem_limit"])
    assert parse_mem(worker["memswap_limit"]) > parse_mem(worker["mem_limit"])


@pytest.mark.parametrize("service", ("stratum-db", "stratum-core", "stratum-worker"))
def test_restart_policy_is_unless_stopped(service: str) -> None:
    services = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))["services"]
    assert services[service]["restart"] == "unless-stopped"


def test_ssd_bind_mounts_present() -> None:
    services = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))["services"]
    db_vols = services["stratum-db"]["volumes"]
    core_vols = services["stratum-core"]["volumes"]
    worker_vols = services["stratum-worker"]["volumes"]

    assert any(
        volume_source(v).endswith("/stratum/pgdata") and volume_target(v) == "/var/lib/postgresql"
        for v in db_vols
    ), "db pgdata must be a bind mount on the SSD (*/stratum/pgdata)"
    for vols in (core_vols, worker_vols):
        assert any(
            volume_source(v).endswith("/stratum/kb") and volume_target(v) == "/var/stratum/kb"
            for v in vols
        ), "kb vault must be a bind mount on the SSD (*/stratum/kb)"


def test_override_overrides_base_named_volumes_by_target() -> None:
    # Compose merges `volumes` by container target: the override bind mount must
    # target the same path the base declared as a named volume, otherwise the
    # named volume would still win.
    base = parse_yaml(BASE_COMPOSE.read_text(encoding="utf-8"))["services"]
    override = parse_yaml(OVERRIDE.read_text(encoding="utf-8"))["services"]

    base_targets = {
        "stratum-db": {volume_target(v) for v in base["stratum-db"].get("volumes", [])},
        "stratum-core": {volume_target(v) for v in base["stratum-core"].get("volumes", [])},
    }
    assert "/var/lib/postgresql" in base_targets["stratum-db"]
    assert "/var/stratum/kb" in base_targets["stratum-core"]

    db_override_targets = {volume_target(v) for v in override["stratum-db"]["volumes"]}
    core_override_targets = {volume_target(v) for v in override["stratum-core"]["volumes"]}
    assert "/var/lib/postgresql" in db_override_targets
    assert "/var/stratum/kb" in core_override_targets


def test_override_does_not_reference_microsd() -> None:
    text = OVERRIDE.read_text(encoding="utf-8")
    assert "mmcblk" not in text
    # Every *data mount* must live outside the microSD. Parse the volumes lists
    # from the YAML (so extra_hosts entries are not mistaken for mounts); host
    # sources are either absolute (/mnt/...) or ${STRATUM_DATA_ROOT:-/mnt/...}.
    doc = parse_yaml(text)
    for svc in doc["services"].values():
        for entry in svc.get("volumes", []) or []:
            src = volume_source(entry)
            assert src.startswith("/mnt/") or src.startswith("./") or src.startswith("../"), (
                f"data mount not on an SSD mount: {src}"
            )


# ================================================================ shell scripts


@pytest.mark.skipif(BASH is None, reason="no working bash on PATH")
@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_are_syntactically_valid(path: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"bash -n {path.name} failed:\n{result.stderr}"


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_use_strict_mode(path: Path) -> None:
    assert "set -euo pipefail" in path.read_text(encoding="utf-8")


# ============================================== setup-pi: bind-mount ownership


def test_setup_pi_chowns_kb_to_container_user() -> None:
    """The kb vault must be owned by the container user (UID/GID 10001) (B3).

    Core and worker run as ``stratum`` (``useradd -m -u 10001`` in the
    Dockerfile). A root-owned ``kb`` bind mount yields EACCES inside them.
    """
    text = SETUP_PI.read_text(encoding="utf-8")
    assert "STRATUM_UID=10001" in text, "setup-pi.sh must document the stratum UID"
    assert re.search(
        r"install -d -m 0755 -o \"\$STRATUM_UID\" -g \"\$STRATUM_GID\" \"\$DATA_ROOT/kb\"",
        text,
    ), "kb must be created with -o 10001 -g 10001"


def test_setup_pi_chowns_pgdata_to_postgres_user() -> None:
    """pgdata must be owned by the Postgres UID (999) (B3)."""
    text = SETUP_PI.read_text(encoding="utf-8")
    assert "POSTGRES_UID=999" in text, "setup-pi.sh must document the Postgres UID"
    assert re.search(
        r"install -d -m 0700 -o \"\$POSTGRES_UID\" -g \"\$POSTGRES_GID\" \"\$DATA_ROOT/pgdata\"",
        text,
    ), "pgdata must be created with -o 999 -g 999"


def test_setup_pi_ownership_matches_dockerfile_and_image() -> None:
    """Guard against drift: the UIDs in setup-pi.sh match the real ones."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "useradd -m -u 10001 stratum" in dockerfile, (
        "Dockerfile must keep creating the stratum user as UID 10001"
    )
    pi = SETUP_PI.read_text(encoding="utf-8")
    assert "STRATUM_UID=10001" in pi


# ======================================================= swap: SSD prio 10, zram


def test_swap_setup_defaults() -> None:
    text = SWAP_SETUP.read_text(encoding="utf-8")
    assert "/mnt/ssd/swapfile" in text, "SSD swapfile path must be /mnt/ssd/swapfile"
    assert "STRATUM_SWAP_PRIO:-10}" in text, "SSD swapfile priority must default to 10"
    assert "STRATUM_ZRAM_PRIO:-100}" in text, "zram priority must default to 100"
    assert "STRATUM_SWAP_SIZE_MB:-4096}" in text, "SSD swapfile must default to 4 GiB"
    assert "STRATUM_ZRAM_SIZE_MB:-1024}" in text, "zram must default to 1 GiB"


def test_swap_setup_activates_both_priorities() -> None:
    text = SWAP_SETUP.read_text(encoding="utf-8")
    # SSD swapfile activated at SWAP_PRIO (10) ...
    assert 'swapon --priority "$SWAP_PRIO"' in text
    # ... zram activated at ZRAM_PRIO (100).
    assert 'swapon --priority "$ZRAM_PRIO"' in text


def test_swap_setup_enforces_priority_when_already_active() -> None:
    """An already-active swap area must still get the desired priority (B5).

    ``swapon --priority`` is ignored on a live area, so merely skipping the
    activation leaves a stale priority. The script must cycle swapoff/swapon.
    """
    text = SWAP_SETUP.read_text(encoding="utf-8")
    assert "ensure_swap_priority" in text, (
        "swap-setup.sh must enforce the priority idempotently"
    )
    assert 'run swapoff "$target"' in text, (
        "already-active swap area must be cycled (swapoff) to re-apply priority"
    )
    # Called for the swapfile in the already-active branch.
    assert 'ensure_swap_priority "$SWAPFILE" "$SWAP_PRIO"' in text


def test_zram_conf_priority_and_size() -> None:
    text = ZRAM_CONF.read_text(encoding="utf-8")
    assert "PRIORITY=100" in text
    assert "SIZE=1024" in text
    assert "ALGO=" in text


def test_swap_setup_installs_sysctl_and_zram_defaults() -> None:
    text = SWAP_SETUP.read_text(encoding="utf-8")
    assert "/etc/sysctl.d/99-stratum.conf" in text
    assert "/etc/default/zramswap" in text
    assert "sysctl --system" in text


# ================================================================= sysctl values


def test_sysctl_conf_has_normative_values() -> None:
    text = SYSCTL_CONF.read_text(encoding="utf-8")
    assert re.search(r"vm\.swappiness\s*=\s*10\b", text), "vm.swappiness must be 10"
    assert re.search(r"vm\.vfs_cache_pressure\s*=\s*50\b", text), (
        "vm.vfs_cache_pressure must be 50"
    )
    assert re.search(r"vm\.overcommit_memory\s*=\s*0\b", text), "vm.overcommit_memory must be 0"


def test_swap_setup_fallback_sysctl_values() -> None:
    # The inline fallback (used when the conf file is absent next to the script)
    # must still write the normative values.
    text = SWAP_SETUP.read_text(encoding="utf-8")
    assert "vm.swappiness = 10" in text
    assert "vm.vfs_cache_pressure = 50" in text
    assert "vm.overcommit_memory = 0" in text


# ============================================================ no microSD swap


@pytest.mark.parametrize("path", (SWAP_SETUP, SETUP_PI, ZRAM_CONF, SYSCTL_CONF))
def test_no_microsd_swap_reference(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    # microSD block devices (mmcblk*) must never appear in the swap setup.
    assert "mmcblk" not in text, f"{path.name} must not reference a microSD device for swap"
    assert "/dev/root" not in text, f"{path.name} must not swap on the root/microSD device"


def test_swap_setup_forbids_microsd_in_comment() -> None:
    # The prohibition is documented explicitly (guard against silent regressions).
    text = SWAP_SETUP.read_text(encoding="utf-8").lower()
    assert "microsd" in text and ("never" in text or "kein" in text or "no " in text)


# ================================================================ systemd unit


def test_systemd_unit_shape() -> None:
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "ExecStart=/opt/stratum/deploy/pi/swap-setup.sh" in text
    assert "Before=docker.service" in text


def test_systemd_unit_requires_ssd_mount() -> None:
    """The host bootstrap must wait for the SSD (B6).

    Without ``RequiresMountsFor=/mnt/ssd`` the swapfile and every container bind
    mount would resolve against the microSD/root at boot time.
    """
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "RequiresMountsFor=/mnt/ssd" in text, (
        "host-bootstrap unit must declare RequiresMountsFor=/mnt/ssd"
    )


def test_systemd_unit_is_named_host_bootstrap() -> None:
    """The unit must not masquerade as a database unit (B6)."""
    assert SYSTEMD_UNIT.name == "stratum-host-bootstrap.service"
    assert not (DEPLOY_DIR / "stratum-db.service").exists(), (
        "the old stratum-db.service name (misleading) must not linger"
    )


# ====================================================================== README


def test_readme_documents_the_operating_steps() -> None:
    text = README.read_text(encoding="utf-8")
    # Compose invocation with both files.
    assert "docker-compose.yml" in text
    assert "deploy/pi/docker-compose.override.yml" in text
    # Bind order is not fixed by the shell, so assert the essential parts.
    assert "up -d" in text
    # Health check + stats.
    assert "pg_isready" in text
    assert "docker stats" in text
    assert "127.0.0.1:8088" in text
    # Troubleshooting: OOM and thrashing.
    assert "OOM" in text
    assert "Thrashing" in text or "thrashing" in text
    assert "vmstat" in text


def test_readme_mentions_expected_stats_budget() -> None:
    text = README.read_text(encoding="utf-8")
    for token in ("2 GiB", "1 GiB", "1.5 GiB"):
        assert token in text, f"README must state the {token} container budget"


def test_readme_documents_bind_mount_ownership() -> None:
    """B3: the README must document the container UIDs and the chown fallback."""
    text = README.read_text(encoding="utf-8")
    assert "10001" in text, "README must document the Core/Worker UID (10001)"
    assert "999" in text, "README must document the Postgres UID (999)"
    assert "chown -R 10001:10001" in text, "README must show the kb chown fix"


def test_readme_references_renamed_host_bootstrap_unit() -> None:
    """B6: the README must reference the renamed unit, not the old name."""
    text = README.read_text(encoding="utf-8")
    assert "stratum-host-bootstrap.service" in text
    assert "stratum-db.service" not in text


# ===================================================== sanity: parser self-check


def test_yaml_parser_roundtrips_nested_and_lists() -> None:
    sample = "\n".join(
        [
            "# comment",
            "services:",
            "  a:",
            "    mem_limit: 2g   # inline",
            "    volumes:",
            "      - pgdata:/var/lib/postgresql/data",
            "      - ./init:/docker-entrypoint-initdb.d:ro",
        ]
    )
    doc = parse_yaml(sample)
    assert doc["services"]["a"]["mem_limit"] == "2g"
    assert doc["services"]["a"]["volumes"] == [
        "pgdata:/var/lib/postgresql/data",
        "./init:/docker-entrypoint-initdb.d:ro",
    ]


def test_parse_mem_helper() -> None:
    assert parse_mem("2g") == 2 * 1024**3
    assert parse_mem("1.5g") == int(1.5 * 1024**3)
    assert parse_mem("512mb") == 512 * 1024**2
