"""Application configuration.

All settings come from the environment (or a local ``.env``). Nothing is hard-coded and no
secret is ever committed. Config files that describe *policy* rather than *deployment* — the
quality rubric, the vertical taxonomy, the city gazetteer — live as YAML under ``config/`` and
are located relative to :attr:`Settings.config_dir`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime settings, resolved once per process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADMIND_",
        extra="ignore",
    )

    environment: str = Field(default="local", description="local | ci | production")
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=True, description="JSON logs; set false for human-readable dev")

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://leadmind:leadmind@127.0.0.1:5432/leadmind"),
    )
    db_echo: bool = Field(default=False)
    db_pool_size: int = Field(default=5, ge=1, le=50)

    repo_root: Path = Field(default=_REPO_ROOT)
    config_dir: Path = Field(default=_REPO_ROOT / "config")
    data_dir: Path = Field(default=_REPO_ROOT / "data")

    ingest_batch_size: int = Field(default=500, ge=1)
    fuzzy_name_threshold: int = Field(
        default=92,
        ge=0,
        le=100,
        description="rapidfuzz token_set_ratio floor for queueing a duplicate candidate",
    )

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def sync_database_url(self) -> str:
        return str(self.database_url)

    def config_file(self, name: str) -> Path:
        path = self.config_dir / name
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
