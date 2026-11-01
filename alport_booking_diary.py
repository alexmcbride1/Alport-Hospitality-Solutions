"""Manager-only reservation diary, stable guest VIPs and private site comments."""
from datetime import date,datetime,timedelta,timezone
import hmac,re,secrets
from flask import request,session,jsonify

ROLES=('Owner','Admin','General Manager','Manager')
STATUSES=('Confirmed','Arrived','Seated','Completed','Cancelled','No-show')
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_guest_vip(
 organisation_id BIGINT NOT NULL,guest_id BIGINT NOT NULL REFERENCES guests(id),
 vip INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 0,
 updated_by BIGINT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(organisation_id,guest_id));
CREATE TABLE IF NOT EXISTS alport_guest_site_comments(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 guest_id BIGINT NOT NULL REFERENCES guests(id),author_id BIGINT NOT NULL,body TEXT NOT NULL,
 request_key TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(organisation_id,site_id,guest_id,request_key));
CREATE INDEX IF NOT EXISTS alport_guest_comments_scope ON alport_guest_site_comments(organisation_id,site_id,guest_id,id);
CREATE INDEX IF NOT EXISTS alport_booking_guest_site ON bookings(organisation_id,site_id,guest_id);
CREATE INDEX IF NOT EXISTS alport_booking_diary_day ON bookings(organisation_id,site_id,booking_date,booking_time,id);
'''

def stamp():return datetime.now(timezone.utc).isoformat(timespec='microseconds')
def valid_day(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v):raise ValueError('Choose a valid date.')
    return date.fromisoformat(v).isoformat()
def valid_time(v):
    if not isinstance(v,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v):raise ValueError('Choose a valid time.')
    return v
def integer(v,lo,hi,label):
    if type(v) is not int or not lo<=v<=hi:raise ValueError(f'{label} must be between {lo} and {hi}.')
    return v

def register_booking_diary(app,env):
    q,conn=env['q'],env['conn']
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000041,))
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    def context():
        u=env['user']();s=env['current_site']()
        if not u:raise PermissionError('Sign in first.')
        if not s or u['role'] not in ROLES or s['organisation_id']!=u['organisation_id']:raise PermissionError('Booking manager access required.')
        if not any(x['id']==s['id'] and x['manager'] for x in app.extensions['alport_experience_sites'](u)):raise PermissionError('An active full management assignment at this site is required.')
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):raise PermissionError('Business subscription needs attention.')
        requested=request.headers.get('X-Booking-Site')
        if requested is not None and requested!=str(s['id']):raise PermissionError('The active site changed. Reload this page before continuing.')
        return u,s
    def token():
        session.setdefault('booking_csrf',secrets.token_urlsafe(32));return session['booking_csrf']
    @app.before_request
    def bookings_guard():
        if request.path=='/api/bookings' or request.path.startswith('/api/bookings/'):
            try:
                context()
                if request.method not in ('GET','HEAD','OPTIONS'):
                    if not hmac.compare_digest(token(),request.headers.get('X-Booking-CSRF','')):raise PermissionError('Reload bookings before saving.')
                    if request.content_length and request.content_length>64000:return jsonify(error='Booking request too large.'),413
            except PermissionError as e:return jsonify(error=str(e)),403 if env['user']() else 401
    @app.after_request
    def booking_headers(response):
        if request.path=='/api/bookings' or request.path.startswith('/api/bookings/'):
            response.headers['Cache-Control']='private, no-store';response.headers['X-Content-Type-Options']='nosniff';response.vary.add('Cookie')
        return response
    def protected(fn):
        from functools import wraps
        @wraps(fn)
        def call(*a,**kw):
            try:return fn(*a,**kw)
            except ValueError as e:return jsonify(error=str(e)),400
            except PermissionError as e:return jsonify(error=str(e)),403
        return call
    def guest(u,s,gid):
        g=q('''SELECT g.id,g.name,g.email,g.phone,g.marketing_consent FROM guests g WHERE g.id=? AND g.organisation_id=?
          AND EXISTS(SELECT 1 FROM bookings b WHERE b.guest_id=g.id AND b.organisation_id=g.organisation_id AND b.site_id=?)''',(gid,u['organisation_id'],s['id']),True)
        if not g:raise PermissionError('Guest not found at this site.')
        return g
    @app.get('/api/bookings/diary-config')
    @protected
    def diary_config():
        u,s=context();return jsonify(csrf=token(),site_id=s['id'],statuses=STATUSES)
    @protected
    def overview():
        u,s=context();day=valid_day(request.args.get('date') or date.today().isoformat());args=(u['organisation_id'],s['id'],day)
        rows=q('''SELECT b.id,b.guest_id,b.booking_date,b.booking_time,b.duration_minutes,b.party_size,b.status,b.source,b.table_id,
          b.guest_name,b.guest_email,b.guest_phone,b.special_requests,b.dietary_requirements,b.internal_notes,b.updated_at,
          b.deposit_status,b.deposit_required,b.deposit_paid,b.review_sent_at,rt.table_name,rt.area,
          COALESCE(v.vip,0) AS vip,COALESCE(v.version,0) AS vip_version,
          (SELECT COUNT(*) FROM alport_guest_site_comments n WHERE n.organisation_id=b.organisation_id AND n.site_id=b.site_id AND n.guest_id=b.guest_id) AS comment_count
          FROM bookings b LEFT JOIN restaurant_tables rt ON rt.id=b.table_id AND rt.organisation_id=b.organisation_id AND rt.site_id=b.site_id
          LEFT JOIN alport_guest_vip v ON v.guest_id=b.guest_id AND v.organisation_id=b.organisation_id
          WHERE b.organisation_id=? AND b.site_id=? AND b.booking_date=? ORDER BY b.booking_time,b.id LIMIT 5001''',args)
        active=[r for r in rows if r['status'] not in ('Cancelled','No-show')];peak={}
        for r in active:peak[r['booking_time']]=peak.get(r['booking_time'],0)+r['party_size']
        peak_time=max(peak,key=peak.get) if peak else '—'
        tables=q('SELECT id,table_name,area,min_capacity,max_capacity,active FROM restaurant_tables WHERE organisation_id=? AND site_id=? ORDER BY sort_order,table_name',args[:2])
        sessions=q('SELECT id,name,start_time,end_time FROM booking_sessions WHERE organisation_id=? AND site_id=? AND day_of_week=? AND active=1 ORDER BY start_time',(*args[:2],date.fromisoformat(day).weekday()))
        return jsonify(date=day,site_id=s['id'],csrf=token(),bookings=rows[:5000],truncated=len(rows)>5000,covers=sum(r['party_size'] for r in active),booking_count=len(active),peak_time=peak_time,peak_covers=peak.get(peak_time,0),tables=tables,sessions=sessions)
    app.view_functions['bookings_overview']=overview
    @protected
    def guests():
        u,s=context()
        term=str(request.args.get('search','')).strip()[:120];after=int(request.args.get('after','0'))
        if not 0<=after<=9223372036854775807:raise ValueError('Invalid guest page.')
        digits=re.sub(r'[^0-9]','',term)
        if digits.startswith('0044'):digits='0'+digits[4:]
        elif digits.startswith('44') and len(digits)==12:digits='0'+digits[2:]
        raw="regexp_replace(COALESCE(g.phone,''),'[^0-9]','','g')"
        phone=f"CASE WHEN substr({raw},1,4)='0044' THEN '0'||substring({raw} FROM 5) WHEN substr({raw},1,2)='44' AND length({raw})=12 THEN '0'||substring({raw} FROM 3) ELSE {raw} END"
        search=" AND (lower(g.name) LIKE ? ESCAPE '!'"+(f" OR {phone} LIKE ?" if digits else '')+")" if term else ''
        escaped=term.lower().replace('!','!!').replace('%','!%').replace('_','!_')
        params=[s['id'],u['organisation_id'],after]
        if term:params.append('%'+escaped+'%')
        if term and digits:params.append('%'+digits+'%')
        rows=q("""SELECT g.id,g.name,g.email,g.phone,g.marketing_consent,COALESCE(v.vip,0) AS vip,COALESCE(v.version,0) AS vip_version,
          COUNT(b.id) AS bookings,SUM(CASE WHEN b.status IN ('Completed','Seated') THEN 1 ELSE 0 END) AS visits,
          MAX(CASE WHEN b.status IN ('Completed','Seated') THEN b.booking_date ELSE NULL END) AS last_visit,
          SUM(CASE WHEN b.status='No-show' THEN 1 ELSE 0 END) AS no_shows
          FROM guests g JOIN bookings b ON b.guest_id=g.id AND b.organisation_id=g.organisation_id AND b.site_id=?
          LEFT JOIN alport_guest_vip v ON v.guest_id=g.id AND v.organisation_id=g.organisation_id
          WHERE g.organisation_id=? AND g.id>?"""+search+""" GROUP BY g.id,g.name,g.email,g.phone,g.marketing_consent,v.vip,v.version ORDER BY g.id LIMIT 51""",tuple(params))
        return jsonify(guests=rows[:50],has_more=len(rows)>50,next_after=rows[49]['id'] if len(rows)>50 else None,site_id=s['id'])
    app.view_functions['booking_guests']=guests
    @app.get('/api/bookings/guests/<int:gid>/profile')
    @protected
    def profile(gid):
        u,s=context();g=guest(u,s,gid)
        vip=q('SELECT vip,version FROM alport_guest_vip WHERE organisation_id=? AND guest_id=?',(u['organisation_id'],gid),True) or {'vip':0,'version':0}
        notes=q('''SELECT n.id,n.body,n.created_at,u.name AS author FROM alport_guest_site_comments n JOIN users u ON u.id=n.author_id
          WHERE n.organisation_id=? AND n.site_id=? AND n.guest_id=? ORDER BY n.id DESC LIMIT 101''',(u['organisation_id'],s['id'],gid))
        return jsonify(guest=g,**vip,comments=notes[:100],more_comments=len(notes)>100,site_id=s['id'])
    @app.post('/api/bookings/guests/<int:gid>/vip')
    @protected
    def vip(gid):
        u,s=context();guest(u,s,gid);d=request.get_json(silent=True)
        if not isinstance(d,dict):raise ValueError('Send valid guest fields.')
        if type(d.get('vip')) is not bool or type(d.get('version')) is not int:raise ValueError('Choose VIP on or off.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(756000000000+u['organisation_id'],))
            old=c.execute('SELECT version FROM alport_guest_vip WHERE organisation_id=%s AND guest_id=%s',(u['organisation_id'],gid)).fetchone()
            if d['version']!=(old['version'] if old else 0):return jsonify(error='VIP status changed. Reopen this guest.'),409
            c.execute('''INSERT INTO alport_guest_vip(organisation_id,guest_id,vip,version,updated_by,updated_at) VALUES(%s,%s,%s,1,%s,%s)
              ON CONFLICT(organisation_id,guest_id) DO UPDATE SET vip=excluded.vip,version=alport_guest_vip.version+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',(u['organisation_id'],gid,int(d['vip']),u['id'],stamp()))
        return jsonify(vip=d['vip'],version=d['version']+1)
    @app.post('/api/bookings/guests/<int:gid>/comments')
    @protected
    def comment(gid):
        u,s=context();guest(u,s,gid);d=request.get_json(silent=True)
        if not isinstance(d,dict):raise ValueError('Send valid comment fields.')
        body=d.get('body');key=d.get('request_key')
        if not isinstance(body,str) or not 1<=len(body.strip())<=2000 or any(ord(x)<32 and x not in '\n\t' for x in body):raise ValueError('Enter a comment of 1–2,000 characters.')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',key):raise ValueError('Reload before saving this comment.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(756000000000+u['organisation_id'],))
            old=c.execute('SELECT id,body FROM alport_guest_site_comments WHERE organisation_id=%s AND site_id=%s AND guest_id=%s AND request_key=%s',(u['organisation_id'],s['id'],gid,key)).fetchone()
            if old:
                if old['body']!=body.strip():return jsonify(error='Save key already used. Reopen the guest.'),409
                return jsonify(id=old['id'],duplicate=True)
            row=c.execute('INSERT INTO alport_guest_site_comments(organisation_id,site_id,guest_id,author_id,body,request_key,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(u['organisation_id'],s['id'],gid,u['id'],body.strip(),key,stamp())).fetchone()
        return jsonify(id=row['id']),201
    @protected
    def update(booking_id):
        u,s=context();d=request.get_json(silent=True)
        if not isinstance(d,dict):raise ValueError('Send valid booking fields.')
        fields={'status','booking_date','booking_time','party_size','duration_minutes','table_id','special_requests','dietary_requirements','internal_notes'}
        if set(d)-fields-{'expected_updated_at'}:raise ValueError('Unsupported booking field.')
        with conn() as c:
            # Same site lock used by the optional social overlap guard.
            c.execute('SELECT pg_advisory_xact_lock(%s)',(-9000000000000-s['id'],))
            b=c.execute('SELECT * FROM bookings WHERE id=%s AND organisation_id=%s AND site_id=%s FOR UPDATE',(booking_id,u['organisation_id'],s['id'])).fetchone()
            if not b:return jsonify(error='Booking not found.'),404
            if 'expected_updated_at' in d and d['expected_updated_at']!=b['updated_at']:return jsonify(error='This booking changed. Refresh before saving.'),409
            changes={k:v for k,v in d.items() if k in fields}
            if not changes:raise ValueError('Nothing to update.')
            merged={**b,**changes}
            valid_day(merged['booking_date']);valid_time(merged['booking_time']);integer(merged['party_size'],1,1000,'Covers');integer(merged['duration_minutes'],1,1440,'Duration')
            if merged['status'] not in STATUSES:raise ValueError('Choose a listed booking status.')
            for key in ('special_requests','dietary_requirements','internal_notes'):
                if key in changes and (not isinstance(changes[key],str) or len(changes[key])>4000):raise ValueError('Notes must be text up to 4,000 characters.')
            if merged['table_id'] in ('',None):merged['table_id']=None
            else:integer(merged['table_id'],1,9223372036854775807,'Table')
            if 'table_id' in changes:changes['table_id']=merged['table_id']
            allocation=bool(set(changes)&{'booking_date','booking_time','party_size','duration_minutes','table_id'}) or (b['status'] in ('Cancelled','No-show') and merged['status'] not in ('Cancelled','No-show'))
            if allocation and merged['status'] not in ('Cancelled','No-show'):
                cfg=c.execute('SELECT * FROM booking_settings WHERE organisation_id=%s AND site_id=%s',(u['organisation_id'],s['id'])).fetchone() or {}
                if merged['party_size']>int(cfg.get('max_party_size') or 1000):raise ValueError('Party exceeds this venue’s maximum size.')
                start=datetime.fromisoformat(merged['booking_date']+'T'+merged['booking_time']);gap=int(cfg.get('turnaround_minutes') or 0);end=start+timedelta(minutes=merged['duration_minutes']+gap)
                others=c.execute("SELECT id,booking_date,booking_time,duration_minutes,party_size,table_id FROM bookings WHERE organisation_id=%s AND site_id=%s AND id<>%s AND booking_date>=%s AND booking_date<=%s AND status NOT IN ('Cancelled','No-show')",(u['organisation_id'],s['id'],booking_id,(start.date()-timedelta(days=2)).isoformat(),(end.date()+timedelta(days=1)).isoformat())).fetchall()
                interval=max(1,int(cfg.get('booking_interval') or 15));slot=(start.hour*60+start.minute)//interval
                covers=sum(x['party_size'] for x in others if x['booking_date']==merged['booking_date'] and env['minutes_of'](x['booking_time'])//interval==slot)
                if covers+merged['party_size']>int(cfg.get('max_covers_per_interval') or 10000):return jsonify(error='Arrival interval cover limit exceeded.'),409
                if merged['table_id']:
                    t=c.execute('SELECT * FROM restaurant_tables WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1',(merged['table_id'],u['organisation_id'],s['id'])).fetchone()
                    if not t or not t['min_capacity']<=merged['party_size']<=t['max_capacity']:raise ValueError('Table must be active at this site and fit the party.')
                    for x in others:
                        a=datetime.fromisoformat(x['booking_date']+'T'+x['booking_time'][:5]);z=a+timedelta(minutes=x['duration_minutes']+gap)
                        if x['table_id']==merged['table_id'] and start<z and a<end:return jsonify(error='Table conflicts with another booking including turnaround.'),409
            timestamp=stamp();changes['updated_at']=timestamp
            if 'status' in changes:
                field={'Arrived':'arrived_at','Seated':'seated_at','Completed':'completed_at','Cancelled':'cancelled_at'}.get(changes['status'])
                if field:changes[field]=timestamp
            c.execute('UPDATE bookings SET '+','.join(k+'=%s' for k in changes)+' WHERE id=%s AND organisation_id=%s AND site_id=%s',(*changes.values(),booking_id,u['organisation_id'],s['id']))
        env['audit']('Updated','booking',booking_id,'Diary fields: '+', '.join(sorted(k for k in changes if k!='updated_at')))
        return jsonify(ok=True,updated_at=timestamp)
    app.view_functions['update_booking']=update
    app.extensions['alport_booking_manager']=context
