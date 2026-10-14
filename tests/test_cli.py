"""Tests for the ``stratum`` CLI distribution commands (``docs/distribution.md``).

These tests are **database-free, Docker-free and network-free**: every Docker
call is intercepted by monkeypatching :func:`subprocess.run` (and
:func:`shutil.which`), every HTTP probe by monkeypatching the module-level
``urlopen``. What is verified is exactly the contract the CLI promises:

* ``version`` prints ``stratum <__version__>``,
* ``install`` writes ``docker-compose.yml`` + ``.env`` + ``secrets/`` into the
  requested directory, idempotently (``--force`` to overwrite),
* ``up`` / ``down`` / ``status`` / ``logs`` shell out to ``docker compose`` with
  the expected argument list *in the install directory*,
* a missing ``docker`` binary is reported as exit code 2 instead of a traceback,
* ``doctor`` never crashes and exits 0 or 1.

The embedded Compose template is also checked structurally with PyYAML when it is
importable (it is a dev extra; the check is skipped otherwise).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

import stratum
from stratum import cli

# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #

IMAGE = "ghcr.io/totalbassjunkie/stratum-memory:latest"


class _Recorder:
    """A :func:`subprocess.run` double that records every call."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)

    @property
    def commands(self) -> list[list[str]]:
        return [command for command, _ in self.calls]


class _FakeResponse:
    """Minimal ``urlopen`` context manager returning a fixed JSON body."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Patch ``subprocess.run`` (and a present ``docker``) with a recorder."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    return rec


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``status`` health probe fail fast instead of dialling a socket."""

    def _boom(*_args: object, **_kwargs: object) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "urlopen", _boom)


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """A directory with a fresh ``stratum install`` already applied."""
    assert cli.main(["install", "--dir", str(tmp_path)]) == 0
    return tmp_path


def _write_marker(path: Path, marker: str) -> None:
    path.write_text(marker, encoding="utf-8")


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"stratum {stratum.__version__}"


def test_version_matches_dunder_version(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["version"])
    printed = capsys.readouterr().out.split()
    assert printed == ["stratum", stratum.__version__]


def test_dash_dash_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert stratum.__version__ in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #


def test_install_writes_stack_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", "--dir", str(tmp_path)]) == 0

    compose = tmp_path / "docker-compose.yml"
    env_file = tmp_path / ".env"
    secrets = tmp_path / "secrets"

    assert compose.is_file()
    assert env_file.is_file()
    assert secrets.is_dir()
    for name in ("pg_password", "app_password", "llm_key"):
        secret = secrets / name
        assert secret.is_file(), f"missing secret placeholder {name}"
        assert secret.read_text(encoding="utf-8") == "", f"{name} must start empty"

    capsys.readouterr()


def test_install_uses_official_image_by_default(tmp_path: Path) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    assert IMAGE in (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")


def test_install_image_is_configurable(tmp_path: Path) -> None:
    cli.main(["install", "--dir", str(tmp_path), "--image", "example.test/stratum:dev"])
    text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert "example.test/stratum:dev" in text
    assert IMAGE not in text


def test_install_env_file_carries_no_secret(tmp_path: Path) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "STRATUM_BIND_IP=127.0.0.1" in text
    # Credentials live in ./secrets/, never in the environment file.
    for forbidden in ("PASSWORD", "API_KEY=", "pg_password", "app_password"):
        assert forbidden not in text


def test_install_compose_mounts_the_secret_files(tmp_path: Path) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("pg_password", "app_password", "llm_key"):
        assert f"./secrets/{name}" in text


def test_install_secrets_are_private_where_posix(tmp_path: Path) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    if os.name != "posix":
        pytest.skip("POSIX permission bits are not enforced on this platform")
    for name in ("pg_password", "app_password", "llm_key"):
        mode = (tmp_path / "secrets" / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"


def test_install_second_run_without_force_keeps_files(installed: Path) -> None:
    compose = installed / "docker-compose.yml"
    env_file = installed / ".env"
    _write_marker(compose, "SENTINEL-COMPOSE")
    _write_marker(env_file, "SENTINEL-ENV")

    assert cli.main(["install", "--dir", str(installed)]) == 0

    assert compose.read_text(encoding="utf-8") == "SENTINEL-COMPOSE"
    assert env_file.read_text(encoding="utf-8") == "SENTINEL-ENV"


def test_install_second_run_does_not_clobber_filled_secret(installed: Path) -> None:
    secret = installed / "secrets" / "app_password"
    secret.write_text("already-filled", encoding="utf-8")

    cli.main(["install", "--dir", str(installed)])

    assert secret.read_text(encoding="utf-8") == "already-filled"


def test_install_force_overwrites_existing_files(installed: Path) -> None:
    compose = installed / "docker-compose.yml"
    secret = installed / "secrets" / "pg_password"
    _write_marker(compose, "SENTINEL-COMPOSE")
    secret.write_text("stale", encoding="utf-8")

    assert cli.main(["install", "--dir", str(installed), "--force"]) == 0

    text = compose.read_text(encoding="utf-8")
    assert "SENTINEL-COMPOSE" not in text
    assert IMAGE in text
    assert secret.read_text(encoding="utf-8") == ""


def test_install_prints_next_steps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Next steps" in out
    assert "stratum up" in out
    assert str(tmp_path) in out


def test_install_reports_written_and_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["install", "--dir", str(tmp_path)])
    first = capsys.readouterr().out
    cli.main(["install", "--dir", str(tmp_path)])
    second = capsys.readouterr().out

    assert "wrote" in first
    assert "kept" in second
    assert "nothing to do" in second


def test_install_respects_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "stack"
    assert cli.main(["install", "--dir", str(target)]) == 0
    assert (target / "docker-compose.yml").is_file()
    assert not (tmp_path / "docker-compose.yml").exists()


def test_install_default_dir_is_dot_stratum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["install"]) == 0
    assert (tmp_path / ".stratum" / "docker-compose.yml").is_file()


# --------------------------------------------------------------------------- #
# up / down
# --------------------------------------------------------------------------- #


def test_up_runs_compose_up_detached(installed: Path, recorder: _Recorder) -> None:
    assert cli.main(["up", "--dir", str(installed)]) == 0
    assert recorder.commands == [["docker", "compose", "up", "-d"]]
    assert recorder.calls[0][1]["cwd"] == str(installed)
    assert recorder.calls[0][1]["check"] is False


def test_down_runs_compose_down(installed: Path, recorder: _Recorder) -> None:
    assert cli.main(["down", "--dir", str(installed)]) == 0
    assert recorder.commands == [["docker", "compose", "down"]]


def test_up_propagates_compose_exit_code(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _Recorder(returncode=17))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cli.main(["up", "--dir", str(installed)]) == 17


def test_up_without_install_dir_fails_before_docker(
    tmp_path: Path, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent"
    assert cli.main(["up", "--dir", str(missing)]) == 1
    assert recorder.calls == []
    assert "stratum install" in capsys.readouterr().err


def test_up_without_docker_is_exit_two(
    installed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("docker must not be invoked when it is missing")

    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", _fail)

    assert cli.main(["up", "--dir", str(installed)]) == 2
    assert "docker" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("command", ["up", "down", "status", "logs"])
def test_every_stack_command_needs_docker(
    command: str,
    installed: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All four stack commands report a friendly exit 2 when Docker is absent."""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("docker must not be invoked when it is missing")

    monkeypatch.setattr(subprocess, "run", _fail)

    assert cli.main([command, "--dir", str(installed)]) == 2
    assert "docker" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_runs_compose_ps(installed: Path, recorder: _Recorder, offline: None) -> None:
    assert cli.main(["status", "--dir", str(installed)]) == 0
    assert recorder.commands == [["docker", "compose", "ps"]]


def test_status_prints_health_json(
    installed: Path,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = b'{"status": "ok", "version": "0.1.0"}'
    monkeypatch.setattr(cli, "urlopen", lambda *_a, **_kw: _FakeResponse(payload))

    assert cli.main(["status", "--dir", str(installed)]) == 0
    out = capsys.readouterr().out
    assert "Health probe:" in out
    assert '"status": "ok"' in out
    assert '"version": "0.1.0"' in out


def test_status_survives_unreachable_health_endpoint(
    installed: Path, recorder: _Recorder, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["status", "--dir", str(installed)]) == 0
    out = capsys.readouterr().out
    assert "unreachable" in out
    assert "Containers" in out


def test_status_returns_compose_exit_code(
    installed: Path, monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    monkeypatch.setattr(subprocess, "run", _Recorder(returncode=1))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cli.main(["status", "--dir", str(installed)]) == 1


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #


def test_logs_uses_default_tail(installed: Path, recorder: _Recorder) -> None:
    assert cli.main(["logs", "--dir", str(installed)]) == 0
    assert recorder.commands == [["docker", "compose", "logs", "--tail", "200"]]


def test_logs_accepts_service_and_tail(installed: Path, recorder: _Recorder) -> None:
    assert cli.main(["logs", "stratum-core", "--dir", str(installed), "-n", "25"]) == 0
    assert recorder.commands == [["docker", "compose", "logs", "--tail", "25", "stratum-core"]]


def test_logs_respects_dir(installed: Path, recorder: _Recorder) -> None:
    cli.main(["logs", "--dir", str(installed), "-n", "5"])
    assert recorder.calls[0][1]["cwd"] == str(installed)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def _stub_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "_check_port", lambda *a, **kw: cli._Check("port 8088 free", True, "stubbed")
    )


def test_doctor_exits_zero_when_everything_is_fine(
    installed: Path,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_port(monkeypatch)
    assert cli.main(["doctor", "--dir", str(installed)]) == 0
    out = capsys.readouterr().out
    assert "Stratum-Memory doctor" in out
    assert "4/4 checks passed" in out


def test_doctor_exits_one_without_docker(
    installed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_port(monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli.main(["doctor", "--dir", str(installed)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "3/4 checks passed" in out


def test_doctor_flags_missing_install_dir(
    tmp_path: Path,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_port(monkeypatch)
    assert cli.main(["doctor", "--dir", str(tmp_path / "absent")]) == 1
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "stratum install" in out


def test_doctor_never_crashes_and_exits_0_or_1(
    tmp_path: Path, recorder: _Recorder, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever the environment looks like, ``doctor`` reports instead of raising."""
    code = cli.main(["doctor", "--dir", str(tmp_path)])
    assert code in (0, 1)
    assert "checks passed" in capsys.readouterr().out


def test_doctor_python_check_reports_version(recorder: _Recorder) -> None:
    check = cli._check_python()
    assert check.name.startswith("Python >=")
    assert str(check.ok) in ("True", "False")
    assert check.detail


def test_doctor_port_check_detects_a_bound_port(recorder: _Recorder) -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        host, port = sock.getsockname()
        check = cli._check_port(host, port)
    assert check.ok is False
    assert "in use" in check.detail


# --------------------------------------------------------------------------- #
# embedded compose template
# --------------------------------------------------------------------------- #


def test_embedded_compose_is_valid_yaml(installed: Path) -> None:
    yaml = pytest.importorskip("yaml", reason="PyYAML is a dev extra")
    text = (installed / "docker-compose.yml").read_text(encoding="utf-8")
    config = yaml.safe_load(text)

    assert set(config["services"]) == {"stratum-db", "stratum-core", "stratum-worker"}
    assert config["services"]["stratum-core"]["image"] == IMAGE
    assert config["services"]["stratum-worker"]["image"] == IMAGE
    assert config["services"]["stratum-core"]["ports"] == [
        "${STRATUM_BIND_IP:-127.0.0.1}:8088:8088"
    ]


def test_embedded_compose_has_no_inline_secret_values() -> None:
    """No ``PASSWORD: <value>`` / ``API_KEY: <value>`` assignment in the template.

    A ``secrets:`` *declaration* (``pg_password:`` followed by ``file: ...``) is a
    name, not key material, so only an inline assignment to an uppercase env-var
    key is forbidden.
    """
    template = cli._COMPOSE_TEMPLATE
    assert re.search(r"(?m)^\s*[A-Z][A-Z0-9_]*PASSWORD\s*:\s*\S", template) is None
    assert re.search(r"(?m)^\s*[A-Z][A-Z0-9_]*API_KEY\s*:\s*\S", template) is None
    assert "_FILE" in template, "credentials must be mounted as *_FILE secrets"


def test_embedded_compose_secret_pointers_are_files(installed: Path) -> None:
    yaml = pytest.importorskip("yaml", reason="PyYAML is a dev extra")
    config = yaml.safe_load((installed / "docker-compose.yml").read_text(encoding="utf-8"))

    secrets = config["secrets"]
    assert set(secrets) == {"pg_password", "app_password", "llm_key"}
    for name, declaration in secrets.items():
        assert declaration["file"] == f"./secrets/{name}"

    for service_name, service in config["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if re.search(r"(?i)password|secret|api_?key", key):
                assert key.endswith("_FILE"), f"{service_name}: {key} must be a *_FILE pointer"
                assert str(value).startswith("/run/secrets/")


def test_embedded_env_has_no_secret() -> None:
    assert "password" not in cli._ENV_TEMPLATE.lower()
