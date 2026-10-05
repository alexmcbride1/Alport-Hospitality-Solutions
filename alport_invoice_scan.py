"""Reviewed invoice capture, purchase receipts and discrepancy holds."""
import json,hashlib,hmac,secrets,re
from datetime import date
from decimal import Decimal,ROUND_HALF_UP
from flask import Blueprint,request,session,jsonify,render_template,Response
from alport_inventory import number

SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_invoice_reviews(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 supplier_id BIGINT NOT NULL,invoice_id BIGINT NOT NULL REFERENCES invoices(id),order_id BIGINT,
 invoice_key TEXT NOT NULL,request_key TEXT NOT NULL,payload JSONB NOT NULL,
 created_by BIGINT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(organisation_id,site_id,supplier_id,invoice_key),UNIQUE(organisation_id,site_id,request_key));
CREATE TABLE IF NOT EXISTS alport_invoice_files(
 id BIGSERIAL PRIMARY KEY,review_id BIGINT NOT NULL REFERENCES alport_invoice_reviews(id),
 filename TEXT NOT NULL,mimetype TEXT NOT NULL,data BYTEA NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_invoice_review_events(
 id BIGSERIAL PRIMARY KEY,review_id BIGINT NOT NULL REFERENCES alport_invoice_reviews(id),
 actor_id BIGINT NOT NULL,created_at TEXT NOT NULL,payload JSONB NOT NULL);
'''

def unpack(v):return json.loads(v) if isinstance(v,str) else v

def register_invoice_scan(app,env):
 conn,q=env['conn'],env['q']
 with conn() as c:
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
 bp=Blueprint('invoice_scan',__name__)
 roles=('Owner','Admin','Finance','General Manager','Manager')
 def context():
  u,s=env['user'](),env['current_site']()
  if not u or not s:raise ValueError('Sign in and select a venue.')
  return u,u['organisation_id'],s['id']
 def lock(c,sid):c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(sid),))
 def rows(c,sql,args=()):return [dict(r) for r in c.execute(sql,args).fetchall()]
 def integer(v):
  try:
   n=int(str(v))
   if n<1:raise ValueError()
   return n
  except (ValueError,TypeError):raise ValueError('Choose a valid record.') from None
 def amount(v,label):
  n=number(v,label)
  if n!=n.quantize(Decimal('.01')):raise ValueError(label+' must have at most two decimal places.')
  return n
 def qty(v):
  n=number(v,'Quantity')
  if n!=n.quantize(Decimal('.000001')):raise ValueError('Use at most six decimal places for quantities.')
  return n
 def text(d,key,limit=1000):return str(d.get(key) or '').strip()[:limit]
 def valid_date(v):
  try:return date.fromisoformat(str(v)).isoformat()
  except ValueError:raise ValueError('Enter valid invoice and due dates.') from None
 def review(c,rid,org,sid):
  r=c.execute('SELECT * FROM alport_invoice_reviews WHERE id=%s AND organisation_id=%s AND site_id=%s',(rid,org,sid)).fetchone()
  if not r:raise ValueError('Invoice review not found.')
  r=dict(r);r['payload']=unpack(r['payload']);return r
 def audit(c,rid,u,data):c.execute('INSERT INTO alport_invoice_review_events(review_id,actor_id,created_at,payload) VALUES(%s,%s,%s,%s::jsonb)',(rid,u['id'],env['now'](),json.dumps(data)))
 @bp.before_request
 def protect():
  if not env['user']():return jsonify(error='Sign in first.'),401
  if env['user']()['role'] not in roles:return jsonify(error='Manager or finance access required.'),403
  if request.method=='POST':
   if request.content_length and request.content_length>22*1024*1024:return jsonify(error='Upload is too large; maximum 20 MB of documents.'),413
   token=session.get('invoice_scan_csrf','')
   if not token or not hmac.compare_digest(token,request.headers.get('X-Invoice-CSRF','')):return jsonify(error='Reload the page and try again.'),403
 @bp.errorhandler(ValueError)
 def invalid(e):return jsonify(error=str(e)),400
 @bp.get('/invoice-scan')
 def page():
  context();session.setdefault('invoice_scan_csrf',secrets.token_urlsafe(32));session.setdefault('demand_csrf',secrets.token_urlsafe(32))
  return render_template('invoice_scan.html',site=env['current_site'](),csrf=session['invoice_scan_csrf'])
 @bp.get('/api/invoice-scan')
 def catalogue():
  u,org,sid=context()
  return jsonify(suppliers=q('SELECT id,name FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name',(org,)),
   orders=q("SELECT id,supplier_id,supplier_name,status FROM alport_purchase_orders WHERE organisation_id=? AND site_id=? AND status IN ('Ordered','Part received','Received','Closed') ORDER BY id DESC LIMIT 200",(org,sid)),
   reviews=q('''SELECT r.id,r.invoice_id,r.created_at,i.supplier,i.invoice_number,i.status,i.gross FROM alport_invoice_reviews r JOIN invoices i ON i.id=r.invoice_id
    WHERE r.organisation_id=? AND r.site_id=? ORDER BY r.id DESC LIMIT 100''',(org,sid)))
 @bp.get('/api/invoice-scan/<int:rid>')
 def detail(rid):
  u,org,sid=context()
  with conn() as c:
   r=review(c,rid,org,sid);r['invoice']=dict(c.execute('SELECT * FROM invoices WHERE id=%s',(r['invoice_id'],)).fetchone())
   r['files']=rows(c,'SELECT id,filename FROM alport_invoice_files WHERE review_id=%s',(rid,));r['events']=rows(c,'SELECT actor_id,created_at,payload FROM alport_invoice_review_events WHERE review_id=%s ORDER BY id',(rid,))
  for e in r['events']:e['payload']=unpack(e['payload'])
  return jsonify(r)
 @bp.get('/api/invoice-scan/files/<int:fid>')
 def document(fid):
  u,org,sid=context()
  r=q('''SELECT f.* FROM alport_invoice_files f JOIN alport_invoice_reviews r ON r.id=f.review_id
   WHERE f.id=? AND r.organisation_id=? AND r.site_id=?''',(fid,org,sid),True)
  if not r:return jsonify(error='Document not found.'),404
  ext={'image/jpeg':'jpg','image/png':'png','image/webp':'webp','application/pdf':'pdf'}[r['mimetype']]
  return Response(bytes(r['data']),mimetype=r['mimetype'],headers={'Content-Disposition':f'attachment; filename=invoice-document-{fid}.{ext}','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
 @bp.post('/api/invoice-scan/save')
 def save():
  u,org,sid=context()
  try:d=json.loads(request.form.get('payload',''))
  except (ValueError,TypeError):raise ValueError('Invalid invoice review data.') from None
  if not isinstance(d,dict) or d.get('confirmed') is not True:raise ValueError('Confirm that you reviewed every invoice line, quantity and total.')
  key=text(d,'request_key',100)
  if len(key)<16:raise ValueError('Missing retry key. Reload the page.')
  supplier=integer(d.get('supplier_id'));invoice_number=text(d,'invoice_number',100)
  if not invoice_number:raise ValueError('Enter the invoice number.')
  invoice_key=re.sub(r'\s+','',invoice_number).casefold()
  invdate=valid_date(d.get('invoice_date'));due=valid_date(d.get('due_date'))
  net=amount(d.get('net'),'Net');vat=amount(d.get('vat'),'VAT');gross=amount(d.get('gross'),'Gross')
  if net<=0 or net+vat!=gross:raise ValueError('Net must be positive and net plus VAT must equal gross.')
  lines=d.get('lines')
  if not isinstance(lines,list) or not 1<=len(lines)<=150:raise ValueError('Review between 1 and 150 invoice lines.')
  files=request.files.getlist('documents');attachments=[];total=0
  if not 1<=len(files)<=10:raise ValueError('Attach 1–10 invoice images/PDFs.')
  for f in files:
   data=f.read(20*1024*1024+1);total+=len(data)
   if total>20*1024*1024:raise ValueError('Maximum total document size is 20 MB.')
   mime='image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'application/pdf' if data.startswith(b'%PDF-') else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None
   if not mime:raise ValueError('Use JPEG, PNG, WebP images or a PDF.')
   attachments.append((str(f.filename or 'invoice')[:200],mime,data,hashlib.sha256(data).hexdigest()))
  with conn() as c:
   lock(c,sid)
   old=c.execute('SELECT id,invoice_id,payload FROM alport_invoice_reviews WHERE organisation_id=%s AND site_id=%s AND request_key=%s',(org,sid,key)).fetchone()
   if old:
    if unpack(old['payload']).get('original_request')!=d:raise ValueError('This retry key was used with different details. Refresh the list before creating another invoice.')
    stored=rows(c,'SELECT sha256 FROM alport_invoice_files WHERE review_id=%s ORDER BY id',(old['id'],))
    if [x['sha256'] for x in stored]!=[f[3] for f in attachments]:raise ValueError('Retry documents differ from the saved invoice. Open the existing review.')
    return jsonify(id=old['id'],invoice_id=old['invoice_id'],replayed=True)
   su=c.execute('SELECT name FROM suppliers WHERE id=%s AND organisation_id=%s AND active=1',(supplier,org)).fetchone()
   if not su:raise ValueError('Supplier not found.')
   # Check legacy invoices too; supplier spelling may differ, so manual review is still needed.
   existing=rows(c,'SELECT invoice_number FROM invoices WHERE organisation_id=%s AND site_id=%s AND LOWER(TRIM(supplier))=LOWER(TRIM(%s))',(org,sid,su['name']))
   if any(re.sub(r'\s+','',x['invoice_number']).casefold()==invoice_key for x in existing):raise ValueError('This supplier invoice number is already recorded. Open the existing invoice instead.')
   if c.execute('SELECT id FROM alport_invoice_reviews WHERE organisation_id=%s AND site_id=%s AND supplier_id=%s AND invoice_key=%s',(org,sid,supplier,invoice_key)).fetchone():raise ValueError('Duplicate supplier invoice.')
   oid=integer(d['order_id']) if d.get('order_id') else None;order=None;orderlines={}
   if oid:
    order=c.execute('SELECT * FROM alport_purchase_orders WHERE id=%s AND organisation_id=%s AND site_id=%s AND supplier_id=%s',(oid,org,sid,supplier)).fetchone()
    if not order:raise ValueError('Purchase order must belong to this venue and supplier.')
    orderlines={x['id']:x for x in rows(c,'SELECT * FROM alport_purchase_lines WHERE order_id=%s',(oid,))}
   mode=d.get('receipt_mode')
   if mode not in ('existing','new','none'):raise ValueError('Choose how delivery is recorded.')
   if mode!='none' and not oid:raise ValueError('Choose a purchase order for delivery reconciliation, or select invoice only.')
   if mode=='new':
    if order['status'] not in ('Ordered','Part received'):raise ValueError('Only outstanding placed orders can receive goods.')
    if c.execute("SELECT id FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Open'",(org,sid)).fetchone():raise ValueError('Complete the open stocktake before receiving goods.')
   used={}
   if oid:
    for x in rows(c,'SELECT payload FROM alport_invoice_reviews WHERE organisation_id=%s AND site_id=%s AND order_id=%s',(org,sid,oid)):
     previous=unpack(x['payload'])
     for line in previous['lines']:
      if previous.get('receipt_mode') in ('existing','new') and line.get('order_line_id'):used[line['order_line_id']]=used.get(line['order_line_id'],Decimal(0))+Decimal(str(line['received']))
   saved=[];seen=set();sum_net=Decimal(0);receipts=[]
   for index,line in enumerate(lines):
    if not isinstance(line,dict):raise ValueError('Invalid invoice line.')
    description=text(line,'description',300)
    if not description:raise ValueError('Every line needs a description.')
    billed=qty(line.get('quantity'));received=qty(line.get('received'));price=number(line.get('unit_price'),'Net unit price');line_net=amount(line.get('line_net'),'Line net')
    if price!=price.quantize(Decimal('.0001')):raise ValueError('Unit price supports four decimal places.')
    if (billed*price).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=line_net:raise ValueError('Line quantity × net unit price must equal line net. Review discounts and pack units.')
    sum_net+=line_net;lid=integer(line['order_line_id']) if line.get('order_line_id') else None;ol=orderlines.get(lid)
    if lid and (not ol or lid in seen):raise ValueError('Map each purchase-order line once, to this order only.')
    if lid:seen.add(lid)
    issues=[];kind=text(line,'discrepancy',60);note=text(line,'note')
    if received!=billed:issues.append('Invoiced and accepted quantities differ')
    if ol:
     if line.get('pack_confirmed') is not True:raise ValueError('Confirm that each matched invoice quantity is in the purchase order pack unit.')
     if price!=Decimal(str(ol['pack_price'])):issues.append('Invoice pack price differs from purchase order')
     if billed>Decimal(str(ol['packs'])):issues.append('Invoice quantity exceeds ordered packs')
     if mode=='existing' and received>Decimal(str(ol['received_packs']))-used.get(lid,Decimal(0)):raise ValueError('Accepted quantity exceeds recorded, unallocated received packs. Check prior invoices and delivery records.')
     if mode=='new' and received:
      if received>Decimal(str(ol['packs']))-Decimal(str(ol['received_packs'])):raise ValueError('Receipt exceeds remaining ordered packs.')
      stock=c.execute('SELECT * FROM stock_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1 FOR UPDATE',(ol['stock_item_id'],org,sid)).fetchone()
      if not stock or stock['unit']!=ol['stock_unit']:raise ValueError('Stock item is archived or its unit changed.')
      units=received*Decimal(str(ol['units_per_pack']))
      if Decimal(str(stock['on_hand']))+units>Decimal('1000000000'):raise ValueError('Resulting stock quantity is too large.')
      c.execute('UPDATE stock_items SET on_hand=on_hand+%s WHERE id=%s',(float(units),ol['stock_item_id']))
      c.execute('UPDATE alport_purchase_lines SET received_packs=received_packs+%s WHERE id=%s',(float(received),lid))
      c.execute('INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(org,sid,ol['stock_item_id'],float(units),'Purchase receipt',f'Invoice {invoice_number}; PO #{oid}',env['now']()))
      receipts.append({'line_id':lid,'packs':float(received),'stock_units':float(units)})
    elif mode=='new' and received and not line.get('non_stock'):raise ValueError('Match received stock to a purchase-order line, or explicitly mark the line as non-stock.')
    if oid and not lid and not line.get('non_stock'):issues.append('Stock line is not matched to the purchase order')
    if kind:issues.append(kind)
    if issues and not note:raise ValueError('Add a discrepancy note for '+description+': '+ '; '.join(issues))
    saved.append(dict(description=description,quantity=float(billed),received=float(received),unit_price=float(price),line_net=float(line_net),order_line_id=lid,non_stock=bool(line.get('non_stock')),issues=issues,note=note,resolved=not bool(issues)))
   if sum_net!=net:raise ValueError('Reviewed line net amounts do not equal invoice net. Check missing lines, discounts or charges.')
   if mode=='new' and receipts:
    remaining=c.execute('SELECT COUNT(*) AS n FROM alport_purchase_lines WHERE order_id=%s AND received_packs<packs',(oid,)).fetchone()['n']
    c.execute('UPDATE alport_purchase_orders SET status=%s,version=version+1 WHERE id=%s',('Part received' if remaining else 'Received',oid))
    c.execute('INSERT INTO alport_purchase_events(order_id,event_type,actor_id,created_at,payload,request_key) VALUES(%s,%s,%s,%s,%s::jsonb,%s)',(oid,'receive',u['id'],env['now'](),json.dumps({'lines':receipts,'note':'Invoice capture '+invoice_number}),'invoice-'+key))
   status='Review required' if any(not x['resolved'] for x in saved) else 'Awaiting approval'
   iid=c.execute('''INSERT INTO invoices(organisation_id,site_id,supplier,invoice_number,invoice_date,due_date,category,net,vat,gross,status,notes,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(org,sid,su['name'],invoice_number,invdate,due,text(d,'category',80) or 'Other',float(net),float(vat),float(gross),status,'Reviewed invoice scan. '+text(d,'notes'),env['now']())).fetchone()['id']
   payload=dict(lines=saved,receipt_mode=mode,original_request=d,ocr_text=text(d,'ocr_text',100000),original_totals={'net':float(net),'vat':float(vat),'gross':float(gross)})
   rid=c.execute('''INSERT INTO alport_invoice_reviews(organisation_id,site_id,supplier_id,invoice_id,order_id,invoice_key,request_key,payload,created_by,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id''',(org,sid,supplier,iid,oid,invoice_key,key,json.dumps(payload),u['id'],env['now']())).fetchone()['id']
   for f in attachments:c.execute('INSERT INTO alport_invoice_files(review_id,filename,mimetype,data,sha256) VALUES(%s,%s,%s,%s,%s)',(rid,*f))
   audit(c,rid,u,{'action':'Captured and reviewed','status':status,'receipt_mode':mode,'receipt_lines':receipts})
  return jsonify(id=rid,invoice_id=iid,status=status)
 @bp.post('/api/invoice-scan/<int:rid>/resolve')
 def resolve(rid):
  u,org,sid=context();d=request.get_json(silent=True)
  if not isinstance(d,dict):raise ValueError('Send valid resolution fields.')
  note=text(d,'note');action=d.get('action')
  if not note or action not in ('accepted_charge','replacement_recorded','corrected_invoice'):raise ValueError('Choose a resolution and record the supplier agreement/reference.')
  with conn() as c:
   lock(c,sid);r=review(c,rid,org,sid)
   if integer(d.get('version'))!=r['version']:raise ValueError('Review changed. Reopen it first.')
   inv=c.execute('SELECT * FROM invoices WHERE id=%s FOR UPDATE',(r['invoice_id'],)).fetchone()
   if inv['status']!='Review required':raise ValueError('Only invoices held for review can be resolved.')
   indexes=d.get('lines')
   if not isinstance(indexes,list) or not indexes or any(type(i)!=int or i<0 or i>=len(r['payload']['lines']) for i in indexes):raise ValueError('Select valid discrepancy lines.')
   for i in indexes:
    line=r['payload']['lines'][i]
    if line['resolved']:raise ValueError('A selected discrepancy is already resolved.')
    if action=='replacement_recorded' and line.get('order_line_id'):
     lid=line['order_line_id']
     ol=c.execute('SELECT received_packs FROM alport_purchase_lines WHERE id=%s AND order_id=%s',(lid,r['order_id'])).fetchone()
     allocated=Decimal(0)
     for other in rows(c,'SELECT id,payload FROM alport_invoice_reviews WHERE organisation_id=%s AND site_id=%s AND order_id=%s',(org,sid,r['order_id'])):
      data=r['payload'] if other['id']==rid else unpack(other['payload'])
      if data.get('receipt_mode') in ('existing','new'):
       for part in data['lines']:
        if part.get('order_line_id')==lid:allocated+=Decimal(str(part['received']))
     additional=max(Decimal(0),Decimal(str(line['quantity']))-Decimal(str(line['received'])))
     if not ol or Decimal(str(ol['received_packs']))-allocated<additional:raise ValueError('Record the replacement delivery under Purchase orders before resolving this shortage.')
     line['received']=max(line['received'],line['quantity'])
    line.update(resolved=True,resolution=action,resolution_note=note)
   change={'action':action,'lines':indexes,'note':note}
   if action=='corrected_invoice':
    net=amount(d.get('net'),'Corrected net');vat=amount(d.get('vat'),'Corrected VAT')
    if net<=0:raise ValueError('Corrected net must be positive. Full cancellation/credit needs separate finance handling.')
    c.execute('UPDATE invoices SET net=%s,vat=%s,gross=%s WHERE id=%s',(float(net),float(vat),float(net+vat),r['invoice_id']))
    change.update(old_net=float(inv['net']),old_vat=float(inv['vat']),net=float(net),vat=float(vat))
   if all(x['resolved'] for x in r['payload']['lines']):c.execute("UPDATE invoices SET status='Awaiting approval' WHERE id=%s",(r['invoice_id'],))
   c.execute('UPDATE alport_invoice_reviews SET payload=%s::jsonb,version=version+1 WHERE id=%s',(json.dumps(r['payload']),rid));audit(c,rid,u,change)
  return jsonify(ok=True)
 app.register_blueprint(bp)
