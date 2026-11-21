"""PeakCut-style OAuth and token refresh for the three video platforms."""
import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from app.platforms.base import PlatformError, checked

PLATFORMS = ("youtube", "instagram", "tiktok")
NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
SCOPES = {
    "youtube": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
    "instagram": "instagram_business_basic,instagram_business_content_publish",
    "tiktok": "user.info.basic,video.publish",
}


def tiktok_response(response):
    data = checked(response)
    if data.get("error", {}).get("code") not in (None, "ok"):
        code = data["error"].get("code", "unknown")
        raise PlatformError(f"TikTok rejected the request ({code}).")
    return data.get("data", {})


class VideoAccounts:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client
        self.refresh_lock = asyncio.Lock()

    def keys(self, platform):
        return {
            "youtube": (self.settings.google_client_id, self.settings.google_client_secret),
            "instagram": (self.settings.instagram_app_id, self.settings.instagram_app_secret),
            "tiktok": (self.settings.tiktok_client_key, self.settings.tiktok_client_secret),
        }[platform]

    def callback(self, platform):
        return self.settings.app_base_url + f"/oauth/{platform}/callback"

    def status(self):
        return [{"platform": p, "name": NAMES[p], "configured": all(self.keys(p)),
                 "connected": bool(self.store.account(p)), "account_name": (self.store.account(p) or {}).get("name"),
                 "redirect_uri": self.callback(p)} for p in PLATFORMS]

    def start(self, platform):
        client_id, _ = self.keys(platform)
        if not all(self.keys(platform)):
            raise PlatformError(f"Set the {NAMES[platform]} developer app credentials using Set up first.")
        if platform in ("instagram", "tiktok") and not self.settings.app_base_url.startswith("https://"):
            raise PlatformError("This platform needs an HTTPS APP_BASE_URL registered with its developer app.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        self.store.add_state(state, platform, verifier)
        params = {"redirect_uri": self.callback(platform), "response_type": "code", "scope": SCOPES[platform], "state": state}
        if platform == "youtube":
            endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
            params.update(client_id=client_id, access_type="offline", prompt="consent")
        elif platform == "instagram":
            endpoint = "https://www.instagram.com/oauth/authorize"
            params.update(client_id=client_id)
        else:
            endpoint = "https://www.tiktok.com/v2/auth/authorize/"
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params.update(client_key=client_id, code_challenge=challenge, code_challenge_method="S256")
        return endpoint + "?" + urlencode(params), state

    async def finish(self, platform, code, state):
        verifier = self.store.take_state(state, platform)
        client_id, secret = self.keys(platform)
        form = {"code": code.split("#")[0], "redirect_uri": self.callback(platform),
                "grant_type": "authorization_code", "client_secret": secret}
        if platform == "youtube":
            form["client_id"] = client_id
            token = checked(await self.client.post("https://oauth2.googleapis.com/token", data=form))
            if not token.get("refresh_token"):
                raise PlatformError("Google did not return a refresh token. Reconnect and grant offline access.")
            profile = checked(await self.client.get("https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {token['access_token']}"}))
            if not profile.get("items"):
                raise PlatformError("Create a YouTube channel on this Google account first.")
            token["name"] = profile["items"][0]["snippet"]["title"]
        elif platform == "instagram":
            form["client_id"] = client_id
            token = checked(await self.client.post("https://api.instagram.com/oauth/access_token", data=form))
            long_token = checked(await self.client.get("https://graph.instagram.com/access_token",
                params={"grant_type": "ig_exchange_token", "client_secret": secret, "access_token": token["access_token"]}))
            token.update(long_token)
            profile = checked(await self.client.get("https://graph.instagram.com/me",
                params={"fields": "user_id,username,account_type"},
                headers={"Authorization": f"Bearer {token['access_token']}"}))
            token.update(user_id=str(profile["user_id"]), name=profile["username"])
        else:
            form.update(client_key=client_id, code_verifier=verifier)
            token = checked(await self.client.post("https://open.tiktokapis.com/v2/oauth/token/", data=form))
            if token.get("error") or not token.get("access_token"):
                raise PlatformError("TikTok sign-in failed. Check the app scopes and redirect URL.")
            token["name"] = "TikTok creator"
        token["token_time"] = time.time()
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        self.store.save_account(platform, token)

    async def token(self, platform):
        async with self.refresh_lock:
            account = self.store.account(platform)
            if not account:
                raise PlatformError(f"Connect {NAMES[platform]} before posting video.")
            refresh_instagram = platform == "instagram" and time.time() - account.get("token_time", time.time()) > 7 * 86400
            if account.get("expires_at", 0) > time.time() and not refresh_instagram:
                return account["access_token"]
            client_id, secret = self.keys(platform)
            if platform == "instagram":
                fresh = checked(await self.client.get("https://graph.instagram.com/refresh_access_token",
                    params={"grant_type": "ig_refresh_token"},
                    headers={"Authorization": f"Bearer {account['access_token']}"}))
            else:
                endpoint = "https://oauth2.googleapis.com/token" if platform == "youtube" else "https://open.tiktokapis.com/v2/oauth/token/"
                identity = {"client_id": client_id} if platform == "youtube" else {"client_key": client_id}
                fresh = checked(await self.client.post(endpoint, data={
                    **identity, "client_secret": secret, "refresh_token": account["refresh_token"], "grant_type": "refresh_token"}))
            if not fresh.get("access_token"):
                raise PlatformError("Token refresh failed. Reconnect this account.")
            account.update(fresh, expires_at=time.time() + int(fresh.get("expires_in", 3600)) - 60, token_time=time.time())
            self.store.save_account(platform, account)
            return account["access_token"]

    async def tiktok_creator(self):
        token = await self.token("tiktok")
        return tiktok_response(await self.client.post("https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                               headers={"Authorization": f"Bearer {token}"}, json={}))
