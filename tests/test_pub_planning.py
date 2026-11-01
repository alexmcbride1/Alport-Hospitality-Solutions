import unittest
import io
from datetime import date,timedelta
from flask import session
from test_pub import PubTests
from test_team_chat import DB
from alport_pub_planning import register_pub_planning,ALLERGENS,forecast

class PlanningTests(PubTests):
    def setUp(self):
        super().setUp()
        self.app._got_first_request=False
        env=dict(conn=lambda:DB(self.db),q=self.q,user=lambda:self.q('SELECT * FROM users WHERE id=? AND active=1',(session.get('user_id'),),True),subscription_for=lambda oid:{},subscription_blocks_access=lambda sub:self.blocked)
        register_pub_planning(self.app,env)
        self.db.execute("INSERT INTO menu_items(id,organisation_id,site_id,name,category,selling_price,active) VALUES(1,1,1,'Soup','Food',8,1)")
        self.db.execute("INSERT INTO menu_components(id,organisation_id,site_id,menu_item_id,component_name,quantity,unit) VALUES(1,1,1,1,'Base',1,'litre')")
        self.db.commit()
    def matrix(self):return self.client.get('/api/pub/sites/1/allergens').json
    def ingredient(self):
        d=self.matrix()['ingredients'][0]
        return dict(stock_id=d['stock']['id'],signature=d['signature'],version=d['assessment']['version'] if d['assessment'] else 0,states={a:'Not declared in reviewed source' for a in ALLERGENS},source='Supplier specification v1; preparation controls checked',checked_on=self.day)
    def link(self):
        c=self.matrix()['matrix'][0]['components'][0]
        return self.pub('/sites/1/allergens/link',dict(component_id=1,stock_id=1,component_signature=c['component_signature'],expected_stock_id=c['linked_stock_id']))
    def review(self):
        d=self.matrix()['matrix'][0]
        return self.pub('/sites/1/allergens/review',dict(menu_id=1,signature=d['signature'],notes='Recipe checked; separate utensils and verified supplier labels.'))
    def repair(self):return dict(date=self.day,equipment='Cellar cooler',reference='C1',urgency='Critical',details='Cooler fault',assignee_id=2,contractor='',estimated_cost='50',actual_cost='0',due_date=self.day,completion_date='',repair_status='Open',notes='')
    def test_forecast_boundaries_cancellation_and_decimal_accuracy(self):
        start=date(2026,1,1)
        e=lambda days,amount,direction='Outgoing',certainty='Expected':dict(date=(start+timedelta(days=days)).isoformat(),amount=amount,direction=direction,certainty=certainty)
        w=forecast(str(start),'0.30',[e(0,'0.10'),e(6,'0.20'),e(7,'2','Income'),e(90,'1'),e(91,'99'),e(-1,'99'),e(2,'99',certainty='Cancelled')])
        self.assertEqual(len(w),13);self.assertEqual(w[0]['closing'],'0.00');self.assertEqual(w[1]['closing'],'2.00');self.assertEqual(w[-1]['closing'],'1.00')
    def test_forecast_api_versions_future_entries_and_scope(self):
        d=dict(start_date=self.day,opening_cash='100',assumptions='Manual opening balance; wages estimate, not approved payroll.',version=0)
        self.assertEqual(self.pub('/sites/1/forecast',d).status_code,200)
        self.assertEqual(self.pub('/sites/1/forecast',d).status_code,409)
        future=(date.today()+timedelta(days=10)).isoformat()
        r=self.create('cashflow',dict(date=future,direction='Outgoing',category='Wages',amount='200',certainty='Expected',assumption='Estimated wages, unapproved'))
        self.assertEqual(r.status_code,201)
        self.assertTrue(any(x['label']=='Forecast weeks below zero' for x in self.client.get('/api/today/sites/1').json['pub_attention']))
        self.sign_pub(2);self.assertEqual(self.client.get('/api/pub/sites/1/forecast').status_code,403)
        self.sign_pub(5);self.assertEqual(self.client.get('/api/pub/sites/1/forecast').status_code,403)
    def test_allergens_unknown_review_and_supplier_recipe_invalidation(self):
        self.assertTrue(self.matrix()['matrix'][0]['unknown']);self.assertEqual(self.review().status_code,400)
        self.assertEqual(self.link().status_code,200)
        d=self.ingredient();d['states']['Milk']='Contains'
        self.assertEqual(self.pub('/sites/1/allergens/ingredient',d).status_code,200)
        self.assertEqual(self.review().status_code,200)
        m=self.matrix()['matrix'][0];self.assertFalse(m['review_required']);self.assertEqual(m['states']['Milk'],'Contains')
        self.db.execute("UPDATE menu_components SET quantity=2 WHERE id=1");self.db.commit()
        self.assertTrue(self.matrix()['matrix'][0]['review_required']);self.assertEqual(self.review().status_code,200)
        self.db.execute("UPDATE stock_items SET supplier='New supplier' WHERE id=1");self.db.commit()
        m=self.matrix()['matrix'][0];self.assertTrue(m['unknown']);self.assertTrue(m['review_required'])
        self.assertEqual(self.pub('/sites/1/allergens/ingredient',d).status_code,409)
        self.assertEqual(self.review().status_code,400)
    def test_allergen_link_inactive_recovery_conflicts_and_access(self):
        self.assertEqual(self.link().status_code,200)
        self.db.execute("UPDATE stock_items SET active=0 WHERE id=1")
        self.db.execute("INSERT INTO stock_items(id,organisation_id,site_id,name,category,unit,active) VALUES(4,1,1,'New ingredient','Food','kg',1)");self.db.commit()
        c=self.matrix()['matrix'][0]['components'][0];self.assertIsNone(c['stock']);self.assertEqual(c['linked_stock_id'],1)
        d=dict(component_id=1,stock_id=4,component_signature=c['component_signature'],expected_stock_id=1)
        self.assertEqual(self.pub('/sites/1/allergens/link',d).status_code,200)
        self.assertEqual(self.pub('/sites/1/allergens/link',d).status_code,409)
        self.sign_pub(2);self.assertEqual(self.client.get('/api/pub/sites/1/allergens').status_code,200)
        self.assertEqual(self.matrix()['ingredients'],[])
        self.assertEqual(self.pub('/sites/1/allergens/link',d).status_code,403)
        self.assertEqual(self.client.get('/api/pub/sites/2/allergens').status_code,403)
    def test_allergen_explicit_states_and_version(self):
        d=self.ingredient();d['states'].pop('Milk');self.assertEqual(self.pub('/sites/1/allergens/ingredient',d).status_code,400)
        d=self.ingredient();self.assertEqual(self.pub('/sites/1/allergens/ingredient',d).status_code,200)
        self.assertEqual(self.pub('/sites/1/allergens/ingredient',d).status_code,409)
    def test_repair_completion_photos_and_tenant_boundary(self):
        d=self.repair();rid=self.create('repair',d).json['id']
        self.assertEqual(self.pub(f'/sites/1/records/{rid}/update',dict(version=1,data={**d,'repair_status':'Done'})).status_code,400)
        path=f'/api/pub/sites/1/records/{rid}/photo'
        import base64
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        r=self.client.post(path,data={'photo':(io.BytesIO(png),'cooler.png')},headers={'X-Pub-CSRF':self.pubcsrf});self.assertEqual(r.status_code,201);pid=r.json['id']
        self.assertEqual(self.client.post(path,data={'photo':(io.BytesIO(b'<script>'),'bad.png')},headers={'X-Pub-CSRF':self.pubcsrf}).status_code,400)
        self.sign_pub(2);url=f'/api/pub/sites/1/records/{rid}/photos/{pid}'
        self.assertEqual(self.client.get(url).data,png)
        self.assertEqual(self.client.post(path,data={'photo':(io.BytesIO(png),'cooler.png')},headers={'X-Pub-CSRF':self.pubcsrf}).status_code,403)
        self.sign_pub(5);self.assertEqual(self.client.get(url).status_code,403)
        self.sign_pub(1);self.assertEqual(self.pub(f'/sites/1/records/{rid}/update',dict(version=1,data={**d,'repair_status':'Done','completion_date':self.day,'notes':'Repaired and checked'})).status_code,200)
    def test_handover_action_is_site_task_idempotent_and_private(self):
        d=dict(date=self.day,reference='Evening',staffing='Covered',bookings='None',unavailable='None',complaints='Private manager note',faults='Cooler',summary='Follow up')
        rid=self.create('handover',d).json['id'];path=f'/sites/1/records/{rid}/action'
        action=dict(title='Call cooler contractor',assignee_id=2,due_date=self.day,request_key='handover-test-key-0001')
        r=self.pub(path,action);self.assertEqual(r.status_code,201)
        self.assertEqual(self.pub(path,action).json['task_id'],r.json['task_id'])
        self.assertEqual(self.pub(path,{**action,'title':'Changed'}).status_code,409)
        self.assertEqual(self.pub(path,{**action,'assignee_id':3,'request_key':'another-action-0001'}).status_code,400)
        self.sign_pub(2);messages=self.read().json['messages'];self.assertEqual(len(messages),1);self.assertNotIn('Private manager note',messages[0]['body'])
        self.assertEqual(self.client.get('/api/pub/sites/1/records/handover').status_code,403)
        self.assertEqual(self.pub(path,action).status_code,403)
        self.assertEqual(self.client.get('/api/team-chat/sites/1/tasks').json['tasks'][0]['assignee_id'],2)

def load_tests(loader,tests,pattern):return unittest.TestSuite(PlanningTests(n) for n in PlanningTests.__dict__ if n.startswith('test_'))
