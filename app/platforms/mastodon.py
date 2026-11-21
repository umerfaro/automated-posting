import httpx

from app.platforms.base import Platform, PlatformError, PublishedPost, checked, timestamp


class Mastodon(Platform):
    name = "Mastodon"

    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.mastodon_token)

    async def publish(self, client: httpx.AsyncClient, message: str):
        data = checked(await client.post(
            self.settings.mastodon_url + "/api/v1/statuses",
            headers={"Authorization": f"Bearer {self.settings.mastodon_token}"},
            json={"status": message, "visibility": "public"},
        ))
        post = PublishedPost(data["url"], timestamp(data["created_at"]), False)
        # Public access is checked without the publishing credential.
        try:
            public = await client.get(self.settings.mastodon_url + f"/api/v1/statuses/{data['id']}")
            body = checked(public)
            post.public = body.get("id") == data["id"] and body.get("visibility") == "public"
        except (httpx.HTTPError, PlatformError, ValueError, KeyError):
            pass
        return post
