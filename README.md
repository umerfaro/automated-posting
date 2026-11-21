# Dispatch SaaS

A FastAPI publishing SaaS MVP with user accounts, isolated workspaces, six social platforms, publishing history and Stripe subscription infrastructure. The existing text/video integrations are adapted from [PeakCut](https://github.com/haziqahmedqasmi/PeakCut).

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 4000
```

Requires Python 3.10+ and FFmpeg/ffprobe for video. Open [Dispatch](http://127.0.0.1:4000) and create an account. [API documentation](http://127.0.0.1:4000/docs) remains available.

SaaS mode is enabled by default. The first account registered directly from the local machine becomes an administrator when ADMIN_EMAIL is unset. Public signup never grants administrator privileges by matching an email address. Operators can promote an existing account through the deployment console:

```bash
python -m app.manage promote owner@example.com
```

The dashboard provides Overview, Create a post, Connected accounts, and Plan & billing. Create and switch between up to 10 owner-managed workspaces. Each workspace has its own credentials, OAuth connections, request keys, history, usage and subscription.

## Customer and administrator setup

Customers configure Mastodon, Bluesky and Telegram credentials through Set up, Save settings and Test connection. Forms contain official credential-page links and numbered instructions. Secrets are never returned in API responses. Blank secret inputs preserve saved credentials.

Administrators configure the shared YouTube, Instagram and TikTok developer applications once through the setup forms. Customers use Connect to authorize their own social accounts. Changing a shared app invalidates its existing workspace connections so accounts can reconnect using the new app.

Mastodon needs write:statuses and profile or read:accounts. Bluesky uses an app password and the account's PDS. Telegram needs a public @channel and a bot administrator with posting permission. Instagram needs a professional account, public HTTPS application/media origins, and appropriate app access. TikTok needs approved scopes and creator access. YouTube requires a channel, Google OAuth consent and appropriate test-user or production access.

## Authentication and isolation

Passwords use salted scrypt hashes. Opaque seven-day sessions use hashed server-side identifiers and HttpOnly, SameSite=Lax cookies. HTTPS application addresses enable Secure cookies. Mutating authenticated requests require the session's X-CSRF-Token and reject foreign origins. Signup/login have persisted 15-minute rate limits.

GET /auth/me provides the signed-in user's CSRF token and workspace memberships. POST /auth/register and POST /auth/login accept email, password and optional name. Passwords require 12–128 characters. POST /auth/logout revokes the current session. Workspace switching validates server-side ownership. The application API key does not bypass SaaS authentication.

Credential settings and OAuth token payloads are encrypted with Fernet. Configure ENCRYPTION_KEY through your secret manager, or keep the automatically generated data/master.key. Back up both the database and its encryption key. File permissions are owner-only on supported systems. Email verification and password recovery use one-time, hashed, expiring tokens and TLS-protected SMTP. HTTPS deployments require email verification by default. Configure SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM before allowing public signup. Local development leaves verification optional unless REQUIRE_EMAIL_VERIFICATION=true. Publishing, provider connection/tests and checkout require verified email when enabled. Password reset revokes the user's existing sessions. Email links keep their tokens in URL fragments so they are not sent in HTTP request URLs. No real email has been sent during testing.

Signup does not assign old local credentials or publishing history to an arbitrary new user.

On first SaaS migration, an owner-only pre-saas-*.sqlite3 backup is created before credential encryption. Existing local data stays in the legacy namespace. This backup contains the original credential material and needs the same protection as the original database.

## Text API

POST /posting accepts exactly one field:

```json
{"message":"Discover our latest tools. https://example.com"}
```

Messages allow up to 200 Unicode code points and exactly one public HTTP/HTTPS CTA link. Invalid input returns 422 before provider calls. CTA validation does not fetch the target. Platforms are selected internally. Mastodon, Bluesky and Telegram deliver independently.

In SaaS mode, requests need a session cookie and X-CSRF-Token. An optional Idempotency-Key prevents duplicate publication and is scoped to the active workspace. The server generates a request key when omitted. Completed responses replay without using additional allowance. Different content or an interrupted request using the same key returns 409.

Each platform result includes platform, status, posted_url, public_access, date, time and error. Dates and times are UTC. Success means at least one platform confirmed publication. Anonymous public-access verification is separate from successful publishing.

For the original standalone assessment contract, use SAAS_ENABLED=false with a separate legacy database. APP_API_KEY then optionally protects endpoints. Do not turn off SaaS authentication on the public deployment.

## Video API

POST /video-posting accepts multipart file, title, caption, youtube_privacy, tiktok_privacy, consent, made_for_kids and interaction flags. Files must be H.264 MP4, at most 180 seconds and 4K, within MAX_VIDEO_MB (250 MB by default). Consent is required. The separate text endpoint's CTA rules do not apply to video captions.

YouTube uses resumable upload, Instagram uses a publicly fetchable random media URL, and TikTok uses creator settings and bounded chunks. Private/unverified posts may have no public link. Pending processing stays pending in history. Media links expire after one hour and expired files are cleaned by subsequent uploads. No automatic publication retries are performed.

## Plans and Stripe billing

Free defaults to 50 publishing requests per workspace per UTC calendar month. Pro defaults to 1,000. Configure FREE_MONTHLY_POSTS and PRO_MONTHLY_POSTS to change allowances. One request can deliver to three platforms. Failed, pending and interrupted requests count because delivery may already have occurred. Quota reservation and request-key creation share one database transaction. Limit exhaustion returns 402 before another provider delivery.

Configure billing through **Administration → Stripe & subscriptions**. The form verifies and saves the platform configuration immediately. Environment variables remain an optional startup fallback. Billing remains inactive until all of these are configured:

- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET
- STRIPE_PRO_PRICE_ID, a recurring subscription price

POST /billing/checkout uses the configured server-side price and active workspace's customer. Open checkout sessions are reused. POST /billing/portal opens that workspace's customer portal. Configure the portal in Stripe before use. Pricing is shown in Checkout; the UI does not invent a price.

Register POST /billing/webhook with Stripe and subscribe to customer.subscription.created, customer.subscription.updated and customer.subscription.deleted. Webhooks verify the raw-body HMAC signature with a five-minute tolerance. Duplicate event IDs are ignored. Current subscription state is fetched from Stripe so delayed notifications do not blindly restore old state. Only active/trialing subscriptions with the configured Pro price receive Pro allowance. Other statuses use Free allowance. Subscription changes apply after webhook confirmation, never from a checkout return URL.

Use Stripe test-mode keys and webhook forwarding first. No live charge or live provider post has been performed in development.

## Deployment

A Dockerfile and compose.yaml run the app as a non-root user with FFmpeg and persistent data. Copy .env.example to .env, configure your deployment values, then run:

```bash
docker compose up -d --build
```

Compose binds to 127.0.0.1:4000 for a local HTTPS reverse proxy. Set APP_BASE_URL to your public HTTPS origin and register its exact OAuth callbacks. PUBLIC_MEDIA_BASE_URL must expose this app's /media route to Instagram. Keep the data volume and encryption key across deployments. Promote the administrator with docker compose exec dispatch python -m app.manage promote owner@example.com.

This release is a single-node MVP. Publishing runs within the HTTP request, and a lost request can remain interrupted. Before a broad public launch, configure transactional SMTP, automated backups, monitoring, abuse controls and deployment-level network egress restrictions. Customer-configurable social API hosts are already restricted by SOCIAL_ALLOWED_HOSTS, which defaults to mastodon.social and bsky.social. Add trusted Mastodon instances or Bluesky PDS hosts through operator configuration. A multi-replica version needs PostgreSQL, shared object storage and a durable worker queue. Billing and social developer-app approvals must be completed for your own accounts. See [deployment notes](intructon.md).

## Verification and design

```bash
python -m unittest discover -s tests -v
```

All 76 tests passed locally and inside the Linux Docker image. A separate container smoke test verified real HTTP flows, non-root execution and encrypted data/session persistence across restart.

Tests use HTTPX MockTransport and local FFmpeg fixtures. They cover existing provider protocols plus SaaS authentication, workspace isolation, encrypted persistence, CSRF, membership checks, usage limits, idempotency, OAuth state ownership and signed Stripe events. Browser QA uses an isolated database and does not create users in the operator's real database.

[artchitectrue.md](artchitectrue.md) describes boundaries and data flow. [intructon.md](intructon.md) records implementation, operator setup, verification and launch work.

Official references include [Stripe Checkout](https://docs.stripe.com/api/checkout/sessions/create), [Stripe customer portal](https://docs.stripe.com/api/customer_portal/sessions/create), [Stripe subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks), [Fernet](https://cryptography.io/en/stable/fernet/), [Mastodon](https://docs.joinmastodon.org/methods/statuses/), [Bluesky](https://docs.bsky.app/docs/advanced-guides/posts), [Telegram](https://core.telegram.org/bots/api), [Google OAuth](https://developers.google.com/identity/protocols/oauth2/web-server), [Instagram](https://developers.facebook.com/documentation/instagram-platform/content-publishing) and [TikTok](https://developers.tiktok.com/doc/content-posting-api-get-started/).

To repeat the integration suite inside the container:

```bash
docker build -t dispatch-saas:local .
docker run --rm -v "$PWD/tests:/app/tests:ro" dispatch-saas:local python -m unittest discover -s tests -v
```

## Administration

Administrators have an **Administration** sidebar entry with Overview, Users, Workspaces, Publishing, Stripe & subscriptions, Platform settings and Audit log.

- Search and paginate users, change administrator roles, suspend/reactivate access and revoke sessions. Your signed-in administrator cannot demote or suspend itself.
- Rename or pause workspaces and set optional publishing-allowance overrides. Overrides change usage limits, without changing a Stripe subscription or claiming payment.
- Inspect publishing metadata and service configuration without reading customer post content or social credentials.
- Control registration, maintenance, new paid upgrades, default plan allowances, approved social API hosts and SMTP settings. Maintenance pauses new publication and provider connections; existing requests may finish.
- Configure shared video apps and send a test email to the signed-in administrator.
- Review an audit of privileged changes and subscription updates. Configuration secrets are never included in audit details.

Suspension is separate from Stripe cancellation. Suspending a user revokes sessions and blocks sign-in; recurring billing continues until canceled in Stripe. A paused workspace still allows its owner to open the billing portal. Operators should handle paid-account requests through Stripe rather than assuming suspension cancels a subscription.

### Set up the platform Stripe account in the browser

Open **Administration → Stripe & subscriptions**. Links take you to Stripe API keys, products/prices, webhooks and customer-portal settings. Enter the platform secret/restricted key, an active paid recurring Pro price ID, and the webhook signing secret. Use **Test Stripe connection**, then **Save Stripe settings**. Saving also performs the read-only account/price checks before activation. No charge is created by testing or saving.

Blank secrets preserve saved values. Saved values are encrypted and override environment defaults immediately, including after restart. Ordinary users cannot access these fields. Users open **Plan & billing → Upgrade to Pro**, complete platform-owned Checkout, and receive Pro only after a signed subscription event confirms entitlement.

The account/price test cannot verify a webhook secret without a delivered event. The form displays the exact endpoint, required subscription event types, and whether a signed event arrived after the latest save. Configure the Stripe customer portal separately. Restricted keys need permission for the account/price reads and the customer, checkout, portal and subscription operations used by the app.

Changing the Stripe account or test/live mode is blocked after customer billing becomes linked. Use separate staging/production data or a planned billing migration. Rotating the Pro price retains prior configured price IDs for existing paid subscriptions. A single-process billing lock prevents account configuration changes from racing an in-flight checkout. Multi-worker deployments require coordinated locking.

Platform settings include a separate **Allow new paid upgrades** switch. Disabling it preserves customer-portal access and signed webhook processing. All purchase amounts remain defined by the configured Stripe price and are shown in Checkout.
