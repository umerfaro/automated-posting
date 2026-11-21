from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.platforms.base import Platform, PlatformError, PublishedPost, checked
from app.schemas import cta_link


class Bluesky(Platform):
    name = "Bluesky"

    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.bluesky_identifier and self.settings.bluesky_password)

    async def publish(self, client: httpx.AsyncClient, message: str):
        session = checked(await client.post(
            self.settings.bluesky_url + "/xrpc/com.atproto.server.createSession",
            json={"identifier": self.settings.bluesky_identifier, "password": self.settings.bluesky_password},
        ))
        created = datetime.now(timezone.utc)
        url, start, end = cta_link(message)
        # Bluesky facet offsets are UTF-8 bytes, including when the message contains emoji.
        record = {
            "$type": "app.bsky.feed.post", "text": message,
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "facets": [{"index": {"byteStart": len(message[:start].encode()), "byteEnd": len(message[:end].encode())},
                        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}],
        }
        data = checked(await client.post(
            self.settings.bluesky_url + "/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
        ))
        uri = data["uri"]
        post = PublishedPost(
            f"https://bsky.app/profile/{quote(session['did'], safe=':')}/post/{quote(uri.rsplit('/', 1)[1], safe='')}",
            created, False,
        )
        try:
            public = checked(await client.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread",
                                              params={"uri": uri, "depth": 0}))
            post.public = public.get("thread", {}).get("post", {}).get("uri") == uri
        except (httpx.HTTPError, PlatformError, ValueError, KeyError):
            pass
        return post
