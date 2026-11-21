# Architecture

## Boundaries

Dispatch is an independent FastAPI project. PeakCut supplies the pattern of connected accounts, platform-specific publishing and normalized delivery outcomes. Text adapters implement APIs appropriate to messages with CTA links. Video adapters adapt PeakCut's OAuth, refresh, upload and processing sequence.

POST /posting is the assessment entry point. POST /video-posting is an extension. The browser calls these same APIs.

## Data flow

~~~mermaid
flowchart TD
    Client[Browser or REST client] --> API[FastAPI validation and optional API key]
    API --> Store[SQLite request-key claim]
    Store --> Text[Text posting service]
    Store --> Video[Video posting service]
    Text --> Mastodon[Mastodon]
    Text --> Bluesky[Bluesky PDS]
    Text --> Telegram[Telegram channel]
    Video --> OAuth[Connected account and token refresh]
    OAuth --> YouTube[YouTube resumable upload]
    OAuth --> Instagram[Instagram container and publish]
    OAuth --> TikTok[TikTok creator info and file upload]
    Mastodon --> Result[Independent platform results]
    Bluesky --> Result
    Telegram --> Result
    YouTube --> Result
    Instagram --> Result
    TikTok --> Result
    Result --> Store
    Result --> Client
~~~

## Modules

- app/main.py owns app lifespan, the shared asynchronous HTTP client, text routes and optional authorization.
- app/config.py reads environment settings without exposing credentials through API responses.
- app/schemas.py validates input and defines text and video result contracts.
- app/platforms/base.py defines the adapter contract and sanitized errors.
- app/platforms/mastodon.py creates public statuses and reads them anonymously.
- app/platforms/bluesky.py creates a session and post record with UTF-8 byte-offset link facets.
- app/platforms/telegram.py sends public-channel messages and checks their anonymous embeds.
- app/services/posting_service.py gathers independent text adapter results.
- app/services/store.py stores request fingerprints, responses, OAuth accounts and expiring states in SQLite.
- app/services/video_accounts.py manages sign-in and refresh for the three video platforms.
- app/services/video_posting.py handles resumable uploads, Instagram containers and TikTok chunks.
- app/video_routes.py validates actual files and consent, and serves expiring media links.
- app/web/index.html contains the responsive browser interface without a frontend build step.

## Reliability

Platforms execute as independent coroutines. Network and provider errors become individual failed results. Publish operations are not retried automatically because a platform may already have accepted a timed-out request.

SQLite claims keys in an immediate transaction. Completed keys replay the stored result. Payload changes, concurrent claims and interrupted requests return 409. The ledger prevents duplicate requests within this application but cannot guarantee exactly-once transactions across external APIs.

Message fingerprints hash the exact message. Video fingerprints include file bytes, title, caption, privacy and interaction settings. The request ledger does not retain raw message bodies or original filenames.

Text results use provider timestamps where returned. Bluesky uses the accepted record's createdAt. Video results use UTC confirmation time where providers return no publication timestamp. Pending processing is explicitly distinct from confirmed success.

## Public access

A text result's public_access is true only after an unauthenticated read confirms the object. Publication success can coexist with unverified public access. Instagram and TikTok video access remains conservatively unverified. Private TikTok publication may return no public link.

CTA URLs are validated without fetching destinations. API calls go to server-configured origins or known provider endpoints. Upload locations returned by providers require expected HTTPS hosts.

## OAuth and storage

Client secrets are supplied in environment variables. Access and refresh tokens are stored in an ignored SQLite database with owner-only file permissions on supporting systems. Storage is not encrypted at rest.

OAuth states expire after ten minutes, are single-use and are bound to a browser HttpOnly SameSite cookie. TikTok also uses PKCE. Token refresh is serialized within a process. This is a single-operator app; multi-user isolation and distributed refresh locking would require a separate design.

YouTube streams uploaded bytes to a resumable session. Instagram receives an expiring, random media URL on a configured public HTTPS origin. TikTok receives bounded upload chunks after creator settings are checked.

Uploads use random 256-bit filenames. Media links expire after an hour, support file ranges and are cleaned by subsequent uploads. A distributed deployment would require shared object storage, a queue and stronger operational controls.

## Verification boundary

The implementation includes real HTTP integrations and mocked provider tests. Live credentials, developer app approvals and real account authorization are external prerequisites. Tests never fabricate successful production results.

TikTok app audit rules and Instagram professional-account requirements still apply. The browser requires consent and visibility choices. Production platform compliance review and public hosting are outside the assessment.
