import os,json,re,secrets,hmac,hashlib,time
from datetime import datetime,timezone,timedelta
from urllib.parse import urlencode
import requests,click
from psycopg.errors import ExclusionViolation
from cryptography.fernet import Fernet
from flask import Blueprint,session,request,abort,redirect,render_template,jsonify
import social_booking as engine
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_social_connections(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 page_id TEXT NOT NULL UNIQUE,instagram_id TEXT NOT NULL DEFAULT '',label TEXT NOT NULL,
 token TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alport_social_auth(
 state TEXT PRIMARY KEY, user_id BIGINT NOT NULL,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 expires TEXT NOT NULL,data TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS alport_social_events(
 id BIGSERIAL PRIMARY KEY,connection_id BIGINT NOT NULL,channel TEXT NOT NULL,mid TEXT NOT NULL,
 sender TEXT NOT NULL,sent_at BIGINT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Pending',
 UNIQUE(connection_id,channel,mid));
CREATE TABLE IF NOT EXISTS alport_social_threads(
 id BIGSERIAL PRIMARY KEY,connection_id BIGINT NOT NULL,channel TEXT NOT NULL,sender TEXT NOT NULL,
 state TEXT NOT NULL,last_inbound BIGINT NOT NULL DEFAULT 0,paused INTEGER NOT NULL DEFAULT 0,
 UNIQUE(connection_id,channel,sender));
CREATE TABLE IF NOT EXISTS alport_social_outbox(
 id BIGSERIAL PRIMARY KEY,event_id BIGINT NOT NULL UNIQUE,thread_id BIGINT NOT NULL,
 body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Pending',provider_mid TEXT NOT NULL DEFAULT '');
'''
# Every booking writer (manager UI, public amendments and this worker) participates.
# This is installed only when the administrator explicitly runs social-install-guard.
GUARD_SQL='''CREATE OR REPLACE FUNCTION alport_guard_booking_overlap() RETURNS trigger AS $$
DECLARE gap integer; oldsite bigint;
BEGIN
 IF TG_OP='UPDATE' THEN oldsite=OLD.site_id; ELSE oldsite=NEW.site_id; END IF;
 PERFORM pg_advisory_xact_lock(-9000000000000-LEAST(oldsite,NEW.site_id));
 IF oldsite<>NEW.site_id THEN PERFORM pg_advisory_xact_lock(-9000000000000-GREATEST(oldsite,NEW.site_id)); END IF;
 IF NEW.table_id IS NOT NULL AND NEW.status NOT IN ('Cancelled','No-show') THEN
  SELECT COALESCE(turnaround_minutes,0) INTO gap FROM booking_settings WHERE site_id=NEW.site_id;
  gap=COALESCE(gap,0);
  IF NOT EXISTS(SELECT 1 FROM restaurant_tables t WHERE t.id=NEW.table_id AND t.site_id=NEW.site_id AND t.organisation_id=NEW.organisation_id AND t.active=1 AND NEW.party_size BETWEEN t.min_capacity AND t.max_capacity) THEN
   RAISE EXCEPTION 'The selected table does not belong to this venue or fit this party' USING ERRCODE='23P01';
  END IF;
  IF EXISTS(SELECT 1 FROM bookings b WHERE b.site_id=NEW.site_id AND b.table_id=NEW.table_id AND b.id<>COALESCE(NEW.id,-1) AND b.status NOT IN ('Cancelled','No-show')
   AND b.booking_date::date+b.booking_time::time < NEW.booking_date::date+NEW.booking_time::time+(NEW.duration_minutes+gap)*interval '1 minute'
   AND NEW.booking_date::date+NEW.booking_time::time < b.booking_date::date+b.booking_time::time+(b.duration_minutes+gap)*interval '1 minute') THEN
   RAISE EXCEPTION 'Table is no longer available' USING ERRCODE='23P01';
  END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql'''

def stamp():return datetime.now(timezone.utc).isoformat()
def milliseconds():return int(time.time()*1000)
def cipher():return Fernet(os.environ['SOCIAL_DATA_KEY'].encode())
def enc(d):return cipher().encrypt(json.dumps(d).encode()).decode()
def dec(s):return json.loads(cipher().decrypt(s.encode()))
def graph(method,path,token,**kw):
 version=os.getenv('META_GRAPH_VERSION','')
 if not re.fullmatch(r'v\d+\.\d+',version):raise ValueError('Set META_GRAPH_VERSION to the version configured for your Meta app.')
 params=kw.pop('params',{});params['appsecret_proof']=hmac.new(os.environ['META_APP_SECRET'].encode(),token.encode(),hashlib.sha256).hexdigest()
 r=requests.request(method,'https://graph.facebook.com/'+version+'/'+path,headers={'Authorization':'Bearer '+token},params=params,timeout=(5,20),allow_redirects=False,**kw)
 if not r.ok:raise ValueError(f'Meta request failed (HTTP {r.status_code}). Check permissions and token access.')
 return r.json()

def register_social(app,host):
 db=host['conn'];bp=Blueprint('alport_social',__name__)
 with db() as c:
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 @app.errorhandler(ExclusionViolation)
 def reservation_conflict(error):
  if request.path.startswith('/api/'):
   return jsonify(error='This table is no longer available or does not fit this party. Refresh availability and select another table.'),409
  return 'This reservation conflicts with another booking. Please return to the venue and choose another time.',409
 @bp.after_request
 def private_response(response):
  response.headers['Cache-Control']='private, no-store'
  response.headers['Referrer-Policy']='no-referrer'
  return response
 def context():
  u=host['user']();s=host['current_site']()
  if not u:abort(401)
  if not s or s['organisation_id']!=u['organisation_id'] or u['role'] not in ('Owner','Admin','General Manager','Manager'):abort(403)
  sub=host['subscription_for'](u['organisation_id'])
  demo=session.get('billing_preview_admin') is True and str(session.get('billing_preview_bypass_org'))==str(u['organisation_id'])
  if not (sub and sub.get('status')=='Active') and not demo:abort(403)
  return u,s
 def configuration():
  for name in ('SOCIAL_DATA_KEY','META_APP_ID','META_APP_SECRET','META_VERIFY_TOKEN','META_GRAPH_VERSION','PUBLIC_BASE_URL'):
   if not os.getenv(name):raise ValueError('Administrator must configure '+name+' first.')
  cipher()
  if not os.environ['PUBLIC_BASE_URL'].startswith('https://'):raise ValueError('PUBLIC_BASE_URL must use HTTPS.')
 def guard_ready(c):
  return bool(c.execute("SELECT 1 AS ready FROM pg_trigger WHERE tgname='alport_booking_overlap_guard' AND tgrelid='bookings'::regclass AND tgenabled='O' AND NOT tgisinternal").fetchone())
 @bp.before_request
 def protect():
  if request.path=='/social/webhook':return
  context()
  if request.content_length and request.content_length>20000:abort(413)
  if request.method=='POST':
   token=session.get('social_csrf','');received=request.form.get('csrf','')
   if not token or not hmac.compare_digest(token,received):abort(403)
 @bp.errorhandler(ValueError)
 def bad(e):return render_template('social.html',error=str(e),connections=[],threads=[],pending=[],csrf=session.get('social_csrf','')),400
 @bp.get('/social')
 def page():
  u,s=context();session.setdefault('social_csrf',secrets.token_urlsafe(32))
  with db() as c:
   connections=c.execute('SELECT id,page_id,instagram_id,label,enabled FROM alport_social_connections WHERE organisation_id=%s AND site_id=%s',(u['organisation_id'],s['id'])).fetchall()
   threads=c.execute('SELECT t.* FROM alport_social_threads t JOIN alport_social_connections c ON c.id=t.connection_id WHERE c.organisation_id=%s AND c.site_id=%s ORDER BY t.last_inbound DESC LIMIT 100',(u['organisation_id'],s['id'])).fetchall()
   for t in threads:
    recent=c.execute('SELECT payload FROM alport_social_events WHERE connection_id=%s AND channel=%s AND sender=%s ORDER BY sent_at DESC,id DESC LIMIT 1',(t['connection_id'],t['channel'],t['sender'])).fetchone()
    t['latest_message']=dec(recent['payload']) if recent else ''
    t['details']=dec(t['state']);t['delivery']=c.execute('SELECT status FROM alport_social_outbox WHERE thread_id=%s ORDER BY id DESC LIMIT 1',(t['id'],)).fetchone()
   pending=[]
   state=session.get('social_auth','')
   if state:
    r=c.execute('SELECT * FROM alport_social_auth WHERE state=%s AND user_id=%s AND organisation_id=%s AND site_id=%s',(hashlib.sha256(state.encode()).hexdigest(),u['id'],u['organisation_id'],s['id'])).fetchone()
    if r and r['data'] and r['expires']>stamp():pending=[{'id':x['id'],'name':x.get('name',x['id'])} for x in dec(r['data'])]
  return render_template('social.html',connections=connections,threads=threads,pending=pending,csrf=session['social_csrf'],error=None)
 def callback_url():return os.environ['PUBLIC_BASE_URL'].rstrip('/')+'/social/callback'
 @bp.post('/social/connect')
 def connect():
  configuration();u,s=context();state=secrets.token_urlsafe(32);session['social_auth']=state
  with db() as c:
   c.execute('DELETE FROM alport_social_auth WHERE expires<%s',(stamp(),))
   c.execute('INSERT INTO alport_social_auth(state,user_id,organisation_id,site_id,expires) VALUES(%s,%s,%s,%s,%s)',(hashlib.sha256(state.encode()).hexdigest(),u['id'],u['organisation_id'],s['id'],(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()))
  scope='pages_show_list,pages_read_engagement,pages_manage_metadata,pages_messaging,instagram_basic,instagram_manage_messages'
  return redirect('https://www.facebook.com/'+os.environ['META_GRAPH_VERSION']+'/dialog/oauth?'+urlencode({'client_id':os.environ['META_APP_ID'],'redirect_uri':callback_url(),'state':state,'scope':scope,'response_type':'code'}))
 @bp.get('/social/callback')
 def callback():
  configuration();u,s=context();state=request.args.get('state','')
  if not state or not hmac.compare_digest(state,session.get('social_auth','')):abort(403)
  with db() as c:
   auth=c.execute('DELETE FROM alport_social_auth WHERE state=%s AND user_id=%s AND organisation_id=%s AND site_id=%s AND data=%s RETURNING *',(hashlib.sha256(state.encode()).hexdigest(),u['id'],u['organisation_id'],s['id'],'')).fetchone()
  if not auth or auth['expires']<stamp() or not request.args.get('code'):raise ValueError('Meta authorisation expired or was declined. Start again.')
  token=graph('GET','oauth/access_token',os.environ['META_APP_ID']+'|'+os.environ['META_APP_SECRET'],params={'client_id':os.environ['META_APP_ID'],'client_secret':os.environ['META_APP_SECRET'],'redirect_uri':callback_url(),'code':request.args['code']})['access_token']
  token=graph('GET','oauth/access_token',token,params={'grant_type':'fb_exchange_token','client_id':os.environ['META_APP_ID'],'client_secret':os.environ['META_APP_SECRET'],'fb_exchange_token':token})['access_token']
  pages=[];after=None
  for _ in range(20):
   result=graph('GET','me/accounts',token,params={'fields':'id,name,access_token,instagram_business_account','limit':100,**({'after':after} if after else {})})
   pages.extend(result.get('data',[]))
   paging=result.get('paging',{})
   if not paging.get('next'):break
   after=paging.get('cursors',{}).get('after')
   if not after:raise ValueError('Incomplete Meta Page list. Contact support.')
  else:raise ValueError('Too many Pages returned; contact support.')
  if not pages:raise ValueError('No managed Facebook Pages returned. Check your Page role and granted permissions.')
  with db() as c:c.execute('INSERT INTO alport_social_auth(state,user_id,organisation_id,site_id,expires,data) VALUES(%s,%s,%s,%s,%s,%s)',(auth['state'],u['id'],u['organisation_id'],s['id'],auth['expires'],enc(pages)))
  return redirect('/social')
 @bp.post('/social/select')
 def select_page():
  u,s=context();state=session.get('social_auth','')
  with db() as c:
   r=c.execute('SELECT * FROM alport_social_auth WHERE state=%s AND user_id=%s AND organisation_id=%s AND site_id=%s FOR UPDATE',(hashlib.sha256(state.encode()).hexdigest(),u['id'],u['organisation_id'],s['id'])).fetchone()
   if not r or r['expires']<stamp() or not r['data']:raise ValueError('Connection selection expired. Start again.')
   p=next((x for x in dec(r['data']) if x['id']==request.form.get('page_id')),None)
   if not p:abort(400)
   c.execute('SELECT pg_advisory_xact_lock(%s)',(int(p['id'])%900000000000,))
   old=c.execute('SELECT * FROM alport_social_connections WHERE page_id=%s',(p['id'],)).fetchone()
   if old and (old['organisation_id'],old['site_id'])!=(u['organisation_id'],s['id']):raise ValueError('This Page is already connected to another venue.')
   graph('POST',p['id']+'/subscribed_apps',p['access_token'],data={'subscribed_fields':'messages'})
   c.execute('INSERT INTO alport_social_connections(organisation_id,site_id,page_id,instagram_id,label,token) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(page_id) DO UPDATE SET token=excluded.token,instagram_id=excluded.instagram_id,label=excluded.label,enabled=0',(u['organisation_id'],s['id'],p['id'],(p.get('instagram_business_account') or {}).get('id',''),p.get('name',p['id']),enc(p['access_token'])))
   c.execute('DELETE FROM alport_social_auth WHERE state=%s',(r['state'],))
  session.pop('social_auth',None);return redirect('/social')
 @bp.post('/social/connection/<int:cid>')
 def connection_action(cid):
  u,s=context();action=request.form.get('action')
  with db() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(-8000000000000-cid,))
   r=c.execute('SELECT * FROM alport_social_connections WHERE id=%s AND organisation_id=%s AND site_id=%s',(cid,u['organisation_id'],s['id'])).fetchone()
   if not r:abort(404)
   if action=='enable':
    configuration()
    if not guard_ready(c):raise ValueError('Install the booking database guard before enabling automation. See installation instructions.')
    if not r['token']:raise ValueError('Reconnect this Page first.')
    if not c.execute('SELECT id FROM booking_sessions WHERE organisation_id=%s AND site_id=%s AND active=1',(u['organisation_id'],s['id'])).fetchone():raise ValueError('Configure booking service sessions first.')
    c.execute('UPDATE alport_social_connections SET enabled=1 WHERE id=%s',(cid,))
   elif action in ('pause','disconnect'):
    c.execute('UPDATE alport_social_connections SET enabled=0 WHERE id=%s',(cid,))
    if action=='disconnect':c.execute("UPDATE alport_social_connections SET token='' WHERE id=%s",(cid,))
    c.execute("UPDATE alport_social_events SET status='Paused' WHERE connection_id=%s AND status='Pending'",(cid,))
    c.execute("UPDATE alport_social_outbox SET status='Paused' WHERE status='Pending' AND thread_id IN (SELECT id FROM alport_social_threads WHERE connection_id=%s)",(cid,))
   else:abort(400)
  return redirect('/social')
 @bp.post('/social/thread/<int:tid>/pause')
 def pause_thread(tid):
  u,s=context()
  with db() as c:
   t=c.execute('SELECT t.* FROM alport_social_threads t JOIN alport_social_connections c ON c.id=t.connection_id WHERE t.id=%s AND c.organisation_id=%s AND c.site_id=%s FOR UPDATE',(tid,u['organisation_id'],s['id'])).fetchone()
   if not t:abort(404)
   c.execute('UPDATE alport_social_threads SET paused=1 WHERE id=%s',(tid,))
   c.execute("UPDATE alport_social_outbox SET status='Paused' WHERE thread_id=%s AND status='Pending'",(tid,))
  return redirect('/social')
 @bp.route('/social/webhook',methods=['GET','POST'])
 def webhook():
  if request.method=='GET':
   expected=os.getenv('META_VERIFY_TOKEN','')
   if not expected or request.args.get('hub.mode')!='subscribe' or not hmac.compare_digest(expected,request.args.get('hub.verify_token','')):abort(403)
   return request.args.get('hub.challenge',''),200,{'Content-Type':'text/plain'}
  if request.content_length and request.content_length>1000000:abort(413)
  raw=request.get_data()
  if len(raw)>1000000:abort(413)
  secret=os.getenv('META_APP_SECRET','')
  expected='sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
  if not secret or not hmac.compare_digest(expected,request.headers.get('X-Hub-Signature-256','')):abort(403)
  body=request.get_json(silent=True)
  if not isinstance(body,dict):abort(400)
  channel={'page':'facebook','instagram':'instagram'}.get(body.get('object'))
  if not channel:return 'Ignored',200
  with db() as c:
   for entry in body.get('entry',[]):
    field='page_id' if channel=='facebook' else 'instagram_id'
    conn=c.execute('SELECT * FROM alport_social_connections WHERE '+field+'=%s AND enabled=1',(str(entry.get('id','')),)).fetchone()
    if not conn:continue
    for event in entry.get('messaging',[]):
     message=event.get('message',{});sender=str(event.get('sender',{}).get('id',''));mid=message.get('mid')
     if message.get('is_echo') or not sender or not mid:continue
     if str(event.get('recipient',{}).get('id',''))!=str(entry.get('id','')):continue
     try:sent=int(event.get('timestamp',0))
     except (ValueError,TypeError):continue
     if not 0<=milliseconds()-sent<24*60*60*1000:continue
     text=message.get('text','')
     if not isinstance(text,str):text=''
     if len(text)>2000 or message.get('attachments'):text='STAFF'
     c.execute('INSERT INTO alport_social_events(connection_id,channel,mid,sender,sent_at,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(connection_id,channel,mid) DO NOTHING',(conn['id'],channel,str(mid)[:250],sender,sent,enc(text)))
  return 'Received',200
 def process_one():
  with db() as c:
   candidate=c.execute("SELECT id,connection_id FROM alport_social_events WHERE status='Pending' ORDER BY sent_at,id LIMIT 1").fetchone()
   if not candidate:return False
   # Match pause/disconnect lock ordering: connection first, then event row.
   c.execute('SELECT pg_advisory_xact_lock(%s)',(-8000000000000-candidate['connection_id'],))
   e=c.execute("SELECT * FROM alport_social_events WHERE id=%s AND status='Pending' FOR UPDATE SKIP LOCKED",(candidate['id'],)).fetchone()
   if not e:return True
   conn=c.execute('SELECT * FROM alport_social_connections WHERE id=%s',(e['connection_id'],)).fetchone()
   sub=host['subscription_for'](conn['organisation_id'])
   if not conn['enabled'] or not sub or sub.get('status')!='Active' or milliseconds()-e['sent_at']>=86400000:
    c.execute("UPDATE alport_social_events SET status='Paused or expired' WHERE id=%s",(e['id'],));return True
   if not guard_ready(c):raise ValueError('Booking overlap guard missing; automation stopped.')
   c.execute('SELECT pg_advisory_xact_lock(%s)',(-9000000000000-conn['site_id'],))
   c.execute('INSERT INTO alport_social_threads(connection_id,channel,sender,state) VALUES(%s,%s,%s,%s) ON CONFLICT(connection_id,channel,sender) DO NOTHING',(conn['id'],e['channel'],e['sender'],enc({})))
   t=c.execute('SELECT * FROM alport_social_threads WHERE connection_id=%s AND channel=%s AND sender=%s FOR UPDATE',(conn['id'],e['channel'],e['sender'])).fetchone()
   if t['paused'] or e['sent_at']<t['last_inbound']:
    c.execute("UPDATE alport_social_events SET status='Paused or out of order' WHERE id=%s",(e['id'],));return True
   state=dec(t['state'])
   # Expired partial conversations require a fresh request; never reuse old consent.
   if t['last_inbound'] and e['sent_at']-t['last_inbound']>86400000 and state.get('stage') not in ('booked','human'):state={}
   def book(d,table):
    cfg=c.execute('SELECT * FROM booking_settings WHERE organisation_id=%s AND site_id=%s',(conn['organisation_id'],conn['site_id'])).fetchone();ts=stamp()
    email=d['contact'] if '@' in d['contact'] else '';phone=d['contact'] if not email else ''
    guest=c.execute('INSERT INTO guests(organisation_id,name,email,phone,marketing_consent,created_at,updated_at) VALUES(%s,%s,%s,%s,0,%s,%s) RETURNING id',(conn['organisation_id'],d['name'],email,phone,ts,ts)).fetchone()['id']
    return c.execute("INSERT INTO bookings(organisation_id,site_id,guest_id,booking_date,booking_time,party_size,duration_minutes,status,source,table_id,guest_name,guest_email,guest_phone,manage_token,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'Confirmed',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",(conn['organisation_id'],conn['site_id'],guest,d['date'],d['time'],d['party'],cfg['default_duration'],e['channel'].title(),table['id'],d['name'],email,phone,secrets.token_urlsafe(32),ts,ts)).fetchone()['id']
   new,reply=engine.advance(state,dec(e['payload']),lambda d:engine.options(c,conn['organisation_id'],conn['site_id'],d),lambda d:engine.alternatives(c,conn['organisation_id'],conn['site_id'],d),book)
   c.execute('UPDATE alport_social_threads SET state=%s,last_inbound=%s WHERE id=%s',(enc(new),e['sent_at'],t['id']))
   if reply:c.execute('INSERT INTO alport_social_outbox(event_id,thread_id,body) VALUES(%s,%s,%s) ON CONFLICT(event_id) DO NOTHING',(e['id'],t['id'],enc(reply)))
   c.execute("UPDATE alport_social_events SET status='Processed' WHERE id=%s",(e['id'],))
  return True
 def send_one():
  # Claim before sending: a timeout is UNKNOWN, never blindly retry a message.
  with db() as c:
   row=c.execute("SELECT o.*,t.connection_id,t.sender,t.channel,t.last_inbound,t.paused FROM alport_social_outbox o JOIN alport_social_threads t ON t.id=o.thread_id WHERE o.status='Pending' ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED").fetchone()
   if not row:return False
   conn=c.execute('SELECT * FROM alport_social_connections WHERE id=%s',(row['connection_id'],)).fetchone()
   sub=host['subscription_for'](conn['organisation_id'])
   if not sub or sub.get('status')!='Active' or row['paused'] or not conn['enabled'] or not conn['token'] or milliseconds()-row['last_inbound']>=86400000:
    c.execute("UPDATE alport_social_outbox SET status='Paused or expired' WHERE id=%s",(row['id'],));return True
   c.execute("UPDATE alport_social_outbox SET status='Sending - check if interrupted' WHERE id=%s",(row['id'],))
  try:
   payload={'recipient':{'id':row['sender']},'message':{'text':dec(row['body'])}}
   if row['channel']=='facebook':payload['messaging_type']='RESPONSE'
   result=graph('POST',conn['page_id']+'/messages',dec(conn['token']),json=payload)
   status='Sent' if result.get('message_id') else 'Unconfirmed - staff review';mid=result.get('message_id','')
  except Exception:status='Failed or unknown - staff review';mid=''
  with db() as c:c.execute('UPDATE alport_social_outbox SET status=%s,provider_mid=%s WHERE id=%s',(status,str(mid),row['id']))
  return True
 @app.cli.command('social-install-guard')
 def install_guard():
  with db() as c:
   c.execute(GUARD_SQL)
   c.execute('DROP TRIGGER IF EXISTS alport_booking_overlap_guard ON bookings')
   c.execute('CREATE TRIGGER alport_booking_overlap_guard BEFORE INSERT OR UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION alport_guard_booking_overlap()')
  click.echo('Installed venue-wide reservation locking and table overlap guard.')
 @app.cli.command('social-worker')
 @click.option('--once',is_flag=True)
 def worker(once):
  configuration()
  while True:
   try:
    busy=process_one();sent=send_one()
   except Exception:
    # Avoid logging message text, credentials or raw provider exceptions.
    click.echo('Social worker stopped: inspect configuration/database; queued data retained.',err=True)
    raise click.ClickException('Worker requires administrator attention.') from None
   if once:return
   if not busy and not sent:time.sleep(2)
 app.extensions['social_process_one']=process_one;app.extensions['social_send_one']=send_one
 app.register_blueprint(bp)
