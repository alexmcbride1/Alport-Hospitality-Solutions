"""Tronc approvals and Yapily bulk payments. Register after existing app routes.
No payroll tax engine: final deductions must come from the venue's payroll system.
"""
import calendar
import csv
import io
import json
import os
import re
import secrets
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode, quote, urlparse

import requests
from flask import Blueprint, request, session, jsonify, render_template, redirect, Response

SCHEMA = """
CREATE TABLE IF NOT EXISTS alport_money_records (
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL REFERENCES organisations(id),
 site_id BIGINT NOT NULL REFERENCES sites(id), kind TEXT NOT NULL,
 data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alport_money_scope ON alport_money_records(organisation_id,site_id,kind);
CREATE TABLE IF NOT EXISTS alport_money_claims (
 organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 kind TEXT NOT NULL, source_id BIGINT NOT NULL, record_id BIGINT NOT NULL REFERENCES alport_money_records(id),
 PRIMARY KEY(organisation_id,site_id,kind,source_id)
);
CREATE TABLE IF NOT EXISTS alport_money_events (
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 user_id BIGINT NOT NULL, record_id BIGINT, action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def pence(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0 or d > Decimal('100000000') or d != d.quantize(Decimal('.01')):
            raise ValueError()
        return int(d * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Use a non-negative amount with at most two decimal places.')


def split_pool(total, weighted):
    """Largest remainder allocation, deterministic by employee id; no missing pennies."""
    if not weighted or total <= 0:
        raise ValueError('A positive pool and at least one eligible worker are required.')
    rows, seen = [], set()
    for item in weighted:
        employee = int(item['employee_id'])
        weight = Decimal(str(item['weight']))
        if employee in seen or not weight.is_finite() or weight <= 0 or weight > 1000000:
            raise ValueError('Each worker needs a unique entry and a positive weight.')
        seen.add(employee)
        rows.append((employee, weight))
    divisor = sum(w for _, w in rows)
    exact = [(eid, Decimal(total) * weight / divisor) for eid, weight in rows]
    result = {eid: int(amount) for eid, amount in exact}
    remainder = total - sum(result.values())
    for eid, _ in sorted(exact, key=lambda x: (-(x[1] - int(x[1])), x[0]))[:remainder]:
        result[eid] += 1
    return result


def month_deadline(month):
    first = date.fromisoformat(month + '-01')
    year, mon = (first.year + 1, 1) if first.month == 12 else (first.year, first.month + 1)
    return date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def bank_details(name, sort_code, account):
    sort_code = re.sub(r'[ -]', '', str(sort_code))
    account = str(account).strip()
    if not str(name).strip() or len(str(name)) > 70 or not re.fullmatch(r'[0-9]{6}', sort_code) or not re.fullmatch(r'[0-9]{8}', account):
        raise ValueError('Enter the account holder, six-digit sort code and eight-digit account number.')
    return {'name': str(name).strip(), 'sort_code': sort_code, 'account': account}


class ProviderError(Exception):
    pass


def yapily(method, path, body=None, consent=None):
    app_id, secret = os.getenv('YAPILY_APPLICATION_ID'), os.getenv('YAPILY_APPLICATION_SECRET')
    if not app_id or not secret:
        raise ValueError('Yapily credentials are not configured. Local preparation is available.')
    headers = {'Accept': 'application/json'}
    if consent:
        headers['Consent'] = consent
    try:
        res = requests.request(method, 'https://api.yapily.com' + path, json=body,
                               auth=(app_id, secret), headers=headers, timeout=(5, 25), allow_redirects=False)
        if not 200 <= res.status_code < 300:
            raise ProviderError('Yapily rejected the request. Check the provider console; no automatic retry was made.')
        result = res.json()
        return result.get('data', result)
    except (requests.RequestException, ValueError):
        raise ProviderError('The provider result is uncertain. Reconcile in Yapily before attempting another payment.') from None


def register_payments(app, host):
    db = host['conn']
    encrypt, decrypt = host['encrypt_staff'], host['decrypt_staff']
    bp = Blueprint('money', __name__)
    with db() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ('alport-money-schema-v1',))
        c.execute(SCHEMA)

    def context():
        u, s = host['user'](), host['current_site']()
        if not u or not s:
            raise PermissionError('Sign in and select a site.')
        return u, s, (u['organisation_id'], s['id'])

    def guard(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                u, s, scope = context()
                if u['role'] not in ('Owner', 'Admin', 'Finance', 'General Manager', 'Manager'):
                    # An explicitly appointed employee login may operate the independent tronc.
                    with db() as c:
                        config = settings(c, scope)
                    if u['id'] != config.get('allocator_id'):
                        raise PermissionError('Payments or appointed tronc permissions required.')
                if request.method != 'GET':
                    token = request.headers.get('X-Money-CSRF', '')
                    if not token or not secrets.compare_digest(token, session.get('money_csrf', '')):
                        raise PermissionError('Refresh this page before submitting.')
                return fn(*args, **kwargs)
            except PermissionError as e:
                return jsonify(error=str(e)), 403
            except (ValueError, KeyError, TypeError, InvalidOperation) as e:
                return jsonify(error=str(e) if isinstance(e, ValueError) else 'Invalid or incomplete request.'), 400
            except ProviderError as e:
                return jsonify(error=str(e)), 502
        return wrapped

    def finance(u):
        if u['role'] not in ('Owner', 'Admin', 'Finance'):
            raise PermissionError('Owner, Admin or Finance permission required.')

    def lock(c, scope):
        # Serialises all workflow transitions within this site, including legacy pay actions.
        c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('alport-money:%s:%s' % scope,))

    def records(c, scope, kind):
        return c.execute('SELECT id,data,created_at FROM alport_money_records WHERE organisation_id=%s AND site_id=%s AND kind=%s ORDER BY id DESC', (*scope, kind)).fetchall()

    def get(c, scope, rid, kind):
        row = c.execute('SELECT id,data FROM alport_money_records WHERE id=%s AND organisation_id=%s AND site_id=%s AND kind=%s FOR UPDATE', (rid, *scope, kind)).fetchone()
        if not row:
            raise ValueError('Record not found in this site.')
        return row['data']

    def put(c, scope, kind, data, rid=None):
        if rid:
            c.execute('UPDATE alport_money_records SET data=%s::jsonb WHERE id=%s AND organisation_id=%s AND site_id=%s AND kind=%s', (json.dumps(data), rid, *scope, kind))
            return rid
        return c.execute('INSERT INTO alport_money_records(organisation_id,site_id,kind,data) VALUES(%s,%s,%s,%s::jsonb) RETURNING id', (*scope, kind, json.dumps(data))).fetchone()['id']

    def event(c, scope, uid, rid, action, detail=''):
        c.execute('INSERT INTO alport_money_events(organisation_id,site_id,user_id,record_id,action,detail) VALUES(%s,%s,%s,%s,%s,%s)', (*scope, uid, rid, action, detail))

    def settings(c, scope):
        rows = records(c, scope, 'settings')
        return rows[0]['data'] if rows else {}

    def source(c, scope, kind, sid):
        table = {'employee': 'employees', 'supplier': 'suppliers', 'invoice': 'invoices', 'payroll': 'payroll_runs'}[kind]
        row = c.execute(f'SELECT * FROM {table} WHERE id=%s AND organisation_id=%s' + ('' if kind == 'supplier' else ' AND site_id=%s') + ' FOR UPDATE', (sid, scope[0]) if kind == 'supplier' else (sid, *scope)).fetchone()
        if not row:
            raise ValueError('Source record not found in this site.')
        return row

    def claim(c, scope, kind, sid, rid):
        found = c.execute('SELECT record_id FROM alport_money_claims WHERE organisation_id=%s AND site_id=%s AND kind=%s AND source_id=%s', (*scope, kind, sid)).fetchone()
        if found:
            raise ValueError('This item is already linked to another run or bank batch.')
        c.execute('INSERT INTO alport_money_claims VALUES(%s,%s,%s,%s,%s)', (*scope, kind, sid, rid))

    def banks():
        allowed = {x.strip() for x in os.getenv('YAPILY_LIVE_INSTITUTIONS', '').split(',') if x.strip()}
        live = os.getenv('YAPILY_LIVE_ENABLED') == 'true'
        return [x for x in yapily('GET', '/institutions')
                if {'CREATE_BULK_PAYMENT', 'INITIATE_BULK_PAYMENT'} <= set(x.get('features', []))
                and (x.get('environmentType') == 'SANDBOX' or (live and x['id'] in allowed))]

    @bp.get('/money')
    @guard
    def page():
        session.setdefault('money_csrf', secrets.token_urlsafe(32))
        return render_template('money.html', csrf=session['money_csrf'])

    @bp.get('/api/money')
    @guard
    def overview():
        u, s, scope = context()
        with db() as c:
            config = settings(c, scope)
            result = {'site': s['name'], 'role': u['role'], 'user_id': u['id'], 'settings': config,
                      'configured': bool(os.getenv('YAPILY_APPLICATION_ID') and os.getenv('YAPILY_APPLICATION_SECRET')),
                      'live_enabled': os.getenv('YAPILY_LIVE_ENABLED') == 'true'}
            for kind in ('pool', 'payroll', 'beneficiary', 'batch'):
                result[kind] = []
                for row in records(c, scope, kind):
                    d = dict(row['data'])
                    if kind == 'batch' and d.get('authoriser_id') != u['id']:
                        d.pop('authorisation_url', None)
                    for key in ('bank', 'payload', 'consent', 'callback_secret', 'provider_consent_id'):
                        d.pop(key, None)
                    result[kind].append(dict(id=row['id'], **d))
            for key, table in (('employees', 'employees'), ('suppliers', 'suppliers'), ('invoices', 'invoices')):
                fields = 'id,name' if key != 'invoices' else 'id,supplier,invoice_number,gross,status'
                result[key] = c.execute(f'SELECT {fields} FROM {table} WHERE organisation_id=%s' + ('' if key == 'suppliers' else ' AND site_id=%s') + ' ORDER BY id', (scope[0],) if key == 'suppliers' else scope).fetchall()
            result['users'] = c.execute('SELECT id,name,role FROM users WHERE organisation_id=%s AND active=1 ORDER BY name', (scope[0],)).fetchall()
            result['events'] = c.execute('SELECT user_id,record_id,action,detail,created_at FROM alport_money_events WHERE organisation_id=%s AND site_id=%s ORDER BY id DESC LIMIT 100', scope).fetchall()
            # Independent allocators need allocations, not invoice/bank/payment information.
            if u['role'] not in ('Owner', 'Admin', 'Finance', 'General Manager', 'Manager'):
                for key in ('beneficiary','batch','payroll','invoices','suppliers','events'):
                    result[key] = []
        return jsonify(result)

    @bp.post('/api/money/settings')
    @guard
    def configure():
        u, s, scope = context()
        if u['role'] not in ('Owner','Admin'):
            raise PermissionError('Only an Owner or Admin can assign site approval responsibilities.')
        d = request.get_json()
        if d['mode'] not in ('Employer allocated', 'Independent tronc') or len(d.get('policy','').strip()) < 20:
            raise ValueError('Select the arrangement and record a meaningful written allocation policy.')
        with db() as c:
            lock(c, scope)
            for field in ('allocator_id', 'manager_id'):
                d[field] = int(d[field])
                person = c.execute('SELECT id,role FROM users WHERE id=%s AND organisation_id=%s AND active=1', (d[field],scope[0])).fetchone()
                if not person or (field == 'manager_id' and person['role'] not in ('Owner','Admin','General Manager','Manager')):
                    raise ValueError('Select an active allocator and a manager for this site.')
                if field == 'allocator_id' and d['mode'] == 'Independent tronc' and person['role'] in ('Owner','Admin'):
                    raise ValueError('An owner/admin cannot be appointed as the independent allocator here.')
            if d['mode'] == 'Independent tronc' and d['allocator_id'] == d['manager_id']:
                raise ValueError('Use separate independent allocator and manager accounts.')
            rows = records(c, scope, 'settings')
            # Do not change responsible people or policy underneath an outstanding approval.
            if any(r['data']['status'] in ('Draft','Review','Approved') for r in records(c,scope,'pool')):
                raise ValueError('Complete outstanding pools before changing the policy or responsible users.')
            clean = {k:d[k] for k in ('mode','allocator_id','manager_id','policy')}
            clean['updated_at'] = stamp()
            rid = put(c, scope, 'settings', clean, rows[0]['id'] if rows else None)
            event(c, scope, u['id'], rid, 'Policy and site responsibilities saved')
        return jsonify(ok=True)

    @bp.post('/api/money/pools')
    @guard
    def save_pool():
        u, s, scope = context()
        d = request.get_json()
        with db() as c:
            lock(c, scope)
            config = settings(c, scope)
            if u['id'] != config.get('allocator_id'):
                raise PermissionError('Only the appointed allocator can prepare or change allocations.')
            rid = int(d.get('id') or 0) or None
            previous = get(c,scope,rid,'pool') if rid else None
            if previous and previous['status'] != 'Draft':
                raise ValueError('Only a draft or returned pool can be changed.')
            total = pence(d['total'])
            allocated = split_pool(total, d['weights'])
            lines = []
            for eid, amount in sorted(allocated.items()):
                employee = source(c,scope,'employee',eid)
                if not employee['active']:
                    raise ValueError('The allocation contains an inactive worker.')
                lines.append({'employee_id':eid,'name':employee['name'],'pence':amount})
            if not str(d.get('source_reference','')).strip():
                raise ValueError('Record the till report or source reference for this pool.')
            for row in records(c,scope,'pool'):
                if row['id'] != rid and row['data']['source_reference'] == d['source_reference'].strip():
                    raise ValueError('This tip source reference is already recorded.')
            pool = {'month':d['month'],'deadline':month_deadline(d['month']), 'total_pence':total,
                    'source_reference':d['source_reference'].strip(), 'weights':d['weights'], 'lines':lines,
                    'policy':config['policy'],'mode':config['mode'],'allocator_id':u['id'],
                    'manager_id':config['manager_id'],'status':'Draft','revision':(previous or {}).get('revision',0)+1}
            rid = put(c,scope,'pool',pool,rid)
            event(c,scope,u['id'],rid,'Tronc draft saved',json.dumps({'revision':pool['revision'],'total_pence':total,'lines':lines}))
        return jsonify(ok=True,id=rid)

    @bp.post('/api/money/pools/<int:rid>/<action>')
    @guard
    def pool_action(rid, action):
        u,s,scope = context()
        with db() as c:
            lock(c,scope)
            d = get(c,scope,rid,'pool')
            if action == 'submit':
                if u['id'] != d['allocator_id'] or d['status'] != 'Draft':
                    raise PermissionError('Only the allocator may submit a draft.')
                d['status'] = 'Review'
            elif action in ('approve','return'):
                if u['id'] != d['manager_id'] or d['status'] != 'Review':
                    raise PermissionError('Only the assigned site manager may review this allocation.')
                if action == 'return':
                    reason = str((request.get_json() or {}).get('reason','')).strip()
                    if not reason:
                        raise ValueError('Explain the correction required.')
                    d['status'], d['return_reason'] = 'Draft', reason
                else:
                    d.update(status='Approved',approved_by=u['id'],approved_at=stamp())
            else:
                raise ValueError('Unknown action.')
            put(c,scope,'pool',d,rid)
            event(c,scope,u['id'],rid,'Tronc '+action,json.dumps({'revision':d['revision'],'reason':d.get('return_reason','')}))
        return jsonify(ok=True)

    @bp.get('/api/money/pools/<int:rid>/export')
    @guard
    def export_pool(rid):
        u,s,scope = context()
        with db() as c:
            d = get(c,scope,rid,'pool')
        if d['status'] not in ('Approved','Payroll'):
            raise ValueError('The site manager must approve before payroll export.')
        buf=io.StringIO(); writer=csv.writer(buf)
        writer.writerow(['Pool','Month','Employee ID','Employee','Gross tips GBP','Arrangement','Approved at'])
        for line in d['lines']:
            name=line['name']
            if name.startswith(('=','+','-','@')): name="'"+name
            writer.writerow([rid,d['month'],line['employee_id'],name,format(Decimal(line['pence'])/100,'.2f'),d['mode'],d['approved_at']])
        return Response(buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=tronc-{rid}.csv'})

    @bp.post('/api/money/beneficiaries')
    @guard
    def save_beneficiary():
        u,s,scope=context(); finance(u); d=request.get_json()
        if d['kind'] not in ('employee','supplier'):
            raise ValueError('Choose employee or supplier.')
        if not os.getenv('STAFF_DATA_KEY') or not app.secret_key or len(app.secret_key) < 32 or app.secret_key == 'CHANGE_THIS_IN_RENDER':
            raise ValueError('Configure stable encryption and session keys before saving bank details; see INSTALL.md.')
        bank=bank_details(d['name'],d['sort_code'],d['account'])
        with db() as c:
            lock(c,scope)
            src=source(c,scope,d['kind'],int(d['source_id']))
            rid=put(c,scope,'beneficiary',{'kind':d['kind'],'source_id':int(d['source_id']),
                  'label':src['name'],'account_name':bank['name'],'last4':bank['account'][-4:],
                  'bank':encrypt(json.dumps(bank)), 'status':'Unverified','created_by':u['id']})
            event(c,scope,u['id'],rid,'Recipient bank details added; verification required')
        return jsonify(ok=True,id=rid)

    @bp.post('/api/money/beneficiaries/<int:rid>/verify')
    @guard
    def verify_beneficiary(rid):
        u,s,scope=context(); finance(u)
        evidence=str((request.get_json() or {}).get('evidence','')).strip()
        if len(evidence)<12:
            raise ValueError('Record how the account details were independently verified, without sensitive bank details.')
        with db() as c:
            lock(c,scope); d=get(c,scope,rid,'beneficiary')
            if d['status'] != 'Unverified':
                raise ValueError('Only newly entered bank details can be verified. Add a new version to replace an account.')
            if d['created_by']==u['id']:
                raise PermissionError('A different Owner, Admin or Finance user must verify bank details.')
            # A replacement verification retires previous details; prepared snapshots must be rebuilt.
            for row in records(c,scope,'beneficiary'):
                old=row['data']
                if old['kind']==d['kind'] and old['source_id']==d['source_id'] and row['id']!=rid:
                    old['status']='Retired';put(c,scope,'beneficiary',old,row['id'])
            d.update(status='Verified',verified_by=u['id'],verified_at=stamp())
            put(c,scope,'beneficiary',d,rid);event(c,scope,u['id'],rid,'Recipient verified',evidence)
        return jsonify(ok=True)

    @bp.post('/api/money/payroll')
    @guard
    def payroll():
        u,s,scope=context();finance(u);d=request.get_json()
        if d.get('confirmed') is not True or not str(d.get('calculation_reference','')).strip():
            raise ValueError('Confirm final payroll calculations and supply the payroll system/report reference.')
        start,end=date.fromisoformat(d['period_start']),date.fromisoformat(d['period_end'])
        if end<start: raise ValueError('Invalid payroll period.')
        with db() as c:
            lock(c,scope)
            pool_id=int(d.get('pool_id') or 0)
            pool=get(c,scope,pool_id,'pool') if pool_id else None
            if pool and pool['status']!='Approved': raise ValueError('Site-manager approval is required before payroll.')
            tips={x['employee_id']:x['pence'] for x in pool['lines']} if pool else {}
            lines=[];seen=set()
            for raw in d['lines']:
                eid=int(raw['employee_id']); emp=source(c,scope,'employee',eid)
                if eid in seen: raise ValueError('Duplicate employee in payroll.')
                seen.add(eid)
                base,deductions,cost=pence(raw['base_gross']),pence(raw['deductions']),pence(raw.get('employer_cost',0))
                tip=tips.get(eid,0)
                if pool and pool['mode']=='Independent tronc' and base:
                    raise ValueError('Independent tronc runs must be separate from wages. Enter zero basic gross and tronc-scheme deductions only.')
                net=base+tip-deductions
                if net<0: raise ValueError('Deductions exceed gross pay.')
                lines.append({'employee_id':eid,'name':emp['name'],'base_pence':base,'tips_pence':tip,'deductions_pence':deductions,'employer_pence':cost,'net_pence':net})
            if not lines or not set(tips)<=seen: raise ValueError('Include every worker in the approved tronc allocation.')
            gross=sum(x['base_pence']+x['tips_pence'] for x in lines);net=sum(x['net_pence'] for x in lines)
            if gross<=0 or net<=0: raise ValueError('Payroll must have positive gross and net totals.')
            ref=d['calculation_reference'].strip()
            if any(x['data']['calculation_reference']==ref for x in records(c,scope,'payroll')):
                raise ValueError('This payroll calculation reference has already been imported.')
            payid=c.execute("""INSERT INTO payroll_runs(organisation_id,site_id,period_start,period_end,gross_pay,employer_costs,deductions,net_pay,status,created_at,approved_by,approved_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'Approved',%s,%s,%s) RETURNING id""",
             (*scope,start.isoformat(),end.isoformat(),gross/100,sum(x['employer_pence'] for x in lines)/100,
              sum(x['deductions_pence'] for x in lines)/100,net/100,stamp(),u['id'],stamp())).fetchone()['id']
            data={'run_id':payid,'pool_id':pool_id,'period_start':start.isoformat(),'period_end':end.isoformat(),
                  'lines':lines,'net_pence':net,'calculation_reference':ref,'approved_by':u['id'],'approved_at':stamp()}
            rid=put(c,scope,'payroll',data)
            if pool:
                claim(c,scope,'pool',pool_id,rid);pool['status']='Payroll';pool['payroll_record_id']=rid;put(c,scope,'pool',pool,pool_id)
            c.execute("""INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
              VALUES(%s,%s,'Payroll','Staff payroll',%s,%s,%s,'Scheduled','Yapily',%s,%s,%s)""",(*scope,'PAY-'+str(payid),net/100,end.isoformat(),payid,u['id'],stamp()))
            event(c,scope,u['id'],rid,'Final payroll imported',ref)
        return jsonify(ok=True,id=rid)

    @bp.get('/api/money/banks')
    @guard
    def list_banks():
        u,_,_=context();finance(u)
        return jsonify(banks=[{'id':x['id'],'name':x.get('name',x['id']),'environment':x.get('environmentType')} for x in banks()])

    def beneficiary(c,scope,rid,kind,sid):
        b=get(c,scope,rid,'beneficiary')
        if b['status']!='Verified' or b['kind']!=kind or b['source_id']!=sid:
            raise ValueError('Select the verified recipient for this employee or supplier.')
        bank=json.loads(decrypt(b['bank']))
        return b,bank

    @bp.post('/api/money/batches')
    @guard
    def create_batch():
        u,s,scope=context();finance(u);d=request.get_json()
        if d['kind'] not in ('invoice','payroll'): raise ValueError('Invalid payment type.')
        with db() as c:
            lock(c,scope); lines=[];claims=[]
            if d['kind']=='invoice':
                seen=set()
                for item in d['items']:
                    iid=int(item['invoice_id'])
                    if iid in seen: raise ValueError('Duplicate invoice.')
                    seen.add(iid);inv=source(c,scope,'invoice',iid)
                    if inv['status']!='Approved': raise ValueError('Every invoice must be approved and unpaid.')
                    supplier=source(c,scope,'supplier',int(item['supplier_id']))
                    if supplier['name'].strip().casefold()!=inv['supplier'].strip().casefold():
                        raise ValueError('Supplier record must match the invoice supplier name.')
                    bid=int(item['beneficiary_id']);b,bank=beneficiary(c,scope,bid,'supplier',supplier['id'])
                    lines.append({'source_id':iid,'beneficiary_id':bid,'name':bank['name'],'last4':bank['account'][-4:],
                                  'pence':pence(inv['gross']),'reference':str(inv['invoice_number'])[:18],'bank':bank})
                    claims.append(('invoice',iid))
            else:
                pr=get(c,scope,int(d['payroll_id']),'payroll'); run=source(c,scope,'payroll',pr['run_id'])
                if run['status']!='Approved': raise ValueError('Payroll is not approved and unpaid.')
                if pence(run['net_pay'])!=pr['net_pence']: raise ValueError('Payroll total changed; investigate before payment.')
                for line in pr['lines']:
                    if not line['net_pence']: continue
                    bid=int(d['beneficiaries'][str(line['employee_id'])]);b,bank=beneficiary(c,scope,bid,'employee',line['employee_id'])
                    lines.append({'source_id':line['employee_id'],'beneficiary_id':bid,'name':bank['name'],'last4':bank['account'][-4:],
                                  'pence':line['net_pence'],'reference':'PAY-'+str(pr['run_id']),'bank':bank})
                claims.append(('payroll',pr['run_id']))
            if not lines or len(lines)>500 or any(x['pence']<=0 for x in lines):
                raise ValueError('A batch must contain 1–500 positive payments.')
            payload={'payments':[]}
            for line in lines:
                key=uuid.uuid4().hex;line['key']=key;bank=line.pop('bank')
                payload['payments'].append({'type':'DOMESTIC_PAYMENT','reference':line['reference'],
                    'paymentIdempotencyId':key,'amount':{'amount':line['pence']/100,'currency':'GBP'},
                    'payee':{'name':bank['name'],'accountIdentifications':[{'type':'ACCOUNT_NUMBER','identification':bank['account']},
                    {'type':'SORT_CODE','identification':bank['sort_code']}],'address':{'country':'GB'}}})
            data={'kind':d['kind'],'lines':lines,'claims':claims,'total_pence':sum(x['pence'] for x in lines),
                  'status':'Prepared','created_by':u['id'],'payload':encrypt(json.dumps(payload))}
            rid=put(c,scope,'batch',data)
            for kind,sid in claims: claim(c,scope,kind,sid,rid)
            event(c,scope,u['id'],rid,'Payment batch prepared',str(data['total_pence']))
        return jsonify(ok=True,id=rid)

    @bp.post('/api/money/batches/<int:rid>/cancel')
    @guard
    def cancel_batch(rid):
        u,s,scope=context();finance(u)
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            if d['status']!='Prepared': raise ValueError('Only a batch never sent for bank authorisation may be cancelled here.')
            d['status']='Cancelled';put(c,scope,'batch',d,rid)
            c.execute('DELETE FROM alport_money_claims WHERE organisation_id=%s AND site_id=%s AND record_id=%s AND kind IN (%s,%s)',(*scope,rid,'invoice','payroll'))
            event(c,scope,u['id'],rid,'Unsubmitted payment batch cancelled')
        return jsonify(ok=True)

    @bp.post('/api/money/batches/<int:rid>/authorise')
    @guard
    def authorise(rid):
        u,s,scope=context();finance(u);d=request.get_json()
        chosen=next((x for x in banks() if x['id']==d['institution_id']),None)
        if not chosen: raise ValueError('This bank is not enabled for bulk payments in this environment.')
        base=os.getenv('ALPORT_PUBLIC_URL','').rstrip('/')
        if urlparse(base).scheme!='https' or not urlparse(base).netloc:
            raise ValueError('Configure ALPORT_PUBLIC_URL with the HTTPS address of Alport.')
        if not os.getenv('STAFF_DATA_KEY') or app.secret_key=='CHANGE_THIS_IN_RENDER':
            raise ValueError('Configure a stable STAFF_DATA_KEY and a strong SECRET_KEY before connecting Yapily.')
        with db() as c:
            lock(c,scope);batch=get(c,scope,rid,'batch')
            if batch['status']!='Prepared': raise ValueError('This batch has already entered authorisation. Do not duplicate it.')
            for line in batch['lines']:
                b=get(c,scope,line['beneficiary_id'],'beneficiary')
                if b['status']!='Verified': raise ValueError('Recipient details have changed. Cancel this prepared batch and rebuild.')
            for kind,sid in batch['claims']:
                if source(c,scope,kind,sid)['status']!='Approved': raise ValueError('A source is no longer approved and unpaid.')
            token=secrets.token_urlsafe(32)
            batch.update(status='Authorising',institution_id=chosen['id'],sandbox=chosen.get('environmentType')=='SANDBOX',
                         authoriser_id=u['id'],callback_secret=token,authorisation_started=stamp())
            put(c,scope,'batch',batch,rid)
            event(c,scope,u['id'],rid,'Bank authorisation requested')
        # Persist Authorising before network I/O. A crash/timeout never re-enables submission.
        result=yapily('POST','/bulk-payment-auth-requests',{
            'applicationUserId':f'alport-{scope[0]}-{scope[1]}-{u["id"]}', 'institutionId':chosen['id'],
            'callback':base+'/money/callback?'+urlencode({'batch':rid,'state':token}), 'oneTimeToken':True,
            'paymentRequest':json.loads(decrypt(batch['payload']))})
        url=result.get('authorisationUrl','')
        if urlparse(url).scheme!='https': raise ProviderError('No secure bank authorisation URL was returned.')
        with db() as c:
            lock(c,scope);batch=get(c,scope,rid,'batch')
            batch.update(status='Awaiting bank approval',provider_consent_id=result['id'],authorisation_url=url)
            put(c,scope,'batch',batch,rid)
        return jsonify(url=url)

    @bp.get('/money/callback')
    @guard
    def callback():
        u,s,scope=context();finance(u)
        rid=int(request.args.get('batch','0'))
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            if d.get('authoriser_id')!=u['id'] or not secrets.compare_digest(request.args.get('state',''),d.get('callback_secret','!')):
                raise PermissionError('Invalid payment callback or wrong signed-in user/site.')
            if d['status']!='Awaiting bank approval': raise ValueError('This callback has already been handled.')
            if request.args.get('error'):
                d['status']='Authorisation failed';put(c,scope,'batch',d,rid)
                event(c,scope,u['id'],rid,'Bank authorisation failed')
                return redirect('/money')
            ott=request.args.get('one-time-token') or request.args.get('oneTimeToken')
            if not ott: raise ValueError('No one-time token returned. Check Yapily callback configuration.')
            d['status']='Exchanging consent';put(c,scope,'batch',d,rid)
        result=yapily('POST','/consent-one-time-token',{'oneTimeToken':ott})
        if (result.get('id')!=d['provider_consent_id'] or result.get('institutionId')!=d['institution_id']
            or result.get('status')!='AUTHORIZED' or not result.get('consentToken')):
            raise PermissionError('The returned consent does not match this payment batch.')
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            d.update(status='Ready to submit',consent=encrypt(result['consentToken']))
            d.pop('callback_secret',None);d.pop('authorisation_url',None)
            put(c,scope,'batch',d,rid);event(c,scope,u['id'],rid,'Bank consent verified')
        response=redirect('/money');response.headers['Referrer-Policy']='no-referrer'
        return response

    @bp.post('/api/money/batches/<int:rid>/submit')
    @guard
    def submit(rid):
        u,s,scope=context();finance(u)
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            if d['status']!='Ready to submit' or d['authoriser_id']!=u['id']:
                raise ValueError('The bank authoriser must submit this authorised batch exactly once.')
            for line in d['lines']:
                if get(c,scope,line['beneficiary_id'],'beneficiary')['status']!='Verified':
                    raise ValueError('Bank details were replaced after approval. Reconcile/cancel at the provider before proceeding.')
            d['status']='Submission uncertain';put(c,scope,'batch',d,rid)
            event(c,scope,u['id'],rid,'Payment submission started')
        result=yapily('POST','/bulk-payments',json.loads(decrypt(d['payload'])),decrypt(d['consent']))
        if not result.get('id'): raise ProviderError('Provider did not return a payment ID; reconcile before any retry.')
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            d.update(status='Submitted',provider_id=result['id'],provider_status=result.get('status','UNKNOWN'))
            put(c,scope,'batch',d,rid);event(c,scope,u['id'],rid,'Payment batch submitted',result['id'])
        return jsonify(ok=True)

    @bp.post('/api/money/batches/<int:rid>/refresh')
    @guard
    def refresh(rid):
        u,s,scope=context();finance(u)
        with db() as c: d=get(c,scope,rid,'batch')
        if not d.get('provider_id'): raise ValueError('No provider payment ID. Inspect uncertain submissions in Yapily; do not resend.')
        result=yapily('GET','/bulk-payments/'+quote(d['provider_id'],safe=''),consent=decrypt(d['consent']))
        status=(result.get('statusDetails') or {}).get('status') or result.get('status','UNKNOWN')
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch');d['provider_status']=status
            if d['status']!='Reconciled': d['status']='Sandbox result' if d['sandbox'] else 'Awaiting reconciliation'
            put(c,scope,'batch',d,rid);event(c,scope,u['id'],rid,'Provider status checked',status)
        return jsonify(ok=True)

    @bp.post('/api/money/batches/<int:rid>/reconcile')
    @guard
    def reconcile(rid):
        u,s,scope=context();finance(u);body=request.get_json()
        evidence=str(body.get('evidence','')).strip()
        if len(evidence)<12 or body.get('all_paid') is not True:
            raise ValueError('Confirm every recipient was paid and record the bank-statement/report reference.')
        with db() as c:
            lock(c,scope);d=get(c,scope,rid,'batch')
            if d.get('sandbox'): raise ValueError('Sandbox transactions can never mark real payroll or invoices paid.')
            if d['status']!='Awaiting reconciliation': raise ValueError('Refresh the submitted payment before reconciling.')
            for kind,sid in d['claims']:
                table='invoices' if kind=='invoice' else 'payroll_runs'
                c.execute(f"UPDATE {table} SET status='Paid',paid_at=%s WHERE id=%s AND organisation_id=%s AND site_id=%s",(stamp(),sid,*scope))
                c.execute("UPDATE payments SET status='Paid',paid_at=%s,external_reference=%s WHERE source_id=%s AND organisation_id=%s AND site_id=%s AND payment_type=%s",
                          (stamp(),d['provider_id'],sid,*scope,'Supplier' if kind=='invoice' else 'Payroll'))
            d.update(status='Reconciled',reconciled_by=u['id'],reconciled_at=stamp())
            put(c,scope,'batch',d,rid);event(c,scope,u['id'],rid,'All payments reconciled against bank evidence',evidence)
        return jsonify(ok=True)

    @app.after_request
    def money_headers(response):
        if request.path.startswith(('/money','/api/money')):
            response.headers['Cache-Control']='no-store'
            response.headers['Referrer-Policy']='no-referrer'
            response.headers['X-Content-Type-Options']='nosniff'
        return response

    # Existing Record paid endpoints must not bypass the new workflow or race batch creation.
    for endpoint, kind in (('pay_invoice','invoice'),('pay_payroll','payroll')):
        original=app.view_functions[endpoint]
        def protect(original=original,kind=kind):
            @wraps(original)
            def wrapped(*args,**kwargs):
                try:
                    u,s,scope=context()
                    sid=kwargs.get('iid') if kind=='invoice' else kwargs.get('rid')
                    with db() as c:
                        lock(c,scope)
                        claimed=c.execute('SELECT 1 FROM alport_money_claims WHERE organisation_id=%s AND site_id=%s AND kind=%s AND source_id=%s',(*scope,kind,sid)).fetchone()
                        if claimed or (kind=='payroll' and any(x['data']['run_id']==sid for x in records(c,scope,'payroll'))):
                            return jsonify(error='Use Payments & Tronc to track and reconcile this payment.'),409
                        return original(*args,**kwargs)
                except PermissionError:
                    return jsonify(error='Sign in first.'),403
            return wrapped
        app.view_functions[endpoint]=protect()
    app.register_blueprint(bp)
