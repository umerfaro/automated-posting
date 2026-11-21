"""Operator administration. Customer payment credentials are never exposed here."""
import json
import re
import shutil
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.tenancy import identity, workspace
from app.saas import serialized_billing

GENERAL_FIELDS = [
    ('registration_enabled','Allow new registrations','boolean'),
    ('maintenance_mode','Pause publishing and new provider connections','boolean'),
    ('upgrades_enabled','Allow new paid upgrades','boolean'),
    ('free_monthly_posts','Free monthly publishing allowance','integer'),
    ('pro_monthly_posts','Pro monthly publishing allowance','integer'),
    ('social_allowed_hosts','Approved social API hostnames (comma separated)','hosts'),
    ('email_verification_required','Require verified email before publishing','boolean'),
    ('smtp_host','SMTP hostname','text'),('smtp_port','SMTP port','integer'),
    ('smtp_username','SMTP username','text'),('smtp_password','SMTP password','secret'),
    ('smtp_from','Sender email address','text'),
]
STRIPE_FIELDS = [('stripe_secret_key','Stripe secret or restricted API key','secret'),
                 ('stripe_pro_price_id','Pro recurring price ID','text'),
                 ('stripe_webhook_secret','Webhook signing secret','secret')]


class SettingInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    values: dict[str,str|int|bool] = Field(default_factory=dict,max_length=15)


class ReasonInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason: str = Field(min_length=3,max_length=300)

    @field_validator('reason')
    @classmethod
    def reason_text(cls,value):
        if len(value.strip())<3:
            raise ValueError('Enter a reason with at least three characters.')
        return value.strip()


class UserInput(ReasonInput):
    admin: StrictBool|None=None
    suspended: StrictBool|None=None


class WorkspaceInput(ReasonInput):
    name: str|None=Field(default=None,min_length=1,max_length=80)
    suspended: StrictBool|None=None
    quota_override: int|None=Field(default=None,ge=0,le=1000000,strict=True)


def metadata(settings, fields):
    current=settings.snapshot()
    return [{'name':key,'label':label,'type':kind,'saved':bool(getattr(current,key)),
             'value':'' if kind=='secret' else ', '.join(getattr(current,key)) if kind=='hosts' else getattr(current,key)}
            for key,label,kind in fields]


def merged_values(settings, payload, fields):
    allowed={key:kind for key,_,kind in fields}
    if set(payload.values)-set(allowed):
        raise HTTPException(422,'Unsupported settings field.')
    values={}
    for key,value in payload.values.items():
        kind=allowed[key]
        if kind=='boolean':
            if type(value) is not bool:
                raise HTTPException(422,'Boolean settings must be true or false.')
        elif kind=='integer':
            if type(value) is not int or not 1<=value<=(65535 if key=='smtp_port' else 1000000):
                raise HTTPException(422,'Enter a valid positive allowance or SMTP port.')
        else:
            if not isinstance(value,str) or len(value)>4096 or any(ord(char)<32 for char in value):
                raise HTTPException(422,'Settings must be at most 4096 characters and have no control characters.')
            if kind=='secret' and not value.strip():
                continue
            value=value if key=='smtp_password' else value.strip()
            if kind=='hosts':
                hosts=tuple(dict.fromkeys(part.strip().lower() for part in value.split(',') if part.strip()))
                if not hosts or len(hosts)>50 or any(not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',host) or host.endswith(('.local','.internal','.localhost')) for host in hosts):
                    raise HTTPException(422,'Enter up to 50 explicit public hostnames, without URLs or wildcards.')
                value=hosts
            if key=='smtp_host' and value and not re.fullmatch(r'[A-Za-z0-9.-]+',value):
                raise HTTPException(422,'Enter an SMTP hostname without a URL or path.')
            if key=='smtp_from' and value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
                raise HTTPException(422,'Enter a valid sender email address.')
            patterns={'stripe_secret_key':r'(?:sk|rk)_(?:test|live)_[A-Za-z0-9]+',
                      'stripe_webhook_secret':r'whsec_[A-Za-z0-9]+','stripe_pro_price_id':r'price_[A-Za-z0-9_]+'}
            if key in patterns and not re.fullmatch(patterns[key],value):
                raise HTTPException(422,'Enter a Stripe API key, recurring price ID or webhook secret in its expected format.')
        values[key]=value
    current=settings.snapshot()
    if values.get('pro_monthly_posts',current.pro_monthly_posts)<values.get('free_monthly_posts',current.free_monthly_posts):
        raise HTTPException(422,'Pro allowance must be at least the Free allowance.')
    return values


def save_global(saas,values,action):
    # Use a single transaction so the encrypted configuration and its audit cannot diverge.
    with saas.store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',[(key,saas.store.encode(json.dumps(value))) for key,value in values.items()])
        saas.audit(action,'platform','dispatch',{'fields':sorted(values)},db=db)


async def inspect_stripe(saas, settings, payload):
    updates=merged_values(settings,payload,STRIPE_FIELDS)
    current=settings.snapshot()
    key=updates.get('stripe_secret_key',current.stripe_secret_key)
    price_id=updates.get('stripe_pro_price_id',current.stripe_pro_price_id)
    if not key or not price_id:
        raise HTTPException(422,'Enter an API key and a recurring Pro price ID first.')
    account=await saas.stripe('GET','account',secret_key=key)
    price=await saas.stripe('GET','prices/'+price_id,secret_key=key)
    live=key.startswith(('sk_live_','rk_live_'))
    if not isinstance(price.get('recurring'),dict) or price['recurring'].get('interval') not in ('day','week','month','year'):
        raise HTTPException(422,'Choose a recurring subscription price.')
    if not isinstance(price.get('currency'),str) or not re.fullmatch('[a-z]{3}',price['currency']):
        raise HTTPException(502,'Stripe did not return a valid price currency.')
    if not isinstance(account.get('id'),str) or not account['id'].startswith('acct_'):
        raise HTTPException(502,'Stripe did not return a valid account profile.')
    if (price.get('id')!=price_id or price.get('active') is not True or price.get('type')!='recurring'
        or price.get('billing_scheme')!='per_unit' or price.get('recurring',{}).get('usage_type')!='licensed'
        or type(price.get('unit_amount')) is not int or price['unit_amount']<=0
        or price.get('livemode') is not live):
        raise HTTPException(422,'Choose an active, paid, fixed recurring price in the same test/live mode as the API key.')
    if live and not account.get('charges_enabled'):
        raise HTTPException(422,'Activate charges on this Stripe account before enabling live upgrades.')
    return updates,account,price,live


def router(settings, authorize):
    async def administrator(request: Request):
        await authorize(request)
        if not settings.saas_enabled or not (identity.get() or {}).get('admin'):
            raise HTTPException(403,'Administrator access is required.')

    routes=APIRouter(prefix='/admin',tags=['Administration'],dependencies=[Depends(administrator)])

    @routes.get('/overview')
    async def overview(request: Request):
        saas=request.app.state.saas
        with saas.store.connect() as db:
            users=db.execute('SELECT count(*),coalesce(sum(suspended),0) FROM users').fetchone()
            spaces=db.execute("SELECT count(*),coalesce(sum(suspended),0),coalesce(sum(plan='pro'),0) FROM workspaces").fetchone()
            posts=db.execute("SELECT status,count(*) FROM publications WHERE strftime('%Y-%m',created)=strftime('%Y-%m','now') GROUP BY status").fetchall()
            last_webhook=db.execute('SELECT max(created) FROM billing_events').fetchone()[0]
        return {'users':users[0],'suspended_users':users[1],'workspaces':spaces[0],'suspended_workspaces':spaces[1],
                'pro_workspaces':spaces[2],'month_publications':sum(row[1] for row in posts),'publication_statuses':dict(posts),
                'billing_configured':saas.billing_enabled(),'smtp_configured':saas.mail_configured(),
                'ffprobe_available':bool(shutil.which('ffprobe')),'uptime_seconds':int(time.time()-saas.started_at),
                'last_stripe_event':last_webhook,'registration_enabled':settings.registration_enabled,'maintenance_mode':settings.maintenance_mode}

    @routes.get('/users')
    async def users(request: Request,q: str=Query(default='',max_length=100),limit: int=Query(default=25,ge=1,le=100),offset: int=Query(default=0,ge=0,le=100000)):
        with request.app.state.store.connect() as db:
            search='%'+q+'%'
            total=db.execute('SELECT count(*) FROM users WHERE email LIKE ? OR name LIKE ?',(search,search)).fetchone()[0]
            rows=db.execute('''SELECT u.id,u.email,u.name,u.admin,u.suspended,u.verified,u.created,u.last_login,
                (SELECT count(*) FROM workspaces w WHERE w.owner=u.id),
                (SELECT count(*) FROM sessions s WHERE s.user=u.id AND s.expires>?)
                FROM users u WHERE u.email LIKE ? OR u.name LIKE ? ORDER BY u.created DESC,u.id LIMIT ? OFFSET ?''',(time.time(),search,search,limit,offset)).fetchall()
        return {'total':total,'items':[dict(zip(('id','email','name','admin','suspended','verified','created','last_login','workspaces','active_sessions'),row)) for row in rows]}

    @routes.patch('/users/{target}')
    async def update_user(target: str,payload: UserInput,request: Request):
        saas=request.app.state.saas;actor=identity.get()
        if payload.admin is None and payload.suspended is None:
            raise HTTPException(422,'Choose a role or account-status change.')
        with saas.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT admin,suspended FROM users WHERE id=?',(target,)).fetchone()
            if not row:
                raise HTTPException(404,'User not found.')
            new_admin=row[0] if payload.admin is None else int(payload.admin)
            new_suspended=row[1] if payload.suspended is None else int(payload.suspended)
            if target==actor['id'] and (not new_admin or new_suspended):
                raise HTTPException(409,'You cannot demote or suspend your signed-in administrator account.')
            if row[0] and not row[1] and (not new_admin or new_suspended) and db.execute('SELECT count(*) FROM users WHERE admin=1 AND suspended=0').fetchone()[0]<=1:
                raise HTTPException(409,'Keep at least one active administrator.')
            db.execute('UPDATE users SET admin=?,suspended=? WHERE id=?',(new_admin,new_suspended,target))
            if new_suspended:
                db.execute('DELETE FROM sessions WHERE user=?',(target,))
                db.execute('DELETE FROM email_tokens WHERE user=?',(target,))
                for (selected,) in db.execute('SELECT id FROM workspaces WHERE owner=?',(target,)).fetchall():
                    db.execute('DELETE FROM oauth_states WHERE platform LIKE ?',(selected+':%',))
            saas.audit('user.updated','user',target,{'admin':bool(new_admin),'suspended':bool(new_suspended),'reason':payload.reason},db=db)
        return {'message':'User updated.'}

    @routes.post('/users/{target}/revoke-sessions')
    async def revoke(target: str,payload: ReasonInput,request: Request):
        saas=request.app.state.saas
        with saas.store.connect() as db:
            if not db.execute('SELECT id FROM users WHERE id=?',(target,)).fetchone():
                raise HTTPException(404,'User not found.')
            db.execute('DELETE FROM sessions WHERE user=?',(target,))
            saas.audit('user.sessions_revoked','user',target,{'reason':payload.reason},db=db)
        return {'message':'All sessions revoked.'}

    @routes.get('/workspaces')
    async def workspaces(request: Request,q: str=Query(default='',max_length=100),limit: int=Query(default=25,ge=1,le=100),offset: int=Query(default=0,ge=0,le=100000)):
        with request.app.state.store.connect() as db:
            search='%'+q+'%'
            total=db.execute('SELECT count(*) FROM workspaces w JOIN users u ON u.id=w.owner WHERE w.name LIKE ? OR u.email LIKE ?',(search,search)).fetchone()[0]
            rows=db.execute('''SELECT w.id,w.name,u.email,w.plan,w.status,w.suspended,w.quota_override,w.customer,w.subscription,
                (SELECT count(*) FROM publications p WHERE p.workspace=w.id AND strftime('%Y-%m',p.created)=strftime('%Y-%m','now'))
                FROM workspaces w JOIN users u ON u.id=w.owner WHERE w.name LIKE ? OR u.email LIKE ? ORDER BY w.created DESC LIMIT ? OFFSET ?''',(search,search,limit,offset)).fetchall()
        return {'total':total,'items':[dict(zip(('id','name','owner_email','plan','subscription_status','suspended','quota_override','stripe_customer','stripe_subscription','usage'),row)) for row in rows]}

    @routes.patch('/workspaces/{target}')
    async def update_workspace(target: str,payload: WorkspaceInput,request: Request):
        values={key:value for key,value in payload.model_dump().items() if key in payload.model_fields_set and key!='reason'}
        if not values or (payload.name is not None and not payload.name.strip()):
            raise HTTPException(422,'Choose a workspace name, status or quota change.')
        if 'name' in values:
            if not isinstance(values['name'],str) or not values['name'].strip():
                raise HTTPException(422,'Enter a non-empty workspace name.')
            values['name']=values['name'].strip()
        if values.get('suspended') is None and 'suspended' in values:
            raise HTTPException(422,'Workspace status cannot be null.')
        saas=request.app.state.saas
        with saas.store.connect() as db:
            if not db.execute('SELECT id FROM workspaces WHERE id=?',(target,)).fetchone():
                raise HTTPException(404,'Workspace not found.')
            # Keys come exclusively from the validated model, never a raw SQL identifier.
            db.execute('UPDATE workspaces SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),target))
            if values.get('suspended'):
                db.execute('DELETE FROM oauth_states WHERE platform LIKE ?',(target+':%',))
            saas.audit('workspace.updated','workspace',target,{**values,'reason':payload.reason},db=db)
        return {'message':'Workspace updated.'}

    @routes.get('/publications')
    async def publications(request: Request,status: str=Query(default='',max_length=30),limit: int=Query(default=25,ge=1,le=100),offset: int=Query(default=0,ge=0,le=100000)):
        with request.app.state.store.connect() as db:
            total=db.execute('SELECT count(*) FROM publications WHERE ?=\'\' OR status=?',(status,status)).fetchone()[0]
            rows=db.execute('''SELECT p.id,w.name,u.email,p.kind,p.status,p.created FROM publications p
                JOIN workspaces w ON w.id=p.workspace JOIN users u ON u.id=w.owner WHERE ?='' OR p.status=?
                ORDER BY p.created DESC,p.rowid DESC LIMIT ? OFFSET ?''',(status,status,limit,offset)).fetchall()
        return {'total':total,'items':[dict(zip(('id','workspace','owner_email','kind','status','created'),row)) for row in rows]}

    @routes.get('/audit')
    async def audit(request: Request,limit: int=Query(default=25,ge=1,le=100),offset: int=Query(default=0,ge=0,le=100000)):
        with request.app.state.store.connect() as db:
            total=db.execute('SELECT count(*) FROM audit_log').fetchone()[0]
            rows=db.execute('''SELECT a.id,coalesce(u.email,a.actor),a.action,a.target_kind,a.target_id,a.details,a.created
                FROM audit_log a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT ? OFFSET ?''',(limit,offset)).fetchall()
        items=[dict(zip(('id','actor','action','target_kind','target_id','details','created'),row)) for row in rows]
        for item in items:
            item['details']=json.loads(item['details'])
        return {'total':total,'items':items}

    @routes.get('/settings')
    async def get_settings():
        return {'fields':metadata(settings,GENERAL_FIELDS)}

    @routes.put('/settings')
    async def put_settings(payload: SettingInput,request: Request):
        values=merged_values(settings,payload,GENERAL_FIELDS)
        save_global(request.app.state.saas,values,'platform.settings_updated')
        return {'message':'Platform settings saved and active.','fields':metadata(settings,GENERAL_FIELDS)}

    @routes.post('/email/test')
    async def test_email(request: Request):
        saas=request.app.state.saas
        await saas.send_email(identity.get()['email'],'Dispatch SMTP test','Your Dispatch email settings accepted this test message for delivery.')
        saas.audit('platform.email_test','platform','smtp')
        return {'message':'Test email accepted for delivery to your administrator email. Check your inbox.'}

    @routes.get('/stripe')
    async def get_stripe(request: Request):
        current=settings.snapshot()
        with request.app.state.store.connect() as db:
            last_event=db.execute('SELECT max(created) FROM billing_events').fetchone()[0]
        return {'fields':metadata(settings,STRIPE_FIELDS),'account_id':current.stripe_account_id,
                'mode':'live' if current.stripe_secret_key.startswith(('sk_live_','rk_live_')) else 'test' if current.stripe_secret_key else 'unconfigured',
                'tested_at':current.stripe_tested_at,'enabled':request.app.state.saas.billing_enabled(),
                'webhook_url':current.app_base_url+'/billing/webhook','last_webhook':last_event,
                'accepted_price_ids':list(current.stripe_accepted_price_ids),
                'events':['customer.subscription.created','customer.subscription.updated','customer.subscription.deleted']}

    @routes.post('/stripe/test')
    async def test_stripe(payload: SettingInput,request: Request):
        _,account,price,live=await inspect_stripe(request.app.state.saas,settings,payload)
        return {'success':True,'account_id':account['id'],'mode':'live' if live else 'test',
                'price_id':price['id'],'currency':price.get('currency'),'interval':price['recurring']['interval'],'interval_count':price['recurring'].get('interval_count',1),
                'charges_enabled':bool(account.get('charges_enabled')),
                'message':'API key and recurring price verified. No charge was created. Webhook signing must be confirmed by a delivered Stripe event.'}

    @routes.put('/stripe')
    @serialized_billing
    async def save_stripe(payload: SettingInput,request: Request):
        saas=request.app.state.saas
        updates,account,price,live=await inspect_stripe(saas,settings,payload)
        current=settings.snapshot()
        if not updates.get('stripe_webhook_secret',current.stripe_webhook_secret):
            raise HTTPException(422,'Enter the webhook signing secret before enabling upgrades.')
        with saas.store.connect() as db:
            linked=db.execute('SELECT count(*) FROM workspaces WHERE customer IS NOT NULL OR subscription IS NOT NULL').fetchone()[0]
        old_account=current.stripe_account_id
        changed_key='stripe_secret_key' in updates and updates['stripe_secret_key']!=current.stripe_secret_key
        if linked and changed_key and not old_account:
            old_account=(await saas.stripe('GET','account',secret_key=current.stripe_secret_key))['id']
        old_live=current.stripe_secret_key.startswith(('sk_live_','rk_live_'))
        if linked and ((old_account and old_account!=account['id']) or old_live!=live):
            raise HTTPException(409,'Stripe account or mode cannot change while customer billing is linked. Complete a billing migration first.')
        accepted=set(current.stripe_accepted_price_ids)
        if current.stripe_pro_price_id:
            accepted.add(current.stripe_pro_price_id)
        accepted.add(price['id'])
        updates.update(stripe_account_id=account['id'],stripe_tested_at=time.time(),stripe_accepted_price_ids=sorted(accepted))
        save_global(saas,updates,'platform.stripe_updated')
        return {'message':'Stripe settings verified and saved. Users can upgrade after your customer portal and signed webhook delivery are configured.'}

    return routes
