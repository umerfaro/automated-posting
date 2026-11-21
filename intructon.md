# Dispatch setup and continuation

Updated November 21, 2026.

## Delivered work

- Converted the single-operator assessment application into a single-node SaaS MVP.
- Added signup/login/logout, salted password hashing, revocable sessions, CSRF protection and auth throttling.
- Added TLS SMTP verification/recovery, one-time hashed tokens and reset-time session revocation.
- Added owner-managed workspaces, creation/switching and server-side ownership checks.
- Isolated credentials, connected accounts, OAuth state, history, usage and request keys per workspace.
- Added encrypted credential/token storage and a pre-SaaS database backup on first migration.
- Retained Mastodon, Bluesky, Telegram, YouTube, Instagram and TikTok publishing and non-publishing tests.
- Moved shared video developer apps to administrator configuration. Customers authorize their own accounts.
- Added Overview, Create a post, Connected accounts and Plan & billing screens.
- Added durable publication history, result replay, quota reservation and Free/Pro allowances.
- Added Stripe Checkout, customer portal, signed/deduplicated subscription events and current-state reconciliation.
- Added non-root Docker packaging, Compose, environment examples and operator promotion tooling.

## Run and create your account

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 4000
```

Open http://127.0.0.1:4000. Create your own account. No demo user has been created in your real app database. First registration directly from the local machine can bootstrap administrator access when ADMIN_EMAIL is unset. For an existing public deployment, register your account and run:

```bash
python -m app.manage promote your@email.com
```

Public signup does not grant administrator privileges through an email match. Keep deployment-console access private.

## Preserve existing data and keys

First SaaS startup adds the new tables, creates data/pre-saas-*.sqlite3 and encrypts existing credential material. Original settings, accounts and request records remain in the legacy namespace. They are not assigned to the first arbitrary registered account. Reconnect accounts in the workspace you intend to use.

Back up the database, uploaded media as needed, and data/master.key together. If ENCRYPTION_KEY is configured, preserve that secret through your secret manager. Losing the key prevents token/credential decryption. Pre-migration backups contain original credentials and require restricted access. No user videos or keys were intentionally deleted during this conversion.

## Configure social publishing

Administrators open Connected accounts and use Set up for the shared Google, Instagram and TikTok developer apps. Each form links to the official credentials page and includes numbered instructions. APP_BASE_URL must exactly match the public application origin. Register each displayed OAuth redirect URI.

Customers use Connect to authorize their own video accounts. Approve any additional Mastodon instance or Bluesky PDS hostname through SOCIAL_ALLOWED_HOSTS before customers configure it. They use Set up to save workspace-specific Mastodon, Bluesky and Telegram details. Tests check account access without publishing. Rotate app secrets through the administrator forms. Changing an app clears that provider's prior authorized connections.

Instagram needs a professional account and a publicly reachable HTTPS media origin serving /media. TikTok and Google require appropriate app/test-user/production access. Provider review and account quotas still apply.

## Configure email delivery

Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM to your transactional provider. Port 587 uses STARTTLS and 465 uses implicit TLS. HTTPS deployments require verified email by default. Without SMTP configuration, required-verification signup returns an unavailable response instead of silently skipping verification. For explicit local testing, REQUIRE_EMAIL_VERIFICATION controls this behavior.

Verification links expire in 24 hours and reset links in one hour. Resending creates a new link. Tokens are single-use and stored hashed. Password reset revokes existing sessions. Use Forgot password on the login page, and Resend verification inside the workspace. Configure real SMTP and test inbox delivery in staging before inviting customers. Automated tests mock email delivery and do not send messages to real recipients.

## Activate subscription infrastructure

1. Create a recurring Pro product/price in Stripe test mode. Choose your actual price and currency there.
2. Configure STRIPE_SECRET_KEY, STRIPE_PRO_PRICE_ID and STRIPE_WEBHOOK_SECRET in the deployment environment. Restart after changes.
3. Configure the Stripe customer portal to allow the subscription operations you want customers to use.
4. Register the HTTPS /billing/webhook endpoint. Subscribe to customer.subscription.created, customer.subscription.updated and customer.subscription.deleted.
5. For local testing, forward events with the Stripe CLI. Use the forwarding secret as STRIPE_WEBHOOK_SECRET.
6. Open Plan & billing, create a test checkout and verify the subscription webhook upgrades the correct workspace.
7. Test cancellation, non-active states and portal access before using live keys.

No live Stripe charge has been made. Checkout is disabled until the required configuration is present. Checkout success alone does not change the plan. The signed webhook must reconcile current subscription state. Unknown/unavailable billing does not pretend to succeed.

Free defaults to 50 requests per workspace per calendar month, Pro to 1,000. Change FREE_MONTHLY_POSTS and PRO_MONTHLY_POSTS in the environment. A request uses one unit regardless of the number of destinations. Failures, pending and interrupted requests count. Completed request replays do not. Monthly reset follows UTC calendar months, independently of the subscription invoice anniversary.

## Deploy one node

```bash
cp .env.example .env
# Set your own public APP_BASE_URL, provider apps, billing secrets and encryption key.
docker compose up -d --build
docker compose exec dispatch python -m app.manage promote your@email.com
```

Compose binds the app to local port 4000. Put a trusted HTTPS reverse proxy in front of it, preserve the named data volume, configure request/upload size limits and allow enough request time for provider uploads. Set ALLOWED_HOSTS to the trusted public hostnames and forward only trusted proxy headers. Keep the public media path reachable by Instagram.

The Docker image was built successfully and tested as UID 10001. All 56 tests passed inside Linux Docker. An isolated container also passed real HTTP signup, administrator promotion, encrypted credential persistence across restart, workspace isolation, idempotency/history, authentication and billing-inactive checks. Cloud deployment and live SMTP/Stripe/provider setup still need staging verification before inviting paying users.

## Verification record

- All 56 automated tests passed locally and inside the built Linux Docker image, including the original 27 provider/settings tests.
- Added coverage for account/session behavior, membership checks, encrypted credentials, workspace isolation, OAuth-state ownership, quota/request replay and Stripe signatures/entitlements.
- Browser journey verified signup, credential save/disconnect, failed publication/history, usage, billing inactive state, separate new workspace, mobile layout, sign-out and sign-in. Forgot/reset forms and same-tab fragment navigation passed with mocked responses.
- Browser test users exist only in an isolated temporary test database. No live social post or payment was attempted.
- The working app remains on port 4000, separately from PeakCut.

## Next launch work

The current product runs as an owner-managed SaaS MVP. Before a broad public launch, configure and verify transactional SMTP, abuse controls, audit logs, monitoring, backup/restore drills and restricted network egress for user-defined social servers. Register production social apps, complete provider reviews, configure Stripe and deploy behind HTTPS.

For more than one app node, move to PostgreSQL, shared object storage and a durable publication queue. Team invitations/roles, scheduling and multiple accounts per provider are future features. Do not claim them in product copy until implemented.
