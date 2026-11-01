"""Alport PDF renderer, adapted from the supplied report export template.
No additional runtime dependencies; documents are generated in memory.
"""
import csv
import io
from datetime import datetime, timezone

# ------------------------------------------------------------------------------------------------
# Branding — the only part most sites need to change. Colours are (red, green, blue) from 0 to 1.
# ------------------------------------------------------------------------------------------------
BRAND = {
    'name': 'ALPORT',
    'tagline': 'HOSPITALITY SOLUTIONS',
    'header_colour': (15/255, 33/255, 26/255),
    'accent_colour': (32/255, 91/255, 73/255),
    'background': (244/255, 247/255, 245/255),
    'positive_colour': (29/255, 90/255, 71/255),
    'negative_colour': (163/255, 66/255, 58/255),
    'footer_lines': [
        'Alport Hospitality Solutions | alporthospitality.co.uk',
        'Confidential management report - based on records entered in Alport.',
    ],
}

PAGE_W, PAGE_H = 595, 842     # A4 in PDF points
MARGIN = 36


_FONT_WIDTHS = [[0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.278, 0.278, 0.355, 0.556, 0.556, 0.889, 0.667, 0.191, 0.333, 0.333, 0.389, 0.584, 0.278, 0.333, 0.278, 0.278, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.278, 0.278, 0.584, 0.584, 0.584, 0.556, 1.015, 0.667, 0.667, 0.722, 0.722, 0.667, 0.611, 0.778, 0.722, 0.278, 0.5, 0.667, 0.556, 0.833, 0.722, 0.778, 0.667, 0.778, 0.722, 0.667, 0.611, 0.722, 0.667, 0.944, 0.667, 0.667, 0.611, 0.278, 0.278, 0.278, 0.469, 0.556, 0.333, 0.556, 0.556, 0.5, 0.556, 0.556, 0.278, 0.556, 0.556, 0.222, 0.222, 0.5, 0.222, 0.833, 0.556, 0.556, 0.556, 0.556, 0.333, 0.5, 0.278, 0.556, 0.5, 0.722, 0.5, 0.5, 0.5, 0.334, 0.26, 0.334, 0.584, 0.761, 0.556, 0.761, 0.222, 0.556, 0.333, 1.0, 0.556, 0.556, 0.333, 1.0, 0.667, 0.333, 1.0, 0.761, 0.611, 0.761, 0.761, 0.222, 0.222, 0.333, 0.333, 0.35, 0.556, 1.0, 0.333, 1.0, 0.5, 0.333, 0.944, 0.761, 0.5, 0.667, 0.278, 0.333, 0.556, 0.556, 0.556, 0.556, 0.26, 0.556, 0.333, 0.737, 0.37, 0.556, 0.584, 0.333, 0.737, 0.333, 0.4, 0.584, 0.333, 0.333, 0.333, 0.556, 0.537, 0.278, 0.333, 0.333, 0.365, 0.556, 0.834, 0.834, 0.834, 0.611, 0.667, 0.667, 0.667, 0.667, 0.667, 0.667, 1.0, 0.722, 0.667, 0.667, 0.667, 0.667, 0.278, 0.278, 0.278, 0.278, 0.722, 0.722, 0.778, 0.778, 0.778, 0.778, 0.778, 0.584, 0.778, 0.722, 0.722, 0.722, 0.722, 0.667, 0.667, 0.611, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.889, 0.5, 0.556, 0.556, 0.556, 0.556, 0.278, 0.278, 0.278, 0.278, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.584, 0.611, 0.556, 0.556, 0.556, 0.556, 0.5, 0.556, 0.5], [0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.761, 0.278, 0.333, 0.474, 0.556, 0.556, 0.889, 0.722, 0.238, 0.333, 0.333, 0.389, 0.584, 0.278, 0.333, 0.278, 0.278, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.333, 0.333, 0.584, 0.584, 0.584, 0.611, 0.975, 0.722, 0.722, 0.722, 0.722, 0.667, 0.611, 0.778, 0.722, 0.278, 0.556, 0.722, 0.611, 0.833, 0.722, 0.778, 0.667, 0.778, 0.722, 0.667, 0.611, 0.722, 0.667, 0.944, 0.667, 0.667, 0.611, 0.333, 0.278, 0.333, 0.584, 0.556, 0.333, 0.556, 0.611, 0.556, 0.611, 0.556, 0.333, 0.611, 0.611, 0.278, 0.278, 0.556, 0.278, 0.889, 0.611, 0.611, 0.611, 0.611, 0.389, 0.556, 0.333, 0.611, 0.556, 0.778, 0.556, 0.556, 0.5, 0.389, 0.28, 0.389, 0.584, 0.761, 0.556, 0.761, 0.278, 0.556, 0.5, 1.0, 0.556, 0.556, 0.333, 1.0, 0.667, 0.333, 1.0, 0.761, 0.611, 0.761, 0.761, 0.278, 0.278, 0.5, 0.5, 0.35, 0.556, 1.0, 0.333, 1.0, 0.556, 0.333, 0.944, 0.761, 0.5, 0.667, 0.278, 0.333, 0.556, 0.556, 0.556, 0.556, 0.28, 0.556, 0.333, 0.737, 0.37, 0.556, 0.584, 0.333, 0.737, 0.333, 0.4, 0.584, 0.333, 0.333, 0.333, 0.611, 0.556, 0.278, 0.333, 0.333, 0.365, 0.556, 0.834, 0.834, 0.834, 0.611, 0.722, 0.722, 0.722, 0.722, 0.722, 0.722, 1.0, 0.722, 0.667, 0.667, 0.667, 0.667, 0.278, 0.278, 0.278, 0.278, 0.722, 0.722, 0.778, 0.778, 0.778, 0.778, 0.778, 0.584, 0.778, 0.722, 0.722, 0.722, 0.722, 0.667, 0.667, 0.611, 0.556, 0.556, 0.556, 0.556, 0.556, 0.556, 0.889, 0.556, 0.556, 0.556, 0.556, 0.556, 0.278, 0.278, 0.278, 0.278, 0.611, 0.611, 0.611, 0.611, 0.611, 0.611, 0.611, 0.584, 0.611, 0.611, 0.611, 0.611, 0.611, 0.556, 0.611, 0.556]]

def text_width(value, bold=False):
    return sum(_FONT_WIDTHS[int(bold)][c] for c in str(value).encode('cp1252', 'replace'))


# ------------------------------------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------------------------------------
def build_csv(headers, rows):
    """headers: list of column names; rows: list of lists (or dicts keyed by header). Returns CSV text."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(h, '') for h in headers] if isinstance(row, dict) else row)
    return out.getvalue()


# ------------------------------------------------------------------------------------------------
# PDF
# ------------------------------------------------------------------------------------------------
def _esc(value):
    return str(value).replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


class _Page:
    """One A4 page of PDF drawing commands. Helvetica only (F1 regular, F2 bold)."""

    def __init__(self, brand):
        self.c, self.brand = [], brand

    def rect(self, x, y, w, h, fill, stroke=None, width=1):
        r, g, b = fill
        self.c.append(f'{r} {g} {b} rg {x} {y} {w} {h} re f')
        if stroke:
            sr, sg, sb = stroke
            self.c.append(f'{sr} {sg} {sb} RG {width} w {x} {y} {w} {h} re S')

    def line(self, x1, y1, x2, y2, colour=(.75, .75, .75), width=1):
        r, g, b = colour
        self.c.append(f'{r} {g} {b} RG {width} w {x1} {y1} m {x2} {y2} l S')

    def text(self, x, y, size, value, bold=False, colour=(.1, .1, .1), max_chars=150, width=None):
        r, g, b = colour
        value = str(value).replace('\n', ' ')
        if width is not None:
            size = min(size, width / max(1, text_width(value, bold)))
        else:
            value = value[:max_chars]
        safe = _esc(value)
        self.c.append(f'{r} {g} {b} rg BT /{"F2" if bold else "F1"} {size} Tf {x} {y} Td ({safe}) Tj ET')

    def wrap(self, x, y, size, value, width_chars=92, leading=12, bold=False, colour=(.25, .25, .25), max_lines=8):
        """Word-wrapped paragraph; returns the y position below the last line."""
        words, lines, cur = str(value or '').split(), [], ''
        for w in words:
            trial = (cur + ' ' + w).strip()
            if len(trial) > width_chars and cur:
                lines.append(cur); cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        for i, part in enumerate(lines[:max_lines]):
            self.text(x, y - i * leading, size, part, bold, colour, width_chars + 5)
        return y - len(lines[:max_lines]) * leading

    def stream(self):
        return '\n'.join(self.c).encode('cp1252', 'replace')


def _logo(page):
    # Same circle, hills, waves and sun as static/alport-logo.svg.
    page.c.append('q 0.49 0 0 -0.49 36 827 cm 0.72 0.86 0.77 RG 2 w')
    k = 46 * 0.55228475
    page.c.append(f'96 50 m 96 {50+k} {50+k} 96 50 96 c '
                  f'{50-k} 96 4 {50+k} 4 50 c 4 {50-k} {50-k} 4 50 4 c '
                  f'{50+k} 4 96 {50-k} 96 50 c S')
    page.c.append('18 62 m 30 55 38 54 47 56 c 57 59 67 63 84 54 c '
                  '18 69 m 31 63 42 62 51 65 c 62 68 72 66 84 61 c '
                  '25 55 m 38 39 l 47 49 l 57 35 l 76 56 l S')
    page.c.append('0.72 0.86 0.77 rg 73 31 m 73 33.21 71.21 35 69 35 c '
                  '66.79 35 65 33.21 65 31 c 65 28.79 66.79 27 69 27 c '
                  '71.21 27 73 28.79 73 31 c f Q')


def _frame(page, report, page_no, page_count, section):
    b = report.get('brand') or BRAND
    page.rect(0, 0, PAGE_W, PAGE_H, b['background'])
    page.rect(0, 752, PAGE_W, 90, b['header_colour'])
    page.rect(0, 746, PAGE_W, 6, b['accent_colour'])
    _logo(page)
    page.text(96, 802, 25, b['name'], True, (1, 1, 1), 30)
    page.text(97, 785, 8, b['tagline'], True, (.85, .9, .87), 60)
    page.text(365, 810, 8, report.get('document_number', ''), False, (.82, .87, .84), 40)
    page.text(365, 792, 8, f'{section} | Page {page_no}/{page_count}', False, (.82, .87, .84), 50)
    page.rect(0, 0, PAGE_W, 42, b['header_colour'])
    for i, ln in enumerate((b.get('footer_lines') or [])[:2]):
        page.text(MARGIN, 25 - i * 13, 7, ln, False, (.72, .79, .75), 130)


def _chart_box(page, x, y, w, h, title, subtitle=''):
    page.rect(x, y, w, h, (1, 1, 1), (.84, .86, .89), .7)
    page.text(x + 12, y + h - 20, 9.5, title, True, max_chars=60)
    if subtitle:
        page.text(x + 12, y + h - 34, 6.8, subtitle, False, (.45, .48, .52), 70)
    for n in range(1, 4):
        gy = y + 18 + n * (h - 64) / 4
        page.line(x + 12, gy, x + w - 12, gy, (.9, .91, .92), .5)


def _line_chart(page, brand, x, y, w, h, spec):
    _chart_box(page, x, y, w, h, spec.get('title', ''), spec.get('subtitle', ''))
    pts = spec.get('points') or []
    if not pts:
        page.text(x + w / 2 - 70, y + h / 2, 9, 'No data for this period', True, (.45, .48, .52)); return
    vals = [v for _, v in pts]; lo, hi = min(vals + [0]), max(vals + [0]); spread = (hi - lo) or 1
    x0, y0, cw, chh = x + 14, y + 26, w - 28, h - 90
    xy = [(x0 + (i / max(1, len(pts) - 1)) * cw, y0 + ((v - lo) / spread) * chh) for i, (_, v) in enumerate(pts)]
    zero = y0 + ((0 - lo) / spread) * chh
    page.line(x0, zero, x0 + cw, zero, (.63, .65, .68), .8)
    if len(xy) == 1:
        page.line(x0, xy[0][1], x0 + cw, xy[0][1], brand['accent_colour'], 2.3)
    for a, b2 in zip(xy, xy[1:]):
        page.line(a[0], a[1], b2[0], b2[1], brand['accent_colour'], 2.3)
    page.text(x0, y + 12, 6.5, pts[0][0], False, (.45, .48, .52), 20)
    page.text(x0 + cw - 50, y + 12, 6.5, pts[-1][0], False, (.45, .48, .52), 20)
    page.text(x + w - 120, y + h - 20, 7, f'High {hi:,.2f}  Low {lo:,.2f}', False, (.45, .48, .52), 40)


def _bar_chart(page, brand, x, y, w, h, spec):
    _chart_box(page, x, y, w, h, spec.get('title', ''), spec.get('subtitle', ''))
    pts = spec.get('points') or []
    if not pts:
        page.text(x + w / 2 - 70, y + h / 2, 9, 'No data for this period', True, (.45, .48, .52)); return
    vals = [v for _, v in pts]; mx = max([abs(v) for v in vals] + [1])
    x0, cw, chh = x + 14, w - 28, h - 90; zero = y + 26 + chh / 2
    page.line(x0, zero, x0 + cw, zero, (.62, .64, .67), .8)
    bw = max(2, min(16, cw / max(1, len(pts)) * .62))
    for i, v in enumerate(vals):
        bx = x0 + (i + .5) * cw / len(pts) - bw / 2; bh = abs(v) / mx * (chh / 2)
        page.rect(bx, zero if v >= 0 else zero - bh, bw, max(.5, bh), brand['positive_colour'] if v >= 0 else brand['negative_colour'])
    page.text(x0, y + 12, 6.5, pts[0][0], False, (.45, .48, .52), 20)
    page.text(x0 + cw - 50, y + 12, 6.5, pts[-1][0], False, (.45, .48, .52), 20)


def _table_pages(report, table):
    """Split one table into page-sized chunks: (title, columns, rows) per page."""
    rows, per_page = table.get('rows') or [], 26
    chunks = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    return [(table.get('title', '') + (f' (continued)' if i else ''), table['columns'], ch, table.get('note', '')) for i, ch in enumerate(chunks)]


def build_pdf(report):
    """Build a multi-page A4 report and return the PDF bytes.

    Page 1: title, summary cards (`metrics`), intro paragraph.
    Page 2: line chart (`line_chart`) and bar chart (`bar_chart`), if given.
    Then one page per 26 rows of each table in `tables`, then a `notes` page if given.
    """
    brand = report.get('brand') or BRAND
    tables = [p for t in (report.get('tables') or []) for p in _table_pages(report, t)]
    has_charts = bool(report.get('line_chart') or report.get('bar_chart'))
    page_count = 1 + int(has_charts) + len(tables) + int(bool(report.get('notes')))
    pages, no = [], 1

    # Page 1 — summary
    p = _Page(brand); _frame(p, report, no, page_count, 'Summary')
    p.text(MARGIN, 716, 21, report.get('title', 'Report'), True, max_chars=45)
    p.text(MARGIN, 696, 9, report.get('subtitle', ''), False, (.35, .38, .42), width=523)
    headline = report.get('headline')        # optional big number: {'label':..., 'value':..., 'note':..., 'positive': True/False}
    top = 670
    if headline:
        col = brand['positive_colour'] if headline.get('positive', True) else brand['negative_colour']
        p.rect(MARGIN, 582, 523, 88, (1, 1, 1), (.82, .84, .87), .8); p.rect(MARGIN, 582, 5, 88, col)
        p.text(54, 646, 7, str(headline.get('label', '')).upper(), True, (.38, .42, .46), 60)
        p.text(54, 611, 27, headline.get('value', ''), True, col, width=487)
        p.text(54, 594, 7, headline.get('note', ''), False, (.45, .48, .52), 100)
        top = 560
    for i, (label, value) in enumerate((report.get('metrics') or [])[:12]):      # up to 12 cards, 4 per row
        x = MARGIN + (i % 4) * 131; y = top - 70 - (i // 4) * 73
        p.rect(x, y, 120, 60, (1, 1, 1), (.85, .87, .89), .7); p.rect(x, y, 3, 60, brand['accent_colour'])
        p.text(x + 11, y + 40, 6.5, str(label).upper(), True, (.42, .45, .49), 24)
        p.text(x + 11, y + 16, 12, value, True, (.1, .12, .15), width=98)
    rows_used = (len((report.get('metrics') or [])[:12]) + 3) // 4
    y = top - 90 - rows_used * 73
    for heading, body in (report.get('summary_paragraphs') or []):
        if y < 90: break
        p.text(MARGIN, y, 11, heading, True, max_chars=80); y = p.wrap(MARGIN, y - 20, 8.5, body, colour=(.30, .33, .37), max_lines=6) - 18
    pages.append(p.stream()); no += 1

    # Page 2 — charts
    if has_charts:
        p = _Page(brand); _frame(p, report, no, page_count, 'Charts')
        p.text(MARGIN, 716, 18, report.get('charts_title', 'Charts'), True, max_chars=60)
        if report.get('line_chart'): _line_chart(p, brand, MARGIN, 425, 523, 260, report['line_chart'])
        if report.get('bar_chart'): _bar_chart(p, brand, MARGIN, 120, 523, 270, report['bar_chart'])
        pages.append(p.stream()); no += 1

    # Table pages — columns: [(header, width_in_points, 'left'|'right'), ...]
    for title, columns, rows, note in tables:
        p = _Page(brand); _frame(p, report, no, page_count, 'Details')
        p.text(MARGIN, 716, 18, title, True, max_chars=60)
        if note: p.text(MARGIN, 698, 8, note, False, (.4, .43, .47), 110)
        y = 670; p.rect(MARGIN, y - 6, 523, 22, brand['header_colour'])
        xs, x = [], MARGIN + 8
        for col in columns:
            xs.append(x); p.text(x, y, 8, col[0], True, (1, 1, 1), width=col[1]-18); x += col[1]
        y -= 24
        for r_i, row in enumerate(rows):
            if r_i % 2 == 0: p.rect(MARGIN, y - 6, 523, 21, (1, 1, 1))
            for col, cx, val in zip(columns, xs, row):
                s = str(val)
                size = min(8.5, (col[1] - 18) / max(1, text_width(s)))
                if len(col) > 2 and col[2] == 'right':
                    cx += col[1] - 18 - text_width(s) * size
                p.text(cx, y, size, s, False, (.12, .14, .17), width=col[1]-18)
            y -= 22
        if not rows: p.text(MARGIN + 8, y, 8, 'No rows for this period.', False, (.45, .48, .52))
        pages.append(p.stream()); no += 1

    # Notes page — [(heading, paragraph), ...]
    if report.get('notes'):
        p = _Page(brand); _frame(p, report, no, page_count, 'Notes')
        p.text(MARGIN, 716, 18, report.get('notes_title', 'Notes'), True, max_chars=60); y = 680
        for heading, body in report['notes']:
            if y < 90: break
            p.text(MARGIN, y, 10, heading, True, max_chars=80); y = p.wrap(MARGIN, y - 19, 8.5, body, max_lines=8) - 22
        pages.append(p.stream())

    return _assemble(pages)


def _assemble(streams):
    n = len(streams); f1 = 3 + 2 * n; f2 = f1 + 1
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>',
            f'<< /Type /Pages /Kids [{" ".join(f"{3 + 2 * i} 0 R" for i in range(n))}] /Count {n} >>'.encode()]
    for i, s in enumerate(streams):
        objs.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {4 + 2 * i} 0 R >>'.encode())
        objs.append(b'<< /Length ' + str(len(s)).encode() + b' >>\nstream\n' + s + b'\nendstream')
    objs += [b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>']
    pdf = bytearray(b'%PDF-1.4\n'); offs = [0]
    for i, o in enumerate(objs, 1):
        offs.append(len(pdf)); pdf += f'{i} 0 obj\n'.encode() + o + b'\nendobj\n'
    xref = len(pdf); pdf += f'xref\n0 {len(objs) + 1}\n'.encode() + b'0000000000 65535 f \n'
    for o in offs[1:]: pdf += f'{o:010d} 00000 n \n'.encode()
    pdf += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    return bytes(pdf)


def simple_pdf(title, lines):
    """Quick one-page PDF: a title plus up to 58 lines of plain text."""
    p = _Page(BRAND); p.text(48, 790, 16, title, True, max_chars=80)
    for i, ln in enumerate(lines[:58]): p.text(48, 766 - i * 13, 9, ln, max_chars=125)
    return _assemble([p.stream()])


# ------------------------------------------------------------------------------------------------
# Optional Flask helpers
# ------------------------------------------------------------------------------------------------
def pdf_response(pdf_bytes, filename='report.pdf'):
    from flask import Response
    return Response(pdf_bytes, mimetype='application/pdf', headers={'Content-Disposition': f'attachment; filename={filename}'})


def csv_response(headers, rows, filename='report.csv'):
    from flask import Response
    return Response(build_csv(headers, rows), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})

