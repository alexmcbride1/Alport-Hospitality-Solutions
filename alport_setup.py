import os, json, secrets, hashlib, base64
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from flask import Blueprint, request, session, jsonify, redirect, render_template, abort
from cryptography.fernet import Fernet
import click
import till_connectors as tc

STEPS = [
 ('welcome','How Alport works',None,'Review how sales, stock, purchasing, staff and payments fit together.'),
 ('venue','Venue details','sites','Venue-specific reporting needs the correct site and address.'),
 ('till','Connect your till',None,'Automatic sales and tip imports need a verified till connection and a successful import.'),
 ('suppliers','Suppliers','purchasing','Purchasing needs your suppliers, contacts and payment terms.'),
 ('menu','Menu and recipes','menu','Ingredient demand requires complete recipes and explicit till-item mappings. Mapping and forecasting are a later release.'),
 ('stock','Opening stock','stock','Stock estimates need an accurate opening count and ingredient units.'),
 ('staff','Staff and payroll','staff','Payroll needs staff records, approved hours, pay rates and payroll checks.'),
 ('banking','Banking and tronc','money','Payments require verified recipients and bank authorisation. Tronc still requires allocation and manager approval.'),
 ('bookings','Bookings and demand','bookings','Bookings improve demand context. Weather, event adjustments and suggested orders are a later release.'),
]
SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_setup_progress (
 organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL, data JSONB NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY(organisation_id,site_id));
CREATE TABLE IF NOT EXISTS alport_till_connections (
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 provider TEXT NOT NULL, account TEXT NOT NULL, location TEXT NOT NULL, label TEXT NOT NULL,
 secret TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Verified - awaiting import',
 last_sync TEXT, error TEXT NOT NULL DEFAULT '', UNIQUE(organisation_id,provider,account,location));
CREATE TABLE IF NOT EXISTS alport_till_auth (
 state TEXT PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 user_id BIGINT NOT NULL, provider TEXT NOT NULL, expires TEXT NOT NULL, secret TEXT, locations JSONB);
CREATE TABLE IF NOT EXISTS alport_till_sales (
 connection_id BIGINT NOT NULL REFERENCES alport_till_connections(id), external_id TEXT NOT NULL,
 sale_date TEXT NOT NULL, data JSONB NOT NULL, PRIMARY KEY(connection_id,external_id));
CREATE INDEX IF NOT EXISTS alport_till_sales_day ON alport_till_sales(sale_date);
'''

def now(): return datetime.now(timezone.utc)
def dump(x): return json.dumps(x, allow_nan=False)
def value(x): return json.loads(x) if isinstance(x,str) else x

def register_setup(app, host):
 db=host['conn']; bp=Blueprint('alport_setup',__name__)
 with db() as c:
  for statement in SCHEMA.split(';'):
   if statement.strip(): c.execute(statement)
 def query(sql,args=(),one=False):
  with db() as c:
   r=c.execute(sql,args);return r.fetchone() if one else r.fetchall()
 def cipher():
  key=os.getenv('TILL_DATA_KEY','')
  if not key:raise tc.TillError('Administrator must set a permanent TILL_DATA_KEY before connecting tills.')
  try:return Fernet(key.encode())
  except Exception:raise tc.TillError('TILL_DATA_KEY is not a valid Fernet key.') from None
 def encrypt(d):return cipher().encrypt(dump(d).encode()).decode()
 def decrypt(s):
  try:return json.loads(cipher().decrypt(s.encode()))
  except tc.TillError:raise
  except Exception:raise tc.TillError('Stored connection cannot be decrypted. Restore the original TILL_DATA_KEY.') from None
 def context():
  u,s=host['user'](),host['current_site']()
  if not u:abort(401)
  if not s or s['organisation_id']!=u['organisation_id']:abort(403)
  if u['role'] not in ('Owner','Admin','General Manager','Manager'):abort(403)
  sub=host['subscription_for'](u['organisation_id'])
  if not sub or sub.get('status')!='Active':abort(403,description='Activate your subscription before venue setup.')
  return u,s
 @bp.before_request
 def guard():
  context()
  if request.content_length and request.content_length>16384:abort(413)
  if request.method=='POST':
   token=session.get('setup_csrf')
   if not token or not secrets.compare_digest(token,request.headers.get('X-Setup-CSRF','')):abort(403)
 @bp.errorhandler(tc.TillError)
 def problem(e):return jsonify(error=str(e)),400
 def scope():
  u,s=context();return u['organisation_id'],s['id']
 def progress():
  r=query('SELECT data FROM alport_setup_progress WHERE organisation_id=%s AND site_id=%s',scope(),True)
  return value(r['data']) if r else {}
 def save_progress(d):
  with db() as c:c.execute('INSERT INTO alport_setup_progress(organisation_id,site_id,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT(organisation_id,site_id) DO UPDATE SET data=excluded.data',(*scope(),dump(d)))
 def connrow(cid):
  r=query('SELECT * FROM alport_till_connections WHERE id=%s AND organisation_id=%s AND site_id=%s',(cid,*scope()),True)
  if not r:abort(404)
  return r
 def redirect_uri(provider):
  base=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
  if not base.startswith('https://') or '?' in base or '#' in base:raise tc.TillError('Administrator must set PUBLIC_BASE_URL to the HTTPS Alport address.')
  return base+'/setup/tills/callback/'+provider
 def authrow(state):
  u,s=context()
  r=query('SELECT * FROM alport_till_auth WHERE state=%s AND organisation_id=%s AND site_id=%s AND user_id=%s',(hashlib.sha256(state.encode()).hexdigest(),u['organisation_id'],s['id'],u['id']),True)
  if not r or datetime.fromisoformat(r['expires'])<now():raise tc.TillError('Connection request expired or belongs to another session. Start again.')
  if not secrets.compare_digest(session.get('till_state',''),state):raise tc.TillError('Connection session changed. Start again.')
  return r
 def pending(provider,token,state=None):
  token['_environment']=tc.square_base() if provider=='square' else tc.ls_base() if provider=='lightspeed' else 'production'
  loc=tc.locations(provider,token['access_token'])
  if not loc:raise tc.TillError('No accessible locations were returned by this provider.')
  u,s=context();state=state or secrets.token_urlsafe(32)
  with db() as c:
   c.execute('DELETE FROM alport_till_auth WHERE expires<%s',(now().isoformat(),))
   c.execute('INSERT INTO alport_till_auth(state,organisation_id,site_id,user_id,provider,expires,secret,locations) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(state) DO UPDATE SET secret=excluded.secret,locations=excluded.locations', (hashlib.sha256(state.encode()).hexdigest(),u['organisation_id'],s['id'],u['id'],provider,(now()+timedelta(minutes=15)).isoformat(),encrypt(token),dump(loc)))
  session['till_state']=state
  return state
 @bp.get('/setup')
 def page():
  session.setdefault('setup_csrf',secrets.token_urlsafe(32));u,s=context()
  return render_template('setup.html',csrf=session['setup_csrf'],site=s,steps=STEPS)
 @bp.get('/api/setup')
 def overview():
  rows=query('SELECT id,provider,label,status,last_sync,error FROM alport_till_connections WHERE organisation_id=%s AND site_id=%s',scope())
  providers=[{'id':k,'name':v['name'],'ready':k=='eposnow' or all(tc.credentials(k)),'auth':v['auth']} for k,v in tc.PROVIDERS.items()]
  return jsonify(progress=progress(),connections=rows,providers=providers)
 @bp.post('/api/setup/progress')
 def set_progress():
  d=request.get_json(silent=True) or {};key=d.get('step');status=d.get('status')
  if key not in [x[0] for x in STEPS] or status not in ('reviewed','skipped','pending'):raise tc.TillError('Choose a valid setup step and status.')
  if key=='till' and status=='reviewed':
   if not query("SELECT id FROM alport_till_connections WHERE organisation_id=%s AND site_id=%s AND status='Imported - reconcile totals'",scope()):raise tc.TillError('Import till data successfully before marking this step reviewed.')
  p=progress();p[key]=status;save_progress(p);return jsonify(ok=True)
 @bp.post('/api/setup/finish')
 def finish():
  p=progress()
  if any(p.get(x[0]) not in ('reviewed','skipped') for x in STEPS):raise tc.TillError('Review or explicitly skip each step first.')
  p['dismissed']=True;save_progress(p);return jsonify(ok=True)
 @bp.post('/api/setup/tills/start')
 def start():
  d=request.get_json(silent=True) or {};provider=d.get('provider')
  if provider not in tc.PROVIDERS:raise tc.TillError('Choose a supported till provider.')
  cipher()
  if provider=='eposnow':
   key,secret=str(d.get('key','')).strip(),str(d.get('secret','')).strip()
   if not key or not secret or len(key)+len(secret)>4096:raise tc.TillError('Enter the API key and secret supplied by Epos Now.')
   state=pending(provider,{'access_token':base64.b64encode((key+':'+secret).encode()).decode()})
   return jsonify(url='/setup?select='+state)
  cid,secret=tc.credentials(provider)
  if not cid or not secret:raise tc.TillError('This provider awaits administrator application approval and configuration.')
  state=secrets.token_urlsafe(32);u,s=context();session['till_state']=state
  with db() as c:
   c.execute('DELETE FROM alport_till_auth WHERE expires<%s',(now().isoformat(),))
   c.execute('INSERT INTO alport_till_auth(state,organisation_id,site_id,user_id,provider,expires) VALUES(%s,%s,%s,%s,%s,%s)',(hashlib.sha256(state.encode()).hexdigest(),u['organisation_id'],s['id'],u['id'],provider,(now()+timedelta(minutes=15)).isoformat()))
  params={'client_id':cid,'response_type':'code','state':state,'scope':tc.PROVIDERS[provider]['scope'],'redirect_uri':redirect_uri(provider)}
  return jsonify(url=tc.auth_urls(provider)[0]+'?'+urlencode(params))
 @bp.get('/setup/tills/callback/<provider>')
 def callback(provider):
  state=request.args.get('state','');r=authrow(state)
  if r['provider']!=provider or r['secret']:raise tc.TillError('Connection request was already used or does not match this provider.')
  if request.args.get('error') or not request.args.get('code'):raise tc.TillError('Till authorisation was declined. Return to /setup to retry.')
  # Consume before network exchange, so concurrent callbacks cannot exchange twice.
  with db() as c:
   row=c.execute('DELETE FROM alport_till_auth WHERE state=%s RETURNING state',(r['state'],)).fetchone()
   if not row:raise tc.TillError('Connection request was already used.')
  token=tc.exchange(provider,{'grant_type':'authorization_code','code':request.args['code'],'redirect_uri':redirect_uri(provider)})
  if not token.get('access_token'):raise tc.TillError('Provider did not return an access token.')
  pending(provider,token,state)
  return redirect('/setup?select='+state)
 @bp.get('/api/setup/tills/locations')
 def location_options():
  r=authrow(request.args.get('state',''))
  if not r['secret']:raise tc.TillError('Complete provider authorisation first.')
  return jsonify(locations=value(r['locations']),provider=r['provider'])
 @bp.post('/api/setup/tills/select')
 def select():
  d=request.get_json(silent=True) or {};r=authrow(str(d.get('state','')))
  if not r['secret']:raise tc.TillError('Complete provider authorisation first.')
  loc=next((x for x in value(r['locations']) if x['id']==str(d.get('location'))),None)
  if not loc or loc.get('currency')!='GBP':raise tc.TillError('Choose an authorised GBP location.')
  with db() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(r['organisation_id'],))
   old=c.execute('SELECT id,site_id FROM alport_till_connections WHERE organisation_id=%s AND provider=%s AND account=%s AND location=%s',(r['organisation_id'],r['provider'],loc['account'],loc['id'])).fetchone()
   if old:c.execute('SELECT pg_advisory_xact_lock(%s)',(-old['id'],))
   if old and old['site_id']!=r['site_id']:raise tc.TillError('This till location is already assigned to another Alport site.')
   used=c.execute('DELETE FROM alport_till_auth WHERE state=%s RETURNING state',(r['state'],)).fetchone()
   if not used:raise tc.TillError('Connection selection was already used.')
   c.execute("INSERT INTO alport_till_connections(organisation_id,site_id,provider,account,location,label,secret) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(organisation_id,provider,account,location) DO UPDATE SET secret=excluded.secret,status='Verified - awaiting import',error=''",(r['organisation_id'],r['site_id'],r['provider'],loc['account'],loc['id'],loc['name'],r['secret']))
  session.pop('till_state',None);return jsonify(ok=True)
 @bp.post('/api/setup/tills/<int:cid>/disconnect')
 def disconnect(cid):
  connrow(cid)
  with db() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(-cid,))
   c.execute("UPDATE alport_till_connections SET secret='',status='Disconnected',error='' WHERE id=%s",(cid,))
  return jsonify(ok=True)
 def sync(cid,start,end):
  # Serialize token rotation, disconnection and imports on this connection.
  # Network failure raises: the entire transaction rolls back, preserving old data.
  with db() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(-cid,))
   r=c.execute('SELECT * FROM alport_till_connections WHERE id=%s',(cid,)).fetchone()
   if not r or not r['secret']:raise tc.TillError('Connection is disconnected.')
   token=decrypt(r['secret'])
   environment=tc.square_base() if r['provider']=='square' else tc.ls_base() if r['provider']=='lightspeed' else 'production'
   if token.get('_environment',environment)!=environment:raise tc.TillError('Provider environment changed. Reconnect this till before importing.')
   if token.get('refresh_token'):
    fresh=tc.exchange(r['provider'],{'grant_type':'refresh_token','refresh_token':token['refresh_token']})
    if not fresh.get('access_token'):raise tc.TillError('Provider did not refresh the access token. Reconnect.')
    token.update(fresh)
    # Persist rotation independently before fetching, otherwise a failed import
    # could discard a single-use refresh token and permanently break the link.
    with db() as separate:separate.execute('UPDATE alport_till_connections SET secret=%s WHERE id=%s',(encrypt(token),cid))
   rows=tc.fetch_sales(r['provider'],token['access_token'],r['location'],start,end)
   for sale in rows:
    c.execute('INSERT INTO alport_till_sales(connection_id,external_id,sale_date,data) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(connection_id,external_id) DO UPDATE SET sale_date=excluded.sale_date,data=excluded.data',(cid,sale['id'],datetime.fromisoformat(sale['at']).astimezone(ZoneInfo('Europe/London')).date().isoformat(),dump(sale)))
   c.execute("UPDATE alport_till_connections SET status='Imported - reconcile totals',last_sync=%s,error='' WHERE id=%s",(now().isoformat(),cid))
   return len(rows)
 def run_sync(cid,start,end):
  try:return sync(cid,start,end)
  except (tc.TillError,ValueError,KeyError,TypeError) as e:
   message=str(e) if isinstance(e,tc.TillError) else 'Unexpected till data; import rolled back. Ask support to review the adapter.'
   with db() as c:c.execute('UPDATE alport_till_connections SET error=%s WHERE id=%s',(message,cid))
   raise tc.TillError(message) from None
 @bp.post('/api/setup/tills/<int:cid>/sync')
 def import_sales(cid):
  connrow(cid);d=request.get_json(silent=True) or {}
  try:a=date.fromisoformat(d['start']);b=date.fromisoformat(d['end'])
  except (KeyError,ValueError,TypeError):raise tc.TillError('Choose valid start and end dates.') from None
  if not 0<=(b-a).days<=31 or b>now().date():raise tc.TillError('Choose up to 32 days, ending no later than today.')
  return jsonify(imported=run_sync(cid,a.isoformat()+'T00:00:00Z',(b+timedelta(days=1)).isoformat()+'T00:00:00Z'))
 @bp.get('/api/setup/history')
 def history():
  try:a=date.fromisoformat(request.args.get('start',''));b=date.fromisoformat(request.args.get('end',''))
  except ValueError:raise tc.TillError('Choose valid history dates.') from None
  if not 0<=(b-a).days<=366:raise tc.TillError('Choose up to one year of history.')
  rows=query('SELECT s.data,s.sale_date FROM alport_till_sales s JOIN alport_till_connections c ON c.id=s.connection_id WHERE c.organisation_id=%s AND c.site_id=%s AND s.sale_date>=%s AND s.sale_date<=%s ORDER BY s.sale_date',(*scope(),a.isoformat(),b.isoformat()))
  days={};warnings=0
  for row in rows:
   x=value(row['data']);day=days.setdefault(row['sale_date'],{'date':row['sale_date'],'sales':0,'gross_pence':0,'tips_pence':0,'service_pence':0})
   day['sales']+=1
   for k in ('gross_pence','tips_pence','service_pence'):day[k]+=x[k]
   warnings+=bool(x['warnings'])
  return jsonify(days=list(days.values()),warnings=warnings,notice='Unreconciled till ledger. Not posted to finance, stock or payroll. Check source totals, refunds and service charges before use.')
 @app.cli.command('till-sync')
 @click.option('--days',default=3,type=click.IntRange(1,32))
 def scheduled_sync(days):
  """Run from a scheduler; keeps credentials out of the command line."""
  rows=query("SELECT c.id FROM alport_till_connections c JOIN subscriptions s ON s.organisation_id=c.organisation_id WHERE c.secret<>'' AND s.status='Active'")
  failures=0;end=now();start=end-timedelta(days=days)
  for r in rows:
   try:click.echo(f"Connection {r['id']}: {run_sync(r['id'],start.isoformat(),end.isoformat())} records")
   except tc.TillError as e:failures+=1;click.echo(f"Connection {r['id']}: {e}",err=True)
  if failures:raise click.ClickException(f'{failures} connection(s) failed; previous imports retained.')
 app.register_blueprint(bp)
 def needs_setup():
  u=host['user']();s=host['current_site']()
  if not u or not s or u['role'] not in ('Owner','Admin','General Manager','Manager'):return False
  sub=host['subscription_for'](u['organisation_id'])
  if not sub or sub.get('status')!='Active':return False
  r=query('SELECT data FROM alport_setup_progress WHERE organisation_id=%s AND site_id=%s',(u['organisation_id'],s['id']),True)
  return not (r and value(r['data']).get('dismissed'))
 app.extensions['alport_needs_setup']=needs_setup
