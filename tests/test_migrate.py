from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from solya import migrate
from solya.config import Settings
from solya.db import init_db, make_engine, session_scope
from solya.models import Owner

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)

# DDL of owners/sites as of the commit before the web-onboarding change.
OLD_OWNERS = """
CREATE TABLE owners (
    id INTEGER NOT NULL,
    display_name VARCHAR NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    active BOOLEAN NOT NULL,
    locale VARCHAR NOT NULL,
    last_inbound_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (telegram_chat_id)
)
"""
OLD_SITES = """
CREATE TABLE sites (
    se_site_id BIGINT NOT NULL,
    owner_id INTEGER NOT NULL,
    portal_name VARCHAR NOT NULL,
    card_name VARCHAR,
    label_he VARCHAR,
    commercial BOOLEAN NOT NULL,
    peak_power_kwp_reported FLOAT,
    billing_kwp_override FLOAT,
    tz VARCHAR NOT NULL,
    lat FLOAT,
    lon FLOAT,
    tariff_ils_per_kwh NUMERIC(6, 4),
    income_basis VARCHAR NOT NULL,
    check_times JSON,
    status VARCHAR NOT NULL,
    comm_state VARCHAR NOT NULL,
    comm_since DATETIME,
    last_update_time DATETIME,
    devices_fetched_at DATETIME,
    expected_inverters JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (se_site_id),
    FOREIGN KEY(owner_id) REFERENCES owners (id)
)
"""


def _old_db():
    engine = make_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(OLD_OWNERS))
        conn.execute(text(OLD_SITES))
        conn.execute(
            text(
                "INSERT INTO owners (id, display_name, telegram_chat_id, active, locale, created_at)"
                " VALUES (1, 'Dana', 555, 1, 'he', '2026-11-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sites (se_site_id, owner_id, portal_name, commercial, tz,"
                " income_basis, status, comm_state, expected_inverters, created_at)"
                " VALUES (3033498, 1, 'Home', 0, 'Asia/Jerusalem', 'production', 'active',"
                " 'ok', '[]', '2026-11-01 00:00:00')"
            )
        )
    return engine


def test_fresh_db_has_new_columns():
    engine = make_engine("sqlite://")
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("owners")}
    assert {"email", "phone", "signup_source", "link_token", "subscription_status"} <= cols
    assert {"payments", "llm_usage"} <= set(inspect(engine).get_table_names())
    factory = sessionmaker(engine, expire_on_commit=False)
    with session_scope(factory) as s:
        s.add(Owner(display_name="A", telegram_chat_id=None, created_at=NOW))
        s.add(Owner(display_name="B", telegram_chat_id=None, created_at=NOW))
    with session_scope(factory) as s:
        owners = s.query(Owner).all()
        assert len(owners) == 2
        assert owners[0].signup_source == "bot"
        assert owners[0].subscription_status == "active"


def test_migrate_old_schema():
    engine = _old_db()
    init_db(engine)  # create_all adds new tables, then migrate.apply alters old ones
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT display_name, telegram_chat_id, signup_source, subscription_status FROM owners")
        ).one()
        assert tuple(row) == ("Dana", 555, "bot", "active")
        site = conn.execute(text("SELECT monitored, call_plan, portal_name FROM sites")).one()
        assert tuple(site) == (1, "standard", "Home")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owners (display_name, telegram_chat_id, active, locale, created_at,"
                " signup_source, subscription_status) VALUES ('Web', NULL, 1, 'he',"
                " '2026-11-02 00:00:00', 'web', 'lead')"
            )
        )
    # site foreign key survived the owners rebuild
    with engine.connect() as conn:
        fks = conn.execute(text("PRAGMA foreign_key_list(sites)")).all()
        assert [f[2] for f in fks] == ["owners"]


def test_migrate_idempotent():
    engine = _old_db()
    from solya import models  # noqa: F401
    from solya.db import Base

    Base.metadata.create_all(engine)
    first = migrate.apply(engine)
    assert first
    assert migrate.apply(engine) == []
    fresh = make_engine("sqlite://")
    init_db(fresh)
    assert migrate.apply(fresh) == []


def _settings(key: str, **kw) -> Settings:
    return Settings(
        _env_file=None,
        solaredge_client_id="c",
        solaredge_client_secret="s",
        telegram_bot_token="1:t",
        telegram_bot_username="b",
        public_base_url="https://solya.example",
        token_encryption_key=key,
        **kw,
    )


def test_session_secret_fallback():
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    a = _settings(k1).effective_session_secret()
    assert isinstance(a, bytes) and len(a) == 32
    assert a == _settings(k1).effective_session_secret()
    assert a != _settings(k2).effective_session_secret()
    explicit = _settings(k1, session_secret="explicit").effective_session_secret()
    assert explicit != a
