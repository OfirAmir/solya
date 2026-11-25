import hashlib
import hmac
from functools import lru_cache

import json
from typing import Annotated

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Burst limit (recon brief: ~5 calls within seconds got 429): space every call.
    budget_min_interval_seconds: float = 7.5
    max_credits_per_site_month: int = 350
    stale_after_minutes: int = 60
    fault_reminder_hours: int = 3
    evening_pull_offset_minutes: int = 25
    report_offset_minutes: int = 30
    default_check_times: list[str] = ["12:30"]
    connect_token_ttl_minutes: int = 15
    ray_implementation: str | None = None
    default_timezone: str = "Asia/Jerusalem"
    session_secret: SecretStr | None = None
    admin_password: SecretStr | None = None
    trusted_proxies: Annotated[list[str], NoDecode] = ["127.0.0.1"]
    llm_enabled: bool = False
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_monthly_tokens_per_owner: int = 50000

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    def effective_session_secret(self) -> bytes:
        if self.session_secret is not None:
            return hashlib.sha256(self.session_secret.get_secret_value().encode()).digest()
        key = self.token_encryption_key.get_secret_value().encode()
        return hmac.new(key, b"solya-session", hashlib.sha256).digest()


@lru_cache
def get_settings() -> Settings:
    return Settings()
