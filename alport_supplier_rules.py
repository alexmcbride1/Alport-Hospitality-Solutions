"""Venue-specific supplier deadlines and minimums. Never increases order quantities."""
from datetime import datetime,timedelta,time
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import re
from alport_inventory import number

SCHEMA='''CREATE TABLE IF NOT EXISTS alport_supplier_rules (
 organisation_id BIGINT NOT NULL,site_id BIGINT NOT NULL,supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
 cutoff TEXT NOT NULL DEFAULT '',order_days TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
 minimum_value NUMERIC(16,2) NOT NULL DEFAULT 0,minimum_packs INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(organisation_id,site_id,supplier_id))'''

def validate(d):
    cutoff=str(d.get('cutoff') or '').strip()
    if cutoff and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',cutoff):raise ValueError('Use a valid cut-off time such as 14:00, or leave it blank.')
    try:
        days=sorted(set(int(x) for x in str(d.get('order_days','0,1,2,3,4,5,6')).split(',')))
        packs=int(str(d.get('minimum_packs',0)))
    except (ValueError,TypeError):raise ValueError('Choose order weekdays and a whole minimum pack quantity.') from None
    if not days or any(x<0 or x>6 for x in days):raise ValueError('Choose at least one valid order weekday.')
    if not 0<=packs<=100000:raise ValueError('Minimum packs must be between 0 and 100,000.')
    value=number(d.get('minimum_value',0),'Minimum order value')
    if value!=value.quantize(Decimal('.01')):raise ValueError('Enter minimum spend to two decimal places.')
    return dict(cutoff=cutoff,order_days=','.join(map(str,days)),minimum_value=float(value),minimum_packs=packs)

def delivery_window(now,lead,delivery_days,rule=None):
    rule=rule or {};local=now.astimezone(ZoneInfo('Europe/London'))
    order_days={int(x) for x in rule.get('order_days','0,1,2,3,4,5,6').split(',')}
    cutoff=rule.get('cutoff','');accepted=None
    for offset in range(8):
        candidate=local.date()+timedelta(days=offset)
        if candidate.weekday() not in order_days:continue
        if offset==0 and cutoff and local.time().replace(tzinfo=None)>=time.fromisoformat(cutoff):continue
        accepted=candidate;break
    if accepted is None:raise ValueError('Supplier order days are invalid.')
    arrival=None
    for offset in range(int(lead),int(lead)+8):
        candidate=accepted+timedelta(days=offset)
        if candidate.weekday() in delivery_days:arrival=candidate;break
    if arrival is None:raise ValueError('Supplier delivery days are invalid.')
    return dict(order_date=accepted.isoformat(),arrival=arrival.isoformat(),cutoff=cutoff,timezone='Europe/London',
                delayed=accepted>local.date(),message=('Order acceptance moves to '+accepted.isoformat()+'. ') if accepted>local.date() else '')

def minimum_review(lines,rule=None):
    rule=rule or {};total=sum((Decimal(str(x['packs']))*Decimal(str(x['pack_price'])) for x in lines),Decimal(0)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    packs=sum((Decimal(str(x['packs'])) for x in lines),Decimal(0))
    value_gap=max(Decimal(0),Decimal(str(rule.get('minimum_value',0)))-total)
    pack_gap=max(Decimal(0),Decimal(str(rule.get('minimum_packs',0)))-packs)
    return dict(total=float(total),packs=float(packs),minimum_value=float(rule.get('minimum_value',0)),minimum_packs=int(rule.get('minimum_packs',0)),
                value_gap=float(value_gap),pack_gap=float(pack_gap),below_minimum=bool(value_gap or pack_gap))

def order_review(c,org,sid,supplier,lines,now=None):
    rule=c.execute('SELECT * FROM alport_supplier_rules WHERE organisation_id=%s AND site_id=%s AND supplier_id=%s',(org,sid,supplier)).fetchone()
    schedule=c.execute('SELECT * FROM alport_delivery_rules WHERE organisation_id=%s AND site_id=%s AND supplier_id=%s',(org,sid,supplier)).fetchone()
    result=minimum_review(lines,dict(rule) if rule else None)
    result['schedule']=delivery_window(now or datetime.now(ZoneInfo('Europe/London')),schedule['lead_days'],[int(x) for x in schedule['delivery_days'].split(',')],dict(rule) if rule else None) if schedule else None
    return result
