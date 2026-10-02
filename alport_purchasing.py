"""Internal purchase orders and audited receipts. No supplier messages or payments."""
import csv
import io
import json
import secrets
import hmac
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from flask import Blueprint, request, session, jsonify, render_template, Response
from alport_inventory import number, converted

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alport_purchase_orders (
 id BIGSERIAL PRIMARY KEY, organisation_id BIGINT NOT NULL, site_id BIGINT NOT NULL,
 supplier_id BIGINT NOT NULL REFERENCES suppliers(id), supplier_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'Draft', version INTEGER NOT NULL DEFAULT 1,
 expected_date TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 source_draft_id BIGINT, created_by BIGINT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(organisation_id,site_id,source_draft_id,supplier_id));
CREATE TABLE IF NOT EXISTS alport_purchase_lines (
 id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL REFERENCES alport_purchase_orders(id),
 stock_item_id BIGINT NOT NULL REFERENCES stock_items(id), product_id BIGINT NOT NULL,
 ingredient_name TEXT NOT NULL, product_name TEXT NOT NULL, stock_unit TEXT NOT NULL,
 pack_quantity NUMERIC(16,6) NOT NULL, pack_unit TEXT NOT NULL, units_per_pack NUMERIC(16,6) NOT NULL,
 pack_price NUMERIC(16,4) NOT NULL, packs NUMERIC(16,6) NOT NULL CHECK(packs>0),
 received_packs NUMERIC(16,6) NOT NULL DEFAULT 0 CHECK(received_packs>=0 AND received_packs<=packs));
CREATE TABLE IF NOT EXISTS alport_purchase_events (
 id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL REFERENCES alport_purchase_orders(id),
 event_type TEXT NOT NULL, actor_id BIGINT NOT NULL, created_at TEXT NOT NULL, payload JSONB NOT NULL,
 request_key TEXT, UNIQUE(order_id,request_key));
'''

def outstanding(c, org, sid, today, end):
    """Only placed, dated, unreceived quantities count. Overdue goods remain exceptions."""
    rows=c.execute('''SELECT l.stock_item_id,l.stock_unit,l.packs,l.received_packs,l.units_per_pack,
        p.id,p.expected_date FROM alport_purchase_lines l JOIN alport_purchase_orders p ON p.id=l.order_id
        WHERE p.organisation_id=%s AND p.site_id=%s AND p.status IN ('Ordered','Part received')''',(org,sid)).fetchall()
    result={}
    for r in rows:
        remain=(Decimal(str(r['packs']))-Decimal(str(r['received_packs'])))*Decimal(str(r['units_per_pack']))
        if remain<=0:continue
        bucket=result.setdefault(r['stock_item_id'],[])
        bucket.append({'order_id':r['id'],'unit':r['stock_unit'],'quantity':float(remain),'date':r['expected_date'],
                       'eligible':today<=r['expected_date']<=end})
    return result

def register_purchasing(app,env):
    conn,q=env['conn'],env['q']
    with conn() as c:
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    bp=Blueprint('alport_purchasing',__name__)
    def context():
        u,s=env['user'](),env['current_site']()
        if not u or not s:raise ValueError('Sign in and select a venue.')
        return u,u['organisation_id'],s['id']
    def lock(c,sid):c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(sid),))
    def allrows(c,sql,args=()):return [dict(r) for r in c.execute(sql,args).fetchall()]
    def load(c,oid,org,sid):
        r=c.execute('SELECT * FROM alport_purchase_orders WHERE id=%s AND organisation_id=%s AND site_id=%s',(oid,org,sid)).fetchone()
        if not r:raise ValueError('Order not found at this venue.')
        return dict(r)
    def event(c,oid,kind,u,payload,key=None):
        c.execute('INSERT INTO alport_purchase_events(order_id,event_type,actor_id,created_at,payload,request_key) VALUES(%s,%s,%s,%s,%s::jsonb,%s)',(oid,kind,u['id'],env['now'](),json.dumps(payload),key))
    def integer(value,label):
        try:
            n=int(str(value))
            if n<1:raise ValueError()
            return n
        except (ValueError,TypeError):raise ValueError('Choose a valid '+label+'.') from None
    def textfield(d,key,maximum=1000):return str(d.get(key) or '').strip()[:maximum]
    def datefield(value):
        try:return date.fromisoformat(str(value)).isoformat()
        except ValueError:raise ValueError('Choose an expected delivery date.') from None
    def product(c,pid,org,sid):
        r=c.execute('''SELECT p.*,s.name AS ingredient_name,s.unit AS stock_unit FROM alport_supplier_products p
            JOIN stock_items s ON s.id=p.stock_item_id WHERE p.id=%s AND p.organisation_id=%s AND p.site_id=%s
            AND s.organisation_id=%s AND s.site_id=%s AND s.active=1''',(pid,org,sid,org,sid)).fetchone()
        if not r:raise ValueError('Supplier product is unavailable at this venue.')
        return dict(r)
    def create(c,u,org,sid,d,source=None):
        supplier=integer(d.get('supplier_id'),'supplier')
        su=c.execute('SELECT name FROM suppliers WHERE id=%s AND organisation_id=%s AND active=1',(supplier,org)).fetchone()
        if not su:raise ValueError('Supplier unavailable.')
        items=d.get('lines')
        if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('Add between 1 and 100 product lines.')
        lines=[];seen=set()
        for item in items:
            if not isinstance(item,dict):raise ValueError('Invalid product line.')
            p=product(c,integer(item.get('product_id'),'product'),org,sid)
            if p['supplier_id']!=supplier or p['id'] in seen:raise ValueError('Use each product once, from the selected supplier.')
            seen.add(p['id']);packs=number(item.get('packs'),'Packs',True)
            if packs>100000 or packs!=packs.to_integral_value():raise ValueError('Order whole packs, up to 100,000.')
            units=converted(p['pack_quantity'],p['pack_unit'],p['stock_unit'])
            if units<=0 or units*packs>Decimal('1000000000'):raise ValueError('Order quantity is outside the supported range.')
            lines.append((p,packs,units))
        expected=datefield(d['expected_date']) if d.get('expected_date') else ''
        oid=c.execute('''INSERT INTO alport_purchase_orders(organisation_id,site_id,supplier_id,supplier_name,expected_date,note,source_draft_id,created_by,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(org,sid,supplier,su['name'],expected,textfield(d,'note'),source,u['id'],env['now']())).fetchone()['id']
        for p,packs,units in lines:
            c.execute('''INSERT INTO alport_purchase_lines(order_id,stock_item_id,product_id,ingredient_name,product_name,stock_unit,pack_quantity,pack_unit,units_per_pack,pack_price,packs)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(oid,p['stock_item_id'],p['id'],p['ingredient_name'],p['product_name'],p['stock_unit'],float(p['pack_quantity']),p['pack_unit'],float(units),float(p['pack_price']),float(packs)))
        event(c,oid,'Created',u,{'source_draft_id':source});return oid
    @bp.before_request
    def protect():
        if not env['user']():return jsonify(error='Sign in first.'),401
        if request.method=='POST':
            if request.content_length and request.content_length>30000:return jsonify(error='Request too large.'),413
            if env['user']()['role'] not in ('Owner','Admin','Finance','General Manager','Manager'):return jsonify(error='Manager access required.'),403
            if not session.get('demand_csrf') or not hmac.compare_digest(session['demand_csrf'],request.headers.get('X-Demand-CSRF','')):return jsonify(error='Reload the page and try again.'),403
            if not isinstance(request.get_json(silent=True),dict):return jsonify(error='Send valid input fields.'),400
    @bp.errorhandler(ValueError)
    def invalid(e):return jsonify(error=str(e)),400
    @bp.get('/purchasing')
    def page():
        context();session.setdefault('demand_csrf',secrets.token_urlsafe(32))
        return render_template('purchasing.html',site=env['current_site'](),csrf=session['demand_csrf'])
    @bp.get('/api/purchasing')
    def catalogue():
        u,org,sid=context()
        return jsonify(orders=q('SELECT * FROM alport_purchase_orders WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 200',(org,sid)),
            products=q('''SELECT p.*,s.name AS ingredient_name,s.unit AS stock_unit FROM alport_supplier_products p JOIN stock_items s ON s.id=p.stock_item_id JOIN suppliers su ON su.id=p.supplier_id
                WHERE p.organisation_id=? AND p.site_id=? AND s.active=1 AND su.active=1 ORDER BY p.product_name''',(org,sid)),
            suppliers=q('SELECT id,name FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name',(org,)))
    @bp.post('/api/purchasing')
    def new():
        u,org,sid=context()
        with conn() as c:
            lock(c,sid);oid=create(c,u,org,sid,request.get_json())
        return jsonify(id=oid)
    @bp.post('/api/purchasing/from-draft/<int:did>')
    def from_draft(did):
        u,org,sid=context()
        with conn() as c:
            lock(c,sid)
            draft=c.execute('SELECT * FROM alport_order_drafts WHERE id=%s AND organisation_id=%s AND site_id=%s',(did,org,sid)).fetchone()
            if not draft:raise ValueError('Suggestion not found.')
            existing=allrows(c,'SELECT id FROM alport_purchase_orders WHERE source_draft_id=%s AND organisation_id=%s AND site_id=%s',(did,org,sid))
            if existing:return jsonify(ids=[x['id'] for x in existing])
            payload=draft['payload'];payload=json.loads(payload) if isinstance(payload,str) else payload
            groups={}
            for line in payload['lines']:
                if not line.get('packs'):continue
                pid=line.get('product_id')
                if not pid:raise ValueError('This suggestion predates purchase orders. Calculate and save a fresh suggestion.')
                group=groups.setdefault(line['supplier_id'],{'supplier_id':line['supplier_id'],'lines':[],'expected_date':line.get('arrival') or '', 'note':f'From suggestion #{did}. Review quantities, current pack sizes and prices before approval.'})
                group['lines'].append({'product_id':pid,'packs':line['packs']})
            if not groups:raise ValueError('This suggestion has no packs to order.')
            ids=[create(c,u,org,sid,d,did) for d in groups.values()]
        return jsonify(ids=ids)
    @bp.get('/api/purchasing/<int:oid>')
    def detail(oid):
        u,org,sid=context()
        with conn() as c:
            r=load(c,oid,org,sid);r['lines']=allrows(c,'SELECT * FROM alport_purchase_lines WHERE order_id=%s ORDER BY id',(oid,));r['events']=allrows(c,'SELECT * FROM alport_purchase_events WHERE order_id=%s ORDER BY id',(oid,))
        for e in r['events']:
            if isinstance(e['payload'],str):e['payload']=json.loads(e['payload'])
        return jsonify(r)
    @bp.post('/api/purchasing/<int:oid>/<action>')
    def change(oid,action):
        u,org,sid=context();d=request.get_json()
        with conn() as c:
            lock(c,sid);r=load(c,oid,org,sid)
            key=textfield(d,'request_key',100)
            if action=='receive':
                if not key or len(key)<16:raise ValueError('Missing delivery retry key. Reload the order.')
                previous=c.execute('SELECT payload FROM alport_purchase_events WHERE order_id=%s AND request_key=%s',(oid,key)).fetchone()
                if previous:
                    saved=previous['payload'];saved=json.loads(saved) if isinstance(saved,str) else saved
                    if saved.get('request')!=d:raise ValueError('This delivery key was used for different quantities. Reopen the order.')
                    return jsonify(ok=True,replayed=True)
            if integer(d.get('version'),'order version')!=r['version']:raise ValueError('Order changed. Reopen it before continuing.')
            status=r['status'];payload={'note':textfield(d,'note')}
            if action=='approve':
                if status!='Draft':raise ValueError('Only draft orders can be approved.')
                status='Approved'
            elif action=='ordered':
                if status!='Approved':raise ValueError('Approve the order first.')
                expected=datefield(d.get('expected_date'));reference=textfield(d,'reference',200)
                if not reference:raise ValueError('Record how the supplier order was placed or its confirmation reference.')
                c.execute('UPDATE alport_purchase_orders SET expected_date=%s,reference=%s WHERE id=%s',(expected,reference,oid));status='Ordered';payload.update(expected_date=expected,reference=reference)
            elif action=='reschedule':
                if status not in ('Ordered','Part received'):raise ValueError('Only outstanding orders can be rescheduled.')
                expected=datefield(d.get('expected_date'))
                if not payload['note']:raise ValueError('Give a reason for the revised delivery date.')
                c.execute('UPDATE alport_purchase_orders SET expected_date=%s WHERE id=%s',(expected,oid));payload['expected_date']=expected
            elif action=='close':
                if status not in ('Draft','Approved','Ordered','Part received'):raise ValueError('This order is already finished.')
                if not payload['note']:raise ValueError('Record why the order or outstanding balance is being closed. Confirm cancellation with the supplier separately.')
                status='Closed' if status=='Part received' else 'Cancelled'
            elif action=='receive':
                if status not in ('Ordered','Part received'):raise ValueError('Only placed orders can receive deliveries.')
                if c.execute("SELECT id FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Open'",(org,sid)).fetchone():raise ValueError('Complete the open stocktake before receiving goods.')
                lines={x['id']:x for x in allrows(c,'SELECT * FROM alport_purchase_lines WHERE order_id=%s ORDER BY stock_item_id,id',(oid,))}
                received=d.get('lines')
                if not isinstance(received,list) or not 1<=len(received)<=100:raise ValueError('Enter received pack quantities.')
                seen=set();changes=[]
                for item in received:
                    if not isinstance(item,dict):raise ValueError('Invalid receipt line.')
                    lid=integer(item.get('line_id'),'order line')
                    if lid not in lines or lid in seen:raise ValueError('Invalid or duplicate order line.')
                    seen.add(lid);line=lines[lid];packs=number(item.get('packs'),'Received packs')
                    if packs!=packs.quantize(Decimal('.000001')):raise ValueError('Use at most six decimal places for packs.')
                    if packs==0:continue
                    if packs>Decimal(str(line['packs']))-Decimal(str(line['received_packs'])):raise ValueError('Received packs exceed the outstanding quantity.')
                    stock=c.execute('SELECT * FROM stock_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1 FOR UPDATE',(line['stock_item_id'],org,sid)).fetchone()
                    if not stock or stock['unit']!=line['stock_unit']:raise ValueError('Stock item was archived or its unit changed. Resolve it before receiving.')
                    units=packs*Decimal(str(line['units_per_pack']))
                    if Decimal(str(stock['on_hand']))+units>Decimal('1000000000'):raise ValueError('Resulting stock balance is too large.')
                    c.execute('UPDATE alport_purchase_lines SET received_packs=received_packs+%s WHERE id=%s',(float(packs),lid))
                    c.execute('UPDATE stock_items SET on_hand=on_hand+%s WHERE id=%s',(float(units),line['stock_item_id']))
                    c.execute('''INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s)''',(org,sid,line['stock_item_id'],float(units),'Purchase receipt',f'PO #{oid}; delivery {key}',env['now']()))
                    changes.append({'line_id':lid,'packs':float(packs),'stock_units':float(units)})
                if not changes:raise ValueError('Enter at least one received quantity greater than zero.')
                remaining=c.execute('SELECT COUNT(*) AS n FROM alport_purchase_lines WHERE order_id=%s AND received_packs<packs',(oid,)).fetchone()['n']
                status='Part received' if remaining else 'Received';payload.update(lines=changes,request=d)
            else:raise ValueError('Unknown action.')
            c.execute('UPDATE alport_purchase_orders SET status=%s,version=version+1 WHERE id=%s',(status,oid))
            event(c,oid,action,u,payload,key if action=='receive' else None)
        return jsonify(ok=True,status=status)
    @bp.get('/api/purchasing/<int:oid>/csv')
    def export(oid):
        u,org,sid=context()
        with conn() as c:
            r=load(c,oid,org,sid);lines=allrows(c,'SELECT * FROM alport_purchase_lines WHERE order_id=%s ORDER BY id',(oid,))
        out=io.StringIO();writer=csv.writer(out)
        def safe(v):
            s=str(v)
            return "'"+s if s.lstrip().startswith(('=','+','-','@')) or s.startswith(('\t','\r','\n')) else s
        writer.writerow(['Order','Status','Supplier','Expected delivery','Product','Ingredient','Pack size','Pack unit','Ordered packs','Received packs','Net pack price GBP'])
        for x in lines:writer.writerow([oid,r['status'],safe(r['supplier_name']),r['expected_date'],safe(x['product_name']),safe(x['ingredient_name']),x['pack_quantity'],safe(x['pack_unit']),x['packs'],x['received_packs'],x['pack_price']])
        return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=alport-purchase-order-{oid}.csv'})
    app.register_blueprint(bp)
