from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.graphics import renderPDF

ROOT=Path(__file__).resolve().parent
OUT=ROOT.parents[1]/'private_docs/Alport-Website-User-Guide.pdf'
OUT.parent.mkdir(parents=True,exist_ok=True)
W,H=595.28,841.89
G=HexColor('#205b49'); D=HexColor('#173c2e'); BG=HexColor('#f4f7f5'); GOLD=HexColor('#c9b889'); MUT=HexColor('#52675c')
c=canvas.Canvas(str(OUT),pagesize=(W,H)); c.setTitle('Alport Hospitality Solutions | Website user guide'); c.setAuthor('Alport Hospitality Solutions')
style=ParagraphStyle('body',fontName='Helvetica',fontSize=11,leading=16,textColor=D)
def drawlogo():
 c.setStrokeColor(G); c.setLineWidth(1.7); c.circle(32,31,29,fill=0,stroke=1)
 p=c.beginPath(); p.moveTo(12,25); p.lineTo(24,39); p.lineTo(31,31); p.lineTo(39,42); p.lineTo(52,24); c.drawPath(p)
 p=c.beginPath(); p.moveTo(11,20); p.curveTo(28,32,36,13,53,23); c.drawPath(p)
 p=c.beginPath(); p.moveTo(11,15); p.curveTo(28,27,36,8,53,18); c.drawPath(p)
 c.setFillColor(G); c.circle(44,46,2.5,fill=1,stroke=0)
 c.setFillColor(D); c.setFont('Helvetica-Bold',25); c.drawString(75,32,'ALPORT'); c.setFont('Helvetica',7); c.drawString(76,16,'H O S P I T A L I T Y   S O L U T I O N S')
def text(s,x,y,w=499,size=11,color=D):
 st=ParagraphStyle('p',parent=style,fontSize=size,leading=size*1.45,textColor=color)
 p=Paragraph(s,st); _,h=p.wrap(w,800); p.drawOn(c,x,y-h); return y-h

def page(n,title,sub):
 c.setFillColor(BG); c.rect(0,0,W,H,fill=1,stroke=0)
 c.saveState(); c.translate(46,H-78); c.scale(.62,.62); drawlogo(); c.restoreState()
 c.setFillColor(GOLD); c.rect(46,H-96,503,3,fill=1,stroke=0)
 c.setFillColor(D); c.setFont('Helvetica-Bold',25); c.drawString(46,H-140,title)
 text(sub,46,H-156,size=10,color=MUT)
 c.setStrokeColor(GOLD); c.line(46,43,549,43)
 c.setFont('Helvetica',8); c.setFillColor(MUT); c.drawString(46,29,'ALPORT HOSPITALITY SOLUTIONS  |  USER GUIDE  |  NOVEMBER 2026  |  REVISION 3'); c.drawRightString(549,29,str(n))

def item(num,title,body,y):
 c.setFillColor(G); c.circle(58,y-10,11,fill=1,stroke=0); c.setFillColor(white); c.setFont('Helvetica-Bold',10); c.drawCentredString(58,y-14,str(num))
 return text('<b>'+title+'</b><br/>'+body,79,y,465)-14

def shot(name,y=635):
 im=Image.open(ROOT/'screenshots'/f'{name}.jpg')
 w=min(503,250*im.width/im.height); h=im.height/im.width*w
 c.drawImage(str(ROOT/'screenshots'/f'{name}.jpg'),46,y-h,width=w,height=h)
 return y-h-14

def flow(labels,y):
 gap=14; w=(503-gap*(len(labels)-1))/len(labels)
 for i,label in enumerate(labels):
  x=46+i*(w+gap); c.setFillColor(G); c.roundRect(x,y-54,w,54,8,fill=1,stroke=0)
  text(label,x+10,y-10,w-20,10,white)
  if i<len(labels)-1:
   c.setStrokeColor(G); c.line(x+w+2,y-27,x+w+gap-2,y-27); c.line(x+w+gap-6,y-23,x+w+gap-2,y-27); c.line(x+w+gap-6,y-31,x+w+gap-2,y-27)

page(1,'Your venue, at your fingertips','A practical guide to finding your way around the Alport website.')
y=text('Welcome to your Alport workspace',46,623,size=22)
y=text('Use this guide to find the right section, understand the screen and follow the main day-to-day workflows. Keep a downloaded copy for onboarding and quick reference.',46,y-18,size=13)
flow(['Sign in','Check your venue','Choose a task','Review & save'],y-38)
y=y-130
for n,title,body in [(2,'Find your way around','Desktop navigation, mobile navigation and a map of the workspace.'),(3,'Bookings & guests','Choose a date, use the diary and add a booking.'),(4,'Stock & purchasing','Understand stock levels and complete a stocktake.'),(5,'People & rota','Find staff tools and plan a shift.'),(6,'EHO Compliance','Find daily checks, records and audit evidence.'),(7,'Finance, setup & help','Choose the right finance screen and troubleshoot common issues.'),(8,'Team chat','Choose a site room, read its history and share an update.')]:
 y=text(f'<b>{n} / {title}</b> &nbsp; {body}',46,y,503,11)-16
text('Refreshed 1 November 2026 after the navigation and Team chat updates. Screenshots show empty workspace records. Your data and available actions may differ. Revision 3.',46,115,503,9,MUT)
c.showPage()
page(2,'Find your way around','Start by checking the organisation and venue shown beside the Alport logo.')
y=shot('navigation')
y=item(1,'Top links and venue header','The branded Workspace tools links open the User guide, Returns & credits, People & attendance, Sales & forecast inputs and Face ID & passkeys. Check your organisation and venue beside the logo.',y)
y=item(2,'Sidebar and screen tabs','Choose a section from the green sidebar. The main panel changes to that task. Tabs such as Diary, Service and Reports switch views within a section.',y)
y=item(3,'On a phone','Use the bottom navigation and tap More for additional sections. Open the Workspace tools dropdown for the top links, including User guide. Team chat and My private assistant appear in the floating tools bar.',y)
flow(['Operations<br/>Bookings, stock,<br/>purchasing, menu','People<br/>Staff, shifts,<br/>payroll','Finance<br/>Invoices, payments,<br/>budgets'],y-3)
text('<b>Also in the sidebar:</b> EHO Compliance; Audit log; Sites; Users; Settings. Some actions require a manager or account administrator.',46,y-74,503,10)
c.showPage()
page(3,'Bookings & guests','From the sidebar, select Bookings & Guests. On mobile, open More > Bookings.')
y=shot('bookings')
y=item(1,'Pick the service date','Use the date field above the summary cards, then review the Diary. Covers, bookings and peak figures relate to the selected date.',y)
y=item(2,'Add a booking','Select + New booking. Fill in the guest and reservation details, review the date, time and party size, then save. Check that the booking appears in the diary.',y)
y=item(3,'Move between views','Use Service for the service view, Guests for guest records and Reports for booking summaries. Settings contains reservation configuration.',y)
text('<b>Empty diary?</b> Check the selected date and venue, then use Refresh. An empty date does not mean the website has no bookings.',46,y,503,10)
c.showPage()
page(4,'Stock & purchasing','Use Stock for quantities and counts; Purchasing for supplier orders.')
y=shot('stock')
y=item(1,'Read your stock list','Review On hand, Par, Unit cost and Status. Par is the target level used to flag low stock. Use + Add stock item to create a stock record.',y)
y=item(2,'Count stock','Select Start stocktake and confirm. Enter each counted quantity. Use Continue on an open stocktake if you need to return to it.',y)
y=item(3,'Complete the count','Review all counts, then select Complete stocktake. Confirm only when ready: completion replaces live on-hand quantities with counted quantities.',y)
flow(['Review levels','Count & check','Complete stocktake'],y-5)
text('<b>Related tools:</b> Stock usage & suggested orders helps review demand. Purchasing handles orders. Returns & credits records supplier returns and credits. Menu & Costing is the place for recipes and dish costs.',46,y-80,503,11)
c.showPage()
page(5,'People & rota','Use Staff for employee records and Shifts & Rota for planned working hours.')
y=shot('rota')
y=item(1,'Choose the right people tool','Staff contains employee records. Staff access & time off manages portal access and requests. People & attendance is available in the top links.',y)
y=item(2,'Plan a shift','Open Shifts & Rota and select + Add shift. Choose the employee and date, enter start and finish times, and record the break in minutes. Select Save shift.',y)
y=item(3,'Review the rota','Check the date, employee, hours, rate and estimated cost in the table. Review booking demand alongside the rota when it is available.',y)
flow(['Staff records','Shifts & breaks','Review labour','Payroll'],y-6)
text('<b>New starters:</b> Add &amp; invite employee assigns six modules: Food Hygiene, Allergen Safety, HACCP/SFBB, Workplace Safety, Fire Safety and Equality &amp; Discrimination. Each has five questions and an 80% pass mark. Check completion in Staff &amp; Onboarding; rota creation does not enforce completion.',46,y-80,503,9)
c.showPage()
page(6,'EHO Compliance','Select EHO Compliance from the sidebar, or from More on mobile.')
y=shot('compliance')
y=item(1,'Open the required check','Use Opening checks or Closing checks for the daily checklist. Record temperature opens the temperature entry form.',y)
y=item(2,'Record and follow up','Use Add compliance record for supporting activity. Review the record, status and any corrective action before saving. Check recent activity to confirm it was recorded.',y)
y=item(3,'Review evidence','Use Check photos & history for daily evidence, Pest control visits for pest records, and Extraction fan cleaning for extraction records. Open EHO audit mode gathers audit evidence. Use Complete 4-week review for the periodic review.',y)
flow(['Record checks','Review actions','Gather evidence'],y-4)
text('Follow your venue\'s procedures and your local authority\'s instructions. This guide explains website navigation; the website stores supporting records and evidence.',46,y-75,503,10,MUT)
c.showPage()
page(7,'Finance, setup & help','Use the menu label that matches the task you need to complete.')
y=630
for title,body in [
 ('Sales / Finance & EBITDA','Use Sales for sales records and Finance & EBITDA to review the venue\'s financial summary. Check the period and site before interpreting figures.'),
 ('Invoices / Payments / Payments & Tronc','Invoices contains supplier invoice records. Payments is the workspace payment section. Payments & Tronc opens the separate money tools. Review the relevant records and permissions before completing a financial action.'),
 ('Budgets / Audit log','Budgets compares plans with actual figures. Audit log helps trace recorded activity. Use it when you need to investigate what changed.'),
 ('Setup & Till connections','Open this link for venue setup and till connections. Sales & forecast inputs contains planning inputs. Ask an administrator if setup or access needs changing.'),
 ('Users / Settings / Face ID & passkeys','Use Users for account access, Settings for workspace configuration and Face ID & passkeys for supported sign-in options. Availability depends on your role.'),
 ('Help & cookies','Open Help & cookies in the lower corner for the available help options. When reporting an issue, include the section, selected date and what you expected to happen.')]:
  y=text('<b>'+title+'</b><br/>'+body,46,y,503,11)-18
text('<b>Quick fixes</b><br/>Missing records: check the venue, date or period, then Refresh.<br/>Permission message: ask your manager or administrator.<br/>On mobile: open More for sections or Workspace tools for extra links.<br/>Finished on a shared device: use Sign out.',46,y,503,10)
c.showPage()
page(8,'Team chat','Open Team chat from the floating workspace tools or your staff portal.')
y=shot('chat')
y=item(1,'Choose the site','Select your venue in the Site dropdown. Each site has its own conversation and history. Check the room name before posting.',y)
y=item(2,'Read and refresh','Messages refresh automatically. Use Refresh if needed and Load older messages when it is available. New members can read earlier messages.',y)
y=item(3,'Write a team update','Type in the Message to field, then select Send message. Enter adds a new line; it does not send. Messages can be up to 2,000 characters.',y)
flow(['Choose your site','Read the room','Write & send'],y-2)
text('<b>Shared with your team:</b> Everyone with access to that site chat, including the business Owner, can read its history. Use My private assistant for your separate assistant conversation.',46,y-77,503,11)
c.showPage(); c.save()
print(OUT)
