"""Customer workspace: explicit site scope, operational summaries and owner visibility."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import calendar
import re
from flask import Blueprint, jsonify, render_template, request, session, redirect

UK = ZoneInfo('Europe/London')


def register_experience(app, env):
    q = env['q']
    bp = Blueprint('alport_experience', __name__)

    def sites(u):
        rows = q('SELECT id,name FROM sites WHERE organisation_id=? AND active=1 ORDER BY name,id', (u['organisation_id'],))
        if u['role'] == 'Owner':
            return [{**s, 'manager': True} for s in rows]
        assignments = q('''SELECT a.site_id,a.profile FROM alport_staff_access a
            JOIN employees e ON e.id=a.employee_id AND e.organisation_id=a.organisation_id AND e.site_id=a.site_id
            WHERE a.user_id=? AND a.organisation_id=? AND a.profile<>'disabled'
            AND a.activated_at IS NOT NULL AND a.activated_at<>'' AND e.active=1''', (u['id'], u['organisation_id']))
        profiles = {a['site_id']: a['profile'] for a in assignments}
        return [{**s, 'manager': profiles[s['id']] == 'full' and u['role'] in ('Admin','General Manager','Manager')}
                for s in rows if s['id'] in profiles]

    @bp.before_request
    def protect():
        u = env['user']()
        if not u:
            return redirect('/login') if request.path == '/today' else (jsonify(error='Sign in first.'), 401)
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):
            return jsonify(error='Business subscription needs attention.'), 403

    @bp.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.vary.add('Cookie')
        return response

    @bp.errorhandler(PermissionError)
    def forbidden(e): return jsonify(error=str(e)), 403

    @bp.errorhandler(ValueError)
    def invalid(e): return jsonify(error=str(e)), 400

    def scope(sid):
        u = env['user']()
        s = next((s for s in sites(u) if s['id'] == sid), None)
        if not s: raise PermissionError('You do not have access to this site.')
        return u, s

    def month_range():
        month = request.args.get('month', datetime.now(UK).strftime('%Y-%m'))
        if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', month): raise ValueError('Choose a month between 2000 and 2099.')
        start = date.fromisoformat(month+'-01')
        end = start + timedelta(days=calendar.monthrange(start.year, start.month)[1])
        return month, start, end

    def finance(org, sid, start, end):
        args = (org, sid, start.isoformat(), end.isoformat())
        sales = q('SELECT COUNT(*) AS n,COALESCE(SUM(gross),0) AS value,MAX(sale_date) AS latest FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<?', args, True)
        costs = q('''SELECT si.category, SUM(ABS(sm.quantity)*si.unit_cost) AS value, COUNT(*) AS n, MAX(sm.created_at) AS latest
            FROM stock_movements sm JOIN stock_items si ON si.id=sm.stock_item_id AND si.organisation_id=sm.organisation_id AND si.site_id=sm.site_id
            WHERE sm.organisation_id=? AND sm.site_id=? AND sm.created_at>=? AND sm.created_at<?
            AND LOWER(sm.movement_type) IN ('usage','waste','sale','cogs') GROUP BY si.category''', args)
        shifts = q('''SELECT sh.start_time,sh.end_time,sh.break_minutes,e.pay_rate,sh.shift_date FROM shifts sh
            JOIN employees e ON e.id=sh.employee_id AND e.organisation_id=sh.organisation_id AND e.site_id=sh.site_id
            WHERE sh.organisation_id=? AND sh.site_id=? AND sh.shift_date>=? AND sh.shift_date<? AND sh.status NOT IN ('Cancelled','Declined')''', args)
        labour = 0
        for sh in shifts:
            a,b = (sum(int(v)*m for v,m in zip(t.split(':')[:2], (60,1))) for t in (sh['start_time'],sh['end_time']))
            if b <= a: b += 1440
            labour += max(0,b-a-sh['break_minutes'])/60 * float(sh['pay_rate'] or 0)
        expense = q("SELECT COUNT(*) AS n,COALESCE(SUM(gross),0) AS value,MAX(expense_date) AS latest FROM expenses WHERE organisation_id=? AND site_id=? AND expense_date>=? AND expense_date<? AND category NOT IN ('Food','Drink','Labour')", args, True)
        actual = {'revenue':float(sales['value']), 'food_cost':sum(float(c['value']) for c in costs if c['category'].lower()=='food'),
                  'cogs':sum(float(c['value']) for c in costs), 'labour':labour, 'overheads':float(expense['value'])}
        actual['ebitda'] = actual['revenue']-actual['cogs']-labour-actual['overheads']
        missing = []
        for label, present in (('sales',sales['n']),('stock usage',costs),('shifts',shifts),('overheads',expense['n'])):
            if not present: missing.append('No '+label+' records in this period; a zero may mean missing data.')
        latest = {'sales':sales['latest'], 'stock_usage':max((c['latest'] for c in costs),default=None),
                  'shifts':max((s['shift_date'] for s in shifts),default=None), 'overheads':expense['latest']}
        return {'actual':{k:round(v,2) for k,v in actual.items()}, 'warnings':missing, 'source_dates':latest}

    def operations(org, sid):
        day = datetime.now(UK).date().isoformat()
        args = (org,sid)
        def count(sql, extra=()): return q(sql,args+extra,True)['n']
        return {'date':day,
            'overdue_checks':count("SELECT COUNT(*) AS n FROM eho_records WHERE organisation_id=? AND site_id=? AND due_date<>'' AND due_date<? AND status NOT IN ('Complete','Resolved','Verified')",(day,)),
            'opening_recorded':bool(count("SELECT COUNT(*) AS n FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND check_type='Opening'",(day,))),
            'bookings':count("SELECT COUNT(*) AS n FROM bookings WHERE organisation_id=? AND site_id=? AND booking_date=? AND status NOT IN ('Cancelled','No show','No-show')",(day,)),
            'covers':q("SELECT COALESCE(SUM(party_size),0) AS n FROM bookings WHERE organisation_id=? AND site_id=? AND booking_date=? AND status NOT IN ('Cancelled','No show','No-show')",args+(day,),True)['n'],
            'scheduled_staff':count("SELECT COUNT(DISTINCT employee_id) AS n FROM shifts WHERE organisation_id=? AND site_id=? AND shift_date=? AND status NOT IN ('Cancelled','Declined')",(day,)),
            'low_stock':count('SELECT COUNT(*) AS n FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 AND on_hand<par_level'),
            'open_tasks':count("SELECT COUNT(*) AS n FROM alport_chat_tasks WHERE organisation_id=? AND site_id=? AND status<>'Done'"),
            'overdue_tasks':count("SELECT COUNT(*) AS n FROM alport_chat_tasks WHERE organisation_id=? AND site_id=? AND status<>'Done' AND due_date<?",(day,))}

    @bp.get('/today')
    def page(): return render_template('today.html')

    @bp.get('/api/today/config')
    def config():
        u = env['user']()
        return jsonify(sites=sites(u), owner=u['role']=='Owner', name=u['name'], month=datetime.now(UK).strftime('%Y-%m'))

    @bp.get('/api/today/sites/<int:sid>')
    def detail(sid):
        u,s = scope(sid)
        if not s['manager']:
            return jsonify(site=s, personal=True, message='Your shifts, checks and tasks are available through the quick actions below.')
        month,start,end = month_range()
        prior_end = start
        prior_start = (start-timedelta(days=1)).replace(day=1)
        current = finance(u['organisation_id'],sid,start,end)
        previous = finance(u['organisation_id'],sid,prior_start,prior_end)
        budget = q('SELECT revenue,food_cost,drink_cost,labour,overheads FROM budgets WHERE organisation_id=? AND site_id=? AND month=?',(u['organisation_id'],sid,month),True)
        if budget:
            budget = {k:float(v) for k,v in budget.items()}
            budget['cogs']=budget['food_cost']+budget['drink_cost']
            budget['ebitda']=budget['revenue']-budget['cogs']-budget['labour']-budget['overheads']
        checks = [
            ('Site details', bool(q('SELECT id FROM sites WHERE id=? AND address<>\'\'',(sid,),True)), '/app?section=sites'),
            ('Staff records', bool(q('SELECT id FROM employees WHERE organisation_id=? AND site_id=? AND active=1 LIMIT 1',(u['organisation_id'],sid))), '/staff-management'),
            ('Activated staff access', bool(q("SELECT id FROM alport_staff_access WHERE organisation_id=? AND site_id=? AND activated_at IS NOT NULL AND activated_at<>'' AND profile<>'disabled' LIMIT 1",(u['organisation_id'],sid))), '/staff-management'),
            ('Opening stock', bool(q("SELECT id FROM stocktakes WHERE organisation_id=? AND site_id=? AND status='Complete' LIMIT 1",(u['organisation_id'],sid))), '/app?section=stock'),
            ('This month’s budget', bool(budget), '/app?section=budgets'),
            ('Sales entered', bool(current['source_dates']['sales']), '/app?section=sales')]
        return jsonify(pub_attention=app.extensions.get('alport_pub_attention',lambda org,sid:[])(u['organisation_id'],sid), site=s, month=month, refreshed_at=datetime.now(UK).isoformat(), operations=operations(u['organisation_id'],sid),
            finance=current, previous=previous, budget=budget,
            setup=[{'label':label,'done':done,'url':url} for label,done,url in checks])

    @bp.get('/api/today/overview')
    def overview():
        u=env['user']()
        if u['role']!='Owner': raise PermissionError('Only the business Owner can compare all sites.')
        month,start,end=month_range()
        return jsonify(month=month,sites=[{'site':s,'operations':operations(u['organisation_id'],s['id']),
            'finance':finance(u['organisation_id'],s['id'],start,end)} for s in sites(u)])

    @bp.get('/api/today/sites/<int:sid>/accountability')
    def accountability(sid):
        u,s=scope(sid)
        if not s['manager']: raise PermissionError('Site manager access required.')
        staff=q('''SELECT u.name,u.role,a.profile,a.activated_at,e.active FROM alport_staff_access a
            JOIN users u ON u.id=a.user_id AND u.organisation_id=a.organisation_id
            JOIN employees e ON e.id=a.employee_id AND e.organisation_id=a.organisation_id AND e.site_id=a.site_id
            WHERE a.organisation_id=? AND a.site_id=? ORDER BY u.name''',(u['organisation_id'],sid))
        checks=q('SELECT signed_name AS actor,check_type AS action,completed_at AS at FROM eho_daily_checks WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 25',(u['organisation_id'],sid))
        approvals=q("SELECT u.name AS actor,'Payroll approved' AS action,p.approved_at AS at FROM payroll_runs p JOIN users u ON u.id=p.approved_by WHERE p.organisation_id=? AND p.site_id=? AND p.approved_at IS NOT NULL ORDER BY p.id DESC LIMIT 25",(u['organisation_id'],sid))
        # The existing audit table has no site_id: only expose it to the Owner,
        # and explicitly label it as business-wide instead of guessing a site.
        audit=[]
        if u['role']=='Owner':
            audit=q('SELECT a.action,a.entity,a.entity_id,a.created_at,u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id=a.user_id WHERE a.organisation_id=? ORDER BY a.id DESC LIMIT 50',(u['organisation_id'],))
        return jsonify(staff=staff,activity=sorted(checks+approvals,key=lambda r:r['at'] or '',reverse=True)[:50],business_audit=audit)

    @bp.post('/api/today/sites/<int:sid>/open')
    def open_site(sid):
        u,s=scope(sid)
        # CSRF uses the existing chat token initialized by the hub config client.
        import hmac
        if not session.get('chat_csrf') or not hmac.compare_digest(session['chat_csrf'],request.headers.get('X-Chat-CSRF','')):
            raise PermissionError('Reload before opening a site.')
        session['site_id']=sid;session['staff_site_id']=sid
        return jsonify(ok=True)

    app.extensions['alport_experience_sites']=sites
    app.register_blueprint(bp)
