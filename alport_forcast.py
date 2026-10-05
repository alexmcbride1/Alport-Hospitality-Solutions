"""Explainable longer-history forecasts and venue-specific planning context."""
import json,math,secrets,hmac,hashlib
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from flask import Blueprint,request,session,jsonify

SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_forecast_profiles(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,payload JSONB NOT NULL,updated_by BIGINT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id));
CREATE TABLE IF NOT EXISTS alport_forecast_weather(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,payload JSONB NOT NULL,fetched_at TEXT NOT NULL,site_signature TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id));
'''
DEFAULTS={'history_days':84,'closed_weekdays':[],'date_overrides':{},'weather_enabled':False,'warm_threshold':20,'warm_pct':0,'rain_threshold':60,'rain_pct':0,'event_adjustments':{},'reason':''}
def unpack(x):return json.loads(x) if isinstance(x,str) else x
def local_now():return datetime.now(ZoneInfo('Europe/London'))
def signature(s):return hashlib.sha256(json.dumps([s.get('name'),s.get('address')]).encode()).hexdigest()
def finite(v,label,lo,hi):
 try:n=float(v)
 except (ValueError,TypeError):raise ValueError(label+' must be a number.') from None
 if not math.isfinite(n) or not lo<=n<=hi:raise ValueError(f'{label} must be between {lo} and {hi}.')
 return n

def validate_profile(d,event_ids):
 if not isinstance(d,dict):raise ValueError('Send valid planning settings.')
 try:history=int(str(d.get('history_days',84)))
 except ValueError:raise ValueError('Choose a supported history window.') from None
 if history not in (28,84,182,365):raise ValueError('History must be 28, 84, 182 or 365 days.')
 closed=d.get('closed_weekdays',[])
 if not isinstance(closed,list) or any(type(x)!=int or x<0 or x>6 for x in closed) or len(set(closed))==7:raise ValueError('Choose valid closed weekdays, leaving at least one trading day.')
 overrides=d.get('date_overrides',{})
 if not isinstance(overrides,dict) or len(overrides)>60:raise ValueError('Use at most 60 dated opening exceptions.')
 for day,state in overrides.items():
  try:date.fromisoformat(day)
  except (ValueError,TypeError):raise ValueError('Invalid opening exception date.') from None
  if state not in ('open','closed'):raise ValueError('Opening exceptions must be open or closed.')
 events=d.get('event_adjustments',{})
 if not isinstance(events,dict) or len(events)>100:raise ValueError('Invalid event adjustments.')
 for key,pct in events.items():
  if key not in event_ids:raise ValueError('An adjusted event is unavailable at this venue.')
  finite(pct,'Event adjustment',-50,200)
 reason=str(d.get('reason') or '').strip()[:1000]
 if not reason:raise ValueError('Explain the basis for these planning settings.')
 if type(d.get('weather_enabled',False))!=bool:raise ValueError('Choose whether weather adjustments are enabled.')
 return {'history_days':history,'closed_weekdays':sorted(set(closed)),'date_overrides':overrides,'weather_enabled':d.get('weather_enabled',False),
  'warm_threshold':finite(d.get('warm_threshold',20),'Warm-day threshold',-20,50),'warm_pct':finite(d.get('warm_pct',0),'Warm-day adjustment',-50,200),
  'rain_threshold':finite(d.get('rain_threshold',60),'Rain probability threshold',0,100),'rain_pct':finite(d.get('rain_pct',0),'Rain adjustment',-50,200),
  'event_adjustments':{k:float(v) for k,v in events.items()},'reason':reason}

def baseline(daily,target,history_days):
 rows=[(date.fromisoformat(k),float(v)) for k,v in daily.items() if target-timedelta(days=history_days)<=date.fromisoformat(k)<target]
 def mean(values):return sum(values)/len(values) if values else None
 yesterday=next((v for d,v in rows if d==target-timedelta(days=1)),None)
 week=mean([v for d,v in rows if d>=target-timedelta(days=7)]);month=mean([v for d,v in rows if d>=target-timedelta(days=28)]);long=mean([v for d,v in rows])
 parts=[(yesterday,.1),(week,.2),(month,.3),(long,.4)];valid=[(v,w) for v,w in parts if v is not None]
 avg=sum(v*w for v,w in valid)/sum(w for v,w in valid) if valid else 0
 weekday=[v for d,v in rows if d.weekday()==target.weekday()]
 prediction=(avg+mean(weekday))/2 if len(weekday)>=2 else avg
 return {'base':prediction,'weighted_base':avg,'day':yesterday,'week':week,'month':month,'long':long,'observed_days':len(rows)}

def backtest(daily,today,history_days):
 """Rolling one-day checks use only observations strictly before each held-out date."""
 targets=sorted(k for k in daily if today-timedelta(days=14)<=date.fromisoformat(k)<today)
 errors=[];actuals=[];naive_errors=[]
 for key in targets:
  target=date.fromisoformat(key);b=baseline(daily,target,history_days)
  if b['observed_days']<14:continue
  actual=float(daily[key]);errors.append(abs(actual-b['base']));actuals.append(actual)
  previous=[(k,float(v)) for k,v in daily.items() if target-timedelta(days=history_days)<=date.fromisoformat(k)<target and date.fromisoformat(k).weekday()==target.weekday()]
  naive_errors.append(abs(actual-sorted(previous)[-1][1]) if previous else None)
 denominator=sum(actuals)
 return {'days':len(errors),'wape_pct':round(100*sum(errors)/denominator,1) if len(errors)>=7 and denominator>0 else None,
  'weekday_naive_wape_pct':round(100*sum(naive_errors)/denominator,1) if len(errors)>=7 and denominator>0 and all(x is not None for x in naive_errors) else None,
  'note':'Historical sales-only one-day comparison; excludes weather and bookings. Not a guarantee of future accuracy.'}

def estimate_history(daily,today,horizon,bookings,manual_weather,manual_event,profile,weather_days,events):
 history=profile['history_days'];b=baseline(daily,today,history);result=[]
 eligible=[(date.fromisoformat(k),v) for k,v in daily.items() if today-timedelta(days=history)<=date.fromisoformat(k)<today]
 covers=[bookings.get(d.isoformat(),0) for d,_ in eligible]
 all_covers=sum(covers)/len(covers) if covers else 0
 for offset in range(horizon):
  target=today+timedelta(days=offset);key=target.isoformat();closed=target.weekday() in profile['closed_weekdays'];override=profile['date_overrides'].get(key)
  if override:closed=override=='closed'
  # Future targets use historical observations only; no recursive invented sales.
  weekday=[float(v) for d,v in eligible if d.weekday()==target.weekday()]
  base=(b['weighted_base']+sum(weekday)/len(weekday))/2 if len(weekday)>=2 else b['weighted_base']
  comparable=[bookings.get(d.isoformat(),0) for d,_ in eligible if d.weekday()==target.weekday()]
  reference=sum(comparable)/len(comparable) if len(comparable)>=2 else all_covers
  ratio=min(2,max(1,bookings.get(key,0)/reference)) if reference>0 else 1
  w=weather_days.get(key);wpct=0
  if profile['weather_enabled'] and w:
   if w.get('high') is not None and float(w['high'])>=profile['warm_threshold']:wpct+=profile['warm_pct']
   if w.get('rain_probability') is not None and float(w['rain_probability'])>=profile['rain_threshold']:wpct+=profile['rain_pct']
  epct=sum(profile['event_adjustments'].get(str(e['id']),0) for e in events if e['event_date']==key)
  # Combined configured/manual effects are bounded per day, not multiplied without limit.
  pct=max(-50,min(200,wpct+epct+manual_weather+manual_event))
  units=0 if closed else max(0,base*ratio*(1+pct/100))
  result.append({'date':key,'units':round(units,6),'closed':closed,'booking_covers':bookings.get(key,0),'booking_factor':round(ratio,3),'weather_pct':wpct,'event_pct':epct,'combined_adjustment_pct':pct,'weather_available':bool(w)})
 return {**{k:v for k,v in b.items() if k not in ('base','weighted_base')},'units':round(sum(x['units'] for x in result),6),'booking_factor':max(x['booking_factor'] for x in result),'daily_forecast':result,'backtest':backtest(daily,today,history)}

def get_profile(q,org,sid):
 row=q('SELECT payload FROM alport_forecast_profiles WHERE organisation_id=? AND site_id=?',(org,sid),True)
 return {**DEFAULTS,**(unpack(row['payload']) if row else {})}
def get_weather(q,org,sid,site):
 row=q('SELECT * FROM alport_forecast_weather WHERE organisation_id=? AND site_id=?',(org,sid),True)
 if not row:return None
 row=dict(row);row['payload']=unpack(row['payload']);row['usable']=row['site_signature']==signature(site) and 0<=(local_now()-datetime.fromisoformat(row['fetched_at'])).total_seconds()<3600
 return row

def register_forecast(app,env):
 conn,q=env['conn'],env['q']
 with conn() as c:
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 bp=Blueprint('alport_forecast',__name__)
 def context():
  u,s=env['user'](),env['current_site']()
  if not u or not s:raise ValueError('Sign in and select a venue.')
  return u,s,u['organisation_id'],s['id']
 @bp.before_request
 def protect():
  if not env['user']():return jsonify(error='Sign in first.'),401
  if request.method=='POST':
   if env['user']()['role'] not in ('Owner','Admin','Finance','General Manager','Manager'):return jsonify(error='Manager access required.'),403
   if not session.get('demand_csrf') or not hmac.compare_digest(session['demand_csrf'],request.headers.get('X-Demand-CSRF','')):return jsonify(error='Reload the planning page.'),403
   if request.content_length and request.content_length>20000:return jsonify(error='Settings too large.'),413
 @bp.errorhandler(ValueError)
 def invalid(e):return jsonify(error=str(e)),400
 @bp.get('/api/demand/context')
 def context_get():
  u,s,org,sid=context();today=local_now().date()
  return jsonify(profile=get_profile(q,org,sid),weather=get_weather(q,org,sid,s),events=q('SELECT id,title,event_date,event_type FROM events WHERE organisation_id=? AND site_id=? AND event_date>=? ORDER BY event_date LIMIT 100',(org,sid,today.isoformat())))
 @bp.post('/api/demand/context')
 def context_save():
  u,s,org,sid=context();events={str(x['id']) for x in q('SELECT id FROM events WHERE organisation_id=? AND site_id=?',(org,sid))}
  profile=validate_profile(request.get_json(silent=True),events)
  with conn() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(sid),))
   c.execute('''INSERT INTO alport_forecast_profiles(organisation_id,site_id,payload,updated_by,updated_at) VALUES(%s,%s,%s::jsonb,%s,%s)
    ON CONFLICT(organisation_id,site_id) DO UPDATE SET payload=excluded.payload,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',(org,sid,json.dumps(profile),u['id'],env['now']()))
  return jsonify(ok=True)
 @bp.post('/api/demand/context/weather')
 def weather_refresh():
  u,s,org,sid=context()
  if request.query_string:raise ValueError('Use the saved venue location for planning weather.')
  cached=get_weather(q,org,sid,s)
  if cached and cached['usable'] and (local_now()-datetime.fromisoformat(cached['fetched_at'])).total_seconds()<300:return jsonify(cached)
  if 'weather' not in env:raise ValueError('Venue weather service is not installed.')
  response=app.make_response(env['weather']());data=response.get_json(silent=True)
  if response.status_code!=200 or not isinstance(data,dict) or not isinstance(data.get('daily'),list):raise ValueError('Weather could not be loaded. Check the venue location and try again.')
  clean=[]
  for day in data['daily'][:14]:
   try:
    date.fromisoformat(day['date']);high=None if day.get('high') is None else finite(day['high'],'Forecast temperature',-100,70);rain=None if day.get('rain_probability') is None else finite(day['rain_probability'],'Rain probability',0,100)
   except (ValueError,KeyError,TypeError):continue
   clean.append({'date':day['date'],'high':high,'low':day.get('low'),'rain_probability':rain,'description':str(day.get('description',''))[:100]})
  if not clean:raise ValueError('Weather service returned no usable forecast days.')
  data={'location':str(data.get('location',''))[:300],'daily':clean,'source':'Open-Meteo'};stamp=local_now().isoformat()
  with conn() as c:
   c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(sid),))
   c.execute('''INSERT INTO alport_forecast_weather(organisation_id,site_id,payload,fetched_at,site_signature) VALUES(%s,%s,%s::jsonb,%s,%s)
    ON CONFLICT(organisation_id,site_id) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at,site_signature=excluded.site_signature''',(org,sid,json.dumps(data),stamp,signature(s)))
  return jsonify(get_weather(q,org,sid,s))
 app.register_blueprint(bp)
