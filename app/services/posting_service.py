"""Independent concurrent delivery. A platform failure cannot cancel other deliveries."""
import asyncio
from datetime import timezone

import httpx

from app.platforms.base import PlatformError
from app.platforms.bluesky import Bluesky
from app.platforms.mastodon import Mastodon
from app.platforms.telegram import Telegram
from app.schemas import PostingResponse, PostingResult


class PostingService:
    def __init__(self, settings, client):
        self.platforms = [Mastodon(settings), Bluesky(settings), Telegram(settings)]
        self.client = client

    async def deliver(self, platform, message):
        if not platform.configured:
            return PostingResult(platform=platform.name, status="failed",
                                 error="Missing or invalid server credentials. See /docs and instruction.md.")
        try:
            post = await platform.publish(self.client, message)
            created = post.created_at.astimezone(timezone.utc)
            return PostingResult(
                platform=platform.name, status="success", posted_url=post.url,
                public_access=post.public, date=created.date(), time=created.time().replace(microsecond=0),
                error=None if post.public else "Published. Anonymous public access could not be verified yet.",
            )
        except PlatformError as error:
            explanation = str(error)
        except httpx.TimeoutException:
            explanation = "Platform request timed out. It may have accepted the post; check the account before retrying."
        except httpx.RequestError:
            explanation = "Could not confirm delivery due to a network error. Check the account before retrying."
        except Exception:
            # Never return raw exception strings: they can include tokens in URLs or headers.
            explanation = "Unexpected platform response. Delivery could not be confirmed; check the account before retrying."
        return PostingResult(platform=platform.name, status="failed", error=explanation)

    async def publish(self, message):
        results = await asyncio.gather(*(self.deliver(platform, message) for platform in self.platforms))
        return PostingResponse(success=any(result.status == "success" for result in results), results=results)
