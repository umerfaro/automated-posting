"""Run with uvicorn app.main:app --host 0.0.0.0 --port 4000."""
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.config import Settings
from app.settings_routes import RuntimeSettings, router as settings_router
from app.schemas import PostingRequest, PostingResponse
from app.services.posting_service import PostingService
from app.services.store import Store
from app.services.video_accounts import VideoAccounts
from app.services.video_posting import VideoPosting
from app.video_routes import router as video_router


def create_app(settings=None, transport=None):
    settings = RuntimeSettings(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout), follow_redirects=False,
            transport=transport, headers={"User-Agent": "AutomatedPosting/1.0"},
        ) as client:
            app.state.store = Store(settings.database_path)
            settings.store = app.state.store
            app.state.client = client
            app.state.service = PostingService(settings, client)
            app.state.video_accounts = VideoAccounts(settings, app.state.store, client)
            app.state.video_posting = VideoPosting(app.state.video_accounts, settings, client)
            yield

    app = FastAPI(
        title="Automated Multi-Platform Posting", version="1.0.0", lifespan=lifespan,
        description="Publish a validated message to Mastodon, Bluesky and Telegram independently. Dates and times are UTC.",
    )

    @app.middleware("http")
    async def settings_snapshot(request, call_next):
        token = settings.context.set(settings.snapshot())
        try:
            return await call_next(request)
        finally:
            settings.context.reset(token)

    async def authorize(x_api_key: Annotated[str | None, Header()] = None):
        if settings.api_key and not secrets.compare_digest(x_api_key or "", settings.api_key):
            raise HTTPException(401, "Provide the configured X-API-Key.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo rejected user input or internal validator exception objects.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in error.errors()
        ]})

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(Path(__file__).parent / "web/index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/health", tags=["System"])
    async def health():
        return {"status": "ok"}

    @app.get("/config", tags=["System"])
    async def configuration():
        # Readiness means configured; it does not claim the credentials were authenticated.
        return {
            "auth_required": bool(settings.api_key),
            "platforms": [{"name": platform.name, "configured": platform.configured} for platform in app.state.service.platforms],
        }

    @app.post("/posting", response_model=PostingResponse, tags=["Posting"], dependencies=[Depends(authorize)])
    async def posting(
        payload: PostingRequest,
        idempotency_key: Annotated[str | None, Header(description="Optional unique request key, 8–128 safe characters.")] = None,
    ):
        if idempotency_key is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
                raise HTTPException(422, "Idempotency-Key must contain 8–128 letters, numbers, underscores or hyphens.")
            previous = app.state.store.claim(idempotency_key, payload.message)
            if previous:
                return previous
        response = await app.state.service.publish(payload.message)
        if idempotency_key is not None:
            app.state.store.finish(idempotency_key, response)
        return response

    app.include_router(settings_router(settings, authorize))
    app.include_router(video_router(settings, authorize))
    return app


app = create_app()
