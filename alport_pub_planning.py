"""Cash planning, ingredient review, repairs and handovers; no automated compliance claims."""
from datetime import date,timedelta
from decimal import Decimal
import json
import hmac
import re
from flask import Blueprint,jsonify,request,session,Response
from alport_pub import MODULES,field,module,D,stamp,dump,digest,decimal
from alport_evidence import read_files

ALLERGENS=['Celery','Cereals containing gluten','Crustaceans','Eggs','Fish','Lupin','Milk','Molluscs','Mustard','Peanuts','Sesame','Soybeans','Sulphur dioxide and sulphites','Tree nuts']
STATES=['Unknown','Contains','May contain','Not declared in reviewed source']
MODULES.update({
 'cashflow':module('Cash-flow entries',[D,field('direction','Direction','choice',True,['Income','Outgoing']),field('category','Category','choice',True,['Sales','Wages','Rent','Suppliers','Tax provision','Other']),field('amount','Amount (£)','money'),field('certainty','Status','choice',True,['Expected','Confirmed','Cancelled']),field('assumption','Timing and amount assumptions / source','textarea')],'Dated planning entries, not bank transactions. Tax provisions are entered by you; the app does not calculate tax liabilities. Cancelled entries are excluded.',True),
 'repair':module('Equipment repairs and servicing',[D,field('equipment','Equipment / asset'),field('reference','Asset / service reference'),field('urgency','Urgency','choice',True,['Routine','Urgent','Critical']),field('details','Fault or service required','textarea'),field('assignee_id','Responsible person','user'),field('contractor','Contractor / contact reference','text',False),field('estimated_cost','Estimated cost (£)','money'),field('actual_cost','Actual cost (£)','money'),field('due_date','Due date','date'),field('completion_date','Completion date','date',False),field('repair_status','Status','choice',True,['Open','Booked','Done']),field('notes','Completion / service notes','textarea',False)],'Save first, then attach photos. Completion requires a date and notes. Record recurring service work as a new dated record for the same asset reference; history preserves previous jobs.'),
 'handover':module('Manager handover',[D,field('reference','Service / shift'),field('staffing','Staffing and coverage','textarea'),field('bookings','Large bookings / service plans','textarea'),field('unavailable','Unavailable products','textarea'),field('complaints','Unresolved complaints (minimum necessary detail)','textarea'),field('faults','Equipment faults','textarea'),field('summary','Next shift summary','textarea')],'Managers can publish a selected action, assignee and due date to the site’s Team chat. Only the action text is shared, not the whole handover.',True)
})
SCHEMA='''
CREATE TABLE IF NOT EXISTS alport_cash_forecast(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,start_date TEXT NOT NULL,opening_cash TEXT NOT NULL,
 assumptions TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,updated_by BIGINT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id));
CREATE TABLE IF NOT EXISTS alport_allergen_ingredients(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,stock_id BIGINT NOT NULL,
 states TEXT NOT NULL,source TEXT NOT NULL,checked_on TEXT NOT NULL,stock_signature TEXT NOT NULL,
 reviewed_by BIGINT NOT NULL,reviewed_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(organisation_id,site_id,stock_id));
CREATE TABLE IF NOT EXISTS alport_allergen_links(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,component_id BIGINT NOT NULL,stock_id BIGINT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,component_id));
CREATE TABLE IF NOT EXISTS alport_allergen_reviews(
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,menu_id BIGINT NOT NULL,signature TEXT NOT NULL,
 notes TEXT NOT NULL,reviewed_by BIGINT NOT NULL,reviewed_at TEXT NOT NULL,
 PRIMARY KEY(organisation_id,site_id,menu_id));
CREATE TABLE IF NOT EXISTS alport_pub_photos(
 id BIGSERIAL PRIMARY KEY,record_id BIGINT NOT NULL REFERENCES alport_pub_records(id),filename TEXT NOT NULL,
 mimetype TEXT NOT NULL,data BYTEA NOT NULL,uploaded_by BIGINT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alport_handover_actions(
 record_id BIGINT NOT NULL REFERENCES alport_pub_records(id),request_key TEXT NOT NULL,signature TEXT NOT NULL,
 message_id BIGINT NOT NULL,task_id BIGINT NOT NULL,PRIMARY KEY(record_id,request_key));
CREATE TABLE IF NOT EXISTS alport_pub_reviews_audit(
 id BIGSERIAL PRIMARY KEY,organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,
 kind TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def forecast(start,opening,entries):
    start=date.fromisoformat(start);balance=Decimal(opening);weeks=[]
    for i in range(13):
        a=start+timedelta(days=7*i);b=a+timedelta(days=7)
        relevant=[r for r in entries if a.isoformat()<=r['date']<b.isoformat() and r['certainty']!='Cancelled']
        incoming=sum((Decimal(r['amount']) for r in relevant if r['direction']=='Income'),Decimal(0))
        outgoing=sum((Decimal(r['amount']) for r in relevant if r['direction']=='Outgoing'),Decimal(0))
        close=balance+incoming-outgoing
        weeks.append(dict(start=a.isoformat(),end=(b-timedelta(days=1)).isoformat(),opening=format(balance,'.2f'),income=format(incoming,'.2f'),outgoings=format(outgoing,'.2f'),closing=format(close,'.2f'),entries=relevant));balance=close
    return weeks


def register_pub_planning(app,env):
    svc=app.extensions['alport_pub_services'];q,conn=svc['q'],svc['conn'];scope,lock,row,event=svc['scope'],svc['lock'],svc['row'],svc['event']
    bp=Blueprint('alport_pub_planning',__name__)
    with conn() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(739000000032,))
        for sql in SCHEMA.split(';'):
            if sql.strip():c.execute(sql)
    @bp.before_request
    def guard():
        if request.endpoint=='alport_pub_planning.upload':
            u=env['user']()
            if not u or env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):raise PermissionError('Active sign-in and subscription required.')
            if not session.get('pub_csrf') or not hmac.compare_digest(session['pub_csrf'],request.headers.get('X-Pub-CSRF','')):raise PermissionError('Reload before uploading.')
            request.max_content_length=9*1024*1024
        else:return svc['guard']()
    bp.after_request(svc['headers'])
    @bp.errorhandler(PermissionError)
    def denied(e):return jsonify(error=str(e)),403
    @bp.errorhandler(ValueError)
    def invalid(e):return jsonify(error=str(e)),400
    def audit(c,u,s,kind,d):c.execute('INSERT INTO alport_pub_reviews_audit(organisation_id,site_id,actor_id,kind,detail,created_at) VALUES(%s,%s,%s,%s,%s,%s)',(u['organisation_id'],s['id'],u['id'],kind,dump(d),stamp()))
    def text(d,key):
        v=d.get(key)
        if not isinstance(v,str) or not 1<=len(v.strip())<=4000:raise ValueError('Enter '+key.replace('_',' ')+' (up to 4,000 characters).')
        return v.strip()
    def day(v):
        try:return date.fromisoformat(v).isoformat()
        except (ValueError,TypeError):raise ValueError('Choose a valid date.') from None
    def stock(u,s,stock_id):
        if type(stock_id) is not int:raise ValueError('Choose a stock ingredient.')
        r=q('SELECT id,name,supplier,unit,category FROM stock_items WHERE id=? AND organisation_id=? AND site_id=? AND active=1',(stock_id,u['organisation_id'],s['id']),True)
        if not r:raise PermissionError('Ingredient not found in this site.')
        return r
    @bp.get('/api/pub/sites/<int:sid>/forecast')
    def get_forecast(sid):
        u,s=scope(sid,True);settings=q('SELECT * FROM alport_cash_forecast WHERE organisation_id=? AND site_id=?',(u['organisation_id'],sid),True)
        entries=[json.loads(r['data']) for r in q("SELECT data FROM alport_pub_records WHERE organisation_id=? AND site_id=? AND kind='cashflow'",(u['organisation_id'],sid))]
        return jsonify(settings=settings,weeks=forecast(settings['start_date'],settings['opening_cash'],entries) if settings else [])
    @bp.post('/api/pub/sites/<int:sid>/forecast')
    def set_forecast(sid):
        d=request.json;start=day(d.get('start_date'));opening=decimal(d.get('opening_cash'),-100000000);notes=text(d,'assumptions')
        if opening!=opening.quantize(Decimal('.01')):raise ValueError('Opening cash needs at most two decimal places.')
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);old=c.execute('SELECT version FROM alport_cash_forecast WHERE organisation_id=%s AND site_id=%s',(u['organisation_id'],sid)).fetchone()
            if type(d.get('version')) is not int or d['version']!=(old['version'] if old else 0):return jsonify(error='Forecast settings changed. Refresh.'),409
            c.execute('''INSERT INTO alport_cash_forecast(organisation_id,site_id,start_date,opening_cash,assumptions,updated_by,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(organisation_id,site_id) DO UPDATE SET start_date=excluded.start_date,opening_cash=excluded.opening_cash,assumptions=excluded.assumptions,updated_by=excluded.updated_by,updated_at=excluded.updated_at,version=alport_cash_forecast.version+1''',(u['organisation_id'],sid,start,str(opening),notes,u['id'],stamp()))
            audit(c,u,s,'Forecast settings',d)
        return jsonify(ok=True)
    def matrix(u,s):
        menus=q('SELECT id,name FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',(u['organisation_id'],s['id']));result=[]
        for menu in menus:
            components=q('SELECT id,component_name,quantity,unit,notes FROM menu_components WHERE organisation_id=? AND site_id=? AND menu_item_id=? ORDER BY id',(u['organisation_id'],s['id'],menu['id']))
            statuses={a:[] for a in ALLERGENS};details=[]
            for comp in components:
                link=q('SELECT stock_id FROM alport_allergen_links WHERE organisation_id=? AND site_id=? AND component_id=?',(u['organisation_id'],s['id'],comp['id']),True)
                ingredient=None;assessment=None
                if link:
                    ingredient=q('SELECT id,name,supplier,unit,category FROM stock_items WHERE id=? AND organisation_id=? AND site_id=? AND active=1',(link['stock_id'],u['organisation_id'],s['id']),True)
                    assessment=q('SELECT * FROM alport_allergen_ingredients WHERE organisation_id=? AND site_id=? AND stock_id=?',(u['organisation_id'],s['id'],link['stock_id']),True)
                current=bool(ingredient and assessment and digest(ingredient)==assessment['stock_signature'])
                states=json.loads(assessment['states']) if current else {a:'Unknown' for a in ALLERGENS}
                for a in ALLERGENS:statuses[a].append(states[a])
                details.append(dict(component=comp,component_signature=digest(comp),linked_stock_id=link["stock_id"] if link else None,stock=ingredient,assessment_version=assessment['version'] if assessment else None,current=current,states=states,source=assessment['source'] if assessment else ''))
            combined={a:('Contains' if 'Contains' in values else 'Unknown' if not values or 'Unknown' in values else 'May contain' if 'May contain' in values else 'Not declared in reviewed source') for a,values in statuses.items()}
            unknown=not components or any(not values or 'Unknown' in values for values in statuses.values())
            signature=digest(dict(menu=menu,components=details))
            review=q('SELECT * FROM alport_allergen_reviews WHERE organisation_id=? AND site_id=? AND menu_id=?',(u['organisation_id'],s['id'],menu['id']),True)
            result.append(dict(menu=menu,components=details,states=combined,unknown=unknown,review_required=unknown or not review or review['signature']!=signature,signature=signature,review=review))
        return result
    @bp.get('/api/pub/sites/<int:sid>/allergens')
    def allergens(sid):
        u,s=scope(sid);assessments=[]
        if s['manager']:
            for ingredient in q('SELECT id,name,supplier,unit,category FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',(u['organisation_id'],sid)):
                assessment=q('SELECT * FROM alport_allergen_ingredients WHERE organisation_id=? AND site_id=? AND stock_id=?',(u['organisation_id'],sid,ingredient['id']),True)
                assessments.append(dict(stock=ingredient,signature=digest(ingredient),assessment={**assessment,'states':json.loads(assessment['states'])} if assessment else None))
        return jsonify(allergens=ALLERGENS,states=STATES,ingredients=assessments,matrix=matrix(u,s),manager=s['manager'])
    @bp.post('/api/pub/sites/<int:sid>/allergens/ingredient')
    def ingredient_review(sid):
        d=request.json;states=d.get('states');source=text(d,'source');checked=day(d.get('checked_on'))
        if checked>date.today().isoformat():raise ValueError('A review cannot be dated in the future.')
        if not isinstance(states,dict) or set(states)!=set(ALLERGENS) or any(v not in STATES for v in states.values()):raise ValueError('Set an explicit state for all 14 allergens.')
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);ingredient=stock(u,s,d.get('stock_id'))
            if d.get('signature')!=digest(ingredient):return jsonify(error='Supplier or ingredient changed. Refresh and review its current specification.'),409
            old=c.execute('SELECT version FROM alport_allergen_ingredients WHERE organisation_id=%s AND site_id=%s AND stock_id=%s',(u['organisation_id'],sid,ingredient['id'])).fetchone()
            if type(d.get('version')) is not int or d['version']!=(old['version'] if old else 0):return jsonify(error='Ingredient review changed. Refresh.'),409
            c.execute('''INSERT INTO alport_allergen_ingredients(organisation_id,site_id,stock_id,states,source,checked_on,stock_signature,reviewed_by,reviewed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(organisation_id,site_id,stock_id) DO UPDATE SET states=excluded.states,source=excluded.source,checked_on=excluded.checked_on,stock_signature=excluded.stock_signature,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,version=alport_allergen_ingredients.version+1''',(u['organisation_id'],sid,ingredient['id'],dump(states),source,checked,digest(ingredient),u['id'],stamp()))
            audit(c,u,s,'Ingredient review',d)
        return jsonify(ok=True)
    @bp.post('/api/pub/sites/<int:sid>/allergens/link')
    def link(sid):
        d=request.json
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);ingredient=stock(u,s,d.get('stock_id'))
            if type(d.get('component_id')) is not int:raise ValueError('Choose a recipe component.')
            component=c.execute('SELECT id,component_name,quantity,unit,notes FROM menu_components WHERE id=%s AND organisation_id=%s AND site_id=%s',(d['component_id'],u['organisation_id'],sid)).fetchone()
            if not component:raise PermissionError('Recipe component not found in this site.')
            if d.get('component_signature')!=digest(component):return jsonify(error='Recipe changed. Refresh.'),409
            previous=c.execute('SELECT stock_id FROM alport_allergen_links WHERE organisation_id=%s AND site_id=%s AND component_id=%s',(u['organisation_id'],sid,component['id'])).fetchone()
            if d.get('expected_stock_id')!=(previous['stock_id'] if previous else None):return jsonify(error='Ingredient link changed. Refresh.'),409
            c.execute('''INSERT INTO alport_allergen_links(organisation_id,site_id,component_id,stock_id) VALUES(%s,%s,%s,%s)
                ON CONFLICT(organisation_id,site_id,component_id) DO UPDATE SET stock_id=excluded.stock_id''',(u['organisation_id'],sid,component['id'],ingredient['id']))
            audit(c,u,s,'Recipe ingredient link',d)
        return jsonify(ok=True)
    @bp.post('/api/pub/sites/<int:sid>/allergens/review')
    def recipe_review(sid):
        d=request.json;notes=text(d,'notes')
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);current=next((r for r in matrix(u,s) if r['menu']['id']==d.get('menu_id')),None)
            if not current:raise PermissionError('Recipe not found in this site.')
            if current['unknown']:raise ValueError('Resolve unknown ingredients and allergen states before reviewing this recipe.')
            if d.get('signature')!=current['signature']:return jsonify(error='Recipe or supplier information changed. Refresh.'),409
            c.execute('''INSERT INTO alport_allergen_reviews(organisation_id,site_id,menu_id,signature,notes,reviewed_by,reviewed_at) VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(organisation_id,site_id,menu_id) DO UPDATE SET signature=excluded.signature,notes=excluded.notes,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at''',(u['organisation_id'],sid,current['menu']['id'],current['signature'],notes,u['id'],stamp()))
            audit(c,u,s,'Recipe review',d)
        return jsonify(ok=True)
    @bp.post('/api/pub/sites/<int:sid>/records/<int:rid>/photo')
    def upload(sid,rid):
        files=read_files(request.files.getlist('photo'),photos_only=True)
        if len(files)!=1:raise ValueError('Choose one photo.')
        f=files[0]
        with conn() as c:
            u,s=scope(sid);lock(c,u['organisation_id']);r=row(c,rid,u,s)
            if r['kind']!='repair':raise ValueError('Photos belong to repair records.')
            if not s['manager'] and r['created_by']!=u['id']:raise PermissionError('Only the author or manager can add photos.')
            if c.execute('SELECT COUNT(*) AS n FROM alport_pub_photos WHERE record_id=%s',(rid,)).fetchone()['n']>=5:raise ValueError('At most five photos per repair.')
            photo=c.execute('INSERT INTO alport_pub_photos(record_id,filename,mimetype,data,uploaded_by,created_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',(rid,f['filename'],f['mimetype'],f['data'],u['id'],stamp())).fetchone()
            event(c,rid,u,'Photo added',{'filename':f['filename']})
        return jsonify(id=photo['id']),201
    @bp.get('/api/pub/sites/<int:sid>/records/<int:rid>/photos')
    def photos(sid,rid):
        with conn() as c:u,s=scope(sid);row(c,rid,u,s)
        return jsonify(photos=q('SELECT id,filename,created_at FROM alport_pub_photos WHERE record_id=? ORDER BY id',(rid,)))
    @bp.get('/api/pub/sites/<int:sid>/records/<int:rid>/photos/<int:pid>')
    def photo(sid,rid,pid):
        with conn() as c:u,s=scope(sid);row(c,rid,u,s)
        f=q('SELECT * FROM alport_pub_photos WHERE id=? AND record_id=?',(pid,rid),True)
        if not f:raise PermissionError('Photo not found.')
        return Response(bytes(f['data']),mimetype=f['mimetype'],headers={'Content-Disposition':'attachment; filename="'+f['filename']+'"'})
    @bp.post('/api/pub/sites/<int:sid>/records/<int:rid>/action')
    def handover_action(sid,rid):
        d=request.json;title=text(d,'title');due=day(d.get('due_date'));key=d.get('request_key','');uid=d.get('assignee_id')
        if len(title)>200 or type(uid) is not int or not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}',key):raise ValueError('Choose an assignee and an action up to 200 characters.')
        signature=digest(dict(title=title,due=due,uid=uid))
        with conn() as c:
            u,s=scope(sid,True);lock(c,u['organisation_id']);c.execute('SELECT pg_advisory_xact_lock(%s)',(741000000000+u['organisation_id'],));r=row(c,rid,u,s)
            if r['kind']!='handover':raise ValueError('Choose a handover.')
            person=c.execute('SELECT * FROM users WHERE id=%s AND organisation_id=%s AND active=1',(uid,u['organisation_id'])).fetchone()
            if not person or not any(x['id']==sid for x in app.extensions['alport_experience_sites'](person)):raise ValueError('Assignee must have an active site assignment.')
            old=c.execute('SELECT * FROM alport_handover_actions WHERE record_id=%s AND request_key=%s',(rid,key)).fetchone()
            if old:
                if old['signature']!=signature:return jsonify(error='Save key belongs to a different action.'),409
                return jsonify(message_id=old['message_id'],task_id=old['task_id'],duplicate=True)
            # Only the action is shared, not potentially private manager notes.
            body='Handover action: '+title+' (due '+due+')'
            message=c.execute('INSERT INTO alport_chat_messages(organisation_id,site_id,user_id,sender_name,body,request_key,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(u['organisation_id'],sid,u['id'],u['name'],body,'handover-'+str(rid)+'-'+key,stamp())).fetchone()
            task=c.execute('INSERT INTO alport_chat_tasks(organisation_id,site_id,message_id,title,assignee_id,due_date,created_by,created_at,updated_by,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(u['organisation_id'],sid,message['id'],title,uid,due,u['id'],stamp(),u['id'],stamp())).fetchone()
            c.execute('INSERT INTO alport_chat_mentions(message_id,user_id) VALUES(%s,%s)',(message['id'],uid))
            c.execute('INSERT INTO alport_chat_task_events(task_id,actor_id,status,created_at) VALUES(%s,%s,%s,%s)',(task['id'],u['id'],'Open',stamp()))
            c.execute('INSERT INTO alport_handover_actions(record_id,request_key,signature,message_id,task_id) VALUES(%s,%s,%s,%s,%s)',(rid,key,signature,message['id'],task['id']))
            event(c,rid,u,'Action shared',dict(title=title,assignee_id=uid,due_date=due,task_id=task['id']))
        return jsonify(message_id=message['id'],task_id=task['id']),201
    prior=app.extensions['alport_pub_attention']
    def attention(org,sid):
        result=prior(org,sid);today=date.today().isoformat();repairs=0
        for r in q("SELECT data FROM alport_pub_records WHERE organisation_id=? AND site_id=? AND kind='repair'",(org,sid)):
            d=json.loads(r['data']);repairs+=d['repair_status']!='Done' and (d['due_date']<=today or d['urgency']=='Critical')
        if repairs:result.append(dict(label='Repairs needing attention',count=repairs,url='/pub-operations?site='+str(sid)))
        reviews=sum(r['review_required'] for r in matrix({'organisation_id':org},{'id':sid}))
        if reviews:result.append(dict(label='Recipes needing allergen review',count=reviews,url='/pub-operations?site='+str(sid)+'&view=allergens'))
        settings=q('SELECT * FROM alport_cash_forecast WHERE organisation_id=? AND site_id=?',(org,sid),True)
        if settings:
            entries=[json.loads(r['data']) for r in q("SELECT data FROM alport_pub_records WHERE organisation_id=? AND site_id=? AND kind='cashflow'",(org,sid))]
            negative=sum(Decimal(w['closing'])<0 for w in forecast(settings['start_date'],settings['opening_cash'],entries))
            if negative:result.append(dict(label='Forecast weeks below zero',count=negative,url='/pub-operations?site='+str(sid)+'&view=cashflow'))
        return result
    app.extensions['alport_pub_attention']=attention
    app.register_blueprint(bp)
