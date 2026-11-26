import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from solya.db import Base, UTCDateTime


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str] = mapped_column(String, default="he")
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    signup_source: Mapped[str] = mapped_column(
        String, default="bot", server_default="bot"
    )  # bot|web
    link_token: Mapped[str | None] = mapped_column(
        String, unique=True, index=True, nullable=True
    )
    # lead|connected|pending_payment|active; web sign-ups set "lead" explicitly
    subscription_status: Mapped[str] = mapped_column(
        String, default="active", server_default="active"
    )


class Invite(Base):
    __tablename__ = "invites"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("owners.id"), nullable=True)


class Site(Base):
    __tablename__ = "sites"

    se_site_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    portal_name: Mapped[str] = mapped_column(String)
    card_name: Mapped[str | None] = mapped_column(String, nullable=True)
    label_he: Mapped[str | None] = mapped_column(String, nullable=True)
    commercial: Mapped[bool] = mapped_column(Boolean, default=False)
    peak_power_kwp_reported: Mapped[float | None] = mapped_column(Float, nullable=True)
    billing_kwp_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    tz: Mapped[str] = mapped_column(String)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    tariff_ils_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    income_basis: Mapped[str] = mapped_column(String, default="production")
    check_times: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")  # active|reauth_needed|paused
    comm_state: Mapped[str] = mapped_column(String, default="ok")
    comm_since: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_update_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    devices_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expected_inverters: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    call_plan: Mapped[str] = mapped_column(
        String, default="standard", server_default="standard"
    )  # standard|low


class SiteToken(Base):
    __tablename__ = "site_tokens"

    site_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sites.se_site_id"), primary_key=True, autoincrement=False
    )
    access_enc: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_enc: Mapped[bytes] = mapped_column(LargeBinary)
    previous_refresh_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    access_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    refreshed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    authorized_at: Mapped[datetime] = mapped_column(UTCDateTime)
    grant_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ConnectRequest(Base):
    __tablename__ = "connect_requests"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    nonce: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    site_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SettingsLink(Base):
    """A single-use link from the bot's /settings to the web settings page. Only the
    SHA-256 hex of the token is stored."""

    __tablename__ = "settings_links"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class RawResponse(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(BigInteger)
    endpoint: Mapped[str] = mapped_column(String)
    params: Mapped[dict] = mapped_column(JSON)
    status: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DailyMetric(Base):
    __tablename__ = "daily_metrics"

    site_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    production_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    export_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    self_consumption_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    income_ils: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    ray: Mapped[float | None] = mapped_column(Float, nullable=True)
    comm_state: Mapped[str] = mapped_column(String)
    lifetime_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FaultRecord(Base):
    __tablename__ = "faults"
    __table_args__ = (UniqueConstraint("site_id", "key", name="uq_faults_site_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(BigInteger)
    key: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    code: Mapped[str] = mapped_column(String)
    serial: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Source-specific alert figures as JSON (weak production: {"days": n, "pct": p}).
    detail: Mapped[str | None] = mapped_column(String, nullable=True)


class OutboxEntry(Base):
    __tablename__ = "outbox"

    dedupe_key: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String)
    recipient: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # pending|sent|blocked|invalid|retry
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class ApiLedger(Base):
    __tablename__ = "api_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    site_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    endpoint: Mapped[str] = mapped_column(String)
    http_status: Mapped[int] = mapped_column(Integer)
    credited: Mapped[bool] = mapped_column(Boolean)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    amount_agorot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str] = mapped_column(String, default="stub")
    status: Mapped[str] = mapped_column(String, default="stub_pending")
    description: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)


class WeatherDaily(Base):
    __tablename__ = "weather_daily"

    site_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cloud_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmax_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    pm10: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RayDaily(Base):
    """Every RAY v1 component per site and day, for comparison with Instinct."""

    __tablename__ = "ray_daily"

    site_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ray: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    hist_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    health: Mapped[float | None] = mapped_column(Float, nullable=True)
    env: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_source: Mapped[str | None] = mapped_column(String, nullable=True)  # history|table
    cv: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmax_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    pm10: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)  # normal|low
    computed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
