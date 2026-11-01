import ast,re,unittest,os
from datetime import datetime,timedelta
from unittest.mock import patch
from flask import session,jsonify
from test_booking_diary import DiaryTests,DiaryDB
from test_team_chat import ROOT
from alport_public_bookings import register_public_bookings
from alport_booking_fields import validate_guest_fields
import social_booking

class PublicDB(DiaryDB):
    def execute(self,sql,args=()):
        if any(x in sql for x in ('CREATE OR REPLACE FUNCTION','CREATE TRIGGER','ENABLE TRIGGER','FROM pg_trigger')):return super().execute('SELECT 1 AS ready')
        return super().execute(sql,args)

class PublicTests(DiaryTests):
    def setUp(self):
        super().setUp();self.app._got_first_request=False
        for field,kind in [('deposit_type',"TEXT DEFAULT 'none'"),('deposit_value','REAL DEFAULT 0'),('deposit_min_party','INTEGER DEFAULT 1'),('cancellation_policy',"TEXT DEFAULT ''"),('stripe_connect_account_id',"TEXT DEFAULT ''"),('large_party_threshold','INTEGER DEFAULT 20')]:self.db.execute('ALTER TABLE booking_settings ADD COLUMN '+field+' '+kind)
        self.day=(social_booking.localnow().date()+timedelta(days=3)).isoformat()
        for dow in range(7):self.db.execute("INSERT INTO booking_sessions(organisation_id,site_id,day_of_week,name,start_time,end_time) VALUES(1,1,?,'Dinner','17:00','23:00')",(dow,))
        self.db.commit()
        self.env.update(conn=lambda:PublicDB(self.db),now=lambda:datetime.now().isoformat(),_public_base_url=lambda:'http://localhost',booking_settings_for=lambda oid,sid:self.q('SELECT * FROM booking_settings WHERE organisation_id=? AND site_id=?',(oid,sid),True))
        register_public_bookings(self.app,self.env)
        self.link=self.client.get('/api/bookings/website-link').json;self.url=self.link['url'].replace('http://localhost','')
        self.assertEqual(self.client.post('/api/bookings/website-link',json={'enabled':True,'contact_phone':'01332 123456'},headers=self.headers()).status_code,200)
        self.public=self.app.test_client()
    def form(self,client=None):
        c=client or self.public;r=c.get(self.url);return re.search(r'name="form_token" value="([^"]+)"',r.text)[1]
    def data(self,token=None,**kwargs):
        return dict(form_token=token or self.form(),date=self.day,time='19:00',party='2',name='Fictional Guest',email='guest@example.test',phone='07700 900123',allergies='None',occasion='No special occasion',requests='',**kwargs)
    def post(self,changes=None,token=None):
        d=self.data(token);d.update(changes or {});return self.public.post(self.url,data=d)
    def test_public_guest_booking_and_duplicate_retry(self):
        token=self.form();r=self.post(token=token);self.assertEqual(r.status_code,303,r.text)
        b=self.q("SELECT * FROM bookings WHERE source='Website'",one=True);self.assertEqual((b['site_id'],b['status'],b['dietary_requirements'],b['special_occasion']),(1,'Confirmed','None','No special occasion'))
        self.assertEqual(self.post(token=token).location,r.location);self.assertEqual(len(self.q("SELECT id FROM bookings WHERE source='Website'")),1)
        receipt=self.public.get(r.location);self.assertEqual(receipt.status_code,200);self.assertNotIn('guest@example.test',receipt.text)
        self.assertIn('no-store',receipt.headers['Cache-Control'])
    def test_required_answers_and_contacts(self):
        for field in ('name','email','phone','allergies','occasion'):
            self.assertEqual(self.post({field:''}).status_code,400,field)
        for changes in ({'email':'not-mail'},{'phone':'abc'},{'party':'0'},{'date':'2026-99-99'},{'time':'25:00'}):self.assertEqual(self.post(changes).status_code,400)
        self.assertEqual(len(self.q("SELECT id FROM bookings WHERE source='Website'")),0)
    def test_site_binding_and_private_fields_never_public(self):
        self.note('PRIVATE COMMENT SENTINEL');self.vip()
        html=self.public.get(self.url).text;self.assertNotIn('PRIVATE COMMENT SENTINEL',html);self.assertNotIn('VIP',html)
        self.assertEqual(self.public.get('/api/bookings/overview').status_code,401)
        self.assertEqual(self.public.get('/book/unknown').status_code,404)
        self.auth(1,2);other=self.client.get('/api/bookings/website-link').json['url'].replace('http://localhost','')
        self.assertNotEqual(other,self.url);self.assertIn('paused',self.public.get(other).text)
        self.auth(1,1)
        self.assertEqual(self.public.post(other,data=self.data()).status_code,200)
        self.assertEqual(len(self.q("SELECT id FROM bookings WHERE source='Website'")),0)
    def test_csrf_disabled_subscription_and_staff_public_access(self):
        self.assertEqual(self.post({'form_token':'invalid'}).status_code,400)
        self.client.post('/api/bookings/website-link',json={'enabled':False,'contact_phone':'01332 123456'},headers=self.headers())
        self.assertIn('paused',self.public.get(self.url).text)
        self.client.post('/api/bookings/website-link',json={'enabled':True,'contact_phone':'01332 123456'},headers=self.headers())
        self.blocked=True;self.assertIn('paused',self.public.get(self.url).text);self.blocked=False
        self.auth(2);self.assertEqual(self.client.get(self.url).status_code,200);self.assertEqual(self.client.get('/api/bookings/website-link').status_code,403)
    def test_capacity_hours_and_session_rate_limit(self):
        self.assertEqual(self.post({'time':'13:00'}).status_code,400)
        self.assertEqual(self.post({'party':'50'}).status_code,400)
        self.assertEqual(self.post().status_code,303)
        self.assertEqual(self.post().status_code,400)
        for i in range(20):r=self.post({'time':'13:00'})
        self.assertEqual(r.status_code,429)
    def test_legacy_guest_answers_remain_missing_until_updated(self):
        r=self.q('SELECT * FROM bookings WHERE id=1',one=True);self.assertEqual(r['special_occasion'],'')
        self.assertEqual(self.patch(1,{'dietary_requirements':'None'}).status_code,400)
        valid=dict(guest_name='Example',guest_email='example@example.test',guest_phone='07700 900123',dietary_requirements='None',special_occasion='No special occasion')
        self.assertEqual(self.patch(1,valid).status_code,200)
    def register_existing_handlers(self):
        import secrets
        from flask import request,render_template
        self.app._got_first_request=False
        self.db.execute('UPDATE booking_settings SET confirmation_enabled=0');self.db.commit()
        def execute(sql,args=()):
            cur=self.db.execute(sql,args);self.db.commit();return cur.lastrowid
        env=dict(self.env,request=request,jsonify=jsonify,render_template=render_template,datetime=datetime,secrets=secrets,execute=execute,booking_available_tables=lambda *a,**k:[{'id':1}],upsert_guest=lambda *a:1,_deposit_amount=lambda *a:0)
        for name,path,methods in [('create_booking','/api/bookings',['POST']),('manage_reservation_page','/manage-reservation/<token>',['GET']),('manage_reservation_action','/manage-reservation/<token>',['POST'])]:
            node=next(n for n in ast.parse((ROOT/'app.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name==name);node.decorator_list=[]
            exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py','exec'),env)
            self.app.add_url_rule(path,name,env[name],methods=methods)
    def test_manager_creation_persists_required_guest_answers(self):
        self.register_existing_handlers()
        data=dict(booking_date=self.day,booking_time='19:00',party_size=2,guest_name='Example',guest_email='EXAMPLE@example.test',guest_phone='07700 900123',dietary_requirements='None',special_occasion='Birthday')
        missing=dict(data,special_occasion='');self.assertEqual(self.client.post('/api/bookings',json=missing,headers=self.headers()).status_code,400)
        r=self.client.post('/api/bookings',json=data,headers=self.headers());self.assertEqual(r.status_code,200,r.text)
        b=self.q('SELECT * FROM bookings WHERE id=?',(r.json['id'],),True);self.assertEqual((b['guest_email'],b['special_occasion']),('example@example.test','Birthday'))
    def test_guest_amendment_requires_csrf_and_retains_answers(self):
        self.register_existing_handlers();r=self.post();b=self.q("SELECT * FROM bookings WHERE source='Website'",one=True);url='/manage-reservation/'+b['manage_token']
        page=self.public.get(url);self.assertEqual(page.status_code,200,page.text)
        token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        data=dict(guest_name='Example',guest_email='example@example.test',guest_phone='07700 900123',dietary_requirements='None',special_occasion='Anniversary')
        self.assertEqual(self.public.post(url,data=data).status_code,403)
        self.assertEqual(self.public.post(url,data=dict(data,csrf=token,special_occasion='')).status_code,400)
        result=self.public.post(url,data=dict(data,csrf=token));self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.q('SELECT special_occasion FROM bookings WHERE id=?',(b['id'],),True)['special_occasion'],'Anniversary')
    def test_social_intake_requires_all_answers(self):
        state={};booked=[]
        def book(d,t):booked.append(d);return 999
        answers=['NEW BOOKING',self.day,'19:00','2','Example Guest','guest@example.test','07700 900123','None','No special occasion','None','CONFIRM']
        for answer in answers:state,reply=social_booking.advance(state,answer,lambda d:([{'id':1}],'available'),lambda d:[],book)
        self.assertEqual(state['stage'],'booked');self.assertEqual(booked[0]['allergies'],'None');self.assertEqual(booked[0]['occasion'],'No special occasion');self.assertTrue(booked[0]['email'] and booked[0]['phone'])
        old={'stage':'confirm','details':{'name':'Example','contact':'guest@example.test'}}
        state,reply=social_booking.advance(old,'CONFIRM',lambda d:([{'id':1}],'available'),lambda d:[],book);self.assertEqual(state['stage'],'date')

def load_tests(loader,tests,pattern):return unittest.TestSuite(PublicTests(n) for n in PublicTests.__dict__ if n.startswith('test_'))
