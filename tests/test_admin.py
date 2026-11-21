from contextlib import ExitStack
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.stack=ExitStack();self.calls=[]
        self.settings=Settings(database_path=str(Path(self.folder.name)/'admin.db'),saas_enabled=True,admin_email='admin@example.com')
        self.app=create_app(self.settings,httpx.MockTransport(self.transport))
        self.admin=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1:4000'))
        self.user=self.stack.enter_context(TestClient(create_app(self.settings,httpx.MockTransport(self.transport)),base_url='http://127.0.0.1:4000'))
        self.admin_info=self.register(self.admin,'admin@example.com')
        with self.admin.app.state.store.connect() as db:
            db.execute('UPDATE users SET admin=1 WHERE id=?',(self.admin_info['user']['id'],))
        self.user_info=self.register(self.user,'user@example.com')

    def tearDown(self):
        self.stack.close();self.folder.cleanup()

    def register(self,client,email):
        response=client.post('/auth/register',json={'email':email,'password':'test password twelve','name':'Example'})
        self.assertEqual(response.status_code,200,response.text)
        result=client.get('/auth/me').json();client.headers['X-CSRF-Token']=result['csrf'];return result

    def transport(self,request):
        self.calls.append(request);path=request.url.path;key=request.headers.get('Authorization','')
        if path=='/v1/account':
            if 'invalid' in key:return httpx.Response(401,json={'error':{'message':'sk_test_private_do_not_leak'}})
            return httpx.Response(200,json={'id':'acct_other' if 'other' in key else 'acct_platform','charges_enabled':True})
        if path.startswith('/v1/prices/'):
            price=path.rsplit('/',1)[1]
            return httpx.Response(200,json={'id':price,'active':price!='price_inactive','type':'one_time' if price=='price_oneoff' else 'recurring',
                'billing_scheme':'per_unit','livemode':'live' in price,'unit_amount':0 if price=='price_free' else 1500,'currency':'usd',
                'recurring':{'interval':'month','usage_type':'metered' if price=='price_metered' else 'licensed'}})
        if path=='/v1/customers':return httpx.Response(200,json={'id':'cus_new'})
        if path=='/v1/checkout/sessions':return httpx.Response(200,json={'id':'cs_new','url':'https://checkout.stripe.com/c/pay/mock'})
        if path=='/v1/billing_portal/sessions':return httpx.Response(200,json={'url':'https://billing.stripe.com/p/session/mock'})
        if path=='/v1/subscriptions/sub_original':return httpx.Response(200,json={'id':'sub_original','customer':'cus_old','status':'active','items':{'data':[{'price':{'id':'price_old'}}]}})
        raise AssertionError('Unexpected provider request')

    def stripe(self,**updates):
        values={'stripe_secret_key':'sk_test_platform','stripe_webhook_secret':'whsec_platform','stripe_pro_price_id':'price_pro'}
        values.update(updates);return self.admin.put('/admin/stripe',json={'values':values})

    def test_customers_cannot_read_or_mutate_admin_data(self):
        for path in ('overview','users','workspaces','publications','audit','settings','stripe'):
            self.assertEqual(self.user.get('/admin/'+path).status_code,403,path)
        self.assertEqual(self.user.put('/admin/stripe',json={'values':{}}).status_code,403)
        self.assertEqual(self.user.patch('/admin/users/'+self.admin_info['user']['id'],json={'admin':False,'reason':'attempted change'}).status_code,403)
        self.assertEqual(self.calls,[])

    def test_admin_mutations_require_csrf(self):
        response=self.admin.put('/admin/settings',json={'values':{'registration_enabled':False}},headers={'X-CSRF-Token':'wrong'})
        self.assertEqual(response.status_code,403)
        self.assertTrue(self.admin.get('/admin/overview').json()['registration_enabled'])

    def test_overview_search_and_pagination_use_real_counts(self):
        data=self.admin.get('/admin/overview').json()
        self.assertEqual(data['users'],2);self.assertEqual(data['workspaces'],2)
        result=self.admin.get('/admin/users',params={'q':'user@example.com','limit':1}).json()
        self.assertEqual(result['total'],1);self.assertEqual(result['items'][0]['email'],'user@example.com')
        self.assertNotIn('password',str(result));self.assertNotIn('csrf',str(result))
        self.assertEqual(len(self.admin.get('/admin/users',params={'limit':1,'offset':1}).json()['items']),1)
        self.assertEqual(self.admin.get('/admin/users',params={'limit':1000}).status_code,422)

    def test_self_demotion_and_suspension_are_blocked(self):
        target=self.admin_info['user']['id']
        for values in ({'admin':False},{'suspended':True}):
            self.assertEqual(self.admin.patch('/admin/users/'+target,json={**values,'reason':'support test'}).status_code,409)
        self.assertEqual(self.admin.get('/admin/overview').status_code,200)

    def test_suspend_revokes_login_and_reactivation_preserves_account(self):
        target=self.user_info['user']['id']
        self.assertEqual(self.admin.patch('/admin/users/'+target,json={'suspended':True,'reason':'abuse investigation'}).status_code,200)
        self.assertEqual(self.user.get('/dashboard').status_code,401)
        self.assertEqual(self.user.post('/auth/login',json={'email':'user@example.com','password':'test password twelve'}).status_code,403)
        self.assertEqual(self.admin.patch('/admin/users/'+target,json={'suspended':False,'reason':'investigation complete'}).status_code,200)
        self.assertEqual(self.user.post('/auth/login',json={'email':'user@example.com','password':'test password twelve'}).status_code,200)

    def test_role_changes_and_session_revocation_are_effective(self):
        target=self.user_info['user']['id']
        self.assertEqual(self.admin.patch('/admin/users/'+target,json={'admin':True,'reason':'operator promotion'}).status_code,200)
        self.assertEqual(self.user.get('/admin/overview').status_code,200)
        self.admin.patch('/admin/users/'+target,json={'admin':False,'reason':'operator removal'})
        self.assertEqual(self.user.get('/admin/overview').status_code,403)
        self.assertEqual(self.admin.post('/admin/users/'+target+'/revoke-sessions',json={'reason':'rotate access'}).status_code,200)
        self.assertEqual(self.user.get('/dashboard').status_code,401)
        self.assertEqual(self.admin.get('/dashboard').status_code,200)

    def test_workspace_pause_and_quota_override_keep_billing_separate(self):
        target=self.user_info['workspace'];message={'message':'News https://example.com'}
        self.assertEqual(self.admin.patch('/admin/workspaces/'+target,json={'quota_override':1,'reason':'temporary allowance'}).status_code,200)
        self.assertEqual(self.user.post('/posting',json=message).status_code,200)
        self.assertEqual(self.user.post('/posting',json=message).status_code,402)
        data=self.user.get('/dashboard').json();self.assertEqual(data['plan'],'free');self.assertTrue(data['custom_allowance'])
        self.admin.patch('/admin/workspaces/'+target,json={'suspended':True,'reason':'workspace review'})
        self.assertEqual(self.user.post('/posting',json=message).status_code,403)
        self.assertEqual(self.user.get('/dashboard').status_code,200)
        self.assertEqual(self.user.post('/billing/portal').status_code,503) # Allowed through suspension; Stripe is unconfigured.
        self.admin.patch('/admin/workspaces/'+target,json={'suspended':False,'quota_override':None,'reason':'restore plan defaults'})
        self.assertEqual(self.user.get('/dashboard').json()['limit'],50)
        self.assertEqual(self.user.post('/posting',json=message).status_code,200)

    def test_invalid_workspace_names_and_changes_are_rejected(self):
        target=self.user_info['workspace']
        for values in ({'name':None},{'name':' '},{'quota_override':True},{'suspended':None}):
            self.assertEqual(self.admin.patch('/admin/workspaces/'+target,json={**values,'reason':'validation test'}).status_code,422)
        self.assertEqual(self.admin.patch('/admin/workspaces/not-found',json={'name':'Other','reason':'support rename'}).status_code,404)

    def test_registration_and_maintenance_switches_apply_immediately(self):
        self.assertEqual(self.admin.put('/admin/settings',json={'values':{'registration_enabled':False,'maintenance_mode':True}}).status_code,200)
        response=self.user.post('/auth/register',json={'email':'new@example.com','password':'test password twelve'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.user.post('/posting',json={'message':'News https://example.com'}).status_code,503)
        self.assertEqual(self.user.post('/settings/mastodon/test').status_code,503)
        self.assertEqual(self.user.get('/dashboard').json()['usage'],0)
        self.assertEqual(self.admin.get('/admin/overview').status_code,200)
        self.assertEqual(self.admin.put('/admin/settings',json={'values':{'maintenance_mode':False}}).status_code,200)
        self.assertEqual(self.user.post('/posting',json={'message':'News https://example.com'}).status_code,200)

    def test_general_settings_validate_and_hide_secrets(self):
        response=self.admin.put('/admin/settings',json={'values':{'smtp_host':'smtp.example.com','smtp_password':'private-mail-password','smtp_from':'dispatch@example.com','free_monthly_posts':60,'pro_monthly_posts':1200}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('private-mail-password',response.text)
        self.assertEqual(self.user.get('/dashboard').json()['limit'],60)
        self.admin.put('/admin/settings',json={'values':{'smtp_password':''}})
        self.assertTrue(next(f for f in self.admin.get('/admin/settings').json()['fields'] if f['name']=='smtp_password')['saved'])
        for values in ({'stripe_secret_key':'sk_test_notallowed'},{'free_monthly_posts':2000},{'social_allowed_hosts':'*.example.com'},{'smtp_from':'invalid'},{'registration_enabled':'false'}):
            self.assertEqual(self.admin.put('/admin/settings',json={'values':values}).status_code,422)

    def test_stripe_draft_test_is_read_only_and_does_not_expose_keys(self):
        response=self.admin.post('/admin/stripe/test',json={'values':{'stripe_secret_key':'sk_test_platform','stripe_pro_price_id':'price_pro'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['account_id'],'acct_platform')
        self.assertTrue(all(call.method=='GET' for call in self.calls))
        self.assertFalse(self.user.get('/dashboard').json()['billing_enabled'])
        self.assertNotIn('sk_test_platform',response.text)

    def test_admin_stripe_form_enables_customer_upgrades_without_env_edit(self):
        response=self.stripe()
        self.assertEqual(response.status_code,200,response.text)
        metadata=self.admin.get('/admin/stripe')
        self.assertNotIn('sk_test_platform',metadata.text);self.assertNotIn('whsec_platform',metadata.text)
        self.assertEqual(metadata.json()['webhook_url'],'http://127.0.0.1:4000/billing/webhook')
        self.assertTrue(self.user.get('/dashboard').json()['billing_enabled'])
        self.assertEqual(self.user.post('/billing/checkout').status_code,200)
        request=next(call for call in self.calls if call.url.path=='/v1/checkout/sessions')
        self.assertEqual(request.headers['Authorization'],'Bearer sk_test_platform')
        self.assertIn(b'price_pro',request.content)
        with self.admin.app.state.store.connect() as db:
            stored=str(db.execute('SELECT * FROM settings').fetchall())
            self.assertNotIn('sk_test_platform',stored);self.assertNotIn('whsec_platform',stored)

    def test_invalid_prices_and_provider_errors_do_not_activate_billing(self):
        for price in ('price_oneoff','price_metered','price_inactive','price_free','price_live'):
            self.assertEqual(self.stripe(stripe_pro_price_id=price).status_code,422,price)
        response=self.stripe(stripe_secret_key='sk_test_invalid')
        self.assertEqual(response.status_code,502)
        self.assertNotIn('sk_test_private_do_not_leak',response.text)
        self.assertFalse(self.user.get('/dashboard').json()['billing_enabled'])

    def test_stripe_account_and_mode_changes_are_blocked_after_customer_linkage(self):
        self.stripe();self.user.post('/billing/checkout')
        self.assertEqual(self.stripe(stripe_secret_key='sk_test_other').status_code,409)
        self.assertEqual(self.stripe(stripe_secret_key='sk_live_platform',stripe_pro_price_id='price_live').status_code,409)
        self.assertEqual(self.admin.get('/admin/stripe').json()['account_id'],'acct_platform')

    def test_rotating_price_preserves_existing_paid_subscriptions(self):
        self.stripe(stripe_pro_price_id='price_old')
        selected=self.user_info['workspace']
        with self.admin.app.state.store.connect() as db:
            db.execute("UPDATE workspaces SET customer='cus_old',subscription='sub_original',plan='pro',status='active' WHERE id=?",(selected,))
        self.assertEqual(self.stripe(stripe_pro_price_id='price_new').status_code,200)
        event={'id':'evt_grandfather','type':'customer.subscription.updated','data':{'object':{'id':'sub_original'}}}
        raw=json.dumps(event).encode();stamp=str(int(time.time()));signature=hmac.new(b'whsec_platform',stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        response=self.user.post('/billing/webhook',content=raw,headers={'Stripe-Signature':f't={stamp},v1={signature}'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.user.get('/dashboard').json()['plan'],'pro')

    def test_upgrade_pause_does_not_block_subscription_portal_or_webhooks(self):
        self.stripe();self.user.post('/billing/checkout')
        self.admin.put('/admin/settings',json={'values':{'upgrades_enabled':False}})
        self.assertEqual(self.user.post('/billing/checkout').status_code,503)
        self.assertEqual(self.user.post('/billing/portal').status_code,200)
        self.assertTrue(self.user.get('/dashboard').json()['billing_enabled'])
        self.assertFalse(self.user.get('/dashboard').json()['upgrades_enabled'])

    def test_audit_contains_actions_and_reasons_without_configuration_secrets(self):
        self.stripe()
        self.admin.patch('/admin/workspaces/'+self.user_info['workspace'],json={'quota_override':100,'reason':'support courtesy sk_test_secretvalue'})
        response=self.admin.get('/admin/audit')
        self.assertEqual(response.status_code,200)
        self.assertIn('platform.stripe_updated',response.text);self.assertIn('workspace.updated',response.text)
        self.assertNotIn('sk_test_platform',response.text);self.assertNotIn('whsec_platform',response.text);self.assertNotIn('sk_test_secretvalue',response.text)
        self.assertIn('[redacted]',response.text)

    def test_email_test_targets_only_signed_in_admin(self):
        with patch('app.saas.SaaS.send_email',new_callable=AsyncMock) as send:
            response=self.admin.post('/admin/email/test',json={'email':'other@example.com'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(send.await_args.args[0],'admin@example.com')
            self.assertEqual(self.user.post('/admin/email/test').status_code,403)
            self.assertEqual(send.await_count,1)

    def test_account_rotation_waits_for_inflight_checkout(self):
        import asyncio
        async def scenario():
            started=asyncio.Event();release=asyncio.Event()
            async def transport(request):
                if request.url.path=='/v1/customers':
                    started.set()
                    await release.wait()
                return self.transport(request)
            app=create_app(self.settings,httpx.MockTransport(transport))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:4000',cookies=dict(self.admin.cookies),headers={'X-CSRF-Token':self.admin_info['csrf']}) as operator:
                    values={'stripe_secret_key':'sk_test_platform','stripe_webhook_secret':'whsec_platform','stripe_pro_price_id':'price_pro'}
                    self.assertEqual((await operator.put('/admin/stripe',json={'values':values})).status_code,200)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:4000',cookies=dict(self.user.cookies),headers={'X-CSRF-Token':self.user_info['csrf']}) as customer:
                        checkout=asyncio.create_task(customer.post('/billing/checkout'))
                        await asyncio.wait_for(started.wait(),2)
                        changed={**values,'stripe_secret_key':'sk_test_other'}
                        rotation=asyncio.create_task(operator.put('/admin/stripe',json={'values':changed}))
                        await asyncio.sleep(.02)
                        self.assertFalse(rotation.done())
                        release.set()
                        self.assertEqual((await checkout).status_code,200)
                        self.assertEqual((await rotation).status_code,409)
        asyncio.run(scenario())

    def test_encrypted_platform_settings_survive_a_fresh_app_start(self):
        self.assertEqual(self.stripe().status_code,200)
        self.admin.put('/admin/settings',json={'values':{'smtp_password':'restart-mail-secret','free_monthly_posts':75}})
        restarted=self.stack.enter_context(TestClient(create_app(self.settings,httpx.MockTransport(self.transport)),base_url='http://127.0.0.1:4000'))
        restarted.cookies.update(dict(self.admin.cookies))
        response=restarted.get('/admin/stripe')
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['enabled'])
        self.assertEqual(response.json()['account_id'],'acct_platform')
        self.assertNotIn('sk_test_platform',response.text)
        self.assertEqual(restarted.get('/dashboard').json()['limit'],75)
