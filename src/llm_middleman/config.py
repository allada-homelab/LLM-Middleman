"""Application settings (pydantic-settings)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str) -> str:
    """Read a Docker secret mounted at /run/secrets/<name> (empty if absent).

    Secrets-as-files keeps sensitive values out of the environment and image
    layers; compose mounts them under /run/secrets. Env vars still override.
    """
    path = Path("/run/secrets") / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


class Settings(BaseSettings):
    """Runtime configuration, read from the environment / .env / Docker secrets."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_MIDDLEMAN_",
        env_file=".env",
        extra="ignore",
    )

    debug: bool = False
    # Gate heavy startup work so tests can construct the app without it.
    init_backend: bool = True
    # Example secret: LLM_MIDDLEMAN_APP_SECRET env wins; otherwise the
    # Docker secret file /run/secrets/app_secret is used; otherwise empty.
    app_secret: str = Field(default_factory=lambda: _read_secret("app_secret"))


def get_settings() -> Settings:
    return Settings()
