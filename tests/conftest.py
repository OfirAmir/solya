import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from solya.clock import FixedClock
from solya.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        solaredge_client_id="test-client-id",
        solaredge_client_secret="test-client-secret",
        telegram_bot_token="123456:test-bot-token",
        telegram_bot_username="solya_test_bot",
        public_base_url="https://solya.example.com",
        token_encryption_key=Fernet.generate_key().decode(),
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def db():
    from solya.db import SessionFactory, init_db, make_engine

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    factory = SessionFactory(bind=engine)
    yield factory
    engine.dispose()
