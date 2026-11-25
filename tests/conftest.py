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
        public_base_url="https://solya.example",
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


class FakeChannel:
    """Records OutboundMessages and returns a configurable SendResult.

    `result` may be a single SendResult or a list consumed one per send
    (the last item repeats).
    """

    name = "fake"
    caption_limit = 1024

    def __init__(self, result=None):
        from solya.messaging.base import Ok

        self.result = result if result is not None else Ok()
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)
        if isinstance(self.result, list):
            return self.result.pop(0) if len(self.result) > 1 else self.result[0]
        return self.result

    def can_send_freeform(self, recipient: str) -> bool:
        return True


@pytest.fixture
def fake_channel() -> FakeChannel:
    return FakeChannel()
