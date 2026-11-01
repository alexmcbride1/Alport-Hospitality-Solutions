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
