# Public booking links

Each site manager can open Bookings & Guests → Website booking link, add the pub contact number, check tables/sessions/rules, and enable online bookings. Copy the unique URL into any external website button, or copy the supplied HTML anchor. Links are disabled by default and stay stable when paused. This is a standalone guest page, not an iframe.

Guests provide name, email, phone, allergies/dietary needs and special occasion (explicit None answers accepted). The server checks bookable sessions, table capacity, sitting duration, turnaround, advance dates and covers per interval. Larger parties and existing deposit-required policies are directed to the pub. No new deposit settings, checkout or payment changes are included.

A successful public submission displays confirmation and a private manage-reservation link. The new public path does not send email; the page tells guests to save their confirmation. Existing manager-created booking emails retain their existing behavior. Required answers are also captured by manager creation, diary edits, guest amendments and social-booking intake. Legacy blank answers remain Not recorded until entered.

Public URLs resolve their own site and organisation; session site selections and submitted site IDs cannot redirect a reservation. Private comments and VIP flags are never selected into public pages. Forms use signed site/session-bound expiring tokens, idempotent submissions, a honeypot and a browser-session rate limit. That limit is not an IP or perimeter bot firewall. Confirmation pages use no-store and no-referrer headers. Guest amendments/cancellations require a session CSRF token.

Enabling a link installs/enables the existing PostgreSQL overlap guard for booking writers, using a shared per-site advisory lock. Public submissions refuse to book if the guard is absent or disabled. New schema is additive. Pausing a link stops new public bookings without changing existing reservations.

Validation: 61 application integration tests, 46 assistant regression checks, rendered-template and JavaScript syntax checks, and PostgreSQL parsing of schema/guard/70 literal statements. SQLite-backed integration fixtures stub PostgreSQL trigger installation; these are not a real PostgreSQL concurrency stress test. Local browser verification completed a fictional booking and checked manager link controls. No fictional production reservations were created.

Guide impact: new Website booking link controls, public guest form/confirmation, required guest answers and amended diary details. Refresh /user-guide and its PDF after the deployment is verified, using the existing website manager/guide workflow. Do not label preview screenshots as deployed UI.
