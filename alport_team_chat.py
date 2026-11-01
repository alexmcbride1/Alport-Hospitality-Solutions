"""Shared team rooms with explicit organisation and site boundaries."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets

from flask import Blueprint, g, jsonify, redirect, render_template, request, session

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_chat_members(
 organisation_id BIGINT NOT NULL REFERENCES organisations(id),
 site_id BIGINT NOT NULL REFERENCES sites(id), user_id BIGINT NOT NULL REFERENCES users(id),
 granted_by BIGINT NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,user_id));
CREATE TABLE IF NOT EXISTS alport_chat_messages(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL REFERENCES organisations(id),
 site_id BIGINT NOT NULL REFERENCES sites(id), user_id BIGINT NOT NULL REFERENCES users(id),
 sender_name TEXT NOT NULL, body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
 request_key TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(organisation_id,site_id,user_id,request_key));
CREATE INDEX IF NOT EXISTS alport_chat_room_history ON alport_chat_messages(organisation_id,site_id,id);
CREATE INDEX IF NOT EXISTS alport_chat_send_rate ON alport_chat_messages(organisation_id,user_id,created_at);
CREATE TABLE IF NOT EXISTS alport_chat_access_events(
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 user_id BIGINT NOT NULL, actor_id BIGINT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_chat_reads(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,user_id BIGINT NOT NULL,last_id BIGINT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,user_id));
CREATE TABLE IF NOT EXISTS alport_chat_mentions(
 message_id BIGINT NOT NULL REFERENCES alport_chat_messages(id),user_id BIGINT NOT NULL,
 PRIMARY KEY(message_id,user_id));
CREATE TABLE IF NOT EXISTS alport_chat_tasks(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 message_id BIGINT NOT NULL REFERENCES alport_chat_messages(id),title TEXT NOT NULL,
 assignee_id BIGINT NOT NULL REFERENCES users(id),due_date TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Open',
 created_by BIGINT NOT NULL,created_at TEXT NOT NULL,updated_by BIGINT NOT NULL,updated_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,UNIQUE(organisation_id,site_id,message_id));
CREATE TABLE IF NOT EXISTS alport_chat_task_events(
 id BIGSERIAL PRIMARY KEY,task_id BIGINT NOT NULL REFERENCES alport_chat_tasks(id),
 actor_id BIGINT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);

'''


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def register_team_chat(app, env):
    conn, q = env['conn'], env['q']
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)', (739000000005,))
        for sql in SCHEMA.split(';'):
            if sql.strip():
                c.execute(sql)
    bp = Blueprint('alport_chat', __name__)

    def query(c, sql, args=(), one=False):
        if c is None:
            return q(sql.replace('%s', '?'), args, one)
        rows = c.execute(sql, args)
        return rows.fetchone() if one else rows.fetchall()

    def available(u, c=None):
        rows = query(c, 'SELECT id,name FROM sites WHERE organisation_id=%s AND active=1 ORDER BY name,id', (u['organisation_id'],))
        if u['role'] == 'Owner':
            return rows
        # An employee assignment always takes precedence over a manual chat grant.
        # A disabled/unactivated assignment cannot be bypassed with a chat-only grant.
        members = query(c, '''SELECT a.site_id,a.profile,a.activated_at,e.active AS employee_active
            FROM alport_staff_access a JOIN employees e ON e.id=a.employee_id
            AND e.organisation_id=a.organisation_id AND e.site_id=a.site_id
            WHERE a.user_id=%s AND a.organisation_id=%s''', (u['id'], u['organisation_id']))
        assigned = {a['site_id'] for a in members}
        allowed = {a['site_id'] for a in members if a['profile'] != 'disabled' and a['activated_at'] and a['employee_active']}
        grants = query(c, 'SELECT site_id FROM alport_chat_members WHERE organisation_id=%s AND user_id=%s', (u['organisation_id'], u['id']))
        allowed |= {a['site_id'] for a in grants if a['site_id'] not in assigned}
        return [s for s in rows if s['id'] in allowed]

    def room(site_id, c=None):
        # Re-fetch identity inside write transactions, not just at the request boundary.
        u = query(c, 'SELECT * FROM users WHERE id=%s AND organisation_id=%s AND active=1', (g.chat_user['id'], g.chat_user['organisation_id']), True)
        if not u:
            raise PermissionError('Your account is no longer active.')
        fingerprint = hashlib.sha256(u['password_hash'].encode()).hexdigest()
        if not hmac.compare_digest(session['chat_auth_hash'], fingerprint):
            raise PermissionError('Your sign-in changed. Sign out and sign in again.')
        site = next((s for s in available(u, c) if s['id'] == site_id), None)
        if not site:
            raise PermissionError('You do not have access to this site chat.')
        return u, site

    @bp.before_request
    def protect():
        u = env['user']()
        if not u:
            return redirect('/login') if request.path == '/team-chat' else (jsonify(error='Sign in to use team chat.'), 401)
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):
            return jsonify(error='Business subscription needs attention.'), 403
        fingerprint = hashlib.sha256(u['password_hash'].encode()).hexdigest()
        if not hmac.compare_digest(session.get('chat_auth_hash', fingerprint), fingerprint):
            return jsonify(error='Your sign-in changed. Sign out and sign in again.'), 403
        session['chat_auth_hash'] = fingerprint
        session.setdefault('chat_csrf', secrets.token_urlsafe(32))
        g.chat_user = u
        # Also bound chunked requests on supported Flask versions.
        request.max_content_length = 12000
        if request.method == 'POST':
            if not hmac.compare_digest(session['chat_csrf'], request.headers.get('X-Chat-CSRF', '')):
                return jsonify(error='Reload team chat before sending.'), 403
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(error='Send a valid message.'), 400

    @bp.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.vary.add('Cookie')
        return response

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(error=str(error)), 403

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @bp.get('/team-chat')
    def home():
        return render_template('team_chat.html')

    @bp.get('/api/team-chat/config')
    def config():
        u = g.chat_user
        return jsonify(user={'id': u['id'], 'name': u['name']}, owner=u['role'] == 'Owner',
                       csrf=session['chat_csrf'], sites=available(u))

    @bp.get('/api/team-chat/sites/<int:site_id>/messages')
    def messages(site_id):
        u, site = room(site_id)
        after, before = request.args.get('after'), request.args.get('before')
        if after is not None and before is not None:
            raise ValueError('Choose one message cursor.')
        cursor = after if after is not None else before
        if cursor is not None and (not re.fullmatch(r'[0-9]{1,18}', cursor)):
            raise ValueError('Invalid message cursor.')
        sql = 'SELECT id,user_id,sender_name,body,created_at FROM alport_chat_messages WHERE organisation_id=%s AND site_id=%s'
        args = [u['organisation_id'], site_id]
        if cursor is not None:
            sql += ' AND id' + ('>' if after is not None else '<') + '%s'
            args.append(int(cursor))
        sql += ' ORDER BY id ' + ('ASC' if after is not None else 'DESC') + ' LIMIT 51'
        rows = query(None, sql, args)
        more = len(rows) > 50
        rows = rows[:50]
        if after is None:
            rows.reverse()
        for row in rows:
            row['mentions'] = q('SELECT u.name FROM alport_chat_mentions x JOIN users u ON u.id=x.user_id WHERE x.message_id=? ORDER BY u.name',(row['id'],))
        return jsonify(site=site, messages=rows, has_more=more)

    @bp.post('/api/team-chat/sites/<int:site_id>/messages')
    def send(site_id):
        data = request.get_json()
        body, key = data.get('body'), data.get('request_key')
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > 2000:
            raise ValueError('Write a message of 1 to 2,000 characters.')
        body = body.strip()
        mentions = data.get('mentions', [])
        if not isinstance(mentions, list) or len(mentions)>20 or any(type(i) is not int for i in mentions):
            raise ValueError('Choose up to 20 people to mention.')
        mentions = sorted(set(mentions))
        if any(ord(ch) < 32 and ch not in '\n\t' for ch in body):
            raise ValueError('Remove control characters from your message.')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key):
            raise ValueError('Reload the page and try sending again.')
        with conn() as c:
            # Serialise sends and manual grants within this organisation. This makes
            # retries and the shared database rate limit safe across web workers.
            c.execute('SELECT pg_advisory_xact_lock(%s)', (741000000000 + g.chat_user['organisation_id'],))
            u, site = room(site_id, c)
            permitted = {p['id'] for p in participants(u,site_id,c)}
            if not set(mentions).issubset(permitted): raise ValueError('A mentioned person no longer has access to this site.')
            args = (u['organisation_id'], site_id, u['id'], key)
            old = c.execute('SELECT id,body FROM alport_chat_messages WHERE organisation_id=%s AND site_id=%s AND user_id=%s AND request_key=%s', args).fetchone()
            if old:
                if old['body'] != body or mentions != sorted(r['user_id'] for r in c.execute('SELECT user_id FROM alport_chat_mentions WHERE message_id=%s',(old['id'],)).fetchall()):
                    return jsonify(error='This send attempt belongs to a different message.'), 409
                return jsonify(id=old['id'], duplicate=True)
            since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec='microseconds')
            count = c.execute('SELECT COUNT(*) AS n FROM alport_chat_messages WHERE organisation_id=%s AND user_id=%s AND created_at>=%s', (u['organisation_id'], u['id'], since)).fetchone()['n']
            if count >= 20:
                return jsonify(error='Please wait a minute before sending more messages.'), 429, {'Retry-After': '60'}
            row = c.execute('''INSERT INTO alport_chat_messages(organisation_id,site_id,user_id,sender_name,body,request_key,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id''', (u['organisation_id'], site_id, u['id'], u['name'], body, key, stamp())).fetchone()
            for uid in mentions:
                c.execute('INSERT INTO alport_chat_mentions(message_id,user_id) VALUES(%s,%s)',(row['id'],uid))
        return jsonify(id=row['id']), 201

    def require_owner(site_id, c=None):
        u, site = room(site_id, c)
        if u['role'] != 'Owner':
            raise PermissionError('Only the business Owner can manage chat access.')
        return u, site

    @bp.get('/api/team-chat/sites/<int:site_id>/access')
    def access(site_id):
        u, site = require_owner(site_id)
        users = q('SELECT id,name,role FROM users WHERE organisation_id=? AND active=1 ORDER BY name,id', (u['organisation_id'],))
        staff = q('''SELECT a.user_id,a.profile,a.activated_at,e.active AS employee_active FROM alport_staff_access a
            JOIN employees e ON e.id=a.employee_id AND e.organisation_id=a.organisation_id AND e.site_id=a.site_id
            WHERE a.organisation_id=? AND a.site_id=?''', (u['organisation_id'], site_id))
        assigned = {a['user_id']: a for a in staff}
        granted = {a['user_id'] for a in q('SELECT user_id FROM alport_chat_members WHERE organisation_id=? AND site_id=?', (u['organisation_id'], site_id))}
        result = []
        for person in users:
            a = assigned.get(person['id'])
            source = 'Owner' if person['role'] == 'Owner' else 'Staff assignment' if a else 'Chat access'
            enabled = person['role'] == 'Owner' or (bool(a['profile'] != 'disabled' and a['activated_at'] and a['employee_active']) if a else person['id'] in granted)
            result.append({**person, 'source': source, 'enabled': enabled, 'editable': source == 'Chat access'})
        return jsonify(users=result)

    @bp.post('/api/team-chat/sites/<int:site_id>/access')
    def set_access(site_id):
        data = request.get_json()
        target, enabled = data.get('user_id'), data.get('enabled')
        if type(target) is not int or not 1 <= target <= 9223372036854775807 or type(enabled) is not bool:
            raise ValueError('Choose a user and access setting.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)', (741000000000 + g.chat_user['organisation_id'],))
            u, site = require_owner(site_id, c)
            person = c.execute('SELECT id,role FROM users WHERE id=%s AND organisation_id=%s AND active=1', (target, u['organisation_id'])).fetchone()
            if not person or person['role'] == 'Owner':
                raise ValueError('Choose an active non-owner account in this business.')
            if c.execute('SELECT id FROM alport_staff_access WHERE user_id=%s AND organisation_id=%s AND site_id=%s', (target, u['organisation_id'], site_id)).fetchone():
                raise ValueError('Manage this user through their staff assignment at this venue.')
            if enabled:
                c.execute('''INSERT INTO alport_chat_members(organisation_id,site_id,user_id,granted_by,updated_at) VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT(organisation_id,site_id,user_id) DO UPDATE SET granted_by=excluded.granted_by,updated_at=excluded.updated_at''', (u['organisation_id'], site_id, target, u['id'], stamp()))
            else:
                c.execute('DELETE FROM alport_chat_members WHERE organisation_id=%s AND site_id=%s AND user_id=%s', (u['organisation_id'], site_id, target))
            c.execute('INSERT INTO alport_chat_access_events(organisation_id,site_id,user_id,actor_id,enabled,created_at) VALUES(%s,%s,%s,%s,%s,%s)', (u['organisation_id'], site_id, target, u['id'], int(enabled), stamp()))
        return jsonify(ok=True)

    def participants(u, site_id, c=None):
        users = query(c, 'SELECT id,name,role,organisation_id FROM users WHERE organisation_id=%s AND active=1 ORDER BY name,id',(u['organisation_id'],))
        return [{'id':p['id'],'name':p['name']} for p in users if any(s['id']==site_id for s in available(p,c))]

    @bp.get('/api/team-chat/sites/<int:site_id>/participants')
    def people(site_id):
        u,site=room(site_id)
        return jsonify(users=participants(u,site_id))

    @bp.get('/api/team-chat/unread')
    def unread():
        u=g.chat_user; result=[]
        for site in available(u):
            last=q('SELECT last_id FROM alport_chat_reads WHERE organisation_id=? AND site_id=? AND user_id=?',(u['organisation_id'],site['id'],u['id']),True)
            last=last['last_id'] if last else 0
            row=q("""SELECT COUNT(*) AS unread FROM alport_chat_messages
                WHERE organisation_id=? AND site_id=? AND id>? AND user_id<>?""",(u['organisation_id'],site['id'],last,u['id']),True)
            mentioned=q("""SELECT COUNT(*) AS mentions FROM alport_chat_mentions x JOIN alport_chat_messages m ON m.id=x.message_id
                WHERE m.organisation_id=? AND m.site_id=? AND m.id>? AND x.user_id=? AND m.user_id<>?""",(u['organisation_id'],site['id'],last,u['id'],u['id']),True)
            result.append({**site,**row,**mentioned})
        return jsonify(sites=result)

    @bp.post('/api/team-chat/sites/<int:site_id>/read')
    def mark_read(site_id):
        last=request.get_json().get('last_id')
        if type(last) is not int or not 0<=last<=9223372036854775807: raise ValueError('Invalid message position.')
        with conn() as c:
            u,site=room(site_id,c)
            if last and not c.execute('SELECT id FROM alport_chat_messages WHERE id=%s AND organisation_id=%s AND site_id=%s',(last,u['organisation_id'],site_id)).fetchone():
                raise ValueError('Message does not belong to this site.')
            c.execute("""INSERT INTO alport_chat_reads(organisation_id,site_id,user_id,last_id) VALUES(%s,%s,%s,%s)
                ON CONFLICT(organisation_id,site_id,user_id) DO UPDATE SET last_id=CASE WHEN excluded.last_id>alport_chat_reads.last_id THEN excluded.last_id ELSE alport_chat_reads.last_id END""",(u['organisation_id'],site_id,u['id'],last))
        return jsonify(ok=True)

    @bp.get('/api/team-chat/sites/<int:site_id>/tasks')
    def tasks(site_id):
        u,site=room(site_id)
        rows=q("""SELECT t.*,u.name AS assignee,a.name AS updated_name FROM alport_chat_tasks t
            JOIN users u ON u.id=t.assignee_id JOIN users a ON a.id=t.updated_by
            WHERE t.organisation_id=? AND t.site_id=? ORDER BY CASE WHEN t.status='Done' THEN 1 ELSE 0 END,t.due_date,t.id DESC LIMIT 200""",(u['organisation_id'],site_id))
        return jsonify(tasks=rows)

    @bp.post('/api/team-chat/sites/<int:site_id>/tasks')
    def create_task(site_id):
        d=request.get_json();mid=d.get('message_id');uid=d.get('assignee_id');title=d.get('title');due=d.get('due_date')
        if type(mid) is not int or type(uid) is not int or not isinstance(title,str) or not 1<=len(title.strip())<=200:
            raise ValueError('Choose a message, assignee and title of up to 200 characters.')
        try: date.fromisoformat(due)
        except (TypeError,ValueError): raise ValueError('Choose a valid deadline.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(741000000000+g.chat_user['organisation_id'],))
            u,site=room(site_id,c)
            if not c.execute('SELECT id FROM alport_chat_messages WHERE id=%s AND organisation_id=%s AND site_id=%s',(mid,u['organisation_id'],site_id)).fetchone(): raise ValueError('Message not found in this room.')
            if uid not in {p['id'] for p in participants(u,site_id,c)}: raise ValueError('Choose someone with access to this room.')
            old=c.execute('SELECT id FROM alport_chat_tasks WHERE organisation_id=%s AND site_id=%s AND message_id=%s',(u['organisation_id'],site_id,mid)).fetchone()
            if old:return jsonify(error='This message already has a task.'),409
            row=c.execute("""INSERT INTO alport_chat_tasks(organisation_id,site_id,message_id,title,assignee_id,due_date,created_by,created_at,updated_by,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(u['organisation_id'],site_id,mid,title.strip(),uid,due,u['id'],stamp(),u['id'],stamp())).fetchone()
            c.execute('INSERT INTO alport_chat_task_events(task_id,actor_id,status,created_at) VALUES(%s,%s,%s,%s)',(row['id'],u['id'],'Open',stamp()))
        return jsonify(id=row['id']),201

    @bp.post('/api/team-chat/sites/<int:site_id>/tasks/<int:tid>')
    def update_task(site_id,tid):
        d=request.get_json();status=d.get('status');version=d.get('version')
        if status not in ('Open','In progress','Done') or type(version) is not int: raise ValueError('Choose a valid task status.')
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(741000000000+g.chat_user['organisation_id'],))
            u,site=room(site_id,c)
            task=c.execute('SELECT * FROM alport_chat_tasks WHERE id=%s AND organisation_id=%s AND site_id=%s',(tid,u['organisation_id'],site_id)).fetchone()
            if not task:raise PermissionError('Task not found in this site.')
            if u['role']!='Owner' and u['id'] not in (task['assignee_id'],task['created_by']): raise PermissionError('Only the assignee, creator or Owner can update this task.')
            changed=c.execute('UPDATE alport_chat_tasks SET status=%s,updated_by=%s,updated_at=%s,version=version+1 WHERE id=%s AND version=%s RETURNING id',(status,u['id'],stamp(),tid,version)).fetchone()
            if not changed:return jsonify(error='Task changed. Refresh before updating.'),409
            c.execute('INSERT INTO alport_chat_task_events(task_id,actor_id,status,created_at) VALUES(%s,%s,%s,%s)',(tid,u['id'],status,stamp()))
        return jsonify(ok=True)

    @bp.get('/api/team-chat/sites/<int:site_id>/tasks/<int:tid>/history')
    def task_history(site_id,tid):
        u,site=room(site_id)
        if not q('SELECT id FROM alport_chat_tasks WHERE id=? AND organisation_id=? AND site_id=?',(tid,u['organisation_id'],site_id),True):raise PermissionError('Task not found in this site.')
        return jsonify(events=q('SELECT e.status,e.created_at,u.name AS actor FROM alport_chat_task_events e JOIN users u ON u.id=e.actor_id WHERE e.task_id=? ORDER BY e.id DESC',(tid,)))

    app.register_blueprint(bp)
