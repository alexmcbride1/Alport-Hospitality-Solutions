"""Site-specific guest booking links, with no access to the management workspace."""
import hashlib,hmac,re,secrets,time
from datetime import timedelta
from flask import request,session,jsonify,render_template,redirect,url_for,abort
from itsdangerous import URLSafeTimedSerializer,BadSignature
import social_booking as availability
from alport_social import GUARD_SQL

SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_public_booking_links(
 site_id BIGINT PRIMARY KEY REFERENCES sites(id),organisation_id BIGINT NOT NULL,
 slug TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 0,contact_phone TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS alport_public_booking_requests(
 request_key TEXT PRIMARY KEY,site_id BIGINT NOT NULL,booking_id BIGINT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_public_booking_limits(
 identity_hash TEXT PRIMARY KEY,window_start BIGINT NOT NULL,attempts INTEGER NOT NULL);
'''

def register_public_bookings(app,env):
    db,q=env['conn'],env['q']
    signer=URLSafeTimedSerializer(app.secret_key,salt='public-reservation-form-v1')
    with db() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000042,))
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    def manager():return app.extensions['alport_booking_manager']()
    def link_for(u,s):
        with db() as c:
            c.execute('INSERT INTO alport_public_booking_links(site_id,organisation_id,slug) VALUES(%s,%s,%s) ON CONFLICT(site_id) DO NOTHING',(s['id'],u['organisation_id'],secrets.token_urlsafe(18)))
        return q('SELECT * FROM alport_public_booking_links WHERE site_id=? AND organisation_id=?',(s['id'],u['organisation_id']),True)
    def share_data(row,s):
        base=env['_public_base_url']()
        return dict(url=base+'/book/'+row['slug'],enabled=bool(row['enabled']),site_name=s['name'],contact_phone=row['contact_phone'])
    @app.get('/api/bookings/website-link')
    def website_link():
        u,s=manager();return jsonify(**share_data(link_for(u,s),s))
    @app.post('/api/bookings/website-link')
    def website_link_save():
        u,s=manager();d=request.get_json(silent=True)
        if not isinstance(d,dict) or type(d.get('enabled')) is not bool:return jsonify(error='Choose whether online bookings are enabled.'),400
        phone=d.get('contact_phone','')
        if not isinstance(phone,str) or len(phone)>60 or not re.fullmatch(r'[0-9+().\s-]*',phone):return jsonify(error='Enter a valid venue contact number.'),400
        row=link_for(u,s)
        if d['enabled']:
            cfg=env['booking_settings_for'](u['organisation_id'],s['id'])
            if not phone.strip():return jsonify(error='Add the pub contact number for guests who need assistance.'),400
            if not q('SELECT id FROM restaurant_tables WHERE organisation_id=? AND site_id=? AND active=1',(u['organisation_id'],s['id']),True) or not q('SELECT id FROM booking_sessions WHERE organisation_id=? AND site_id=? AND active=1',(u['organisation_id'],s['id']),True):return jsonify(error='Add active tables and bookable sessions before enabling the link.'),400
            if not 5<=int(cfg['booking_interval'])<=120 or int(cfg['default_duration'])<15:return jsonify(error='Check the booking interval and sitting duration first.'),400
        with db() as c:
            if d['enabled']:
                # Existing social-booking safety guard: all booking writers use the same site lock.
                c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000043,))
                c.execute(GUARD_SQL)
                if not c.execute("SELECT 1 AS ready FROM pg_trigger WHERE tgname='alport_booking_overlap_guard' AND tgrelid='bookings'::regclass AND NOT tgisinternal").fetchone():
                    c.execute('CREATE TRIGGER alport_booking_overlap_guard BEFORE INSERT OR UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION alport_guard_booking_overlap()')
                c.execute('ALTER TABLE bookings ENABLE TRIGGER alport_booking_overlap_guard')
            c.execute('SELECT pg_advisory_xact_lock(%s)',(-9000000000000-s['id'],))
            c.execute('UPDATE alport_public_booking_links SET enabled=%s,contact_phone=%s WHERE site_id=%s AND organisation_id=%s',(int(d['enabled']),phone.strip(),s['id'],u['organisation_id']))
        env['audit']('Updated','booking website link',s['id'],'Enabled' if d['enabled'] else 'Paused')
        row.update(enabled=int(d['enabled']),contact_phone=phone.strip());return jsonify(**share_data(row,s))
    def public_site(slug):
        row=q('''SELECT l.site_id,l.organisation_id,l.slug,l.enabled,l.contact_phone,s.name,s.address
          FROM alport_public_booking_links l JOIN sites s ON s.id=l.site_id AND s.organisation_id=l.organisation_id
          WHERE l.slug=? AND s.active=1''',(slug,),True)
        if not row:abort(404)
        if env['subscription_blocks_access'](env['subscription_for'](row['organisation_id'])):row['enabled']=0
        return row
    def csrf():
        session.setdefault('public_booking_csrf',secrets.token_urlsafe(32));return session['public_booking_csrf']
    def form_token(s):return signer.dumps({'site':s['site_id'],'nonce':secrets.token_urlsafe(24),'csrf':csrf()})
    def limit(s):
        # Browser-session limit avoids conflating every guest behind the hosting proxy.
        identity=hmac.new(str(app.secret_key).encode(),(str(s['site_id'])+':'+csrf()).encode(),hashlib.sha256).hexdigest();now=int(time.time())
        with db() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000044,))
            c.execute('DELETE FROM alport_public_booking_limits WHERE window_start<%s',(now-86400,))
            row=c.execute('SELECT * FROM alport_public_booking_limits WHERE identity_hash=%s',(identity,)).fetchone()
            n=row['attempts']+1 if row and now-row['window_start']<600 else 1
            start=row['window_start'] if row and now-row['window_start']<600 else now
            c.execute('INSERT INTO alport_public_booking_limits(identity_hash,window_start,attempts) VALUES(%s,%s,%s) ON CONFLICT(identity_hash) DO UPDATE SET window_start=excluded.window_start,attempts=excluded.attempts',(identity,start,n))
        return n<=20
    @app.context_processor
    def management_form_token():
        if request.path.startswith('/manage-reservation/'):
            session.setdefault('reservation_csrf',secrets.token_urlsafe(32));return {'reservation_csrf':session['reservation_csrf']}
        return {}
    @app.before_request
    def management_form_guard():
        if request.path.startswith('/manage-reservation/') and request.method=='POST':
            if not session.get('reservation_csrf') or not hmac.compare_digest(session['reservation_csrf'],request.form.get('csrf','')):abort(403)
    @app.after_request
    def public_headers(response):
        if request.path.startswith(('/book/','/manage-reservation/')):
            response.headers['Cache-Control']='private, no-store';response.headers['Referrer-Policy']='no-referrer';response.headers['X-Content-Type-Options']='nosniff'
        if request.path.startswith('/book/'):
            response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; img-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response
    @app.route('/book/<slug>',methods=['GET','POST'])
    def public_book(slug):
        s=public_site(slug);cfg=env['booking_settings_for'](s['organisation_id'],s['site_id']);at=availability.localnow();values={};error=None;status=200;alternatives=[]
        token=form_token(s) if request.method=='GET' else request.form.get('form_token','')
        def page():return render_template('public_booking.html',venue=s,settings=cfg,currency=(q('SELECT currency FROM organisations WHERE id=?',(s['organisation_id'],),True) or {}).get('currency','GBP'),values=values,error=error,alternatives=alternatives,form_token=token,today=at.date().isoformat(),last_day=(at.date()+timedelta(days=max(0,int(cfg['advance_days'])))).isoformat()),status
        if not s['enabled']:return page()
        if request.method=='POST':
            if request.content_length and request.content_length>12000:abort(413)
            try:
                signed=signer.loads(token,max_age=3600)
                if signed['site']!=s['site_id'] or not hmac.compare_digest(signed['csrf'],csrf()):raise BadSignature('Wrong form')
            except (BadSignature,KeyError,TypeError):
                error='This form expired. Please try again.';token=form_token(s);status=400;return page()
            if request.form.get('website') or not limit(s):
                error='Please wait before trying again, or contact the pub.';status=429;return page()
            values={k:request.form.get(k,'').strip() for k in ('date','time','party','name','email','phone','requests','allergies','occasion')}
            try:
                from datetime import date
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',values['date']):raise ValueError('Choose a valid date.')
                date.fromisoformat(values['date']);availability.minutes(values['time']);party=int(values['party'])
                if not 1<=party<=100:raise ValueError('Enter a party size between 1 and 100.')
                if not 1<=len(values['name'])<=120:raise ValueError('Enter your name (up to 120 characters).')
                if len(values['email'])>200 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',values['email']):raise ValueError('Enter a valid contact email.')
                if not 5<=len(re.sub(r'\D','',values['phone']))<=18 or len(values['phone'])>60:raise ValueError('Enter a valid contact phone number.')
                if len(values['requests'])>1000:raise ValueError('Keep requests under 1,000 characters.')
                from alport_booking_fields import validate_guest_fields
                validate_guest_fields(dict(guest_name=values['name'],guest_email=values['email'],guest_phone=values['phone'],dietary_requirements=values['allergies'],special_occasion=values['occasion']))
                key=hashlib.sha256(signed['nonce'].encode()).hexdigest()
                with db() as c:
                    c.execute('SELECT pg_advisory_xact_lock(%s)',(-9000000000000-s['site_id'],))
                    old=c.execute('SELECT b.manage_token FROM alport_public_booking_requests r JOIN bookings b ON b.id=r.booking_id WHERE r.request_key=%s AND r.site_id=%s',(key,s['site_id'])).fetchone()
                    if old:return redirect(url_for('public_booking_receipt',slug=slug,token=old['manage_token']),303)
                    fresh=c.execute('SELECT enabled FROM alport_public_booking_links WHERE site_id=%s',(s['site_id'],)).fetchone()
                    if not fresh or not fresh['enabled']:raise ValueError('Online bookings have been paused. Please contact the pub.')
                    if not c.execute("SELECT 1 AS ready FROM pg_trigger WHERE tgname='alport_booking_overlap_guard' AND tgrelid='bookings'::regclass AND tgenabled='O' AND NOT tgisinternal").fetchone():raise ValueError('Online bookings are temporarily unavailable. Please contact the pub.')
                    choices,reason=availability.options(c,s['organisation_id'],s['site_id'],values,at)
                    if not choices:
                        if reason=='staff':raise ValueError('Please contact the pub for this booking. Larger parties, deposits or special arrangements need the team’s help.')
                        alternatives=availability.alternatives(c,s['organisation_id'],s['site_id'],values,at)
                        raise ValueError('That time is not available for this party. Please choose another time.')
                    email=values['email'].lower();ts=env['now']();manage=secrets.token_urlsafe(32)
                    # Never overwrite an existing profile or reveal whether it exists.
                    guest=c.execute('SELECT id FROM guests WHERE organisation_id=%s AND lower(email)=%s ORDER BY id LIMIT 1',(s['organisation_id'],email)).fetchone()
                    gid=guest['id'] if guest else c.execute('INSERT INTO guests(organisation_id,name,email,phone,marketing_consent,created_at,updated_at) VALUES(%s,%s,%s,%s,0,%s,%s) RETURNING id',(s['organisation_id'],values['name'],email,values['phone'],ts,ts)).fetchone()['id']
                    current_cfg=c.execute('SELECT default_duration FROM booking_settings WHERE organisation_id=%s AND site_id=%s',(s['organisation_id'],s['site_id'])).fetchone()
                    bid=c.execute("INSERT INTO bookings(organisation_id,site_id,guest_id,booking_date,booking_time,party_size,duration_minutes,status,source,table_id,guest_name,guest_email,guest_phone,special_requests,dietary_requirements,special_occasion,manage_token,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'Confirmed','Website',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",(s['organisation_id'],s['site_id'],gid,values['date'],values['time'],party,current_cfg['default_duration'],choices[0]['id'],values['name'],email,values['phone'],values['requests'],values['allergies'],values['occasion'],manage,ts,ts)).fetchone()['id']
                    c.execute('INSERT INTO alport_public_booking_requests(request_key,site_id,booking_id,created_at) VALUES(%s,%s,%s,%s)',(key,s['site_id'],bid,ts))
                return redirect(url_for('public_booking_receipt',slug=slug,token=manage),303)
            except (ValueError,OverflowError) as e:
                error=str(e) or 'Check your booking details.';status=400
            except Exception as e:
                if getattr(e,'sqlstate',None)!='23P01':raise
                error='That table was just booked. Please try another time.';status=409
        return page()
    @app.get('/book/<slug>/confirmation/<token>')
    def public_booking_receipt(slug,token):
        s=public_site(slug)
        b=q('SELECT id,booking_date,booking_time,party_size,status FROM bookings WHERE organisation_id=? AND site_id=? AND manage_token=?',(s['organisation_id'],s['site_id'],token),True)
        if not b:abort(404)
        return render_template('public_booking_receipt.html',venue=s,booking=b,manage_url='/manage-reservation/'+token)
