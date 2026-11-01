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
 c.setFont('Helvetica',8); c.setFillColor(MUT); c.drawString(46,29,'ALPORT HOSPITALITY SOLUTIONS  |  USER GUIDE  |  NOVEMBER 2026  |  REVISION 4'); c.drawRightString(549,29,str(n))

def item(num,title,body,y):
 c.setFillColor(G); c.circle(58,y-10,11,fill=1,stroke=0); c.setFillColor(white); c.setFont('Helvetica-Bold',10); c.drawCentredString(58,y-14,str(num))
 return text('<b>'+title+'</b><br/>'+body,79,y,465)-14

def shot(name,y=635,maxh=250):
 im=Image.open(ROOT/'screenshots'/f'{name}.jpg')
 w=min(503,maxh*im.width/im.height); h=im.height/im.width*w
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
for n,title,body in [('2','Find your way around','Navigation and the workspace map.'),('3-6','Everyday operations','Bookings, stock, people, rota and EHO checks.'),('7-8','Finance and team chat','Find tools, mention colleagues and assign tasks.'),('9-10','Today at my site','Daily actions, finance, setup and owner oversight.'),('11-13','Pub records','Cashing up, licensing, incidents and cellar controls.'),('14-15','Planning and allergens','Manual cash forecast and recipe-linked reviews.'),('16-17','Repairs and handover','Equipment, photos and actions for the next shift.'),('18','Product tour and help','Explore examples and find the right support.')]:
 y=text(f'<b>{n} / {title}</b> &nbsp; {body}',46,y,503,11)-16
text('Refreshed 1 November 2026 after the navigation and Team chat updates. Screenshots show empty workspace records. Your data and available actions may differ. Revision 4.',46,115,503,9,MUT)
c.showPage()
page(2,'Find your way around','Start by checking the organisation and venue shown beside the Alport logo.')
y=shot('navigation')
y=item(1,'Top links and venue header','The branded Workspace tools links open the User guide, Returns & credits, People & attendance, Sales & forecast inputs and Face ID & passkeys. Check your organisation and venue beside the logo.',y)
y=item(2,'Sidebar and screen tabs','Choose a section from the green sidebar. The main panel changes to that task. Tabs such as Diary, Service and Reports switch views within a section.',y)
y=item(3,'On a phone','Use the bottom navigation and tap More for additional sections. Open the Workspace tools dropdown for the top links, including User guide. Team chat and My private assistant appear in the floating tools bar.',y)
flow(['Operations<br/>Bookings, stock,<br/>purchasing, menu','People<br/>Staff, shifts,<br/>payroll','Finance<br/>Invoices, payments,<br/>budgets'],y-3)
text('<b>New starting points:</b> Today at my site brings daily actions together; Pub operations opens the new registers. EHO Compliance, Audit log, Sites, Users and Settings remain available.',46,y-74,503,10)
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
page(8,'Team chat & tasks','Open Team chat from Today at my site, the workspace tools or your staff portal.')
y=shot('chat')
y=item(1,'Choose the site and check unread messages','Each site has its own shared room. Read the unread count, use Refresh if needed and Load older messages for earlier history.',y)
y=item(2,'Send an update and mention colleagues','Write up to 2,000 characters. Select names in Mention people to flag unread mentions, then select Send message. Enter adds a new line. Mentions stay inside Alport.',y)
y=item(3,'Turn a message into an action','Select Create task on a message. Check the task title, choose a Responsible person and Deadline, then Create task. Review progress and history in Team tasks; available changes depend on your permissions.',y)
flow(['Choose site','Read & mention','Assign & follow up'],y-4)
text('Everyone with room access, including new members, can read its history and tasks. The list shows up to 200 tasks, open first. Use My private assistant for a separate assistant conversation.',46,y-77,503,10)
c.showPage()

page(9,'Today at my site','Open Today at my site from the workspace. Always check the selected site first.')
y=shot('today')
y=item(1,'Start with the right context','Choose Site and Financial month, then Refresh. Staff see their permitted sites and personal actions. Full site managers see operational and financial summaries.',y)
y=item(2,'Follow the actions that need attention','Review overdue checks, opening checks, bookings, scheduled staff, low stock and team tasks. Pub-operation issues appear when relevant. Select Review or a Quick action to open the working screen.',y)
y=item(3,'Check service coverage yourself','The staffing flag identifies bookings with no scheduled staff. A positive staff count does not confirm enough cover; review the rota against the expected service.',y)
flow(['Choose site','Review exceptions','Open task','Follow up'],y-8)
text('Use Pub operations for cashing up, licence records, cellar checks, forecasting, allergens, repairs and handovers. The Section menu shows the areas available to your role.',46,y-88,503,10)
c.showPage()

page(10,'Figures, setup & access','Use the lower sections of Today at my site to understand the records behind the headline numbers.')
y=625
y=item(1,'Read the financial comparison','Compare the selected month with its full-month budget and the previous full month. Actual minus budget shows the difference; a positive cost variance means higher costs. A part-month is not like-for-like with a full month.',y)
y=item(2,'Check the basis and missing records','The management estimate uses gross sales, recorded stock usage at current costs, scheduled wages and gross overheads. Employer costs are excluded. Read missing-data warnings and Where these figures come from before interpreting profit.',y)
y=item(3,'Complete the setup checklist','Get your site ready links to site details, staff records, activated access, opening stock, the monthly budget and sales. Records present means data exists, not that it has been checked for accuracy.',y)
y=item(4,'Review ownership and accountability','Owners can choose Compare my sites. Use Show access and recent activity to inspect permissions and recorded changes. Chat-only access does not grant access to site financial figures.',y)
flow(['Check period','Check sources','Compare','Investigate'],y-12)
text('Source dates show the latest dated records in the selected month, not the last edit time. These figures are management estimates, not statutory accounts, bank balances or cash profit. Use the manual cash forecast for planned cash timing.',46,y-94,503,11)
c.showPage()

page(11,'Daily cashing up','Pub operations > choose Site > Section: Daily cashing up. Manager access is required.')
y=shot('cashup',maxh=205)
y=item(1,'Identify the count','Enter the record date and a distinct Till / session reference. Enter opening float, cash sales before refunds, cash refunds, paid-outs and counted cash including the float.',y)
y=item(2,'Compare cash and cards','Enter card sales before card refunds, card refunds and net card receipts. Discounts are already reflected in sales: do not subtract them again. Save record and review the calculated cash and card variances.',y)
y=item(3,'Explain and sign off','Add an explanation for differences. Edit while unsigned, then use Manager sign-off only when checked: this locks the figures. History shows recorded changes.',y)
flow(['Count','Compare','Explain','Sign off'],y-7)
text('Expected cash = opening float + cash sales - cash refunds - paid-outs. A cash-up is a management record; saving it does not post sales into the sales ledger or reconcile your bank automatically.',46,y-88,503,10)
c.showPage()

page(12,'Licences, refusals & incidents','Choose the relevant Section within Pub operations and check the site before recording anything.')
y=shot('licence',maxh=205)
y=item(1,'Maintain the premises licence register','Managers enter the jurisdiction, issuing authority, licence reference and responsible person. Copy actual permitted hours, exceptions and conditions. Record the next review or fee deadline and its source.',y)
y=item(2,'Record a refused sale','Choose Refused sale. Enter the date, time, reason, facts and action taken; add an internal reference if useful. Record only necessary facts, without identity-document numbers.',y)
y=item(3,'Record and follow up an incident','Choose Incident log. Record the category, time, immediate action and any reference or follow-up date. Staff can see their own reports; managers can review site reports. Use History for recorded changes.',y)
flow(['Select register','Record facts','Save','Follow up'],y-6)
text('Use emergency procedures when needed: saving an incident does not contact emergency services. This register records your venue’s actual requirements; it does not certify licensing compliance.',46,y-87,503,10)
c.showPage()

page(13,'Cellar checks & stock loss','Pub operations contains separate sections for cleaning, temperatures, keg yield and stock wastage.')
y=shot('cellar',maxh=205)
y=item(1,'Record cleaning and temperature checks','Beer line cleaning records the line, method, next due date and notes. Cellar temperature records the location, reading, venue target range and corrective action. Follow product and equipment instructions.',y)
y=item(2,'Track cask or keg yield','Select the stock item and record the batch, container and serving volumes, sold servings, waste and remaining volume. Review expected servings, unaccounted volume and sold yield. Enter the product-based review/use-by date.',y)
y=item(3,'Post actual stock wastage carefully','Cellar stock wastage deducts the saved quantity from on-hand stock. Use the stock item’s own unit and state the reason. This record cannot be edited; corrections require an authorised stock adjustment.',y)
flow(['Check cellar','Record yield','Review loss','Post wastage'],y-6)
text('A keg-change/yield record does not itself deduct stock. Its waste volume supports the yield calculation; the separate wastage entry changes the stock balance. Avoid posting the same loss twice.',46,y-87,503,10)
c.showPage()

page(14,'Plan the next 13 weeks','Pub operations > Cash-flow entries. This section is available to managers.')
y=shot('forecast')
y=item(1,'Set the forecast starting point','Enter Forecast start, Opening available cash and the balance source/assumptions, then Save forecast settings. The forecast runs for 13 consecutive seven-day periods from that date.',y)
y=item(2,'Add dated income and outgoings','Use New record for each expected cash movement. Choose Income or Outgoing, category, amount and status. State the timing and amount assumptions, then Save record. Cancelled entries are excluded.',y)
y=item(3,'Review projected balances','Check each week’s opening, income, outgoings and closing figures. Expand its planned entries to investigate. Negative closing balances are highlighted. Refresh your estimates when plans change.',y)
flow(['Opening cash','Dated entries','Weekly balance','Review'],y-6)
text('Only manually entered planning entries are included. Supplier bills, wages and tax provisions are not imported automatically. Wage estimates are not approved payroll; the app does not calculate tax liabilities or move money.',46,y-87,503,10)
c.showPage()

page(15,'Recipe-linked allergen reviews','Pub operations > Allergen matrix. Add recipes and stock ingredients before linking them.')
y=shot('allergens')
y=item(1,'Review current supplier specifications','Managers open Review ingredient supplier specifications. Record each of the 14 allergen states, the checked date and source, including specific cereal/nut details. Save ingredient review.',y)
y=item(2,'Link every recipe component','For each dish, choose the matching stock ingredient for each component and select Link ingredient. Unlinked components and missing or changed specifications need review.',y)
y=item(3,'Record the recipe review','Check current ingredients, preparation and cross-contact controls. Add review notes and select Record recipe review. Recipe or supplier changes can invalidate earlier reviews; resolve Unknown and Review required states.',y)
flow(['Supplier source','Ingredient link','Recipe review','Recheck changes'],y-6)
text('Not declared in reviewed source is not an allergen-free guarantee. The matrix does not replace PPDS labelling or a conversation with the customer. Follow the official guidance linked on the screen and your trained team’s procedure.',46,y-87,503,10)
c.showPage()

page(16,'Equipment repairs & servicing','Pub operations > Equipment repairs and servicing.')
y=shot('repair',maxh=205)
y=item(1,'Record the job','Identify the equipment and a consistent asset/service reference. Enter urgency, fault, responsible person, contractor reference if relevant, estimated/actual cost and due date. Save record.',y)
y=item(2,'Attach evidence after saving','On the saved record, choose a JPEG, PNG or WebP photo up to 8 MB and select Upload photo. Show photos provides download links. Only upload evidence appropriate for the venue record.',y)
y=item(3,'Track and complete the repair','Use Edit to change Open to Booked or Done as work progresses. Done requires a completion date and notes. History preserves changes. Create a new dated record with the same asset reference for the next service.',y)
flow(['Report','Assign','Attach evidence','Complete'],y-6)
text('Available edits depend on your role and who created the record. Critical faults still require the venue’s immediate safety and escalation procedures; recording a job does not contact a contractor.',46,y-87,503,10)
c.showPage()

page(17,'Hand over the next shift','Pub operations > Manager handover. Manager access is required.')
y=shot('handover',maxh=205)
y=item(1,'Summarise the service','Enter the date and shift reference. Cover staffing, large bookings, unavailable products, unresolved complaints, equipment faults and the next-shift summary. Keep complaint details to the minimum necessary.',y)
y=item(2,'Save the handover','Check the correct site and select Save record. Review the saved handover and use History when you need to see changes.',y)
y=item(3,'Turn follow-up into a team task','Open Assign an action in Team chat on the saved record. Write the action (up to 200 characters), choose a Responsible person and Due date, then Share action and create task. Follow progress in the site room.',y)
flow(['Summarise','Save handover','Assign action','Track task'],y-6)
text('Only the selected action is posted to Team chat, not the whole handover. Everyone with room access can read that action and its task; review the wording before sharing.',46,y-87,503,10)
c.showPage()

page(18,'Explore examples & get help','The public product tour is available at alporthospitality.co.uk/product-tour.')
y=shot('tour')
y=item(1,'Use the three walkthrough steps','Select Start the day, Work with your team or Understand performance to see the interface in context. The tour uses fictional example venues, people and records.',y)
y=item(2,'Review pricing and integrations','Pricing shows the current configured venue rate and a venue-count calculator. Read the minimum term, VAT and integration notes. The calculator does not place an order or alter an existing agreement.',y)
y=item(3,'Get help from the right screen','Use Report a problem from Today at my site or Pub operations. Include the site, screen, action and what you expected, without passwords or unnecessary personal data. The guide download remains available after sign-in.',y)
flow(['Explore examples','Open workspace','Read guide','Get support'],y-6)
text('If a page is empty, first check Site, date/period, access and whether records have been entered. Refresh before retrying. After a save error, check the record list before submitting again to avoid accidental duplication.',46,y-87,503,10)
c.showPage()
c.save()
