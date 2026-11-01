# Alport-
2.0

### Site team chat

Open **Team chat** from the workspace, staff portal or shared tools links, then
choose a site explicitly. Each site has its own shared message history. Active
staff assignments grant access only to their assigned sites; the business Owner
can open their own organisation's sites. Other role names (including Admin and
Manager) do not grant chat access automatically. Owners can use **Manage site
chat access** in a room to grant other existing users access to that site.
Staff-linked accounts must instead be managed through Staff access.

All authorised room members can read earlier messages, including new members.
These are shared team messages, separate from the private AI assistant. This
version supports plain text up to 2,000 characters, not attachments or direct
messages. History stays in PostgreSQL, without automatic expiry or in-app
message deletion; include it in the business's retention/deletion process.
There are no push/email notifications. Open visible rooms refresh about every
five seconds. Switching rooms keeps each unsent draft separately in tab memory.

The module adds `alport_chat_messages`, `alport_chat_members` and
`alport_chat_access_events` plus indexes on startup. It does not alter existing
business records. Back up PostgreSQL before deployment as usual. No persistent
web-service disk, external chat provider or new Python dependency is needed.
Send retries are idempotent; a database-backed limit allows 20 messages per user
per minute across sites. API requests recheck active accounts, subscriptions and
site membership, and writes require CSRF tokens. Chat grants affect only chat.

Run checks with `python -m unittest discover -s tests -p 'test_team_chat.py'`.
The integration tests use SQLite adaptation; production PostgreSQL locking
still needs deployment validation. Rollback: revert the feature commit and
redeploy; the added tables can remain without affecting older code.


## Daily site workspace and team tasks

`/today` provides site-scoped operational summaries, gross-basis management estimates versus budgets and the previous full month, source-date and missing-data warnings, setup progress, and access/activity views. Owners can compare their active sites. Other users need an activated staff assignment; full-profile managers see site financials, while staff see personal shortcuts. Chat grants do not grant financial access.

Team chat supports explicit mentions, per-user unread positions, and one task per source message with an assignee, deadline and status. Task changes are versioned and audited; only the creator, assignee or Owner can change status. Everyone currently authorised for the room can read tasks and history. There are no email or push notifications for chat mentions. Task lists show the first 200 entries (open tasks first).

New tables are created on startup under the existing advisory lock: `alport_chat_reads`, `alport_chat_mentions`, `alport_chat_tasks`, and `alport_chat_task_events`. No additional disk or service is required. Validate on PostgreSQL staging before production; SQLite integration fixtures do not exercise PostgreSQL locking.

Financial estimates preserve the existing gross sales/expense basis. Stock usage uses current item costs; labour uses scheduled shifts (including overnight work), excludes cancelled/declined shifts and excludes employer costs. Full-month comparisons are labelled; missing records are not proof of zero activity. Staffing warnings identify bookings with no scheduled staff, not every possible shortage.

Run `python -m unittest discover -s tests`. Browser-check Owner and staff views, chat mentions/task creation and mobile layout at 390px. The existing user guide, support form and evidence capture are reused.

### Pub operations

Open **Pub operations** from **Today at my site**. Every API request rechecks active business/site access. Manager-only sections require Owner access or an active full site assignment with Manager, General Manager or Admin role. Chat-only membership does not grant operational access.

- Cashing up: separate till/session references, exact decimal cash/card variances, discounts already included in sales, explanation before signing a variance, immutable manager sign-off and audited edits. No automatic sales-ledger posting.
- Licensing: venue jurisdiction, actual licence conditions/hours, responsible person and entered deadlines. Refusal/incident reports are private to their author and site managers. No inferred legal permissions or automatic compliance certification.
- Cellar: line-cleaning dates, venue-set temperature targets, cask/keg yield and stock-item wastage. Wastage deducts once in the stock item’s unit, with an atomic stock movement; signed cash-ups and wastage are immutable.
- Cash flow: opening available cash and 13 seven-day periods with dated income/outgoings. Cancelled entries are excluded. Wage and tax provision entries are manual assumptions, not approved actual payroll or statutory calculations. No payroll payments or bank transfers.
- Allergens: explicitly link each recipe component to an active stock ingredient and review all 14 states against a supplier specification. Unlinked, unknown or changed supplier/ingredient data requires review; recipe/mapping changes invalidate a prior recipe review. “Not declared in reviewed source” is not an allergen-free claim. Record specific cereal/nut details and preparation/cross-contact checks.
- Repairs: urgency, assignee, contractor, costs, due date, completion notes and up to five private JPEG/PNG/WebP photos (8 MB each). Completion requires a valid date and notes. Recurring services use separate dated records.
- Manager handovers: service notes plus selected actions shared into the existing site Team chat with assignee, due date, mention and task history. Retried action submissions do not create duplicate tasks.

Alerts feed into the existing Today dashboard. Records paginate in batches of 50. Additive tables are created under PostgreSQL advisory transaction locks; writes use existing database connections and audited transactions. No external services, new credentials or production dependency changes. There is no automatic record expiry or customer-facing deletion in this version.

Validation: `python -m unittest discover -s tests` covers scope, CSRF, exact cash calculations, immutable sign-off, atomic/retried wastage, forecast week boundaries, supplier/recipe invalidation, photo permissions and handover tasks. SQLite fixtures validate application behavior; they do not replace PostgreSQL execution testing. Local PostgreSQL execution was attempted but blocked by the desktop sandbox’s shared-memory restriction.

Guide impact: the Alport website manager / existing guide worker owns the post-deployment update to `/user-guide`, screenshots and `private_docs/Alport-Website-User-Guide.pdf`. Capture the deployed revision and add the seven workflows above; do not label preview screenshots as live. Guide authentication must remain unchanged.
## Public product walkthrough

`/product-tour` is an example-only public walkthrough with screenshots captured from local fixtures, not production customer data. Pricing uses `BASE_PRICE_PER_SITE`, `VAT_RATE` and `CONTRACT_MONTHS`; it does not change checkout or existing agreements. The optional calculator supports 1–100 venues and includes the minimum-term total. All walkthrough panels and base pricing remain available without JavaScript. Screenshots should be refreshed when the underlying workspace changes. This change depends on the daily workspace/team-task feature.
