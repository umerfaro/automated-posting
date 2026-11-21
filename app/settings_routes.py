"""Browser-managed credentials and non-publishing account checks."""
from dataclasses import replace
from contextvars import ContextVar
import ipaddress
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter

from app.platforms.base import PlatformError, checked
from app.tenancy import identity, workspace
from app.config import OPERATOR_KEYS

# The browser receives field metadata and saved flags, never stored secret values.
FIELDS = {
    'mastodon': [('mastodon_url', 'Server URL', False), ('mastodon_token', 'Access token', True)],
    'bluesky': [('bluesky_url', 'PDS server URL', False), ('bluesky_identifier', 'Handle or email', False), ('bluesky_password', 'App password', True)],
    'telegram': [('telegram_token', 'Bot token', True), ('telegram_channel', 'Public channel (@username)', False)],
    'youtube': [('google_client_id', 'Client ID', False), ('google_client_secret', 'Client secret', True), ('app_base_url', 'Application address', False)],
    'instagram': [('instagram_app_id', 'App ID', False), ('instagram_app_secret', 'App secret', True), ('app_base_url', 'Application address', False), ('public_media_base_url', 'Public HTTPS media address', False)],
    'tiktok': [('tiktok_client_key', 'Client key', False), ('tiktok_client_secret', 'Client secret', True), ('app_base_url', 'Application address', False)],
}
HELP = {
    'mastodon': 'Create a token with write:statuses and profile (or read:accounts) permissions. Testing checks the profile, without creating a post.',
    'bluesky': 'Use an app password from Bluesky Settings → App passwords.',
    'telegram': 'Create a bot with BotFather and add it as an administrator of a public channel with permission to post messages.',
    'youtube': 'Create a Google Web application OAuth client. Register the redirect URI below, save, then Connect. Add your Google account as a test user while the app is in testing.',
    'instagram': 'Use an Instagram Login developer app and a professional account. Register the redirect URI below. Application and media addresses must use public HTTPS.',
    'tiktok': 'Use a TikTok developer app with user.info.basic and video.publish. Register the redirect URI below. The application address must use public HTTPS.',
}


class RuntimeSettings:
    """Environment defaults plus persisted overrides, pinned for each HTTP request."""
    def __init__(self, baseline):
        self.baseline = baseline
        self.store = None
        self.context = ContextVar('dispatch_settings', default=None)

    def snapshot(self):
        values = self.store.settings() if self.store else {}
        base = self.baseline
        if base.saas_enabled:
            # Social credentials belong to workspaces; developer apps belong to the operator.
            base = replace(base, mastodon_token='', bluesky_identifier='', bluesky_password='', telegram_token='', telegram_channel='')
            if self.store:
                token = workspace.set('')
                try:
                    shared = self.store.settings()
                finally:
                    workspace.reset(token)
                keys = OPERATOR_KEYS
                base = replace(base, **{key:value for key,value in shared.items() if key in keys})
                values = {key:value for key,value in values.items() if key not in keys}
        return replace(base, **values)

    def __getattr__(self, name):
        return getattr(self.context.get() or self.snapshot(), name)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict[str, str] = Field(max_length=8)


def validate_value(key, value):
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError('Values must be at most 4096 characters with no control characters.')
    if key.endswith('_url') and value:
        url = TypeAdapter(HttpUrl).validate_python(value)
        local = url.host in ('localhost', '127.0.0.1', '[::1]', '::1')
        if url.username or url.password or url.query or url.fragment or url.path not in (None, '/'):
            raise ValueError('Use an origin address without a path, credentials, query or fragment.')
        if url.scheme != 'https' and not (key == 'app_base_url' and local):
            raise ValueError('Use HTTPS. The application address also permits HTTP on localhost.')
        try:
            private = not ipaddress.ip_address(url.host.strip('[]')).is_global
        except ValueError:
            private = url.host == 'localhost' or url.host.endswith(('.local', '.localhost'))
        if private and not (key == 'app_base_url' and local):
            raise ValueError('Use a public server address.')
        value = str(url).rstrip('/')
    if key == 'telegram_channel' and value and not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', value):
        raise ValueError('Use the public channel username, for example @mychannel.')
    if key in ('mastodon_url', 'bluesky_url', 'app_base_url') and not value:
        raise ValueError('Server address is required.')
    return value


def router(settings, authorize):
    routes = APIRouter(prefix='/settings', tags=['Connections'], dependencies=[Depends(authorize)])

    def metadata(platform):
        if platform not in FIELDS:
            raise HTTPException(404, 'Unknown platform.')
        current = settings.snapshot()
        customer_video = settings.saas_enabled and platform in ('youtube','instagram','tiktok') and not (identity.get() or {}).get('admin')
        return {
            'managed': customer_video,
            'platform': platform, 'help': HELP[platform],
            'fields': [{'name': key, 'label': label, 'secret': secret,
                        'saved': bool(getattr(current, key)),
                        'value': '' if secret else getattr(current, key)} for key, label, secret in ([] if customer_video else FIELDS[platform])],
            'redirect_uri': current.app_base_url + f'/oauth/{platform}/callback' if platform in ('youtube','instagram','tiktok') else None,
        }

    @routes.get('/{platform}')
    async def get_settings(platform: str):
        return metadata(platform)

    @routes.put('/{platform}')
    async def save_settings(platform: str, payload: SettingsUpdate, request: Request):
        metadata(platform)
        shared = settings.saas_enabled and platform in ('youtube','instagram','tiktok')
        if shared and not identity.get()['admin']:
            raise HTTPException(403, 'Developer apps are managed by the administrator. Use Connect to authorize your own account.')
        allowed = {key: secret for key, _, secret in FIELDS[platform]}
        if set(payload.values) - set(allowed):
            raise HTTPException(422, 'Unsupported settings field.')
        try:
            values = {key: validate_value(key, value) for key, value in payload.values.items()
                      if value.strip() or not allowed[key]}
        except ValueError:
            raise HTTPException(422, 'Invalid setting. Use public HTTPS origin URLs (HTTP localhost is allowed for the application address), a valid @channel, and values without control characters.') from None
        if settings.saas_enabled:
            for key in ('mastodon_url','bluesky_url'):
                if key in values and urlsplit(values[key]).hostname not in settings.social_allowed_hosts:
                    raise HTTPException(422, 'This social server is not enabled. Ask your administrator to approve its hostname.')
        current = settings.snapshot()
        changed_app = platform in ('youtube', 'instagram', 'tiktok') and any(
            getattr(current, key) != value for key, value in values.items() if key != 'public_media_base_url')
        token = workspace.set('') if shared else None
        try:
            request.app.state.store.save_settings(values, platform if changed_app else None)
        finally:
            if token is not None:
                workspace.reset(token)
        if shared and changed_app:
            # Invalidate every tenant's tokens when the operator rotates the shared app.
            with request.app.state.store.connect() as db:
                db.execute("DELETE FROM accounts WHERE platform LIKE ?", ('%:' + platform,))
                db.execute("DELETE FROM oauth_states WHERE platform LIKE ?", ('%:' + platform,))
        if settings.saas_enabled:
            request.app.state.saas.audit('integration.updated','platform',platform,{'fields':sorted(values)})
        return {**metadata(platform), 'message': 'Saved. Connect again to authorize the updated app.' if changed_app else 'Saved. Settings are active immediately.'}

    @routes.post('/{platform}/test')
    async def test_settings(platform: str, request: Request):
        metadata(platform)
        cfg, client = settings.snapshot(), request.app.state.client
        try:
            required = [key for key, _, _ in FIELDS[platform] if key != 'public_media_base_url']
            if not all(getattr(cfg, key) for key in required):
                raise PlatformError('Save the required credentials first.')
            name = ''
            if platform == 'mastodon':
                data = checked(await client.get(cfg.mastodon_url + '/api/v1/accounts/verify_credentials', headers={'Authorization': f'Bearer {cfg.mastodon_token}'}))
                name = data['acct']
            elif platform == 'bluesky':
                data = checked(await client.post(cfg.bluesky_url + '/xrpc/com.atproto.server.createSession', json={'identifier': cfg.bluesky_identifier, 'password': cfg.bluesky_password}))
                name = data['handle']
                if not data.get('accessJwt') or not data.get('did'):
                    raise PlatformError('The server did not confirm the account session.')
            elif platform == 'telegram':
                base = 'https://api.telegram.org/bot' + cfg.telegram_token
                async def telegram(method, params=None):
                    data = checked(await client.get(base + '/' + method, params=params))
                    if data.get('ok') is not True:
                        raise PlatformError('Telegram could not verify this bot or channel.')
                    return data['result']
                bot = await telegram('getMe')
                chat = await telegram('getChat', {'chat_id': cfg.telegram_channel})
                member = await telegram('getChatMember', {'chat_id': cfg.telegram_channel, 'user_id': bot['id']})
                if chat.get('type') != 'channel' or not chat.get('username') or member.get('status') not in ('administrator','creator') or (member.get('status') == 'administrator' and not member.get('can_post_messages')):
                    raise PlatformError('The bot must be an administrator of a public channel with permission to post messages.')
                name = '@' + chat['username']
            else:
                accounts = request.app.state.video_accounts
                token = await accounts.token(platform)
                if platform == 'youtube':
                    data = checked(await client.get('https://www.googleapis.com/youtube/v3/channels', params={'part':'snippet','mine':'true'}, headers={'Authorization':f'Bearer {token}'}))
                    name = data['items'][0]['snippet']['title']
                elif platform == 'instagram':
                    data = checked(await client.get('https://graph.instagram.com/me', params={'fields':'user_id,username,account_type'}, headers={'Authorization':f'Bearer {token}'}))
                    name = data['username']
                else:
                    data = await accounts.tiktok_creator()
                    name = data.get('creator_nickname') or data['creator_username']
            return {'success': True, 'message': f'Account access verified · {name}. No post was created. Publishing still depends on platform permissions and limits.'}
        except PlatformError as exc:
            message = str(exc)
        except httpx.RequestError:
            message = 'Could not reach the platform. Check the server address and try again.'
        except Exception:
            message = 'The platform did not return a valid account profile. Check credentials and permissions.'
        return {'success': False, 'message': message}

    return routes
