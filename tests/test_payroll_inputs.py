import unittest
from datetime import datetime, timezone
from alport_payroll_inputs import prepare_hours, period_bounds

class PayrollInputsTests(unittest.TestCase):
    def row(self, **changes):
        return dict(dict(id=1, employee_id=3, name='Test', in_at='2026-10-01T09:00:00+00:00', out_at='2026-10-01T17:00:00+00:00', break_minutes=30, status='Approved', approved_by=9, approved_at='2026-10-02', pay_type='Hourly', pay_rate='12.50', version=2), **changes)
    def run_rows(self, *rows):
        return prepare_hours(rows, '2026-10-01', '2026-10-31', datetime(2026,11,1,tzinfo=timezone.utc))
    def test_breaks_and_gross(self):
        r=self.run_rows(self.row());self.assertEqual(r['gross_pence'],9375);self.assertFalse(r['ready_for_payment']);self.assertEqual(r['lines'][0]['hours'],'7.5000')
    def test_unapproved_excluded(self):
        r=self.run_rows(self.row(status='Pending'));self.assertEqual(r['gross_pence'],0);self.assertEqual(len(r['issues']),1)
    def test_open_flagged(self):
        self.assertTrue(self.run_rows(self.row(out_at=None))['issues'])
    def test_rejected_ignored(self):
        self.assertEqual(self.run_rows(self.row(status='Rejected'))['issues'],[])
    def test_overlap_blocks(self):
        self.assertTrue(self.run_rows(self.row(),self.row(id=2))['issues'])
    def test_salary_blocks(self):
        self.assertTrue(self.run_rows(self.row(pay_type='Salary'))['issues'])
    def test_invalid_rate(self):
        for rate in ['NaN','Infinity','-1','0','12.345']:
            self.assertTrue(self.run_rows(self.row(pay_rate=rate))['issues'])
    def test_invalid_break(self):
        for value in [-1,480,500,'NaN','1.5']:
            self.assertTrue(self.run_rows(self.row(break_minutes=value))['issues'])
    def test_boundary_blocks(self):
        self.assertTrue(self.run_rows(self.row(in_at='2026-09-30T22:00:00Z',out_at='2026-10-01T02:00:00Z'))['issues'])
    def test_naive_time_blocks(self):
        self.assertTrue(self.run_rows(self.row(in_at='2026-10-01T09:00:00'))['issues'])
    def test_dst_actual_elapsed(self):
        r=self.run_rows(self.row(in_at='2026-10-25T00:00:00+01:00',out_at='2026-10-25T04:00:00+00:00',break_minutes=0))
        self.assertEqual(r['lines'][0]['hours'],'5.0000')
    def test_period_validation(self):
        for a,b in [('2026-10-02','2026-10-01'),('bad','bad'),('2026-01-01','2026-12-01')]:
            with self.assertRaises(ValueError):period_bounds(a,b)
    def test_approval_metadata(self):
        self.assertTrue(self.run_rows(self.row(approved_by=None))['issues'])
    def test_outside_period(self):
        self.assertEqual(self.run_rows(self.row(in_at='2026-09-01T00:00:00Z',out_at='2026-09-01T08:00:00Z'))['lines'],[])
if __name__=='__main__':unittest.main()
