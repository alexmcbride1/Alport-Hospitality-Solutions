"""Real Flask requests with SQLite fixtures; PostgreSQL deployment still required."""
from datetime import datetime
from zoneinfo import ZoneInfo
import re
from test_team_chat import TeamChatTests, ROOT
from alport_experience import register_experience

class ExperienceTests(TeamChatTests):
    def setUp(self):
        super().setUp()
        source=(ROOT/'app.py').read_text()
        for table in ('sales','expenses','stock_items','stock_movements','shifts','bookings','budgets','stocktakes','payroll_runs','eho_daily_checks','eho_records','audit_log'):
            sql=re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\([\s\S]*?\n\);',source)[0]
            self.db.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY'))
        from flask import session
        env=dict(q=self.q,user=lambda:self.q('SELECT * FROM users WHERE id=? AND active=1',(session.get('user_id'),),True),subscription_for=lambda oid:{},subscription_blocks_access=lambda sub:self.blocked)
        self.app._got_first_request=False
        register_experience(self.app,env)
        self.month=datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m')
        self.day=self.month+'-01'

    def chat_api(self,path,data):
        return self.client.post('/api/team-chat'+path,json=data,headers={'X-Chat-CSRF':self.csrf})

    def test_hub_site_boundaries(self):
        self.assertEqual(len(self.client.get('/api/today/config').json['sites']),2)
        self.assertEqual(self.client.get('/api/today/sites/3').status_code,403)
        self.sign(2)
        self.assertEqual([s['id'] for s in self.client.get('/api/today/config').json['sites']],[1])
        self.assertTrue(self.client.get('/api/today/sites/1').json['personal'])
        self.assertEqual(self.client.get('/api/today/sites/2').status_code,403)
        self.assertEqual(self.client.get('/api/today/overview').status_code,403)
        self.assertEqual(self.client.get('/api/today/sites/1/accountability').status_code,403)

    def test_chat_grant_not_financial_access(self):
        self.grant(4)
        self.sign(4)
        self.assertEqual(self.client.get('/api/today/config').json['sites'],[])
        self.assertEqual(self.client.get('/api/today/sites/1').status_code,403)

    def test_finance_zero_warnings_and_budget(self):
        d=self.client.get('/api/today/sites/1').json
        self.assertEqual(len(d['finance']['warnings']),4)
        self.assertIsNone(d['budget'])
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(1,1,?,'Food',100)",(self.day,))
        self.db.execute("UPDATE employees SET pay_rate=12 WHERE id=1")
        self.db.execute("INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes) VALUES(1,1,1,?,'22:00','02:00',30)",(self.day,))
        self.db.execute("INSERT INTO budgets(organisation_id,site_id,month,revenue,labour) VALUES(1,1,?,120,50)",(self.month,))
        self.db.commit()
        d=self.client.get('/api/today/sites/1').json
        self.assertEqual(d['finance']['actual']['revenue'],100)
        self.assertEqual(d['finance']['actual']['labour'],42)
        self.assertEqual(d['finance']['actual']['ebitda'],58)
        self.assertEqual(d['budget']['ebitda'],70)
        self.assertEqual(self.client.get('/api/today/sites/2').json['finance']['actual']['revenue'],0)
        self.assertEqual(self.client.get('/api/today/sites/1?month=invalid').status_code,400)

    def test_overview_owner_only_and_site_switch_csrf(self):
        d=self.client.get('/api/today/overview').json
        self.assertEqual([s['site']['id'] for s in d['sites']],[2,1])
        url='/api/today/sites/2/open'
        self.assertEqual(self.client.post(url,json={}).status_code,403)
        self.assertEqual(self.client.post(url,json={},headers={'X-Chat-CSRF':self.csrf}).status_code,200)
        self.assertEqual(self.client.post('/api/today/sites/3/open',json={},headers={'X-Chat-CSRF':self.csrf}).status_code,403)

    def test_mentions_unread_read_receipt_and_retries(self):
        response=self.post(mentions=[2]);mid=response.json['id']
        self.assertEqual(self.post(mentions=[2]).status_code,200)
        self.assertEqual(self.post(mentions=[]).status_code,409)
        self.assertEqual(self.post(key='test-other-00000001',mentions=[3]).status_code,400)
        self.sign(2)
        d=self.client.get('/api/team-chat/unread').json['sites'][0]
        self.assertEqual((d['unread'],d['mentions']),(1,1))
        self.assertEqual(self.chat_api('/sites/1/read',{'last_id':mid}).status_code,200)
        self.assertEqual(self.client.get('/api/team-chat/unread').json['sites'][0]['unread'],0)
        self.assertEqual(self.chat_api('/sites/1/read',{'last_id':9999}).status_code,400)
        self.assertEqual(self.chat_api('/sites/2/read',{'last_id':mid}).status_code,403)

    def test_tasks_assignment_history_conflict_and_permissions(self):
        mid=self.post().json['id']
        data=dict(message_id=mid,title='Check cellar stock',assignee_id=2,due_date='2026-12-01')
        path='/sites/1/tasks'
        self.assertEqual(self.chat_api(path,{**data,'assignee_id':3}).status_code,400)
        self.assertEqual(self.chat_api('/sites/2/tasks',data).status_code,400)
        created=self.chat_api(path,data);self.assertEqual(created.status_code,201);tid=created.json['id']
        self.assertEqual(self.chat_api(path,data).status_code,409)
        self.sign(6)
        self.assertEqual(self.chat_api(path+'/'+str(tid),dict(status='Done',version=1)).status_code,403)
        self.sign(2)
        self.assertEqual(self.chat_api(path+'/'+str(tid),dict(status='Done',version=1)).status_code,200)
        self.assertEqual(self.chat_api(path+'/'+str(tid),dict(status='Open',version=1)).status_code,409)
        self.assertEqual(len(self.client.get('/api/team-chat'+path+'/'+str(tid)+'/history').json['events']),2)
        self.sign(3)
        self.assertEqual(self.client.get('/api/team-chat'+path).status_code,403)

    def test_revoked_assignment_loses_dashboard_and_tasks(self):
        self.sign(2)
        self.db.execute("UPDATE alport_staff_access SET profile='disabled' WHERE user_id=2")
        self.db.commit()
        self.assertEqual(self.client.get('/api/today/config').status_code,403)
        self.assertEqual(self.client.get('/api/team-chat/sites/1/tasks').status_code,403)

# Inherited chat tests run once from their own module, not again in this suite.
def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(ExperienceTests(name) for name in ExperienceTests.__dict__ if name.startswith('test_'))
