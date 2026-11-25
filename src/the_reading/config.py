"""Typed application configuration loaded from config.yaml and .env."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeedConfig(BaseModel):
    name: str
    url: str


class EditionConfig(BaseModel):
    title: str = "The Reading"
    max_articles: int = 20
    target_reading_minutes_min: int = 60
    target_reading_minutes_max: int = 120
    words_per_minute: int = 220
    wildcard_slots: int = 1
    min_extracted_words: int = 250
    gemini_model: str = "gemini-3.8-flash"


class InterestConfig(BaseModel):
    lookback_days: int = 14
    letterboxd_username: str = "nikkyvarga"
    enable_interest_discovery: bool = True
    max_discovery_queries: int = 8


class DeliveryConfig(BaseModel):
    drive_folder_name: str = "The Reading"
    archive_subfolder_name: str = "Archive"
    archive_after_days: int = 21


class AppConfig(BaseModel):
    edition: EditionConfig = Field(default_factory=EditionConfig)
    interest: InterestConfig = Field(default_factory=InterestConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    feeds: list[FeedConfig] = Field(default_factory=list)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""

    seerr_url: str = "http://192.168.1.12:5055"
    seerr_api_key: str = ""

    jellyfin_url: str = "http://192.168.1.12:8096"
    jellyfin_api_key: str = ""
    jellyfin_user_id: str = ""

    navidrome_url: str = "http://192.168.1.12:4533"
    navidrome_user: str = ""
    navidrome_password: str = ""

    gdrive_credentials_path: Path = Path("./data/gdrive_credentials.json")
    gdrive_token_path: Path = Path("./data/gdrive_token.json")

    data_dir: Path = Path("./data")
    config_path: Path = Path("./config.yaml")


class Settings(BaseModel):
    env: EnvSettings
    app: AppConfig

    @property
    def db_path(self) -> Path:
        return self.env.data_dir / "the_reading.db"


def load_settings(
    config_path: Path | None = None,
    env_file: Path | None = None,
) -> Settings:
    """Load environment variables and YAML config into a unified Settings object."""
    env_kwargs: dict[str, Any] = {}
    if env_file is not None:
        env_kwargs["_env_file"] = env_file
    env = EnvSettings(**env_kwargs)

    cfg_path = config_path or env.config_path
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        app = AppConfig.model_validate(raw)
    else:
        app = AppConfig()

    return Settings(env=env, app=app)
