"""Run: python -m unittest discover -s tests -p 'test_finance_exports.py'.
Uses Flask, SQLite and the real finance/staff functions; no production services.
"""
import ast
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
import unittest

from flask import Flask, session
from alport_finance_reports import register_finance_reports, finance_report
from alport_report_pdf import build_pdf
from alport_staff import register_staff

ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self, cursor): self.cursor = cursor
    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


class Connection:
    def __init__(self, db): self.db = db
    def execute(self, sql, args=()):
        if 'pg_advisory' in sql or 'DROP CONSTRAINT IF EXISTS' in sql:
            sql, args = 'SELECT 1', ()
        sql = sql.replace('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY')
        sql = sql.replace('ADD COLUMN IF NOT EXISTS', 'ADD COLUMN').replace('%s', '?')
        return Cursor(self.db.execute(sql, args))
    def __enter__(self): return self
    def __exit__(self, error, *args):
        self.db.rollback() if error else self.db.commit()


class FinanceExports(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('GREATEST', -1, max)
        self.addCleanup(self.db.close)
        source = (ROOT / 'app.py').read_text()
        for table in ('organisations', 'users', 'sites', 'employees', 'shifts', 'stock_items',
                      'stock_movements', 'sales', 'expenses'):
            ddl = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r'\([\s\S]*?\n\);', source)[0]
            self.db.executescript(ddl.replace('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY'))
        self.db.execute("INSERT INTO organisations VALUES(1,'Example Hospitality','Pub','GBP',1,'today')")
        self.db.execute("INSERT INTO organisations VALUES(2,'OTHER COMPANY','Pub','GBP',1,'today')")
        for sid, oid, name in ((1, 1, 'Riverside Venue'), (2, 1, 'Other venue'), (3, 2, 'OTHER TENANT')):
            self.db.execute('INSERT INTO sites(id,organisation_id,name,active,created_at) VALUES(?,?,?,1,?)', (sid, oid, name, 'today'))
        for uid, oid, role in ((1, 1, 'Owner'), (2, 1, 'Staff'), (3, 1, 'Manager'), (4, 2, 'Owner')):
            self.db.execute('INSERT INTO users(id,organisation_id,name,email,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?,1,?)', (uid, oid, role, str(uid)+'@example.test', 'test-password-hash', role, 'today'))
        self.db.execute("INSERT INTO employees(id,organisation_id,site_id,name,pay_rate,active) VALUES(1,1,1,'Example Employee',10,1)")
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(1,1,'2026-10-02','Food',1000)")
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(1,1,'2026-10-03','Drink',500)")
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(1,2,'2026-10-02','Food',900000)")
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(2,3,'2026-10-02','Food',800000)")
        self.db.execute("INSERT INTO sales(organisation_id,site_id,sale_date,category,gross) VALUES(1,1,'2026-11-01','Food',700000)")
        self.db.execute("INSERT INTO stock_items(id,organisation_id,site_id,name,category,unit,unit_cost) VALUES(1,1,1,'Example stock','Food','each',2)")
        self.db.execute("INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,movement_type,quantity,created_at) VALUES(1,1,1,'usage',-100,'2026-10-03')")
        self.db.execute("INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes,status) VALUES(1,1,1,'2026-10-04','09:00','17:00',0,'Scheduled')")
        self.db.execute("INSERT INTO expenses(organisation_id,site_id,expense_date,category,description,gross) VALUES(1,1,'2026-10-04','Utilities','Power',120)")
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='tests-only')
        self.blocked = False
        def q(sql, args=(), one=False):
            result = Connection(self.db).execute(sql, args)
            return result.fetchone() if one else result.fetchall()
        self.env = dict(q=q, conn=lambda: Connection(self.db), session=session, date=date,
                        subscription_for=lambda oid: {}, subscription_blocks_access=lambda sub: self.blocked)
        # Load the existing calculation and tenancy functions without importing app.py,
        # whose module-level startup connects to PostgreSQL.
        names = {'user', 'org', 'current_site', 'month_bounds', 'finance_summary'}
        module = ast.parse(source)
        module.body = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(module, str(ROOT/'app.py'), 'exec'), self.env)
        register_finance_reports(self.app, self.env)
        register_staff(self.app, self.env)
        self.client = self.app.test_client()
        self.sign(1)

    def sign(self, uid, site=1):
        with self.client.session_transaction() as s:
            s.clear()
            s.update(user_id=uid, site_id=site, staff_site_id=site,
                     staff_auth_hash=hashlib.sha256(b'test-password-hash').hexdigest())

    def get(self, month='2026-10', site='1'):
        return self.client.get('/api/finance/export.pdf', query_string={'month': month, 'site_id': site})

    def test_export_matches_screen_and_is_private(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        self.assertTrue(response.data.startswith(b'%PDF-1.4'))
        self.assertIn(b'ALPORT', response.data)
        self.assertIn(b'Riverside Venue', response.data)
        self.assertIn(b'\xa31,500.00', response.data)
        self.assertIn(b'\xa31,100.00', response.data)  # 1500 - 200 - 80 - 120
        for value in (b'900,000', b'800,000', b'700,000', b'OTHER TENANT'):
            self.assertNotIn(value, response.data)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('Cookie', response.headers['Vary'])
        self.assertIn('alport-finance-site-1-2026-10.pdf', response.headers['Content-Disposition'])
        self.assertIn(b'/Count 3', response.data)

    def test_signed_out_or_disabled_user_denied(self):
        self.sign(999)
        self.assertEqual(self.get().status_code, 401)
        self.sign(1)
        self.db.execute('UPDATE users SET active=0 WHERE id=1')
        self.assertEqual(self.get().status_code, 401)

    def test_staff_denied(self):
        self.sign(2)
        self.assertEqual(self.get().status_code, 403)

    def test_restricted_manager_staff_profile_denied(self):
        self.db.execute("INSERT INTO alport_staff_access(organisation_id,site_id,employee_id,user_id,profile,activated_at,created_at,updated_at) VALUES(1,1,1,3,'orders','today','today','today')")
        self.sign(3)
        self.assertEqual(self.get().status_code, 403)

    def test_subscription_denied(self):
        self.blocked = True
        self.assertEqual(self.get().status_code, 403)

    def test_venue_cannot_be_overridden_by_url(self):
        self.assertEqual(self.get(site='2').status_code, 409)
        self.assertEqual(self.get(site='3').status_code, 409)
        self.assertEqual(self.get(site='').status_code, 409)

    def test_changed_site_in_another_tab(self):
        self.sign(1, 2)
        self.assertEqual(self.get().status_code, 409)
        response = self.get(site='2')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'900,000.00', response.data)
        self.assertNotIn(b'Riverside Venue', response.data)

    def test_foreign_session_site_cannot_leak_data(self):
        self.sign(1, 3)
        self.assertEqual(self.get(site='3').status_code, 409)
        self.assertNotIn(b'800,000', self.get().data)

    def test_inactive_sites_denied(self):
        self.db.execute('UPDATE sites SET active=0 WHERE organisation_id=1')
        self.assertEqual(self.get().status_code, 403)

    def test_invalid_months(self):
        for month in ('', '2026-13', '2026-00', '0000-01', '26-10', '2026-1', '../secret', '2026-10\r\nX:1'):
            with self.subTest(month=month):
                self.assertEqual(self.get(month).status_code, 400)

    def test_no_records_still_has_clear_reporting_basis(self):
        response = self.get('2025-02')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'\xa30.00', response.data)
        self.assertIn(b'incomplete totals', response.data)

    def test_negative_ebitda_and_no_numeric_clipping(self):
        self.db.execute('UPDATE expenses SET gross=123456789.12 WHERE organisation_id=1')
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'-\xa3123,455,569.12', response.data)

    def test_pdf_paginates_large_tables_and_escapes_text(self):
        report = dict(title='Test', tables=[dict(title='Details', columns=[('Name', 400), ('GBP',123,'right')], rows=[['A (B) \\ C', '£123.45'] for i in range(60)])])
        pdf = build_pdf(report)
        self.assertIn(b'/Count 4', pdf)
        self.assertIn(b'A \\(B\\) \\\\ C', pdf)
        self.assertIn(b'\xa3123.45', pdf)


if __name__ == '__main__':
    unittest.main()
