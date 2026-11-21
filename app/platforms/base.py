"""Adapter contract and sanitized errors, following PeakCut's per-platform posting pattern."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


class PlatformError(Exception):
    pass


@dataclass
class PublishedPost:
    url: str
    created_at: datetime
    public: bool


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Platform timestamp is missing its timezone.")
    return parsed.astimezone(timezone.utc)


class Platform(ABC):
    name: str

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def publish(self, client: httpx.AsyncClient, message: str) -> PublishedPost: ...


def checked(response: httpx.Response) -> dict:
    if response.status_code in (401, 403):
        raise PlatformError("Authentication or posting permission denied. Check the server credentials.")
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        suffix = f" Try again after {retry} seconds." if retry.isdecimal() else ""
        raise PlatformError("Platform rate limit reached." + suffix)
    if response.status_code >= 400:
        raise PlatformError(f"Platform rejected the request (HTTP {response.status_code}).")
    try:
        data = response.json()
    except ValueError:
        raise PlatformError("Platform returned an invalid JSON response.") from None
    if not isinstance(data, dict):
        raise PlatformError("Platform returned an unexpected response.")
    return data
