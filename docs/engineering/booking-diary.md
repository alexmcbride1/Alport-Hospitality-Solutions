# Daily booking diary

The daily diary replaces reservation cards with a sortable table. Search and status, service, table-area and VIP filters operate on the loaded day. Previous/Today/Next controls clear stale records immediately; late responses cannot replace a newer date. Rendered pages contain at most 100 rows, with a disclosed 5,000-booking daily fetch ceiling. Hidden service cards are built only when that tab is opened.

Booking detail edits preserve the existing creation, service, reporting and settings flows. Saves use optimistic timestamps, validate table ownership/capacity, arrival cover limits and overlapping table allocations including overnight reservations and turnaround. Status actions, VIP changes and private comments do not send guest messages or charge deposits. Explicit existing review/retention actions remain separate. Retention recipients are now restricted to the active site's completed-visit history and existing consent/opt-out conditions.

## Access and data boundaries

All internal `/api/bookings` routes require an active business subscription and an active management assignment for the current site. Owner can manage their active sites; Admin, General Manager and Manager require a full site assignment. Staff and Finance do not receive booking access. Writes additionally require the session CSRF token. Requests from a page opened at another site fail safely until reloaded. Public booking and guest self-service routes are unchanged.

VIP metadata follows the existing stable guest ID within its organisation. Matching names are not merged. VIP is independent of marketing consent and can be removed. Concurrent VIP changes require the latest version.

Private comments are append-only, attributed and dated, stored separately by organisation/site/guest. Saving retries are idempotent. The profile shows the latest 100 comments; older comments remain stored. The diary receives only a count. Comment text is not added to booking records, public reservation responses, email templates, assistant context or default exports. Managers should enter factual, relevant comments; the organisation must administer retention/deletion of this additional stored data through its existing data-management process.

Guest search queries the active site's entire booking history with 50-result cursor pages. Name matching ignores case; phone matching ignores punctuation and normalises complete UK 0044/+44 forms to local 0. Visits count Seated and Completed bookings only. Booking/contact details belong to the existing guest record; this feature does not merge or repair historical duplicates.

## Validation and limits

- 52 Flask integration tests, including manager/site/tenant isolation, revoked site access, subscription blocks, CSRF, phone lookup, pagination, VIP conflicts, private comment isolation/retries, stale booking edits, table overlaps/overnight capacity and retention scope.
- 46 existing assistant regression tests and rendered template/JavaScript syntax checks.
- PostgreSQL parser validation for schema, 18 literal queries and the assembled phone-search query. Integration execution uses a SQLite adapter; it does not prove PostgreSQL concurrent transactions.
- `node tests/booking_diary_ui.cjs` checks filtering, sorting, services and dates. Local 5,000-record/100-run measurement: median 0.59 ms, p95 0.90 ms for pure filtering/sorting, excluding network and DOM. This is not a competitor comparison or a production latency guarantee.
- Browser preview uses 120 fictional reservations. Checked diary filters, detail mouse/keyboard access, booking note save, VIP set/remove, attributed private comment persistence, formatted phone lookup, empty/error states and date-response protection. Responsive checks cover desktop, tablet and mobile; the dense table scrolls horizontally.

Booking edits share the existing social allocation guard's site lock. Full cross-writer concurrency protection still depends on other writers participating in that guard; this change does not install or replace database triggers.

## Release and guide handoff

Guide impact: Bookings/Guests screenshots, daily diary controls, role access, VIP and site-private comments need a deployed-UI refresh. Hand off the actual merged/live revision to the Alport website manager and existing guide worker. Do not label preview screenshots as production or claim the guide is refreshed until their update is verified.
