"""PeakCut's upload protocols adapted for bounded asynchronous delivery."""
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.platforms.base import PlatformError, checked
from app.schemas import VideoResponse, VideoResult
from app.services.video_accounts import PLATFORMS, NAMES, tiktok_response


async def file_chunks(path):
    with path.open("rb") as source:
        while block := await asyncio.to_thread(source.read, 1024 * 1024):
            yield block


def upload_url(value, suffix):
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == suffix or host.endswith("." + suffix)):
        raise PlatformError("Platform returned an unexpected upload host.")
    return value


class VideoPosting:
    def __init__(self, accounts, settings, client):
        self.accounts, self.settings, self.client = accounts, settings, client

    async def youtube(self, path, title, caption, privacy, media_url, **options):
        token = await self.accounts.token("youtube")
        auth = {"Authorization": f"Bearer {token}"}
        session = await self.client.post("https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"}, headers={
                **auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(path.stat().st_size)},
            json={"snippet": {"title": title, "description": caption, "categoryId": "22"},
                  "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": options["made_for_kids"]}})
        checked(session)
        target = upload_url(session.headers["Location"], "googleapis.com")
        data = checked(await self.client.put(target, content=file_chunks(path), timeout=900,
                                             headers={**auth, "Content-Type": "video/mp4", "Content-Length": str(path.stat().st_size)}))
        url = "https://www.youtube.com/watch?v=" + data["id"]
        public = False
        if privacy != "private":
            try:
                check = await self.client.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
                public = check.status_code == 200
            except httpx.HTTPError:
                pass
        return VideoResult(platform="YouTube", status="success", posted_url=url, public_access=public, remote_id=data["id"],
                           error=None if public else "Uploaded. Public access is private or has not been verified.")

    async def instagram(self, path, title, caption, privacy, media_url, **options):
        if not media_url:
            raise PlatformError("Set PUBLIC_MEDIA_BASE_URL to a publicly reachable HTTPS address so Instagram can fetch the video.")
        token = await self.accounts.token("instagram")
        account = self.accounts.store.account("instagram")
        auth = {"Authorization": f"Bearer {token}"}
        box = checked(await self.client.post(f"https://graph.instagram.com/{account['user_id']}/media",
            headers=auth, data={"media_type": "REELS", "video_url": media_url, "caption": caption, "share_to_feed": "true"}))
        for _ in range(20):
            state = checked(await self.client.get(f"https://graph.instagram.com/{box['id']}",
                                                 params={"fields": "status_code"}, headers=auth))
            if state.get("status_code") == "FINISHED":
                break
            if state.get("status_code") in ("ERROR", "EXPIRED"):
                raise PlatformError("Instagram rejected or expired the video container.")
            await asyncio.sleep(3)
        else:
            return VideoResult(platform="Instagram", status="pending", remote_id=box["id"],
                               error="Video container is still processing. No publish call has been made. Check before retrying.")
        post = checked(await self.client.post(f"https://graph.instagram.com/{account['user_id']}/media_publish",
                                             headers=auth, data={"creation_id": box["id"]}))
        result = VideoResult(platform="Instagram", status="success", remote_id=post["id"],
                             error="Published. Anonymous public access has not been verified.")
        try:
            info = checked(await self.client.get(f"https://graph.instagram.com/{post['id']}",
                           params={"fields": "permalink"}, headers=auth))
            result.posted_url = info.get("permalink")
        except (httpx.HTTPError, PlatformError, ValueError):
            pass
        return result

    async def tiktok(self, path, title, caption, privacy, media_url, **options):
        creator = await self.accounts.tiktok_creator()
        level = options["tiktok_privacy"]
        if level not in creator.get("privacy_level_options", []):
            raise PlatformError("Choose an available TikTok privacy level from the latest creator information.")
        if options["duration"] > creator.get("max_video_post_duration_sec", 180):
            raise PlatformError("This video exceeds the TikTok creator's allowed duration.")
        token = await self.accounts.token("tiktok")
        auth = {"Authorization": f"Bearer {token}"}
        size = path.stat().st_size
        chunk = size if size <= 64_000_000 else 10_000_000
        count = max(1, size // chunk)
        box = tiktok_response(await self.client.post("https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers=auth, json={
                "post_info": {"title": caption, "privacy_level": level,
                    "disable_comment": options["disable_comment"] or creator.get("comment_disabled", False),
                    "disable_duet": options["disable_duet"] or creator.get("duet_disabled", False),
                    "disable_stitch": options["disable_stitch"] or creator.get("stitch_disabled", False)},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
            }))
        target = upload_url(box["upload_url"], "tiktokapis.com")
        with path.open("rb") as source:
            for index in range(count):
                first = index * chunk
                length = size - first if index == count - 1 else chunk
                block = await asyncio.to_thread(source.read, length)
                response = await self.client.put(target, content=block, timeout=900, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length), "Content-Range": f"bytes {first}-{first+length-1}/{size}"})
                if response.status_code not in (200, 201, 206):
                    checked(response)
                    raise PlatformError("TikTok upload was not accepted.")
        for _ in range(20):
            state = tiktok_response(await self.client.post("https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                                    headers=auth, json={"publish_id": box["publish_id"]}))
            if state.get("status") == "PUBLISH_COMPLETE":
                ids = state.get("publicaly_available_post_id") or state.get("publicly_available_post_id") or []
                url = f"https://www.tiktok.com/@{creator['creator_username']}/video/{ids[0]}" if ids else None
                return VideoResult(platform="TikTok", status="success", posted_url=url, remote_id=box["publish_id"],
                    public_access=False, error="Published. Anonymous access has not been verified; visibility follows your TikTok selection.")
            if state.get("status") == "FAILED":
                raise PlatformError("TikTok did not publish the video. Check the creator account and app audit status.")
            await asyncio.sleep(3)
        return VideoResult(platform="TikTok", status="pending", remote_id=box["publish_id"],
                           error="Upload accepted; TikTok is still processing. Check the account before retrying.")

    async def deliver(self, platform, *args, **options):
        if not self.accounts.store.account(platform):
            return VideoResult(platform=NAMES[platform], status="failed", error=f"Connect {NAMES[platform]} first.")
        try:
            result = await getattr(self, platform)(*args, **options)
            if result.status == "success":
                now = datetime.now(timezone.utc)
                result.date, result.time = now.date(), now.time().replace(microsecond=0)
            return result
        except PlatformError as error:
            detail = str(error)
        except httpx.TimeoutException:
            detail = "Request timed out. Publication may have started; check your account before retrying."
        except httpx.RequestError:
            detail = "Network failure. Delivery could not be confirmed; check your account before retrying."
        except Exception:
            detail = "Unexpected platform response. Check your account before retrying."
        return VideoResult(platform=NAMES[platform], status="failed", error=detail)

    async def publish(self, *args, **options):
        results = await asyncio.gather(*(self.deliver(p, *args, **options) for p in PLATFORMS))
        return VideoResponse(success=any(r.status == "success" for r in results), results=results)
