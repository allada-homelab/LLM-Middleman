"""FastAPI application factory with a fail-soft lifespan.

`/healthz` always returns 200 once the process is up; `/readyz` returns 503 with a
reason while the app is initializing or degraded. Startup failures are logged and
surfaced via `/readyz` rather than crashing the process.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from llm_middleman.config import Settings, get_settings

logger = logging.getLogger("llm_middleman")

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    ready: bool = getattr(request.app.state, "ready", False)
    if ready:
        return JSONResponse({"status": "ready"})
    reason: str | None = getattr(request.app.state, "degraded_reason", None)
    return JSONResponse({"status": "not ready", "reason": reason}, status_code=503)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings: Settings = app.state.settings
    app.state.ready = False
    app.state.degraded_reason = None
    try:
        if settings.init_backend:
            # TODO: initialize real resources (db pools, clients, …) here.
            pass
        app.state.ready = True
    except Exception as exc:  # fail soft: stay up, report via /readyz
        app.state.degraded_reason = str(exc)
        logger.exception("startup init failed; serving in degraded mode")
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="LLM Middleman", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()
