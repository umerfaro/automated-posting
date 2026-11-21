"""Server-owned platform selection and environment-only credentials."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from pydantic import HttpUrl, TypeAdapter

ROOT = Path(__file__).resolve().parent.parent


def endpoint(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    url = TypeAdapter(HttpUrl).validate_python(value)
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment or url.path not in (None, "/"):
        raise ValueError(f"{name} must be an HTTPS origin without credentials or a path.")
    return str(url).rstrip("/")


@dataclass(frozen=True)
class Settings:
    mastodon_url: str = "https://mastodon.social"
    mastodon_token: str = field(default="", repr=False)
    bluesky_url: str = "https://bsky.social"
    bluesky_identifier: str = ""
    bluesky_password: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_channel: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = 20
    database_path: str = str(ROOT / "data/posting.sqlite3")

    app_base_url: str = "http://127.0.0.1:4000"
    public_media_base_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    instagram_app_id: str = ""
    instagram_app_secret: str = field(default="", repr=False)
    tiktok_client_key: str = ""
    tiktok_client_secret: str = field(default="", repr=False)
    max_video_mb: int = 250

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        timeout = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
        if not 1 <= timeout <= 120:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be between 1 and 120.")
        max_video_mb = int(os.getenv("MAX_VIDEO_MB", "250"))
        if not 1 <= max_video_mb <= 1000:
            raise ValueError("MAX_VIDEO_MB must be between 1 and 1000.")
        database = Path(os.getenv("DATABASE_PATH", "data/posting.sqlite3"))
        return cls(
            mastodon_url=endpoint("MASTODON_BASE_URL", "https://mastodon.social"),
            mastodon_token=os.getenv("MASTODON_ACCESS_TOKEN", "").strip(),
            bluesky_url=endpoint("BLUESKY_PDS_URL", "https://bsky.social"),
            bluesky_identifier=os.getenv("BLUESKY_IDENTIFIER", "").strip(),
            bluesky_password=os.getenv("BLUESKY_APP_PASSWORD", "").strip(),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_channel=os.getenv("TELEGRAM_CHANNEL", "").strip(),
            api_key=os.getenv("APP_API_KEY", "").strip(),
            timeout=timeout,
            database_path=str(database if database.is_absolute() else ROOT / database),
            app_base_url=os.getenv("APP_BASE_URL", "http://127.0.0.1:4000").rstrip("/"),
            public_media_base_url=os.getenv("PUBLIC_MEDIA_BASE_URL", "").rstrip("/"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            instagram_app_id=os.getenv("INSTAGRAM_APP_ID", "").strip(),
            instagram_app_secret=os.getenv("INSTAGRAM_APP_SECRET", "").strip(),
            tiktok_client_key=os.getenv("TIKTOK_CLIENT_KEY", "").strip(),
            tiktok_client_secret=os.getenv("TIKTOK_CLIENT_SECRET", "").strip(),
            max_video_mb=max_video_mb,
        )
