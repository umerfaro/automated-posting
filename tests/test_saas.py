from contextlib import ExitStack
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class SaaSTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.settings=Settings(database_path=str(Path(self.folder.name)/'saas.db'),saas_enabled=True,
                               admin_email='owner@example.com',stripe_secret_key='sk_test_fake',
                               stripe_webhook_secret='whsec_fake',stripe_pro_price_id='price_pro')
        self.stack=ExitStack();self.calls=[];self.subscription_status='active';self.checkout_status='open'
        self.owner=self.client();self.customer=self.client()
        self.signup(self.owner,'owner@example.com');self.signup(self.customer,'customer@example.com')

    def tearDown(self):
        self.stack.close();self.folder.cleanup()

    def client(self, settings=None):
        return self.stack.enter_context(TestClient(create_app(settings or self.settings,httpx.MockTransport(self.transport)),base_url="http://127.0.0.1:4000"))

    def transport(self,request):
        self.calls.append(request)
        path=request.url.path
        if path=='/v1/customers':return httpx.Response(200,json={'id':'cus_demo'})
        if path=='/v1/checkout/sessions':return httpx.Response(200,json={'id':'cs_demo','url':'https://checkout.stripe.com/c/pay/test'})
        if path=='/v1/checkout/sessions/cs_demo':return httpx.Response(200,json={'status':self.checkout_status,'url':'https://checkout.stripe.com/c/pay/test'})
        if path=='/v1/billing_portal/sessions':return httpx.Response(200,json={'url':'https://billing.stripe.com/p/session/test'})
        if path=='/v1/subscriptions/sub_demo':return httpx.Response(200,json={'id':'sub_demo','customer':'cus_demo','status':self.subscription_status,'items':{'data':[{'price':{'id':'price_pro'}}]}})
        if path=='/token':return httpx.Response(200,json={'access_token':'workspace-oauth-secret','refresh_token':'refresh','expires_in':3600})
        if path=='/youtube/v3/channels':return httpx.Response(200,json={'items':[{'snippet':{'title':'Workspace channel'}}]})
        if path=='/api/v1/accounts/verify_credentials':return httpx.Response(200,json={'acct':'demo'})
        raise AssertionError('Unexpected external request')

    def signup(self,client,email):
        response=client.post('/auth/register',json={'email':email,'password':'correct horse battery','name':'Demo'})
        self.assertEqual(response.status_code,200,response.text)
        if email=='owner@example.com':
            with client.app.state.store.connect() as db:
                db.execute('UPDATE users SET admin=1 WHERE email=?',(email,))
        me=client.get('/auth/me').json();client.headers['X-CSRF-Token']=me['csrf']
        return me

    def settings_save(self,client,platform,values):
        return client.put('/settings/'+platform,json={'values':values})

    def test_signup_private_session_and_no_password_readback(self):
        me=self.owner.get('/auth/me').json()
        self.assertTrue(me['authenticated']);self.assertTrue(me['user']['admin'])
        self.assertFalse(self.customer.get('/auth/me').json()['user']['admin'])
        self.assertNotIn('password',me['user'])
        with self.owner.app.state.store.connect() as db:
            password=db.execute('SELECT password FROM users LIMIT 1').fetchone()[0]
            self.assertNotEqual(password,'correct horse battery')
            session=db.execute('SELECT hash FROM sessions LIMIT 1').fetchone()[0]
            self.assertNotEqual(session,self.owner.cookies.get('dispatch_session'))
        self.assertEqual(self.owner.post('/auth/register',json={'email':'owner@example.com','password':'correct horse battery'}).status_code,409)

    def test_unauthenticated_requests_are_blocked(self):
        guest=self.client()
        for method,path in [('GET','/config'),('GET','/dashboard'),('GET','/settings/mastodon'),('GET','/video/accounts'),('POST','/posting'),('POST','/billing/checkout')]:
            response=guest.request(method,path,json={'message':'News https://example.com'} if path=='/posting' else None)
            self.assertEqual(response.status_code,401,(path,response.text))
        self.assertEqual(guest.get('/health').status_code,200)
        self.assertFalse(guest.get('/auth/me').json()['authenticated'])

    def test_workspace_credential_encryption_and_isolation(self):
        response=self.settings_save(self.owner,'mastodon',{'mastodon_token':'tenant-private-token'})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('tenant-private-token',response.text)
        own=self.owner.get('/settings/mastodon').json()['fields'][1]
        other=self.customer.get('/settings/mastodon').json()['fields'][1]
        self.assertTrue(own['saved']);self.assertFalse(other['saved'])
        self.assertFalse(self.customer.get('/config').json()['platforms'][0]['configured'])
        with self.owner.app.state.store.connect() as db:
            self.assertNotIn('tenant-private-token',str(db.execute('SELECT * FROM settings').fetchall()))
        self.assertTrue(self.owner.post('/settings/mastodon/test').json()['success'])
        self.assertEqual(self.calls[-1].headers['Authorization'],'Bearer tenant-private-token')

    def test_workspace_switch_membership_and_empty_new_workspace(self):
        self.settings_save(self.owner,'mastodon',{'mastodon_token':'token'})
        old=self.owner.get('/auth/me').json()['workspace']
        selected=self.owner.post('/workspaces',json={'name':'Second brand'}).json()['id']
        self.assertEqual(self.customer.post('/workspaces/'+selected+'/switch').status_code,404)
        self.assertEqual(self.owner.post('/workspaces/'+selected+'/switch').status_code,200)
        self.assertFalse(self.owner.get('/settings/mastodon').json()['fields'][1]['saved'])
        self.owner.post('/workspaces/'+old+'/switch')
        self.assertTrue(self.owner.get('/settings/mastodon').json()['fields'][1]['saved'])

    def test_csrf_and_origin_enforcement(self):
        self.assertEqual(self.owner.post('/workspaces',json={'name':'bad'},headers={'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.owner.post('/workspaces',json={'name':'bad'},headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.owner.post('/auth/login',json={'email':'owner@example.com','password':'correct horse battery'},headers={'Origin':'https://evil.example'}).status_code,403)

    def test_logout_revokes_session_and_login_works(self):
        cookie=self.owner.cookies.get('dispatch_session')
        self.assertEqual(self.owner.post('/auth/logout').status_code,200)
        self.owner.cookies.set('dispatch_session',cookie)
        self.assertEqual(self.owner.get('/dashboard').status_code,401)
        self.owner.cookies.clear()
        self.assertEqual(self.owner.post('/auth/login',json={'email':'owner@example.com','password':'wrong password here'}).status_code,401)
        self.assertEqual(self.owner.post('/auth/login',json={'email':'owner@example.com','password':'correct horse battery'}).status_code,200)
        self.assertTrue(self.owner.get('/auth/me').json()['authenticated'])

    def test_history_request_keys_and_usage_are_workspace_scoped(self):
        payload={'message':'Look here https://example.com'}
        first=self.owner.post('/posting',json=payload,headers={'Idempotency-Key':'same-key-123'})
        self.assertEqual(first.status_code,200,first.text)
        self.owner.post('/posting',json=payload,headers={'Idempotency-Key':'same-key-123'})
        data=self.owner.get('/dashboard').json()
        self.assertEqual(data['usage'],1);self.assertEqual(len(data['history']),1)
        self.assertEqual(self.customer.get('/dashboard').json()['usage'],0)
        response=self.customer.post('/posting',json={'message':'Different https://example.com'},headers={'Idempotency-Key':'same-key-123'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Different',self.customer.get('/dashboard').json()['history'][0]['title'])

    def test_plan_limit_is_enforced_server_side(self):
        limited=self.client(replace(self.settings,free_monthly_posts=1))
        self.signup(limited,'limited@example.com')
        payload={'message':'Look here https://example.com'}
        self.assertEqual(limited.post('/posting',json=payload).status_code,200)
        self.assertEqual(limited.post('/posting',json=payload).status_code,402)
        self.assertEqual(limited.get('/dashboard').json()['usage'],1)

    def test_developer_app_setup_is_operator_owned(self):
        self.assertEqual(self.settings_save(self.customer,'youtube',{'google_client_id':'client','google_client_secret':'private'}).status_code,403)
        self.assertEqual(self.settings_save(self.owner,'youtube',{'google_client_id':'client','google_client_secret':'private'}).status_code,200)
        data=self.customer.get('/settings/youtube').json()
        self.assertTrue(data['managed']);self.assertEqual(data['fields'],[])
        accounts=self.customer.get('/video/accounts').json()['accounts']
        self.assertTrue(accounts[0]['configured']);self.assertFalse(accounts[0]['connected'])
        self.assertNotIn('private',str(data))

    def test_oauth_state_cannot_be_completed_by_another_workspace(self):
        self.settings_save(self.owner,'youtube',{'google_client_id':'client','google_client_secret':'private'})
        started=self.owner.post('/video/accounts/youtube/connect').json()
        state=parse_qs(started['authorization_url'].split('?')[1])['state'][0]
        self.customer.cookies.set('oauth_youtube',state)
        response=self.customer.get('/oauth/youtube/callback',params={'state':state,'code':'somecode'})
        self.assertEqual(response.status_code,400)
        self.assertFalse(self.customer.get('/video/accounts').json()['accounts'][0]['connected'])
        with self.owner.app.state.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT state FROM oauth_states WHERE state=?',(state,)).fetchone())

    def test_checkout_uses_server_price_and_does_not_grant_pro(self):
        response=self.owner.post('/billing/checkout')
        self.assertEqual(response.status_code,200,response.text)
        request=self.calls[-1];form=parse_qs(request.content.decode())
        self.assertEqual(form['line_items[0][price]'],['price_pro'])
        self.assertEqual(self.owner.get('/dashboard').json()['plan'],'free')
        self.assertEqual(self.owner.post('/billing/portal').status_code,200)
        self.assertEqual(self.customer.post('/billing/portal').status_code,409)

    def signed(self,event,stamp=None):
        raw=json.dumps(event).encode();stamp=stamp or str(int(time.time()))
        signature=hmac.new(b'whsec_fake',stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        return raw,{'stripe-signature':f't={stamp},v1={signature}','content-type':'application/json'}

    def test_signed_webhook_updates_only_customer_workspace_and_is_idempotent(self):
        self.owner.post('/billing/checkout')
        event={'id':'evt_1','type':'customer.subscription.updated','data':{'object':{'id':'sub_demo'}}}
        raw,headers=self.signed(event)
        self.assertEqual(self.customer.post('/billing/webhook',content=raw,headers=headers).status_code,200)
        self.assertEqual(self.owner.get('/dashboard').json()['plan'],'pro')
        self.assertEqual(self.customer.get('/dashboard').json()['plan'],'free')
        count=len(self.calls)
        self.owner.post('/billing/webhook',content=raw,headers=headers)
        self.assertEqual(len(self.calls),count)
        self.subscription_status='canceled';event['id']='evt_2'
        raw,headers=self.signed(event)
        self.owner.post('/billing/webhook',content=raw,headers=headers)
        self.assertEqual(self.owner.get('/dashboard').json()['plan'],'free')

    def test_forged_and_expired_webhooks_cannot_upgrade(self):
        event={'id':'evt_fake','type':'customer.subscription.updated','data':{'object':{'id':'sub_demo'}}}
        raw,headers=self.signed(event)
        self.assertEqual(self.owner.post('/billing/webhook',content=raw+b' ',headers=headers).status_code,400)
        raw,headers=self.signed(event,str(int(time.time())-600))
        self.assertEqual(self.owner.post('/billing/webhook',content=raw,headers=headers).status_code,400)
        self.assertEqual(self.owner.get('/dashboard').json()['plan'],'free')

    def test_billing_is_disabled_without_configuration(self):
        client=self.client(replace(self.settings,stripe_secret_key=''))
        self.signup(client,'no-billing@example.com')
        self.assertEqual(client.post('/billing/checkout').status_code,503)
        self.assertFalse(client.get('/dashboard').json()['billing_enabled'])

    def test_database_restart_retains_credentials_and_session(self):
        self.settings_save(self.owner,'mastodon',{'mastodon_token':'persisted-token'})
        cookie=self.owner.cookies.get('dispatch_session')
        restarted=self.client();restarted.cookies.set('dispatch_session',cookie)
        self.assertTrue(restarted.get('/auth/me').json()['authenticated'])
        self.assertTrue(restarted.get('/settings/mastodon').json()['fields'][1]['saved'])

    def test_pending_video_history_remains_pending(self):
        from app.tenancy import workspace
        from app.schemas import VideoResponse, VideoResult
        selected=self.owner.get('/auth/me').json()['workspace']
        token=workspace.set(selected)
        try:
            store=self.owner.app.state.store
            store.claim('pending-video-key','sample identity',kind='video',title='Pending clip')
            store.finish('pending-video-key',VideoResponse(success=False,results=[VideoResult(platform='TikTok',status='pending')]))
        finally:
            workspace.reset(token)
        self.assertEqual(self.owner.get('/dashboard').json()['history'][0]['status'],'pending')

    def test_remote_registration_cannot_claim_admin_by_email(self):
        client=self.client(replace(self.settings,admin_email='attacker@example.com'))
        self.signup(client,'attacker@example.com')
        self.assertFalse(client.get('/auth/me').json()['user']['admin'])

    def test_expired_session_is_rejected(self):
        with self.owner.app.state.store.connect() as db:
            db.execute('UPDATE sessions SET expires=0 WHERE hash=?',(hashlib.sha256(self.owner.cookies.get('dispatch_session').encode()).hexdigest(),))
        self.assertEqual(self.owner.get('/dashboard').status_code,401)

    def test_open_checkout_is_reused(self):
        self.assertEqual(self.owner.post('/billing/checkout').status_code,200)
        self.assertEqual(self.owner.post('/billing/checkout').status_code,200)
        creations=[request for request in self.calls if request.url.path=='/v1/checkout/sessions' and request.method=='POST']
        self.assertEqual(len(creations),1)

    def test_migration_preserves_legacy_data_without_assigning_it_to_signup(self):
        from app.services.store import Store
        path=str(Path(self.folder.name)/'legacy.db')
        legacy=Store(path)
        legacy.save_settings({'mastodon_token':'old-private-token'})
        legacy.save_account('youtube',{'access_token':'old-youtube-token'})
        client=self.client(replace(self.settings,database_path=path))
        self.signup(client,'new-owner@example.com')
        self.assertFalse(client.get('/settings/mastodon').json()['fields'][1]['saved'])
        self.assertFalse(client.get('/video/accounts').json()['accounts'][0]['connected'])
        with client.app.state.store.connect() as db:
            self.assertNotIn('old-private-token',str(db.execute('SELECT * FROM settings').fetchall()))
            self.assertNotIn('old-youtube-token',str(db.execute('SELECT * FROM accounts').fetchall()))
        self.assertTrue(list(Path(self.folder.name).glob('pre-saas-*.sqlite3')))

    def test_trusted_host_rejects_unrecognized_domains(self):
        client=self.client(replace(self.settings,allowed_hosts=('dispatch.example',)))
        self.assertEqual(client.get('/auth/me',headers={'Host':'evil.example'}).status_code,400)
        self.assertEqual(client.get('/auth/me',headers={'Host':'dispatch.example'}).status_code,200)

    def test_customer_cannot_target_arbitrary_social_api_servers(self):
        response=self.settings_save(self.customer,'mastodon',{'mastodon_url':'https://127.0.0.1.nip.io'})
        self.assertEqual(response.status_code,422)
        response=self.settings_save(self.customer,'bluesky',{'bluesky_url':'https://unapproved.example'})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.calls,[])

    def test_canceled_subscription_can_start_a_new_checkout(self):
        self.owner.post('/billing/checkout')
        self.checkout_status='complete';self.subscription_status='canceled'
        raw,headers=self.signed({'id':'evt_cancel','type':'customer.subscription.deleted','data':{'object':{'id':'sub_demo'}}})
        self.assertEqual(self.owner.post('/billing/webhook',content=raw,headers=headers).status_code,200)
        self.assertEqual(self.owner.post('/billing/checkout').status_code,200)
        creations=[request for request in self.calls if request.url.path=='/v1/checkout/sessions' and request.method=='POST']
        self.assertEqual(len(creations),2)
        self.assertNotEqual(creations[0].headers['Idempotency-Key'],creations[1].headers['Idempotency-Key'])

    def test_authorized_video_account_is_encrypted_and_workspace_private(self):
        self.settings_save(self.owner,'youtube',{'google_client_id':'client','google_client_secret':'secret'})
        started=self.owner.post('/video/accounts/youtube/connect').json()
        state=parse_qs(started['authorization_url'].split('?')[1])['state'][0]
        response=self.owner.get('/oauth/youtube/callback',params={'state':state,'code':'code'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(self.owner.get('/video/accounts').json()['accounts'][0]['connected'])
        self.assertFalse(self.customer.get('/video/accounts').json()['accounts'][0]['connected'])
        with self.owner.app.state.store.connect() as db:
            self.assertNotIn('workspace-oauth-secret',str(db.execute('SELECT * FROM accounts').fetchall()))
        self.settings_save(self.owner,'youtube',{'google_client_id':'rotated-client'})
        self.assertFalse(self.owner.get('/video/accounts').json()['accounts'][0]['connected'])

    def test_verification_link_is_single_use_and_required_before_publishing(self):
        from unittest.mock import patch, AsyncMock
        import re
        mail=self.client(replace(self.settings,email_verification_required=True,smtp_host='smtp.example.com',smtp_from='dispatch@example.com'))
        with patch('app.saas.SaaS.send_email',new_callable=AsyncMock) as send:
            self.signup(mail,'verify@example.com')
            body=send.await_args.args[2]
            token=re.search(r'#verify=([^\s]+)',body).group(1)
        self.assertEqual(mail.post('/posting',json={'message':'Look https://example.com'}).status_code,403)
        self.assertEqual(mail.post('/auth/verify-email',json={'token':token}).status_code,200)
        self.assertEqual(mail.post('/auth/verify-email',json={'token':token}).status_code,400)
        self.assertTrue(mail.get('/auth/me').json()['user']['verified'])
        self.assertEqual(mail.post('/posting',json={'message':'Look https://example.com'}).status_code,200)

    def test_password_reset_is_single_use_and_revokes_old_sessions(self):
        from unittest.mock import patch, AsyncMock
        import re
        mail=self.client(replace(self.settings,smtp_host='smtp.example.com',smtp_from='dispatch@example.com'))
        with patch('app.saas.SaaS.send_email',new_callable=AsyncMock) as send:
            known=mail.post('/auth/forgot-password',json={'email':'owner@example.com'})
            body=send.await_args.args[2]
            token=re.search(r'#reset=([^\s]+)',body).group(1)
            unknown=mail.post('/auth/forgot-password',json={'email':'unknown@example.com'})
            self.assertEqual(known.json(),unknown.json())
        self.assertEqual(mail.post('/auth/reset-password',json={'token':token,'password':'new long password here'}).status_code,200)
        self.assertEqual(mail.post('/auth/reset-password',json={'token':token,'password':'another password here'}).status_code,400)
        self.assertEqual(self.owner.get('/dashboard').status_code,401)
        self.assertEqual(mail.post('/auth/login',json={'email':'owner@example.com','password':'correct horse battery'}).status_code,401)
        self.assertEqual(mail.post('/auth/login',json={'email':'owner@example.com','password':'new long password here'}).status_code,200)

    def test_public_signup_fails_closed_without_email_configuration(self):
        mail=self.client(replace(self.settings,email_verification_required=True))
        response=mail.post('/auth/register',json={'email':'no-mail@example.com','password':'correct horse battery'})
        self.assertEqual(response.status_code,503)
        with mail.app.state.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM users WHERE email=?',('no-mail@example.com',)).fetchone())

    def test_expired_recovery_token_cannot_change_password(self):
        from unittest.mock import patch, AsyncMock
        import re
        mail=self.client(replace(self.settings,smtp_host='smtp.example.com',smtp_from='dispatch@example.com'))
        with patch('app.saas.SaaS.send_email',new_callable=AsyncMock) as send:
            mail.post('/auth/forgot-password',json={'email':'owner@example.com'})
            token=re.search(r'#reset=([^\s]+)',send.await_args.args[2]).group(1)
        with mail.app.state.store.connect() as db:
            db.execute('UPDATE email_tokens SET expires=0')
        self.assertEqual(mail.post('/auth/reset-password',json={'token':token,'password':'new long password here'}).status_code,400)
        self.assertTrue(self.owner.get('/auth/me').json()['authenticated'])

    def test_webhook_body_limit_is_enforced_before_signature_processing(self):
        response=self.owner.post('/billing/webhook',content=b'x'*(1024*1024+1))
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.calls,[])
