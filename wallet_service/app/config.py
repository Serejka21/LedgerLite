from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://wallet:wallet@localhost:5432/wallet"

    # Outbound calls to the payment provider
    provider_base_url: str = "http://localhost:8001"
    provider_api_key: str = "dev-provider-key"
    provider_connect_timeout_seconds: float = 2.0
    provider_read_timeout_seconds: float = 5.0

    # Inbound webhooks from the provider
    provider_webhook_secret: str = "dev-webhook-secret"
    webhook_tolerance_seconds: int = 300
    # Base URL the provider uses to reach this service (callback_url)
    wallet_public_url: str = "http://localhost:8000"

    # Bonus: rate limiting of POST requests per API key
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
