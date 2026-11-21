# Setup and continuation instructions

Updated November 21, 2026.

## Completed work

- Created a separate automated-posting project and isolated Python environment.
- Implemented POST /posting, the required message schema and exactly-one-link validation.
- Integrated Mastodon, Bluesky and Telegram text publication.
- Added independent asynchronous results, UTC times and anonymous public checks.
- Added durable optional request keys, sanitized errors and optional application API authentication.
- Built a responsive interface with text and video tabs and delivery reports.
- Added YouTube, Instagram and TikTok video publishing using PeakCut's connected-account approach.
- Added OAuth, state replay protection, TikTok PKCE and token refresh.
- Added MP4 inspection, upload limits, media expiration and bounded provider processing checks.
- Added mock API tests and a locally generated video fixture.
- Added architecture, setup instructions, API sources, environment example and MIT attribution.

## Start locally

~~~bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 4000
~~~

For a fresh checkout follow README.md to create the virtual environment and install requirements. Install FFmpeg for the video flow.

Open http://localhost:4000 and http://localhost:4000/docs. For OAuth, open the exact configured APP_BASE_URL, defaulting to http://127.0.0.1:4000.

Open Set up beside a platform in the browser. Save credentials and run Test connection. No restart is needed. Video accounts also require Connect after saving. Secrets remain on the server and saved values survive restarts. Optional environment variables take precedence over .env, while saved UI settings override both. APP_API_KEY and resource limits remain environment settings.

## Text platform setup

1. Create a Mastodon user token with write:statuses and profile or read:accounts scopes. Enter its server URL and access token in Mastodon → Set up, save and test.
2. Create a Bluesky app password. Enter your handle, app password and PDS origin in Bluesky → Set up, save and test.
3. Create a Telegram bot through BotFather. Give it posting permission as an administrator of a public channel. Enter the bot token and public @channel in Telegram → Set up, save and test.
4. Submit a short message with one HTTP or HTTPS CTA link and inspect each delivery result.

Use destinations that permit the submitted content. The app publishes the supplied message as written.

## YouTube connection

1. Enable YouTube Data API v3 in a Google Cloud project.
2. Configure OAuth consent and add your Google account as a test user when the app is in testing.
3. Create an OAuth client of type Web application.
4. Register http://127.0.0.1:4000/oauth/youtube/callback for the default local APP_BASE_URL.
5. Enter Client ID and Client secret in YouTube → Set up and click Save settings.
6. Connect YouTube from the Video post tab at APP_BASE_URL.
7. Choose the channel and grant the requested scopes. Review audience and visibility before posting.

Channel and API limits are returned as platform errors. There is no custom 24-hour pause.

## Instagram connection

1. Configure a Meta app with Instagram API and Instagram Login.
2. Use a Creator or Business Instagram account.
3. Enter the HTTPS origin serving this application as Application address in Set up. This address is shared by all video connections.
4. Register APP_BASE_URL/oauth/instagram/callback in Instagram business login settings.
5. Enter App ID and App secret in Instagram → Set up.
6. Enter a publicly reachable HTTPS media address serving this app's /media route in the Instagram setup form and save.
7. Connect Instagram from the Video post tab.
8. Ensure your account has tester or approved access as required by the Meta app.

Instagram must fetch media directly without an application login screen in front of the /media route. Links are random and expire after one hour. Set APP_API_KEY to protect posting controls when exposing the application.

## TikTok connection

1. Register a TikTok developer app with Login Kit and Content Posting API.
2. Register APP_BASE_URL/oauth/tiktok/callback with an HTTPS APP_BASE_URL.
3. Enable user.info.basic and video.publish scopes.
4. Enter Client key and Client secret in TikTok → Set up and save.
5. Connect TikTok.
6. Refresh connections to load current creator privacy and interaction options.
7. Explicitly choose available visibility, preview the video and confirm publishing.

Unaudited clients can be limited to private posting. Select SELF_ONLY when that is the permitted option. The app reports unsupported visibility instead of changing it silently.

## Verification

~~~bash
python -m unittest discover -s tests -v
~~~

External responses are mocked through HTTPX MockTransport. Video tests use FFmpeg to generate a tiny fixture and inspect real uploaded bytes. Real credentialed publication remains pending because the user requested mocked integration tests first.

Final verification results are appended below before delivery.

## Resume work

- Add real text credentials locally.
- Register video developer apps and authorize accounts.
- Ensure the browser origin and registered OAuth redirects match APP_BASE_URL.
- Supply a public HTTPS media origin for Instagram.
- Run one approved live test per platform and record actual links and privacy.
- Check platform accounts before retrying an uncertain or pending delivery.
- Keep .env, SQLite files, OAuth tokens and uploaded videos out of Git.

Idempotency keys are durable. A completed request replays its saved result; an interrupted request returns 409. New keys can create new posts, so inspect accounts before deliberately starting another request after an uncertain result.

## Verification record

- November 21, 2026 — 20 automated tests passed with mocked provider APIs.
- Python compilation and browser JavaScript syntax checks passed.
- Real local HTTP validation returned 422 for missing CTA links before any platform call.
- Real local requests without credentials returned three failed results, without simulated successes.
- Desktop and 390-pixel mobile browser checks passed with no uncaught JavaScript errors or horizontal overflow.
- Text/video tab switching, account readiness states, response cards and Swagger availability were verified.
- YouTube upload bytes, Instagram container/publish calls and TikTok upload chunks were verified through mocked API requests.
- The app runs separately on port 4000. PeakCut remains in its original project folder.

Live external publishing remains untested pending credentials and approved account connections, as agreed.

## Browser credential setup update

- Added Set up and Test connection controls for all six destinations.
- Added authenticated settings metadata and update endpoints, URL validation, secret redaction and persistent overrides.
- Added read-only profile/permission checks. Video checks use authorized accounts and may refresh access tokens. Bluesky checks create an authentication session.
- Changing a video developer app clears its old account connection. Connect again after saving.
- All 27 automated tests passed, including persistence across app restarts, access-key enforcement, secret preservation, and non-publishing checks across all six providers.
- Live checks still require your own credentials and provider authorization.

- Browser credential forms verified at desktop and 390-pixel mobile widths, including redirect display, Save payloads, secret clearing and failure feedback. Browser Save checks intercepted the request, so no fake credentials were persisted on the running server.

All six setup forms include links to official credential pages and numbered setup steps. Mastodon’s Development link follows the server URL entered in the form. Links open in a new tab so setup details stay available.
