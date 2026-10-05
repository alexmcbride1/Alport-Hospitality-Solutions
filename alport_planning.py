"""Reviewed manual sales/event imports and weather-assisted forecasts.
CSV imports are immutable daily totals, not financial or payroll postings.
"""
import csv,io,json,math,secrets,hmac,hashlib,os
from collections import defaultdict
from datetime import date,datetime,timedelta,timezone
from flask import Blueprint,request,session,jsonify,render_template,g
from alport_inventory import converted
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_manual_sales(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,sale_date TEXT NOT NULL,menu_item_id BIGINT NOT NULL,
 quantity NUMERIC(16,6) NOT NULL,reference TEXT NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,sale_date,menu_item_id));
CREATE TABLE IF NOT EXISTS alport_manual_sales_corrections(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,sale_date TEXT NOT NULL,menu_item_id BIGINT NOT NULL,old_quantity NUMERIC NOT NULL,new_quantity NUMERIC NOT NULL,reason TEXT NOT NULL,actor_id BIGINT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_planning_settings(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,latitude NUMERIC,longitude NUMERIC,learned INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(organisation_id,site_id));
CREATE TABLE IF NOT EXISTS alport_weather_history(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,day TEXT NOT NULL,high NUMERIC NOT NULL,rain NUMERIC NOT NULL,
 location_key TEXT NOT NULL,source TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(organisation_id,site_id,day));
CREATE TABLE IF NOT EXISTS alport_external_events(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,external_id TEXT NOT NULL,event_id BIGINT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,external_id));
CREATE TABLE IF NOT EXISTS alport_planning_imports(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,kind TEXT NOT NULL,content_hash TEXT NOT NULL,
 row_count INTEGER NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,UNIQUE(organisation_id,site_id,kind,content_hash));
'''
def finite(v,low,high):
 try:n=float(v)
 except (ValueError,TypeError):raise ValueError('Enter a valid number.') from None
 if not math.isfinite(n) or not low<=n<=high:raise ValueError('Number outside the supported range.')
 return n
def read_csv(text,kind):
 if not isinstance(text,str) or len(text)>2_000_000:raise ValueError('CSV must be under 2 MB.')
 reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')));required={'sales':{'date','menu_id','quantity','reference'},'events':{'external_id','date','title','type'},'weather':{'date','high','rain_mm'}}[kind]
 if not required.issubset(set(reader.fieldnames or [])):raise ValueError('CSV needs columns: '+', '.join(sorted(required)))
 rows=[];seen=set()
 for row in reader:
  if len(rows)>=10000:raise ValueError('Split imports into at most 10,000 rows.')
  try:day=date.fromisoformat(row['date']).isoformat()
  except (TypeError,ValueError):raise ValueError('Dates must use YYYY-MM-DD.') from None
  if kind=='sales':
   try:mid=int(row['menu_id'])
   except (ValueError,TypeError):raise ValueError('Use the menu ID shown on this page.') from None
   qty=finite(row['quantity'],0,1000000);ref=str(row['reference'] or '').strip()
   if not ref or day>=date.today().isoformat():raise ValueError('Use completed past trading days and a source/report reference.')
   item={'day':day,'menu_id':mid,'quantity':qty,'reference':ref[:200]};key=(day,mid)
  elif kind=='events':
   ext=str(row['external_id'] or '').strip();title=str(row['title'] or '').strip();typ=str(row['type'] or '').strip()
   if not ext or not title or not typ:raise ValueError('Every event needs an external ID, title and type.')
   item={'day':day,'external_id':ext[:200],'title':title[:200],'type':typ[:100]};key=ext
  else:
   if day>=date.today().isoformat():raise ValueError('Historical weather must be before today.')
   item={'day':day,'high':finite(row['high'],-80,65),'rain':finite(row['rain_mm'],0,1000)};key=day
  if key in seen:raise ValueError('Duplicate row key in CSV.')
  seen.add(key);rows.append(item)
 if not rows:raise ValueError('No data rows found.')
 return rows

def solve(x,y):
 """Ridge regression, intercept + weekday controls + weather/event features."""
 n=len(x[0]);a=[[sum(row[i]*row[j] for row in x)+(1 if i==j and i else 0) for j in range(n)]+[sum(row[i]*v for row,v in zip(x,y))] for i in range(n)]
 for i in range(n):
  pivot=max(range(i,n),key=lambda j:abs(a[j][i]));a[i],a[pivot]=a[pivot],a[i]
  if abs(a[i][i])<1e-10:return None
  divisor=a[i][i];a[i]=[v/divisor for v in a[i]]
  for j in range(n):
   if j!=i:
    factor=a[j][i];a[j]=[v-factor*w for v,w in zip(a[j],a[i])]
 return [r[-1] for r in a]
def features(day,w,event):return [1.]+[float(date.fromisoformat(day).weekday()==i) for i in range(6)]+[float(w['high'])/20,min(float(w['rain']),30)/10,float(event)]
def calibrate(daily,weather,event_days):
 keys=sorted(k for k in daily if k in weather)
 if len(keys)<112:return {'usable':False,'reason':'Needs at least 112 observed sales days with matching historical weather.'}
 train,hold=keys[:-28],keys[-28:];x=[features(k,weather[k],k in event_days) for k in train];y=[float(daily[k]) for k in train]
 beta=solve(x,y)
 if not beta:return {'usable':False,'reason':'Historical inputs do not support a stable fit.'}
 # Fit base weekday model on the identical training window; keep holdout untouched.
 base=solve([r[:7] for r in x],y);den=sum(float(daily[k]) for k in hold)
 if not base or den<=0:return {'usable':False,'reason':'Insufficient nonzero held-out sales.'}
 pred=lambda coeff,row:max(0,sum(a*b for a,b in zip(coeff,row)))
 model_error=sum(abs(float(daily[k])-pred(beta,features(k,weather[k],k in event_days))) for k in hold)/den
 base_error=sum(abs(float(daily[k])-pred(base,features(k,weather[k],False)[:7])) for k in hold)/den
 valid=base_error>0 and model_error<base_error*.95
 return {'usable':valid,'reason':'Passed held-out comparison.' if valid else 'Weather/events did not improve held-out error by at least 5%.','coefficients':beta,'base_coefficients':base,'weather_event_wape':round(model_error*100,2),'weekday_wape':round(base_error*100,2),'training_days':len(train),'held_out_days':28,'training_end':train[-1],'validation_end':hold[-1]}
def register_planning(app,env):
 conn,q=env['conn'],env['q']
 with conn() as c:
  c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000002,))
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 bp=Blueprint('alport_planning',__name__)
 def context():
  u,s=env['user'](),env['current_site']()
  if not u or not s:raise ValueError('Sign in and choose a venue.')
  return u,s
 def lock(c,s):c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(s['id']),))
 def settings(u,s):return q('SELECT * FROM alport_planning_settings WHERE organisation_id=? AND site_id=?',(u['organisation_id'],s['id']),True) or {'latitude':None,'longitude':None,'learned':0,'version':0}
 def location(p):
  if p.get('latitude') is None or p.get('longitude') is None:raise ValueError('Save and confirm the venue coordinates first.')
  return f"{float(p['latitude']):.5f},{float(p['longitude']):.5f}"
 @bp.before_request
 def guard():
  u=env['user']()
  if not u:return jsonify(error='Sign in first.'),401
  if u['role'] not in ('Owner','Admin','Finance','General Manager','Manager'):return jsonify(error='Manager access required.'),403
  if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):return jsonify(error='Subscription needs attention.'),403
  if request.content_length and request.content_length>2_100_000:return jsonify(error='Import too large.'),413
  if request.method=='POST' and (not session.get('planning_csrf') or not hmac.compare_digest(session['planning_csrf'],request.headers.get('X-Planning-CSRF',''))):return jsonify(error='Reload this page.'),403
 @bp.errorhandler(ValueError)
 def invalid(e):return jsonify(error=str(e)),400
 @bp.get('/planning-data')
 def page():session.setdefault('planning_csrf',secrets.token_urlsafe(32));return render_template('planning_data.html',csrf=session['planning_csrf'])
 @bp.get('/api/planning-data')
 def overview():
  u,s=context();org=u['organisation_id'];sid=s['id'];p=settings(u,s)
  return jsonify(site=s['name'],settings=p,manual_sales=q('SELECT m.*,mi.name FROM alport_manual_sales m JOIN menu_items mi ON mi.id=m.menu_item_id WHERE m.organisation_id=? AND m.site_id=? ORDER BY m.sale_date DESC,m.menu_item_id LIMIT 100',(org,sid)),menu=q('SELECT id,name FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',(org,sid)),imports=q('SELECT kind,row_count,created_at FROM alport_planning_imports WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 30',(org,sid)),weather_days=q('SELECT COUNT(*) AS n FROM alport_weather_history WHERE organisation_id=? AND site_id=?',(org,sid),True)['n'],weather_service=bool(os.getenv('OPEN_METEO_API_KEY')))
 @bp.post('/api/planning-data/settings')
 def save_settings():
  u,s=context();d=request.get_json(silent=True) or {};lat=finite(d.get('latitude'),-90,90);lon=finite(d.get('longitude'),-180,180)
  if d.get('location_confirmed') is not True:raise ValueError('Confirm these coordinates identify your venue.')
  with conn() as c:
   lock(c,s);old=c.execute('SELECT version FROM alport_planning_settings WHERE organisation_id=%s AND site_id=%s',(u['organisation_id'],s['id'])).fetchone()
   if (old['version'] if old else 0)!=d.get('version'):raise ValueError('Settings changed. Refresh first.')
   c.execute('''INSERT INTO alport_planning_settings(organisation_id,site_id,latitude,longitude,learned) VALUES(%s,%s,%s,%s,%s)
    ON CONFLICT(organisation_id,site_id) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,learned=excluded.learned,version=alport_planning_settings.version+1''',(u['organisation_id'],s['id'],lat,lon,int(d.get('learned') is True)))
  return jsonify(ok=True)
 @bp.post('/api/planning-data/import/<kind>')
 def import_csv(kind):
  if kind not in ('sales','events','weather'):raise ValueError('Unknown import type.')
  u,s=context();org=u['organisation_id'];sid=s['id'];d=request.get_json(silent=True) or {};items=read_csv(d.get('csv'),kind);content=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
  with conn() as c:
   lock(c,s);old=c.execute('SELECT id FROM alport_planning_imports WHERE organisation_id=%s AND site_id=%s AND kind=%s AND content_hash=%s',(org,sid,kind,content)).fetchone()
   if old:return jsonify(replayed=True,id=old['id'])
   loc=location(settings(u,s)) if kind=='weather' else ''
   for r in items:
    if kind=='sales':
     if not c.execute('SELECT id FROM menu_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1',(r['menu_id'],org,sid)).fetchone():raise ValueError('A menu ID is unavailable at this venue.')
     if c.execute('SELECT 1 FROM alport_manual_sales WHERE organisation_id=%s AND site_id=%s AND sale_date=%s AND menu_item_id=%s',(org,sid,r['day'],r['menu_id'])).fetchone():raise ValueError('A daily total for this menu item already exists. Do not import it twice.')
     if c.execute('SELECT 1 FROM alport_till_sales t JOIN alport_till_connections tc ON tc.id=t.connection_id WHERE tc.organisation_id=%s AND tc.site_id=%s AND t.sale_date=%s LIMIT 1',(org,sid,r['day'])).fetchone():raise ValueError('Till data already exists for this day. Manual totals would double-count it.')
    elif kind=='events':
     if c.execute('SELECT 1 FROM alport_external_events WHERE organisation_id=%s AND site_id=%s AND external_id=%s',(org,sid,r['external_id'])).fetchone():r['skip']='Already imported; edit this event on the events page.'
   if not d.get('confirmed'):return jsonify(preview=items[:100],rows=len(items),fingerprint=content,note='Review the rows before saving. Manual sales are forecasting inputs only; they do not post stock movements.')
   if d.get('fingerprint')!=content:raise ValueError('Preview these exact rows before confirming.')
   for r in items:
    if r.get('skip'):continue
    if kind=='sales':c.execute('INSERT INTO alport_manual_sales(organisation_id,site_id,sale_date,menu_item_id,quantity,reference,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(org,sid,r['day'],r['menu_id'],r['quantity'],r['reference'],u['id'],env['now']()))
    elif kind=='events':
     eid=c.execute('INSERT INTO events(organisation_id,site_id,title,event_date,event_type,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(org,sid,r['title'],r['day'],r['type'],u['id'],env['now']())).fetchone()['id'];c.execute('INSERT INTO alport_external_events VALUES(%s,%s,%s,%s)',(org,sid,r['external_id'],eid))
    else:c.execute('''INSERT INTO alport_weather_history VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(organisation_id,site_id,day) DO UPDATE SET high=excluded.high,rain=excluded.rain,location_key=excluded.location_key,source=excluded.source,updated_at=excluded.updated_at''',(org,sid,r['day'],r['high'],r['rain'],loc,'Reviewed CSV',env['now']()))
   rid=c.execute('INSERT INTO alport_planning_imports(organisation_id,site_id,kind,content_hash,row_count,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(org,sid,kind,content,len(items),u['id'],env['now']())).fetchone()['id']
  return jsonify(id=rid,rows=len(items))
 @bp.post('/api/planning-data/sales/correct')
 def correct_sales():
  u,s=context();d=request.get_json(silent=True) or {};qty=finite(d.get('quantity'),0,1000000);prior=finite(d.get('previous_quantity'),0,1000000);reason=str(d.get('reason') or '').strip()
  if not reason or len(reason)>1000:raise ValueError('Record why the original report total needs correcting.')
  with conn() as c:
   lock(c,s);r=c.execute('SELECT * FROM alport_manual_sales WHERE organisation_id=%s AND site_id=%s AND sale_date=%s AND menu_item_id=%s',(u['organisation_id'],s['id'],d.get('date'),d.get('menu_id'))).fetchone()
   if not r or float(r['quantity'])!=prior:raise ValueError('The daily total changed or is unavailable. Refresh first.')
   c.execute('INSERT INTO alport_manual_sales_corrections(organisation_id,site_id,sale_date,menu_item_id,old_quantity,new_quantity,reason,actor_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(u['organisation_id'],s['id'],r['sale_date'],r['menu_item_id'],prior,qty,reason,u['id'],env['now']()))
   c.execute('UPDATE alport_manual_sales SET quantity=%s WHERE organisation_id=%s AND site_id=%s AND sale_date=%s AND menu_item_id=%s',(qty,u['organisation_id'],s['id'],r['sale_date'],r['menu_item_id']))
  return jsonify(ok=True)
 @bp.post('/api/planning-data/weather')
 def collect_weather():
  u,s=context();p=settings(u,s);location(p);key=os.getenv('OPEN_METEO_API_KEY','')
  if not key:raise ValueError('Configure a commercial Open-Meteo API key, or import authorised historical weather CSV.')
  from alport_jobs import refresh_weather
  try:
   with conn() as c:
    lock(c,s);refresh_weather(c,u['organisation_id'],s['id'],p['latitude'],p['longitude'],key)
  except Exception:raise ValueError('Weather refresh failed. Check the commercial key and confirmed coordinates.') from None
  return jsonify(ok=True)
 @bp.post('/api/planning-data/event-feed')
 def event_feed():
  # URLs come only from trusted server configuration, never from browser input.
  import requests
  u,s=context();feeds=json.loads(os.getenv('ALPORT_EVENT_FEEDS','{}'));url=feeds.get(str(s['id']))
  if not url or not url.startswith('https://'):raise ValueError('Configure an authorised HTTPS CSV event feed for this venue, or upload its CSV export.')
  try:
   response=requests.get(url,timeout=20,allow_redirects=False,stream=True);response.raise_for_status();raw=b''
   for chunk in response.iter_content(16384):
    raw+=chunk
    if len(raw)>2000000:raise ValueError()
   text=raw.decode('utf-8-sig');read_csv(text,'events')
  except Exception:raise ValueError('Event feed is unavailable or not the supported CSV format.') from None
  return jsonify(csv=text,note='Preview and confirm these events before import.')
 def manual_daily(org,sid,start,end,recipes,stocks):
  daily=defaultdict(lambda:defaultdict(float));days=set();excluded=0
  # If a till is connected later, its dates supersede manual totals to prevent duplication.
  rows=q('''SELECT m.* FROM alport_manual_sales m WHERE m.organisation_id=? AND m.site_id=? AND m.sale_date>=? AND m.sale_date<? AND NOT EXISTS
   (SELECT 1 FROM alport_till_sales t JOIN alport_till_connections tc ON tc.id=t.connection_id WHERE tc.organisation_id=m.organisation_id AND tc.site_id=m.site_id AND t.sale_date=m.sale_date)''',(org,sid,start.isoformat(),end.isoformat()))
  for r in rows:
   try:
    recipe=recipes.get(r['menu_item_id']);parts={}
    if not recipe:raise ValueError()
    for part in recipe:
     stock=stocks[part['stock_item_id']];parts[stock['id']]=parts.get(stock['id'],0)+float(converted(part['quantity'],part['unit'],stock['unit']))*float(r['quantity'])
   except (ValueError,KeyError,TypeError):excluded+=1;continue
   days.add(r['sale_date'])
   for iid,qty in parts.items():daily[iid][r['sale_date']]+=qty
  return {'daily':daily,'days':days,'excluded':excluded}
 app.extensions['alport_manual_daily']=manual_daily
 def learned(org,sid,iid,daily,today,prediction,bookings,profile,manual_weather,manual_event):
  cache=getattr(g,'alport_model_context',{})
  if (org,sid) not in cache:
   p=q('SELECT * FROM alport_planning_settings WHERE organisation_id=? AND site_id=?',(org,sid),True)
   if not p or not p['learned']:cache[(org,sid)]=None
   else:
    loc=location(p);history={r['day']:r for r in q('SELECT * FROM alport_weather_history WHERE organisation_id=? AND site_id=? AND location_key=?',(org,sid,loc))}
    events={r['event_date'] for r in q('SELECT event_date FROM events WHERE organisation_id=? AND site_id=?',(org,sid))}
    future=q('SELECT payload,fetched_at FROM alport_forecast_weather WHERE organisation_id=? AND site_id=?',(org,sid),True)
    cache[(org,sid)]=(loc,history,events,future)
   g.alport_model_context=cache
  context=cache[(org,sid)]
  if context is None:return prediction
  loc,history,events,future=context;model=calibrate(daily,history,events)
  prediction['learned_model']={k:v for k,v in model.items() if k not in ('coefficients','base_coefficients')}
  if not model['usable']:return prediction
  # Future forecast must use the same confirmed coordinates as historical weather.
  if not future:
   prediction['learned_model'].update(usable=False,reason='Refresh weather for the confirmed venue coordinates.');return prediction
  payload=json.loads(future['payload']) if isinstance(future['payload'],str) else future['payload']
  if payload.get('location_key')!=loc or (datetime.now(timezone.utc)-datetime.fromisoformat(future['fetched_at'])).total_seconds()>3600:
   prediction['learned_model'].update(usable=False,reason='Refresh a forecast for the confirmed coordinates.');return prediction
  weather={r['date']:r for r in payload.get('daily',[])}
  applied=0
  for row in prediction['daily_forecast']:
   w=weather.get(row['date'])
   if row['closed'] or not w or w.get('rain_mm') is None:continue
   x=features(row['date'],{'high':w['high'],'rain':w['rain_mm']},row['date'] in events);base=max(0,sum(a*b for a,b in zip(model['base_coefficients'],x[:7])))
   if base<=0:continue
   predicted=max(0,sum(a*b for a,b in zip(model['coefficients'],x)));factor=max(.5,min(1.5,predicted/base))
   # Learned effects replace configured weather/event effects, preserving manual adjustments and booking uplift.
   original=1+row['combined_adjustment_pct']/100;manual=1+max(-50,min(200,manual_weather+manual_event))/100
   row['units']=round(row['units']/original*manual*factor,6);row['learned_factor']=round(factor,4);applied+=1
  prediction['learned_model']['applied_days']=applied
  if not applied:prediction['learned_model'].update(usable=False,reason='No open forecast days with usable matching weather.')
  prediction['units']=round(sum(r['units'] for r in prediction['daily_forecast']),6);return prediction
 app.extensions['alport_learned_forecast']=learned
 @bp.after_request
 def private(response):response.headers['Cache-Control']='no-store';return response
 app.register_blueprint(bp)
