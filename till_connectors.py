import os, json, hashlib, time
from contextvars import ContextVar
_deadline=ContextVar("till_deadline", default=None)
from decimal import Decimal, ROUND_HALF_EVEN
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests

PROVIDERS={
 'square':{'name':'Square','auth':'oauth','scope':'MERCHANT_PROFILE_READ ORDERS_READ PAYMENTS_READ ITEMS_READ','authorize':'https://connect.squareup.com/oauth2/authorize','token':'https://connect.squareup.com/oauth2/token'},
 'lightspeed':{'name':'Lightspeed Restaurant K-Series','auth':'oauth','scope':'financial-api offline_access','authorize':'https://auth.lsk-prod.app/realms/k-series/protocol/openid-connect/auth','token':'https://auth.lsk-prod.app/realms/k-series/protocol/openid-connect/token'},
 'eposnow':{'name':'Epos Now','auth':'basic'},
 'zettle':{'name':'Zettle','auth':'oauth','scope':'READ:PURCHASE READ:USERINFO','authorize':'https://oauth.zettle.com/authorize','token':'https://oauth.zettle.com/token'},
}

class TillError(ValueError): pass

def minor(value):
 d=Decimal(str(value))
 if not d.is_finite() or abs(d)>100000000:raise TillError('Invalid monetary value from till.')
 return int((d*100).quantize(Decimal('1'),rounding=ROUND_HALF_EVEN))

def quantity(value):
 d=Decimal(str(value))
 if not d.is_finite() or abs(d)>1000000:raise TillError('Invalid quantity from till.')
 return float(d)

def http(method,url,**kwargs):
 try:
  remaining=(_deadline.get()-time.monotonic()) if _deadline.get() else 30
  if remaining<=0:raise TillError('Import time limit reached. Select a smaller range or ask support about incremental ingestion.')
  r=requests.request(method,url,timeout=(min(5,remaining),min(25,remaining)),allow_redirects=False,**kwargs)
  if not 200<=r.status_code<300:raise TillError(f'Till returned HTTP {r.status_code}. Check access, permissions or rate limits; retry later.')
  return r.json()
 except requests.RequestException:raise TillError('Till connection timed out or failed. No partial import was saved.') from None
 except ValueError as e:
  if isinstance(e,TillError):raise
  raise TillError('The till returned an unreadable response.') from None

def square_base():return 'https://connect.squareupsandbox.com' if os.getenv('SQUARE_ENVIRONMENT','sandbox')=='sandbox' else 'https://connect.squareup.com'
def ls_base():return 'https://api.lsk.lightspeed.app' if os.getenv('LIGHTSPEED_ENVIRONMENT')=='production' else 'https://api.trial.lsk.lightspeed.app'
def auth_urls(provider):
 p=PROVIDERS[provider]
 if provider=='square':return square_base()+'/oauth2/authorize',square_base()+'/oauth2/token'
 if provider=='lightspeed' and os.getenv('LIGHTSPEED_ENVIRONMENT')!='production':
  base='https://auth.lsk-demo.app/realms/k-series/protocol/openid-connect/'
  return base+'auth',base+'token'
 return p['authorize'],p['token']
def credentials(provider):
 prefix=provider.upper();return os.getenv(prefix+'_CLIENT_ID',''),os.getenv(prefix+'_CLIENT_SECRET','')
def exchange(provider,fields):
 cid,secret=credentials(provider)
 if not cid or not secret:raise TillError('Alport administrator must configure this provider application first.')
 payload={'client_id':cid,'client_secret':secret,**fields}
 return http('POST',auth_urls(provider)[1],**({'json':payload} if provider=='square' else {'data':payload}))
def headers(provider,token):
 if provider=='eposnow':return {'Authorization':'Basic '+token,'Accept':'application/json'}
 return {'Authorization':'Bearer '+token,'Accept':'application/json',**({'Square-Version':'2025-08-20'} if provider=='square' else {})}

def paged_list(url,h,params=None):
 result=[]
 for page in range(1,501):
  rows=http('GET',url,headers=h,params={**(params or {}),'page':page})
  if not isinstance(rows,list):raise TillError('Unexpected paged response; import stopped.')
  result.extend(rows)
  if len(rows)<200:return result
 raise TillError('Account exceeds this connector scan limit. Ask Alport support to configure incremental ingestion.')

def locations(provider,token):
 h=headers(provider,token)
 if provider=='square':
  rows=http('GET',square_base()+'/v2/locations',headers=h).get('locations',[])
  return [{'id':x['id'],'name':x['name'],'currency':x.get('currency'),'account':x.get('merchant_id',x['id'])} for x in rows if x.get('status')=='ACTIVE']
 if provider=='lightspeed':
  out=[]
  for page in range(100):
   rows=http('GET',ls_base()+'/f/data/businesses',headers=h,params={'page':page,'size':100}).get('_embedded',{}).get('businessList',[])
   for b in rows:
    for x in b.get('businessLocations',[]):out.append({'id':str(x['blID']),'name':x['blName'],'currency':b['currencyCode'],'account':str(b['businessId'])})
   if len(rows)<100:return out
  raise TillError('Business list exceeds supported pagination limit.')
 if provider=='eposnow':
  return [{'id':str(x['Id']),'name':x['Name'],'currency':'GBP','account':'eposnow'} for x in paged_list('https://api.eposnowhq.com/api/v4/Location',h)]
 # Zettle purchase API is merchant-wide; it does not expose a public till-location API.
 info=http('GET','https://oauth.zettle.com/users/self',headers=h)
 account=str(info.get('organizationUuid') or info.get('organization',{}).get('uuid') or '')
 if not account:raise TillError('Zettle did not return a merchant organisation identifier.')
 return [{'id':account,'name':'Entire Zettle merchant account (one Alport site only)','currency':'GBP','account':account}]

def signature(base,modifiers):
 return str(base)+(':'+hashlib.sha256(json.dumps(modifiers,sort_keys=True).encode()).hexdigest()[:16] if modifiers else '')
def line(key,name,qty):return {'key':str(key),'name':str(name or key),'quantity':quantity(qty)}
def record(tid,when,gross,tax,tips,service,lines,currency='GBP',warnings=None):
 if currency!='GBP':raise TillError('This release supports GBP venues only.')
 dt=datetime.fromisoformat(when.replace('Z','+00:00'))
 if dt.tzinfo is None:dt=dt.replace(tzinfo=ZoneInfo('Europe/London'))
 return {'id':str(tid),'at':dt.isoformat(),'gross_pence':int(gross),'tax_pence':int(tax),'tips_pence':int(tips),
         'service_pence':int(service),'lines':lines,'warnings':warnings or []}

def normalise_square(x):
 if x.get('state') not in ('COMPLETED','CANCELED'):return None
 amounts=x.get('net_amounts') or {};m=lambda key:int((amounts.get(key) or x.get(key) or {}).get('amount',0))
 lines=[]
 for item in x.get('line_items',[]):
  mods=[{'id':m.get('catalog_object_id') or m.get('name'),'quantity':m.get('quantity','1')} for m in item.get('modifiers',[])]
  lines.append(line(signature(item.get('catalog_object_id') or 'custom:'+item['name'],mods),item.get('name'),item['quantity']))
 warnings=[]
 if x.get('returns'):warnings.append('Refund/return present: stock is not automatically put back; review physical stock and item demand.')
 gross,tax,tips,service=m('total_money')-m('tip_money')-m('service_charge_money'),m('tax_money'),m('tip_money'),m('service_charge_money')
 if x.get('service_charges'):warnings.append('Service-charge tax may be included in order tax; accounting reconciliation required.')
 if x['state']=='CANCELED':gross=tax=tips=service=0;lines=[]
 return record(x['id'],x.get('closed_at') or x['updated_at'],gross,tax,tips,service,lines,(x.get('total_money') or {}).get('currency','GBP'),warnings)

def normalise_lightspeed(x):
 if x.get('type') not in ('SALE','REFUND') or x.get('cancelled'):return None
 rows=[a for a in x.get('salesLines',[]) if not a.get('voidReason')];lines=[]
 for a in rows:
  if a.get('voidReason'):continue
  lines.append(line(a.get('sku') or 'custom:'+a['name'],a.get('name'),a['quantity']))
 return record(x['accountFiscId'],x['timeClosed'],sum(minor(a['totalNetAmountWithTax']) for a in rows),sum(minor(a.get('taxAmount',0)) for a in rows),sum(minor(p.get('tip',0)) for p in x.get('payments',[])),sum(minor(a.get('serviceCharge',0)) for a in rows),lines,rows[0].get('currency','GBP') if rows else 'GBP', ['Service charges require classification before accounting or tronc.'] if any(Decimal(str(a.get('serviceCharge',0))) for a in rows) else [])

def normalise_epos(x,products):
 if x.get('StatusId')!=1:return None
 rows=x.get('TransactionItems',[]);lines=[]
 for a in rows:
  lines.append(line(signature(a['ProductId'],a.get('MultipleChoiceItems',[])),products.get(a['ProductId'],str(a['ProductId'])),a['Quantity']))
 tax=sum(minor(a.get('TaxAmount',0)) for a in rows)
 gross=sum(minor(Decimal(str(a['UnitPrice']))*Decimal(str(a['Quantity']))-Decimal(str(a.get('DiscountAmount',0)))) for a in rows)
 tip,svc=minor(x.get('Gratuity',0)),minor(x.get('ServiceCharge',0))
 warnings=[]
 if abs(gross+tip+svc-minor(x['TotalAmount']))>1:warnings.append('Transaction total differs from item totals: discounts/miscellaneous items require reconciliation.')
 return record(x['Id'],x['DateTime'],gross,tax,tip,svc,lines,warnings=warnings)

def normalise_zettle(x):
 tips=sum(int(p.get('gratuityAmount',0)) for p in x.get('payments',[]));warnings=[]
 lines=[line(signature(a.get('variantUuid') or a.get('productUuid') or 'custom:'+a['name'],a.get('comment')),a.get('name'),a['quantity']) for a in x.get('products',[]) if a.get('type')!='GIFTCARD']
 if any(a.get('type')=='GIFTCARD' for a in x.get('products',[])):warnings.append('Gift-card sale: requires accounting classification, not food/drink revenue.')
 return record(x['purchaseUUID1'],x['timestamp'],int(x['amount']),int(x['vatAmount']),tips,0,lines,x['currency'],warnings)

def _fetch_sales(provider,token,location,start,end):
 h=headers(provider,token);out=[];cursor=None;seen=set()
 if provider=='eposnow':
  devices={x['Id']:str(x['LocationId']) for x in paged_list('https://api.eposnowhq.com/api/v4/Device',h)}
  products={x['Id']:x['Name'] for x in paged_list('https://api.eposnowhq.com/api/v4/Product',h)}
  for x in paged_list('https://api.eposnowhq.com/api/v4/Transaction',h,{'status':1}):
   if x.get('DeviceId') not in devices:raise TillError('A transaction has an unknown device; resolve its location before importing.')
   if devices[x['DeviceId']]!=location:continue
   if start[:10]<=x['DateTime'][:10]<end[:10]:out.append(normalise_epos(x,products))
  return [x for x in out if x]
 for _ in range(500):
  if provider=='square':
   body={'location_ids':[location],'limit':500,'query':{'filter':{'date_time_filter':{'updated_at':{'start_at':start,'end_at':end}}},'sort':{'sort_field':'UPDATED_AT','sort_order':'ASC'}}}
   if cursor:body['cursor']=cursor
   response=http('POST',square_base()+'/v2/orders/search',headers=h,json=body)
   out.extend(filter(None,(normalise_square(x) for x in response.get('orders',[]))));nxt=response.get('cursor')
  elif provider=='lightspeed':
   response=http('GET',ls_base()+'/f/v2/business-location/'+quote(location,safe='')+'/sales',headers=h,params={'from':start,'to':end,'include':'payments','pageSize':100,**({'nextPageToken':cursor} if cursor else {})})
   out.extend(filter(None,(normalise_lightspeed(x) for x in response.get('sales',[]))));nxt=response.get('nextPageToken')
  else:
   response=http('GET','https://purchase.izettle.com/purchases/v2',headers=h,params={'startDate':start,'endDate':end,'limit':100,'descending':'false',**({'lastPurchaseHash':cursor} if cursor else {})})
   rows=response.get('purchases',[]);out.extend(normalise_zettle(x) for x in rows)
   nxt=response.get('lastPurchaseHash') if rows and response.get('linkUrls') else None
   if len(rows)==100 and not nxt:raise TillError('Zettle pagination is incomplete; refusing a partial import.')
  if not nxt:return out
  if nxt in seen:raise TillError('Provider repeated its pagination cursor; no partial import saved.')
  seen.add(nxt);cursor=nxt
 raise TillError('Import exceeds 500 pages; select a smaller date range.')

def fetch_sales(provider,token,location,start,end):
 marker=_deadline.set(time.monotonic()+90)
 try:return _fetch_sales(provider,token,location,start,end)
 finally:_deadline.reset(marker)
