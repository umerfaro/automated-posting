"""Validation runs before any external API calls."""
import ipaddress
import re
from datetime import date as Date, time as Time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator

LINK_PATTERN = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>]+", re.I)
URL_ADAPTER = TypeAdapter(HttpUrl)


def cta_link(message: str) -> tuple[str, int, int]:
    matches = list(LINK_PATTERN.finditer(message))
    if len(matches) != 1:
        raise ValueError("Include exactly one CTA link starting with https:// or http://.")
    match = matches[0]
    text = match.group().rstrip(".,!;:")
    while text.endswith((")", "]", "}")) and text.count(text[-1]) > text.count({")": "(", "]": "[", "}": "{"}[text[-1]]):
        text = text[:-1]
    try:
        url = URL_ADAPTER.validate_python(text)
    except ValueError:
        raise ValueError("The CTA link must be a valid HTTP or HTTPS URL.") from None
    host = (url.host or "").rstrip(".").lower()
    if url.username or url.password or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("Use a public CTA URL without embedded login credentials.")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("The CTA link must use a public address.")
    return str(url), match.start(), match.start() + len(text)


class PostingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=200, examples=["Discover our latest tools. https://example.com"])

    @field_validator("message")
    @classmethod
    def validate_message(cls, value):
        if not value.strip() or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value):
            raise ValueError("Enter a message without control characters.")
        cta_link(value)
        return value


class PostingResult(BaseModel):
    platform: str
    status: Literal["success", "failed"]
    posted_url: str | None = None
    public_access: bool = False
    date: Date | None = None
    time: Time | None = None
    error: str | None = None


class PostingResponse(BaseModel):
    success: bool
    results: list[PostingResult]


class VideoResult(BaseModel):
    platform: str
    status: Literal["success", "failed", "pending"]
    posted_url: str | None = None
    public_access: bool = False
    date: Date | None = None
    time: Time | None = None
    remote_id: str | None = None
    error: str | None = None


class VideoResponse(BaseModel):
    success: bool
    results: list[VideoResult]
