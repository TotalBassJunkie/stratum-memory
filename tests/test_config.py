"""DB-free tests for :mod:`stratum.config` (B11 owner-DSN fix)."""

from __future__ import annotations

from pathlib import Path

from stratum.config import Settings


def test_dsn_app_role_uses_app_password(tmp_path: Path):
    pw_file = tmp_path / "app_password"
    pw_file.write_text("app-secret\n", encoding="utf-8")
    settings = Settings(
        db_user="stratum_app",
        db_owner_user="stratum",
        db_password_file=str(pw_file),
        db_host="db",
        db_port=5432,
        db_name="stratum",
    )
    assert settings.dsn() == "postgresql://stratum_app:app-secret@db:5432/stratum"


def test_dsn_owner_prefers_owner_password_file(tmp_path: Path):
    """B11: ``dsn(owner=True)`` must use the owner secret, not the app password."""
    app_file = tmp_path / "app_password"
    app_file.write_text("app-secret", encoding="utf-8")
    owner_file = tmp_path / "owner_password"
    owner_file.write_text("owner-secret", encoding="utf-8")
    settings = Settings(
        db_user="stratum_app",
        db_owner_user="stratum",
        db_password_file=str(app_file),
        db_owner_password_file=str(owner_file),
        db_host="db",
        db_name="stratum",
    )
    assert settings.resolved_owner_password() == "owner-secret"
    assert settings.dsn(owner=True) == "postgresql://stratum:owner-secret@db:5432/stratum"


def test_dsn_owner_falls_back_to_app_password(tmp_path: Path):
    """With no owner secret configured, a single-secret setup still works."""
    app_file = tmp_path / "app_password"
    app_file.write_text("only-secret", encoding="utf-8")
    settings = Settings(
        db_owner_user="stratum",
        db_password_file=str(app_file),
        db_owner_password_file=None,
        db_host="db",
        db_name="stratum",
    )
    assert settings.dsn(owner=True) == "postgresql://stratum:only-secret@db:5432/stratum"


def test_dsn_percent_encodes_credentials(tmp_path: Path):
    pw_file = tmp_path / "app_password"
    pw_file.write_text("p@ss/word:1", encoding="utf-8")
    settings = Settings(
        db_user="stratum_app",
        db_password_file=str(pw_file),
        db_host="db",
        db_name="stratum",
    )
    assert settings.dsn() == "postgresql://stratum_app:p%40ss%2Fword%3A1@db:5432/stratum"
