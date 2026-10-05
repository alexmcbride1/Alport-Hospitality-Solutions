"""Audited receipt corrections, supplier returns, substitutions and credit tracking."""
import json,hashlib,secrets,hmac
from decimal import Decimal
from datetime import date
from flask import Blueprint,request,session,jsonify,render_template,Response
from alport_inventory import converted
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_delivery_cases(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 order_id BIGINT NOT NULL REFERENCES alport_purchase_orders(id),line_id BIGINT NOT NULL REFERENCES alport_purchase_lines(id),
 kind TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Pending',payload TEXT NOT NULL,
 request_key TEXT NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,approved_by BIGINT,approved_at TEXT,
 version INTEGER NOT NULL DEFAULT 1,UNIQUE(organisation_id,site_id,request_key));
CREATE TABLE IF NOT EXISTS alport_supplier_credits(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,supplier_id BIGINT NOT NULL,
 case_id BIGINT,invoice_id BIGINT,reference TEXT NOT NULL,credit_date TEXT NOT NULL,
 net NUMERIC(16,2) NOT NULL,vat NUMERIC(16,2) NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL,
 created_by BIGINT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(organisation_id,site_id,supplier_id,reference));
CREATE TABLE IF NOT EXISTS alport_delivery_evidence(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 case_id BIGINT,credit_id BIGINT,filename TEXT NOT NULL,mimetype TEXT NOT NULL,data BYTEA NOT NULL,
 sha256 TEXT NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_delivery_case_events(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 case_id BIGINT,credit_id BIGINT,event TEXT NOT NULL,payload TEXT NOT NULL,actor_id BIGINT NOT NULL,created_at TEXT NOT NULL);
'''
def dec(value,label,signed=False):
 try:n=Decimal(str(value))
 except Exception:raise ValueError('Enter a valid '+label+'.') from None
 if not n.is_finite() or abs(n)>Decimal('1000000000') or (not signed and n<0):raise ValueError('Invalid '+label+'.')
 if n!=n.quantize(Decimal('.000001')):raise ValueError(label+' supports six decimal places.')
 return n
def register_returns(app,env):
 conn,q=env['conn'],env['q']
 with conn() as c:
  c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000002,))
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 bp=Blueprint('alport_returns',__name__)
 def scope():
  u,s=env['user'](),env['current_site']()
  if not u or not s:raise ValueError('Sign in and select a venue.')
  return u,u['organisation_id'],s['id']
 def event(c,u,org,sid,kind,payload,case=None,credit=None):c.execute('INSERT INTO alport_delivery_case_events(organisation_id,site_id,case_id,credit_id,event,payload,actor_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(org,sid,case,credit,kind,json.dumps(payload),u['id'],env['now']()))
 def lock(c,sid):c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(sid),))
 def line(c,lid,org,sid):
  r=c.execute('''SELECT l.*,p.supplier_id,p.status AS order_status,p.version AS order_version FROM alport_purchase_lines l JOIN alport_purchase_orders p ON p.id=l.order_id
   WHERE l.id=%s AND p.organisation_id=%s AND p.site_id=%s''',(lid,org,sid)).fetchone()
  if not r:raise ValueError('Purchase-order line not found at this venue.')
  return dict(r)
 def stock(c,iid,org,sid,unit):
  s=c.execute('SELECT * FROM stock_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1 FOR UPDATE',(iid,org,sid)).fetchone()
  if not s or s['unit']!=unit:raise ValueError('Stock item is unavailable or its unit changed.')
  return s
 def movement(c,org,sid,iid,qty,note):
  c.execute('UPDATE stock_items SET on_hand=on_hand+%s WHERE id=%s',(float(qty),iid))
  c.execute('INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(org,sid,iid,float(qty),'Delivery adjustment',note,env['now']()))
 def fingerprint(r):return hashlib.sha256(json.dumps(r,sort_keys=True,default=str).encode()).hexdigest()
 @bp.before_request
 def protect():
  u=env['user']()
  if not u:return jsonify(error='Sign in first.'),401
  if u['role'] not in ('Owner','Admin','Finance','General Manager','Manager'):return jsonify(error='Manager access required.'),403
  if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):return jsonify(error='Business subscription needs attention.'),403
  if request.content_length and request.content_length>12*1024*1024:return jsonify(error='Maximum request size is 12 MB.'),413
  if request.method=='POST' and (not session.get('returns_csrf') or not hmac.compare_digest(session['returns_csrf'],request.headers.get('X-Returns-CSRF',''))):return jsonify(error='Reload the returns page.'),403
 @bp.errorhandler(ValueError)
 def invalid(e):return jsonify(error=str(e)),400
 @bp.get('/returns')
 def page():
  scope();session.setdefault('returns_csrf',secrets.token_urlsafe(32));return render_template('returns.html',csrf=session['returns_csrf'],site=env['current_site']())
 @bp.get('/api/returns')
 def listing():
  u,org,sid=scope()
  return jsonify(lines=q('''SELECT l.*,p.supplier_id,p.supplier_name,p.status AS order_status FROM alport_purchase_lines l JOIN alport_purchase_orders p ON p.id=l.order_id
   WHERE p.organisation_id=? AND p.site_id=? ORDER BY p.id DESC,l.id LIMIT 1000''',(org,sid)),
   products=q('''SELECT p.*,s.name AS ingredient_name FROM alport_supplier_products p JOIN stock_items s ON s.id=p.stock_item_id WHERE p.organisation_id=? AND p.site_id=? AND s.active=1''',(org,sid)),
   suppliers=q('SELECT id,name FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name',(org,)),
   invoices=q('SELECT id,supplier,invoice_number,gross,status FROM invoices WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 200',(org,sid)),
   cases=q('SELECT * FROM alport_delivery_cases WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 200',(org,sid)),
   credits=q('SELECT * FROM alport_supplier_credits WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 200',(org,sid)),
   evidence=q('SELECT id,case_id,credit_id,filename FROM alport_delivery_evidence WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 500',(org,sid)))
 @bp.post('/api/returns/cases')
 def create():
  u,org,sid=scope();d=request.get_json(silent=True) or {};kind=d.get('kind');note=str(d.get('note') or '').strip();key=str(d.get('request_key') or '')
  if kind not in ('Return','Receipt correction','Substitution','Substitute return') or not note or len(note)>1000 or not 16<=len(key)<=100:raise ValueError('Choose a case type, give a reason and reload if the retry key is missing.')
  packs=dec(d.get('packs'),'pack quantity',kind=='Receipt correction')
  if packs==0:raise ValueError('Pack quantity cannot be zero.')
  if kind!='Receipt correction' and packs<0:raise ValueError('Use a positive quantity.')
  with conn() as c:
   lock(c,sid)
   old=c.execute('SELECT id,payload FROM alport_delivery_cases WHERE organisation_id=%s AND site_id=%s AND request_key=%s',(org,sid,key)).fetchone()
   if old:
    if json.loads(old['payload'])['request']!=d:raise ValueError('That retry key belongs to different details.')
    return jsonify(id=old['id'],replayed=True)
   r=line(c,d.get('line_id'),org,sid)
   if r['order_status'] in ('Draft','Approved','Cancelled'):raise ValueError('Use an order that was placed with the supplier.')
   actual=None
   if kind=='Substitution':
    actual=c.execute('''SELECT p.*,s.unit AS stock_unit FROM alport_supplier_products p JOIN stock_items s ON s.id=p.stock_item_id
      WHERE p.id=%s AND p.organisation_id=%s AND p.site_id=%s AND p.supplier_id=%s AND s.active=1''',(d.get('product_id'),org,sid,r['supplier_id'])).fetchone()
    if not actual:raise ValueError('Choose a replacement supplier product from this venue and supplier.')
    if actual['stock_item_id']==r['stock_item_id']:raise ValueError('Use a normal receipt for the same ingredient.')
    if dec(d.get('actual_packs'),'replacement packs')<=0:raise ValueError('Enter the replacement quantity actually accepted.')
   if kind=='Substitute return':
    original=c.execute("SELECT * FROM alport_delivery_cases WHERE id=%s AND organisation_id=%s AND site_id=%s AND line_id=%s AND kind='Substitution' AND status='Approved'",(d.get('substitution_id'),org,sid,r['id'])).fetchone()
    if not original:raise ValueError('Choose an approved substitution case for this order line.')
    original_payload=json.loads(original['payload']);actual=original_payload['actual']
   payload={'request':d,'line':r,'line_hash':fingerprint(r),'actual':dict(actual) if actual else None}
   rid=c.execute('''INSERT INTO alport_delivery_cases(organisation_id,site_id,order_id,line_id,kind,payload,request_key,created_by,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(org,sid,r['order_id'],r['id'],kind,json.dumps(payload,default=str),key,u['id'],env['now']())).fetchone()['id']
   event(c,u,org,sid,'Proposed',{'kind':kind,'note':note},case=rid)
  return jsonify(id=rid)
 @bp.post('/api/returns/cases/<int:rid>/decision')
 def decide(rid):
  u,org,sid=scope();d=request.get_json(silent=True) or {};decision=d.get('decision');note=str(d.get('note') or '').strip()
  if decision not in ('Approved','Declined') or not note or len(note)>1000:raise ValueError('Choose a decision and add a review note.')
  with conn() as c:
   lock(c,sid);r=c.execute('SELECT * FROM alport_delivery_cases WHERE id=%s AND organisation_id=%s AND site_id=%s FOR UPDATE',(rid,org,sid)).fetchone()
   if not r or r['status']!='Pending' or r['version']!=d.get('version'):raise ValueError('Case changed. Refresh first.')
   payload=json.loads(r['payload']);saved=payload['request'];ol=line(c,r['line_id'],org,sid);packs=dec(saved['packs'],'packs',True)
   if decision=='Approved':
    if fingerprint(ol)!=payload['line_hash']:raise ValueError('The purchase order changed. Decline this proposal and create a fresh one.')
    if c.execute("SELECT id FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Open'",(org,sid)).fetchone():raise ValueError('Complete the open stocktake first.')
    st=stock(c,ol['stock_item_id'],org,sid,ol['stock_unit']);units=packs*dec(ol['units_per_pack'],'stock units')
    if r['kind']=='Substitute return':
     original=c.execute("SELECT payload FROM alport_delivery_cases WHERE id=%s AND status='Approved'",(saved['substitution_id'],)).fetchone()
     original_payload=json.loads(original['payload']);actual=original_payload['actual'];st=stock(c,actual['stock_item_id'],org,sid,actual['stock_unit'])
     returned=sum((dec(json.loads(x['payload'])['request']['packs'],'returned replacement packs') for x in c.execute("SELECT payload FROM alport_delivery_cases WHERE line_id=%s AND kind='Substitute return' AND status='Approved'",(ol['id'],)).fetchall() if str(json.loads(x['payload'])['request'].get('substitution_id'))==str(saved['substitution_id'])),Decimal(0))
     units=packs*converted(actual['pack_quantity'],actual['pack_unit'],actual['stock_unit'])
     if packs>dec(original_payload['request']['actual_packs'],'replacement packs')-returned or dec(st['on_hand'],'on hand')<units:raise ValueError('Return exceeds the unreturned replacement quantity or available stock.')
     movement(c,org,sid,actual['stock_item_id'],-units,f'Replacement return case #{rid}: {note}')
    elif r['kind']=='Return':
     substituted=sum((dec(json.loads(x['payload'])['request']['packs'],'substituted packs') for x in c.execute("SELECT payload FROM alport_delivery_cases WHERE line_id=%s AND kind='Substitution' AND status='Approved'",(ol['id'],)).fetchall()),Decimal(0))
     returned=Decimal(0)
     for previous in c.execute("SELECT payload FROM alport_delivery_cases WHERE line_id=%s AND kind='Return' AND status='Approved'",(ol['id'],)).fetchall():returned+=dec(json.loads(previous['payload'])['request']['packs'],'returned packs')
     if packs>dec(ol['received_packs'],'received packs')-returned-substituted:raise ValueError('Return exceeds the quantity received and not already returned.')
     if dec(st['on_hand'],'on hand')<units:raise ValueError('Not enough stock remains to return this quantity.')
     movement(c,org,sid,ol['stock_item_id'],-units,f'Return case #{rid}: {note}')
    elif r['kind']=='Receipt correction':
     if c.execute("SELECT id FROM alport_delivery_cases WHERE line_id=%s AND status='Approved' AND kind IN ('Substitution','Return')",(ol['id'],)).fetchone():raise ValueError('This line has returns/substitutions. Use a separate replacement order rather than rewriting its receipt.')
     received=dec(ol['received_packs'],'received packs')+packs
     if received<0 or received>dec(ol['packs'],'ordered packs') or not 0<=dec(st['on_hand'],'on hand')+units<=Decimal('1000000000'):raise ValueError('Correction exceeds the order or available stock.')
     allocated=Decimal(0)
     for ir in c.execute('SELECT payload FROM alport_invoice_reviews WHERE organisation_id=%s AND site_id=%s',(org,sid)).fetchall():
      data=ir['payload'];data=json.loads(data) if isinstance(data,str) else data
      if data.get('receipt_mode') in ('existing','new'):
       for part in data['lines']:
        if part.get('order_line_id')==ol['id']:allocated+=dec(part['received'],'allocated packs')
     if received<allocated:raise ValueError('These goods were allocated to an invoice. Record a return and credit instead of rewriting that receipt.')
     c.execute('UPDATE alport_purchase_lines SET received_packs=%s WHERE id=%s',(float(received),ol['id']));movement(c,org,sid,ol['stock_item_id'],units,f'Receipt correction #{rid}: {note}')
    else:
     if packs>dec(ol['packs'],'ordered packs')-dec(ol['received_packs'],'received packs'):raise ValueError('Substitution exceeds the undelivered original packs.')
     actual=payload['actual'];a=stock(c,actual['stock_item_id'],org,sid,actual['stock_unit'])
     accepted=dec(saved['actual_packs'],'replacement packs')*converted(actual['pack_quantity'],actual['pack_unit'],actual['stock_unit'])
     if dec(a['on_hand'],'on hand')+accepted>Decimal('1000000000'):raise ValueError('Resulting stock is too large.')
     movement(c,org,sid,actual['stock_item_id'],accepted,f'Substitute accepted, case #{rid}, original PO line {ol["id"]}: {note}')
     c.execute('UPDATE alport_purchase_lines SET received_packs=received_packs+%s WHERE id=%s',(float(packs),ol['id']))
    if r['kind'] not in ('Return','Substitute return'):
     remaining=c.execute('SELECT COUNT(*) AS n FROM alport_purchase_lines WHERE order_id=%s AND received_packs<packs',(ol['order_id'],)).fetchone()['n']
     c.execute('UPDATE alport_purchase_orders SET status=%s,version=version+1 WHERE id=%s',('Part received' if remaining else 'Received',ol['order_id']))
    else:c.execute('UPDATE alport_purchase_orders SET version=version+1 WHERE id=%s',(ol['order_id'],))
    c.execute('INSERT INTO alport_purchase_events(order_id,event_type,actor_id,created_at,payload) VALUES(%s,%s,%s,%s,%s::jsonb)',(ol['order_id'],r['kind'],u['id'],env['now'](),json.dumps({'case_id':rid,'note':note})))
   c.execute('UPDATE alport_delivery_cases SET status=%s,approved_by=%s,approved_at=%s,version=version+1 WHERE id=%s',(decision,u['id'],env['now'](),rid));event(c,u,org,sid,decision,{'note':note},case=rid)
  return jsonify(ok=True)
 @bp.post('/api/returns/credits')
 def credit():
  u,org,sid=scope();d=request.get_json(silent=True) or {};net=dec(d.get('net'),'net credit');vat=dec(d.get('vat'),'VAT credit');ref=str(d.get('reference') or '').strip();note=str(d.get('note') or '').strip()
  if net<=0 or any(x!=x.quantize(Decimal('.01')) for x in (net,vat)) or not ref or len(ref)>100 or not note or len(note)>1000:raise ValueError('Enter a positive net amount, pennies, a supplier reference and note.')
  try:credit_date=date.fromisoformat(str(d.get('credit_date'))).isoformat()
  except ValueError:raise ValueError('Choose the credit date.') from None
  with conn() as c:
   lock(c,sid)
   if not c.execute('SELECT id FROM suppliers WHERE id=%s AND organisation_id=%s',(d.get('supplier_id'),org)).fetchone():raise ValueError('Supplier not found.')
   if d.get('case_id'):
    r=c.execute('''SELECT p.supplier_id FROM alport_delivery_cases r JOIN alport_purchase_orders p ON p.id=r.order_id WHERE r.id=%s AND r.organisation_id=%s AND r.site_id=%s''',(d['case_id'],org,sid)).fetchone()
    if not r or str(r['supplier_id'])!=str(d['supplier_id']):raise ValueError('Case must belong to the same venue and supplier.')
   if d.get('invoice_id'):
    inv=c.execute('SELECT supplier FROM invoices WHERE id=%s AND organisation_id=%s AND site_id=%s',(d['invoice_id'],org,sid)).fetchone();su=c.execute('SELECT name FROM suppliers WHERE id=%s',(d['supplier_id'],)).fetchone()
    if not inv or inv['supplier']!=su['name']:raise ValueError('Invoice must belong to the same venue and supplier.')
   if c.execute('SELECT id FROM alport_supplier_credits WHERE organisation_id=%s AND site_id=%s AND supplier_id=%s AND reference=%s',(org,sid,d['supplier_id'],ref)).fetchone():raise ValueError('That supplier credit reference is already recorded.')
   cid=c.execute('''INSERT INTO alport_supplier_credits(organisation_id,site_id,supplier_id,case_id,invoice_id,reference,credit_date,net,vat,status,note,created_by,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'Requested',%s,%s,%s) RETURNING id''',(org,sid,d['supplier_id'],d.get('case_id') or None,d.get('invoice_id') or None,ref,credit_date,float(net),float(vat),note,u['id'],env['now']())).fetchone()['id'];event(c,u,org,sid,'Credit requested',{'reference':ref},credit=cid)
  return jsonify(id=cid)
 @bp.post('/api/returns/credits/<int:cid>/status')
 def credit_status(cid):
  u,org,sid=scope();d=request.get_json(silent=True) or {};status=d.get('status');note=str(d.get('note') or '').strip()
  with conn() as c:
   lock(c,sid);r=c.execute('SELECT * FROM alport_supplier_credits WHERE id=%s AND organisation_id=%s AND site_id=%s',(cid,org,sid)).fetchone()
   transitions={'Requested':('Received','Cancelled'),'Received':('Reconciled','Cancelled'),'Reconciled':(),'Cancelled':()}
   if not r or r['version']!=d.get('version') or status not in transitions.get(r['status'],()) or not note:raise ValueError('Refresh and choose a valid next status with a reference/note.')
   if status=='Received' and not c.execute('SELECT id FROM alport_delivery_evidence WHERE credit_id=%s',(cid,)).fetchone():raise ValueError('Attach the supplier credit document first.')
   c.execute('UPDATE alport_supplier_credits SET status=%s,note=%s,version=version+1 WHERE id=%s',(status,r['note']+'\n'+note,cid));event(c,u,org,sid,'Credit '+status,{'note':note},credit=cid)
  return jsonify(ok=True,note='Tracking only: reconcile the credit with your accounting/payment records separately.')
 @bp.post('/api/returns/evidence')
 def evidence():
  u,org,sid=scope();case=request.form.get('case_id') or None;credit=request.form.get('credit_id') or None
  if bool(case)==bool(credit):raise ValueError('Attach evidence to one case or one credit.')
  table='alport_delivery_cases' if case else 'alport_supplier_credits';identity=case or credit
  if not q(f'SELECT id FROM {table} WHERE id=? AND organisation_id=? AND site_id=?',(identity,org,sid),True):raise ValueError('Record not found.')
  f=request.files.get('document')
  if not f:raise ValueError('Choose an image or PDF.')
  data=f.read(10*1024*1024+1)
  if len(data)>10*1024*1024:raise ValueError('Use a file under 10 MB.')
  mime='image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'application/pdf' if data.startswith(b'%PDF-') else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None
  if not mime:raise ValueError('Use JPEG, PNG, WebP or PDF.')
  with conn() as c:
   fid=c.execute('''INSERT INTO alport_delivery_evidence(organisation_id,site_id,case_id,credit_id,filename,mimetype,data,sha256,created_by,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(org,sid,case,credit,str(f.filename)[:200],mime,data,hashlib.sha256(data).hexdigest(),u['id'],env['now']())).fetchone()['id']
   event(c,u,org,sid,'Evidence attached',{'file_id':fid},case=case,credit=credit)
  return jsonify(id=fid)
 @bp.get('/api/returns/evidence/<int:fid>')
 def file(fid):
  u,org,sid=scope();r=q('SELECT * FROM alport_delivery_evidence WHERE id=? AND organisation_id=? AND site_id=?',(fid,org,sid),True)
  if not r:return jsonify(error='File not found.'),404
  return Response(bytes(r['data']),mimetype=r['mimetype'],headers={'Content-Disposition':f'attachment; filename=evidence-{fid}', 'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
 app.register_blueprint(bp)
