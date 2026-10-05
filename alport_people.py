"""Multi-venue staff, employer-configured leave, attendance, swaps and notifications."""
import json,secrets,hmac,hashlib,os,math
from datetime import datetime,date,timedelta,timezone,time
from zoneinfo import ZoneInfo
from flask import Blueprint,request,session,jsonify,render_template,Response
UK=ZoneInfo('Europe/London')
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_leave_policies(
 employee_id BIGINT NOT NULL REFERENCES employees(id),year_start TEXT NOT NULL,year_end TEXT NOT NULL,
 annual_hours NUMERIC(12,4) NOT NULL,carry_hours NUMERIC(12,4) NOT NULL,opening_hours NUMERIC(12,4) NOT NULL,
 daily_hours NUMERIC(8,4) NOT NULL,weekdays TEXT NOT NULL,accrual TEXT NOT NULL,hourly_rate NUMERIC(12,6) NOT NULL,
 note TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(employee_id,year_start));
CREATE TABLE IF NOT EXISTS alport_attendance(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,employee_id BIGINT NOT NULL,
 in_at TEXT NOT NULL,out_at TEXT,break_minutes INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'Open',
 approved_by BIGINT,approved_at TEXT,note TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS alport_one_open_clock ON alport_attendance(employee_id) WHERE out_at IS NULL;
CREATE TABLE IF NOT EXISTS alport_shift_swaps(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,shift_id BIGINT NOT NULL,
 from_employee BIGINT NOT NULL,to_employee BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'Offered',
 snapshot TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS alport_one_active_swap ON alport_shift_swaps(shift_id) WHERE status IN ('Offered','Accepted');
CREATE TABLE IF NOT EXISTS alport_notifications(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,user_id BIGINT NOT NULL,
 event_key TEXT NOT NULL,message TEXT NOT NULL,url TEXT NOT NULL,created_at TEXT NOT NULL,read_at TEXT,
 email_sent_at TEXT,push_sent_at TEXT,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,UNIQUE(user_id,event_key));
CREATE TABLE IF NOT EXISTS alport_notification_preferences(
 user_id BIGINT PRIMARY KEY,email_enabled INTEGER NOT NULL DEFAULT 0,push_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alport_push_subscriptions(
 id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,endpoint_hash TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,created_at TEXT NOT NULL);
'''
def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def numeric(v,label,low=0,high=100000):
 try:n=float(v)
 except (ValueError,TypeError):raise ValueError('Enter a valid '+label+'.') from None
 if not math.isfinite(n) or not low<=n<=high:raise ValueError('Invalid '+label+'.')
 return n
def bounds(r):
 start=r.get('leave_start') or r['start_date']+'T00:00:00'
 end=r.get('leave_end') or (date.fromisoformat(r['end_date'])+timedelta(days=1)).isoformat()+'T00:00:00'
 return datetime.fromisoformat(start).replace(tzinfo=UK),datetime.fromisoformat(end).replace(tzinfo=UK)
def shift_bounds(r):
 start=datetime.fromisoformat(r['shift_date']+'T'+r['start_time']).replace(tzinfo=UK);end=datetime.fromisoformat(r['shift_date']+'T'+r['end_time']).replace(tzinfo=UK)
 if end<=start:end+=timedelta(days=1)
 return start,end
def overlaps(a,b):return a[0]<b[1] and b[0]<a[1]
def policy_hours(p,start,end):
 days=json.loads(p['weekdays']) if isinstance(p['weekdays'],str) else p['weekdays']
 return round(sum(float(p['daily_hours']) for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() in days),4)
def register_people(app,env):
 conn,q=env['conn'],env['q'];own=app.extensions['alport_staff_own'];manager=app.extensions['alport_staff_manager'];access=app.extensions['alport_staff_access'];live=app.extensions['alport_staff_live'];event=app.extensions['alport_staff_event']
 with conn() as c:
  c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000002,))
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
  for sql in ['ALTER TABLE alport_leave_requests ADD COLUMN IF NOT EXISTS units NUMERIC(12,4)','ALTER TABLE alport_leave_requests ADD COLUMN IF NOT EXISTS leave_start TEXT','ALTER TABLE alport_leave_requests ADD COLUMN IF NOT EXISTS leave_end TEXT']:
   c.execute(sql)
 bp=Blueprint('alport_people',__name__)
 def lock(c,org):c.execute('SELECT pg_advisory_xact_lock(%s)',(720000000000+int(org),))
 def notify(c,org,sid,uid,key,message,url='/staff'):
  c.execute('''INSERT INTO alport_notifications(organisation_id,site_id,user_id,event_key,message,url,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,event_key) DO NOTHING''',(org,sid,uid,key,message,url,now()))
 def notify_managers(c,org,sid,key,message):
  for r in c.execute("SELECT id FROM users WHERE organisation_id=%s AND active=1 AND role IN ('Owner','Admin','General Manager','Manager')",(org,)).fetchall():notify(c,org,sid,r['id'],key,message,'/staff-management')
 def employee(c,eid,u,s):
  e=c.execute('SELECT * FROM employees WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1',(eid,u['organisation_id'],s['id'])).fetchone()
  if not e:raise ValueError('Employee not found at this venue.')
  return dict(e)
 def get_policy(c,eid,day):return c.execute('SELECT * FROM alport_leave_policies WHERE employee_id=%s AND year_start<=%s AND year_end>=%s',(eid,day,day)).fetchone()
 def balance(c,eid,p,exclude=None):
  today=datetime.now(UK).date();start=date.fromisoformat(p['year_start']);end=date.fromisoformat(p['year_end']);days=(end-start).days+1
  fraction=min(1,max(0,((today-start).days+1)/days))
  earned=float(p['annual_hours'])
  if p['accrual']=='daily':earned*=fraction
  if p['accrual']=='worked_hours':
   earned=0
   for r in c.execute("SELECT * FROM alport_attendance WHERE employee_id=%s AND status='Approved' AND in_at>=%s AND in_at<%s",(eid,p['year_start'],(end+timedelta(days=1)).isoformat())).fetchall():earned+=max(0,(datetime.fromisoformat(r['out_at'])-datetime.fromisoformat(r['in_at'])).total_seconds()/3600-r['break_minutes']/60)*float(p['hourly_rate'])
   earned=min(float(p['annual_hours']),earned)
  used=pending=0
  for r in c.execute("SELECT * FROM alport_leave_requests WHERE employee_id=%s AND leave_type='Holiday' AND status IN ('Pending','Approved') AND start_date<=%s AND end_date>=%s",(eid,p['year_end'],p['year_start'])).fetchall():
   if r['id']==exclude:continue
   units=float(r['units']) if r.get('units') is not None else policy_hours(p,max(start,date.fromisoformat(r['start_date'])),min(end,date.fromisoformat(r['end_date'])))
   if r['status']=='Approved':used+=units
   else:pending+=units
  allowance=earned+float(p['carry_hours'])+float(p['opening_hours'])
  return {'year_start':p['year_start'],'year_end':p['year_end'],'accrual':p['accrual'],'accrued_hours':round(allowance,2),'approved_hours':round(used,2),'pending_hours':round(pending,2),'available_hours':round(allowance-used,2),'after_pending_hours':round(allowance-used-pending,2),'note':p['note']}
 @bp.before_request
 def guard():
  if request.endpoint=='alport_people.service_worker':return None
  u=env['user']()
  if not u:return jsonify(error='Sign in first.'),401
  if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):return jsonify(error='Business subscription needs attention.'),403
  if request.content_length and request.content_length>30000:return jsonify(error='Request too large.'),413
  if request.method=='POST':
   if not session.get('staff_csrf') or not hmac.compare_digest(session['staff_csrf'],request.headers.get('X-Staff-CSRF','')):return jsonify(error='Reload the page.'),403
   if not isinstance(request.get_json(silent=True),dict):return jsonify(error='Send valid fields.'),400
 @bp.errorhandler(ValueError)
 def invalid(e):return jsonify(error=str(e)),400
 @bp.errorhandler(PermissionError)
 def denied(e):return jsonify(error=str(e)),403
 @bp.get('/people')
 def page():
  u=env['user']();a=access(u)
  if not live(a):manager()
  session.setdefault('staff_csrf',secrets.token_urlsafe(32));return render_template('people.html',csrf=session['staff_csrf'])
 @bp.get('/api/people/sites')
 def sites():
  u=env['user']()
  rows=q('''SELECT a.site_id,s.name,a.profile FROM alport_staff_access a JOIN sites s ON s.id=a.site_id JOIN employees e ON e.id=a.employee_id
   WHERE a.user_id=? AND a.organisation_id=? AND a.profile<>'disabled' AND a.activated_at IS NOT NULL AND s.active=1 AND e.active=1 ORDER BY s.name''',(u['id'],u['organisation_id']))
  return jsonify(sites=rows,selected=session.get('staff_site_id',session.get('site_id')))
 @bp.post('/api/people/sites')
 def select_site():
  u=env['user']();d=request.json
  a=q('''SELECT a.* FROM alport_staff_access a JOIN sites s ON s.id=a.site_id JOIN employees e ON e.id=a.employee_id WHERE a.user_id=? AND a.organisation_id=? AND a.site_id=? AND a.profile<>'disabled' AND a.activated_at IS NOT NULL AND e.active=1 AND s.active=1''',(u['id'],u['organisation_id'],d.get('site_id')),True)
  if not a:raise PermissionError('This venue is not assigned to your employee account.')
  session.update(staff_site_id=a['site_id'],site_id=a['site_id']);return jsonify(ok=True)
 @bp.get('/api/people')
 def overview():
  u=env['user']();a=access(u)
  can_manage=u['role'] in ('Owner','Admin','General Manager','Manager') and (not a or a['profile']=='full')
  if not live(a) and not can_manage:raise PermissionError('Staff account required.')
  s=env['current_site']();show_own=live(a) and a['site_id']==s['id'];result={'manager':can_manage,'owner':u['role'] in ('Owner','Admin'),'site':s['name'],'membership':dict(a) if show_own else None}
  if result['membership']:
   for key in ('invite_hash','invite_expires'):result['membership'].pop(key,None)
  result['notifications']=q('SELECT id,message,url,created_at,read_at FROM alport_notifications WHERE user_id=? AND organisation_id=? ORDER BY id DESC LIMIT 100',(u['id'],u['organisation_id']))
  result['preferences']=q('SELECT * FROM alport_notification_preferences WHERE user_id=?',(u['id'],),True) or {'email_enabled':0,'push_enabled':0}
  result['push_public_key']=os.getenv('VAPID_PUBLIC_KEY','');result['email_worker_enabled']=os.getenv('ALPORT_SEND_NOTIFICATIONS')=='1'
  if show_own:
   with conn() as c:
    p=get_policy(c,a['employee_id'],datetime.now(UK).date().isoformat());result['balance']=balance(c,a['employee_id'],p) if p else None
   result['attendance']=q('SELECT * FROM alport_attendance WHERE employee_id=? ORDER BY id DESC LIMIT 60',(a['employee_id'],))
   result['swaps']=q('SELECT * FROM alport_shift_swaps WHERE organisation_id=? AND site_id=? AND (from_employee=? OR to_employee=?) ORDER BY id DESC LIMIT 100',(u['organisation_id'],a['site_id'],a['employee_id'],a['employee_id']))
   result['colleagues']=q('''SELECT e.id,e.name FROM employees e JOIN alport_staff_access a ON a.employee_id=e.id WHERE e.organisation_id=? AND e.site_id=? AND e.active=1 AND a.profile<>'disabled' AND a.activated_at IS NOT NULL AND e.id<>? ORDER BY e.name''',(u['organisation_id'],a['site_id'],a['employee_id']))
   result['shifts']=q("SELECT id,shift_date,start_time,end_time FROM shifts WHERE employee_id=? AND organisation_id=? AND site_id=? AND status='Scheduled' AND shift_date>=? ORDER BY shift_date",(a['employee_id'],u['organisation_id'],a['site_id'],datetime.now(UK).date().isoformat()))
  if can_manage:
   result['employees']=q('SELECT id,name,email FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',(u['organisation_id'],s['id']))
   result['logins']=q('SELECT id,name,email,role FROM users WHERE organisation_id=? AND active=1 ORDER BY name',(u['organisation_id'],)) if result['owner'] else []
   result['review_attendance']=q("SELECT a.*,e.name FROM alport_attendance a JOIN employees e ON e.id=a.employee_id WHERE a.organisation_id=? AND a.site_id=? AND a.status IN ('Pending','Open') ORDER BY a.id DESC LIMIT 200",(u['organisation_id'],s['id']))
   result['review_swaps']=q("SELECT sw.*,e.name AS from_name,t.name AS to_name FROM alport_shift_swaps sw JOIN employees e ON e.id=sw.from_employee JOIN employees t ON t.id=sw.to_employee WHERE sw.organisation_id=? AND sw.site_id=? AND sw.status='Accepted' ORDER BY sw.id",(u['organisation_id'],s['id']))
   with conn() as c:
    result['balances']=[]
    for e in result['employees']:
     p=get_policy(c,e['id'],datetime.now(UK).date().isoformat());result['balances'].append({'employee':e,'balance':balance(c,e['id'],p) if p else None,'policy':dict(p) if p else None})
  return jsonify(result)
 @bp.post('/api/people/policy')
 def policy():
  u,s=manager();d=request.json
  try:start=date.fromisoformat(d['year_start']);end=date.fromisoformat(d['year_end'])
  except (ValueError,KeyError,TypeError):raise ValueError('Choose valid leave-year dates.') from None
  weekdays=d.get('weekdays');mode=d.get('accrual');note=str(d.get('note') or '').strip()
  if not 1<=(end-start).days<=366 or not isinstance(weekdays,list) or not weekdays or any(type(x)!=int or not 0<=x<=6 for x in weekdays) or mode not in ('upfront','daily','worked_hours') or not note:raise ValueError('Set the leave year, working weekdays, accrual method and employer policy note.')
  annual=numeric(d.get('annual_hours'),'annual hours');carry=numeric(d.get('carry_hours',0),'carry hours');opening=numeric(d.get('opening_hours',0),'opening adjustment',-100000);daily=numeric(d.get('daily_hours'),'hours per day',.01,24);rate=numeric(d.get('hourly_rate',0),'hourly accrual rate',0,1)
  if mode=='worked_hours' and rate==0:raise ValueError('Set an employer-confirmed accrual rate per approved worked hour.')
  with conn() as c:
   lock(c,u['organisation_id']);e=employee(c,d.get('employee_id'),u,s)
   other=c.execute('SELECT year_start FROM alport_leave_policies WHERE employee_id=%s AND year_start<>%s AND year_start<=%s AND year_end>=%s',(e['id'],start.isoformat(),end.isoformat(),start.isoformat())).fetchone()
   if other:raise ValueError('Leave years cannot overlap.')
   old=c.execute('SELECT version FROM alport_leave_policies WHERE employee_id=%s AND year_start=%s',(e['id'],start.isoformat())).fetchone()
   if (old['version'] if old else 0)!=d.get('version',0):raise ValueError('Policy changed. Refresh before saving.')
   c.execute('''INSERT INTO alport_leave_policies(employee_id,year_start,year_end,annual_hours,carry_hours,opening_hours,daily_hours,weekdays,accrual,hourly_rate,note)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(employee_id,year_start) DO UPDATE SET year_end=excluded.year_end,annual_hours=excluded.annual_hours,carry_hours=excluded.carry_hours,opening_hours=excluded.opening_hours,daily_hours=excluded.daily_hours,weekdays=excluded.weekdays,accrual=excluded.accrual,hourly_rate=excluded.hourly_rate,note=excluded.note,version=alport_leave_policies.version+1''',(e['id'],start.isoformat(),end.isoformat(),annual,carry,opening,daily,json.dumps(sorted(set(weekdays))),mode,rate,note[:1000]))
   event(c,u,e,'Leave policy saved',{'year_start':start.isoformat(),'annual_hours':annual,'accrual':mode,'note':note})
  return jsonify(ok=True)
 def request_leave():
  u,a=own();d=request.get_json();kind=d.get('leave_type');key=str(d.get('request_key') or '');note=str(d.get('note') or '').strip()
  if kind not in ('Holiday','Unpaid leave','Other') or not 16<=len(key)<=100 or len(note)>1000:raise ValueError('Choose a leave type and provide valid request details.')
  try:start=date.fromisoformat(d['start_date']);end=date.fromisoformat(d['end_date'])
  except (KeyError,ValueError,TypeError):raise ValueError('Choose valid dates.') from None
  if start<datetime.now(UK).date() or end<start or (end-start).days>365:raise ValueError('Choose future dates in order, up to a year.')
  partial=d.get('duration','full')
  beginning=datetime.combine(start,time.min,tzinfo=UK);ending=datetime.combine(end+timedelta(days=1),time.min,tzinfo=UK)
  with conn() as c:
   lock(c,u['organisation_id']);p=get_policy(c,a['employee_id'],start.isoformat());units=None
   if p and end.isoformat()>p['year_end']:raise ValueError('Split this request at the end of your leave year.')
   if partial!='full':
    if partial not in ('half','hours') or start!=end or not p:raise ValueError('Half-day/hour requests need one date and a configured leave policy.')
    try:beginning=datetime.combine(start,time.fromisoformat(d['start_time']),tzinfo=UK);ending=datetime.combine(end,time.fromisoformat(d['end_time']),tzinfo=UK)
    except (KeyError,ValueError,TypeError):raise ValueError('Enter the hours you will be away.') from None
    hours=(ending-beginning).total_seconds()/3600
    if not 0<hours<=24:raise ValueError('End time must be after the start time.')
    units=float(p['daily_hours'])/2 if partial=='half' else hours
    if partial=='half' and abs(hours-units)>.0001:raise ValueError('The half-day time interval must equal half your configured daily hours.')
   elif p:units=policy_hours(p,start,end)
   if kind=='Holiday' and p and (units is None or units<=0):raise ValueError('The request contains no configured working hours.')
   ls,le=beginning.replace(tzinfo=None).isoformat(),ending.replace(tzinfo=None).isoformat()
   old=c.execute('SELECT * FROM alport_leave_requests WHERE employee_id=%s AND request_key=%s',(a['employee_id'],key)).fetchone()
   if old:
    if (old['leave_start'],old['leave_end'],old['note'],old['leave_type'],float(old['units']) if old['units'] is not None else None)!=(ls,le,note,kind,units):raise ValueError('Retry details changed. Refresh first.')
    return jsonify(id=old['id'],replayed=True)
   for r in c.execute("SELECT * FROM alport_leave_requests WHERE employee_id=%s AND status IN ('Pending','Approved') AND start_date<=%s AND end_date>=%s",(a['employee_id'],end.isoformat(),start.isoformat())).fetchall():
    if overlaps((beginning,ending),bounds(dict(r))):raise ValueError('This overlaps another pending or approved request.')
   rid=c.execute('''INSERT INTO alport_leave_requests(organisation_id,site_id,employee_id,start_date,end_date,leave_type,note,created_at,request_key,units,leave_start,leave_end)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(a['organisation_id'],a['site_id'],a['employee_id'],start.isoformat(),end.isoformat(),kind,note,now(),key,units,ls,le)).fetchone()['id'];event(c,u,a,'Leave requested',{'request_id':rid,'units':units})
  return jsonify(id=rid)
 app.extensions['alport_leave_request']=request_leave
 def approval(c,r,status):
  if status=='Approved' and r['leave_type']=='Holiday':
   p=get_policy(c,r['employee_id'],r['start_date'])
   if not p:raise ValueError('Configure this employee’s leave policy before approving paid holiday.')
   if r['end_date']>p['year_end']:raise ValueError('Split requests across leave years before approval.')
   units=float(r['units']) if r.get('units') is not None else policy_hours(p,date.fromisoformat(r['start_date']),date.fromisoformat(r['end_date']))
   if units>balance(c,r['employee_id'],p,exclude=r['id'])['available_hours']+.0001:raise ValueError('Insufficient accrued allowance. Review the employer policy/opening adjustment before approval.')
   c.execute('UPDATE alport_leave_requests SET units=%s WHERE id=%s',(units,r['id']))
 app.extensions['alport_leave_decision']=approval
 def shift_conflicts(c,org,sid,d):
  proposed=shift_bounds(d);start=(proposed[0].date()-timedelta(days=1)).isoformat();end=proposed[1].date().isoformat()
  return any(overlaps(proposed,bounds(dict(r))) for r in c.execute("SELECT * FROM alport_leave_requests WHERE organisation_id=%s AND site_id=%s AND employee_id=%s AND status='Approved' AND start_date<=%s AND end_date>=%s",(org,sid,d.get('employee_id'),end,start)).fetchall())
 app.extensions['alport_shift_leave_conflicts']=shift_conflicts
 app.extensions['alport_leave_overlap']=lambda leave,shift:overlaps(bounds(leave),shift_bounds(shift))
 @bp.post('/api/people/clock/<action>')
 def clock(action):
  u,a=own();d=request.json
  with conn() as c:
   lock(c,u['organisation_id']);row=c.execute('SELECT * FROM alport_attendance WHERE employee_id=%s AND out_at IS NULL',(a['employee_id'],)).fetchone()
   if action=='in':
    elsewhere=c.execute('SELECT t.id FROM alport_attendance t JOIN alport_staff_access a ON a.employee_id=t.employee_id WHERE a.user_id=%s AND t.organisation_id=%s AND t.out_at IS NULL AND t.employee_id<>%s',(u['id'],u['organisation_id'],a['employee_id'])).fetchone()
    if elsewhere:raise ValueError('Clock out at your other venue before clocking in here.')
    if row:return jsonify(id=row['id'],replayed=True)
    rid=c.execute('INSERT INTO alport_attendance(organisation_id,site_id,employee_id,in_at) VALUES(%s,%s,%s,%s) RETURNING id',(u['organisation_id'],a['site_id'],a['employee_id'],now())).fetchone()['id'];event(c,u,a,'Clocked in',{'attendance_id':rid})
   elif action=='out':
    if not row:raise ValueError('There is no open clock-in at this venue.')
    minutes=numeric(d.get('break_minutes',0),'break minutes',0,1440)
    if minutes!=int(minutes):raise ValueError('Use whole break minutes.')
    duration=(datetime.now(timezone.utc)-datetime.fromisoformat(row['in_at'])).total_seconds()/60
    if minutes>duration:raise ValueError('Break exceeds the recorded time.')
    rid=row['id'];c.execute("UPDATE alport_attendance SET out_at=%s,break_minutes=%s,status='Pending',version=version+1 WHERE id=%s",(now(),int(minutes),rid));event(c,u,a,'Clocked out',{'attendance_id':rid})
   else:raise ValueError('Unknown clock action.')
  return jsonify(id=rid)
 @bp.post('/api/people/attendance/<int:rid>')
 def attendance(rid):
  u,s=manager();d=request.json;note=str(d.get('note') or '').strip();status=d.get('status')
  if status not in ('Approved','Rejected') or not note:raise ValueError('Choose a decision and note.')
  with conn() as c:
   lock(c,u['organisation_id']);r=c.execute('SELECT * FROM alport_attendance WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,u['organisation_id'],s['id'])).fetchone()
   if not r or r['version']!=d.get('version') or r['status'] not in ('Pending','Open'):raise ValueError('Attendance changed. Refresh first.')
   a=access(u)
   if a and a['employee_id']==r['employee_id']:raise PermissionError('Another manager must review your attendance.')
   start=d.get('in_at') or r['in_at'];end=d.get('out_at') or r['out_at']
   try:
    begin,finish=datetime.fromisoformat(start),datetime.fromisoformat(end)
    if not begin.tzinfo or not finish.tzinfo or finish<=begin or finish>datetime.now(timezone.utc) or (finish-begin).total_seconds()>36*3600:raise ValueError()
   except (ValueError,TypeError):raise ValueError('Use timezone-aware timestamps in order, no future finish, maximum 36 hours.') from None
   minutes=numeric(d.get('break_minutes',r['break_minutes']),'break minutes',0,(finish-begin).total_seconds()/60)
   if minutes!=int(minutes):raise ValueError('Use whole break minutes.')
   c.execute('UPDATE alport_attendance SET in_at=%s,out_at=%s,break_minutes=%s,status=%s,note=%s,approved_by=%s,approved_at=%s,version=version+1 WHERE id=%s',(begin.astimezone(timezone.utc).isoformat(),finish.astimezone(timezone.utc).isoformat(),int(minutes),status,note,u['id'],now(),rid));event(c,u,dict(r),'Attendance '+status,{'attendance_id':rid,'before':dict(r),'note':note})
  return jsonify(ok=True)
 @bp.post('/api/people/swaps')
 def offer():
  u,a=own();d=request.json;note=str(d.get('note') or '').strip()[:1000]
  with conn() as c:
   lock(c,u['organisation_id']);r=c.execute("SELECT * FROM shifts WHERE id=%s AND employee_id=%s AND organisation_id=%s AND site_id=%s AND status='Scheduled'",(d.get('shift_id'),a['employee_id'],u['organisation_id'],a['site_id'])).fetchone()
   if not r or shift_bounds(dict(r))[0]<=datetime.now(UK):raise ValueError('Choose your future scheduled shift.')
   target=c.execute("SELECT a.user_id FROM alport_staff_access a JOIN employees e ON e.id=a.employee_id WHERE a.employee_id=%s AND a.site_id=%s AND a.organisation_id=%s AND a.profile<>'disabled' AND a.activated_at IS NOT NULL AND e.active=1 AND EXISTS(SELECT 1 FROM users u WHERE u.id=a.user_id AND u.active=1)",(d.get('to_employee'),a['site_id'],u['organisation_id'])).fetchone()
   if not target or target['user_id']==u['id']:raise ValueError('Choose another active staff member at this venue.')
   if c.execute("SELECT id FROM alport_shift_swaps WHERE shift_id=%s AND status IN ('Offered','Accepted')",(r['id'],)).fetchone():raise ValueError('This shift already has an active swap request.')
   rid=c.execute('INSERT INTO alport_shift_swaps(organisation_id,site_id,shift_id,from_employee,to_employee,snapshot,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(u['organisation_id'],a['site_id'],r['id'],a['employee_id'],d['to_employee'],json.dumps(dict(r),default=str),note,now())).fetchone()['id'];notify(c,u['organisation_id'],a['site_id'],target['user_id'],'swap-'+str(rid),'A colleague has offered you a shift. Review it in your staff portal.','/people')
  return jsonify(id=rid)
 @bp.post('/api/people/swaps/<int:rid>')
 def swap_decision(rid):
  u=env['user']();a=access(u);d=request.json;action=d.get('status')
  with conn() as c:
   lock(c,u['organisation_id']);r=c.execute('SELECT * FROM alport_shift_swaps WHERE id=%s AND organisation_id=%s',(rid,u['organisation_id'])).fetchone()
   if not r or r['version']!=d.get('version'):raise ValueError('Swap changed. Refresh first.')
   if action in ('Accepted','Declined') and r['status']=='Offered':
    if not live(a) or a['employee_id']!=r['to_employee']:raise PermissionError('Only the proposed recipient can respond.')
   elif action=='Withdrawn' and r['status'] in ('Offered','Accepted'):
    if not live(a) or a['employee_id']!=r['from_employee']:raise PermissionError('Only the shift owner can withdraw it.')
   elif action in ('Approved','Rejected') and r['status']=='Accepted':
    _,s=manager()
    if s['id']!=r['site_id']:raise PermissionError('Select the swap’s venue.')
    if a and a['employee_id'] in (r['from_employee'],r['to_employee']):raise PermissionError('Another manager must review this swap.')
    if action=='Approved':
     if d.get('suitability_confirmed') is not True:raise ValueError('Confirm the recipient is trained and suitable for this shift.')
     sh=c.execute('SELECT * FROM shifts WHERE id=%s',(r['shift_id'],)).fetchone()
     if not sh or json.dumps(dict(sh),sort_keys=True,default=str)!=json.dumps(json.loads(r['snapshot']),sort_keys=True) or shift_bounds(dict(sh))[0]<=datetime.now(UK):raise ValueError('Shift changed or has already started.')
     target=employee(c,r['to_employee'],u,s)
     if not c.execute("SELECT a.id FROM alport_staff_access a JOIN users u ON u.id=a.user_id WHERE a.employee_id=%s AND a.profile<>'disabled' AND a.activated_at IS NOT NULL AND u.active=1",(target['id'],)).fetchone():raise ValueError('Recipient staff access is no longer active.')
     proposed=dict(sh);proposed['employee_id']=target['id']
     if shift_conflicts(c,u['organisation_id'],s['id'],proposed):raise ValueError('Recipient has approved time off.')
     for other in c.execute("SELECT * FROM shifts WHERE (employee_id=%s OR employee_id IN (SELECT b.employee_id FROM alport_staff_access a JOIN alport_staff_access b ON a.user_id=b.user_id WHERE a.employee_id=%s)) AND organisation_id=%s AND status NOT IN ('Cancelled','Declined') AND shift_date>=%s AND shift_date<=%s",(target['id'],target['id'],u['organisation_id'],(date.fromisoformat(sh['shift_date'])-timedelta(days=1)).isoformat(),(date.fromisoformat(sh['shift_date'])+timedelta(days=1)).isoformat())).fetchall():
      if overlaps(shift_bounds(dict(sh)),shift_bounds(dict(other))):raise ValueError('Recipient has a conflicting shift.')
     c.execute('UPDATE shifts SET employee_id=%s WHERE id=%s',(target['id'],sh['id']))
   else:raise ValueError('This decision is not available.')
   c.execute('UPDATE alport_shift_swaps SET status=%s,version=version+1 WHERE id=%s',(action,rid));event(c,u,{'organisation_id':u['organisation_id'],'site_id':r['site_id'],'employee_id':r['from_employee']},'Shift swap '+action,{'swap_id':rid,'note':str(d.get('note') or '')[:1000]})
   if action=='Accepted':notify_managers(c,u['organisation_id'],r['site_id'],'swap-review-'+str(rid),'A shift swap is ready for manager review.')
   for eid in (r['from_employee'],r['to_employee']):
    target=c.execute('SELECT user_id FROM alport_staff_access WHERE employee_id=%s',(eid,)).fetchone()
    if target:notify(c,u['organisation_id'],r['site_id'],target['user_id'],f'swap-{rid}-{action}','A shift-swap request has been updated.','/people')
  return jsonify(ok=True)
 @bp.post('/api/people/notifications/read')
 def read():
  u=env['user']()
  with conn() as c:c.execute('UPDATE alport_notifications SET read_at=%s WHERE user_id=%s AND organisation_id=%s AND id=%s',(now(),u['id'],u['organisation_id'],request.json.get('id')))
  return jsonify(ok=True)
 @bp.post('/api/people/notifications/preferences')
 def preferences():
  u=env['user']();d=request.json
  with conn() as c:c.execute('INSERT INTO alport_notification_preferences(user_id,email_enabled,push_enabled) VALUES(%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET email_enabled=excluded.email_enabled,push_enabled=excluded.push_enabled',(u['id'],int(d.get('email') is True),int(d.get('push') is True)))
  return jsonify(ok=True)
 @bp.post('/api/people/notifications/push')
 def push():
  from urllib.parse import urlsplit
  u=env['user']();d=request.json;endpoint=d.get('endpoint','');host=urlsplit(endpoint).hostname or ''
  if len(endpoint)>4096 or urlsplit(endpoint).username or urlsplit(endpoint).password or urlsplit(endpoint).port not in (None,443) or urlsplit(endpoint).scheme!='https' or not any(host==x or host.endswith('.'+x) for x in ('fcm.googleapis.com','push.services.mozilla.com','web.push.apple.com','notify.windows.com','wns.windows.com')):raise ValueError('Unsupported push endpoint.')
  if not isinstance(d.get('keys'),dict) or not d['keys'].get('p256dh') or not d['keys'].get('auth'):raise ValueError('Missing browser subscription keys.')
  encrypted=env['encrypt_staff'](json.dumps(d));digest=hashlib.sha256(endpoint.encode()).hexdigest()
  with conn() as c:
   old=c.execute('SELECT user_id FROM alport_push_subscriptions WHERE endpoint_hash=%s',(digest,)).fetchone()
   if old and old['user_id']!=u['id']:raise ValueError('This browser subscription belongs to another account. Unsubscribe in that browser first.')
   c.execute('INSERT INTO alport_push_subscriptions(user_id,endpoint_hash,payload,created_at) VALUES(%s,%s,%s,%s) ON CONFLICT(endpoint_hash) DO UPDATE SET payload=excluded.payload',(u['id'],digest,encrypted,now()))
  return jsonify(ok=True)
 @bp.get('/staff-notifications-sw.js')
 def service_worker():
  return Response("self.addEventListener('push',e=>{let d=e.data?e.data.json():{};e.waitUntil(self.registration.showNotification('Alport',{body:d.message||'You have an update.',tag:'alport-'+d.id,data:{url:'/people'},icon:'/static/alport-logo.svg'}));});self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.openWindow('/people'));});",mimetype='application/javascript',headers={'Service-Worker-Allowed':'/','Cache-Control':'no-cache'})
 def staff_notify(c,u,e,kind,payload):
  eid=e.get('employee_id',e.get('id'));org=e['organisation_id'];sid=e['site_id'];key=kind+'-'+str(payload.get('request_id',payload.get('attendance_id','')))+'-'+now()
  target=c.execute('SELECT user_id FROM alport_staff_access WHERE employee_id=%s',(eid,)).fetchone()
  if target and kind.startswith(('Leave ','Attendance ','Shift ')):notify(c,org,sid,target['user_id'],key,'Your staff record or request has been updated. Sign in to review.','/people')
  if kind in ('Leave requested','Clocked out'):notify_managers(c,org,sid,key,'A staff request or attendance record needs review.')
 app.extensions['alport_staff_notify']=staff_notify
 @bp.after_request
 def private(response):response.headers['Cache-Control']='no-store';return response
 app.extensions['alport_notify']=notify;app.register_blueprint(bp)
