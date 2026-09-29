import hashlib, hmac, secrets, os
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from flask import Blueprint, request, session, render_template, redirect, abort, jsonify
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import click

COOKIE_NAME='alport_cookie_preferences'
COOKIE_VERSION=1
COOKIE_AGE=180*24*60*60
STATUSES=('New','Investigating','Resolved','Closed')
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_bug_reports (
 reference TEXT PRIMARY KEY, organisation_id BIGINT, site_id BIGINT, user_id BIGINT,
 title TEXT NOT NULL, steps TEXT NOT NULL, expected TEXT NOT NULL, actual TEXT NOT NULL,
 page TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'New',
 admin_notes TEXT NOT NULL DEFAULT '', email_status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alport_bug_reports_created ON alport_bug_reports(created_at);
CREATE TABLE IF NOT EXISTS alport_bug_limits (
 actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alport_bug_limits_actor ON alport_bug_limits(actor,created_at);
'''

def now():return datetime.now(timezone.utc).isoformat()
def safe_page(raw):
 # Never retain query strings or fragments (OAuth codes / reservation tokens).
 try:path=urlsplit(str(raw)).path
 except ValueError:return ''
 if not path.startswith('/') or path.startswith('//'):return ''
 return path[:200]

def register_support(app,host):
 db=host['conn'];bp=Blueprint('alport_support',__name__)
 with db() as c:
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 signer=URLSafeTimedSerializer(app.secret_key,salt='alport-cookie-notice-v1')
 def acknowledged():
  try:
   d=signer.loads(request.cookies.get(COOKIE_NAME,''),max_age=COOKIE_AGE)
   return isinstance(d,dict) and d.get('version')==COOKIE_VERSION and d.get('necessary') is True
  except (BadSignature,SignatureExpired):return False
 def token():
  session.setdefault('support_csrf',secrets.token_urlsafe(32));return session['support_csrf']
 def admin():return host['company_admin_logged_in']()
 def render(mode,**kwargs):
  return render_template('support.html',mode=mode,csrf=token(),statuses=STATUSES,admin=admin(),**kwargs)
 @bp.before_request
 def guard():
  if request.content_length and request.content_length>24000:abort(413)
  if request.method=='POST':
   sent=request.headers.get('X-Support-CSRF') or request.form.get('csrf','')
   if not session.get('support_csrf') or not hmac.compare_digest(str(sent),session['support_csrf']):abort(403)
 @bp.get('/cookies')
 def cookies():return render('cookies',saved=acknowledged(),session_cookie=app.config.get('SESSION_COOKIE_NAME','session'),cookie_name=COOKIE_NAME)
 @bp.post('/cookies/preferences')
 def preferences():
  action=request.form.get('action')
  if action not in ('necessary','reset'):abort(400)
  response=jsonify(ok=True) if request.headers.get('X-Requested-With')=='AlportSupport' else redirect('/cookies',303)
  if action=='reset':response.delete_cookie(COOKIE_NAME,path='/',secure=app.config.get('SESSION_COOKIE_SECURE',False),httponly=True,samesite='Lax')
  else:response.set_cookie(COOKIE_NAME,signer.dumps({'version':COOKIE_VERSION,'necessary':True}),max_age=COOKIE_AGE,secure=app.config.get('SESSION_COOKIE_SECURE',False),httponly=True,samesite='Lax',path='/')
  return response
 def notify_report(ref):
  with db() as c:row=c.execute('SELECT * FROM alport_bug_reports WHERE reference=%s',(ref,)).fetchone()
  if not row:return
  recipient=os.environ.get('SUPPORT_EMAIL','').strip()
  delivery='Not configured'
  if recipient and host.get('send_alport_email'):
   body='\n'.join([
    'New Alport bug report: '+ref,'Title: '+row['title'],
    'Contact email: '+(row['email'] or 'Not supplied'),
    'Organisation: '+str(row['organisation_id'] or 'Public visitor'),
    'Site: '+str(row['site_id'] or 'Not supplied'),
    'Page: '+(row['page'] or 'Not supplied'),
    '', 'Steps to reproduce:',row['steps'],'',
    'Expected:',row['expected'] or 'Not supplied','',
    'What happened:',row['actual'],'',
    'Review this report in Alport company admin → Bug reports.'])
   try:
    result=host['send_alport_email'](recipient,'Alport bug report '+ref,body)
    delivery='Sent' if result.get('ok') else 'Failed or unconfirmed'
   except Exception:delivery='Failed or unconfirmed'
  with db() as c:c.execute('UPDATE alport_bug_reports SET email_status=%s WHERE reference=%s',(delivery,ref))
 def form(error=None,values=None,code=200):
  session.setdefault('bug_reference','ALP-'+secrets.token_hex(8).upper())
  return render('report',error=error,values=values or {},reference=session['bug_reference']),code
 @bp.get('/report-bug')
 def report_page():return form(values={'page':safe_page(request.args.get('page',''))})
 @bp.post('/report-bug')
 def report():
  fields={k:(request.form.get(k) or '').strip() for k in ('title','steps','expected','actual','page','email')}
  limits={'title':160,'steps':5000,'expected':2000,'actual':3000,'page':200,'email':200}
  if any(len(fields[k])>n for k,n in limits.items()):return form('One of the fields is too long. Shorten it and try again.',fields,400)
  if not all(fields[k] for k in ('title','steps','actual')):return form('Add a title, steps to reproduce and what happened.',fields,400)
  if fields['email'] and ('@' not in fields['email'] or any(c.isspace() for c in fields['email'])):return form('Enter a valid contact email or leave it blank.',fields,400)
  if request.form.get('website'):return form('Unable to submit this report. Please try again.',fields,400)
  ref=request.form.get('reference','')
  if not ref or not hmac.compare_digest(ref,session.get('bug_reference','')):return form('This form expired. Submit the refreshed form below.',fields,400)
  u=host['user']();s=host['current_site']() if u else None
  if s and s['organisation_id']!=u['organisation_id']:s=None
  fields['page']=safe_page(fields['page'])
  # HMAC prevents reversible IP hashes. No raw IP or browser fingerprint is saved.
  def digest(x):return hmac.new(str(app.secret_key).encode(),x.encode(),hashlib.sha256).hexdigest()
  ip=digest('ip:'+str(request.remote_addr or 'unknown'))
  actor=digest('user:'+str(u['id'])) if u else digest('session:'+token())
  cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
  with db() as c:
   # Fixed ordering protects parallel submissions and prevents limit races.
   for key in sorted({ip,actor}):c.execute('SELECT pg_advisory_xact_lock(%s)',(int(key[:15],16),))
   existing=c.execute('SELECT reference FROM alport_bug_reports WHERE reference=%s',(ref,)).fetchone()
   if not existing:
    for key,limit in ((ip,30),(actor,5)):
     count=c.execute('SELECT COUNT(*) AS n FROM alport_bug_limits WHERE actor=%s AND created_at>%s',(key,cutoff)).fetchone()['n']
     if count>=limit:
      response,status=form('Too many reports in the last hour. Please try again later.',fields,429)
      return response,status,{'Retry-After':'3600'}
    stamp=now()
    c.execute('INSERT INTO alport_bug_reports(reference,organisation_id,site_id,user_id,title,steps,expected,actual,page,email,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(ref,u['organisation_id'] if u else None,s['id'] if s else None,u['id'] if u else None,fields['title'],fields['steps'],fields['expected'],fields['actual'],fields['page'],fields['email'],stamp,stamp))
    for key in {ip,actor}:c.execute('INSERT INTO alport_bug_limits(actor,created_at) VALUES(%s,%s)',(key,stamp))
    c.execute('DELETE FROM alport_bug_limits WHERE created_at<%s',(cutoff,))
  if not existing:notify_report(ref)
  session['bug_receipt']=ref
  # Keep the form reference until receipt/new form is visited so retries dedupe.
  return redirect('/report-bug/sent',303)
 @bp.get('/report-bug/sent')
 def sent():
  ref=session.get('bug_receipt')
  if not ref:return redirect('/report-bug')
  if session.get('bug_reference')==ref:session.pop('bug_reference',None)
  return render('sent',reference=ref)
 @bp.get('/company-admin/bugs')
 def inbox():
  if not admin():return redirect('/company-admin/login')
  status=request.args.get('status','')
  if status and status not in STATUSES:abort(400)
  try:page=max(1,int(request.args.get('page','1')))
  except ValueError:abort(400)
  with db() as c:
   sql='SELECT reference,title,status,email_status,created_at,organisation_id FROM alport_bug_reports'
   args=()
   if status:sql+=' WHERE status=%s';args=(status,)
   reports=c.execute(sql+' ORDER BY created_at DESC LIMIT 51 OFFSET %s',(*args,(page-1)*50)).fetchall()
  return render('inbox',reports=reports[:50],more=len(reports)>50,page=page,status=status)
 @bp.route('/company-admin/bugs/<reference>',methods=['GET','POST'])
 def detail(reference):
  if not admin():abort(403)
  with db() as c:
   row=c.execute('SELECT * FROM alport_bug_reports WHERE reference=%s',(reference,)).fetchone()
   if not row:abort(404)
   if request.method=='POST':
    if request.form.get('action')=='delete':
     c.execute('DELETE FROM alport_bug_reports WHERE reference=%s',(reference,));return redirect('/company-admin/bugs',303)
    status=request.form.get('status');notes=request.form.get('notes','').strip()
    if status not in STATUSES or len(notes)>5000:abort(400)
    c.execute('UPDATE alport_bug_reports SET status=%s,admin_notes=%s,updated_at=%s WHERE reference=%s',(status,notes,now(),reference))
    return redirect('/company-admin/bugs/'+reference,303)
  return render('detail',report=row)
 @app.cli.command('purge-bug-reports')
 def purge():
  """Remove resolved/closed reports unchanged for 90 days; keep open reports."""
  with db() as c:
   c.execute("DELETE FROM alport_bug_reports WHERE status IN ('Resolved','Closed') AND updated_at<%s",((datetime.now(timezone.utc)-timedelta(days=90)).isoformat(),))
   c.execute('DELETE FROM alport_bug_limits WHERE created_at<%s',((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),))
  click.echo('Removed old closed/resolved reports and expired rate-limit records.')
 app.register_blueprint(bp)
 @app.after_request
 def support_widget(response):
  if request.endpoint and request.endpoint.startswith('alport_support.'):
   response.headers['Cache-Control']='private, no-store'
   response.headers['Referrer-Policy']='same-origin'
  if response.status_code!=200 or response.mimetype!='text/html' or response.is_streamed or response.direct_passthrough:return response
  if response.headers.get('Content-Encoding'):return response
  html=response.get_data(as_text=True);at=html.lower().rfind('</body>')
  if at<0:return response
  # Route pattern avoids collecting query parameters or actual URL path tokens.
  page=request.url_rule.rule if request.url_rule else ''
  widget=render_template('support_widget.html',show_notice=not acknowledged(),csrf=token(),support_page=page,support_admin=admin())
  response.set_data(html[:at]+widget+html[at:])
  response.headers['Cache-Control']='private, no-store'
  response.headers.pop('ETag',None)
  return response
