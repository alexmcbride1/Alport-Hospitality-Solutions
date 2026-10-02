
import hashlib
import hmac
import json
import math
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from flask import Blueprint, jsonify, render_template, request, session, Response
from alport_inventory import converted, number

SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_till_recipe_map(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,connection_id BIGINT NOT NULL,
 item_key TEXT NOT NULL,menu_item_id BIGINT REFERENCES menu_items(id),ignore_reason TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(connection_id,item_key));
CREATE TABLE IF NOT EXISTS alport_till_consumed(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,connection_id BIGINT NOT NULL,external_id TEXT NOT NULL,
 source_hash TEXT NOT NULL,usage JSONB NOT NULL,posted_by BIGINT NOT NULL,posted_at TEXT NOT NULL,
 PRIMARY KEY(connection_id,external_id));
CREATE TABLE IF NOT EXISTS alport_order_drafts(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,
 fingerprint TEXT NOT NULL,payload JSONB NOT NULL,status TEXT NOT NULL DEFAULT 'Draft',
 created_by BIGINT NOT NULL,created_at TEXT NOT NULL,approved_by BIGINT,approved_at TEXT,
 UNIQUE(organisation_id,site_id,fingerprint));
CREATE TABLE IF NOT EXISTS alport_delivery_rules(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
 lead_days INTEGER NOT NULL DEFAULT 1,delivery_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
 PRIMARY KEY(organisation_id,site_id,supplier_id));
'''
def dump(value): return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)
def digest(value): return hashlib.sha256(dump(value).encode()).hexdigest()
def unpack(value): return json.loads(value) if isinstance(value,str) else value

def usage_for_sale(sale,mappings,recipes,stocks):
    if sale.get('warnings'): raise ValueError('Till flagged this sale: '+ '; '.join(map(str,sale['warnings'])))
    usage=defaultdict(float)
    lines=sale.get('lines')
    if not isinstance(lines,list): raise ValueError('Missing sale lines.')
    for line in lines:
        qty=float(number(line.get('quantity'),'Till quantity'))
        if qty==0: continue
        mapping=mappings.get(str(line.get('key','')))
        if not mapping: raise ValueError('Map till item: '+str(line.get('name') or line.get('key')))
        if mapping.get('ignore_reason'): continue
        recipe=recipes.get(mapping.get('menu_item_id'))
        if not recipe: raise ValueError('Mapped recipe is archived, unavailable or empty.')
        for component in recipe:
            stock=stocks.get(component.get('stock_item_id'))
            if not stock: raise ValueError('Every recipe component must link to active stock.')
            units=float(converted(component['quantity'],component['unit'],stock['unit']))
            usage[stock['id']]+=qty*units
    if any(not math.isfinite(v) or v>1e9 for v in usage.values()): raise ValueError('Stock usage is outside the supported range.')
    return dict(usage)

def next_delivery(today,lead,weekdays):
    for offset in range(lead,lead+8):
        candidate=today+timedelta(days=offset)
        if candidate.weekday() in weekdays:return candidate
    raise ValueError('Choose at least one delivery day.')

def estimate(daily,today,horizon,bookings,weather_pct,event_pct):
    """Blend observed trading days; never invent zero-sales days for missing imports."""
    history=[(date.fromisoformat(day),qty) for day,qty in daily.items() if today-timedelta(days=28)<=date.fromisoformat(day)<today]
    if not history:return {'units':0,'observed_days':0,'day':None,'week':None,'month':None,'booking_factor':1}
    def avg(rows):return sum(x[1] for x in rows)/len(rows) if rows else None
    yesterday=next((q for d,q in history if d==today-timedelta(days=1)),None)
    week=avg([x for x in history if x[0]>=today-timedelta(days=7)]);month=avg(history)
    parts=[(yesterday,.2),(week,.3),(month,.5)];valid=[(v,w) for v,w in parts if v is not None]
    baseline=sum(v*w for v,w in valid)/sum(w for v,w in valid)
    total=0
    for i in range(horizon):
        target=today+timedelta(days=i)
        comparable=[x for x in history if x[0].weekday()==target.weekday()]
        # Weekday signal is blended only when at least two comparable observations exist.
        total+=(baseline+avg(comparable))/2 if len(comparable)>=2 else baseline
    historical_covers=sum(bookings.get(d.isoformat(),0) for d,_ in history)/len(history)
    upcoming=sum(bookings.get((today+timedelta(days=i)).isoformat(),0) for i in range(horizon))
    ratio=min(2,max(1,upcoming/(historical_covers*horizon))) if historical_covers else 1
    factor=(1+weather_pct/100)*(1+event_pct/100)
    return {'units':round(total*ratio*factor,6),'observed_days':len(history),'day':yesterday,'week':week,'month':month,'booking_factor':round(ratio,3)}


def register_demand(app,env):
    from alport_purchasing import register_purchasing, outstanding
    from alport_supplier_rules import SCHEMA as RULE_SCHEMA
    with env['conn']() as c:c.execute(RULE_SCHEMA)
    register_purchasing(app,env)
    conn,q=env['conn'],env['q']
    with conn() as c:
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    bp=Blueprint('alport_demand',__name__)
    def context():
        u,s=env['user'](),env['current_site']()
        if not u or not s:raise ValueError('Sign in and select a venue.')
        return u,s
    def lock(c,site_id): c.execute('SELECT pg_advisory_xact_lock(%s)',(710000000000+int(site_id),))
    def rows(c,sql,args=()): return [dict(x) for x in c.execute(sql,args).fetchall()]
    def scope():
        u,s=context();return u['organisation_id'],s['id']
    @bp.before_request
    def protect():
        if not env['user']():return jsonify(error='Sign in first.'),401
        if request.method=='POST':
            if not isinstance(request.get_json(silent=True),dict):return jsonify(error='Send an object with valid input fields.'),400
            if env['user']()['role'] not in ('Owner','Admin','Finance','General Manager','Manager'):return jsonify(error='Manager access required.'),403
            token=session.get('demand_csrf','')
            if not token or not hmac.compare_digest(token,request.headers.get('X-Demand-CSRF','')):return jsonify(error='Reload this page and try again.'),403
            if request.content_length and request.content_length>20000:return jsonify(error='Request too large.'),413
    @bp.errorhandler(ValueError)
    def invalid(e):return jsonify(error=str(e)),400
    @bp.get('/demand')
    def page():
        u,s=context();session.setdefault('demand_csrf',secrets.token_urlsafe(32))
        return render_template('demand.html',site=s,csrf=session['demand_csrf'])
    def dates(d):
        try:start,end=date.fromisoformat(d['start']),date.fromisoformat(d['end'])
        except (ValueError,KeyError,TypeError):raise ValueError('Choose valid start and end dates.') from None
        if end<start or (end-start).days>31:raise ValueError('Select a period of up to 32 days.')
        if end>datetime.now(ZoneInfo('Europe/London')).date():raise ValueError('Stock usage cannot be posted for future dates.')
        return start.isoformat(),end.isoformat()
    def snapshot(c,start,end,locking=False):
        org,sid=scope();scope_args=(org,sid)
        # Row locks protect the reviewed sale and recipe versions until posting commits.
        suffix=' FOR SHARE' if locking else ''
        sales=rows(c,'''SELECT t.connection_id,t.external_id,t.sale_date,t.data FROM alport_till_sales t
             JOIN alport_till_connections tc ON tc.id=t.connection_id
             WHERE tc.organisation_id=%s AND tc.site_id=%s AND t.sale_date BETWEEN %s AND %s
             ORDER BY t.connection_id,t.external_id LIMIT 50001'''+(' FOR SHARE OF t' if locking else ''),scope_args+(start,end))
        if len(sales)>50000:raise ValueError('More than 50,000 sales. Use a smaller date range.')
        stocks={x['id']:x for x in rows(c,'SELECT * FROM stock_items WHERE organisation_id=%s AND site_id=%s AND active=1 ORDER BY id'+(' FOR UPDATE' if locking else ''),scope_args)}
        menus={x['id']:x for x in rows(c,'SELECT * FROM menu_items WHERE organisation_id=%s AND site_id=%s AND active=1 ORDER BY id'+suffix,scope_args)}
        recipes=defaultdict(list)
        for part in rows(c,'SELECT * FROM menu_components WHERE organisation_id=%s AND site_id=%s ORDER BY id'+suffix,scope_args):
            if part['menu_item_id'] in menus:recipes[part['menu_item_id']].append(part)
        mappings={(x['connection_id'],x['item_key']):x for x in rows(c,'SELECT * FROM alport_till_recipe_map WHERE organisation_id=%s AND site_id=%s',scope_args)}
        posted={(x['connection_id'],x['external_id']):x for x in rows(c,'SELECT * FROM alport_till_consumed WHERE organisation_id=%s AND site_id=%s',scope_args)}
        return sales,stocks,menus,recipes,mappings,posted
    def plan(c,start,end,locking=False):
        org,sid=scope();sales,stocks,menus,recipes,maps,posted=snapshot(c,start,end,locking)
        cutoff=c.execute("SELECT MAX(completed_at) AS cutoff FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Complete'",(org,sid)).fetchone()['cutoff']
        ready=[];blocked=[];totals=defaultdict(float);already=0
        for row in sales:
            identity=(row['connection_id'],row['external_id']);sale=unpack(row['data']);source_hash=digest(sale)
            if identity in posted:
                if posted[identity]['source_hash']!=source_hash:blocked.append({'sale':row['external_id'],'reason':'Previously posted sale changed. Review refunds/voids and adjust physical stock manually; no automatic reversal.'})
                else:already+=1
                continue
            try:
                if cutoff:
                    sold=datetime.fromisoformat(sale['at'].replace('Z','+00:00'))
                    counted=datetime.fromisoformat(str(cutoff).replace('Z','+00:00'))
                    if sold.replace(tzinfo=sold.tzinfo or timezone.utc)<=counted.replace(tzinfo=counted.tzinfo or timezone.utc):raise ValueError('Sale predates the latest completed stocktake; already covered by that count.')
                mapping={key:m for (cid,key),m in maps.items() if cid==row['connection_id']}
                use=usage_for_sale(sale,mapping,recipes,stocks)
                ready.append({'connection_id':row['connection_id'],'external_id':row['external_id'],'source_hash':source_hash,'usage':use})
                for iid,qty in use.items():totals[iid]+=qty
            except (ValueError,KeyError,TypeError) as e:blocked.append({'sale':row['external_id'],'reason':str(e)})
        usage=[{'id':iid,'name':stocks[iid]['name'],'unit':stocks[iid]['unit'],'on_hand':float(stocks[iid]['on_hand']),'quantity':round(qty,6),'after':round(float(stocks[iid]['on_hand'])-qty,6)} for iid,qty in sorted(totals.items())]
        return {'start':start,'end':end,'ready':ready,'usage':usage,'blocked':blocked,'already_posted':already,'fingerprint':digest([ready,usage,blocked])}
    @bp.get('/api/demand/catalogue')
    def catalogue():
        org,sid=scope();today=datetime.now(ZoneInfo('Europe/London')).date();start=(today-timedelta(days=31)).isoformat()
        with conn() as c:
            sales,stocks,menus,recipes,maps,posted=snapshot(c,start,today.isoformat())
        found={}
        for row in sales:
            for line in unpack(row['data']).get('lines',[]):
                key=str(line['key']);identity=(row['connection_id'],key)
                found[identity]={'connection_id':identity[0],'item_key':key,'name':line['name'],'mapping':maps.get(identity)}
        return jsonify(items=list(found.values()),menus=list(menus.values()),suppliers=q('SELECT id,name FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name',(org,)),policies=q('SELECT * FROM alport_supplier_rules WHERE organisation_id=? AND site_id=?',(org,sid)),rules=q('SELECT * FROM alport_delivery_rules WHERE organisation_id=? AND site_id=?',(org,sid)),drafts=q('SELECT id,status,created_at FROM alport_order_drafts WHERE organisation_id=? AND site_id=? ORDER BY id DESC LIMIT 20',(org,sid)))
    @bp.post('/api/demand/mapping')
    def mapping():
        d=request.get_json() or {};org,sid=scope()
        try:cid=int(d.get('connection_id'));mid=int(d['menu_item_id']) if d.get('menu_item_id') else None
        except (ValueError,TypeError):raise ValueError('Choose a valid connection and recipe.') from None
        key=str(d.get('item_key') or '')[:500];reason=str(d.get('ignore_reason') or '').strip()[:300]
        if not key or (mid is None)==(not reason):raise ValueError('Select a recipe OR explain why this item has no stock usage.')
        with conn() as c:
            lock(c,sid)
            if not c.execute('SELECT id FROM alport_till_connections WHERE id=%s AND organisation_id=%s AND site_id=%s',(cid,org,sid)).fetchone():raise ValueError('Till is unavailable at this site.')
            if mid and not c.execute('SELECT id FROM menu_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1',(mid,org,sid)).fetchone():raise ValueError('Recipe is unavailable at this site.')
            c.execute('''INSERT INTO alport_till_recipe_map(organisation_id,site_id,connection_id,item_key,menu_item_id,ignore_reason)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(connection_id,item_key) DO UPDATE SET menu_item_id=excluded.menu_item_id,ignore_reason=excluded.ignore_reason''',(org,sid,cid,key,mid,reason))
        return jsonify(ok=True)
    @bp.post('/api/demand/preview')
    def preview():
        start,end=dates(request.get_json() or {})
        with conn() as c:result=plan(c,start,end)
        return jsonify(result)
    @bp.post('/api/demand/post')
    def post():
        d=request.get_json() or {};start,end=dates(d);org,sid=scope();u,_=context()
        if d.get('opening_stock_confirmed') is not True:raise ValueError('Confirm this period is after your opening stock balance.')
        with conn() as c:
            lock(c,sid)
            if c.execute("SELECT id FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Open'",(org,sid)).fetchone():raise ValueError('Complete the open stocktake before posting sales.')
            result=plan(c,start,end,True)
            if not hmac.compare_digest(result['fingerprint'],str(d.get('fingerprint',''))):raise ValueError('Sales, mappings or stock changed. Preview again before posting.')
            if any(x['after']<0 for x in result['usage']):raise ValueError('Insufficient recorded stock. Reconcile deliveries/counts before posting; nothing was deducted.')
            for row in result['ready']:
                c.execute('''INSERT INTO alport_till_consumed(organisation_id,site_id,connection_id,external_id,source_hash,usage,posted_by,posted_at)
                    VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)''',(org,sid,row['connection_id'],row['external_id'],row['source_hash'],dump(row['usage']),u['id'],env['now']()))
            for item in result['usage']:
                c.execute('UPDATE stock_items SET on_hand=on_hand-%s WHERE id=%s',(item['quantity'],item['id']))
                c.execute('''INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)''',(org,sid,item['id'],-item['quantity'],'Till consumption',f'Reviewed sales {start} to {end}',env['now']()))
        return jsonify(ok=True,posted=len(result['ready']),blocked=len(result['blocked']))
    @bp.post('/api/demand/delivery')
    def delivery():
        d=request.get_json() or {};org,sid=scope()
        try:supplier=int(d['supplier_id']);lead=int(d['lead_days']);days=sorted(set(int(x) for x in str(d['delivery_days']).split(',')))
        except (KeyError,ValueError,TypeError):raise ValueError('Enter lead days and delivery weekdays (0=Monday, 6=Sunday).') from None
        if not 0<=lead<=14 or not days or any(x<0 or x>6 for x in days):raise ValueError('Lead time must be 0–14 days; choose valid weekdays.')
        from alport_supplier_rules import validate
        policy=validate(d)
        if not q('SELECT id FROM suppliers WHERE id=? AND organisation_id=? AND active=1',(supplier,org),True):raise ValueError('Supplier unavailable.')
        with conn() as c:
            lock(c,sid)
            c.execute('''INSERT INTO alport_delivery_rules(organisation_id,site_id,supplier_id,lead_days,delivery_days) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(organisation_id,site_id,supplier_id) DO UPDATE SET lead_days=excluded.lead_days,delivery_days=excluded.delivery_days''',(org,sid,supplier,lead,','.join(map(str,days))))
            c.execute('''INSERT INTO alport_supplier_rules(organisation_id,site_id,supplier_id,cutoff,order_days,minimum_value,minimum_packs)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(organisation_id,site_id,supplier_id)
                DO UPDATE SET cutoff=excluded.cutoff,order_days=excluded.order_days,minimum_value=excluded.minimum_value,minimum_packs=excluded.minimum_packs''',
                (org,sid,supplier,policy['cutoff'],policy['order_days'],policy['minimum_value'],policy['minimum_packs']))
        return jsonify(ok=True)
    def forecast(d):
        # Keep receipts/postings from splitting on-hand and incoming reads.
        _,sid=scope()
        with conn() as guard:
            lock(guard,sid)
            return forecast_unlocked(d)
    def forecast_unlocked(d):
        org,sid=scope();today=datetime.now(ZoneInfo('Europe/London')).date()
        try:
            horizon=int(d.get('days',7));weather=float(d.get('weather_pct',0));events=float(d.get('event_pct',0))
        except (TypeError,ValueError):raise ValueError('Enter valid forecast values.') from None
        if not 1<=horizon<=14 or any(not math.isfinite(x) or x< -50 or x>200 for x in (weather,events)):raise ValueError('Use 1–14 days and adjustments between -50% and +200%.')
        note=str(d.get('note') or '').strip()[:1000]
        if (weather or events) and not note:raise ValueError('Explain your weather/event adjustment for the audit trail.')
        with conn() as c:
            sales,stocks,menus,recipes,maps,posted=snapshot(c,(today-timedelta(days=28)).isoformat(),(today-timedelta(days=1)).isoformat())
        daily=defaultdict(lambda:defaultdict(float));excluded=0;observed=set()
        for row in sales:
            try:use=usage_for_sale(unpack(row['data']),{key:m for (cid,key),m in maps.items() if cid==row['connection_id']},recipes,stocks)
            except (ValueError,TypeError,KeyError):excluded+=1;continue
            observed.add(row['sale_date'])
            for iid,qty in use.items():daily[iid][row['sale_date']]+=qty
        # A day with valid imported sales but no sales of this ingredient is an observed zero for that ingredient.
        for iid in stocks:
            for day in observed:daily[iid].setdefault(day,0)
        bookings={x['booking_date']:int(x['covers']) for x in q("SELECT booking_date,SUM(party_size) AS covers FROM bookings WHERE organisation_id=? AND site_id=? AND status NOT IN ('Cancelled','No-show') AND booking_date BETWEEN ? AND ? GROUP BY booking_date",(org,sid,(today-timedelta(days=28)).isoformat(),(today+timedelta(days=horizon-1)).isoformat()))}
        events_list=q('SELECT title,event_date,event_type FROM events WHERE organisation_id=? AND site_id=? AND event_date BETWEEN ? AND ? ORDER BY event_date',(org,sid,today.isoformat(),(today+timedelta(days=horizon-1)).isoformat()))
        products={x['stock_item_id']:x for x in q('''SELECT p.*,s.name AS supplier_name FROM alport_supplier_products p JOIN suppliers s ON s.id=p.supplier_id
            WHERE p.organisation_id=? AND p.site_id=? AND p.preferred=TRUE AND s.active=1''',(org,sid))}
        rules={x['supplier_id']:x for x in q('SELECT * FROM alport_delivery_rules WHERE organisation_id=? AND site_id=?',(org,sid))}
        with conn() as c:
            incoming=outstanding(c,org,sid,today.isoformat(),(today+timedelta(days=horizon-1)).isoformat())
        from alport_supplier_rules import delivery_window,minimum_review
        policies={x['supplier_id']:x for x in q('SELECT * FROM alport_supplier_rules WHERE organisation_id=? AND site_id=?',(org,sid))}
        clock=datetime.now(ZoneInfo('Europe/London'))
        lines=[]
        for iid,stock in stocks.items():
            prediction=estimate(daily[iid],today,horizon,bookings,weather,events)
            deliveries=incoming.get(iid,[])
            incoming_units=sum(x['quantity'] for x in deliveries if x['eligible'] and x['unit']==stock['unit'])
            shortage=max(0,prediction['units']+float(stock['par_level'])-float(stock['on_hand'])-incoming_units)
            product=products.get(iid);packs=None;cost=None;arrival=None;warning=''
            if not prediction['observed_days']:warning='No usable history; par-level replenishment only.'
            elif prediction['observed_days']<7:warning='Limited sales history; review this estimate carefully.'
            if product:
                pack=float(converted(product['pack_quantity'],product['pack_unit'],stock['unit']))
                packs=math.ceil(round(shortage/pack,9));cost=round(packs*float(product['pack_price']),2)
                rule=rules.get(product['supplier_id'])
                if rule:
                    window=delivery_window(clock,rule['lead_days'],[int(x) for x in rule['delivery_days'].split(',')],policies.get(product['supplier_id']))
                    arrival=window['arrival'];warning+=' '+window['message']
                    lead=(date.fromisoformat(arrival)-today).days
                    if lead and stock['on_hand']<prediction['units']/horizon*lead:warning+=' Stock may run out before delivery.'
                    if lead>=horizon:warning+=' Delivery falls outside this planning period.'
                else:warning+=' Add supplier lead time and delivery days.'
            else:warning+=' Choose a preferred supplier product.'
            if any(not x['eligible'] for x in deliveries):warning+=' Outstanding deliveries are overdue or outside this window; excluded from replenishment calculation.'
            if any(x['unit']!=stock['unit'] for x in deliveries):warning+=' Outstanding order units changed; reconcile before buying.'
            if incoming_units:
                balance=float(stock['on_hand'])
                for offset in range(horizon):
                    day=(today+timedelta(days=offset)).isoformat()
                    balance+=sum(x['quantity'] for x in deliveries if x['eligible'] and x['date']==day and x['unit']==stock['unit'])
                    balance-=prediction['units']/horizon
                    if balance<0:
                        warning+=' Stock may run short before an outstanding delivery; review timing.'
                        break
            lines.append({'incoming_units':round(incoming_units,6),'outstanding_deliveries':deliveries,'pack_price':float(product['pack_price']) if product else None,'product_id':product['id'] if product else None,'stock_item_id':iid,'name':stock['name'],'unit':stock['unit'],'on_hand':stock['on_hand'],'par_level':stock['par_level'],**prediction,'shortage':round(shortage,6),'packs':packs,'cost':cost,'supplier_id':product['supplier_id'] if product else None,'supplier':product['supplier_name'] if product else '', 'product':product['product_name'] if product else '', 'arrival':arrival,'warning':warning.strip()})
        supplier_checks=[]
        for supplier in sorted({x['supplier_id'] for x in lines if x.get('packs')}):
            group=[x for x in lines if x['supplier_id']==supplier and x.get('packs')]
            supplier_checks.append({'supplier_id':supplier,'supplier':group[0]['supplier'],**minimum_review(group,policies.get(supplier))})
        pending=q('SELECT COUNT(*) AS n FROM alport_order_drafts WHERE organisation_id=? AND site_id=? AND status IN (?,?)',(org,sid,'Draft','Approved'),True)['n']
        result={'supplier_checks':supplier_checks,'today':today.isoformat(),'days':horizon,'lines':lines,'bookings':bookings,'events':events_list,'weather_pct':weather,'event_pct':events,'note':note,'excluded_sales':excluded,'observed_days':len(observed),'existing_drafts':pending,
                'method':'20% yesterday, 30% last 7 days, 50% last 28 days (observed trading days only); weekday blend where 2+ observations exist. Bookings can increase demand up to 2×. Weather/event adjustments are entered by staff, not learned automatically. Par level is a safety buffer. Unreceived quantities on placed orders due within this window reduce suggested purchases. Overdue orders and deliveries outside this window are excluded; review those exceptions before buying. Drafts and approvals are not incoming stock.'}
        result['fingerprint']=digest({k:v for k,v in result.items() if k!='existing_drafts'});return result
    @bp.post('/api/demand/forecast')
    def forecast_preview():return jsonify(forecast(request.get_json() or {}))
    @bp.post('/api/demand/drafts')
    def save_draft():
        d=request.get_json() or {};result=forecast(d);org,sid=scope();u,_=context()
        if d.get('fingerprint')!=result['fingerprint']:raise ValueError('Forecast inputs changed. Refresh the suggestion before saving.')
        with conn() as c:
            r=c.execute('''INSERT INTO alport_order_drafts(organisation_id,site_id,fingerprint,payload,created_by,created_at)
                VALUES(%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(organisation_id,site_id,fingerprint) DO UPDATE SET fingerprint=excluded.fingerprint RETURNING id''',(org,sid,result['fingerprint'],dump(result),u['id'],env['now']())).fetchone()
        return jsonify(ok=True,id=r['id'])
    @bp.get('/api/demand/drafts/<int:identifier>')
    def get_draft(identifier):
        org,sid=scope();r=q('SELECT * FROM alport_order_drafts WHERE id=? AND organisation_id=? AND site_id=?',(identifier,org,sid),True)
        if not r: return jsonify(error='Draft not found.'),404
        r['payload']=unpack(r['payload']);return jsonify(r)
    @bp.get('/api/demand/drafts/<int:identifier>/csv')
    def export_draft(identifier):
        import csv,io
        org,sid=scope()
        r=q('SELECT payload FROM alport_order_drafts WHERE id=? AND organisation_id=? AND site_id=?',(identifier,org,sid),True)
        if not r:return jsonify(error='Draft not found.'),404
        output=io.StringIO();writer=csv.writer(output)
        writer.writerow(['Supplier','Ingredient','Product','Packs','Estimated net cost GBP','Earliest delivery','Review notes'])
        def cell(value):
            text=str(value or '')
            return "'"+text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text
        for item in unpack(r['payload'])['lines']:
            if item.get('packs'):
                writer.writerow([cell(item['supplier']),cell(item['name']),cell(item['product']),item['packs'],item['cost'],item['arrival'] or '',cell(item['warning'])])
        return Response(output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=alport-draft-{identifier}.csv'})
    @bp.post('/api/demand/drafts/<int:identifier>/approve')
    def approve(identifier):
        org,sid=scope();u,_=context()
        with conn() as c:
            r=c.execute("UPDATE alport_order_drafts SET status='Approved',approved_by=%s,approved_at=%s WHERE id=%s AND organisation_id=%s AND site_id=%s AND status='Draft' RETURNING id",(u['id'],env['now'](),identifier,org,sid)).fetchone()
            if not r:raise ValueError('Draft not found or already approved.')
        return jsonify(ok=True,message='Approved internally. No order has been sent to a supplier.')
    app.register_blueprint(bp)

    # Stocktakes and manual movements share the posting lock so one cannot overwrite another.
    def stock_route(fn):
        from functools import wraps
        @wraps(fn)
        def safe(*args,**kwargs):
            try:return fn(*args,**kwargs)
            except (ValueError,TypeError,KeyError) as e:return jsonify(error=str(e)),400
        return env['login_required'](env['manager_required'](safe))
    def ensure_no_count(c,org,sid):
        if c.execute("SELECT id FROM stocktakes WHERE organisation_id=%s AND site_id=%s AND status='Open'",(org,sid)).fetchone():raise ValueError('Complete the open stocktake first.')
    @stock_route
    def start_count():
        u,s=context();org,sid=scope()
        with conn() as c:
            lock(c,sid);ensure_no_count(c,org,sid)
            identifier=c.execute("INSERT INTO stocktakes(organisation_id,site_id,stocktake_date,status,started_by,started_at) VALUES(%s,%s,%s,'Open',%s,%s) RETURNING id",(org,sid,date.today().isoformat(),u['id'],env['now']())).fetchone()['id']
            for stock in rows(c,'SELECT * FROM stock_items WHERE organisation_id=%s AND site_id=%s AND active=1 ORDER BY id FOR UPDATE',(org,sid)):
                c.execute('INSERT INTO stocktake_lines(stocktake_id,stock_item_id,expected_quantity,unit_cost) VALUES(%s,%s,%s,%s)',(identifier,stock['id'],stock['on_hand'],stock['unit_cost']))
        env['audit']('Started','stocktake',identifier);return jsonify(ok=True,id=identifier)
    @stock_route
    def count_line(stocktake_id):
        org,sid=scope();d=request.get_json() or {};count=float(number(d.get('counted_quantity'),'Count'));line=int(d.get('line_id',0))
        with conn() as c:
            lock(c,sid)
            if not c.execute("SELECT id FROM stocktakes WHERE id=%s AND organisation_id=%s AND site_id=%s AND status='Open'",(stocktake_id,org,sid)).fetchone():raise ValueError('Open stocktake not found.')
            row=c.execute('SELECT * FROM stocktake_lines WHERE id=%s AND stocktake_id=%s',(line,stocktake_id)).fetchone()
            if not row:raise ValueError('Stocktake line not found.')
            variance=count-float(row['expected_quantity'])
            c.execute('UPDATE stocktake_lines SET counted_quantity=%s,variance_quantity=%s,variance_value=%s,counted_at=%s WHERE id=%s',(count,variance,variance*float(row['unit_cost']),env['now'](),line))
        return jsonify(ok=True)
    @stock_route
    def finish_count(stocktake_id):
        org,sid=scope();u,_=context()
        with conn() as c:
            lock(c,sid)
            if not c.execute("SELECT id FROM stocktakes WHERE id=%s AND organisation_id=%s AND site_id=%s AND status='Open'",(stocktake_id,org,sid)).fetchone():raise ValueError('Open stocktake not found.')
            lines=rows(c,'SELECT * FROM stocktake_lines WHERE stocktake_id=%s ORDER BY stock_item_id',(stocktake_id,))
            if any(x['counted_quantity'] is None for x in lines):raise ValueError('Count every item first.')
            for line in lines:
                c.execute('UPDATE stock_items SET on_hand=%s WHERE id=%s AND organisation_id=%s AND site_id=%s',(line['counted_quantity'],line['stock_item_id'],org,sid))
                if abs(line['variance_quantity'])>1e-6:
                    c.execute('INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(org,sid,line['stock_item_id'],line['variance_quantity'],'Stocktake variance',f'Stocktake #{stocktake_id}',env['now']()))
            c.execute("UPDATE stocktakes SET status='Complete',completed_by=%s,completed_at=%s WHERE id=%s",(u['id'],env['now'](),stocktake_id))
        env['audit']('Completed','stocktake',stocktake_id);return jsonify(ok=True)
    @stock_route
    def move_stock(iid):
        org,sid=scope();d=request.get_json() or {};qty=float(d.get('quantity',0))
        if not math.isfinite(qty) or not qty or abs(qty)>1e9:raise ValueError('Enter a finite, nonzero quantity.')
        with conn() as c:
            lock(c,sid);ensure_no_count(c,org,sid)
            stock=c.execute('SELECT * FROM stock_items WHERE id=%s AND organisation_id=%s AND site_id=%s FOR UPDATE',(iid,org,sid)).fetchone()
            if not stock:raise ValueError('Stock item not found.')
            after=float(stock['on_hand'])+qty
            if after<0:raise ValueError('This movement would make recorded stock negative. Reconcile the quantity first.')
            c.execute('UPDATE stock_items SET on_hand=on_hand+%s WHERE id=%s',(qty,iid))
            c.execute('INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',(org,sid,iid,qty,str(d.get('movement_type') or 'Adjustment')[:80],str(d.get('note') or '')[:1000],env['now']()))
        env['audit']('Updated','stock_item',iid,f'Movement {qty}');return jsonify(ok=True,on_hand=after)
    for name,fn in [('stocktake_start',start_count),('stocktake_count',count_line),('stocktake_complete',finish_count),('stock_move',move_stock)]:
        if name in app.view_functions:app.view_functions[name]=fn
