import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        path = Path(cls.fixture.name) / "sample.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x568:r=24:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
        cls.video = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.calls = []
        self.fail_youtube = False
        self.settings = Settings(
            google_client_id="google-client", google_client_secret="google-secret",
            instagram_app_id="instagram-app", instagram_app_secret="instagram-secret",
            tiktok_client_key="tiktok-client", tiktok_client_secret="tiktok-secret",
            app_base_url="https://dispatch.example", public_media_base_url="https://dispatch.example",
            database_path=str(Path(self.folder.name) / "posting.db"),
        )

    def tearDown(self):
        self.folder.cleanup()

    def transport(self, request):
        self.calls.append(request)
        path = request.url.path
        bodies = {
            "/oauth/access_token": {"access_token": "ig-short", "user_id": "ig-user"},
            "/access_token": {"access_token": "ig-long", "expires_in": 5184000},
            "/me": {"user_id": "ig-user", "username": "instagram-demo", "account_type": "BUSINESS"},
            "/v2/oauth/token/": {"access_token": "tt-access", "refresh_token": "tt-refresh", "expires_in": 86400, "scope": "user.info.basic,video.publish"},
            "/token": {"access_token": "new-google-token", "refresh_token": "refresh", "expires_in": 3600},
            "/youtube/v3/channels": {"items": [{"snippet": {"title": "Demo Channel"}}]},
            "/resumable/video-session": {"id": "video123"},
            "/oembed": {"title": "Demo"},
            "/ig-user/media": {"id": "ig-container"},
            "/ig-container": {"status_code": "FINISHED"},
            "/ig-user/media_publish": {"id": "ig-post"},
            "/ig-post": {"permalink": "https://www.instagram.com/reel/demo/"},
            "/v2/post/publish/creator_info/query/": {"error": {"code": "ok"}, "data": {
                "creator_username": "demo", "creator_nickname": "Demo creator",
                "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"], "max_video_post_duration_sec": 180}},
            "/v2/post/publish/video/init/": {"error": {"code": "ok"}, "data": {
                "publish_id": "tt-publish", "upload_url": "https://open-upload.tiktokapis.com/upload"}},
            "/v2/post/publish/status/fetch/": {"error": {"code": "ok"}, "data": {
                "status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["456"]}},
        }
        if path == "/upload/youtube/v3/videos":
            if self.fail_youtube:
                return httpx.Response(403, json={"error": "google-secret"})
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/resumable/video-session"}, json={})
        if path in ("/resumable/video-session", "/upload"):
            self.assertEqual(request.content, self.video)
        if path == "/upload":
            return httpx.Response(206)
        if path in bodies:
            return httpx.Response(200, json=bodies[path])
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    def client(self):
        return TestClient(create_app(self.settings, httpx.MockTransport(self.transport)), base_url="https://dispatch.example")

    def seed(self, client):
        for platform in ("youtube", "instagram", "tiktok"):
            client.app.state.store.save_account(platform, {
                "access_token": "test-token", "refresh_token": "test-refresh",
                "expires_at": time.time() + 3600, "user_id": "ig-user", "name": "Test account"})

    def upload(self, client, **changes):
        data = {"title": "Test video", "caption": "Watch this", "consent": "true",
                "youtube_privacy": "public", "tiktok_privacy": "SELF_ONLY"}
        data.update(changes)
        return client.post("/video-posting", data=data, files={"file": ("sample.mp4", self.video, "video/mp4")},
                           headers={"Idempotency-Key": "video-request-123"})

    def test_three_uploads_and_idempotent_replay(self):
        with self.client() as client:
            self.seed(client)
            response = self.upload(client)
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual([r["status"] for r in data["results"]], ["success"] * 3, data)
            self.assertTrue(data["results"][0]["public_access"])
            self.assertFalse(data["results"][1]["public_access"])
            self.assertIn("instagram.com/reel", data["results"][1]["posted_url"])
            self.assertIn("tiktok.com/@demo", data["results"][2]["posted_url"])
            count = len(self.calls)
            self.assertEqual(self.upload(client).json(), data)
            self.assertEqual(len(self.calls), count)

    def test_failure_is_independent(self):
        self.fail_youtube = True
        with self.client() as client:
            self.seed(client)
            data = self.upload(client).json()
        self.assertEqual([r["status"] for r in data["results"]], ["failed", "success", "success"])
        self.assertNotIn("google-secret", json.dumps(data))

    def test_consent_and_actual_video_validation(self):
        with self.client() as client:
            self.assertEqual(self.upload(client, consent="false").status_code, 422)
            response = client.post("/video-posting", data={"title": "fake", "consent": "true"},
                                   files={"file": ("fake.mp4", b"not a video", "video/mp4")})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_account_status_and_creator_info(self):
        with self.client() as client:
            self.seed(client)
            data = client.get("/video/accounts").json()
            self.assertNotIn("test-token", json.dumps(data))
            self.assertEqual(len(data["accounts"]), 3)
            self.assertEqual(client.get("/video/tiktok-creator").json()["creator_username"], "demo")

    def test_oauth_cookie_state_and_replay(self):
        with self.client() as client:
            response = client.post("/video/accounts/youtube/connect")
            state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
            self.assertEqual(client.get("/oauth/youtube/callback", params={"code": "demo", "state": "wrong"}).status_code, 400)
            self.assertEqual(client.get("/oauth/youtube/callback", params={"code": "demo", "state": state}).status_code, 200)
            self.assertEqual(client.app.state.store.account("youtube")["name"], "Demo Channel")
            self.assertEqual(client.get("/oauth/youtube/callback", params={"code": "demo", "state": state}).status_code, 400)

    def test_instagram_and_tiktok_oauth(self):
        for platform in ("instagram", "tiktok"):
            with self.subTest(platform=platform), self.client() as client:
                response = client.post(f"/video/accounts/{platform}/connect")
                self.assertEqual(response.status_code, 200)
                query = parse_qs(urlparse(response.json()["authorization_url"]).query)
                if platform == "tiktok":
                    self.assertIn("code_challenge", query)
                state = query["state"][0]
                finish = client.get(f"/oauth/{platform}/callback", params={"code": "demo", "state": state})
                self.assertEqual(finish.status_code, 200, finish.text)
                self.assertIn("access_token", client.app.state.store.account(platform))

    def test_tiktok_rejects_unavailable_visibility(self):
        with self.client() as client:
            self.seed(client)
            result = self.upload(client, tiktok_privacy="UNAVAILABLE").json()["results"][2]
            self.assertEqual(result["status"], "failed")
            self.assertFalse(any(request.url.path.endswith("video/init/") for request in self.calls))

    def test_google_token_refresh(self):
        with self.client() as client:
            self.seed(client)
            account = client.app.state.store.account("youtube")
            account["expires_at"] = 0
            client.app.state.store.save_account("youtube", account)
            self.assertTrue(self.upload(client).json()["success"])
            self.assertEqual(client.app.state.store.account("youtube")["access_token"], "new-google-token")

    def test_instagram_requires_https_media(self):
        self.settings = replace(self.settings, public_media_base_url="")
        with self.client() as client:
            self.seed(client)
            result = self.upload(client).json()["results"][1]
        self.assertEqual(result["status"], "failed")
        self.assertIn("PUBLIC_MEDIA_BASE_URL", result["error"])

    def test_media_links_expire(self):
        with self.client() as client:
            folder = Path(self.settings.database_path).parent / "uploads"
            folder.mkdir()
            token = "a" * 64
            path = folder / (token + ".mp4")
            path.write_bytes(self.video)
            self.assertEqual(client.get(f"/media/{token}.mp4").status_code, 200)
            os.utime(path, (time.time() - 3601, time.time() - 3601))
            self.assertEqual(client.get(f"/media/{token}.mp4").status_code, 404)
            self.assertEqual(client.get("/media/invalid.mp4").status_code, 404)


if __name__ == "__main__":
    unittest.main()
