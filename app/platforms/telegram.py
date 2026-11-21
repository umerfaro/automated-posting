import re
from datetime import datetime, timezone

import httpx

from app.platforms.base import Platform, PlatformError, PublishedPost, checked


class Telegram(Platform):
    name = "Telegram"

    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.telegram_token and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", self.settings.telegram_channel))

    async def publish(self, client: httpx.AsyncClient, message: str):
        data = checked(await client.post(
            f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage",
            json={"chat_id": self.settings.telegram_channel, "text": message},
        ))
        if not data.get("ok"):
            raise PlatformError("Telegram refused the message. Check bot permissions and channel configuration.")
        result = data["result"]
        username = result["chat"].get("username")
        url = f"https://t.me/{username}/{result['message_id']}" if username else ""
        if not url:
            raise PlatformError("Telegram accepted the message but returned no public channel URL. Check the channel before retrying.")
        post = PublishedPost(url, datetime.fromtimestamp(result["date"], timezone.utc), False)
        # The embed endpoint can be read anonymously and contains the specific message's permalink.
        try:
            public = await client.get(url, params={"embed": "1", "mode": "tme"})
            post.public = public.status_code == 200 and f'data-post="{username}/{result["message_id"]}"' in public.text
        except httpx.HTTPError:
            pass
        return post
