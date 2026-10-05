"""Employee portal, explicit access grants and reviewed leave. No payroll adjustments."""
import hashlib
import hmac
import secrets
import json
import inspect
import math
from datetime import date, datetime, timedelta, timezone
from flask import Blueprint, request, session, jsonify, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_staff_access(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 employee_id BIGINT NOT NULL UNIQUE REFERENCES employees(id), user_id BIGINT NOT NULL REFERENCES users(id),
 profile TEXT NOT NULL CHECK(profile IN ('staff','orders','full','disabled')),
 invite_hash TEXT UNIQUE, invite_expires TEXT, activated_at TEXT, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_leave_requests(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 employee_id BIGINT NOT NULL REFERENCES employees(id), start_date TEXT NOT NULL, end_date TEXT NOT NULL,
 leave_type TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Pending',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, decided_by BIGINT, decided_at TEXT,
 decision_note TEXT NOT NULL DEFAULT '', request_key TEXT NOT NULL,
 UNIQUE(employee_id,request_key));
CREATE TABLE IF NOT EXISTS alport_staff_events(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 employee_id BIGINT NOT NULL, actor_id BIGINT NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_staff_order_requests(
 user_id BIGINT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
 order_id BIGINT NOT NULL REFERENCES alport_purchase_orders(id), PRIMARY KEY(user_id,request_key));
CREATE TABLE IF NOT EXISTS alport_staff_login_limits(
 identity_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, blocked_until TEXT NOT NULL DEFAULT '');
'''
PEOPLE_ROLES = ('Owner','Admin','General Manager','Manager')

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def day(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError,TypeError):
        raise ValueError('Use a valid calendar date.') from None

def text(d,key,limit=1000):
    value=str(d.get(key) or '').strip()
    if len(value)>limit:raise ValueError('Text is too long: '+key)
    return value

def register_staff(app,env):
    conn,q=env['conn'],env['q']
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000002,))
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    with conn() as c:
        c.execute('ALTER TABLE alport_staff_access DROP CONSTRAINT IF EXISTS alport_staff_access_user_id_key')
        c.execute('ALTER TABLE alport_staff_access ADD COLUMN IF NOT EXISTS imported_login INTEGER NOT NULL DEFAULT 0')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS alport_staff_user_site ON alport_staff_access(user_id,site_id)')
    bp=Blueprint('alport_staff',__name__)
    def access(u=None):
        u=u or env['user']()
        if not u:return None
        chosen=session.get('staff_site_id',session.get('site_id')) if session.get('user_id')==u['id'] else None
        rows=q('SELECT a.*,e.active AS employee_active,s.active AS site_active FROM alport_staff_access a JOIN employees e ON e.id=a.employee_id JOIN sites s ON s.id=a.site_id WHERE a.user_id=? AND a.organisation_id=? ORDER BY a.id',(u['id'],u['organisation_id']))
        return next((a for a in rows if a['site_id']==chosen),None) or next((a for a in rows if a['profile']!='disabled' and a['employee_active']==1 and a['site_active']==1 and a['activated_at']),None) or (rows[0] if rows else None)
    def live(a):
        return a and a['profile']!='disabled' and a['employee_active']==1 and a['site_active']==1 and bool(a['activated_at'])
    def csrf():
        session.setdefault('staff_csrf',secrets.token_urlsafe(32))
        return session['staff_csrf']
    def event(c,u,e,kind,payload):
        c.execute('''INSERT INTO alport_staff_events(organisation_id,site_id,employee_id,actor_id,event_type,payload,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',(e['organisation_id'],e['site_id'],e.get('employee_id',e.get('id')),u['id'],kind,json.dumps(payload),stamp()))
        if app.extensions.get('alport_staff_notify'):app.extensions['alport_staff_notify'](c,u,e,kind,payload)
    def lock(c,org):
        c.execute('SELECT pg_advisory_xact_lock(%s)',(720000000000+int(org),))
    def manager():
        u,s=env['user'](),env['current_site']()
        if not u or not s or u['role'] not in PEOPLE_ROLES or (access(u) and access(u)['profile']!='full'):raise PermissionError('People manager access required.')
        return u,s
    def employee(c,eid,u,s):
        e=c.execute('SELECT * FROM employees WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1',(eid,u['organisation_id'],s['id'])).fetchone()
        if not e:raise ValueError('Active employee not found at this venue.')
        return dict(e)
    def own():
        u=env['user']();a=access(u)
        if not u or not live(a):raise PermissionError('Your manager must activate your staff access.')
        return u,a
    def overlapping(c,org,sid,eid,start,end):
        prior=(date.fromisoformat(start)-timedelta(days=1)).isoformat()
        return c.execute("""SELECT id,shift_date,start_time,end_time,status FROM shifts
            WHERE organisation_id=%s AND site_id=%s AND employee_id=%s AND shift_date<=%s
            AND (shift_date>=%s OR (shift_date=%s AND end_time<=start_time AND end_time>'00:00'))
            AND status NOT IN ('Cancelled','Declined') ORDER BY shift_date""",(org,sid,eid,end,start,prior)).fetchall()
    def invite(c,a):
        token=secrets.token_urlsafe(32)
        c.execute('UPDATE alport_staff_access SET invite_hash=%s,invite_expires=%s WHERE id=%s',
            (hashlib.sha256(token.encode()).hexdigest(),(datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(),a['id']))
        return env['_public_base_url']().rstrip('/')+'/staff/activate/'+token
    eho_endpoints={'eho_overview','save_eho_daily_check','get_eho_daily_checks','save_eho_temperature',
        'get_eho_temperatures','save_eho_record','get_eho_records','resolve_eho_record','verify_eho_record',
        'save_four_week_review','eho_audit_pack'}
    # Fail closed for staff on every legacy/new route, not merely on navigation links.
    @app.before_request
    def restrict_staff():
        if request.endpoint in ('static','alport_people.service_worker','alport_passkeys.config','alport_passkeys.auth_options','alport_passkeys.auth_verify'):return None
        u=env['user']()
        if not u:return None
        a=access(u)
        if a and (not live(a) or session.get('staff_auth_hash')!=hashlib.sha256(u['password_hash'].encode()).hexdigest()):
            if request.endpoint not in ('logout','alport_staff.activate','alport_staff.login','alport_people.sites','alport_people.select_site'):
                return jsonify(error='Staff access is inactive. Contact your manager.'),403
        limited=(a and a['profile'] in ('staff','orders')) or u['role'] in ('Staff','User')
        if not limited:return None
        if a:session['site_id']=a['site_id']
        allowed={'logout','privacy_policy','alport_staff.login','alport_staff.activate',
                 'alport_staff.portal','alport_staff.me','alport_staff.request_leave','alport_staff.withdraw',
                 'alport_staff.orders','alport_staff.order_detail','alport_staff.new_order','alport_staff.password','alport_staff.compliance','alport_staff.training','staff_onboarding_portal','staff_onboarding_details','staff_training_submit'} | eho_endpoints
        if request.endpoint=='app_home':return redirect('/staff')
        if request.blueprint in ('alport_people','alport_passkeys'):allowed.add(request.endpoint)
        if request.endpoint not in allowed:return jsonify(error='This account has personal staff and compliance access only.'),403
        if request.endpoint in ('staff_onboarding_portal','staff_onboarding_details','staff_training_submit'):
            if not live(a) or not q('SELECT id FROM employee_onboarding WHERE token=? AND employee_id=? AND organisation_id=? AND site_id=?',(request.view_args.get('token'),a['employee_id'],a['organisation_id'],a['site_id']),True):return jsonify(error='This onboarding invitation does not belong to your account.'),403
        if request.endpoint in eho_endpoints:
            if not live(a):return jsonify(error='An active employee link is required for compliance access.'),403
            if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):return jsonify(error='Business subscription needs attention.'),403
            if request.content_length and request.content_length>30000:return jsonify(error='Request too large.'),413
            if request.method=='POST':
                if not session.get('staff_csrf') or not hmac.compare_digest(session['staff_csrf'],request.headers.get('X-Staff-CSRF','')):return jsonify(error='Reload the compliance page.'),403
                d=request.get_json(silent=True)
                if not isinstance(d,dict):return jsonify(error='Send valid input fields.'),400
                if request.endpoint in ('resolve_eho_record','verify_eho_record'):
                    row=q('SELECT category FROM eho_records WHERE id=? AND organisation_id=? AND site_id=?',(request.view_args['record_id'],a['organisation_id'],a['site_id']),True)
                    if row and row['category']=='Fit to work':return jsonify(error='Private fit-to-work records require a manager.'),403
                if request.endpoint=='save_eho_temperature':
                    try:
                        for key in ('temperature','target_min','target_max'):
                            if d.get(key) not in (None,'') and not math.isfinite(float(d[key])):raise ValueError()
                    except (ValueError,TypeError):return jsonify(error='Use finite temperature values.'),400
    @bp.before_request
    def protect():
        if request.content_length and request.content_length>30000:return jsonify(error='Request too large.'),413
        public=request.endpoint in ('alport_staff.login','alport_staff.activate')
        if not public:
            u=env['user']()
            if not u:return redirect('/staff/login') if request.method=='GET' and not request.path.startswith('/api/') else (jsonify(error='Sign in first.'),401)
            sub=env['subscription_for'](u['organisation_id'])
            if env['subscription_blocks_access'](sub):return jsonify(error='The business subscription needs attention. Contact your manager.'),403
        if request.method=='POST':
            supplied=request.form.get('csrf','') if public else request.headers.get('X-Staff-CSRF','')
            if not session.get('staff_csrf') or not hmac.compare_digest(session['staff_csrf'],supplied):return jsonify(error='Reload this page before trying again.'),403
            if not public and not isinstance(request.get_json(silent=True),dict):return jsonify(error='Send valid input fields.'),400
    @bp.errorhandler(ValueError)
    def invalid(e):return jsonify(error=str(e)),400
    @bp.errorhandler(PermissionError)
    def forbidden(e):return jsonify(error=str(e)),403
    @bp.get('/staff/compliance')
    def compliance():
        u,a=own();session['site_id']=a['site_id']
        return render_template('staff_compliance.html',site=q('SELECT name FROM sites WHERE id=?',(a['site_id'],),True),csrf=csrf())
    @bp.get('/api/staff/training-register')
    def training():
        u,a=own()
        rows=q('''SELECT e.id,e.name,e.training_status,
            COUNT(t.id) AS total_modules,SUM(CASE WHEN t.status='Complete' THEN 1 ELSE 0 END) AS complete_modules
            FROM employees e LEFT JOIN employee_training t ON t.employee_id=e.id AND t.organisation_id=e.organisation_id AND t.site_id=e.site_id
            WHERE e.organisation_id=? AND e.site_id=? AND e.active=1 GROUP BY e.id,e.name,e.training_status ORDER BY e.name''',(a['organisation_id'],a['site_id']))
        return jsonify(employees=rows)
    @bp.get('/staff')
    def portal():
        own();return render_template('staff_portal.html',mode='staff',csrf=csrf())
    @bp.get('/staff-management')
    def management():
        manager();return render_template('staff_portal.html',mode='manager',csrf=csrf())
    @bp.route('/staff/login',methods=['GET','POST'])
    def login():
        error=None
        if request.method=='POST':
            email=text(request.form,'email',254).lower();orgid=text(request.form,'organisation',20)
            identity=hashlib.sha256((orgid+'|'+email).encode()).hexdigest()
            with conn() as c:
                # Serialize authentication attempts per identity across workers.
                c.execute('SELECT pg_advisory_xact_lock(%s)',(int(identity[:14],16),))
                lim=c.execute('SELECT * FROM alport_staff_login_limits WHERE identity_hash=%s',(identity,)).fetchone()
                if lim and lim['blocked_until']>stamp():error='Too many attempts. Try again in 15 minutes.'
                else:
                    u=c.execute('SELECT * FROM users WHERE organisation_id=%s AND lower(email)=%s AND active=1',(int(orgid) if orgid.isdigit() else -1,email)).fetchone()
                    a=access(dict(u)) if u else None
                    if u and live(a) and check_password_hash(u['password_hash'],request.form.get('password','')):
                        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):error='Your business subscription needs attention.'
                        else:
                            c.execute('DELETE FROM alport_staff_login_limits WHERE identity_hash=%s',(identity,))
                            session.clear();session.update(user_id=u['id'],site_id=a['site_id'],staff_site_id=a['site_id'],staff_auth_hash=hashlib.sha256(u['password_hash'].encode()).hexdigest())
                            return redirect('/staff')
                    else:
                        n=(lim['failures'] if lim and not lim['blocked_until'] else 0)+1
                        until=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(timespec='seconds') if n>=5 else ''
                        c.execute('''INSERT INTO alport_staff_login_limits(identity_hash,failures,blocked_until) VALUES(%s,%s,%s)
                            ON CONFLICT(identity_hash) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until''',(identity,n,until))
                        error='Sign-in details are incorrect or staff access is inactive.'
        return render_template('staff_auth.html',activate=False,csrf=csrf(),error=error,organisation=request.args.get('organisation',''))
    @bp.route('/staff/activate/<token>',methods=['GET','POST'])
    def activate(token):
        hashed=hashlib.sha256(token.encode()).hexdigest()
        a=q('SELECT * FROM alport_staff_access WHERE invite_hash=? AND invite_expires>? AND profile<>?',(hashed,stamp(),'disabled'),True)
        if not a:return 'This invitation has expired or was used. Ask your manager for a new invitation.',400
        error=None
        if request.method=='POST':
            password=request.form.get('password','')
            if len(password)<12 or len(password)>128 or password!=request.form.get('confirm'):error='Use 12–128 characters and enter the same password twice.'
            else:
                with conn() as c:
                    lock(c,a['organisation_id'])
                    current=c.execute('SELECT * FROM alport_staff_access WHERE id=%s FOR UPDATE',(a['id'],)).fetchone()
                    e=c.execute('SELECT active FROM employees WHERE id=%s',(a['employee_id'],)).fetchone()
                    if not current or current['invite_hash']!=hashed or current['invite_expires']<=stamp() or current['profile']=='disabled' or not e or e['active']!=1:raise ValueError('Invitation is no longer available.')
                    c.execute('UPDATE users SET password_hash=%s WHERE id=%s',(generate_password_hash(password),a['user_id']))
                    c.execute('UPDATE alport_staff_access SET activated_at=%s,invite_hash=NULL,invite_expires=NULL,updated_at=%s,version=version+1 WHERE id=%s',(stamp(),stamp(),a['id']))
                    event(c,{'id':a['user_id']},a,'Activated',{})
                session.clear()
                return redirect('/staff/login?organisation='+str(a['organisation_id']))
        response=app.make_response(render_template('staff_auth.html',activate=True,csrf=csrf(),error=error,organisation=a['organisation_id']))
        response.headers['Referrer-Policy']='no-referrer';response.headers['Cache-Control']='no-store';return response
    def leave_conflicts(c,u,s,r):
        candidates=overlapping(c,u['organisation_id'],s['id'],r['employee_id'],r['start_date'],r['end_date'])
        if app.extensions.get('alport_leave_overlap'):
            return [x for x in candidates if app.extensions['alport_leave_overlap'](dict(r),dict(x))]
        return candidates
    @bp.get('/api/staff-access/token')
    def access_token():
        manager();return jsonify(csrf=csrf())
    @bp.get('/api/staff-access')
    def people():
        u,s=manager()
        rows=q('''SELECT e.id,e.name,e.email,e.department,a.profile,a.version,a.activated_at,a.user_id
            FROM employees e LEFT JOIN alport_staff_access a ON a.employee_id=e.id
            WHERE e.organisation_id=? AND e.site_id=? AND e.active=1 ORDER BY e.name''',(u['organisation_id'],s['id']))
        leave=q('''SELECT l.*,e.name FROM alport_leave_requests l JOIN employees e ON e.id=l.employee_id
            WHERE l.organisation_id=? AND l.site_id=? ORDER BY CASE WHEN l.status='Pending' THEN 0 ELSE 1 END,l.id DESC LIMIT 300''',(u['organisation_id'],s['id']))
        with conn() as c:
            for r in leave:r['conflicts']=leave_conflicts(c,u,s,r)
        return jsonify(employees=rows,leave=leave,can_grant_full=u['role'] in ('Owner','Admin'),site=s['name'])
    @bp.post('/api/staff-access/<int:eid>')
    def grant(eid):
        u,s=manager();d=request.get_json();profile=d.get('profile')
        if profile not in ('staff','orders','full','disabled'):raise ValueError('Select a valid access level.')
        with conn() as c:
            lock(c,u['organisation_id']);e=employee(c,eid,u,s)
            a=c.execute('SELECT * FROM alport_staff_access WHERE employee_id=%s',(eid,)).fetchone()
            if (profile=='full' or (a and a['profile']=='full')) and u['role'] not in ('Owner','Admin'):raise PermissionError('Only Owner or Admin can change full management access.')
            if a and a['user_id']==u['id']:raise PermissionError('Ask another administrator to change your access.')
            if int(d.get('version') or 0)!=(a['version'] if a else 0):raise ValueError('Access changed. Refresh before saving.')
            if not a:
                if d.get('existing_user_id'):
                    if u['role'] not in ('Owner','Admin') or d.get('link_confirmed') is not True:raise PermissionError('Owner/Admin must explicitly confirm linking an existing login.')
                    target=c.execute('SELECT * FROM users WHERE id=%s AND organisation_id=%s AND active=1',(d['existing_user_id'],u['organisation_id'])).fetchone()
                    if not target or str(target['email']).strip().lower()!=str(e['email']).strip().lower():raise ValueError('Select the active login with this employee’s exact email in this business.')
                    if profile=='disabled':raise ValueError('Choose active access when linking an existing login.')
                    if target['role'] in ('Owner','Admin') and profile!='full':raise ValueError('Owner/Admin accounts must retain full access; change those roles through account administration.')
                    if c.execute('SELECT id FROM alport_staff_access WHERE user_id=%s AND site_id=%s',(target['id'],s['id'])).fetchone():raise ValueError('This account already has an employee record at this venue.')
                    aid=c.execute('INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,activated_at,imported_login,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s) RETURNING id',(u['organisation_id'],s['id'],eid,target['id'],profile,stamp(),stamp(),stamp())).fetchone()['id']
                    a=c.execute('SELECT * FROM alport_staff_access WHERE id=%s',(aid,)).fetchone()
                    if target['id']==u['id']:session.update(staff_site_id=s['id'],staff_auth_hash=hashlib.sha256(target['password_hash'].encode()).hexdigest())
                    event(c,u,e,'Existing login linked',{'user_id':target['id'],'profile':profile})
            if not a:
                if profile=='disabled':raise ValueError('No portal account exists for this employee.')
                email=str(e.get('email') or '').strip().lower()
                if '@' not in email or len(email)>254:raise ValueError('Add a valid, individual employee email first.')
                if c.execute('SELECT id FROM users WHERE organisation_id=%s AND lower(email)=%s',(u['organisation_id'],email)).fetchone():raise ValueError('This email already has a business login. Do not create a duplicate; existing-account linking needs administrator migration.')
                uid=c.execute('''INSERT INTO users(organisation_id,name,email,password_hash,role,active,created_at) VALUES(%s,%s,%s,%s,%s,1,%s) RETURNING id''',
                    (u['organisation_id'],e['name'],email,generate_password_hash(secrets.token_urlsafe(48)),'Staff',stamp())).fetchone()['id']
                aid=c.execute('''INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,created_at,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(u['organisation_id'],s['id'],eid,uid,profile,stamp(),stamp())).fetchone()['id']
                a=c.execute('SELECT * FROM alport_staff_access WHERE id=%s',(aid,)).fetchone()
            else:
                c.execute('UPDATE alport_staff_access SET profile=%s,updated_at=%s,version=version+1,invite_hash=NULL,invite_expires=NULL WHERE id=%s',(profile,stamp(),a['id']))
                a=c.execute('SELECT * FROM alport_staff_access WHERE id=%s',(a['id'],)).fetchone()
            target=c.execute('SELECT role FROM users WHERE id=%s',(a['user_id'],)).fetchone()
            if target['role'] in ('Owner','Admin') and profile!='full':raise ValueError('Owner/Admin accounts must retain their existing role.')
            if target['role'] not in ('Owner','Admin'):
                full=c.execute("SELECT id FROM alport_staff_access WHERE user_id=%s AND profile='full'",(a['user_id'],)).fetchone()
                c.execute('UPDATE users SET role=%s WHERE id=%s',('Manager' if full else 'Staff',a['user_id']))
            if a.get('imported_login') and d.get('reissue') is True:raise ValueError('This is a linked existing login. Use its existing password or the account’s own password recovery procedure.')
            link=invite(c,a) if profile!='disabled' and (not a['activated_at'] or d.get('reissue') is True) else None
            event(c,u,e,'Access changed',{'profile':profile,'invitation_created':bool(link)})
        return jsonify(ok=True,invite_url=link,organisation=u['organisation_id'])
    @bp.get('/api/staff/me')
    def me():
        u,a=own()
        e=q('SELECT name,department,job_title,holiday_allowance FROM employees WHERE id=?',(a['employee_id'],),True)
        start=(date.today()-timedelta(days=31)).isoformat();end=(date.today()+timedelta(days=366)).isoformat()
        shifts=q('''SELECT id,shift_date,start_time,end_time,break_minutes,status FROM shifts
            WHERE employee_id=? AND organisation_id=? AND site_id=? AND shift_date>=? AND shift_date<=? ORDER BY shift_date,start_time''',(a['employee_id'],a['organisation_id'],a['site_id'],start,end))
        leave=q('SELECT id,start_date,end_date,leave_type,note,status,version,decision_note,units,leave_start,leave_end FROM alport_leave_requests WHERE employee_id=? AND organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 200',(a['employee_id'],a['organisation_id'],a['site_id']))
        onboarding=q('SELECT token FROM employee_onboarding WHERE employee_id=? AND organisation_id=? AND site_id=?',(a['employee_id'],a['organisation_id'],a['site_id']),True)
        return jsonify(onboarding_url='/staff-onboarding/'+onboarding['token'] if onboarding else None,employee=e,shifts=shifts,leave=leave,access=a['profile'],site=q('SELECT name FROM sites WHERE id=?',(a['site_id'],),True)['name'])
    @bp.post('/api/staff/password')
    def password():
        u,a=own();d=request.get_json();new=str(d.get('password') or '')
        if not check_password_hash(u['password_hash'],str(d.get('current') or '')):raise ValueError('Current password is incorrect.')
        if not 12<=len(new)<=128:raise ValueError('Use a password of 12–128 characters.')
        with conn() as c:
            c.execute('UPDATE users SET password_hash=%s WHERE id=%s',(generate_password_hash(new),u['id']))
            event(c,u,a,'Password changed',{})
        session.clear();return jsonify(ok=True)
    @bp.post('/api/staff/leave')
    def request_leave():
        if app.extensions.get('alport_leave_request'):return app.extensions['alport_leave_request']()
        u,a=own();d=request.get_json();start,end=day(d.get('start_date')),day(d.get('end_date'))
        if start<date.today().isoformat() or end<start or (date.fromisoformat(end)-date.fromisoformat(start)).days>365:raise ValueError('Choose future dates, in order, covering no more than one year.')
        kind=d.get('leave_type');note=text(d,'note');key=text(d,'request_key',100)
        if kind not in ('Holiday','Unpaid leave','Other'):raise ValueError('Choose a time-off type.')
        if len(key)<16:raise ValueError('Reload to create a request key.')
        with conn() as c:
            lock(c,u['organisation_id'])
            old=c.execute('SELECT * FROM alport_leave_requests WHERE employee_id=%s AND request_key=%s',(a['employee_id'],key)).fetchone()
            if old:
                if (old['start_date'],old['end_date'],old['leave_type'],old['note'])!=(start,end,kind,note):raise ValueError('Retry key belongs to another request.')
                return jsonify(id=old['id'],replayed=True)
            if c.execute('''SELECT id FROM alport_leave_requests WHERE employee_id=%s AND status IN ('Pending','Approved') AND start_date<=%s AND end_date>=%s''',(a['employee_id'],end,start)).fetchone():raise ValueError('These dates overlap an existing pending or approved request.')
            rid=c.execute('''INSERT INTO alport_leave_requests(organisation_id,site_id,employee_id,start_date,end_date,leave_type,note,created_at,request_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(a['organisation_id'],a['site_id'],a['employee_id'],start,end,kind,note,stamp(),key)).fetchone()['id']
            event(c,u,a,'Leave requested',{'request_id':rid})
        return jsonify(id=rid)
    @bp.post('/api/staff/leave/<int:rid>/withdraw')
    def withdraw(rid):
        u,a=own();d=request.get_json()
        with conn() as c:
            lock(c,u['organisation_id'])
            r=c.execute('SELECT * FROM alport_leave_requests WHERE id=%s AND employee_id=%s AND organisation_id=%s AND site_id=%s',(rid,a['employee_id'],a['organisation_id'],a['site_id'])).fetchone()
            if not r or r['status']!='Pending' or r['version']!=d.get('version'):raise ValueError('Only your unchanged pending request can be withdrawn.')
            c.execute("UPDATE alport_leave_requests SET status='Withdrawn',version=version+1 WHERE id=%s",(rid,));event(c,u,a,'Leave withdrawn',{'request_id':rid})
        return jsonify(ok=True)
    @bp.post('/api/staff-access/leave/<int:rid>')
    def decide(rid):
        u,s=manager();d=request.get_json();status=d.get('status');note=text(d,'note')
        if status not in ('Approved','Declined','Cancelled') or not note:raise ValueError('Choose a decision and enter a manager note.')
        with conn() as c:
            lock(c,u['organisation_id'])
            r=c.execute('SELECT * FROM alport_leave_requests WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,u['organisation_id'],s['id'])).fetchone()
            if not r or r['version']!=d.get('version'):raise ValueError('Request changed or is unavailable. Refresh first.')
            a=access(u)
            if a and a['employee_id']==r['employee_id']:raise PermissionError('Another manager must decide your own request.')
            if (status=='Cancelled' and r['status']!='Approved') or (status!='Cancelled' and r['status']!='Pending'):raise ValueError('This decision is not available for the current status.')
            conflicts=leave_conflicts(c,u,s,r)
            if status=='Approved' and conflicts and d.get('conflicts_acknowledged') is not True:raise ValueError('Existing shifts overlap. Confirm you will arrange cover and update the rota.')
            if app.extensions.get('alport_leave_decision'):app.extensions['alport_leave_decision'](c,dict(r),status)
            c.execute('UPDATE alport_leave_requests SET status=%s,decided_by=%s,decided_at=%s,decision_note=%s,version=version+1 WHERE id=%s',(status,u['id'],stamp(),note,rid))
            event(c,u,dict(r),'Leave '+status.lower(),{'request_id':rid,'note':note,'conflicting_shift_ids':[x['id'] for x in conflicts]})
        return jsonify(ok=True)
    @bp.post('/api/staff-access/leave/<int:rid>/cancel-shifts')
    def cancel_leave_shifts(rid):
        u,s=manager();d=request.get_json();note=text(d,'note')
        if not note:raise ValueError('Record how cover will be arranged before cancelling shifts.')
        with conn() as c:
            lock(c,u['organisation_id'])
            r=c.execute('SELECT * FROM alport_leave_requests WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,u['organisation_id'],s['id'])).fetchone()
            if not r or r['status']!='Approved' or r['version']!=d.get('version'):raise ValueError('Refresh the approved request first.')
            a=access(u)
            if a and a['employee_id']==r['employee_id']:raise PermissionError('Another manager must update your shifts for approved leave.')
            rows=[x for x in leave_conflicts(c,u,s,r) if x['status']=='Scheduled' and x['shift_date']>=date.today().isoformat()]
            for shift in rows:c.execute("UPDATE shifts SET status='Cancelled' WHERE id=%s",(shift['id'],))
            event(c,u,dict(r),'Leave shifts cancelled',{'request_id':rid,'shift_ids':[x['id'] for x in rows],'note':note})
            c.execute('UPDATE alport_leave_requests SET version=version+1 WHERE id=%s',(rid,))
        return jsonify(ok=True,cancelled=len(rows))
    def order_access():
        u,a=own()
        if a['profile'] not in ('orders','full'):raise PermissionError('Your manager has not enabled supplier orders.')
        return u,a
    @bp.get('/api/staff/orders')
    def orders():
        u,a=order_access()
        rows=q('''SELECT id,supplier_name,status,expected_date,created_at FROM alport_purchase_orders
            WHERE organisation_id=? AND site_id=? AND created_by=? ORDER BY id DESC LIMIT 200''',(a['organisation_id'],a['site_id'],u['id']))
        products=q('''SELECT p.id,p.supplier_id,p.product_name,p.pack_quantity,p.pack_unit,p.pack_price,su.name AS supplier_name
            FROM alport_supplier_products p JOIN stock_items st ON st.id=p.stock_item_id JOIN suppliers su ON su.id=p.supplier_id
            WHERE p.organisation_id=? AND p.site_id=? AND st.active=1 AND su.active=1 ORDER BY su.name,p.product_name''',(a['organisation_id'],a['site_id']))
        return jsonify(orders=rows,products=products)
    @bp.get('/api/staff/orders/<int:oid>')
    def order_detail(oid):
        u,a=order_access()
        r=q('''SELECT id,supplier_name,status,expected_date,reference,note FROM alport_purchase_orders
            WHERE id=? AND organisation_id=? AND site_id=? AND created_by=?''',(oid,a['organisation_id'],a['site_id'],u['id']),True)
        if not r:return jsonify(error='Order not found.'),404
        r['lines']=q('SELECT product_name,packs,received_packs,pack_quantity,pack_unit,pack_price FROM alport_purchase_lines WHERE order_id=?',(oid,))
        return jsonify(r)
    @bp.post('/api/staff/orders')
    def new_order():
        u,a=order_access();d=request.get_json()
        create=app.extensions.get('alport_purchase_create')
        if not create:raise ValueError('Install the matching purchasing module first.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(a['site_id']),))
            key=text(d,'request_key',100)
            if len(key)<16:raise ValueError('Reload to create an order request key.')
            fingerprint=hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            old=c.execute('SELECT * FROM alport_staff_order_requests WHERE user_id=%s AND request_key=%s',(u['id'],key)).fetchone()
            if old:
                if old['payload_hash']!=fingerprint:raise ValueError('This retry key was already used with different order details. Refresh to start another draft.')
                return jsonify(id=old['order_id'],replayed=True)
            oid=create(c,u,a['organisation_id'],a['site_id'],d)
            c.execute('INSERT INTO alport_staff_order_requests(user_id,request_key,payload_hash,order_id) VALUES(%s,%s,%s,%s)',(u['id'],key,fingerprint,oid))
        return jsonify(id=oid)
    @bp.after_request
    def private(response):
        response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer';return response
    # Block new shifts on approved full-day leave. The existing rota remains the source of shift data.
    previous=app.view_functions.get('add_shift')
    if previous:
        def guarded_shift():
            u,s=env['user'](),env['current_site']()
            if not u or not s or u['role'] not in (*PEOPLE_ROLES,'Finance'):return previous()
            with conn() as c:
                lock(c,u['organisation_id'])
                d=request.get_json(silent=True) or {}
                try:
                    start=day(d.get('shift_date'));end=start
                    if str(d.get('end_time') or '')<str(d.get('start_time') or '') and str(d.get('end_time') or '')>'00:00':end=(date.fromisoformat(start)+timedelta(days=1)).isoformat()
                except ValueError:return jsonify(error='Choose a valid shift date.'),400
                conflicts=app.extensions.get('alport_shift_leave_conflicts')
                if conflicts and conflicts(c,u['organisation_id'],s['id'],d):return jsonify(error='This shift overlaps approved time off.'),409
                if not conflicts and c.execute("SELECT id FROM alport_leave_requests WHERE organisation_id=%s AND site_id=%s AND employee_id=%s AND status='Approved' AND start_date<=%s AND end_date>=%s",(u['organisation_id'],s['id'],d.get('employee_id'),end,start)).fetchone():return jsonify(error='This employee has approved time off during that shift. Review the leave before assigning it.'),409
                return previous()
        app.view_functions['add_shift']=guarded_shift
    # Staff are authorized for the complete operational EHO feature set only.
    # Unwrap manager decorators on these exact handlers, never change the user's role.
    for name in ('resolve_eho_record','verify_eho_record','save_four_week_review','eho_audit_pack'):
        original=app.view_functions.get(name)
        if not original:continue
        def operational(*args,_original=original,**kwargs):
            u=env['user']();a=access(u)
            if live(a) and a['profile'] in ('staff','orders'):
                return inspect.unwrap(_original)(*args,**kwargs)
            return _original(*args,**kwargs)
        app.view_functions[name]=operational
    @app.after_request
    def private_compliance(response):
        if request.endpoint in eho_endpoints:
            response.headers['Cache-Control']='no-store'
            u=env['user']();a=access(u)
            if a and a['profile'] in ('staff','orders') and response.is_json and response.status_code==200:
                data=response.get_json()
                for key in ('recent','records'):
                    if key in data:
                        data[key]=[r for r in data[key] if r.get('category')!='Fit to work']
                response.set_data(app.json.dumps(data))
        return response
    app.extensions.update(alport_staff_own=own,alport_staff_manager=manager,alport_staff_access=access,alport_staff_live=live,alport_staff_event=event,alport_staff_csrf=csrf)
    app.register_blueprint(bp)
