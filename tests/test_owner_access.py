"""Exercise the actual login handlers without a production database."""
import ast
import hashlib
import hmac
import unittest
from datetime import date,datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from flask import Flask,session,request,redirect,url_for,jsonify
from werkzeug.security import generate_password_hash,check_password_hash
from alport_access import preview_session_values
ROOT=Path(__file__).resolve().parents[1]

def function(path,name,env):
    node=next(n for n in ast.walk(ast.parse((ROOT/path).read_text())) if isinstance(n,ast.FunctionDef) and n.name==name)
    node.decorator_list=[]
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),env)
    return env[name]

class AccessTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.app.secret_key='fictional-test-secret';self.app.testing=True
        self.u=dict(id=1,organisation_id=10,role='Owner',active=1,password_hash=generate_password_hash('fictional-password',method='pbkdf2:sha256:1000'))
        self.sub={'status':'Payment required'}
        def q(sql,args=(),one=False):
            if 'FROM users' in sql:return self.u if args[0]=='owner@example.test' else None
            return {'id':20}
        env=dict(app=self.app,session=session,request=request,redirect=redirect,url_for=url_for,jsonify=jsonify,q=q,user=lambda:self.u,org=lambda:{},current_site=lambda:{},subscription_for=lambda oid:self.sub,check_password_hash=check_password_hash,preview_session_values=preview_session_values,render_template=lambda name,**kw:name, date=date,datetime=datetime,timedelta=timedelta,PAYMENT_GRACE_DAYS=7,_parse_iso_date=lambda s:date.fromisoformat(s) if s else None)
        function('app.py','subscription_blocks_access',env)
        self.env=env
        for name,path,methods in [('login','/login',['GET','POST']),('logout','/logout',['GET','POST']),('app_home','/app',['GET'])]:self.app.add_url_rule(path,name,function('app.py',name,env),methods=methods)
        self.app.add_url_rule('/subscribe','subscribe',lambda:'subscription')
        self.client=self.app.test_client()
    def verified(self,**changes):
        with self.client.session_transaction() as s:s.update(dict(user_id=1,billing_preview_admin=True,billing_preview_bypass_org=10,**changes))
    def login(self):return self.client.post('/login',data={'email':'owner@example.test','password':'fictional-password'})
    def test_legacy_verified_same_user_session_survives_password_login(self):
        self.verified();self.assertEqual(self.login().location,'/app')
        with self.client.session_transaction() as s:self.assertEqual(s['billing_preview_user_id'],1)
        self.assertEqual(self.client.get('/app').status_code,200)
    def test_fresh_customer_owner_still_requires_payment(self):
        self.assertEqual(self.login().location,'/subscribe');self.assertEqual(self.client.get('/app').location,'/subscribe')
    def test_different_user_org_or_explicit_binding_cannot_inherit_preview(self):
        for key,value in [('user_id',2),('billing_preview_bypass_org',99),('billing_preview_user_id',2),('billing_preview_admin','true')]:
            self.verified()
            with self.client.session_transaction() as s:s[key]=value
            self.assertEqual(self.login().location,'/subscribe',key)
            with self.client.session_transaction() as s:self.assertNotIn('billing_preview_admin',s)
    def test_logout_revokes_preview(self):
        self.verified();self.client.post('/logout');self.assertEqual(self.login().location,'/subscribe')
    def test_suspended_cancelled_and_overdue_still_block(self):
        for status in ['Suspended','Cancelled','Past due']:
            self.verified();self.sub=dict(status=status,past_due_since='2000-01-01');self.assertEqual(self.login().status_code,403)
            self.assertEqual(self.client.get('/app').location,'/login')
    def test_active_customer_does_not_need_preview(self):
        self.sub={'status':'Active'};self.assertEqual(self.login().location,'/app')
    def test_invalid_binding_fails_closed(self):
        for value in [None,'not-an-id',{},[]]:
            self.assertEqual(preview_session_values(dict(user_id=1,billing_preview_admin=True,billing_preview_bypass_org=value),self.u),{})
    def subscribe_handler(self):
        env=dict(self.env,pricing_for=lambda oid:{},os=SimpleNamespace(environ={'COMPANY_ADMIN_EMAIL':'admin@example.test','COMPANY_ADMIN_PASSWORD':'fictional-admin-secret'}),hmac=hmac,CONTRACT_MONTHS=12)
        self.app.view_functions['subscribe']=function('app.py','subscribe',env)
        self.app.add_url_rule('/preview','preview',env['subscribe'],methods=['GET','POST'])
    def test_successful_admin_preview_is_bound_and_existing_preview_skips_billing(self):
        self.subscribe_handler()
        with self.client.session_transaction() as s:s['user_id']=1
        r=self.client.post('/preview',data={'action':'admin_preview','admin_email':'admin@example.test','admin_password':'fictional-admin-secret'})
        self.assertEqual(r.location,'/app')
        with self.client.session_transaction() as s:self.assertEqual(s['billing_preview_user_id'],1)
        self.assertEqual(self.client.get('/subscribe').location,'/app')
    def test_rejected_company_credentials_never_grant_preview(self):
        self.subscribe_handler()
        with self.client.session_transaction() as s:s['user_id']=1
        self.assertEqual(self.client.post('/preview',data={'action':'admin_preview','admin_email':'admin@example.test','admin_password':'wrong'}).status_code,200)
        with self.client.session_transaction() as s:self.assertNotIn('billing_preview_admin',s)
    def test_actual_business_passkey_handler_preserves_only_same_user_preview(self):
        p=dict(company=False,user=self.u,staff=None,subject='user:1',name='Owner')
        class Conn:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql,args=()):self.sql=sql;return self
            def fetchone(self):
                if 'FROM alport_passkeys ' in self.sql:return dict(id=1,revoked_at=None,subject='user:1',password_stamp='stamp',public_key=b'key',sign_count=1)
                if 'FROM alport_passkey_subjects' in self.sql:return dict(user_handle='handle')
                return {'id':20}
        class PasskeyError(Exception):pass
        env=dict(self.env,env=self.env,conn=Conn,lane_ok=lambda lane:None,consume=lambda *a:{'challenge':'challenge','origin':'https://example.test'},credential=lambda d:{'id':'test-credential','response':{'userHandle':'handle'}},principal=lambda *a:p,stamp=lambda p:'stamp',hmac=hmac,hashlib=hashlib,base64url_to_bytes=lambda v:v.encode(),rp_id='example.test',verify_authentication_response=lambda **k:SimpleNamespace(new_sign_count=2),PasskeyError=PasskeyError,WebAuthnException=ValueError,now=lambda:1,event=lambda *a:None)
        self.app.add_url_rule('/passkey','passkey',function('alport_passkeys.py','auth_verify',env),defaults={'lane':'business'},methods=['POST'])
        self.verified();self.assertEqual(self.client.post('/passkey',json={}).json['redirect'],'/app')
        with self.client.session_transaction() as s:self.assertEqual(s['billing_preview_user_id'],1);s['user_id']=2
        self.assertEqual(self.client.post('/passkey',json={}).json['redirect'],'/subscribe')
