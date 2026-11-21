"""Video upload and connected accounts, isolated from the text assessment endpoint."""
import asyncio
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.platforms.base import PlatformError
from app.schemas import VideoResponse
from app.tenancy import identity


async def probe_video(path):
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise HTTPException(503, "Install FFmpeg/ffprobe to validate video uploads.") from None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise HTTPException(422, "Video inspection timed out.") from None
    try:
        data = json.loads(out)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        duration = float(data["format"]["duration"])
        if process.returncode or video["codec_name"] != "h264" or not 0 < duration <= 180:
            raise ValueError()
        if video["width"] * video["height"] > 3840 * 2160 or "mp4" not in data["format"]["format_name"]:
            raise ValueError()
    except (ValueError, KeyError, StopIteration):
        raise HTTPException(422, "Use an H.264 MP4 video, at most 180 seconds and 4K resolution.") from None
    return duration


def router(settings, authorize):
    routes = APIRouter()

    @routes.get("/video/accounts", tags=["Video"], dependencies=[Depends(authorize)])
    async def accounts(request: Request):
        return {"accounts": request.app.state.video_accounts.status(), "max_video_mb": settings.max_video_mb}

    @routes.post("/video/accounts/{platform}/connect", tags=["Video"], dependencies=[Depends(authorize)])
    async def connect(platform: Literal["youtube", "instagram", "tiktok"], request: Request):
        if settings.saas_enabled and str(request.base_url).rstrip('/') != settings.app_base_url:
            raise HTTPException(400, f"Open Dispatch at {settings.app_base_url} before connecting so sign-in returns to this browser session.")
        try:
            url, state = request.app.state.video_accounts.start(platform)
        except PlatformError as error:
            raise HTTPException(400, str(error)) from None
        response = JSONResponse({"authorization_url": url})
        response.set_cookie("oauth_" + platform, state, httponly=True, samesite="lax", max_age=600,
                            secure=settings.app_base_url.startswith("https://"))
        return response

    @routes.get("/oauth/{platform}/callback", include_in_schema=False)
    async def callback(platform: Literal["youtube", "instagram", "tiktok"], request: Request,
                       state: str = "", code: str = "", error: str = ""):
        if settings.saas_enabled and not identity.get():
            raise HTTPException(401, "Sign in again before reconnecting this account.")
        if settings.saas_enabled and (identity.get().get("workspace_suspended") or settings.maintenance_mode):
            raise HTTPException(403, "Account connections are paused for this workspace.")
        cookie = request.cookies.get("oauth_" + platform, "")
        if not state or not secrets.compare_digest(cookie, state):
            raise HTTPException(400, "OAuth state mismatch. Start Connect again from this browser.")
        if error or not code:
            raise HTTPException(400, "Platform sign-in was cancelled or returned no code.")
        try:
            await request.app.state.video_accounts.finish(platform, code, state)
        except (PlatformError, ValueError):
            raise HTTPException(400, "Could not connect the account. Check developer app settings and retry sign-in.") from None
        except Exception:
            raise HTTPException(502, "Platform sign-in could not be completed. Check configuration and reconnect.") from None
        response = HTMLResponse('<!doctype html><title>Account connected</title><p>Account connected. <a href="/">Return to Dispatch</a>.</p>')
        response.delete_cookie("oauth_" + platform)
        return response

    @routes.get("/video/tiktok-creator", tags=["Video"], dependencies=[Depends(authorize)])
    async def creator(request: Request):
        try:
            return await request.app.state.video_accounts.tiktok_creator()
        except PlatformError as error:
            raise HTTPException(400, str(error)) from None
        except Exception:
            raise HTTPException(502, "Could not load current TikTok creator settings. Reconnect and retry.") from None

    @routes.get("/media/{token}.mp4", include_in_schema=False)
    async def media(token: str):
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise HTTPException(404)
        path = Path(settings.database_path).parent / "uploads" / (token + ".mp4")
        if not path.is_file() or time.time() - path.stat().st_mtime > 3600:
            raise HTTPException(404, "Media link expired.")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=60"})

    @routes.post("/video-posting", response_model=VideoResponse, tags=["Video"], dependencies=[Depends(authorize)])
    async def posting(
        request: Request,
        file: Annotated[UploadFile, File(description="H.264 MP4 video, up to 180 seconds.")],
        title: Annotated[str, Form(min_length=1, max_length=100)],
        caption: Annotated[str, Form(max_length=2200)] = "",
        youtube_privacy: Annotated[Literal["private", "unlisted", "public"], Form()] = "private",
        tiktok_privacy: Annotated[str, Form()] = "",
        consent: Annotated[bool, Form()] = False,
        made_for_kids: Annotated[bool, Form()] = False,
        disable_comment: Annotated[bool, Form()] = True,
        disable_duet: Annotated[bool, Form()] = True,
        disable_stitch: Annotated[bool, Form()] = True,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        if settings.saas_enabled and idempotency_key is None:
            idempotency_key = secrets.token_hex(16)
        if not consent:
            raise HTTPException(422, "Confirm that you want to publish this video to the connected platforms.")
        if not title.strip():
            raise HTTPException(422, "Enter a video title.")
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise HTTPException(422, "Use an Idempotency-Key with 8–128 safe characters.")
        directory = Path(settings.database_path).parent / "uploads"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for old in directory.glob("*.mp4"):
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)
        token = secrets.token_hex(32)
        path = directory / (token + ".mp4")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("xb") as target:
                path.chmod(0o600)
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    if size > settings.max_video_mb * 1024 * 1024:
                        raise HTTPException(413, f"Video exceeds {settings.max_video_mb} MB.")
                    digest.update(block)
                    target.write(block)
            duration = await probe_video(path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        identity = json.dumps([digest.hexdigest(), title, caption, youtube_privacy, tiktok_privacy,
                               made_for_kids, disable_comment, disable_duet, disable_stitch])
        try:
            if idempotency_key is not None:
                previous = request.app.state.store.claim(idempotency_key, identity, kind="video", title=title)
                if previous:
                    path.unlink(missing_ok=True)
                    return previous
        except Exception:
            path.unlink(missing_ok=True)
            raise
        media_url = settings.public_media_base_url + f"/media/{token}.mp4" if settings.public_media_base_url.startswith("https://") else ""
        response = await request.app.state.video_posting.publish(
            path, title, caption or title, youtube_privacy, media_url,
            duration=duration, tiktok_privacy=tiktok_privacy, made_for_kids=made_for_kids,
            disable_comment=disable_comment, disable_duet=disable_duet, disable_stitch=disable_stitch,
        )
        if idempotency_key is not None:
            request.app.state.store.finish(idempotency_key, response)
        return response

    return routes
