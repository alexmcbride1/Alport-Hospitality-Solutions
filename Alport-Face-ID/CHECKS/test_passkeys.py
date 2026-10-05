"""Real WebAuthn signature tests with a SQLite SQL adapter, not a live PostgreSQL test."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import unittest
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, session, render_template
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1] / "UPLOAD"
sys.path.insert(0, str(ROOT))
from alport_passkeys import register_passkeys
from alport_staff import register_staff


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def unb64(value):
    return base64.urlsafe_b64decode(value + '===')


class Cursor:
    def __init__(self, cursor): self.cursor = cursor
    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


class DB:
    def __init__(self, db): self.db = db
    def execute(self, sql, args=()):
        if 'pg_advisory_xact_lock' in sql or 'DROP CONSTRAINT IF EXISTS' in sql:
            sql, args = 'SELECT 1', ()
        sql = sql.replace('ADD COLUMN IF NOT EXISTS', 'ADD COLUMN')
        for old, new in [('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY'), (' FOR UPDATE', ''), ('%s', '?')]:
            sql = sql.replace(old, new)
        return Cursor(self.db.execute(sql, args))
    def __enter__(self): return self
    def __exit__(self, exc, *_): self.db.rollback() if exc else self.db.commit()


class Device:
    """A synthetic discoverable credential with an actual ES256 signing key."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cid = secrets.token_bytes(32)
        self.handle = None
    def registration(self, options, origin, uv=True, rp='alporthospitality.co.uk', **client_fields):
        self.handle = options['user']['id']
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        auth = hashlib.sha256(rp.encode()).digest() + bytes([0x41 | (4 if uv else 0)]) + bytes(4) + bytes(16) + len(self.cid).to_bytes(2, 'big') + self.cid + cose
        client = dict(type='webauthn.create', challenge=options['challenge'], origin=origin, crossOrigin=False)
        client.update(client_fields)
        return dict(id=b64(self.cid), rawId=b64(self.cid), type='public-key', response=dict(
            clientDataJSON=b64(json.dumps(client).encode()),
            attestationObject=b64(cbor2.dumps({'fmt': 'none', 'authData': auth, 'attStmt': {}})), transports=['internal']))
    def assertion(self, options, origin, counter=1, uv=True, rp='alporthospitality.co.uk', **client_fields):
        auth = hashlib.sha256(rp.encode()).digest() + bytes([1 | (4 if uv else 0)]) + counter.to_bytes(4, 'big')
        client = dict(type='webauthn.get', challenge=options['challenge'], origin=origin, crossOrigin=False)
        client.update(client_fields)
        client = json.dumps(client).encode()
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return dict(id=b64(self.cid), rawId=b64(self.cid), type='public-key', response=dict(
            clientDataJSON=b64(client), authenticatorData=b64(auth), signature=b64(signature), userHandle=self.handle))


class PasskeyTests(unittest.TestCase):
    origin = 'https://alporthospitality.co.uk'
    def setUp(self):
        self.envpatch = patch.dict(os.environ, {'COMPANY_ADMIN_EMAIL': 'admin@example.test', 'COMPANY_ADMIN_PASSWORD': 'Company-password-test-only', 'WEBAUTHN_RP_ID': 'alporthospitality.co.uk', 'WEBAUTHN_ORIGINS': self.origin + ',https://www.alporthospitality.co.uk'})
        self.envpatch.start()
        self.addCleanup(self.envpatch.stop)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE users(id INTEGER PRIMARY KEY,organisation_id INT,name TEXT,email TEXT,password_hash TEXT,role TEXT,active INT);
            CREATE TABLE employees(id INTEGER PRIMARY KEY,organisation_id INT,site_id INT,active INT);
            CREATE TABLE sites(id INTEGER PRIMARY KEY,organisation_id INT,active INT);
            CREATE TABLE alport_purchase_orders(id INTEGER PRIMARY KEY);
            INSERT INTO sites VALUES(1,1,1),(2,2,1),(3,1,1);
            INSERT INTO employees VALUES(1,1,1,1),(2,1,3,1);
        ''')
        self.password = 'Example-password-test-only'
        self.hash = generate_password_hash(self.password, method='pbkdf2:sha256:1000')
        self.db.executemany('INSERT INTO users VALUES(?,?,?,?,?,?,1)', [
            (1,1,'Owner','owner@example.test',self.hash,'Owner'),
            (2,1,'Staff','staff@example.test',self.hash,'Staff'),
            (3,2,'Other owner','owner@example.test',self.hash,'Owner')])
        self.db.commit()
        self.app = Flask(__name__, template_folder=str(ROOT/'templates'), static_folder=str(ROOT/'static'))
        self.app.config.update(TESTING=True, SECRET_KEY='test-secret-not-production', SERVER_NAME='alporthospitality.co.uk', PREFERRED_URL_SCHEME='https')
        def q(sql, args=(), one=False):
            result = DB(self.db).execute(sql, args)
            return result.fetchone() if one else result.fetchall()
        self.q = q
        self.sub = {}
        self.env = dict(conn=lambda: DB(self.db), q=q, user=lambda: q('SELECT * FROM users WHERE id=? AND active=1', (session.get('user_id'),), True),
                        subscription_for=lambda org:self.sub, subscription_blocks_access=lambda sub:sub.get('status')=='Suspended')
        @self.app.get('/app')
        def app_home(): return 'workspace'
        @self.app.get('/logout')
        def logout(): session.clear(); return 'signed out'
        @self.app.get('/login')
        def login(): return render_template('login.html')
        @self.app.get('/company-admin/login')
        def company_admin_login(): return render_template('company_admin_login.html')
        @self.app.get('/api/payroll')
        def payroll(): return 'restricted'
        register_staff(self.app, self.env)
        self.db.execute("INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,activated_at,created_at,updated_at) VALUES(1,1,1,2,'orders','today','today','today')")
        self.db.commit()
        register_passkeys(self.app, self.env)
        self.client = self.app.test_client()
        self.sign(1)
    def sign(self, uid=None, company=False):
        with self.client.session_transaction() as s:
            s.clear()
            if company: s.update(company_admin=True, company_admin_email='admin@example.test')
            elif uid: s.update(user_id=uid, site_id=1, staff_auth_hash=hashlib.sha256(self.hash.encode()).hexdigest())
        self.config()
    def config(self):
        self.csrf = self.client.get('/api/passkeys/config', base_url=self.origin).json['csrf']
    def post(self, path, data, **kw):
        headers = kw.pop('headers', {'Origin':self.origin,'X-Passkey-CSRF':self.csrf})
        return self.client.post('/api/passkeys/'+path, json=data, base_url=self.origin, headers=headers, **kw)
    def options(self, ceremony, lane='business', **kw):
        data = dict(password='Company-password-test-only' if lane=='company-admin' else self.password, label='Test device') if ceremony=='register' else {}
        data.update(kw)
        r = self.post(lane+'/'+ceremony+'/options', data)
        self.assertEqual(r.status_code, 200, r.json)
        return r.json
    def finish(self, ceremony, flow, credential, lane='business'):
        return self.post(lane+'/'+ceremony+'/verify', dict(flow=flow['flow'], credential=credential))
    def enrol(self, lane='business'):
        d = Device(); flow = self.options('register', lane)
        r = self.finish('register', flow, d.registration(flow['options'], self.origin), lane)
        self.assertEqual(r.status_code, 200, r.json)
        return d
    def authenticate(self, device, lane='business', **kw):
        flow = self.options('authenticate', lane)
        return self.finish('authenticate', flow, device.assertion(flow['options'], self.origin, **kw), lane)
    def test_business_real_signature_and_session_rotation(self):
        d = self.enrol(); self.sign()
        old = self.csrf
        r = self.authenticate(d)
        self.assertEqual(r.status_code, 200, r.json); self.assertEqual(r.json['redirect'],'/app')
        with self.client.session_transaction() as s:
            self.assertEqual(s['user_id'],1); self.assertNotIn('company_admin',s); self.assertNotIn('passkey_nonce',s)
        self.config(); self.assertNotEqual(old,self.csrf)
    def test_staff_own_permissions_survive_passkey_login(self):
        self.sign(2); d=self.enrol(); self.sign()
        r=self.authenticate(d,'staff'); self.assertEqual(r.status_code,200,r.json); self.assertEqual(r.json['redirect'],'/staff')
        with self.client.session_transaction() as s:
            self.assertEqual(s['user_id'],2); self.assertEqual(s['staff_site_id'],1); self.assertIn('staff_auth_hash',s)
        self.assertEqual(self.client.get('/passkeys',base_url=self.origin).status_code,200)
        self.assertEqual(self.client.get('/api/payroll',base_url=self.origin).status_code,403)
        self.assertEqual(self.client.get('/app',base_url=self.origin).location,'/staff')
    def test_company_admin_separate_principal(self):
        self.sign(company=True); d=self.enrol('company-admin'); self.sign()
        r=self.authenticate(d,'company-admin'); self.assertEqual(r.status_code,200,r.json)
        with self.client.session_transaction() as s:
            self.assertTrue(s['company_admin']); self.assertEqual(s['company_admin_email'],'admin@example.test'); self.assertNotIn('user_id',s)
    def test_ordinary_key_never_grants_company_admin(self):
        d=self.enrol(); self.sign(); self.assertEqual(self.authenticate(d,'company-admin').status_code,403)
    def test_company_key_never_grants_business_or_staff(self):
        self.sign(company=True); d=self.enrol('company-admin'); self.sign()
        for lane in ['business','staff']:
            with self.subTest(lane=lane): self.assertEqual(self.authenticate(d,lane).status_code,403)
    def test_owner_cannot_use_staff_lane(self):
        d=self.enrol(); self.sign(); self.assertEqual(self.authenticate(d,'staff').status_code,403)
    def test_shared_email_does_not_mix_businesses(self):
        d=self.enrol(); self.sign(3); other=self.enrol(); self.sign()
        self.assertEqual(self.authenticate(other).status_code,200)
        with self.client.session_transaction() as s:self.assertEqual(s['user_id'],3);self.assertEqual(s['site_id'],2)
        self.sign(); self.assertEqual(self.authenticate(d).status_code,200)
        with self.client.session_transaction() as s:self.assertEqual(s['user_id'],1)
    def test_enrol_requires_current_password_and_login(self):
        self.assertEqual(self.post('business/register/options',dict(password='wrong',label='key')).status_code,403)
        self.sign(); self.assertEqual(self.post('business/register/options',dict(password=self.password,label='key')).status_code,401)
    def test_password_rate_limit(self):
        for _ in range(5):self.assertEqual(self.post('business/register/options',dict(password='wrong',label='key')).status_code,403)
        self.assertEqual(self.post('business/register/options',dict(password=self.password,label='key')).status_code,429)
    def test_registration_crypto_and_origin_requirements(self):
        for changes in [dict(uv=False),dict(rp='evil.test'),dict(origin='https://evil.test'),dict(challenge='wrong'),dict(crossOrigin=True),dict(topOrigin=self.origin),dict(type='webauthn.get')]:
            with self.subTest(changes=changes):
                self.db.execute('DELETE FROM alport_passkey_limits');self.db.commit()
                d=Device();f=self.options('register');args=dict(origin=self.origin);args.update(changes)
                self.assertEqual(self.finish('register',f,d.registration(f['options'],**args)).status_code,400)
        self.assertEqual(self.q('SELECT COUNT(*) AS n FROM alport_passkeys',one=True)['n'],0)
    def test_authentication_crypto_and_origin_requirements(self):
        d=self.enrol();self.sign()
        for changes in [dict(uv=False),dict(rp='evil.test'),dict(origin='https://evil.test'),dict(challenge='wrong'),dict(crossOrigin=True),dict(topOrigin=self.origin),dict(type='webauthn.create')]:
            with self.subTest(changes=changes):
                f=self.options('authenticate');args=dict(origin=self.origin);args.update(changes)
                self.assertIn(self.finish('authenticate',f,d.assertion(f['options'],**args)).status_code,(400,403))
    def test_invalid_signature_and_handle(self):
        d=self.enrol();self.sign()
        for field,value in [('signature',b64(b'not-a-signature')),('userHandle',b64(b'wrong-account')),('userHandle',None)]:
            with self.subTest(field=field,value=value):
                f=self.options('authenticate');cred=d.assertion(f['options'],self.origin);cred['response'][field]=value
                self.assertEqual(self.finish('authenticate',f,cred).status_code,403)
    def test_raw_id_mismatch(self):
        d=self.enrol();self.sign();f=self.options('authenticate');cred=d.assertion(f['options'],self.origin);cred['rawId']=b64(b'wrong')
        self.assertEqual(self.finish('authenticate',f,cred).status_code,403)
    def test_unknown_key(self):
        d=Device();d.handle=b64(secrets.token_bytes(32));self.sign();self.assertEqual(self.authenticate(d).status_code,403)
    def test_challenge_consumed_on_invalid_assertion(self):
        d=self.enrol();self.sign();f=self.options('authenticate');valid=d.assertion(f['options'],self.origin)
        bad=json.loads(json.dumps(valid));bad['response']['signature']=b64(b'bad')
        self.assertEqual(self.finish('authenticate',f,bad).status_code,403)
        self.assertEqual(self.finish('authenticate',f,valid).status_code,400)
    def test_registration_single_use(self):
        d=Device();f=self.options('register');cred=d.registration(f['options'],self.origin)
        self.assertEqual(self.finish('register',f,cred).status_code,200)
        self.assertEqual(self.finish('register',f,cred).status_code,400)
    def test_expired_and_session_bound_challenges(self):
        d=self.enrol();self.sign();f=self.options('authenticate');cred=d.assertion(f['options'],self.origin)
        self.sign();self.assertEqual(self.finish('authenticate',f,cred).status_code,400)
        f=self.options('authenticate');cred=d.assertion(f['options'],self.origin)
        self.db.execute('UPDATE alport_passkey_challenges SET expires_at=0');self.db.commit()
        self.assertEqual(self.finish('authenticate',f,cred).status_code,400)
    def test_counter_monotonic_and_synced_zero_counter(self):
        d=self.enrol();self.sign();self.assertEqual(self.authenticate(d,counter=2).status_code,200)
        self.sign();self.assertEqual(self.authenticate(d,counter=2).status_code,403)
        self.assertEqual(self.authenticate(d,counter=1).status_code,403)
        self.assertEqual(self.authenticate(d,counter=3).status_code,200)
        self.sign(3);synced=self.enrol();self.sign();self.assertEqual(self.authenticate(synced,counter=0).status_code,200)
        self.sign();self.assertEqual(self.authenticate(synced,counter=0).status_code,200)
    def test_password_change_invalidates_keys_and_pending_enrolment(self):
        d=self.enrol();f=self.options('register');new=Device();cred=new.registration(f['options'],self.origin)
        self.db.execute('UPDATE users SET password_hash=? WHERE id=1',(generate_password_hash('new-password'),));self.db.commit()
        self.assertEqual(self.finish('register',f,cred).status_code,403)
        self.sign();self.assertEqual(self.authenticate(d).status_code,403)
    def test_company_password_or_email_change_invalidates_keys(self):
        self.sign(company=True);d=self.enrol('company-admin');self.sign()
        for changes in [{'COMPANY_ADMIN_PASSWORD':'changed-password'},{'COMPANY_ADMIN_EMAIL':'other@example.test'}]:
            with self.subTest(changes=changes),patch.dict(os.environ,changes):self.assertEqual(self.authenticate(d,'company-admin').status_code,403)
    def test_revocation_and_other_account_scope(self):
        d=self.enrol();key=self.q('SELECT id FROM alport_passkeys',one=True)['id'];self.sign(3)
        self.assertEqual(self.post('business/revoke/'+str(key),dict(password=self.password)).status_code,404)
        self.assertEqual(self.client.get('/api/passkeys/business/list',base_url=self.origin).json['keys'],[])
        self.sign(1);self.assertEqual(self.post('business/revoke/'+str(key),dict(password='wrong')).status_code,403)
        self.assertEqual(self.post('business/revoke/'+str(key),dict(password=self.password)).status_code,200)
        self.sign();self.assertEqual(self.authenticate(d).status_code,403)
    def test_disabled_user_and_staff_links(self):
        self.sign(2);d=self.enrol();self.sign()
        for sql,restore in [("UPDATE users SET active=0 WHERE id=2","UPDATE users SET active=1 WHERE id=2"),("UPDATE alport_staff_access SET profile='disabled'","UPDATE alport_staff_access SET profile='orders'"),("UPDATE employees SET active=0 WHERE id=1","UPDATE employees SET active=1 WHERE id=1"),("UPDATE sites SET active=0 WHERE id=1","UPDATE sites SET active=1 WHERE id=1"),("UPDATE alport_staff_access SET activated_at=NULL","UPDATE alport_staff_access SET activated_at='today'")]:
            with self.subTest(sql=sql):
                self.db.execute(sql);self.db.commit();self.assertEqual(self.authenticate(d,'staff').status_code,403);self.db.execute(restore);self.db.commit()
    def test_current_subscription_and_required_payment(self):
        d=self.enrol();self.sign();self.sub['status']='Suspended';self.assertEqual(self.authenticate(d).status_code,403)
        self.sub['status']='Payment required';r=self.authenticate(d);self.assertEqual(r.status_code,200);self.assertEqual(r.json['redirect'],'/subscribe')
    def test_management_cannot_cross_account_types(self):
        self.assertEqual(self.post('company-admin/register/options',dict(password='Company-password-test-only',label='bad')).status_code,401)
        self.sign(company=True);self.assertEqual(self.post('business/register/options',dict(password=self.password,label='bad')).status_code,401)
    def test_stale_staff_session_can_sign_in_but_cannot_enrol(self):
        self.sign(2);d=self.enrol()
        with self.client.session_transaction() as s:s['staff_auth_hash']='old'
        self.assertEqual(self.post('business/register/options',dict(password=self.password,label='bad')).status_code,403)
        self.assertEqual(self.authenticate(d,'staff').status_code,200)
    def test_csrf_origin_and_host_guard(self):
        for headers in [{},{'Origin':self.origin},{'X-Passkey-CSRF':self.csrf,'Origin':'https://evil.test'},{'X-Passkey-CSRF':'é','Origin':self.origin}]:
            with self.subTest(headers=headers):self.assertEqual(self.post('business/authenticate/options',{},headers=headers).status_code,403)
        self.assertEqual(self.post('business/authenticate/options',{},headers={'X-Passkey-CSRF':self.csrf,'Origin':'https://www.alporthospitality.co.uk'}).status_code,403)
        self.assertEqual(self.client.get('/api/passkeys/config',base_url='https://other.test').json['enabled'],False)
    def test_render_internal_http_scheme(self):
        r=self.client.post('/api/passkeys/business/authenticate/options',json={},base_url='http://alporthospitality.co.uk',headers={'Origin':self.origin,'X-Passkey-CSRF':self.csrf})
        self.assertEqual(r.status_code,200,r.json)
    def test_www_origin_can_use_same_rp(self):
        d=self.enrol();self.origin='https://www.alporthospitality.co.uk';self.sign();self.assertEqual(self.authenticate(d).status_code,200)
    def test_pages_and_cache_headers(self):
        for path in ['/login','/company-admin/login','/staff/login','/passkeys']:
            with self.subTest(path=path):
                r=self.client.get(path,base_url=self.origin);self.assertEqual(r.status_code,200);self.assertIn(b'passkey',r.data)
        r=self.client.get('/passkeys',base_url=self.origin);self.assertEqual(r.headers['Cache-Control'],'no-store');self.assertEqual(r.headers['X-Frame-Options'],'DENY')
        self.sign(company=True);self.assertEqual(self.client.get('/company-admin/passkeys',base_url=self.origin).status_code,200)
    def test_malformed_requests_fail_closed(self):
        for cred in [None,[],{},dict(response=None),dict(response={'clientDataJSON':'junk'}),dict(response={'clientDataJSON':b64(b'[]')})]:
            with self.subTest(credential=cred):
                f=self.options('authenticate');self.assertEqual(self.finish('authenticate',f,cred).status_code,400)
        self.assertEqual(self.post('business/authenticate/options',{'large':'x'*66000}).status_code,413)
    def test_audit_and_public_key_storage_only(self):
        d=self.enrol();row=self.q('SELECT * FROM alport_passkeys',one=True)
        self.assertNotIn(self.password,str(row));self.assertNotIn('private_key',row)
        self.assertEqual(self.q('SELECT event FROM alport_passkey_events',one=True)['event'],'Registered')
        keys=self.client.get('/api/passkeys/business/list',base_url=self.origin).json['keys']
        self.assertNotIn('public_key',keys[0]);self.assertNotIn('password_stamp',keys[0])

if __name__=='__main__': unittest.main()
