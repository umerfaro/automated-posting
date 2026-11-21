"""Single-node SaaS identity, workspace isolation, usage and billing infrastructure."""
import asyncio
import hashlib
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
import sqlite3
import time

import httpx
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tenancy import identity, workspace

COOKIE = 'dispatch_session'


def serialized_billing(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        request=kwargs.get('request') or next(value for value in args if isinstance(value,Request))
        async with request.app.state.saas.billing_lock:
            return await function(*args,**kwargs)
    return guarded


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(default='', max_length=80)

    @field_validator('email')
    @classmethod
    def email_address(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Enter a valid email address.')
        return value


class EmailInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3,max_length=254)


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=20,max_length=200)


class ResetInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=20,max_length=200)
    password: str = Field(min_length=12,max_length=128)


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)

    @field_validator('name')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Enter a workspace name.')
        return value.strip()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=1, maxmem=64*1024*1024).hex()


class SaaS:
    def __init__(self, store, settings, client):
        self.store, self.settings, self.client = store, settings, client
        self.started_at = time.time()
        self.billing_lock = asyncio.Lock()
        bootstrap=getattr(settings,'baseline',settings)
        key = bootstrap.encryption_key
        if not key:
            keyfile = Path(bootstrap.database_path).parent / 'master.key'
            try:
                descriptor = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = keyfile.read_text().strip()
            else:
                key = Fernet.generate_key().decode()
                with os.fdopen(descriptor, 'w') as target:
                    target.write(key)
        store.vault = Fernet(key.encode())
        with store.connect() as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
                backup = Path(settings.database_path).with_name('pre-saas-' + str(time.time_ns()) + '.sqlite3')
                target = sqlite3.connect(backup)
                try:
                    source.backup(target)
                finally:
                    target.close()
                backup.chmod(0o600)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,salt TEXT NOT NULL,password TEXT NOT NULL,admin INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,name TEXT NOT NULL,owner TEXT NOT NULL,created REAL NOT NULL,customer TEXT UNIQUE,subscription TEXT,status TEXT NOT NULL DEFAULT 'free',plan TEXT NOT NULL DEFAULT 'free');
                CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user TEXT NOT NULL,workspace TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,request_key TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,response TEXT,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(workspace,request_key));
                CREATE INDEX IF NOT EXISTS publication_workspace_date ON publications(workspace,created);
                CREATE TABLE IF NOT EXISTS auth_attempts(bucket TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS auth_attempt_bucket ON auth_attempts(bucket,created);
                CREATE TABLE IF NOT EXISTS email_tokens(hash TEXT PRIMARY KEY,user TEXT NOT NULL,purpose TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS billing_checkouts(workspace TEXT PRIMARY KEY,session TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS billing_events(id TEXT PRIMARY KEY,created REAL NOT NULL);
            ''')
            if 'verified' not in {row[1] for row in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0')
            for table, name, definition in (
                ('users','suspended','INTEGER NOT NULL DEFAULT 0'),
                ('users','created','REAL NOT NULL DEFAULT 0'),
                ('users','last_login','REAL NOT NULL DEFAULT 0'),
                ('workspaces','suspended','INTEGER NOT NULL DEFAULT 0'),
                ('workspaces','quota_override','INTEGER'),
            ):
                if name not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,
                target_kind TEXT NOT NULL,target_id TEXT NOT NULL,details TEXT NOT NULL,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            # Preserve existing local data while encrypting credential material in place.
            for table, column, primary in [('settings','value','name'),('accounts','payload','platform')]:
                for name,value in db.execute(f'SELECT {primary},{column} FROM {table}').fetchall():
                    if value.startswith('gAAAA'):
                        try:
                            store.decode(value)
                        except Exception:
                            raise RuntimeError('The encryption key does not match saved credentials. Restore the original master key.') from None
                    else:
                        db.execute(f'UPDATE {table} SET {column}=? WHERE {primary}=?',(store.encode(value),name))
        store.saas = self

    def authenticate(self, cookie):
        if not cookie:
            return None
        with self.store.connect() as db:
            row = db.execute('''SELECT u.id,u.email,u.name,u.admin,s.workspace,s.csrf,s.hash,u.verified,w.suspended FROM sessions s
                JOIN users u ON u.id=s.user JOIN workspaces w ON w.id=s.workspace AND w.owner=u.id
                WHERE s.hash=? AND s.expires>? AND u.suspended=0''',(hashlib.sha256(cookie.encode()).hexdigest(),time.time())).fetchone()
        return dict(zip(('id','email','name','admin','workspace','csrf','session_hash','verified','workspace_suspended'),row)) if row else None

    def throttle(self, request, email):
        address = request.client.host if request.client else 'unknown'
        buckets = ['ip:'+address, 'email:'+hashlib.sha256(email.encode()).hexdigest()]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_attempts WHERE created<?',(time.time()-900,))
            for bucket in buckets:
                if db.execute('SELECT count(*) FROM auth_attempts WHERE bucket=?',(bucket,)).fetchone()[0]>=20:
                    raise HTTPException(429,'Too many sign-in attempts. Try again in 15 minutes.')
            db.executemany('INSERT INTO auth_attempts VALUES (?,?)',[(bucket,time.time()) for bucket in buckets])

    def origin(self, request):
        source = request.headers.get('origin')
        allowed = {self.settings.app_base_url, str(request.base_url).rstrip('/')}
        if source and source not in allowed:
            raise HTTPException(403,'This request must come from Dispatch.')

    def session_response(self, user, selected):
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM users WHERE id=? AND suspended=0',(user,)).fetchone():
                raise HTTPException(403,'This account is suspended. Contact the administrator.')
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user,selected,csrf,time.time()+7*86400))
        response = JSONResponse({'success':True})
        response.set_cookie(COOKIE,token,httponly=True,samesite='lax',secure=self.settings.app_base_url.startswith('https://'),max_age=7*86400)
        return response

    def reserve(self, db, selected, request_key, kind, title):
        plan = db.execute('SELECT plan,suspended,quota_override FROM workspaces WHERE id=?',(selected,)).fetchone()
        if not plan:
            raise HTTPException(403,'Workspace unavailable.')
        if plan[1] or self.settings.maintenance_mode:
            raise HTTPException(403,'Publishing is paused for this workspace or by the administrator.')
        limit = plan[2] if plan[2] is not None else (self.settings.pro_monthly_posts if plan[0]=='pro' else self.settings.free_monthly_posts)
        used = db.execute("SELECT count(*) FROM publications WHERE workspace=? AND strftime('%Y-%m',created)=strftime('%Y-%m','now')",(selected,)).fetchone()[0]
        if used>=limit:
            raise HTTPException(402,'Your custom publishing allowance has been reached. Contact the administrator.' if plan[2] is not None else 'Workspace monthly publishing limit reached. Manage your plan in Billing.')
        db.execute('INSERT INTO publications(id,workspace,request_key,kind,title,status) VALUES (?,?,?,?,?,?)',
                   (secrets.token_hex(16),selected,request_key,kind,title[:100],'processing'))

    def workspaces(self, user):
        with self.store.connect() as db:
            rows=db.execute('SELECT id,name,plan,status,suspended FROM workspaces WHERE owner=? ORDER BY created',(user,)).fetchall()
        return [dict(zip(('id','name','plan','status','suspended'),row)) for row in rows]

    def overview(self):
        selected=workspace.get()
        with self.store.connect() as db:
            rows=db.execute('SELECT id,kind,title,status,response,created FROM publications WHERE workspace=? ORDER BY created DESC,rowid DESC LIMIT 50',(selected,)).fetchall()
            used=db.execute("SELECT count(*) FROM publications WHERE workspace=? AND strftime('%Y-%m',created)=strftime('%Y-%m','now')",(selected,)).fetchone()[0]
            plan=db.execute('SELECT plan,status,suspended,quota_override FROM workspaces WHERE id=?',(selected,)).fetchone()
        history=[dict(zip(('id','kind','title','status','response','created'),row)) for row in rows]
        for item in history:
            item['response']=json.loads(item['response']) if item['response'] else None
        return {'history':history,'usage':used,'limit':plan[3] if plan[3] is not None else (self.settings.pro_monthly_posts if plan[0]=='pro' else self.settings.free_monthly_posts),
                'workspace_suspended':bool(plan[2]),'custom_allowance':plan[3] is not None,'upgrades_enabled':self.settings.upgrades_enabled,
                'billing_mode':'live' if self.settings.stripe_secret_key.startswith(('sk_live_','rk_live_')) else 'test' if self.settings.stripe_secret_key else 'unconfigured','maintenance_mode':self.settings.maintenance_mode,
                'free_limit':self.settings.free_monthly_posts,'pro_limit':self.settings.pro_monthly_posts,'plan':plan[0],'subscription_status':plan[1],'billing_enabled':self.billing_enabled()}

    def audit(self, action, target_kind, target_id, details=None, db=None, actor=None):
        user=identity.get()
        actor=actor or (user['id'] if user else 'system')
        payload=json.dumps(details or {})
        # Configuration audits record field names, never their secret values.
        payload=re.sub(r'\b(?:sk|rk|pk)_(?:test|live)_[A-Za-z0-9]+|\bwhsec_[A-Za-z0-9]+','[redacted]',payload)
        values=(actor,action,target_kind,target_id,payload)
        if db is not None:
            db.execute('INSERT INTO audit_log(actor,action,target_kind,target_id,details) VALUES (?,?,?,?,?)',values)
        else:
            with self.store.connect() as connection:
                connection.execute('INSERT INTO audit_log(actor,action,target_kind,target_id,details) VALUES (?,?,?,?,?)',values)

    def mail_configured(self):
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    async def send_email(self, recipient, subject, body):
        if not self.mail_configured():
            raise HTTPException(503,'Email delivery is not configured. Contact the administrator.')
        def deliver():
            message=EmailMessage()
            message['From']=self.settings.smtp_from
            message['To']=recipient
            message['Subject']=subject
            message.set_content(body)
            smtp_class=smtplib.SMTP_SSL if self.settings.smtp_port==465 else smtplib.SMTP
            options={'context':ssl.create_default_context()} if self.settings.smtp_port==465 else {}
            with smtp_class(self.settings.smtp_host,self.settings.smtp_port,timeout=15,**options) as smtp:
                if self.settings.smtp_port!=465:
                    smtp.starttls(context=ssl.create_default_context())
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username,self.settings.smtp_password)
                smtp.send_message(message)
        try:
            await asyncio.to_thread(deliver)
        except Exception:
            raise HTTPException(503,'Email could not be delivered. Try again or contact the administrator.') from None

    async def issue_email(self, user, recipient, purpose):
        token=secrets.token_urlsafe(40)
        with self.store.connect() as db:
            db.execute('DELETE FROM email_tokens WHERE user=? AND purpose=?',(user,purpose))
            db.execute('INSERT INTO email_tokens VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user,purpose,time.time()+(86400 if purpose=='verify' else 3600)))
        url=self.settings.app_base_url+('/#verify=' if purpose=='verify' else '/#reset=')+token
        await self.send_email(recipient,'Verify your Dispatch email' if purpose=='verify' else 'Reset your Dispatch password',
                              'Open this link to '+('verify your email' if purpose=='verify' else 'reset your password')+':\n\n'+url+'\n\nIf you did not request this, ignore this email.')

    def consume_email(self, token, purpose, db):
        digest=hashlib.sha256(token.encode()).hexdigest()
        row=db.execute('SELECT user FROM email_tokens WHERE hash=? AND purpose=? AND expires>?',(digest,purpose,time.time())).fetchone()
        if not row:
            raise HTTPException(400,'This email link is invalid or expired. Request a new one.')
        db.execute('DELETE FROM email_tokens WHERE hash=?',(digest,))
        return row[0]

    def billing_enabled(self):
        return bool(self.settings.stripe_secret_key and self.settings.stripe_pro_price_id and self.settings.stripe_webhook_secret)

    async def stripe(self, method, path, data=None, headers=None, secret_key=None):
        try:
            response=await self.client.request(method,'https://api.stripe.com/v1/'+path,data=data,
                headers={'Authorization':'Bearer '+(secret_key or self.settings.stripe_secret_key),'Stripe-Version':'2026-11-18.endive',**(headers or {})})
        except httpx.RequestError:
            raise HTTPException(503,'Billing is temporarily unavailable. Try again shortly.') from None
        if response.status_code>=400:
            raise HTTPException(502,'Stripe could not complete this request. Check billing configuration or try again.')
        try:
            result=response.json()
            if not isinstance(result,dict):
                raise ValueError()
        except ValueError:
            raise HTTPException(502,'Stripe returned an unexpected response. Try again shortly.') from None
        return result


def router(settings, authorize):
    routes=APIRouter(tags=['SaaS'])

    def service(request):
        if not settings.saas_enabled:
            raise HTTPException(404,'SaaS mode is disabled.')
        return request.app.state.saas

    @routes.get('/auth/me')
    async def me(request: Request):
        if not settings.saas_enabled:
            return {'saas':False}
        saas=service(request)
        user=identity.get()
        if not user:
            return {'saas':True,'authenticated':False,'registration_open':settings.registration_enabled}
        return {'saas':True,'authenticated':True,'verification_required':settings.email_verification_required,'maintenance_mode':settings.maintenance_mode,'user':{k:user[k] for k in ('id','email','name','admin','verified')},
                'csrf':user['csrf'],'workspace':user['workspace'],'workspaces':saas.workspaces(user['id'])}

    @routes.post('/auth/register')
    async def register(payload: Credentials, request: Request):
        saas=service(request); saas.origin(request); saas.throttle(request,payload.email)
        if not settings.registration_enabled:
            raise HTTPException(403,'New registrations are currently paused.')
        if settings.email_verification_required and not saas.mail_configured():
            raise HTTPException(503,'Account signup is temporarily unavailable. Email delivery needs to be configured.')
        salt=secrets.token_hex(16)
        digest=await asyncio.to_thread(password_hash,payload.password,salt)
        user,selected=secrets.token_hex(16),secrets.token_hex(16)
        try:
            with saas.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                first=db.execute('SELECT count(*) FROM users').fetchone()[0]==0
                # Public registration never grants admin based on an unverified email address.
                local = request.client and request.client.host in ('127.0.0.1','::1') and request.url.hostname in ('localhost','127.0.0.1','::1')
                admin = first and local and not settings.admin_email
                name=payload.name.strip() or payload.email.split('@')[0]
                db.execute('INSERT INTO users(id,email,name,salt,password,admin,created) VALUES (?,?,?,?,?,?,?)',(user,payload.email,name,salt,digest,int(admin),time.time()))
                db.execute('INSERT INTO workspaces(id,name,owner,created) VALUES (?,?,?,?)',(selected,name+"’s workspace",user,time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409,'This email is already registered. Sign in instead.') from None
        if settings.email_verification_required:
            await saas.issue_email(user,payload.email,'verify')
        return saas.session_response(user,selected)

    @routes.post('/auth/login')
    async def login(payload: Credentials, request: Request):
        saas=service(request); saas.origin(request); saas.throttle(request,payload.email)
        with saas.store.connect() as db:
            row=db.execute('SELECT id,salt,password,suspended FROM users WHERE email=?',(payload.email,)).fetchone()
        candidate=await asyncio.to_thread(password_hash,payload.password,row[1] if row else '00'*16)
        if not row or not hmac.compare_digest(candidate,row[2]):
            raise HTTPException(401,'Email or password is incorrect.')
        if row[3]:
            raise HTTPException(403,'This account is suspended. Contact the administrator.')
        with saas.store.connect() as db:
            db.execute('UPDATE users SET last_login=? WHERE id=?',(time.time(),row[0]))
        return saas.session_response(row[0],saas.workspaces(row[0])[0]['id'])

    @routes.post('/auth/resend-verification',dependencies=[Depends(authorize)])
    async def resend(request: Request):
        saas=service(request);user=identity.get()
        saas.throttle(request,user['email'])
        if user['verified']:
            return {'message':'Your email is already verified.'}
        await saas.issue_email(user['id'],user['email'],'verify')
        return {'message':'Verification email sent. Check your inbox.'}

    @routes.post('/auth/verify-email')
    async def verify(payload: VerifyInput, request: Request):
        saas=service(request);saas.origin(request)
        with saas.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user=saas.consume_email(payload.token,'verify',db)
            db.execute('UPDATE users SET verified=1 WHERE id=?',(user,))
        return {'message':'Your email is verified. You can sign in and publish.'}

    @routes.post('/auth/forgot-password')
    async def forgot(payload: EmailInput, request: Request):
        saas=service(request);saas.origin(request)
        email=payload.email.strip().lower();saas.throttle(request,email)
        if not saas.mail_configured():
            raise HTTPException(503,'Password recovery is unavailable until email delivery is configured. Contact the administrator.')
        with saas.store.connect() as db:
            row=db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
        if row:
            try:
                await saas.issue_email(row[0],email,'reset')
            except HTTPException:
                pass  # The public response must not reveal whether an account exists.
        return {'message':'If an account exists, a password reset link will be sent to its email address.'}

    @routes.post('/auth/reset-password')
    async def reset(payload: ResetInput, request: Request):
        saas=service(request);saas.origin(request);saas.throttle(request,payload.token)
        salt=secrets.token_hex(16)
        digest=await asyncio.to_thread(password_hash,payload.password,salt)
        with saas.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user=saas.consume_email(payload.token,'reset',db)
            db.execute('UPDATE users SET salt=?,password=? WHERE id=?',(salt,digest,user))
            db.execute('DELETE FROM sessions WHERE user=?',(user,))
            db.execute('DELETE FROM email_tokens WHERE user=? AND purpose=?',(user,'reset'))
        return {'message':'Password updated. Sign in with your new password.'}

    @routes.post('/auth/logout',dependencies=[Depends(authorize)])
    async def logout(request: Request):
        saas=service(request)
        with saas.store.connect() as db:
            db.execute('DELETE FROM sessions WHERE hash=?',(identity.get()['session_hash'],))
        response=JSONResponse({'success':True}); response.delete_cookie(COOKIE)
        return response

    @routes.get('/dashboard',dependencies=[Depends(authorize)])
    async def dashboard(request: Request):
        return service(request).overview()

    @routes.post('/workspaces',dependencies=[Depends(authorize)])
    async def create_workspace(payload: WorkspaceInput, request: Request):
        saas=service(request);user=identity.get()
        selected=secrets.token_hex(16)
        with saas.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM workspaces WHERE owner=?',(user['id'],)).fetchone()[0]>=10:
                raise HTTPException(409,'A user can create up to 10 workspaces.')
            db.execute('INSERT INTO workspaces(id,name,owner,created) VALUES (?,?,?,?)',(selected,payload.name,user['id'],time.time()))
        return {'id':selected,'name':payload.name}

    @routes.post('/workspaces/{selected}/switch',dependencies=[Depends(authorize)])
    async def switch_workspace(selected: str, request: Request):
        saas=service(request); user=identity.get()
        if selected not in {item['id'] for item in saas.workspaces(user['id'])}:
            raise HTTPException(404,'Workspace not found.')
        with saas.store.connect() as db:
            db.execute('UPDATE sessions SET workspace=? WHERE hash=?',(selected,user['session_hash']))
        return {'success':True}

    @routes.delete('/connections/{platform}',dependencies=[Depends(authorize)])
    async def disconnect(platform: str, request: Request):
        saas=service(request)
        if platform not in ('youtube','instagram','tiktok','mastodon','bluesky','telegram'):
            raise HTTPException(404,'Unknown platform.')
        key={'mastodon':'mastodon_token','bluesky':'bluesky_password','telegram':'telegram_token'}.get(platform)
        with saas.store.connect() as db:
            db.execute('DELETE FROM accounts WHERE platform=?',(saas.store.scoped(platform),))
            db.execute('DELETE FROM oauth_states WHERE platform=?',(saas.store.scoped(platform),))
        if key:
            saas.store.save_settings({key:''})
        return {'success':True}

    @routes.post('/billing/checkout',dependencies=[Depends(authorize)])
    @serialized_billing
    async def checkout(request: Request):
        saas=service(request)
        if not saas.billing_enabled():
            raise HTTPException(503,'Billing is not configured. The administrator must add Stripe keys, a webhook secret and a Pro price ID.')
        if not settings.upgrades_enabled:
            raise HTTPException(503,'New paid upgrades are temporarily paused. Existing subscriptions can still be managed.')
        selected=workspace.get();user=identity.get()
        with saas.store.connect() as db:
            row=db.execute('SELECT customer,plan,status FROM workspaces WHERE id=?',(selected,)).fetchone()
        if row[1]=='pro':
            raise HTTPException(409,'This workspace already has Pro. Use Manage billing.')
        customer=row[0]
        if not customer:
            result=await saas.stripe('POST','customers',{'email':user['email'],'metadata[workspace_id]':selected},headers={'Idempotency-Key':'customer-'+selected})
            customer=result['id']
            with saas.store.connect() as db:
                db.execute('UPDATE workspaces SET customer=? WHERE id=?',(customer,selected))
        with saas.store.connect() as db:
            previous=db.execute('SELECT session FROM billing_checkouts WHERE workspace=?',(selected,)).fetchone()
        if previous:
            existing=await saas.stripe('GET','checkout/sessions/'+previous[0])
            if existing.get('status')=='open':
                return {'url':existing['url']}
            if existing.get('status')=='complete' and row[2] not in ('canceled','incomplete_expired'):
                raise HTTPException(409,'Checkout is complete. Refresh your plan or use Manage billing for the existing subscription.')
        result=await saas.stripe('POST','checkout/sessions',{
            'mode':'subscription','customer':customer,'line_items[0][price]':settings.stripe_pro_price_id,
            'line_items[0][quantity]':'1','client_reference_id':selected,'subscription_data[metadata][workspace_id]':selected,
            'success_url':settings.app_base_url+'/?billing=success','cancel_url':settings.app_base_url+'/?billing=cancelled',
        },headers={'Idempotency-Key':'checkout-'+selected+'-'+(previous[0] if previous else 'initial')+'-'+row[2]+'-'+str(int(time.time()//1800))})
        with saas.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO billing_checkouts VALUES (?,?)',(selected,result['id']))
        return {'url':result['url']}

    @routes.post('/billing/portal',dependencies=[Depends(authorize)])
    async def portal(request: Request):
        saas=service(request)
        if not saas.billing_enabled():
            raise HTTPException(503,'Billing is not configured.')
        with saas.store.connect() as db:
            customer=db.execute('SELECT customer FROM workspaces WHERE id=?',(workspace.get(),)).fetchone()[0]
        if not customer:
            raise HTTPException(409,'This workspace has no billing customer yet.')
        result=await saas.stripe('POST','billing_portal/sessions',{'customer':customer,'return_url':settings.app_base_url+'/'})
        return {'url':result['url']}

    @routes.post('/billing/webhook')
    async def webhook(request: Request):
        saas=service(request)
        if not saas.billing_enabled():
            raise HTTPException(503,'Billing is not configured.')
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>1024*1024:
                raise HTTPException(413,'Webhook payload is too large.')
            body.extend(chunk)
        raw=bytes(body)
        try:
            parts=request.headers.get('stripe-signature','').split(',')
            stamp=next(part[2:] for part in parts if part.startswith('t='))
            expected=hmac.new(settings.stripe_webhook_secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
            if abs(time.time()-int(stamp))>300 or not any(hmac.compare_digest(expected,part[3:]) for part in parts if part.startswith('v1=')):
                raise ValueError()
            event=json.loads(raw)
            event_id=event['id'];obj=event['data']['object']
        except (ValueError,StopIteration,KeyError,TypeError):
            raise HTTPException(400,'Invalid Stripe signature or event.') from None
        with saas.store.connect() as db:
            if db.execute('SELECT 1 FROM billing_events WHERE id=?',(event_id,)).fetchone():
                return {'received':True}
        if event.get('type') in ('customer.subscription.created','customer.subscription.updated','customer.subscription.deleted'):
            # Fetch current provider state so delayed or reordered events cannot restore an expired plan.
            current=await saas.stripe('GET','subscriptions/'+obj['id'])
            prices={item['price']['id'] for item in current.get('items',{}).get('data',[])}
            plan='pro' if current.get('status') in ('active','trialing') and bool(prices & ({settings.stripe_pro_price_id} | set(settings.stripe_accepted_price_ids))) else 'free'
            with saas.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM billing_events WHERE id=?',(event_id,)).fetchone():
                    return {'received':True}
                db.execute('UPDATE workspaces SET plan=?,status=?,subscription=? WHERE customer=?',
                           (plan,current.get('status','unknown'),current['id'],current['customer']))
                db.execute('INSERT OR IGNORE INTO billing_events VALUES (?,?)',(event_id,time.time()))
                saas.audit('billing.subscription_updated','subscription',current['id'],{'plan':plan,'status':current.get('status','unknown')},db=db,actor='stripe')
        else:
            with saas.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO billing_events VALUES (?,?)',(event_id,time.time()))
        return {'received':True}

    return routes
