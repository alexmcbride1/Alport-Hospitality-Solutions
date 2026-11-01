"""Site-scoped pub records. Writes, stock adjustments and audit events commit together."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import secrets
from flask import Blueprint, g, jsonify, render_template, request, session, redirect, Response


def field(key,label,kind='text',required=True,choices=None):
    return dict(key=key,label=label,type=kind,required=required,choices=choices or [])


def module(label,fields,note='',manager=False):
    return dict(label=label,fields=fields,note=note,manager=manager)

D=field('date','Record date','date')
MODULES={
 'cashup':module('Daily cashing up',[D,field('reference','Till / session reference'),field('opening_float','Opening float (£)','money'),field('cash_sales','Cash sales before cash refunds (£)','money'),field('cash_refunds','Cash refunds (£)','money'),field('paid_out','Paid-outs (£)','money'),field('cash_counted','Cash counted including float (£)','money'),field('card_sales','Card sales before card refunds (£)','money'),field('card_refunds','Card refunds (£)','money'),field('card_received','Net card receipts (£)','money'),field('discounts','Discounts already reflected in sales (£)','money'),field('notes','Variance explanation / notes','textarea',False)],'Enter separate till/session references. Expected cash = opening float + cash sales − cash refunds − paid-outs. Discounts are informational and already included in sales. Counts are management records, not postings to the sales ledger.',True),
 'licence':module('Premises licence',[D,field('jurisdiction','Venue jurisdiction','choice',True,['England','Wales','Scotland','Northern Ireland','Other']),field('authority','Issuing authority'),field('reference','Licence / certificate reference'),field('supervisor','DPS / premises manager / responsible person'),field('hours','Actual permitted hours, days and exceptions','textarea'),field('conditions','Conditions copied from the actual licence','textarea'),field('due_date','Next review / fee deadline','date'),field('notes','Deadline type and source document','textarea')],'Record the actual licence and local requirements. No permitted hours or review interval is assumed. This register does not certify compliance.',True),
 'refusal':module('Refused sale',[D,field('time','Time','time'),field('reason','Reason','choice',True,['Age verification','Intoxication','Other']),field('details','Facts and action taken','textarea'),field('reference','Incident / internal reference','text',False)],'Record only necessary facts. Do not enter identity document numbers or unnecessary personal details. Staff can see their own reports; managers can see site reports.'),
 'incident':module('Incident log',[D,field('time','Time','time'),field('category','Category','choice',True,['Disorder','Injury','Complaint','Security','Other']),field('details','Facts and immediate action','textarea'),field('reference','External / internal reference','text',False),field('due_date','Follow-up due','date',False)],'Use emergency procedures when needed. This record does not contact emergency services. Staff see their own reports; managers review the site log.'),
 'cleaning':module('Beer line cleaning',[D,field('line','Line / tap'),field('method','Cleaning method and checks','textarea'),field('due_date','Next clean due','date'),field('notes','Notes','textarea',False)],'Use your supplier’s cleaning procedure and frequency; no automatic interval is imposed.'),
 'temperature':module('Cellar temperature',[D,field('time','Time','time'),field('location','Location'),field('temperature','Measured temperature (°C)','number'),field('minimum','Venue target minimum (°C)','number'),field('maximum','Venue target maximum (°C)','number'),field('notes','Corrective action / notes','textarea',False)],'Set targets for your products and equipment. Out-of-range readings require review; the app does not declare the cellar safe.'),
 'cellar':module('Cask / keg change and yield',[D,field('stock_id','Stock item','stock'),field('line','Line / tap'),field('reference','Batch / keg reference'),field('volume_ml','Container volume (ml)','positive'),field('serve_ml','Serving volume (ml)','positive'),field('sold','Servings sold so far','number'),field('waste_ml','Recorded waste volume (ml)','number'),field('remaining_ml','Remaining volume (ml)','number'),field('due_date','Review / use-by date from product guidance','date'),field('notes','Source of sales count / notes','textarea')],'Yield uses entered container and serving volumes, sold servings, waste and remaining volume. Opening a keg does not deduct stock. Record stock wastage separately in the stock item’s own units.'),
 'waste':module('Cellar stock wastage',[D,field('stock_id','Stock item','stock'),field('quantity','Quantity lost in this stock item’s unit','positive'),field('reason','Reason','textarea')],'Saving deducts this quantity from on-hand stock and records a Waste movement atomically. This record is immutable; use an authorised stock adjustment for corrections.')
}
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_pub_records(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,kind TEXT NOT NULL,
 data TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Open',version INTEGER NOT NULL DEFAULT 1,
 created_by BIGINT NOT NULL,created_at TEXT NOT NULL,updated_by BIGINT NOT NULL,updated_at TEXT NOT NULL,
 signed_by BIGINT,signed_at TEXT,request_key TEXT NOT NULL,request_hash TEXT NOT NULL,
 UNIQUE(organisation_id,site_id,request_key));
CREATE INDEX IF NOT EXISTS alport_pub_scope ON alport_pub_records(organisation_id,site_id,kind,id);
CREATE TABLE IF NOT EXISTS alport_pub_events(
 id BIGSERIAL PRIMARY KEY,record_id BIGINT NOT NULL REFERENCES alport_pub_records(id),actor_id BIGINT NOT NULL,
 action TEXT NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def stamp():return datetime.now(timezone.utc).isoformat(timespec='microseconds')
def dump(d):return json.dumps(d,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(d):return hashlib.sha256(dump(d).encode()).hexdigest()
def decimal(value,low=0,high=100000000):
    try:n=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ValueError('Enter a valid number.') from None
    if not n.is_finite() or not Decimal(str(low))<=n<=Decimal(str(high)):raise ValueError('Number is out of range.')
    return n

def cashup(d):
    n=lambda k:Decimal(d[k])
    expected=n('opening_float')+n('cash_sales')-n('cash_refunds')-n('paid_out')
    cards=n('card_sales')-n('card_refunds')
    return {'expected_cash':str(expected),'cash_variance':str(n('cash_counted')-expected),'expected_cards':str(cards),'card_variance':str(n('card_received')-cards)}

def yield_summary(d):
    volume=Decimal(str(d['volume_ml']));serve=Decimal(str(d['serve_ml']))
    accounted=Decimal(str(d['sold']))*serve+Decimal(str(d['waste_ml']))+Decimal(str(d['remaining_ml']))
    return {'expected_servings':str((volume/serve).quantize(Decimal('.01'))),'unaccounted_ml':str(volume-accounted),'sold_yield_percent':str((Decimal(str(d['sold']))*serve/volume*100).quantize(Decimal('.01')))}


def register_pub(app,env):
    q,conn=env['q'],env['conn'];bp=Blueprint('alport_pub',__name__)
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000031,))
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    def scope(sid,manager=False):
        u=env['user']()
        if not u:raise PermissionError('Sign in first.')
        s=next((s for s in app.extensions['alport_experience_sites'](u) if s['id']==sid),None)
        if not s or (manager and not s['manager']):raise PermissionError('This site or action is not available to your account.')
        return u,s
    def lock(c,org):c.execute('SELECT pg_advisory_xact_lock(%s)',(748000000000+org,))
    def row(c,rid,u,s):
        r=c.execute('SELECT * FROM alport_pub_records WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,u['organisation_id'],s['id'])).fetchone()
        if not r:raise PermissionError('Record not found in this site.')
        if MODULES[r['kind']]['manager'] and not s['manager']:raise PermissionError('Manager access required.')
        if r['kind'] in ('incident','refusal') and not s['manager'] and r['created_by']!=u['id']:raise PermissionError('This report is restricted to its author and site managers.')
        return r
    def event(c,rid,u,action,d):c.execute('INSERT INTO alport_pub_events(record_id,actor_id,action,snapshot,created_at) VALUES(%s,%s,%s,%s,%s)',(rid,u['id'],action,dump(d),stamp()))
    def validated(kind,d,u,s,c):
        if not isinstance(d,dict):raise ValueError('Send valid fields.')
        clean={}
        for f in MODULES[kind]['fields']:
            v=d.get(f['key']);t=f['type']
            if v in (None,''):
                if f['required']:raise ValueError('Enter '+f['label']+'.')
                clean[f['key']]='';continue
            if t in ('money','number','positive'):
                n=decimal(v,-100 if f['key'] in ('temperature','minimum','maximum') else 0)
                if t=='positive' and n<=0:raise ValueError(f['label']+' must be greater than zero.')
                if t=='money' and n!=n.quantize(Decimal('.01')):raise ValueError('Use at most two decimal places for money.')
                clean[f['key']]=str(n.quantize(Decimal('.01'))) if t=='money' else float(n)
            elif t in ('stock','user','menu'):
                if type(v) is not int or v<1:raise ValueError('Choose '+f['label']+'.')
                table={'stock':'stock_items','user':'users','menu':'menu_items'}[t]
                sql=f'SELECT id FROM {table} WHERE id=%s AND organisation_id=%s AND active=1';args=(v,u['organisation_id'])
                if t!='user':sql+=' AND site_id=%s';args+=(s['id'],)
                if not c.execute(sql,args).fetchone():raise ValueError('Selected item is not active in this site.')
                if t=='user' and not any(x['id']==s['id'] for x in app.extensions['alport_experience_sites'](q('SELECT * FROM users WHERE id=?',(v,),True))):raise ValueError('Assignee must have site access.')
                clean[f['key']]=v
            else:
                if not isinstance(v,str) or len(v)>4000 or any(ord(ch)<32 and ch not in '\n\t' for ch in v):raise ValueError('Use text of up to 4,000 characters.')
                v=v.strip()
                if f['required'] and not v:raise ValueError('Enter '+f['label']+'.')
                if t=='date':
                    try:v=date.fromisoformat(v).isoformat()
                    except ValueError:raise ValueError('Use a valid date.') from None
                if t=='time' and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v):raise ValueError('Use a valid time.')
                if t=='choice' and v not in f['choices']:raise ValueError('Choose a listed option.')
                clean[f['key']]=v
        if kind not in ('licence','cashflow') and clean.get('date','')>date.today().isoformat():raise ValueError('Actual records cannot be dated in the future.')
        if kind=='repair':
            if clean['repair_status']=='Done' and (not clean['completion_date'] or not clean['notes']):raise ValueError('Completed repairs need a completion date and notes.')
            if clean['completion_date'] and (clean['completion_date']<clean['date'] or clean['completion_date']>date.today().isoformat()):raise ValueError('Completion date must be between the record date and today.')
            if clean['repair_status']!='Done' and clean['completion_date']:raise ValueError('Only completed repairs have a completion date.')
        if kind=='cashup':clean.update(cashup(clean))
        if kind=='cellar':clean.update(yield_summary(clean))
        if kind=='temperature':
            if clean['minimum']>clean['maximum']:raise ValueError('Minimum cannot exceed maximum.')
            clean['outside_target']=not clean['minimum']<=clean['temperature']<=clean['maximum']
        return clean
    @bp.before_request
    def guard():
        u=env['user']()
        if not u:return redirect('/login') if not request.path.startswith('/api/') else (jsonify(error='Sign in first.'),401)
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):return jsonify(error='Business subscription needs attention.'),403
        session.setdefault('pub_csrf',secrets.token_urlsafe(32))
        request.max_content_length=120000
        if request.method=='POST':
            if not hmac.compare_digest(session['pub_csrf'],request.headers.get('X-Pub-CSRF','')):raise PermissionError('Reload before saving.')
            if not isinstance(request.get_json(silent=True),dict):raise ValueError('Send valid fields.')
    @bp.after_request
    def headers(r):
        r.headers['Cache-Control']='private, no-store';r.headers['X-Content-Type-Options']='nosniff';r.vary.add('Cookie')
        r.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return r
    @bp.errorhandler(PermissionError)
    def denied(e):return jsonify(error=str(e)),403
    @bp.errorhandler(ValueError)
    def invalid(e):return jsonify(error=str(e)),400
    @bp.get('/pub-operations')
    def page():return render_template('pub_operations.html')
    @bp.get('/api/pub/config')
    def config():
        u=env['user']();return jsonify(sites=app.extensions['alport_experience_sites'](u),modules=MODULES,csrf=session['pub_csrf'],user_id=u['id'])
    @bp.get('/api/pub/sites/<int:sid>/options')
    def options(sid):
        u,s=scope(sid);args=(u['organisation_id'],sid)
        stock=q('SELECT id,name,unit FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',args)
        menu=q('SELECT id,name FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',args)
        users=[{'id':p['id'],'name':p['name']} for p in q('SELECT * FROM users WHERE organisation_id=? AND active=1 ORDER BY name',(u['organisation_id'],)) if any(x['id']==sid for x in app.extensions['alport_experience_sites'](p))]
        return jsonify(stock=stock,menu=menu,user=users)
    @bp.get('/api/pub/sites/<int:sid>/records/<kind>')
    def records(sid,kind):
        if kind not in MODULES:raise ValueError('Unknown section.')
        u,s=scope(sid,MODULES[kind]['manager']);before=request.args.get('before','9223372036854775807')
        if not re.fullmatch(r'[0-9]{1,19}',before) or int(before)>9223372036854775807:raise ValueError('Invalid page.')
        sql='SELECT r.*,u.name AS author FROM alport_pub_records r JOIN users u ON u.id=r.created_by WHERE r.organisation_id=? AND r.site_id=? AND r.kind=? AND r.id<?';args=[u['organisation_id'],sid,kind,int(before)]
        if kind in ('incident','refusal') and not s['manager']:sql+=' AND r.created_by=?';args.append(u['id'])
        rows=q(sql+' ORDER BY r.id DESC LIMIT 51',args)
        return jsonify(records=[{**r,'data':json.loads(r['data'])} for r in rows[:50]],has_more=len(rows)>50)
    @bp.post('/api/pub/sites/<int:sid>/records/<kind>')
    def create(sid,kind):
        if kind not in MODULES:raise ValueError('Unknown section.')
        payload=request.get_json();key=payload.get('request_key','')
        if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}',key):raise ValueError('Reload before saving.')
        with conn() as c:
            u,s=scope(sid,MODULES[kind]['manager']);lock(c,u['organisation_id']);d=validated(kind,payload.get('data'),u,s,c);signature=digest({'kind':kind,'data':d})
            old=c.execute('SELECT id,request_hash FROM alport_pub_records WHERE organisation_id=%s AND site_id=%s AND request_key=%s',(u['organisation_id'],sid,key)).fetchone()
            if old:
                if old['request_hash']!=signature:return jsonify(error='This save belongs to different input.'),409
                return jsonify(id=old['id'],duplicate=True)
            if kind=='cashup':
                for old in c.execute("SELECT data FROM alport_pub_records WHERE organisation_id=%s AND site_id=%s AND kind='cashup'",(u['organisation_id'],sid)).fetchall():
                    previous=json.loads(old['data'])
                    if previous['date']==d['date'] and previous['reference'].casefold()==d['reference'].casefold():return jsonify(error='This date and till/session already has a cash-up. Edit the existing record.'),409
            if kind=='waste':
                changed=c.execute('UPDATE stock_items SET on_hand=on_hand-%s WHERE id=%s AND organisation_id=%s AND site_id=%s AND on_hand>=%s RETURNING id',(d['quantity'],d['stock_id'],u['organisation_id'],sid,d['quantity'])).fetchone()
                if not changed:raise ValueError('Wastage exceeds stock on hand. Check the unit and quantity.')
                c.execute('INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(u['organisation_id'],sid,d['stock_id'],-d['quantity'],'Waste','Cellar: '+d['reason'],d['date']+'T12:00:00+00:00'))
            r=c.execute('INSERT INTO alport_pub_records(organisation_id,site_id,kind,data,created_by,created_at,updated_by,updated_at,request_key,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(u['organisation_id'],sid,kind,dump(d),u['id'],stamp(),u['id'],stamp(),key,signature)).fetchone()
            event(c,r['id'],u,'Created',d)
        return jsonify(id=r['id']),201
    @bp.post('/api/pub/sites/<int:sid>/records/<int:rid>/update')
    def update(sid,rid):
        payload=request.get_json()
        with conn() as c:
            u,s=scope(sid);lock(c,u['organisation_id']);r=row(c,rid,u,s)
            if not s['manager'] and r['created_by']!=u['id']:raise PermissionError('Only the author or a site manager can edit.')
            if r['signed_at'] or r['kind']=='waste':raise ValueError('Signed cash-ups and stock wastage records are immutable.')
            if type(payload.get('version')) is not int or payload.get('version')!=r['version']:return jsonify(error='Record changed. Reload before editing.'),409
            d=validated(r['kind'],payload.get('data'),u,s,c)
            if r['kind']=='cashup' and any(d[k]!=json.loads(r['data'])[k] for k in ('date','reference')):raise ValueError('The cash-up date and reference cannot be changed.')
            c.execute('UPDATE alport_pub_records SET data=%s,version=version+1,updated_by=%s,updated_at=%s WHERE id=%s',(dump(d),u['id'],stamp(),rid));event(c,rid,u,'Updated',d)
        return jsonify(ok=True)
    @bp.post('/api/pub/sites/<int:sid>/records/<int:rid>/sign')
    def sign(sid,rid):
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);r=row(c,rid,u,s)
            if r['kind']!='cashup':raise ValueError('Only cash-ups use this sign-off.')
            if r['signed_at'] or type(request.json.get('version')) is not int or request.json.get('version')!=r['version']:return jsonify(error='Cash-up changed or is already signed.'),409
            d=json.loads(r['data'])
            if (Decimal(d['cash_variance']) or Decimal(d['card_variance'])) and not d['notes']:raise ValueError('Explain the variance before signing.')
            c.execute("UPDATE alport_pub_records SET signed_by=%s,signed_at=%s,status='Signed',version=version+1 WHERE id=%s",(u['id'],stamp(),rid));event(c,rid,u,'Manager signed',d)
        return jsonify(ok=True)
    @bp.get('/api/pub/sites/<int:sid>/records/<int:rid>/history')
    def history(sid,rid):
        with conn() as c:u,s=scope(sid);row(c,rid,u,s)
        rows=q('SELECT e.*,u.name AS actor FROM alport_pub_events e JOIN users u ON u.id=e.actor_id WHERE e.record_id=? ORDER BY e.id DESC',(rid,))
        return jsonify(events=[{**r,'snapshot':json.loads(r['snapshot'])} for r in rows])
    def attention(org,sid):
        rows=q('SELECT kind,data,status FROM alport_pub_records WHERE organisation_id=? AND site_id=? ORDER BY id',(org,sid));counts={'Unsigned cash-ups':0,'Licence deadlines':0,'Line cleans due':0,'Cellar reviews due':0,'Temperature exceptions':0}
        today=date.today().isoformat();latest={}
        for r in rows:
            d=json.loads(r['data']);kind=r['kind']
            if kind=='cashup' and r['status']!='Signed':counts['Unsigned cash-ups']+=1
            if kind in ('licence','cleaning','cellar'):
                key=(kind,d.get('reference') if kind!='cleaning' else d.get('line'))
                if key not in latest or d['date']>=latest[key]['date']:latest[key]=d
            if kind=='temperature' and d.get('outside_target') and d['date']==today:counts['Temperature exceptions']+=1
        for (kind,_),d in latest.items():
            if d.get('due_date','9999')<=today:counts[{'licence':'Licence deadlines','cleaning':'Line cleans due','cellar':'Cellar reviews due'}[kind]]+=1
        return [{'label':label,'count':count,'url':'/pub-operations?site='+str(sid)} for label,count in counts.items() if count]
    app.extensions['alport_pub_attention']=attention
    app.extensions['alport_pub_services']=dict(bp=bp,scope=scope,row=row,event=event,lock=lock,validated=validated,q=q,conn=conn,guard=guard,headers=headers)
    app.register_blueprint(bp)
