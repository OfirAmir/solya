import hashlib
import hmac
from functools import lru_cache

import json
import math
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
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
    # An inverter fault mode alerts only after this many hourly samples in the day.
    fault_min_samples: int = Field(2, ge=1)
    # Weak production alert (smarter-alerts design); needs RAY_IMPLEMENTATION.
    weak_alerts_enabled: bool = False
    weak_threshold: float = 70
    weak_recover: float = 85
    weak_days: int = Field(3, ge=1)
    weak_max_cloud_pct: float = 70
    evening_pull_offset_minutes: int = 25
    report_offset_minutes: int = 30
    # Midday checks for commercial sites are off by default: one evening pull a day
    # (the owner's choice, to save API credits). Set e.g. ["12:30"] to turn them on.
    default_check_times: list[str] = []
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
    ray_visible: bool = False
    # PVGIS monthly E_d (kWh/kWp/day), lat 32.1 lon 34.8, optimal tilt, 14% losses.
    # Source: published PVGIS v5.2 figures for central Israel, recalled from
    # knowledge (re.jrc.ec.europa.eu was unreachable from the build network),
    # recorded 2026-11-26. Re-fetch from /api/v5_2/PVcalc when possible.
    # Old table: 3.5, 4.5, 6.0, 7.5, 8.0, 7.2, 6.5, 6.5, 7.0, 7.5, 5.0, 3.5
    ray_national_kwh_per_kwp_day: list[float] = [
        3.75, 4.45, 5.35, 5.90, 6.10, 6.20, 6.20, 6.10, 5.75, 4.95, 3.95, 3.40,
    ]

    @field_validator("ray_national_kwh_per_kwp_day")
    @classmethod
    def _check_national_table(cls, v):
        if len(v) != 12 or any(not math.isfinite(x) or x <= 0 for x in v):
            raise ValueError("ray_national_kwh_per_kwp_day needs exactly 12 finite positive values")
        return v

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
