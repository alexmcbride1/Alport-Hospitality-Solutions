import re,secrets
from datetime import datetime,date,timedelta,time
from zoneinfo import ZoneInfo
ZONE=ZoneInfo('Europe/London')
PROMPTS={
 'date':'What date would you like? Please use DD/MM/YYYY (or say today or tomorrow).',
 'time':'What time would you like? Please use 24-hour format, for example 19:30.',
 'party':'How many guests, including children?',
 'name':'What name should the reservation be under?',
 'contact':'Please provide a contact email or phone number for the booking.',
 'requests':'Any seating or accessibility requests? Reply NONE if not. Please discuss allergies directly with the venue; do not send medical details here.',
}
def localnow():return datetime.now(ZONE)
def minutes(s):
 if not re.fullmatch(r'\d{2}:\d{2}',str(s)):raise ValueError('Use HH:MM')
 h,m=map(int,s.split(':'))
 if h>23 or m>59:raise ValueError('Invalid time')
 return h*60+m

def options(c,org,site,data,at=None):
 at=at or localnow();day=date.fromisoformat(data['date']);start=minutes(data['time']);party=int(data['party'])
 cfg=c.execute('SELECT * FROM booking_settings WHERE organisation_id=%s AND site_id=%s',(org,site)).fetchone()
 if not cfg:return [],'staff'
 duration=int(cfg['default_duration']);gap=int(cfg['turnaround_minutes']);interval=int(cfg['booking_interval'])
 if duration<15 or interval<5 or interval>120 or gap<0:return [],'staff'
 if party>int(cfg['max_party_size']):return [],'staff'
 if cfg.get('large_party_threshold') and party>=int(cfg['large_party_threshold']):return [],'staff'
 if cfg.get('deposit_type','none')!='none' and float(cfg.get('deposit_value') or 0)>0 and party>=int(cfg.get('deposit_min_party') or 1):return [],'staff'
 when=datetime.combine(day,time(start//60,start%60),ZONE)
 if when<at+timedelta(minutes=int(cfg['min_notice_minutes'])) or day>at.date()+timedelta(days=int(cfg['advance_days'])):return [],'unavailable'
 sessions=c.execute('SELECT * FROM booking_sessions WHERE organisation_id=%s AND site_id=%s AND day_of_week=%s AND active=1',(org,site,day.weekday())).fetchall()
 if not any(minutes(x['start_time'])<=start and start+duration<=minutes(x['end_time']) and (start-minutes(x['start_time']))%interval==0 for x in sessions):return [],'unavailable'
 rows=c.execute("SELECT * FROM bookings WHERE organisation_id=%s AND site_id=%s AND booking_date>=%s AND booking_date<=%s AND status NOT IN ('Cancelled','No-show')",(org,site,(day-timedelta(days=1)).isoformat(),(day+timedelta(days=1)).isoformat())).fetchall()
 if sum(int(b['party_size']) for b in rows if b['booking_date']==data['date'] and minutes(b['booking_time'])//interval==start//interval)+party>int(cfg['max_covers_per_interval']):return [],'unavailable'
 def overlaps(b):
  other=(date.fromisoformat(b['booking_date'])-day).days*1440+minutes(b['booking_time'])
  return start<other+int(b['duration_minutes'])+gap and other<start+duration+gap
 # Unassigned reservations still consume capacity: do not assume they have a table.
 if any(b['table_id'] is None and overlaps(b) for b in rows):return [],'staff'
 tables=c.execute('SELECT * FROM restaurant_tables WHERE organisation_id=%s AND site_id=%s AND active=1 AND min_capacity<=%s AND max_capacity>=%s ORDER BY max_capacity,sort_order,id',(org,site,party,party)).fetchall()
 return [t for t in tables if not any(b['table_id']==t['id'] and overlaps(b) for b in rows)],'available'

def alternatives(c,org,site,d,at=None):
 cfg=c.execute('SELECT * FROM booking_settings WHERE organisation_id=%s AND site_id=%s',(org,site)).fetchone()
 if not cfg:return []
 step=max(5,int(cfg['booking_interval']));wanted=minutes(d['time']);out=[]
 for offset in range(4):
  day=date.fromisoformat(d['date'])+timedelta(days=offset)
  sessions=c.execute('SELECT * FROM booking_sessions WHERE organisation_id=%s AND site_id=%s AND day_of_week=%s AND active=1',(org,site,day.weekday())).fetchall()
  starts=set()
  for s in sessions:starts.update(range(minutes(s['start_time']),max(minutes(s['start_time']),minutes(s['end_time'])-int(cfg['default_duration'])+1),step))
  for start in sorted(starts,key=lambda x:(abs(x-wanted),x)):
   candidate={**d,'date':day.isoformat(),'time':f'{start//60:02d}:{start%60:02d}'}
   tables,_=options(c,org,site,candidate,at)
   if tables:out.append({'date':candidate['date'],'time':candidate['time']})
   if len(out)==3:return out
 return out

def summary(d):return f"{d['party']} guests on {date.fromisoformat(d['date']).strftime('%d/%m/%Y')} at {d['time']} (UK time), under {d['name']}. Contact: {d['contact']}."
def advance(state,text,check,find_alternatives,book,at=None):
 """Callbacks execute within one DB transaction and the venue lock."""
 at=at or localnow();s=dict(state);text=text.strip();low=text.lower()
 if s.get('stage')=='human':
  if low=='new booking':s={}
  else:return s,None
 if re.search(r'\b(staff|human|manager|cancel|change|amend|allergy|allergies|allergic)\b',low):return {**s,'stage':'human'},'I have flagged this for the venue team. No booking has been changed or cancelled. Please contact the venue directly if urgent.'
 if low=='stop':return {**s,'stage':'human'},'Automated replies are paused. No existing booking has been cancelled.'
 if s.get('stage')=='booked':
  if low!='new booking':return s,'Your reservation is already recorded. Reply NEW BOOKING for another reservation, or STAFF for help with the existing one.'
  s={}
 if not s:
  if not re.search(r'\b(book|booking|reserve|reservation|table)\b',low):return {'stage':'human'},'I’m the venue’s automated reservation assistant. For a new table reservation, reply NEW BOOKING. Other enquiries are for the venue team.'
  return {'stage':'date'},'I’m the venue’s automated reservation assistant. I’ll check availability and ask you to confirm before booking. '+PROMPTS['date']
 stage=s['stage'];d=dict(s.get('details',{}))
 try:
  if stage=='date':
   day=at.date()+timedelta(days=low=='tomorrow') if low in ('today','tomorrow') else datetime.strptime(text,'%d/%m/%Y').date() if '/' in text else date.fromisoformat(text)
   if day<at.date() or day>at.date()+timedelta(days=366):raise ValueError()
   d['date']=day.isoformat();stage='time'
  elif stage=='time':minutes(text);d['time']=text;stage='party'
  elif stage=='party':
   if not text.isdigit() or not 1<=int(text)<=100:raise ValueError()
   d['party']=int(text);stage='name'
  elif stage=='name':
   if not 2<=len(text)<=100 or '@' in text:raise ValueError()
   d['name']=text;stage='contact'
  elif stage=='contact':
   if not (re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',text) or (re.fullmatch(r'\+?[\d ()-]{7,25}',text) and 7<=sum(c.isdigit() for c in text)<=15)):raise ValueError()
   d['contact']=text;stage='requests'
  elif stage=='requests':
   if low not in ('none','no','no thanks'):return {'stage':'human','details':d},'Your request needs the venue team to check it. I’ve flagged the conversation; a table is not yet booked.'
   stage='check'
  elif stage=='choose':
   if not text.isdigit() or not 1<=int(text)<=len(s['choices']):raise ValueError()
   d.update(s['choices'][int(text)-1]);stage='check'
  elif stage=='confirm':
   if low=='restart':return {'stage':'date'},PROMPTS['date']
   if low!='confirm':return s,'Reply CONFIRM to book those exact details, RESTART to enter different details, or STAFF for help.'
   tables,reason=check(d)
   if tables:
    bid=book(d,tables[0]);return {'stage':'booked','booking_id':bid,'details':d},'Your table is booked. '+summary(d)+f' Booking reference: ALP-{bid}. Reply STAFF for changes or cancellations.'
   stage='check'
  else:return {'stage':'human'},'Please ask the venue team for help with this conversation.'
 except (ValueError,KeyError):return s, PROMPTS.get(s.get('stage'),'Please reply with one of the numbered alternatives, or STAFF for help.')
 if stage=='check':
  tables,reason=check(d)
  if reason=='staff':return {'stage':'human','details':d},'This reservation needs the venue team to check capacity, booking rules or deposits. I’ve flagged it for them. It is not yet booked.'
  if tables:return {'stage':'confirm','details':d},summary(d)+' This is available now but not held. Reply CONFIRM to book, RESTART to change details, or STAFF for help.'
  choices=find_alternatives(d)
  if choices:return {'stage':'choose','details':d,'choices':choices},'That slot is unavailable. Available alternatives (not held):\n'+'\n'.join(f"{i+1}. {x['date']} at {x['time']}" for i,x in enumerate(choices))+'\nReply with the option number, or STAFF for help.'
  return {'stage':'human','details':d},'I couldn’t find a suitable slot in the requested day or the following three days. The venue team will need to help; no reservation has been made.'
 return {'stage':stage,'details':d},PROMPTS[stage]
