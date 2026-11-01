import sqlite3,unittest
from functools import wraps
from flask import Flask,session,render_template_string,redirect
from alport_hq_preview import register_hq_preview

class HQPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.app.secret_key='fictional-test';self.app.testing=True
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript("""CREATE TABLE sites(id INTEGER,organisation_id INTEGER,active INTEGER,name TEXT);
        CREATE TABLE users(id INTEGER,organisation_id INTEGER,active INTEGER,role TEXT,name TEXT,email TEXT);
        CREATE TABLE alport_staff_access(user_id INTEGER);
        INSERT INTO sites VALUES(20,10,1,'Main'),(21,11,1,'Other'),(22,10,0,'Closed');
        INSERT INTO users VALUES(1,10,1,'Owner','Owner','different-from-hq@example.test'),(2,11,1,'Owner','Other','other@example.test'),(3,10,0,'Owner','Inactive','inactive@example.test'),(4,10,1,'Manager','Manager','manager@example.test'),(5,10,1,'Owner','Staff','staff@example.test');
        INSERT INTO alport_staff_access VALUES(5);
        """)
        def q(sql,args=(),one=False):
            cur=self.db.execute(sql,args);return cur.fetchone() if one else cur.fetchall()
        def required(fn):
            @wraps(fn)
            def wrapped(*a,**k):
                if not session.get('company_admin'):return redirect('/company-admin/login')
                return fn(*a,**k)
            return wrapped
        self.events=[];self.blocked=False
        self.env=dict(q=q,company_admin_logged_in=lambda:bool(session.get('company_admin')),company_admin_required=required,subscription_for=lambda oid:{'status':'Payment required'},subscription_blocks_access=lambda sub:self.blocked,company_event=lambda *a:self.events.append(a))
        self.app.add_url_rule('/company-admin','company_admin_home',required(lambda:render_template_string('{{ hq_preview_csrf }}')))
        register_hq_preview(self.app,self.env);self.c=self.app.test_client()
        with self.c.session_transaction() as s:s.update(company_admin=True,company_admin_email='hq@example.test',old_private_unlock='must clear')
        self.token=self.c.get('/company-admin').text
    def tearDown(self):self.db.close()
    def post(self,**changes):
        d=dict(csrf=self.token,organisation_id='10',site_id='20',user_id='1');d.update(changes)
        return self.c.post('/company-admin/venue-preview',data=d)
    def test_explicit_hq_choice_opens_exact_owner_site_and_audits_actor(self):
        before=self.db.total_changes;r=self.post();self.assertEqual(r.status_code,303);self.assertEqual(r.location,'/app?setup_section=dashboard')
        self.assertEqual(self.db.total_changes,before)
        with self.c.session_transaction() as s:
            self.assertEqual((s['user_id'],s['site_id'],s['billing_preview_bypass_org'],s['billing_preview_user_id']),(1,20,10,1))
            self.assertTrue(s['company_admin']);self.assertNotIn('old_private_unlock',s)
        self.assertEqual(self.events,[('Venue preview opened','HQ hq@example.test selected owner user 1, site 20',10)])
    def test_tenant_owner_alone_cannot_open_preview(self):
        with self.c.session_transaction() as s:s.clear();s['user_id']=1
        self.assertEqual(self.post().location,'/company-admin/login');self.assertEqual(self.events,[])
    def test_csrf_required_and_single_session_transition(self):
        self.assertEqual(self.post(csrf='wrong').status_code,403);self.assertEqual(self.post().status_code,303)
        self.assertEqual(self.post().status_code,403);self.assertEqual(len(self.events),1)
    def test_cross_tenant_site_and_owner_rejected(self):
        for changes in [dict(site_id='21'),dict(user_id='2'),dict(organisation_id='11')]:self.assertEqual(self.post(**changes).status_code,403)
        self.assertEqual(self.events,[])
    def test_inactive_staff_and_nonowner_rejected(self):
        for uid in ['3','4','5']:self.assertEqual(self.post(user_id=uid).status_code,403)
        self.assertEqual(self.post(site_id='22').status_code,403)
    def test_explicit_selection_required_no_first_owner_fallback(self):
        for key in ['user_id','site_id','organisation_id']:self.assertEqual(self.post(**{key:''}).status_code,400)
        self.assertEqual(self.events,[])
    def test_blocked_subscription_stays_blocked(self):
        self.blocked=True;self.assertEqual(self.post().status_code,403);self.assertEqual(self.events,[])
    def test_audit_failure_does_not_change_session(self):
        def fail(*a):raise RuntimeError('audit unavailable')
        self.env['company_event']=fail
        with self.assertRaises(RuntimeError):self.post()
        with self.c.session_transaction() as s:self.assertNotIn('billing_preview_admin',s);self.assertEqual(s['old_private_unlock'],'must clear')
