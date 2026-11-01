"""Site chat integration tests. Run: python -m unittest discover -s tests -p 'test_team_chat.py'."""
import hashlib
from pathlib import Path
import re
import sqlite3
import unittest
from flask import Flask, session
from alport_staff import register_staff

ROOT = Path(__file__).resolve().parents[1]

class Cursor:
    def __init__(self, cursor): self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone()
        return dict(row) if row is not None else None
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]

class DB:
    def __init__(self, db): self.db=db
    def execute(self, sql, args=()):
        if 'pg_advisory' in sql or 'DROP CONSTRAINT IF EXISTS' in sql: sql,args='SELECT 1',()
        sql=sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('ADD COLUMN IF NOT EXISTS','ADD COLUMN').replace('%s','?')
        return Cursor(self.db.execute(sql,args))
    def __enter__(self): return self
    def __exit__(self, error, *args): self.db.rollback() if error else self.db.commit()

class TeamChatTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        source=(ROOT/'app.py').read_text()
        for table in ('organisations','users','sites','employees'):
            sql=re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\([\s\S]*?\n\);',source)[0]
            self.db.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY'))
        for oid,name in ((1,'Example Hospitality'),(2,'Other Business')):
            self.db.execute("INSERT INTO organisations VALUES(?,?,'Pub','GBP',1,'today')",(oid,name))
        for sid,oid,name in ((1,1,'Riverside'),(2,1,'Hilltop'),(3,2,'Foreign venue')):
            self.db.execute("INSERT INTO sites(id,organisation_id,name,active,created_at) VALUES(?,?,?,1,'today')",(sid,oid,name))
        for uid,oid,role in ((1,1,'Owner'),(2,1,'Staff'),(3,1,'Staff'),(4,1,'Manager'),(5,2,'Owner'),(6,1,'Staff'),(7,1,'Admin'),(8,1,'Staff'),(9,1,'User')):
            self.db.execute("INSERT INTO users(id,organisation_id,name,email,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?,1,'today')",(uid,oid,'User '+str(uid),str(uid)+'@example.test','test-hash',role))
        self.app=Flask(__name__,template_folder=str(ROOT/'templates'),static_folder=str(ROOT/'static'))
        self.app.config.update(TESTING=True,SECRET_KEY='test-only')
        self.blocked=False
        def q(sql,args=(),one=False):
            c=DB(self.db).execute(sql,args)
            return c.fetchone() if one else c.fetchall()
        self.q=q
        env=dict(conn=lambda:DB(self.db),q=q,user=lambda:q('SELECT * FROM users WHERE id=? AND active=1',(session.get('user_id'),),True),current_site=lambda:q('SELECT * FROM sites WHERE id=?',(session.get('site_id'),),True),subscription_for=lambda oid:{},subscription_blocks_access=lambda sub:self.blocked)
        # Includes the production staff guard, evidence registration and chat registration.
        register_staff(self.app,env)
        for eid,uid,sid,profile in ((1,2,1,'staff'),(2,3,2,'orders'),(3,6,1,'staff'),(4,6,2,'staff'),(5,8,1,'disabled')):
            self.db.execute("INSERT INTO employees(id,organisation_id,site_id,name,active) VALUES(?,1,?,'Employee',1)",(eid,sid))
            self.db.execute("INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,activated_at,created_at,updated_at) VALUES(1,?,?,?,?,'today','today','today')",(sid,eid,uid,profile))
        self.db.commit();self.client=self.app.test_client();self.sign(1)

    def sign(self,uid,site=1):
        with self.client.session_transaction() as s:
            s.clear();s.update(user_id=uid,site_id=site,staff_site_id=site,staff_auth_hash=hashlib.sha256(b'test-hash').hexdigest())
        response=self.client.get('/api/team-chat/config')
        self.csrf=response.json.get('csrf','') if response.is_json else ''
        return response

    def post(self,site=1,body='Hello team',key='test-message-000001',**extra):
        return self.client.post(f'/api/team-chat/sites/{site}/messages',json=dict(body=body,request_key=key,**extra),headers={'X-Chat-CSRF':self.csrf})
    def read(self,site=1,**params):
        return self.client.get(f'/api/team-chat/sites/{site}/messages',query_string=params)
    def grant(self,uid=4,site=1,enabled=True):
        return self.client.post(f'/api/team-chat/sites/{site}/access',json={'user_id':uid,'enabled':enabled},headers={'X-Chat-CSRF':self.csrf})

    def test_staff_can_send_and_read_only_assigned_room(self):
        self.sign(2);r=self.post();self.assertEqual(r.status_code,201)
        rows=self.read().json['messages'];self.assertEqual(rows[0]['sender_name'],'User 2')
        self.assertEqual(rows[0]['body'],'Hello team')
        self.assertEqual(self.read(2).status_code,403);self.assertEqual(self.post(2).status_code,403)

    def test_same_site_colleagues_can_communicate(self):
        self.sign(2);self.post(body='Handover for Riverside')
        self.sign(6);self.assertEqual(self.read().json['messages'][0]['body'],'Handover for Riverside')
        self.sign(3,2);self.assertEqual(self.read(2).json['messages'],[])

    def test_owner_has_only_own_business_sites(self):
        result=self.sign(1).json
        self.assertEqual([s['id'] for s in result['sites']],[2,1])
        self.assertEqual(self.read(3).status_code,403);self.assertEqual(self.post(3).status_code,403)

    def test_foreign_owner_cannot_read_known_message_or_grant_access(self):
        self.post(body='Company one only');self.sign(5,3)
        self.assertEqual(self.read(1,after='0').status_code,403)
        self.assertEqual(self.grant(4,1).status_code,403)
        self.assertEqual(self.read(3).json['messages'],[])

    def test_role_alone_does_not_grant_chat(self):
        for uid in (4,7,9):
            with self.subTest(uid=uid):
                self.assertEqual(self.sign(uid).json['sites'],[])
                self.assertEqual(self.read().status_code,403)
                self.assertEqual(self.post().status_code,403)

    def test_owner_manual_grant_is_site_specific_and_revocable(self):
        self.assertEqual(self.grant().status_code,200)
        self.sign(4);self.assertEqual(self.post().status_code,201);self.assertEqual(self.read(2).status_code,403)
        self.sign(1);self.assertEqual(self.grant(enabled=False).status_code,200)
        self.sign(4);self.assertEqual(self.read().status_code,403)
        self.assertEqual(self.q('SELECT COUNT(*) n FROM alport_chat_access_events',one=True)['n'],2)

    def test_members_cannot_grant_and_owner_cannot_add_foreign_user(self):
        self.sign(2);self.assertEqual(self.grant(4).status_code,403)
        self.sign(1);self.assertEqual(self.grant(5).status_code,400)
        self.assertEqual(self.grant(1).status_code,400)
        self.assertEqual(self.grant(2).status_code,400)

    def test_manual_grant_cannot_bypass_disabled_assignment(self):
        self.assertEqual(self.grant(8).status_code,400)
        self.db.execute("INSERT INTO alport_chat_members VALUES(1,1,8,1,'today')");self.db.commit()
        self.sign(8);self.assertEqual(self.read().status_code,403)

    def test_revoked_assignment_and_inactive_employee_block_read_and_send(self):
        self.sign(2);self.post()
        self.db.execute("UPDATE alport_staff_access SET profile='disabled' WHERE user_id=2");self.db.commit()
        self.assertEqual(self.read().status_code,403);self.assertEqual(self.post(key='test-message-000002').status_code,403)
        self.db.execute("UPDATE alport_staff_access SET profile='staff' WHERE user_id=2")
        self.db.execute('UPDATE employees SET active=0 WHERE id=1');self.db.commit()
        self.assertEqual(self.read().status_code,403)

    def test_inactive_site_and_user(self):
        self.db.execute('UPDATE sites SET active=0 WHERE id=1');self.db.commit()
        self.assertEqual(self.read().status_code,403)
        self.db.execute('UPDATE users SET active=0 WHERE id=1');self.db.commit()
        self.assertEqual(self.read().status_code,401)

    def test_anonymous_and_subscription(self):
        self.sign(999);self.assertEqual(self.read().status_code,401)
        self.assertEqual(self.client.get('/team-chat').status_code,302)
        self.sign(1);self.blocked=True;self.assertEqual(self.read().status_code,403)
        self.assertEqual(self.post().status_code,403)

    def test_csrf_and_wrong_content_type_rejected(self):
        r=self.client.post('/api/team-chat/sites/1/messages',json={'body':'Bad','request_key':'valid-key-0000001'})
        self.assertEqual(r.status_code,403)
        r=self.client.post('/api/team-chat/sites/1/messages',data='bad',headers={'X-Chat-CSRF':self.csrf})
        self.assertEqual(r.status_code,400)

    def test_client_cannot_forge_sender_or_site(self):
        self.sign(2);self.post(user_id=1,sender_name='Owner',organisation_id=2,site_id=2)
        row=self.read().json['messages'][0];self.assertEqual(row['user_id'],2);self.assertEqual(row['sender_name'],'User 2')
        self.sign(1);self.assertEqual(self.read(2).json['messages'],[])

    def test_retries_are_idempotent_and_changed_body_conflicts(self):
        one=self.post();two=self.post();self.assertEqual(one.json['id'],two.json['id'])
        self.assertEqual(len(self.read().json['messages']),1)
        self.assertEqual(self.post(body='Changed').status_code,409)

    def test_shared_database_send_limit(self):
        for n in range(20):self.assertEqual(self.post(key=f'rate-limit-key-{n:04d}').status_code,201)
        self.assertEqual(self.post(site=2,key='next-room-key-0001').status_code,429)
        self.assertEqual(self.post(key='rate-limit-key-0000').status_code,200)

    def test_payload_validation(self):
        for body in ('','  ','x'*2001,[],123,'bad\x00message'):
            with self.subTest(body=str(body)[:20]):self.assertEqual(self.post(body=body).status_code,400)
        for key in ('','short','x'*81,'not a valid key value'):
            with self.subTest(key=key):self.assertEqual(self.post(key=key).status_code,400)
        self.assertEqual(self.post(body='x'*13000).status_code,413)
        self.assertEqual(self.post(body='Line one\nLine two £ 👍').status_code,201)

    def test_history_pagination_has_no_gaps_and_no_cross_site_rows(self):
        for n in range(121):
            self.db.execute('INSERT INTO alport_chat_messages(organisation_id,site_id,user_id,sender_name,body,request_key,created_at) VALUES(1,1,1,?,?,?,?)',('Owner',str(n),str(n),'2026-11-01T12:00:00+00:00'))
        self.db.commit()
        initial=self.read().json;self.assertEqual(len(initial['messages']),50);self.assertTrue(initial['has_more'])
        before=self.read(before=str(initial['messages'][0]['id'])).json
        oldest=self.read(before=str(before['messages'][0]['id'])).json
        self.assertEqual(len(oldest['messages']),21);self.assertFalse(oldest['has_more'])
        values=[r['body'] for page in (oldest,before,initial) for r in page['messages']]
        self.assertEqual(values,[str(i) for i in range(121)])
        after=self.read(after='0').json;self.assertEqual(after['messages'][0]['body'],'0');self.assertTrue(after['has_more'])
        self.assertEqual(self.read(2,after='0').json['messages'],[])
        for params in ({'after':'-1'},{'after':'x'},{'after':'1','before':'2'},{'after':'9'*30}):
            self.assertEqual(self.read(**params).status_code,400)

    def test_password_change_revokes_chat_session(self):
        self.db.execute("UPDATE users SET password_hash='changed' WHERE id=1");self.db.commit()
        self.assertEqual(self.read().status_code,403);self.assertEqual(self.post().status_code,403)

    def test_multi_site_user_keeps_rooms_separate_without_changing_session(self):
        self.sign(6);self.post(1,body='First site');self.post(2,body='Second site')
        self.assertEqual(self.read(1).json['messages'][0]['body'],'First site')
        self.assertEqual(self.read(2).json['messages'][0]['body'],'Second site')
        with self.client.session_transaction() as s:self.assertEqual(s['site_id'],1)

    def test_html_is_data_and_page_has_protective_headers(self):
        text='<img src=x onerror=alert(1)><script>alert(2)</script>'
        self.post(body=text);r=self.read();self.assertEqual(r.json['messages'][0]['body'],text)
        self.assertIn('no-store',r.headers['Cache-Control'])
        self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
        page=self.client.get('/team-chat');self.assertEqual(page.status_code,200)
        self.assertNotIn(text.encode(),page.data)
        self.assertIn(b'Choose a site',page.data)

if __name__=='__main__':unittest.main()
