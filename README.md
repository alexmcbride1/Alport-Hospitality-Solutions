# Alport-
2.0

### Finance PDF reports

Open **Finance & EBITDA**, use the month arrows to select the reporting month,
then choose **Download financial PDF**. The Alport-branded report contains an
EBITDA summary, management P&L and calculation notes for the current venue.
It uses the same `finance_summary` calculation as the screen; it does not change
the existing accounting basis or reconcile statutory accounts. The current
finance screen and report use GBP without currency conversion.

Downloads require an active signed-in account with existing finance access, an
active venue and an unblocked subscription. Restricted staff cannot export.
If the venue changes in another tab, reload before downloading. PDFs are built
in memory and downloaded directly; no database migration, persistent disk,
provider account or additional Python dependency is required.

Checks: `python -m unittest discover -s tests -p 'test_finance_exports.py'`
and `node tests/test_finance_export_ui.cjs`.
