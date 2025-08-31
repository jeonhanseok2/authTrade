from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    alpaca_key: str | None = None
    alpaca_secret: str | None = None
    mode: str = "paper"  # paper | live

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # risk
    max_daily_loss: float = 0.02
    max_order_value: int = 1_000_000
    max_positions: int = 8
    market_index: str = "QQQ"
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AUTH_")

settings = Settings()
