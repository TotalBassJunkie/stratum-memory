"""Configuration for Stratum-Memory, sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STRATUM_", extra="ignore")

    # Database (app role)
    db_user: str = "stratum_app"
    db_password: str | None = None
    db_password_file: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "stratum"

    # Migration/owner DSN (optional; owner/superuser role)
    db_owner_user: str = "stratum"
    db_owner_password_file: str | None = None

    # Vector type: "halfvec" (Pi default) or "vector" (Server)
    vector_type: str = "halfvec"
    embedding_dim: int = 1024

    # MCP transport
    mcp_transport: str = "stdio"  # stdio | streamable-http
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8088

    # Filesystem vault
    kb_path: str = "/var/stratum/kb"

    # Curator
    curator_max_promotions_per_run: int = 200
    dedup_threshold: float = 0.92
    min_confidence: float = 0.6

    # Reflect
    reflect_budget_tokens: int = 20000
    reflect_max_tokens: int = 50000
    reflect_events_trigger: int = 500

    # LLM (external, FreeLLMAPI-compatible)
    llm_base_url: str = ""
    llm_api_key: str | None = None
    llm_api_key_file: str | None = None
    llm_chat_model: str = "gpt-4o-mini"
    llm_embed_model: str = "text-embedding-3-small"

    # Optional separate endpoint for embeddings (e.g. a local Ollama with an
    # unlimited, 1024-dim model) so bulk embedding does not hit the chat
    # provider's rate limits. Empty => use ``llm_base_url`` for both.
    llm_embed_base_url: str = ""
    llm_embed_api_key: str | None = None
    llm_embed_api_key_file: str | None = None

    def resolved_db_password(self) -> str | None:
        return self.db_password or _read_secret(self.db_password_file)

    def resolved_owner_password(self) -> str | None:
        """Return the owner/owner DSN password.

        Prefers the dedicated owner secret when configured; when only the app
        password is set, falls back to it so a single-secret local setup keeps
        working. This is what :meth:`dsn` uses for ``owner=True`` (B11) — the
        ``db_owner_password_file`` setting is no longer dead config.
        """
        return _read_secret(self.db_owner_password_file) or self.resolved_db_password()

    def resolved_llm_api_key(self) -> str | None:
        return self.llm_api_key or _read_secret(self.llm_api_key_file)

    def resolved_llm_embed_api_key(self) -> str | None:
        explicit = self.llm_embed_api_key or _read_secret(self.llm_embed_api_key_file)
        if explicit:
            return explicit
        # Only fall back to the chat key when the embedding endpoint IS the chat
        # endpoint. A separate embed host (e.g. a local Ollama) must not receive
        # the chat provider's key.
        if self.llm_embed_base_url and self.llm_embed_base_url.rstrip("/") != self.llm_base_url.rstrip("/"):
            return None
        return self.resolved_llm_api_key()

    def dsn(self, owner: bool = False) -> str:
        user = self.db_owner_user if owner else self.db_user
        pwd = self.resolved_owner_password() if owner else self.resolved_db_password()
        # Percent-encode credentials: secrets are base64 by default and may
        # contain URI-unsafe characters (@ / ? # : [ ]) that would otherwise be
        # parsed as delimiters, corrupting the DSN (e.g. host == user).
        user_q = quote(user, safe="")
        auth = f"{user_q}:{quote(pwd, safe='')}" if pwd else user_q
        return f"postgresql://{auth}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
