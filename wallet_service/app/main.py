from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.errors import install_error_handlers
from app.provider_client import ProviderClient
from app.ratelimit import RateLimitMiddleware, SlidingWindowLimiter


def create_app(settings: Settings | None = None, provider: ProviderClient | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.provider = provider or ProviderClient(settings)
        try:
            yield
        finally:
            if provider is None:
                await app.state.provider.aclose()
            await engine.dispose()

    app = FastAPI(title="wallet-service", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(router)

    if settings.rate_limit_enabled:
        app.state.rate_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
        app.add_middleware(RateLimitMiddleware, limiter=app.state.rate_limiter)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
