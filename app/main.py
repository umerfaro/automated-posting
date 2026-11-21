"""Run with uvicorn app.main:app --host 0.0.0.0 --port 4000."""
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings
from app.admin import router as admin_router
from app.settings_routes import RuntimeSettings, router as settings_router
from app.schemas import PostingRequest, PostingResponse
from app.services.posting_service import PostingService
from app.services.store import Store
from app.saas import SaaS, router as saas_router, COOKIE
from app.tenancy import identity, workspace
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
            app.state.store = Store(settings.baseline.database_path)
            settings.store = app.state.store
            app.state.client = client
            if settings.baseline.saas_enabled:
                app.state.saas = SaaS(app.state.store, settings, client)
            app.state.service = PostingService(settings, client)
            app.state.video_accounts = VideoAccounts(settings, app.state.store, client)
            app.state.video_posting = VideoPosting(app.state.video_accounts, settings, client)
            yield

    app = FastAPI(
        title="Dispatch SaaS", version="2.0.0", lifespan=lifespan,
        description="Publish a validated message to Mastodon, Bluesky and Telegram independently. Dates and times are UTC.",
    )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def settings_snapshot(request, call_next):
        user = app.state.saas.authenticate(request.cookies.get(COOKIE)) if settings.saas_enabled else None
        user_token = identity.set(user)
        workspace_token = workspace.set(user['workspace'] if user else '')
        config_token = settings.context.set(settings.snapshot())
        try:
            response = await call_next(request)
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['Content-Security-Policy'] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
            if request.url.path != '/':
                response.headers['Cache-Control'] = 'no-store'
            return response
        finally:
            settings.context.reset(config_token)
            workspace.reset(workspace_token)
            identity.reset(user_token)

    async def authorize(request: Request, x_api_key: Annotated[str | None, Header()] = None):
        if settings.saas_enabled:
            user = identity.get()
            if not user:
                raise HTTPException(401, "Sign in to your workspace.")
            path=request.url.path
            administrative=path.startswith('/admin/')
            safe_read=request.method in ('GET','HEAD') and (path in ('/dashboard','/config','/video/accounts') or path.startswith('/settings/'))
            if user.get('workspace_suspended') and not (administrative or safe_read or path.startswith(('/auth/','/workspaces')) or path=='/billing/portal'):
                raise HTTPException(403,'This workspace is suspended. Contact the administrator or switch workspace.')
            provider_operation=path in ('/posting','/video-posting','/video/tiktok-creator') or path.endswith(('/test','/connect'))
            if settings.maintenance_mode and provider_operation and not administrative:
                raise HTTPException(503,'Publishing and provider connections are temporarily paused for maintenance.')
            if settings.email_verification_required and not user['verified'] and not administrative and (request.url.path in ('/posting','/video-posting','/billing/checkout') or request.url.path.endswith(('/test','/connect'))):
                raise HTTPException(403, 'Verify your email before publishing or connecting accounts. Use Resend verification in your workspace.')
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                app.state.saas.origin(request)
                if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), user['csrf']):
                    raise HTTPException(403, "Refresh the page and try again. The session token is missing or expired.")
        elif settings.api_key and not secrets.compare_digest(x_api_key or "", settings.api_key):
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
    async def configuration(request: Request):
        if settings.saas_enabled:
            await authorize(request)
        # Readiness means configured; it does not claim the credentials were authenticated.
        return {
            "auth_required": bool(settings.api_key) and not settings.saas_enabled,
            "saas": settings.saas_enabled,
            "platforms": [{"name": platform.name, "configured": platform.configured} for platform in app.state.service.platforms],
        }

    @app.post("/posting", response_model=PostingResponse, tags=["Posting"], dependencies=[Depends(authorize)])
    async def posting(
        payload: PostingRequest,
        idempotency_key: Annotated[str | None, Header(description="Optional unique request key, 8–128 safe characters.")] = None,
    ):
        if settings.saas_enabled and idempotency_key is None:
            idempotency_key = secrets.token_hex(16)
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

    app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "web/assets"), name="assets")
    app.include_router(saas_router(settings, authorize))
    app.include_router(admin_router(settings, authorize))
    app.include_router(settings_router(settings, authorize))
    app.include_router(video_router(settings, authorize))
    return app


app = create_app()
