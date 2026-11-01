"""Venue-scoped EHO evidence. Files and their records commit together in PostgreSQL."""
import hashlib
import hmac
import json
import secrets
from datetime import date
from zoneinfo import ZoneInfo
from datetime import datetime
from flask import Blueprint, Response, g, jsonify, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

FILE_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 24 * 1024 * 1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_eho_files(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 daily_id BIGINT REFERENCES eho_daily_checks(id), record_id BIGINT REFERENCES eho_records(id),
 filename TEXT NOT NULL, mimetype TEXT NOT NULL, data BYTEA NOT NULL, sha256 TEXT NOT NULL,
 byte_size INTEGER NOT NULL, uploaded_by BIGINT NOT NULL, uploaded_name TEXT NOT NULL, uploaded_at TEXT NOT NULL,
 CHECK ((daily_id IS NOT NULL AND record_id IS NULL) OR (daily_id IS NULL AND record_id IS NOT NULL)));
CREATE INDEX IF NOT EXISTS alport_eho_files_scope ON alport_eho_files(organisation_id,site_id,daily_id,record_id);
CREATE TABLE IF NOT EXISTS alport_eho_requests(
 organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
 request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, kind TEXT NOT NULL, target_id BIGINT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,user_id,request_key));
'''

def today():
    return datetime.now(ZoneInfo('Europe/London')).date().isoformat()


def text(d, key, limit=4000, required=False):
    value = str(d.get(key) or '').strip()
    if len(value) > limit or (required and not value):
        raise ValueError('Enter '+key.replace('_',' ')+f' (maximum {limit} characters).')
    return value


def day(value, required=False):
    if not value and not required:
        return ''
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Enter a valid date.') from None


def unpack(value):
    try:
        result = json.loads(value or '{}') if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def read_files(files, photos_only=False):
    if len(files) > 5:
        raise ValueError('Attach at most 5 files at once.')
    result, total = [], 0
    for f in files:
        data = f.read(FILE_LIMIT + 1)
        if not data or len(data) > FILE_LIMIT:
            raise ValueError('Each file must be non-empty and no larger than 8 MB.')
        total += len(data)
        if total > TOTAL_LIMIT:
            raise ValueError('Keep each upload below 24 MB in total.')
        mime = ('image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else
                'application/pdf' if data.startswith(b'%PDF-') else None)
        if not mime or (photos_only and mime == 'application/pdf'):
            raise ValueError('Use JPEG, PNG or WebP photos'+(' only.' if photos_only else ', or PDF documents.'))
        # Downloads only: file signatures are checked, but this is not a malware scanner.
        name = secure_filename(f.filename or '')[:150] or 'evidence'
        result.append(dict(filename=name, mimetype=mime, data=data, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest()))
    return result


def register_evidence(app, env):
    if 'alport_evidence' in app.blueprints:
        return
    conn, q = env['conn'], env['q']
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)', (739000000019,))
        for sql in SCHEMA.split(';'):
            if sql.strip():
                c.execute(sql)
    bp = Blueprint('alport_evidence', __name__)

    @bp.before_request
    def protect():
        u = env['user']()
        if not u:
            return jsonify(error='Sign in first.'), 401
        a = app.extensions['alport_staff_access'](u)
        if a or u['role'] in ('Staff','User'):
            if not a or not app.extensions['alport_staff_live'](a):
                return jsonify(error='Active staff access is required.'), 403
            if session.get('staff_auth_hash') != hashlib.sha256(u['password_hash'].encode()).hexdigest():
                return jsonify(error='Sign in again.'), 403
            session['site_id'] = a['site_id']
        s = env['current_site']()
        if not s or s['organisation_id'] != u['organisation_id']:
            return jsonify(error='Select an active venue first.'), 403
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):
            return jsonify(error='The business subscription needs attention.'), 403
        g.eho_user, g.eho_site = u, s
        if request.method == 'POST':
            request.max_content_length = TOTAL_LIMIT + 1024 * 1024
            if request.content_length and request.content_length > request.max_content_length:
                return jsonify(error='Upload exceeds 24 MB.'), 413
            token = session.get('eho_evidence_csrf', '')
            if not token or not hmac.compare_digest(token, request.headers.get('X-Evidence-CSRF','')):
                return jsonify(error='Reload this page before saving.'), 403
            if request.form.get('site_id') != str(s['id']):
                return jsonify(error='Your selected venue changed. Reload before saving.'), 409

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.errorhandler(ValueError)
    def invalid(e):
        return jsonify(error=str(e)), 400

    @bp.errorhandler(RequestEntityTooLarge)
    def too_large(e):
        return jsonify(error='Upload exceeds the allowed size.'), 413

    def scope():
        return g.eho_user['organisation_id'], g.eho_site['id']

    def token():
        session.setdefault('eho_evidence_csrf', secrets.token_urlsafe(32))
        return session['eho_evidence_csrf']

    def lock(c):
        c.execute('SELECT pg_advisory_xact_lock(%s)', (739100000000 + int(g.eho_site['id']),))

    def audit(c, action, kind, rid, detail):
        c.execute('''INSERT INTO audit_log(organisation_id,user_id,action,entity,entity_id,detail,created_at)
                     VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                  (scope()[0],g.eho_user['id'],action,kind,rid,detail,env['now']()))

    def payload():
        try:
            d = json.loads(request.form.get('payload','{}'))
        except (ValueError, TypeError):
            raise ValueError('Invalid form data.') from None
        if not isinstance(d, dict):
            raise ValueError('Invalid form data.')
        return d

    def prior(c, kind, d, files):
        key = request.form.get('request_key','')
        if not 16 <= len(key) <= 100:
            raise ValueError('Reload this form before saving.')
        digest = hashlib.sha256(json.dumps([kind,d,[(f['filename'],f['sha256']) for f in files]],sort_keys=True).encode()).hexdigest()
        row = c.execute('''SELECT * FROM alport_eho_requests WHERE organisation_id=%s AND site_id=%s
            AND user_id=%s AND request_key=%s''', (*scope(),g.eho_user['id'],key)).fetchone()
        if row and row['payload_hash'] != digest:
            raise ValueError('This form was already saved with different content. Reload before creating another record.')
        return key,digest,row

    def remember(c, key, digest, kind, rid):
        c.execute('INSERT INTO alport_eho_requests VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  (*scope(),g.eho_user['id'],key,digest,kind,rid))

    def attach(c, kind, rid, files):
        column = 'daily_id' if kind == 'daily' else 'record_id'
        old = c.execute(f'SELECT COUNT(*) AS n,COALESCE(SUM(byte_size),0) AS size FROM alport_eho_files WHERE {column}=%s AND organisation_id=%s AND site_id=%s', (rid,*scope())).fetchone()
        if old['n'] + len(files) > 20 or int(old['size']) + sum(f['byte_size'] for f in files) > 64 * 1024 * 1024:
            raise ValueError('Each record can hold up to 20 files / 64 MB.')
        for f in files:
            c.execute(f'''INSERT INTO alport_eho_files(organisation_id,site_id,{column},filename,mimetype,data,sha256,byte_size,uploaded_by,uploaded_name,uploaded_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (*scope(),rid,f['filename'],f['mimetype'],f['data'],f['sha256'],f['byte_size'],g.eho_user['id'],g.eho_user['name'],env['now']()))

    def target(c, kind, rid):
        if kind not in ('daily','record'):
            raise ValueError('Unknown record type.')
        table = 'eho_daily_checks' if kind == 'daily' else 'eho_records'
        r = c.execute(f'SELECT * FROM {table} WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,*scope())).fetchone()
        if r and kind == 'record' and r['category'] != 'Pest control' and unpack(r.get('metadata_json')).get('evidence_kind') != 'extraction':
            return None
        return dict(r) if r else None

    @bp.get('/eho/evidence')
    def page():
        return render_template('eho_evidence.html',site=g.eho_site,csrf=token(),today=today(),staff=bool(app.extensions['alport_staff_access'](g.eho_user)))

    @bp.get('/api/eho/evidence/config')
    def config():
        return jsonify(csrf=token(),site_id=g.eho_site['id'])

    @bp.post('/api/eho/evidence/daily')
    def save_daily():
        d = payload()
        kind = d.get('check_type')
        if kind not in ('Opening','Closing'):
            raise ValueError('Choose opening or closing checks.')
        expected = env['OPENING_CHECKS'] if kind == 'Opening' else env['CLOSING_CHECKS']
        answers = d.get('answers')
        if not isinstance(answers,dict) or set(answers) != set(expected) or any(type(v) is not bool for v in answers.values()):
            raise ValueError('Answer every checklist item with Pass or Fail.')
        problems, corrective = text(d,'problems'), text(d,'corrective_action')
        failed = [x for x in expected if not answers[x]]
        if failed and (not problems or not corrective):
            raise ValueError('Record the problem and corrective action for failed checks.')
        files = read_files(request.files.getlist('files'), photos_only=True)
        with conn() as c:
            lock(c)
            key,digest,old = prior(c,'daily',d,files)
            if old:
                return jsonify(ok=True,id=old['target_id'],replayed=True)
            rid = c.execute('''INSERT INTO eho_daily_checks(organisation_id,site_id,check_date,check_type,answers_json,problems,corrective_action,signed_by,signed_name,completed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (*scope(),today(),kind,json.dumps(answers),problems,corrective,g.eho_user['id'],g.eho_user['name'],env['now']())).fetchone()['id']
            if failed:
                c.execute('''INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,created_by,created_name,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (*scope(),'HACCP/SFBB',today(),kind+' check exception','Action required','; '.join(failed)+' | '+problems,corrective,g.eho_user['id'],g.eho_user['name'],env['now']()))
            attach(c,'daily',rid,files)
            audit(c,'Completed','eho_daily_check',rid,kind+' checks signed with '+str(len(files))+' photos')
            remember(c,key,digest,'daily',rid)
        return jsonify(ok=True,id=rid)

    @bp.post('/api/eho/evidence/visits')
    def save_visit():
        d = payload()
        kind = d.get('kind')
        if kind not in ('pest','extraction'):
            raise ValueError('Choose pest control or extraction cleaning.')
        performed = day(d.get('visit_date'),True)
        if performed > today():
            raise ValueError('Use the actual past or current visit/cleaning date, not a future date.')
        due = day(d.get('due_date'))
        if due and due < performed:
            raise ValueError('Next visit date cannot be before the recorded visit.')
        contractor = text(d,'contractor',200,True)
        title = text(d,'title',200,True)
        details, action = text(d,'details',6000,True), text(d,'corrective_action')
        status = d.get('status','Complete')
        if status not in ('Complete','Action required'):
            raise ValueError('Choose a valid status.')
        if status == 'Action required' and not action:
            raise ValueError('Describe the corrective action or next step.')
        reference = text(d,'reference',200)
        meta = dict(evidence_kind=kind,contractor=contractor,service_type=text(d,'service_type',100,True),area=text(d,'area',300))
        files = read_files(request.files.getlist('files'))
        with conn() as c:
            lock(c)
            key,digest,old = prior(c,'record',d,files)
            if old:
                return jsonify(ok=True,id=old['target_id'],replayed=True)
            rid = c.execute('''INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,due_date,reference,metadata_json,created_by,created_name,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (*scope(),'Pest control' if kind=='pest' else 'Cleaning',performed,title,status,details,action,due,reference,json.dumps(meta),g.eho_user['id'],g.eho_user['name'],env['now']())).fetchone()['id']
            attach(c,'record',rid,files)
            audit(c,'Recorded','eho_record',rid,kind+' visit/clean; '+str(len(files))+' attachments')
            remember(c,key,digest,'record',rid)
        return jsonify(ok=True,id=rid)

    @bp.get('/api/eho/evidence/history')
    def history():
        kind = request.args.get('kind','pest')
        if kind not in ('pest','extraction','daily'):
            raise ValueError('Choose a history type.')
        start, end = day(request.args.get('start')),day(request.args.get('end'))
        if start and end and start > end:
            raise ValueError('Start date must be before end date.')
        try:
            offset = int(request.args.get('offset','0'))
            if offset < 0:raise ValueError()
        except ValueError:
            raise ValueError('Invalid page.') from None
        daily = kind == 'daily'
        table, column, dt = ('eho_daily_checks','daily_id','check_date') if daily else ('eho_records','record_id','record_date')
        args = list(scope()); where = 'r.organisation_id=? AND r.site_id=?'
        if kind == 'pest':where += " AND r.category='Pest control'"
        if kind == 'extraction':
            where += ' AND r.metadata_json LIKE ?'
            args.append('%"evidence_kind": "extraction"%')
        if start:where += f' AND r.{dt}>=?';args.append(start)
        if end:where += f' AND r.{dt}<=?';args.append(end)
        rows = q(f'''SELECT r.*, (SELECT COUNT(*) FROM alport_eho_files f WHERE f.{column}=r.id AND f.organisation_id=r.organisation_id AND f.site_id=r.site_id) AS file_count
            FROM {table} r WHERE {where} ORDER BY r.{dt} DESC,r.id DESC LIMIT 51 OFFSET ?''',(*args,offset))
        return jsonify(rows=[dict(r) for r in rows[:50]],has_more=len(rows)>50)

    @bp.get('/api/eho/evidence/<kind>/<int:rid>')
    def detail(kind,rid):
        with conn() as c:
            r = target(c,kind,rid)
            if not r:return jsonify(error='Record not found at this venue.'),404
            column = 'daily_id' if kind=='daily' else 'record_id'
            files = c.execute(f'''SELECT id,filename,mimetype,byte_size,uploaded_name,uploaded_at FROM alport_eho_files
                WHERE {column}=%s AND organisation_id=%s AND site_id=%s ORDER BY id''',(rid,*scope())).fetchall()
        r['metadata'] = unpack(r.get('metadata_json'))
        r['answers'] = unpack(r.get('answers_json'))
        return jsonify(record=r,files=[dict(f) for f in files])

    @bp.post('/api/eho/evidence/<kind>/<int:rid>/files')
    def add_files(kind,rid):
        files = read_files(request.files.getlist('files'),photos_only=kind=='daily')
        if not files:raise ValueError('Choose at least one file.')
        with conn() as c:
            lock(c)
            if not target(c,kind,rid):return jsonify(error='Record not found at this venue.'),404
            key,digest,old = prior(c,'attach:'+kind,{'id':rid},files)
            if old:return jsonify(ok=True,id=rid,replayed=True)
            attach(c,kind,rid,files)
            audit(c,'Attached evidence','eho_'+kind,rid,str(len(files))+' files; original sign-off unchanged')
            remember(c,key,digest,'attach:'+kind,rid)
        return jsonify(ok=True,id=rid)

    @bp.get('/api/eho/evidence/files/<int:fid>')
    def download(fid):
        r = q('SELECT * FROM alport_eho_files WHERE id=? AND organisation_id=? AND site_id=?',(fid,*scope()),True)
        if not r:return jsonify(error='File not found at this venue.'),404
        with conn() as c:
            if not target(c,'daily' if r['daily_id'] else 'record',r['daily_id'] or r['record_id']):
                return jsonify(error='Record not found at this venue.'),404
        ext = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp','application/pdf':'pdf'}[r['mimetype']]
        return Response(bytes(r['data']),mimetype=r['mimetype'],headers={
            'Content-Disposition':f'attachment; filename="eho-evidence-{fid}.{ext}"',
            'Content-Security-Policy':"sandbox; default-src 'none'",'X-Content-Type-Options':'nosniff'})

    # Append an enhancement only to the two existing EHO screens; keep customised templates intact.
    @app.after_request
    def enhance(response):
        if request.endpoint in ('app_home','alport_staff.compliance') and response.status_code==200 and response.mimetype=='text/html':
            html = response.get_data(as_text=True)
            tag = '<script src="/static/eho_evidence_widget.js?v=1" defer></script>'
            if '</body>' in html and tag not in html:
                response.set_data(html.replace('</body>',tag+'</body>'))
        return response

    app.register_blueprint(bp)
