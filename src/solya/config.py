from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    solaredge_client_id: str
    solaredge_client_secret: SecretStr
    telegram_bot_token: SecretStr
    telegram_bot_username: str
    public_base_url: AnyHttpUrl
    token_encryption_key: SecretStr
    admin_chat_ids: list[int] = []
    database_url: str = "sqlite:///./data/solya.db"
    se_api_base: str = "https://monitoringapi.solaredge.com/v2"
    se_authorize_url: str = "https://connect.solaredge.com/authorize"
    se_scope: str = "SITE_DATA DEVICE_DATA"
    se_access_duration_months: int = 24
    budget_monthly_credits: int = 2000
    budget_billing_day: int = 1
    budget_per_minute: int = 8
    max_credits_per_site_month: int = 350
    stale_after_minutes: int = 60
    fault_reminder_hours: int = 3
    evening_pull_offset_minutes: int = 25
    report_offset_minutes: int = 30
    default_check_times: list[str] = ["12:30"]
    connect_token_ttl_minutes: int = 15
    ray_implementation: str | None = None
    default_timezone: str = "Asia/Jerusalem"


@lru_cache
def get_settings() -> Settings:
    return Settings()
