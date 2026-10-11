"""Tests for the WP-14b server profile (``deploy/server/``).

These tests are deliberately **database-free and side-effect-free**: nothing
connects to Postgres and nothing starts a container. They statically verify the
server overlay and its reverse-proxy config:

* the Compose override parses (via :mod:`yaml`, or — when PyYAML is not
  importable in the environment — via ``docker compose config --format json``,
  which is an even stricter Compose-level parse),
* ``STRATUM_VECTOR_TYPE=vector`` is set on ``stratum-core`` **and**
  ``stratum-worker`` (server profile, no fp16; MASTER-PLAN §5.2/§10.5),
* the ``stratum-caddy`` service exists, publishes 80/443 and mounts the
  ``Caddyfile``,
* the Caddyfile reverse-proxies the MCP HTTP endpoint to ``stratum-core:8088``,
* the DB is tuned up (``shared_buffers``/``max_connections``),
* none of the shipped files contains a secret.

The repository root is derived from ``__file__`` so the tests run regardless of
the working directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "server"
OVERRIDE_PATH = DEPLOY_DIR / "docker-compose.override.yml"
CADDYFILE_PATH = DEPLOY_DIR / "Caddyfile"
README_PATH = DEPLOY_DIR / "README.md"
BASE_COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

#: The upstream the MCP HTTP endpoint is proxied to.
MCP_UPSTREAM = "stratum-core:8088"
#: FastMCP's default streamable-http path.
MCP_PATH = "/mcp"


# ============================================================ parse helpers


def _load_override_via_pyyaml() -> dict[str, Any] | None:
    """Parse the override file directly with PyYAML, or return ``None``."""
    try:
        import yaml  # noqa: PLC0415  (optional dependency at call time)
    except ModuleNotFoundError:
        return None
    with OVERRIDE_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _docker_available() -> bool:
    """Return whether a working ``docker`` CLI is on ``PATH``."""
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        probe = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _load_override_via_docker() -> dict[str, Any] | None:
    """Return the *merged* Compose config as a dict, or ``None``.

    This is a strict parseability proof: ``docker compose config`` fails loudly
    when the override introduces a schema error, an unknown service key or a
    malformed merge. The result is the base ⊕ override merge, which contains all
    the keys this module asserts on.
    """
    if not _docker_available():
        return None
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(BASE_COMPOSE_PATH),
                "-f",
                str(OVERRIDE_PATH),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


@pytest.fixture(scope="module")
def override_config() -> dict[str, Any]:
    """The parsed override, however it could be parsed best.

    Priority: PyYAML (reads the file as written) → ``docker compose config``
    (merged view). Skipped only when neither is available, which would make the
    parseability claim unverifiable rather than false.
    """
    config = _load_override_via_pyyaml()
    if isinstance(config, dict) and "services" in config:
        return config
    config = _load_override_via_docker()
    if isinstance(config, dict) and "services" in config:
        return config
    pytest.skip("neither PyYAML nor a working docker compose is available to parse the override")


def _services(config: dict[str, Any]) -> dict[str, Any]:
    services = config.get("services")
    assert isinstance(services, dict) and services, "override has no services mapping"
    return services


def _published_ports(service: dict[str, Any]) -> set[str]:
    """Normalize a Compose service's ``ports`` to the set of published values.

    Handles the long syntax (list of dicts with ``published``) as emitted by
    ``docker compose config --format json`` and the short syntax (``"80:80"``)
    as written in the file.
    """
    ports: set[str] = set()
    for entry in service.get("ports") or []:
        if isinstance(entry, dict):
            published = entry.get("published")
            if published is not None:
                ports.add(str(published))
        elif isinstance(entry, str):
            # "80:80" → published "80"; "80" → "80"
            left = entry.split(":")[0]
            ports.add(left)
    return ports


def _volume_mounts(service: dict[str, Any]) -> set[str]:
    """Normalize a service's ``volumes`` to the set of host-side sources.

    Returns both the raw ``source``/left-hand-side strings *and* the container
    ``target`` values so callers can assert on either.
    """
    mounts: set[str] = set()
    for entry in service.get("volumes") or []:
        if isinstance(entry, dict):
            source = entry.get("source")
            target = entry.get("target")
            if source:
                mounts.add(str(source))
            if target:
                mounts.add(str(target))
        elif isinstance(entry, str):
            mounts.add(entry)
            parts = entry.split(":")
            if len(parts) >= 2:
                mounts.add(parts[1])
    return mounts


# ============================================================ file presence


@pytest.mark.parametrize(
    "path",
    [OVERRIDE_PATH, CADDYFILE_PATH, README_PATH],
    ids=["override", "caddyfile", "readme"],
)
def test_deploy_files_exist_and_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing deploy artefact: {path}"
    assert path.stat().st_size > 0, f"empty deploy artefact: {path}"


def test_caddyfile_mount_uses_relative_deploy_path(override_config: dict[str, Any]) -> None:
    """The Caddyfile is mounted from ``deploy/server/`` and exists on disk."""
    caddy = _services(override_config)["stratum-caddy"]
    mounts = _volume_mounts(caddy)
    assert any("Caddyfile" in mount for mount in mounts), (
        f"Caddyfile is not mounted; volumes={caddy.get('volumes')!r}"
    )
    # And the mounted file must actually exist in the repo.
    assert CADDYFILE_PATH.is_file()


# ============================================================ parseability


def test_override_yaml_is_parseable(override_config: dict[str, Any]) -> None:
    """The override parses to a mapping with a non-empty ``services`` section."""
    assert isinstance(override_config, dict)
    assert isinstance(override_config.get("services"), dict)


# ============================================================ vector profile


def test_core_sets_vector_type_vector(override_config: dict[str, Any]) -> None:
    """Server profile must request full-precision ``vector``, never fp16."""
    core = _services(override_config)["stratum-core"]
    env = core.get("environment") or {}
    assert env.get("STRATUM_VECTOR_TYPE") == "vector", (
        "stratum-core must set STRATUM_VECTOR_TYPE=vector in the server profile; "
        f"got {env.get('STRATUM_VECTOR_TYPE')!r}"
    )


def test_worker_sets_vector_type_vector(override_config: dict[str, Any]) -> None:
    """The worker writes vectors too, so it must use the same profile as core.

    ``curator.py``/``graph.py`` cast the embedding column from
    ``Settings.vector_type``; a worker left on the ``halfvec`` default would
    write fp16 values against the server's ``vector(1024)`` column.
    """
    worker = _services(override_config)["stratum-worker"]
    env = worker.get("environment") or {}
    assert env.get("STRATUM_VECTOR_TYPE") == "vector", (
        "stratum-worker must set STRATUM_VECTOR_TYPE=vector in the server profile "
        "(it writes vectors via the Curator); "
        f"got {env.get('STRATUM_VECTOR_TYPE')!r}"
    )


def test_db_sets_vector_type_vector(override_config: dict[str, Any]) -> None:
    """The DB container must render the matching schema profile (B2).

    ``init/003_schema.sh`` reads ``STRATUM_VECTOR_TYPE`` at first-time init to
    choose ``vector(1024)`` over ``halfvec(1024)``; if the DB is left on the
    default, core/worker cast full-precision values against fp16 columns.
    """
    db = _services(override_config)["stratum-db"]
    env = db.get("environment") or {}
    assert env.get("STRATUM_VECTOR_TYPE") == "vector", (
        "stratum-db must set STRATUM_VECTOR_TYPE=vector in the server profile "
        f"(schema init); got {env.get('STRATUM_VECTOR_TYPE')!r}"
    )


def test_core_requires_api_key_by_default(override_config: dict[str, Any]) -> None:
    """B1: the publicly exposed server must gate ``/mcp`` behind an API key.

    The overlay defaults ``STRATUM_REQUIRE_API_KEY`` to ``1`` so the endpoint
    reachable through Caddy is never unauthenticated. The value is an ``${VAR:-1}``
    interpolation, so accept either the literal or the interpolation source.
    """
    core = _services(override_config)["stratum-core"]
    env = core.get("environment") or {}
    raw = str(env.get("STRATUM_REQUIRE_API_KEY", ""))
    assert raw, "stratum-core must set STRATUM_REQUIRE_API_KEY in the server profile"
    # ``docker compose config`` resolves the interpolation to an explicit value;
    # the raw file carries ``${STRATUM_REQUIRE_API_KEY:-1}``. Neither may be a
    # disabled (0/false) default.
    assert ":-0" not in raw and raw.strip().lower() not in {"0", "false", "no", "off"}, (
        f"STRATUM_REQUIRE_API_KEY must default to enabled (1); got {raw!r}"
    )


def test_worker_scans_tenants_by_default(override_config: dict[str, Any]) -> None:
    """B6: the worker's tenant list must be settable (RLS-scoped claim)."""
    worker = _services(override_config)["stratum-worker"]
    env = worker.get("environment") or {}
    raw = str(env.get("STRATUM_WORKER_TENANTS", ""))
    assert raw, "stratum-worker must expose STRATUM_WORKER_TENANTS in the server profile"


def test_core_uses_streamable_http(override_config: dict[str, Any]) -> None:
    """The resident server speaks streamable-http (ADR-0007 / §10.6)."""
    core = _services(override_config)["stratum-core"]
    env = core.get("environment") or {}
    assert env.get("STRATUM_MCP_TRANSPORT") == "streamable-http"


# ============================================================ db tuning


def _db_command(config: dict[str, Any]) -> str:
    db = _services(config)["stratum-db"]
    command = db.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def test_db_shared_buffers_raised(override_config: dict[str, Any]) -> None:
    assert "shared_buffers=4GB" in _db_command(override_config)


def test_db_effective_cache_size_raised(override_config: dict[str, Any]) -> None:
    assert "effective_cache_size=12GB" in _db_command(override_config)


def test_db_max_connections_raised(override_config: dict[str, Any]) -> None:
    assert "max_connections=100" in _db_command(override_config)


def test_db_stays_swapless_after_memory_raise(override_config: dict[str, Any]) -> None:
    """Raising the DB memory limit must keep ``memswap_limit == mem_limit``."""
    db = _services(override_config)["stratum-db"]
    assert db.get("mem_limit") is not None, (
        "override must re-declare mem_limit after raising shared_buffers"
    )
    assert db.get("memswap_limit") == db.get("mem_limit"), (
        "stratum-db must stay swapless (memswap_limit == mem_limit)"
    )


# ============================================================ caddy service


def test_caddy_service_present(override_config: dict[str, Any]) -> None:
    assert "stratum-caddy" in _services(override_config)


def test_caddy_uses_official_image(override_config: dict[str, Any]) -> None:
    caddy = _services(override_config)["stratum-caddy"]
    assert str(caddy.get("image", "")).startswith("caddy:2"), (
        f"stratum-caddy must use the caddy:2 image; got {caddy.get('image')!r}"
    )


def test_caddy_publishes_http_and_https(override_config: dict[str, Any]) -> None:
    caddy = _services(override_config)["stratum-caddy"]
    ports = _published_ports(caddy)
    assert "80" in ports, f"caddy must publish port 80; got {sorted(ports)}"
    assert "443" in ports, f"caddy must publish port 443; got {sorted(ports)}"


def test_caddy_depends_on_core(override_config: dict[str, Any]) -> None:
    caddy = _services(override_config)["stratum-caddy"]
    depends = caddy.get("depends_on") or {}
    assert "stratum-core" in depends, f"caddy must depend on stratum-core; got {depends!r}"


# ============================================================ caddyfile body


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    return CADDYFILE_PATH.read_text(encoding="utf-8")


def test_caddyfile_reverse_proxies_to_core(caddyfile_text: str) -> None:
    assert "reverse_proxy" in caddyfile_text, "Caddyfile must contain a reverse_proxy directive"
    assert MCP_UPSTREAM in caddyfile_text, f"Caddyfile must proxy to {MCP_UPSTREAM!r}"


def test_caddyfile_exposes_mcp_path(caddyfile_text: str) -> None:
    assert MCP_PATH in caddyfile_text, "Caddyfile must expose the /mcp endpoint"


def test_caddyfile_healthz_proxies_to_core(caddyfile_text: str) -> None:
    """B19: ``/healthz`` must proxy the real ``/health`` probe, not fake ``ok``."""
    assert "/healthz" in caddyfile_text, "Caddyfile must expose /healthz"
    # The old static reply must be gone, and the path must be proxied upstream.
    assert 'respond "ok" 200' not in caddyfile_text, (
        "/healthz must not return a static ok; it must reflect the real probe"
    )
    healthz_block = caddyfile_text.split("/healthz", 1)[1]
    assert "reverse_proxy" in healthz_block[:400], (
        "/healthz must reverse_proxy to the core /health endpoint"
    )


def test_caddyfile_sets_security_headers(caddyfile_text: str) -> None:
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in caddyfile_text, f"Caddyfile is missing security header {header!r}"


def test_caddyfile_domain_placeholder_is_reserved(caddyfile_text: str) -> None:
    """The domain must be an obvious placeholder, not a real host or a secret."""
    assert ".invalid" in caddyfile_text, "domain placeholder should use the reserved .invalid TLD"
    match = re.search(r"\{\$STRATUM_DOMAIN:([^}]+)\}", caddyfile_text)
    assert match, "Caddyfile should read the domain from $STRATUM_DOMAIN with a safe default"
    assert match.group(1).endswith(".invalid")


# ============================================================ secrets


#: Patterns that would indicate a leaked credential (not a *path* to one).
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    # A literal password/token assignment (as opposed to a *_FILE pointer).
    (
        "literal password",
        r"(?im)^\s*(?:POSTGRES_PASSWORD|STRATUM_DB_PASSWORD|DB_PASSWORD|PGPASSWORD)\s*[:=]\s*[^\s]",
    ),
    # A high-entropy blob (hex/base64) of 40+ chars — typical of leaked keys.
    ("long random token", r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)


@pytest.mark.parametrize(
    "path",
    [OVERRIDE_PATH, CADDYFILE_PATH, README_PATH],
    ids=["override", "caddyfile", "readme"],
)
def test_no_secrets_in_deploy_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for label, pattern in _SECRET_PATTERNS:
        match = re.search(pattern, text)
        assert match is None, f"possible {label} in {path.name}: {match.group(0)!r}"


def test_override_only_points_at_secret_files(override_config: dict[str, Any]) -> None:
    """Any password env var in the overlay must be a ``*_FILE`` pointer.

    ``STRATUM_REQUIRE_API_KEY`` is a boolean *toggle* (not key material), so it
    is the one allowed exception to the ``*_FILE`` rule.
    """
    non_secret_toggles = {"STRATUM_REQUIRE_API_KEY"}
    for name, service in _services(override_config).items():
        env = service.get("environment") or {}
        if not isinstance(env, dict):
            continue
        for key in env:
            if key in non_secret_toggles:
                continue
            if re.search(r"(?i)password|secret|api_?key", str(key)):
                assert str(key).endswith("_FILE"), (
                    f"{name}: {key} must be a *_FILE reference, not an inline secret"
                )
