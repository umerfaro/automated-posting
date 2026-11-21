import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.store import Store


class PostingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.calls = []
        self.failure = None
        self.public_failure = False
        self.settings = Settings(
            mastodon_token="secret-mastodon", bluesky_identifier="demo.bsky.social",
            bluesky_password="secret-bluesky", telegram_token="12345:secret-telegram",
            telegram_channel="@demo_channel", database_path=str(Path(self.folder.name) / "test.db"),
        )

    def tearDown(self):
        self.folder.cleanup()

    def transport(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/api/v1/statuses":
            if self.failure == "auth":
                return httpx.Response(401, json={"error": "secret-mastodon"})
            if self.failure == "timeout":
                raise httpx.ReadTimeout("secret-mastodon", request=request)
            if self.failure == "malformed":
                return httpx.Response(200, text="invalid")
            if self.failure == "rate":
                return httpx.Response(429, headers={"Retry-After": "30"}, json={})
            return httpx.Response(200, json={"id": "123", "url": "https://mastodon.social/@demo/123",
                                            "created_at": "2026-11-21T10:00:00Z"})
        if path == "/api/v1/statuses/123":
            return httpx.Response(503 if self.public_failure else 200,
                                  json={"id": "123", "visibility": "public"})
        if path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "session-secret", "did": "did:plc:demo"})
        if path.endswith("createRecord"):
            return httpx.Response(200, json={"uri": "at://did:plc:demo/app.bsky.feed.post/abc"})
        if path.endswith("getPostThread"):
            return httpx.Response(200, json={"thread": {"post": {"uri": "at://did:plc:demo/app.bsky.feed.post/abc"}}})
        if path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {
                "message_id": 42, "date": 1795255200, "chat": {"username": "demo_channel"},
            }})
        if request.url.host == "t.me":
            return httpx.Response(200, text='<div data-post="demo_channel/42"></div>')
        raise AssertionError(f"Unexpected request {request.method} {request.url.host} {path}")

    def client(self, settings=None):
        return TestClient(create_app(settings or self.settings, httpx.MockTransport(self.transport)))

    def post(self, client, message="Look here https://example.com", **kwargs):
        return client.post("/posting", json={"message": message}, **kwargs)

    def test_all_three_publish_with_public_verification(self):
        with self.client() as client:
            result = self.post(client).json()
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 3)
        for item in result["results"]:
            self.assertEqual(item["status"], "success")
            self.assertTrue(item["public_access"])
            self.assertIsNotNone(item["date"])
            self.assertIsNotNone(item["time"])
            self.assertIsNone(item["error"])

    def test_invalid_messages_never_contact_platforms(self):
        invalid = [
            "", "No link", "www.example.com", "FTP ftp://example.com",
            "Two https://a.example https://b.example", "Invalid https://",
            "Private http://127.0.0.1", "Local http://localhost",
            "Login https://user:pass@example.com", "a" * 201 + " https://example.com",
            "Mix https://example.com www.other.com", "Nul\x00 https://example.com",
        ]
        with self.client() as client:
            for text in invalid:
                with self.subTest(text=text):
                    self.assertEqual(self.post(client, text).status_code, 422)
            self.assertEqual(client.post("/posting", json={"message": 123}).status_code, 422)
            self.assertEqual(client.post("/posting", json={"message": "https://example.com", "platforms": ["X"]}).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_exactly_200_characters_and_markdown(self):
        text = "x" * (200 - len(" https://example.com")) + " https://example.com"
        with self.client() as client:
            self.assertEqual(self.post(client, text).status_code, 200)
            self.assertEqual(self.post(client, "See [our site](https://example.com).").status_code, 200)

    def test_unicode_link_facet_uses_bytes(self):
        text = "🚀 Visit https://example.com!"
        with self.client() as client:
            self.post(client, text)
        request = next(r for r in self.calls if r.url.path.endswith("createRecord"))
        record = json.loads(request.content)["record"]
        facet = record["facets"][0]
        link = text.encode()[facet["index"]["byteStart"]:facet["index"]["byteEnd"]]
        self.assertEqual(link.decode(), "https://example.com")

    def test_one_platform_failure_does_not_cancel_others(self):
        for failure in ("auth", "timeout", "malformed", "rate"):
            self.calls.clear()
            self.failure = failure
            with self.client() as client:
                data = self.post(client).json()
            self.assertTrue(data["success"])
            self.assertEqual([r["status"] for r in data["results"]], ["failed", "success", "success"])
            self.assertNotIn("secret-mastodon", json.dumps(data))

    def test_unverified_public_access_is_still_published(self):
        self.public_failure = True
        with self.client() as client:
            result = self.post(client).json()["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["public_access"])
        self.assertIsNotNone(result["posted_url"])

    def test_missing_credentials_are_real_failures(self):
        with self.client(Settings(database_path=self.settings.database_path)) as client:
            data = self.post(client).json()
        self.assertFalse(data["success"])
        self.assertTrue(all(item["status"] == "failed" for item in data["results"]))
        self.assertEqual(self.calls, [])

    def test_idempotency_replays_and_rejects_changed_message(self):
        headers = {"Idempotency-Key": "request-key-123"}
        with self.client() as client:
            first = self.post(client, headers=headers)
            count = len(self.calls)
            second = self.post(client, headers=headers)
            self.assertEqual(first.json(), second.json())
            self.assertEqual(len(self.calls), count)
            self.assertEqual(self.post(client, "Other https://example.com", headers=headers).status_code, 409)

    def test_in_progress_request_cannot_publish_twice(self):
        store = Store(self.settings.database_path)
        store.claim("pending-key-123", "Look here https://example.com")
        with self.client() as client:
            self.assertEqual(self.post(client, headers={"Idempotency-Key": "pending-key-123"}).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_optional_api_auth_and_configuration_redaction(self):
        from dataclasses import replace
        settings = replace(self.settings, api_key="local-access-key")
        with self.client(settings) as client:
            self.assertEqual(self.post(client).status_code, 401)
            self.assertEqual(self.post(client, headers={"X-API-Key": "local-access-key"}).status_code, 200)
            config = client.get("/config").json()
            self.assertNotIn("secret", json.dumps(config))
            self.assertTrue(config["auth_required"])
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/docs").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
