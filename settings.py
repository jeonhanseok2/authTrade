from __future__ import annotations
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    alpaca_key:    Optional[str] = None
    alpaca_secret: Optional[str] = None
    mode: str = "paper"  # paper | live

    telegram_bot_token: Optional[str] = None
    telegram_chat_id:   Optional[str] = None

    # risk
    max_daily_loss: float = 0.02
    max_order_value: int = 1_000_000
    max_positions:   int = 8
    market_index: str = "QQQ"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AUTH_")


settings = Settings()
