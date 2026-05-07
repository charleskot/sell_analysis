from __future__ import annotations

from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = Field(default="")
    analysis_model: str = Field(default="claude-opus-4-7")

    database_url: str = Field(default="sqlite:///./auctions.db")

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    target_provinces: List[str] = Field(default_factory=lambda: ["08", "17", "25", "43"])
    max_price_eur: int = 400_000
    min_opportunity_score: int = 60

    daily_run_hour: int = 8
    daily_run_minute: int = 0
    timezone: str = "Europe/Madrid"

    http_timeout_seconds: int = 30
    http_user_agent: str = "Mozilla/5.0 (compatible; AuctionAnalyzer/1.0)"
    enabled_scrapers: List[str] = Field(
        default_factory=lambda: [
            "boe", "addmeet", "solvia", "haya", "servihabitat", "aliseda",
        ]
    )

    @field_validator("target_provinces", "enabled_scrapers", mode="before")
    @classmethod
    def split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


settings = Settings()
