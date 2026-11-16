from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    allowed_roots: str = ""
    database_path: Path = Path(".repopilot/repopilot.db")
    provider: Literal["offline", "openai_compatible"] = "offline"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    sandbox_mode: Literal["local", "docker"] = "local"
    docker_image: str = "repopilot:latest"
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    max_steps: int = Field(default=16, ge=2, le=100)
    max_file_bytes: int = Field(default=512_000, ge=1024, le=5_000_000)
    max_output_chars: int = Field(default=20_000, ge=1000, le=200_000)
    max_repeat_actions: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def validate_provider(self) -> "Settings":
        if self.provider == "openai_compatible" and not self.model:
            raise ValueError("REPOPILOT_MODEL is required for openai_compatible provider")
        return self

    @property
    def allowed_root_paths(self) -> list[Path]:
        values = [item.strip() for item in self.allowed_roots.split(os.pathsep) if item.strip()]
        roots = [Path(item).expanduser().resolve() for item in values]
        return roots or [Path.cwd().resolve()]

    def ensure_directories(self) -> None:
        self.database_path = self.database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
