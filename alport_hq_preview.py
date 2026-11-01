"""Explicit, audited HQ entry into one selected business account and site."""
import hmac
import secrets
from flask import request,session,redirect,abort


def register_hq_preview(app,env):
    @app.context_processor
    def preview_form_context():
        if request.endpoint=='company_admin_home' and env['company_admin_logged_in']():
            session.setdefault('hq_preview_csrf',secrets.token_urlsafe(32))
            return {'hq_preview_csrf':session['hq_preview_csrf']}
        return {}

    @app.post('/company-admin/venue-preview')
    @env['company_admin_required']
    def company_admin_venue_preview():
        token=session.get('hq_preview_csrf','')
        if not token or not hmac.compare_digest(token.encode(),request.form.get('csrf','').encode()):
            abort(403,description='This preview form expired. Return to company HQ and try again.')
        try:
            oid=int(request.form.get('organisation_id',''))
            sid=int(request.form.get('site_id',''))
            uid=int(request.form.get('user_id',''))
            if min(oid,sid,uid)<1:raise ValueError()
        except (TypeError,ValueError):abort(400,description='Choose the business, active site and owner account explicitly.')
        q=env['q']
        site=q('SELECT id,name FROM sites WHERE id=? AND organisation_id=? AND active=1',(sid,oid),True)
        owner=q("""SELECT id,name,email FROM users WHERE id=? AND organisation_id=? AND active=1 AND role='Owner'
            AND NOT EXISTS (SELECT 1 FROM alport_staff_access a WHERE a.user_id=users.id)""",(uid,oid),True)
        if not site or not owner:abort(403,description='The selected active site and owner must belong to this business.')
        if env['subscription_blocks_access'](env['subscription_for'](oid)):
            abort(403,description='This business is suspended, cancelled or overdue. Preview does not override that restriction.')
        admin=session.get('company_admin_email','')
        # Record the original HQ identity before changing the business session.
        # No subscription, payment, password or persistent entitlement is changed.
        env['company_event']('Venue preview opened',f'HQ {admin} selected owner user {uid}, site {sid}',oid)
        session.clear()
        session.update(company_admin=True,company_admin_email=admin,user_id=uid,site_id=sid,
                       billing_preview_admin=True,billing_preview_bypass_org=oid,billing_preview_user_id=uid)
        return redirect('/app?setup_section=dashboard',303)
