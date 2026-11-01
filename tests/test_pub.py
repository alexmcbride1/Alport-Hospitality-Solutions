import unittest
import re
from decimal import Decimal
from test_experience import ExperienceTests
from test_team_chat import ROOT
from alport_pub import register_pub,cashup,yield_summary

class PubTests(ExperienceTests):
    def setUp(self):
        super().setUp()
        source=(ROOT/'app.py').read_text()
        for table in ('menu_items','menu_components'):
            sql=re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\([\s\S]*?\n\);',source)[0]
            self.db.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY'))
        self.app._got_first_request=False
        from flask import session
        from test_team_chat import DB
        env=dict(conn=lambda:DB(self.db),q=self.q,user=lambda:self.q('SELECT * FROM users WHERE id=? AND active=1',(session.get('user_id'),),True),subscription_for=lambda oid:{},subscription_blocks_access=lambda sub:self.blocked)
        register_pub(self.app,env)
        self.pubcsrf=self.client.get('/api/pub/config').json['csrf']
        for sid in (1,2,3):self.db.execute("INSERT INTO stock_items(id,organisation_id,site_id,name,category,unit,on_hand,active) VALUES(?,?,?,'Beer','Drink','litre',50,1)",(sid,1 if sid<3 else 2,sid))
        self.db.commit()
    def sign_pub(self,uid):
        self.sign(uid);r=self.client.get('/api/pub/config');self.pubcsrf=r.json.get('csrf','');return r
    def pub(self,path,data):return self.client.post('/api/pub'+path,json=data,headers={'X-Pub-CSRF':self.pubcsrf})
    def create(self,kind,data,site=1,key='pub-test-request-000001'):
        return self.pub(f'/sites/{site}/records/{kind}',dict(data=data,request_key=key))
    def cash(self):return dict(date=self.day,reference='Till 1 evening',opening_float='100',cash_sales='200',cash_refunds='10',paid_out='5',cash_counted='280',card_sales='300',card_refunds='20',card_received='280',discounts='15',notes='Cash £5 short; counted twice.')
    def test_cashup_arithmetic_idempotency_and_signed_immutability(self):
        r=self.create('cashup',self.cash());self.assertEqual(r.status_code,201);rid=r.json['id']
        self.assertEqual(self.create('cashup',self.cash()).json['id'],rid)
        self.assertEqual(self.create('cashup',self.cash(),key='another-request-0001').status_code,409)
        d=self.client.get('/api/pub/sites/1/records/cashup').json['records'][0]['data']
        self.assertEqual(d['expected_cash'],'285.00');self.assertEqual(d['cash_variance'],'-5.00');self.assertEqual(d['card_variance'],'0.00')
        self.assertEqual(self.pub(f'/sites/1/records/{rid}/sign',dict(version=1)).status_code,200)
        self.assertEqual(self.pub(f'/sites/1/records/{rid}/update',dict(version=2,data=self.cash())).status_code,400)
        events=self.client.get(f'/api/pub/sites/1/records/{rid}/history').json['events'];self.assertEqual(len(events),2)
    def test_cashup_requires_variance_explanation(self):
        d=self.cash();d['notes']='';rid=self.create('cashup',d).json['id']
        self.assertEqual(self.pub(f'/sites/1/records/{rid}/sign',dict(version=1)).status_code,400)
    def test_staff_reports_are_private_and_cash_restricted(self):
        self.sign_pub(2)
        self.assertEqual(self.create('cashup',self.cash()).status_code,403)
        d=dict(date=self.day,time='19:00',reason='Age verification',details='Sale refused; no acceptable ID.',reference='')
        rid=self.create('refusal',d).json['id']
        self.assertEqual(self.create('refusal',d,site=2).status_code,403)
        self.sign_pub(6)
        self.assertEqual(self.client.get('/api/pub/sites/1/records/refusal').json['records'],[])
        self.assertEqual(self.client.get(f'/api/pub/sites/1/records/{rid}/history').status_code,403)
        self.sign_pub(1);self.assertEqual(len(self.client.get('/api/pub/sites/1/records/refusal').json['records']),1)
    def test_wastage_atomic_once_and_cross_site(self):
        d=dict(date=self.day,stock_id=1,quantity=2,reason='Line cleaning loss')
        r=self.create('waste',d);self.assertEqual(r.status_code,201)
        self.assertEqual(self.create('waste',d).status_code,200)
        self.assertEqual(self.q('SELECT on_hand FROM stock_items WHERE id=1',one=True)['on_hand'],48)
        self.assertEqual(self.q('SELECT COUNT(*) AS n FROM stock_movements',one=True)['n'],1)
        self.assertEqual(self.create('waste',{**d,'stock_id':2},key='different-key-00001').status_code,400)
        self.assertEqual(self.create('waste',{**d,'quantity':100},key='different-key-00002').status_code,400)
        self.assertEqual(self.q('SELECT on_hand FROM stock_items WHERE id=1',one=True)['on_hand'],48)
    def test_validation_csrf_versions_and_tenant(self):
        self.assertEqual(self.client.post('/api/pub/sites/1/records/cashup',json={}).status_code,403)
        self.assertEqual(self.create('cashup',{**self.cash(),'cash_sales':'NaN'}).status_code,400)
        self.assertEqual(self.create('cashup',{**self.cash(),'cash_sales':'1.001'}).status_code,400)
        self.assertEqual(self.create('cashup',self.cash(),site=3).status_code,403)
        rid=self.create('cashup',self.cash()).json['id']
        self.assertEqual(self.pub(f'/sites/1/records/{rid}/update',dict(version=99,data=self.cash())).status_code,409)
        self.sign_pub(5);self.assertEqual(self.client.get(f'/api/pub/sites/1/records/{rid}/history').status_code,403)
    def test_cellar_math_and_dashboard_attention(self):
        d=dict(volume_ml=50000,serve_ml=500,sold=80,waste_ml=1000,remaining_ml=8000)
        y=yield_summary(d);self.assertEqual(Decimal(y['expected_servings']),100);self.assertEqual(Decimal(y['unaccounted_ml']),1000)
        self.create('cleaning',dict(date=self.day,line='Tap 1',method='Supplier procedure',due_date=self.day,notes=''))
        self.assertTrue(any(x['label']=='Line cleans due' for x in self.client.get('/api/today/sites/1').json['pub_attention']))
        self.assertEqual(self.client.get('/api/today/sites/2').json['pub_attention'],[])

def load_tests(loader,tests,pattern):return unittest.TestSuite(PubTests(n) for n in PubTests.__dict__ if n.startswith('test_'))
