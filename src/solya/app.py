"""Process wiring: one container, one FastAPI app, PTB polling and the scheduler in its lifespan."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from solya.bot import build_bot_application
from solya.clock import Clock, SystemClock
from solya.config import Settings
from solya.crypto import TokenCipher
from solya.db import SessionFactory, init_db, make_engine
from solya.messaging.outbox import Outbox
from solya.messaging.telegram import TelegramChannel
from solya.render.card import assert_raqm
from solya.rules.ray import load_ray
from solya.scheduler import SchedulerService
from solya.services.reporting import Reporter
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.oauth import TokenManager

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    clock: Clock
    session_factory: SessionFactory
    cipher: TokenCipher
    http: httpx.AsyncClient
    budget: CreditBudget
    tokens: TokenManager
    client: SolarEdgeClient
    outbox: Outbox
    channel: Any
    reporter: Reporter
    scheduler: SchedulerService
    ray: Any
    bot_app: Any = None


def _ensure_db_dir(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


def build_container(
    settings: Settings,
    clock: Clock | None = None,
    channel: Any = None,
    budget_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Container:
    """Wire every component. Performs no network activity."""
    clock = clock or SystemClock()
    _ensure_db_dir(settings.database_url)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = SessionFactory(bind=engine)
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    http = httpx.AsyncClient(timeout=20)
    budget = CreditBudget(factory, settings, clock, sleep=budget_sleep)
    holder: dict[str, Container] = {}

    async def reauth(site_id: int) -> None:
        await holder["c"].reporter.on_reauth_needed(site_id)

    tokens = TokenManager(factory, settings, cipher, http, budget, clock, reauth)
    client = SolarEdgeClient(http, settings, tokens, budget, factory, clock)
    outbox = Outbox(factory, clock)
    ray = load_ray(settings.ray_implementation)

    container = Container(
        settings=settings, clock=clock, session_factory=factory, cipher=cipher, http=http,
        budget=budget, tokens=tokens, client=client, outbox=outbox, channel=channel,
        reporter=None, scheduler=None, ray=ray,  # type: ignore[arg-type]
    )
    holder["c"] = container
    container.bot_app = build_bot_application(settings.telegram_bot_token.get_secret_value(), container)
    if channel is None:
        container.channel = TelegramChannel(container.bot_app.bot)
    container.reporter = Reporter(
        factory, client, tokens, outbox, container.channel, clock, settings, ray,
        lambda sid, at: container.scheduler.schedule_retry(sid, at),
    )
    container.scheduler = SchedulerService(container.reporter, factory, settings, clock)
    return container


def create_app(container: Container, start_background: bool = True) -> FastAPI:
    from solya.web import router

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(container.session_factory.kw["bind"])
        assert_raqm()
        bot_app = container.bot_app
        if start_background:
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling()
            container.scheduler.start()
        try:
            yield
        finally:
            if start_background:
                container.scheduler.shutdown()
                await bot_app.updater.stop()
                await bot_app.stop()
                await bot_app.shutdown()
            await container.http.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.container = container
    app.include_router(router)
    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    from solya.config import get_settings

    logging.basicConfig(level=logging.INFO)
    app = create_app(build_container(get_settings()))
    # access log would record the OAuth callback query string (code)
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
