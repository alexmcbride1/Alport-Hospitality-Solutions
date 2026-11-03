"""Read-only attendance estimates. Never a tax calculation or payment instruction."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

LONDON = ZoneInfo('Europe/London')


def period_bounds(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if not 0 <= (last - first).days <= 92:
            raise ValueError()
        return (datetime.combine(first, time.min, LONDON).astimezone(timezone.utc),
                datetime.combine(last + timedelta(days=1), time.min, LONDON).astimezone(timezone.utc))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Choose a valid payroll period of no more than 93 days.')


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Attendance must include a time zone.')
    return result.astimezone(timezone.utc)


def prepare_hours(rows, start, end, now=None):
    lower, upper = period_bounds(start, end)
    now = now or datetime.now(timezone.utc)
    employees, issues, intervals, seen = {}, [], {}, set()
    for r in rows:
        if r.get('status') == 'Rejected':
            continue
        label = f"{r.get('name', 'Employee')} · attendance #{r['id']}"
        try:
            began = instant(r['in_at'])
            ended = instant(r['out_at']) if r.get('out_at') else None
            if began >= upper or (ended and ended <= lower):
                continue
            if r['id'] in seen:
                raise ValueError('Duplicate attendance record.')
            seen.add(r['id'])
            if r.get('status') != 'Approved' or not ended or not r.get('approved_by') or not r.get('approved_at'):
                raise ValueError('Finish and approve this attendance record first.')
            if began < lower or ended > upper:
                raise ValueError('Shift crosses this payroll period; resolve its allocation before payroll.')
            seconds = Decimal(str((ended - began).total_seconds()))
            if seconds <= 0 or seconds > 36 * 3600 or ended > now:
                raise ValueError('Check attendance dates and duration.')
            breaks = Decimal(str(r.get('break_minutes', 0)))
            if not breaks.is_finite() or breaks < 0 or breaks != breaks.to_integral_value() or breaks * 60 >= seconds:
                raise ValueError('Check unpaid break minutes.')
            if str(r.get('pay_type', '')).lower() != 'hourly':
                raise ValueError('Salary or other pay arrangement needs payroll-provider calculation.')
            rate = Decimal(str(r.get('pay_rate')))
            if not rate.is_finite() or rate <= 0 or rate > 100000 or rate != rate.quantize(Decimal('.01')):
                raise ValueError('Set a valid positive hourly rate with at most two decimal places.')
            eid = r['employee_id']
            spans = intervals.setdefault(eid, [])
            if any(began < b and ended > a for a, b in spans):
                raise ValueError('Overlapping approved attendance; resolve before payroll.')
            spans.append((began, ended))
            entry = employees.setdefault(eid, {'employee_id': eid, 'name': r['name'], 'seconds': Decimal(0),
                                               'rate': rate, 'attendance': []})
            if rate != entry['rate']:
                raise ValueError('Inconsistent hourly rates; review before payroll.')
            entry['seconds'] += seconds - breaks * 60
            entry['attendance'].append({'id': r['id'], 'version': r.get('version')})
        except (ValueError, TypeError, InvalidOperation) as exc:
            issues.append(label + ': ' + (str(exc) if isinstance(exc, ValueError) else 'Invalid attendance or pay details.'))
    lines = []
    for e in employees.values():
        hours = e.pop('seconds') / Decimal(3600)
        rate = e.pop('rate')
        e.update(hours=str(hours.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)),
                 hourly_rate=str(rate), gross_pence=int((hours * rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
        lines.append(e)
    return {'period_start': start, 'period_end': end, 'lines': lines, 'issues': issues,
            'gross_pence': sum(e['gross_pence'] for e in lines), 'ready_for_payment': False,
            'basis': 'Current hourly rates; approved actual attendance; unpaid breaks deducted. Historical rate changes, overtime, holiday pay, tips and statutory pay require separate review.'}
