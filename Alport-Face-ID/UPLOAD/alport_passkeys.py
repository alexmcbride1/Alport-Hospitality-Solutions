"""Optional WebAuthn sign-in. No biometric data or private keys reach Alport."""
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash
from webauthn import (generate_registration_options, generate_authentication_options,
                      verify_registration_response, verify_authentication_response, options_to_json)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                     UserVerificationRequirement, PublicKeyCredentialDescriptor)
from webauthn.helpers.exceptions import WebAuthnException

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_passkey_subjects (
 subject TEXT PRIMARY KEY, user_handle TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS alport_passkeys (
 id BIGSERIAL PRIMARY KEY, subject TEXT NOT NULL REFERENCES alport_passkey_subjects(subject),
 credential_id TEXT NOT NULL UNIQUE, public_key BYTEA NOT NULL, sign_count BIGINT NOT NULL,
 password_stamp TEXT NOT NULL, label TEXT NOT NULL, created_at BIGINT NOT NULL,
 last_used BIGINT, revoked_at BIGINT);
CREATE INDEX IF NOT EXISTS alport_passkeys_subject ON alport_passkeys(subject);
CREATE TABLE IF NOT EXISTS alport_passkey_challenges (
 id TEXT PRIMARY KEY, session_hash TEXT NOT NULL, purpose TEXT NOT NULL, lane TEXT NOT NULL,
 subject TEXT, password_stamp TEXT, challenge TEXT NOT NULL, origin TEXT NOT NULL,
 label TEXT NOT NULL DEFAULT '', expires_at BIGINT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alport_passkey_limits (
 key TEXT PRIMARY KEY, window_start BIGINT NOT NULL, attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alport_passkey_events (
 id BIGSERIAL PRIMARY KEY, subject TEXT NOT NULL, event TEXT NOT NULL,
 passkey_id BIGINT, created_at BIGINT NOT NULL);
'''


class PasskeyError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def register_passkeys(app, env):
    conn = env['conn']
    bp = Blueprint('alport_passkeys', __name__)
    rp_id = app.config.get('WEBAUTHN_RP_ID') or os.environ.get('WEBAUTHN_RP_ID', 'alporthospitality.co.uk')
    configured = app.config.get('WEBAUTHN_ORIGINS') or os.environ.get(
        'WEBAUTHN_ORIGINS', 'https://alporthospitality.co.uk,https://www.alporthospitality.co.uk')
    origins = {x.strip().rstrip('/') for x in configured.split(',') if x.strip()}
    # Configuration errors disable this optional feature, not existing password sign-in.
    def valid_origin(origin):
        try:
            p = urlsplit(origin)
            return (p.scheme == 'https' or (p.scheme == 'http' and rp_id == 'localhost')) and \
                (p.hostname == rp_id or (p.hostname or '').endswith('.' + rp_id)) and \
                not p.username and not p.password and p.path == '' and not p.query and not p.fragment
        except ValueError:
            return False
    configured_ok = bool(origins and rp_id and all(valid_origin(o) for o in origins)
                         and app.secret_key and app.secret_key != 'CHANGE_THIS_IN_RENDER')

    def allowed_host():
        # Render terminates TLS upstream. Do not infer the public origin from its internal HTTP scheme.
        return any(urlsplit(o).netloc == request.host for o in origins)
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)', (739000000006,))
        for sql in SCHEMA.split(';'):
            if sql.strip():
                c.execute(sql)

    def now():
        return int(time.time())

    def digest(value):
        secret = app.secret_key
        if isinstance(secret, str):
            secret = secret.encode()
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    def nonce():
        if 'passkey_nonce' not in session:
            session['passkey_nonce'] = secrets.token_urlsafe(32)
        return session['passkey_nonce']

    def lane_ok(lane):
        if lane not in ('business', 'staff', 'company-admin'):
            raise PasskeyError('Unknown sign-in page.', 404)

    def limit(key, maximum, seconds=900):
        key = digest(key)
        exceeded = False
        with conn() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)', (int(key[:14], 16),))
            row = c.execute('SELECT * FROM alport_passkey_limits WHERE key=%s', (key,)).fetchone()
            start = row['window_start'] if row and row['window_start'] > now() - seconds else now()
            count = row['attempts'] + 1 if row and start == row['window_start'] else 1
            c.execute('''INSERT INTO alport_passkey_limits(key,window_start,attempts) VALUES(%s,%s,%s)
                ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,attempts=excluded.attempts''',
                      (key, start, count))
            exceeded = count > maximum
        if exceeded:
            raise PasskeyError('Too many attempts. Try again in 15 minutes or use password sign-in.', 429)

    def principal(c, subject):
        if subject.startswith('company:'):
            email = os.environ.get('COMPANY_ADMIN_EMAIL', '').strip().lower()
            password = os.environ.get('COMPANY_ADMIN_PASSWORD', '')
            if not email or not password or subject != 'company:' + email:
                raise PasskeyError('This account is no longer available.', 403)
            return {'subject': subject, 'name': email, 'secret': password, 'company': True}
        try:
            uid = int(subject.removeprefix('user:'))
        except ValueError:
            raise PasskeyError('Sign in with your password.', 403) from None
        u = c.execute('SELECT * FROM users WHERE id=%s AND active=1', (uid,)).fetchone()
        if not u:
            raise PasskeyError('This account is no longer available.', 403)
        memberships = c.execute('''SELECT a.*,e.active AS employee_active,s.active AS site_active
            FROM alport_staff_access a JOIN employees e ON e.id=a.employee_id AND e.organisation_id=a.organisation_id
            JOIN sites s ON s.id=a.site_id AND s.organisation_id=a.organisation_id
            WHERE a.user_id=%s AND a.organisation_id=%s ORDER BY a.id''', (uid, u['organisation_id'])).fetchall()
        live = next((a for a in memberships if a['profile'] != 'disabled' and a['employee_active'] == 1
                     and a['site_active'] == 1 and a['activated_at']), None)
        if (memberships or u['role'] in ('Staff', 'User')) and not live:
            raise PasskeyError('Staff access is inactive. Contact your manager.', 403)
        return {'subject': subject, 'name': u['email'], 'secret': u['password_hash'],
                'company': False, 'user': dict(u), 'staff': dict(live) if live else None}

    def stamp(p):
        return digest(p['subject'] + '\0' + p['secret'])

    def signed_in(c, lane):
        if lane == 'company-admin':
            email = session.get('company_admin_email', '')
            if not session.get('company_admin') or not email:
                raise PasskeyError('Sign in to company admin first.', 401)
            return principal(c, 'company:' + email)
        uid = session.get('user_id')
        if not uid:
            raise PasskeyError('Sign in with your password first.', 401)
        p = principal(c, 'user:' + str(uid))
        if p['staff'] and session.get('staff_auth_hash') != hashlib.sha256(p['secret'].encode()).hexdigest():
            raise PasskeyError('Sign in again with your password.', 401)
        return p

    def confirm_password(p, data):
        limit('password:' + p['subject'], 5)
        password = data.get('password')
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise PasskeyError('Enter your current password.', 403)
        correct = hmac.compare_digest(password.encode(), p['secret'].encode()) if p['company'] else \
            check_password_hash(p['secret'], password)
        if not correct:
            raise PasskeyError('The password is incorrect.', 403)

    def event(c, subject, kind, key_id=None):
        c.execute('INSERT INTO alport_passkey_events(subject,event,passkey_id,created_at) VALUES(%s,%s,%s,%s)',
                  (subject, kind, key_id, now()))

    def new_challenge(c, options, purpose, lane, p=None, label=''):
        fid = secrets.token_urlsafe(32)
        c.execute('DELETE FROM alport_passkey_challenges WHERE expires_at<%s', (now() - 3600,))
        c.execute('DELETE FROM alport_passkey_limits WHERE window_start<%s', (now() - 86400,))
        c.execute('''INSERT INTO alport_passkey_challenges
            (id,session_hash,purpose,lane,subject,password_stamp,challenge,origin,label,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (fid, digest(nonce()), purpose, lane, p['subject'] if p else None, stamp(p) if p else None,
             options['challenge'], request.headers['Origin'], label, now() + 300))
        return jsonify(flow=fid, options=options)

    def consume(data, purpose, lane):
        fid = data.get('flow')
        if not isinstance(fid, str) or len(fid) > 100:
            raise PasskeyError('Start again from the sign-in or passkey page.')
        # Committed before verification: even a failed assertion consumes its challenge.
        with conn() as c:
            row = c.execute('SELECT * FROM alport_passkey_challenges WHERE id=%s FOR UPDATE', (fid,)).fetchone()
            if not row or row['used'] or row['expires_at'] <= now() or row['session_hash'] != digest(nonce()) \
                    or row['purpose'] != purpose or row['lane'] != lane:
                raise PasskeyError('This request expired or was already used. Please start again.')
            c.execute('UPDATE alport_passkey_challenges SET used=1 WHERE id=%s', (fid,))
            return dict(row)

    def credential(data):
        cred = data.get('credential')
        if not isinstance(cred, dict):
            raise PasskeyError('The device did not return a passkey.')
        # Explicitly disallow iframe / cross-origin ceremonies.
        try:
            raw_client = cred['response']['clientDataJSON']
            if not isinstance(raw_client, str):
                raise ValueError()
            client = json.loads(base64url_to_bytes(raw_client))
            if not isinstance(client, dict) or not all(isinstance(client.get(k), str) for k in ('type', 'challenge', 'origin')) \
                    or client.get('crossOrigin', False) is not False or client.get('topOrigin'):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise PasskeyError('Open Alport directly in your browser and try again.') from None
        return cred

    @bp.before_request
    def protect():
        if request.method == 'POST':
            if request.content_length and request.content_length > 65536:
                raise PasskeyError('Request too large.', 413)
            if not configured_ok or not allowed_host() or request.headers.get('Origin') not in origins \
                    or urlsplit(request.headers.get('Origin', '')).netloc != request.host:
                raise PasskeyError('Use the secure Alport website to use passkeys.', 403)
            token = request.headers.get('X-Passkey-CSRF', '')
            if not session.get('passkey_nonce') or not hmac.compare_digest(token.encode(), digest(nonce()).encode()):
                raise PasskeyError('Reload this page and try again.', 403)
            if not isinstance(request.get_json(silent=True), dict):
                raise PasskeyError('Send valid input fields.')
            limit('ip:' + (request.remote_addr or 'unknown'), 240)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
        return response

    @bp.errorhandler(PasskeyError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @bp.get('/api/passkeys/config')
    def config():
        return jsonify(csrf=digest(nonce()), enabled=configured_ok and allowed_host())

    @bp.get('/passkeys')
    @bp.get('/company-admin/passkeys')
    def manage():
        lane = 'company-admin' if request.path.startswith('/company-admin/') else 'business'
        try:
            with conn() as c:
                p = signed_in(c, lane)
        except PasskeyError:
            return redirect('/company-admin/login' if lane == 'company-admin' else '/login')
        return render_template('passkeys.html', lane=lane, account=p['name'],
                               back='/company-admin' if p['company'] else '/staff' if p['staff'] else '/app')

    @bp.post('/api/passkeys/<lane>/register/options')
    def register_options(lane):
        lane_ok(lane)
        data = request.get_json()
        with conn() as c:
            p = signed_in(c, lane)
        confirm_password(p, data)
        label = data.get('label', '')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise PasskeyError('Give this passkey a name of 1–80 characters.')
        with conn() as c:
            c.execute('INSERT INTO alport_passkey_subjects(subject,user_handle) VALUES(%s,%s) ON CONFLICT(subject) DO NOTHING',
                      (p['subject'], secrets.token_urlsafe(32)))
            subject = c.execute('SELECT * FROM alport_passkey_subjects WHERE subject=%s FOR UPDATE', (p['subject'],)).fetchone()
            rows = c.execute('SELECT * FROM alport_passkeys WHERE subject=%s AND revoked_at IS NULL', (p['subject'],)).fetchall()
            if sum(r['password_stamp'] == stamp(p) for r in rows) >= 10:
                raise PasskeyError('Remove an unused passkey before adding another (maximum 10).')
            options = generate_registration_options(rp_id=rp_id, rp_name='Alport Hospitality Solutions',
                user_name=p['name'] + (' (Company admin)' if p['company'] else ' (Business ' + str(p['user']['organisation_id']) + ')'),
                user_id=base64url_to_bytes(subject['user_handle']),
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                    require_resident_key=True, user_verification=UserVerificationRequirement.REQUIRED),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r['credential_id'])) for r in rows
                                     if r['password_stamp'] == stamp(p)])
            return new_challenge(c, json.loads(options_to_json(options)), 'register', lane, p, label.strip())

    @bp.post('/api/passkeys/<lane>/register/verify')
    def register_verify(lane):
        lane_ok(lane)
        data = request.get_json()
        challenge = consume(data, 'register', lane)
        cred = credential(data)
        try:
            verified = verify_registration_response(credential=cred,
                expected_challenge=base64url_to_bytes(challenge['challenge']), expected_rp_id=rp_id,
                expected_origin=challenge['origin'], require_user_verification=True)
        except (WebAuthnException, ValueError, TypeError, KeyError):
            raise PasskeyError('The passkey could not be verified. Start again or use your password.') from None
        from webauthn.helpers import bytes_to_base64url
        cid = bytes_to_base64url(verified.credential_id)
        with conn() as c:
            p = signed_in(c, lane)
            if p['subject'] != challenge['subject'] or stamp(p) != challenge['password_stamp']:
                raise PasskeyError('Your account changed. Sign in again with your password.', 403)
            c.execute('SELECT * FROM alport_passkey_subjects WHERE subject=%s FOR UPDATE', (p['subject'],)).fetchone()
            rows = c.execute('SELECT * FROM alport_passkeys WHERE subject=%s AND revoked_at IS NULL', (p['subject'],)).fetchall()
            if sum(r['password_stamp'] == stamp(p) for r in rows) >= 10:
                raise PasskeyError('Remove an unused passkey before adding another.')
            added = c.execute('''INSERT INTO alport_passkeys(subject,credential_id,public_key,sign_count,password_stamp,label,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(credential_id) DO NOTHING RETURNING id''',
                (p['subject'], cid, verified.credential_public_key, verified.sign_count, stamp(p), challenge['label'], now())).fetchone()
            if not added:
                raise PasskeyError('That passkey is already registered. Create a new passkey on your device.')
            event(c, p['subject'], 'Registered', added['id'])
        return jsonify(ok=True)

    @bp.post('/api/passkeys/<lane>/authenticate/options')
    def auth_options(lane):
        lane_ok(lane)
        limit('session:' + nonce(), 40)
        options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        with conn() as c:
            return new_challenge(c, json.loads(options_to_json(options)), 'authenticate', lane)

    @bp.post('/api/passkeys/<lane>/authenticate/verify')
    def auth_verify(lane):
        lane_ok(lane)
        data = request.get_json()
        challenge = consume(data, 'authenticate', lane)
        cred = credential(data)
        cid = cred.get('id')
        if not isinstance(cid, str) or len(cid) > 2048:
            raise PasskeyError('Sign-in failed. Use your password or try another passkey.', 403)
        with conn() as c:
            row = c.execute('SELECT * FROM alport_passkeys WHERE credential_id=%s FOR UPDATE', (cid,)).fetchone()
            if not row or row['revoked_at'] is not None or row['subject'].startswith('company:') != (lane == 'company-admin'):
                raise PasskeyError('This passkey cannot sign in here. Use your password or another passkey.', 403)
            p = principal(c, row['subject'])
            if not hmac.compare_digest(row['password_stamp'], stamp(p)):
                raise PasskeyError('Your password changed. Sign in with it and add a new passkey.', 403)
            subject = c.execute('SELECT user_handle FROM alport_passkey_subjects WHERE subject=%s', (p['subject'],)).fetchone()
            try:
                handle = base64url_to_bytes(cred['response']['userHandle'])
                if not hmac.compare_digest(handle, base64url_to_bytes(subject['user_handle'])):
                    raise ValueError()
                verified = verify_authentication_response(credential=cred,
                    expected_challenge=base64url_to_bytes(challenge['challenge']), expected_rp_id=rp_id,
                    expected_origin=challenge['origin'], credential_public_key=bytes(row['public_key']),
                    credential_current_sign_count=row['sign_count'], require_user_verification=True)
            except (WebAuthnException, ValueError, TypeError, KeyError):
                raise PasskeyError('Sign-in failed. Use your password or try another passkey.', 403) from None
            if p['company']:
                values = dict(company_admin=True, company_admin_email=p['name'])
                destination = '/company-admin'
            else:
                u, a = p['user'], p['staff']
                sub = env['subscription_for'](u['organisation_id'])
                if env['subscription_blocks_access'](sub):
                    raise PasskeyError('Your business subscription needs attention.', 403)
                if lane == 'staff' and not a:
                    raise PasskeyError('Use the business sign-in page for this account.', 403)
                values = dict(user_id=u['id'])
                if a:
                    values.update(site_id=a['site_id'], staff_site_id=a['site_id'],
                                  staff_auth_hash=hashlib.sha256(u['password_hash'].encode()).hexdigest())
                    destination = '/staff'
                else:
                    site = c.execute('SELECT id FROM sites WHERE organisation_id=%s AND active=1 ORDER BY id LIMIT 1',
                                     (u['organisation_id'],)).fetchone()
                    if site:
                        values['site_id'] = site['id']
                    destination = '/subscribe' if sub and sub.get('status') == 'Payment required' else '/app'
            c.execute('UPDATE alport_passkeys SET sign_count=%s,last_used=%s WHERE id=%s',
                      (verified.new_sign_count, now(), row['id']))
            event(c, p['subject'], 'Signed in', row['id'])
        session.clear()
        session.update(values)
        return jsonify(ok=True, redirect=destination)

    @bp.get('/api/passkeys/<lane>/list')
    def list_keys(lane):
        lane_ok(lane)
        with conn() as c:
            p = signed_in(c, lane)
            rows = c.execute('''SELECT id,label,created_at,last_used,password_stamp FROM alport_passkeys
                WHERE subject=%s AND revoked_at IS NULL ORDER BY id''', (p['subject'],)).fetchall()
        return jsonify(keys=[dict(id=r['id'], label=r['label'], created_at=r['created_at'], last_used=r['last_used'],
                                  valid=hmac.compare_digest(r['password_stamp'], stamp(p))) for r in rows])

    @bp.post('/api/passkeys/<lane>/revoke/<int:key_id>')
    def revoke(lane, key_id):
        lane_ok(lane)
        with conn() as c:
            p = signed_in(c, lane)
        confirm_password(p, request.get_json())
        with conn() as c:
            row = c.execute('''UPDATE alport_passkeys SET revoked_at=%s WHERE id=%s AND subject=%s
                AND revoked_at IS NULL RETURNING id''', (now(), key_id, p['subject'])).fetchone()
            if not row:
                raise PasskeyError('Passkey not found.', 404)
            event(c, p['subject'], 'Removed', key_id)
        return jsonify(ok=True)

    app.register_blueprint(bp)
