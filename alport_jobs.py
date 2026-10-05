"""Opt-in background jobs. Run `flask --app app alport-jobs` periodically.
No external delivery is enabled merely by uploading this module.
"""
import os,json,time,hashlib
from datetime import datetime,timezone,date,timedelta
import click,requests

def refresh_weather(c,org,sid,latitude,longitude,key):
 from alport_planning import finite
 loc=f'{float(latitude):.5f},{float(longitude):.5f}';stamp=datetime.now(timezone.utc).isoformat()
 p={'latitude':float(latitude),'longitude':float(longitude),'daily':'temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max','timezone':'Europe/London','forecast_days':14,'apikey':key}
 response=requests.get('https://customer-api.open-meteo.com/v1/forecast',params=p,timeout=30,allow_redirects=False);response.raise_for_status();d=response.json()['daily'];future=[]
 for i,day in enumerate(d['time']):
  if d['temperature_2m_max'][i] is None:continue
  date.fromisoformat(day)
  future.append({'date':day,'high':finite(d['temperature_2m_max'][i],-80,65),'low':d['temperature_2m_min'][i],'rain_mm':finite(d['precipitation_sum'][i],0,1000),'rain_probability':d['precipitation_probability_max'][i],'description':''})
 site=c.execute('SELECT name,address FROM sites WHERE id=%s AND organisation_id=%s',(sid,org)).fetchone()
 from alport_forecast import signature
 payload={'location':loc,'location_key':loc,'daily':future,'source':'Open-Meteo commercial forecast'}
 c.execute('''INSERT INTO alport_forecast_weather(organisation_id,site_id,payload,fetched_at,site_signature) VALUES(%s,%s,%s::jsonb,%s,%s)
 ON CONFLICT(organisation_id,site_id) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at,site_signature=excluded.site_signature''',(org,sid,json.dumps(payload),stamp,signature(dict(site))))
 end=date.today()-timedelta(days=6)
 p={'latitude':float(latitude),'longitude':float(longitude),'daily':'temperature_2m_max,precipitation_sum','timezone':'Europe/London','start_date':(end-timedelta(days=364)).isoformat(),'end_date':end.isoformat(),'apikey':key}
 last=c.execute('SELECT MAX(updated_at) AS latest FROM alport_weather_history WHERE organisation_id=%s AND site_id=%s AND location_key=%s',(org,sid,loc)).fetchone()
 if last and last['latest'] and last['latest'][:10]==date.today().isoformat():return
 response=requests.get('https://customer-archive-api.open-meteo.com/v1/archive',params=p,timeout=30,allow_redirects=False);response.raise_for_status();d=response.json()['daily']
 for day,high,rain in zip(d['time'],d['temperature_2m_max'],d['precipitation_sum']):
  if high is None or rain is None:continue
  date.fromisoformat(day)
  c.execute('''INSERT INTO alport_weather_history VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(organisation_id,site_id,day) DO UPDATE SET high=excluded.high,rain=excluded.rain,location_key=excluded.location_key,source=excluded.source,updated_at=excluded.updated_at''',(org,sid,day,finite(high,-80,65),finite(rain,0,1000),loc,'Open-Meteo reanalysis',stamp))

def register_jobs(app,env):
 @app.cli.command('alport-jobs')
 def jobs():
  """One bounded pass; schedule every 15 minutes. Use --help before configuring."""
  conn=env['conn'];now=lambda:datetime.now(timezone.utc).isoformat();count=0;errors=0
  # One worker at a time, including external sends. Session lock is released with connection.
  with conn() as c:
   acquired=c.execute('SELECT pg_try_advisory_lock(%s)',(739000000001,)).fetchone()
   if not acquired or not next(iter(acquired.values())):click.echo('Another job is running.');return
   try:
    for sh in c.execute('''SELECT sh.*,a.user_id FROM shifts sh JOIN alport_staff_access a ON a.employee_id=sh.employee_id
      WHERE sh.shift_date>=%s AND sh.status='Scheduled' AND a.profile<>'disabled' AND a.activated_at IS NOT NULL''',(date.today().isoformat(),)).fetchall():
     key='shift-'+str(sh['id'])+'-'+hashlib.sha256(json.dumps({k:sh[k] for k in ('employee_id','shift_date','start_time','end_time','status')},sort_keys=True).encode()).hexdigest()[:20]
     env['app'].extensions['alport_notify'](c,sh['organisation_id'],sh['site_id'],sh['user_id'],key,'Your rota has been updated. Sign in to review it.','/staff')
    c.commit()
    if os.getenv('ALPORT_SEND_NOTIFICATIONS')=='1':
     rows=c.execute('''SELECT n.*,u.email,p.email_enabled,p.push_enabled FROM alport_notifications n JOIN users u ON u.id=n.user_id JOIN alport_notification_preferences p ON p.user_id=u.id
      WHERE u.active=1 AND n.attempts<5 AND n.created_at>=%s AND ((p.email_enabled=1 AND n.email_sent_at IS NULL) OR (p.push_enabled=1 AND n.push_sent_at IS NULL)) ORDER BY n.id LIMIT 50''',((datetime.now(timezone.utc)-timedelta(hours=20)).isoformat(),)).fetchall()
     for r in rows:
      try:
       memberships=c.execute("SELECT a.profile,a.activated_at,e.active FROM alport_staff_access a JOIN employees e ON e.id=a.employee_id WHERE a.user_id=%s AND a.site_id=%s",(r['user_id'],r['site_id'])).fetchall()
       if memberships and not any(a['profile']!='disabled' and a['activated_at'] and a['active'] for a in memberships):continue
       if r['email_enabled'] and not r['email_sent_at']:
        key=os.getenv('RESEND_API_KEY');sender=os.getenv('FROM_EMAIL') or os.getenv('RESEND_FROM_EMAIL')
        if not key or not sender:raise ValueError('Email configuration incomplete')
        response=requests.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+key,'Idempotency-Key':'alport-staff-'+str(r['id'])},json={'from':sender,'to':[r['email']],'subject':'Your Alport staff update','text':r['message']+'\n'+env['_public_base_url']()+r['url']},timeout=20,allow_redirects=False);response.raise_for_status()
        c.execute('UPDATE alport_notifications SET email_sent_at=%s WHERE id=%s',(now(),r['id']));c.commit()
       if r['push_enabled'] and not r['push_sent_at']:
        from pywebpush import webpush,WebPushException
        private=os.getenv('VAPID_PRIVATE_KEY');contact=os.getenv('VAPID_CONTACT')
        if not private or not contact:raise ValueError('Push configuration incomplete')
        for sub in c.execute('SELECT * FROM alport_push_subscriptions WHERE user_id=%s',(r['user_id'],)).fetchall():
         try:webpush(subscription_info=json.loads(env['decrypt_staff'](sub['payload'])),data=json.dumps({'message':r['message'],'id':r['id']}),vapid_private_key=private,vapid_claims={'sub':contact},timeout=15)
         except WebPushException as exc:
          if exc.response is not None and exc.response.status_code in (404,410):c.execute('DELETE FROM alport_push_subscriptions WHERE id=%s',(sub['id'],))
          else:raise
        c.execute('UPDATE alport_notifications SET push_sent_at=%s WHERE id=%s',(now(),r['id']));c.commit()
       count+=1
      except Exception:
       c.rollback();c.execute("UPDATE alport_notifications SET attempts=attempts+1,last_error='Delivery failed; check provider configuration/logs without exposing credentials.' WHERE id=%s",(r['id'],));c.commit();errors+=1
    key=os.getenv('OPEN_METEO_API_KEY')
    if key:
     for p in c.execute('SELECT * FROM alport_planning_settings WHERE latitude IS NOT NULL AND longitude IS NOT NULL').fetchall():
      try:
       c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(p['site_id']),));refresh_weather(c,p['organisation_id'],p['site_id'],p['latitude'],p['longitude'],key);c.commit()
      except Exception:c.rollback();errors+=1
   finally:c.execute('SELECT pg_advisory_unlock(%s)',(739000000001,));c.commit()
  click.echo(f'Job pass complete: {count} notifications processed; {errors} failures. No supplier orders or payments sent.')
  if errors:raise click.ClickException('Some jobs failed; inspect account configuration and retry.')
