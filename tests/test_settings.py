"""Credential setup, persistence, redaction and non-publishing checks."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=str(Path(self.folder.name) / 'settings.db'))
        self.calls = []
        self.denied = False

    def tearDown(self):
        self.folder.cleanup()

    def transport(self, request):
        self.calls.append(request)
        if self.denied:
            return httpx.Response(401, json={'error': 'super-secret-token'})
        path = request.url.path
        data = {
            '/api/v1/accounts/verify_credentials': {'acct': 'demo'},
            '/xrpc/com.atproto.server.createSession': {'handle': 'demo.bsky.social', 'did': 'did:plc:demo', 'accessJwt': 'session'},
            '/bot123:secret/getMe': {'ok': True, 'result': {'id': 123}},
            '/bot123:secret/getChat': {'ok': True, 'result': {'type':'channel','username':'demo_channel'}},
            '/bot123:secret/getChatMember': {'ok': True, 'result': {'status':'administrator','can_post_messages':True}},
            '/youtube/v3/channels': {'items':[{'snippet':{'title':'Demo channel'}}]},
            '/me': {'username': 'demo_ig'},
            '/v2/post/publish/creator_info/query/': {'data':{'creator_nickname':'Demo creator'},'error':{'code':'ok'}},
        }.get(path)
        if data is None:
            raise AssertionError('Unexpected outbound call')
        return httpx.Response(200, json=data)

    def client(self, settings=None):
        return TestClient(create_app(settings or self.settings, httpx.MockTransport(self.transport)))

    def save(self, client, platform, values):
        return client.put('/settings/' + platform, json={'values':values})

    def test_saved_secrets_are_redacted_preserved_and_durable(self):
        with self.client() as client:
            response = self.save(client, 'mastodon', {'mastodon_token':'super-secret-token'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('super-secret-token', response.text)
            fields = client.get('/settings/mastodon').json()['fields']
            self.assertTrue(fields[1]['saved'])
            self.assertEqual(fields[1]['value'], '')
            self.save(client, 'mastodon', {'mastodon_token':''})
            self.assertEqual(client.app.state.store.settings()['mastodon_token'], 'super-secret-token')
            self.assertTrue(client.get('/config').json()['platforms'][0]['configured'])
        with self.client() as client:
            self.assertTrue(client.get('/config').json()['platforms'][0]['configured'])
            self.assertTrue(client.post('/settings/mastodon/test').json()['success'])
            self.assertEqual(self.calls[-1].headers['Authorization'], 'Bearer super-secret-token')

    def test_validation_rejects_unknown_fields_and_unsafe_addresses(self):
        with self.client() as client:
            for values in ({'api_key':'injected'}, {'mastodon_url':'http://example.com'},
                           {'mastodon_url':'https://127.0.0.1'}, {'mastodon_url':'https://example.com/path'},
                           {'mastodon_token':'token\nheader'}):
                self.assertEqual(self.save(client,'mastodon',values).status_code,422)
            self.assertEqual(client.app.state.store.settings(), {})
            self.assertEqual(client.get('/settings/unknown').status_code,404)
        self.assertEqual(self.calls, [])

    def test_setup_and_tests_require_access_key(self):
        with self.client(replace(self.settings,api_key='operator-key')) as client:
            for method,path in [('GET','/settings/mastodon'),('PUT','/settings/mastodon'),('POST','/settings/mastodon/test')]:
                self.assertEqual(client.request(method,path,json={'values':{}} if method=='PUT' else None).status_code,401)
            self.assertEqual(client.get('/settings/mastodon',headers={'X-API-Key':'operator-key'}).status_code,200)

    def test_all_six_checks_do_not_publish(self):
        with self.client() as client:
            credentials = {
                'mastodon': {'mastodon_token':'token'},
                'bluesky': {'bluesky_identifier':'demo.bsky.social','bluesky_password':'app-pass'},
                'telegram': {'telegram_token':'123:secret','telegram_channel':'@demo_channel'},
                'youtube': {'google_client_id':'client','google_client_secret':'secret'},
                'instagram': {'instagram_app_id':'client','instagram_app_secret':'secret'},
                'tiktok': {'tiktok_client_key':'client','tiktok_client_secret':'secret'},
            }
            for platform,values in credentials.items():
                self.assertEqual(self.save(client,platform,values).status_code,200)
                if platform in ('youtube','instagram','tiktok'):
                    client.app.state.store.save_account(platform, {'access_token':'access','expires_at':time.time()+3600})
                result = client.post('/settings/'+platform+'/test').json()
                self.assertTrue(result['success'], result)
            self.assertEqual(len(self.calls),8)
            self.assertTrue(all(req.method=='GET' or req.url.path.endswith(('createSession','creator_info/query/')) for req in self.calls))

    def test_video_app_changes_require_reconnect(self):
        with self.client() as client:
            self.save(client,'youtube',{'google_client_id':'first','google_client_secret':'secret'})
            client.app.state.store.save_account('youtube',{'access_token':'old'})
            self.save(client,'youtube',{'google_client_id':'first','google_client_secret':''})
            self.assertIsNotNone(client.app.state.store.account('youtube'))
            self.save(client,'youtube',{'google_client_id':'second'})
            self.assertIsNone(client.app.state.store.account('youtube'))
            response=client.post('/settings/youtube/test').json()
            self.assertFalse(response['success'])
            self.assertIn('Connect',response['message'])

    def test_missing_and_denied_credentials_never_report_success_or_leak(self):
        with self.client() as client:
            self.assertFalse(client.post('/settings/mastodon/test').json()['success'])
            self.save(client,'mastodon',{'mastodon_token':'super-secret-token'})
            self.denied=True
            response=client.post('/settings/mastodon/test')
            self.assertFalse(response.json()['success'])
            self.assertNotIn('super-secret-token',response.text)

    def test_local_callback_and_shared_application_address(self):
        with self.client() as client:
            response=self.save(client,'youtube',{'app_base_url':'http://localhost:4000/'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/settings/tiktok').json()['redirect_uri'],'http://localhost:4000/oauth/tiktok/callback')
