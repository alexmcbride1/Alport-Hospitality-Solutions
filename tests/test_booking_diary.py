import ast,re,unittest
from flask import session,jsonify
from test_experience import ExperienceTests
from test_team_chat import ROOT,DB
from alport_booking_diary import register_booking_diary
class DiaryDB(DB):
    def execute(self,sql,args=()):
        sql=sql.replace(' FOR UPDATE','')
        sql=sql.replace("substring(regexp_replace(COALESCE(g.phone,''),'[^0-9]','','g') FROM 5)","substr(regexp_replace(COALESCE(g.phone,''),'[^0-9]','','g'),5)").replace("substring(regexp_replace(COALESCE(g.phone,''),'[^0-9]','','g') FROM 3)","substr(regexp_replace(COALESCE(g.phone,''),'[^0-9]','','g'),3)")
        return super().execute(sql,args)
class DiaryTests(ExperienceTests):
    def setUp(self):
        super().setUp();source=(ROOT/'app.py').read_text()
        self.db.create_function('regexp_replace',4,lambda s,p,r,flags:re.sub(p,r,s))
        for table in ('guests','restaurant_tables','booking_settings','booking_sessions'):
            self.db.executescript(re.search(r'CREATE TABLE IF NOT EXISTS '+table+r'\([\s\S]*?\n\);',source)[0].replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY'))
        for field,kind in [('deposit_status',"TEXT DEFAULT 'Not required'"),('deposit_required','REAL DEFAULT 0'),('deposit_paid','REAL DEFAULT 0'),('manage_token',"TEXT DEFAULT ''")]:self.db.execute('ALTER TABLE bookings ADD COLUMN '+field+' '+kind)
        self.app._got_first_request=False
        def q(sql,args=(),one=False):
            cur=DiaryDB(self.db).execute(sql,args);return cur.fetchone() if one else cur.fetchall()
        self.q=q;self.audits=[]
        self.env=dict(q=q,conn=lambda:DiaryDB(self.db),user=lambda:q('SELECT * FROM users WHERE id=? AND active=1',(session.get('user_id'),),True),current_site=lambda:q('SELECT * FROM sites WHERE id=?',(session.get('site_id'),),True),subscription_for=lambda oid:{},subscription_blocks_access=lambda sub:self.blocked,audit=lambda *args:self.audits.append(args),minutes_of=lambda t:int(t[:2])*60+int(t[3:5]))
        for route,name,methods in [('/api/bookings/overview','bookings_overview',['GET']),('/api/bookings/guests','booking_guests',['GET']),('/api/bookings/<int:booking_id>','update_booking',['PATCH'])]:self.app.add_url_rule(route,name,lambda **kw:jsonify(ok=True),methods=methods)
        for path in ('settings','reports','tables','communications','rota-demand','retention/send'):
            self.app.add_url_rule('/api/bookings/'+path,'test_'+path,lambda:jsonify(ok=True),methods=['GET','POST'])
        register_booking_diary(self.app,self.env)
        for gid,org,name,phone in [(1,1,'Jamie Example','+44 7700 900123'),(2,1,'Jamie Example','07700 900456'),(3,2,'Foreign Guest','07700 900789')]:self.db.execute("INSERT INTO guests(id,organisation_id,name,phone,created_at,updated_at) VALUES(?,?,?,?,'seed','seed')",(gid,org,name,phone))
        for sid in (1,2,3):
            org=1 if sid<3 else 2
            self.db.execute("INSERT INTO restaurant_tables(id,organisation_id,site_id,table_name,min_capacity,max_capacity) VALUES(?,?,?,'T1',1,8)",(sid,org,sid))
            self.db.execute("INSERT INTO booking_settings(organisation_id,site_id,updated_at) VALUES(?,?,'seed')",(org,sid))
        self.seed(1,1,1,1);self.seed(2,2,2,2);self.seed(3,3,3,3,org=2)
        self.db.execute("UPDATE users SET role='Finance' WHERE id=9");self.db.commit();self.auth(1)
    def seed(self,bid,sid,gid,table,org=1,day=None,time='19:00',status='Confirmed'):
        self.db.execute("INSERT INTO bookings(id,organisation_id,site_id,guest_id,booking_date,booking_time,party_size,table_id,guest_name,status,created_at,updated_at,manage_token) VALUES(?,?,?,?,?,?,2,?,'Jamie Example',?,'seed','v1','private-token')",(bid,org,sid,gid,day or self.day,time,table,status))
    def auth(self,uid,sid=1):
        self.sign(uid,sid);r=self.client.get('/api/bookings/diary-config');self.token=r.json.get('csrf','');return r
    def headers(self):return {'X-Booking-CSRF':self.token}
    def patch(self,bid,data):return self.client.patch('/api/bookings/'+str(bid),json=data,headers=self.headers())
    def profile(self,gid=1):return self.client.get(f'/api/bookings/guests/{gid}/profile')
    def note(self,body='Anniversary preference; ask before arranging.',gid=1,key='private-comment-0001'):return self.client.post(f'/api/bookings/guests/{gid}/comments',json=dict(body=body,request_key=key),headers=self.headers())
    def vip(self,gid=1,value=True,version=0):return self.client.post(f'/api/bookings/guests/{gid}/vip',json=dict(vip=value,version=version),headers=self.headers())
    def test_manager_only_and_active_assignment(self):
        for uid in (2,3,6,9,4):
            self.auth(uid)
            for path in ('overview','guests','settings','reports','tables','communications','rota-demand','guests/1/profile'):
                self.assertEqual(self.client.get('/api/bookings/'+path).status_code,403,(uid,path))
        self.db.execute("INSERT INTO employees(id,organisation_id,site_id,name,active) VALUES(9,1,1,'Manager',1)")
        self.db.execute("INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,activated_at,created_at,updated_at) VALUES(1,1,9,4,'full','now','now','now')");self.db.commit()
        self.assertEqual(self.auth(4).status_code,200);self.assertEqual(self.auth(4,2).status_code,403)
    def test_day_projection_and_invalid_date(self):
        r=self.client.get('/api/bookings/overview?date='+self.day);self.assertEqual(r.status_code,200);self.assertEqual(len(r.json['bookings']),1)
        self.assertNotIn('manage_token',r.json['bookings'][0]);self.assertNotIn('comments',r.json['bookings'][0])
        self.assertEqual(self.client.get('/api/bookings/overview?date=2026-99-99').status_code,400)
    def test_vip_persistence_guest_link_not_name_and_undo(self):
        self.assertEqual(self.vip().status_code,200);self.seed(4,1,1,None,day='2025-01-01');self.db.commit()
        for day in (self.day,'2025-01-01'):
            r=self.client.get('/api/bookings/overview?date='+day).json;self.assertEqual(r['bookings'][0]['vip'],1)
        self.assertEqual(self.vip(value=False,version=0).status_code,409)
        self.assertEqual(self.vip(value=False,version=1).status_code,200)
        self.assertEqual(self.profile().json['vip'],0)
        self.auth(1,2);self.assertEqual(self.profile(2).json['vip'],0)
    def test_private_comments_same_guest_different_sites(self):
        self.assertEqual(self.note().status_code,201);self.assertEqual(self.note().status_code,200)
        self.assertEqual(len(self.profile().json['comments']),1)
        self.seed(4,2,1,None);self.db.commit();self.auth(1,2)
        self.assertEqual(self.profile().json['comments'],[])
        self.assertEqual(self.note('Site two note',key='site-two-comment-001').status_code,201)
        self.auth(1,1);self.assertEqual(len(self.profile().json['comments']),1)
        self.assertEqual(self.profile(2).status_code,403);self.auth(5,3);self.assertEqual(self.profile(1).status_code,403)
    def test_comment_validation_and_no_booking_email_leakage(self):
        self.assertEqual(self.note('x'*2001).status_code,400);self.assertEqual(self.note('  ').status_code,400)
        self.assertEqual(self.note('<img src=x onerror=alert(1)>').status_code,201)
        booking=self.q('SELECT * FROM bookings WHERE id=1',one=True)
        self.assertNotIn('comments',booking);self.assertEqual(booking['internal_notes'],'')
        r=self.client.get('/api/bookings/overview?date='+self.day).json['bookings'][0]
        self.assertEqual(r['comment_count'],1);self.assertNotIn('onerror',str(r))
        # Email/self-service sources use booking records; comments live only in a separate scoped table.
        source=(ROOT/'app.py').read_text();self.assertNotIn('alport_guest_site_comments',source)
    def test_search_history_phone_name_scope_and_actual_visits(self):
        self.db.execute("UPDATE bookings SET booking_date='2020-01-01',status='Completed' WHERE id=1");self.seed(4,1,1,None,status='Cancelled');self.db.commit()
        for query in ('jAMie','07700 900123','+44 (7700) 900123','00447700900123'):
            r=self.client.get('/api/bookings/guests',query_string={'search':query}).json
            self.assertEqual([g['id'] for g in r['guests']],[1],query);self.assertEqual(r['guests'][0]['visits'],1);self.assertEqual(r['guests'][0]['last_visit'],'2020-01-01')
        for query in ('900456',"%' OR 1=1 --"):
            self.assertEqual(self.client.get('/api/bookings/guests',query_string={'search':query}).json['guests'],[])
    def test_search_beyond_first_page(self):
        for i in range(10,70):
            self.db.execute("INSERT INTO guests(id,organisation_id,name,created_at,updated_at) VALUES(?,1,?,'s','s')",(i,'Historic '+str(i)));self.seed(i,1,i,None,day='2020-02-01',status='Completed')
        self.db.commit();r=self.client.get('/api/bookings/guests').json;self.assertTrue(r['has_more']);self.assertEqual(len(r['guests']),50)
        ids=[g['id'] for g in self.client.get('/api/bookings/guests?after='+str(r['next_after'])).json['guests']];self.assertIn(69,ids)
        self.assertEqual(self.client.get('/api/bookings/guests?search=Historic+69').json['guests'][0]['id'],69)
    def test_edits_validation_tenant_csrf_stale(self):
        self.assertEqual(self.client.patch('/api/bookings/1',json={'status':'Seated'}).status_code,403)
        self.assertEqual(self.patch(2,{'status':'Seated'}).status_code,404)
        self.assertEqual(self.patch(1,{'status':'Anything'}).status_code,400)
        self.assertEqual(self.patch(1,{'party_size':-1}).status_code,400)
        self.assertEqual(self.patch(1,{'table_id':2}).status_code,400)
        self.assertEqual(self.patch(1,{'status':'Seated','expected_updated_at':'old'}).status_code,409)
        r=self.patch(1,{'status':'Seated','expected_updated_at':'v1'});self.assertEqual(r.status_code,200);self.assertTrue(r.json['updated_at'])
        self.assertEqual(self.q('SELECT status FROM bookings WHERE id=1',one=True)['status'],'Seated')
        self.assertEqual(self.client.get('/api/bookings/guests',headers={'X-Booking-Site':'2'}).status_code,403)
    def test_overlap_capacity_cancel_and_overnight(self):
        self.seed(4,1,1,1,time='21:00');self.db.commit()
        self.assertEqual(self.patch(1,{'booking_time':'20:00'}).status_code,409)
        self.assertEqual(self.patch(1,{'party_size':9}).status_code,400)
        self.assertEqual(self.patch(4,{'status':'Cancelled'}).status_code,200)
        self.assertEqual(self.patch(1,{'booking_time':'20:00'}).status_code,200)
        self.assertEqual(self.patch(4,{'status':'Confirmed'}).status_code,409)
        self.db.execute("UPDATE bookings SET booking_date='2026-11-01',booking_time='23:00',duration_minutes=120 WHERE id=1");self.db.commit()
        self.assertEqual(self.patch(4,{'booking_date':'2026-11-02','booking_time':'00:30','status':'Confirmed'}).status_code,409)

    def test_malformed_guest_requests_and_page_bounds(self):
        for path in ('vip','comments'):
            self.assertEqual(self.client.post('/api/bookings/guests/1/'+path,json=['bad'],headers=self.headers()).status_code,400)
        for page in ('-1','oops','999999999999999999999'):
            self.assertEqual(self.client.get('/api/bookings/guests?after='+page).status_code,400)
        self.blocked=True
        self.assertEqual(self.profile().status_code,403)
        self.blocked=False
        self.auth(1)
        self.db.execute('UPDATE sites SET active=0 WHERE id=1');self.db.commit()
        self.assertEqual(self.profile().status_code,403)
    def test_retention_selects_only_current_site_with_consent(self):
        from datetime import date,timedelta
        tree=ast.parse((ROOT/'app.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='send_retention_campaign');fn.decorator_list=[]
        self.db.execute("UPDATE bookings SET status='Completed',booking_date='2025-01-01'")
        self.db.execute('UPDATE guests SET marketing_consent=1');self.db.commit()
        recipients=[]
        env=dict(self.env,date=date,timedelta=timedelta,jsonify=jsonify,booking_settings_for=lambda *a:{},send_alport_email=lambda address,*a:recipients.append(address) or {'ok':False})
        self.db.execute("UPDATE guests SET email='one@example.test' WHERE id=1");self.db.execute("UPDATE guests SET email='two@example.test' WHERE id=2");self.db.commit()
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'retention','exec'),env)
        with self.app.test_request_context('/'):
            session.update(user_id=1,site_id=1);result=env['send_retention_campaign']();self.assertEqual(result.json['eligible'],1)
        self.assertEqual(recipients,['one@example.test'])

def load_tests(loader,tests,pattern):return unittest.TestSuite(DiaryTests(n) for n in DiaryTests.__dict__ if n.startswith('test_'))
