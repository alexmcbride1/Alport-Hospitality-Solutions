# Payroll preparation — setup and user guide

Payments & Tronc → Payroll → Prepare payroll from worked hours. Owners, Admins and Finance users choose an inclusive UK date range and review approved actual attendance for the selected site. Unpaid breaks are removed and elapsed time handles daylight-saving changes. Rates are CURRENT employee hourly rates, not historical snapshots. Totals round once per employee to pennies. Resolve all listed issues; totals are incomplete while issues remain.

This view is read-only. It does not reserve attendance, create a payroll run, calculate net pay, submit RTI or initiate payments. Repeating a preview never creates another payment. Existing final-figure import and bank workflows are unchanged. Do not copy these estimates into final payroll without provider calculation and review. Pension costs and wage forecasts elsewhere are not approved payroll.

## Required before embedded live payroll

1. Obtain a UK payroll API account and commercial access. Staffology is a candidate, not yet integrated or contracted: https://app.staffology.co.uk/api/docs/guides/intro/intro . Confirm provider suitability, pricing and access before implementation.
2. Each actual employer must have PAYE registration, relevant HMRC references/credentials, pension setup and payroll opening balances. Alport's own company registration does not register its customer employers. See https://www.gov.uk/register-employer and https://www.thepensionsregulator.gov.uk/en/employers .
3. Map employees across ALL sites of the same legal employer/PAYE employment. Validate tax codes, NI categories, starter/leaver and year-to-date data, student loans, pension assessment, statutory pay, salary/overtime/holiday pay, historical rates and approved tronc treatment. Site estimates must not produce duplicate tax/NI calculations for multi-site employees.
4. Implement and test provider authentication, employer mapping, immutable input snapshots, duplicate prevention/attendance claims, draft calculation, human approval, finalisation, payslips, RTI responses/corrections and pension outputs. None of these provider operations is implemented by this preparation view.
5. Obtain Yapily application access and confirm supported bank/bulk-payment capability. Configure credentials securely in Render, validate callback and payment status handling in sandbox, then conduct a controlled live acceptance process. Bank approval is required by the existing flow; unattended payment availability is not promised.
6. Match approved provider net pay to independently verified beneficiaries, authorise each batch, reconcile bank outcomes before recording paid. Never initiate payments from an estimate or before full reconciliation of uncertain submissions.

## Validation

Run `python -m unittest discover -s tests -p test_payroll_inputs.py` from the repository root. Tests cover breaks, approval requirements, overlap, invalid rates/breaks, salary exclusion, period boundaries, timezone validation and UK daylight-saving elapsed hours. This does not constitute live payroll or tax acceptance testing. Also test role/site isolation and realistic attendance against PostgreSQL before deployment.

No new environment variables or database migrations are required for this preview. Requires the existing people/attendance module. Include alport_payroll_inputs.py alongside alport_payments.py when releasing.
