# Dispatch — Automated Multi-Platform Posting

A Python 3.10+ and FastAPI assessment with a browser interface. A validated message is sent independently to Mastodon, Bluesky and a Telegram public channel. A separate video flow sends an uploaded MP4 to connected YouTube, Instagram and TikTok accounts.

The connected-account and video-publishing patterns are adapted from [PeakCut](https://github.com/haziqahmedqasmi/PeakCut). This project adds asynchronous delivery, bounded uploads, durable request keys, explicit consent and independent results.

## Run

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 4000
~~~

On Windows activate with .venv\Scripts\activate. Install FFmpeg and ffprobe on PATH for video validation. The text endpoint requires only the Python dependencies.

Open [the browser app](http://localhost:4000) or [Swagger documentation](http://localhost:4000/docs). For OAuth, open the exact APP_BASE_URL, defaulting to http://127.0.0.1:4000.

## Assessment API

~~~bash
curl -X POST http://localhost:4000/posting \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-message-request-001' \
  -d '{"message":"Discover our latest platform. https://example.com"}'
~~~

The only request field is message. It accepts at most 200 Unicode code points and exactly one explicit HTTP or HTTPS link. Invalid or extra fields return 422 before external calls. The URL is validated syntactically, without fetching the CTA destination. Embedded credentials, local hostnames and private literal IP addresses are rejected.

All three destinations are selected internally. Each result contains platform, status, posted_url, public_access, date, time and error. Top-level success is true if at least one platform succeeds. HTTP 200 can contain partial or total delivery failures. Dates and times are UTC.

Publication and public verification are separate. A successfully published post may have public_access false while indexing, moderation or privacy prevents an anonymous read. The result retains its published URL and includes an explanation.

Missing credentials produce real failed results. Production code does not simulate successful posts.

## Configure text platforms

- Mastodon requires MASTODON_BASE_URL and MASTODON_ACCESS_TOKEN with write:statuses plus profile or read:accounts for connection testing.
- Bluesky requires BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD. BLUESKY_PDS_URL must match the account's PDS. The default supports accounts hosted on bsky.social.
- Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL, a public @channel username. Add the bot as a channel administrator with posting permission.

Open either browser tab and click **Set up** beside a platform. Enter the credentials, click **Save settings**, then **Test connection**. Video platforms also need **Connect** to authorize the account through the provider. No manual .env editing or restart is needed for these settings.

Saved settings override environment defaults and persist in the ignored local SQLite database. Passwords and tokens are returned only as a saved flag. Leave a saved secret blank to retain it. Changing a video app's credentials clears its previous connection and outstanding sign-in states. The application address is shared by all video platforms, and each setup form shows the exact redirect URI.

Connection tests authenticate or retrieve account/creator details without publishing content. Mastodon testing also needs profile or read:accounts permission. Telegram checks bot administrator posting permission. A successful test does not guarantee available publishing quota, app review, or all publishing scopes. An unconnected video account must complete Connect before testing.

Optional .env credentials remain available as startup defaults. Restart after editing those defaults. Once a setting is saved in the UI, its stored override takes precedence. APP_API_KEY, database location and resource limits remain server environment settings.

Connection endpoints are GET /settings/{platform}, PUT /settings/{platform} with a values object, and POST /settings/{platform}/test. All use the same optional X-API-Key protection as posting. Supported platform IDs are mastodon, bluesky, telegram, youtube, instagram and tiktok.

## Video extension

The Video post tab connects accounts through OAuth and accepts an MP4, title, caption, visibility, audience and consent. All three destinations are handled independently. Unconnected accounts return failed results while others continue.

~~~bash
curl -X POST http://localhost:4000/video-posting \
  -H 'Idempotency-Key: demo-video-request-001' \
  -F 'file=@short.mp4;type=video/mp4' \
  -F 'title=My short video' \
  -F 'caption=A short description' \
  -F 'youtube_privacy=private' \
  -F 'tiktok_privacy=SELF_ONLY' \
  -F 'consent=true' \
  -F 'made_for_kids=false'
~~~

Video input must be H.264 MP4, at most 180 seconds and 4K, within MAX_VIDEO_MB (default 250 MB). Captions allow 2,200 characters. The assessment's 200-character and one-link rules apply to the separate text endpoint.

YouTube defaults to private. TikTok visibility is explicitly selected from current creator options. Instagram follows its professional-account publishing behavior.

A pending video result means processing is unconfirmed after the bounded polling period. Check the account before retrying. Uploaded files have one-hour public-link validity and are cleaned up by a subsequent upload after expiration.

## Reliability and access

The browser supplies an Idempotency-Key. Completed requests replay their saved response without posting again. A changed payload with the same key returns 409. Concurrent or interrupted requests also return 409 so the operator can check platform accounts before making a new attempt.

No automatic publish retries are performed. A timeout may happen after a platform accepted the post. Using a new key can create a duplicate.

Set APP_API_KEY to require X-API-Key on posting, credential setup, testing and video-account controls. The browser holds the key in page memory. Use HTTPS and an access key when exposing the service. SQLite tokens have owner-only file permissions but are not encrypted at rest. Protect the host and backups.

The application is designed for a single server operator. Local SQLite and media storage are not a distributed deployment.

## Tests

~~~bash
python -m unittest discover -s tests -v
~~~

Tests use HTTPX MockTransport. They cover message validation, partial delivery, Unicode link facets, authentication, rate limits, timeouts, sanitized errors, idempotency, OAuth state replay, token refresh, real MP4 inspection, upload protocols and media expiration. FFmpeg creates a tiny local fixture.

Live posting requires real credentials, developer apps, approved scopes and account authorization. The user requested mocked integration tests first, so no live posts were made.

## Official API research

- [Mastodon account verification](https://docs.joinmastodon.org/methods/accounts/#verify-account-credentials)
- [Telegram channel membership](https://core.telegram.org/bots/api#getchatmember)
- [Mastodon statuses](https://docs.joinmastodon.org/methods/statuses/)
- [Bluesky posts and link facets](https://docs.bsky.app/docs/advanced-guides/posts)
- [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)
- [YouTube resumable uploads](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol)
- [Google web OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Instagram publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- [Instagram business login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)
- [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post)
- [TikTok creator information](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info)
- [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/)
- [Pydantic HTTP URL types](https://docs.pydantic.dev/latest/api/networks/)

These networks have publication APIs suitable for the assignment. LLM completion APIs return generated text rather than publishing public posts.

## Design and continuation

[artchitectrue.md](artchitectrue.md) describes components and decisions. [intructon.md](intructon.md) records setup, completed work, verification and the next account steps. Filenames follow the requested deliverables.
