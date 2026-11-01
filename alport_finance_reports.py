"""Venue-scoped monthly finance PDFs using the existing management P&L."""
from datetime import date, datetime, timezone
import math
import re

from flask import Blueprint, jsonify, request
from alport_report_pdf import build_pdf, pdf_response


METHOD_NOTES = [
    ('Revenue', 'Uses the gross sales amounts recorded for the selected venue and month, '
     'including any VAT within those amounts. Food and drink use their recorded categories; '
     'other sales are the remaining revenue. This matches the Finance & EBITDA screen.'),
    ('Cost of sales', 'The absolute quantities of stock movements marked usage, waste, sale or cogs '
     'are multiplied by each stock item\'s current unit cost. Historical cost changes can therefore '
     'change a previously exported period. Supplier invoice totals are not used as cost of sales.'),
    ('Labour', 'Estimated from non-cancelled scheduled shifts, recorded breaks and employees\' current '
     'pay rates, using the existing finance calculation. This is not a reconciled payroll figure. '
     'It does not add employer costs or adjust overnight shifts across midnight.'),
    ('Overheads and EBITDA', 'Overheads are gross expense records excluding the Food, Drink and '
     'Labour categories. Gross profit equals revenue less cost of sales. EBITDA equals gross '
     'profit less labour and overheads. Margins divide the result by revenue; where revenue is '
     'zero, the screen and report show 0.0%.'),
    ('Report scope', 'This is a management snapshot of the records entered in Alport at export time, '
     'not reconciled statutory accounts or a cash-flow statement. Missing entries may produce '
     'zero or incomplete totals. No interest, tax, depreciation or amortisation reclassification '
     'is performed beyond the existing expense categories. Compare with your accounting records.'),
]


def finance_report(summary, organisation, site, generated=None):
    """Format, without recalculating, the same figures returned by /api/finance."""
    generated = generated or datetime.now(timezone.utc)
    month = date.fromisoformat(summary['month'] + '-01')
    currency = str(organisation.get('currency') or 'GBP').upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):
        currency = 'GBP'
    # The existing finance UI labels its figures GBP. Retain that basis here.
    # Do not imply currency conversion for older organisations with other settings.
    currency_note = ('The Finance & EBITDA screen reports in GBP; no currency conversion is applied.'
                     if currency != 'GBP' else 'Amounts in GBP. Figures match the Finance & EBITDA screen.')

    def amount(value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError('Finance records contain a non-finite amount. Review them before exporting.')
        return ('-' if value < 0 else '') + f'£{abs(value):,.2f}'

    lines = [('Revenue', summary['revenue']), ('Cost of sales', -summary['cogs']),
             ('Gross profit', summary['gross_profit']), ('Labour', -summary['labour']),
             ('Overheads', -summary['overheads']), ('EBITDA', summary['ebitda'])]
    rows = []
    for label, value in lines:
        ratio = float(value) / float(summary['revenue']) * 100 if summary['revenue'] else 0
        rows.append([label, amount(value), f'{ratio:,.1f}%'])
    return {
        'title': 'Finance & EBITDA',
        'subtitle': f'{month:%B %Y} | GBP | Generated {generated:%d %b %Y, %H:%M UTC}',
        'document_number': f'FIN-{site["id"]}-{month:%Y%m}',
        'headline': {'label': 'EBITDA', 'value': amount(summary['ebitda']),
                     'positive': summary['ebitda'] >= 0,
                     'note': f'{summary["ebitda_margin"]:,.1f}% EBITDA margin | Selected venue and month'},
        'metrics': [('Revenue', amount(summary['revenue'])),
                    ('Gross profit', amount(summary['gross_profit'])),
                    ('Cost of sales', amount(summary['cogs'])),
                    ('Labour', amount(summary['labour'])),
                    ('Overheads', amount(summary['overheads'])),
                    ('Gross margin', f'{summary["gross_margin"]:,.1f}%'),
                    ('Food sales', amount(summary['food'])),
                    ('Drink sales', amount(summary['drink']))],
        'summary_paragraphs': [('Business / venue', f'{organisation["name"]} / {site["name"]}'),
                               ('Reporting basis', currency_note + ' The report includes only this venue '
                                'and calendar month. Zero values do not confirm that all records are complete.')],
        'tables': [{'title': 'Management P&L',
                    'note': f'{month:%B %Y} | Costs are shown as deductions; margins are relative to revenue.',
                    'columns': [('Line', 261, 'left'), ('Actual (GBP)', 145, 'right'),
                                ('% of revenue', 117, 'right')], 'rows': rows}],
        'notes_title': 'How the figures are calculated',
        'notes': METHOD_NOTES,
    }


def register_finance_reports(app, env):
    bp = Blueprint('alport_finance_reports', __name__)

    @bp.after_request
    def private_download(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.vary.add('Cookie')
        return response

    @bp.get('/api/finance/export.pdf')
    def export_pdf():
        # The app-wide staff guard also runs for this blueprint (never allowlisted).
        u = env['user']()
        if not u:
            return jsonify(error='Sign in again to export your report.'), 401
        if u['role'] in ('Staff', 'User'):
            return jsonify(error='Finance access is required to export this report.'), 403
        if env['subscription_blocks_access'](env['subscription_for'](u['organisation_id'])):
            return jsonify(error='Business subscription needs attention.'), 403
        site, organisation = env['current_site'](), env['org']()
        if (not site or not organisation or site['organisation_id'] != u['organisation_id']
                or organisation['id'] != u['organisation_id'] or not site.get('active')):
            return jsonify(error='Select an active venue before exporting.'), 403
        # Treat the browser's venue as an expectation, never a data-selection override.
        if request.args.get('site_id') != str(site['id']):
            return jsonify(error='The selected venue changed. Reload this page before exporting.'), 409
        month = request.args.get('month', '')
        try:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month):
                raise ValueError()
            date.fromisoformat(month + '-01')
        except ValueError:
            return jsonify(error='Choose a valid report month (YYYY-MM).'), 400
        try:
            report = finance_report(env['finance_summary'](month), organisation, site)
            pdf = build_pdf(report)
        except ValueError as error:
            return jsonify(error=str(error)), 422
        return pdf_response(pdf, f'alport-finance-site-{site["id"]}-{month}.pdf')

    app.register_blueprint(bp)
