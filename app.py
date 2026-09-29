import os
from datetime import date, datetime, timedelta
from functools import wraps
import hashlib
import hmac
import json
import time
import secrets
from html import escape

import psycopg
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
import base64


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. Add the PostgreSQL Internal Database URL "
        "to the Render environment variables."
    )


app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_IN_RENDER")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)


# Public privacy-policy identity. Set these in Render before public launch.
PRIVACY_LEGAL_NAME = os.environ.get("PRIVACY_LEGAL_NAME", "Alport Hospitality Solutions")
PRIVACY_TRADING_NAME = os.environ.get("PRIVACY_TRADING_NAME", "Alport Hospitality Solutions")
PRIVACY_EMAIL = os.environ.get("PRIVACY_EMAIL", "")
PRIVACY_ADDRESS = os.environ.get("PRIVACY_ADDRESS", "")
PRIVACY_ICO_NUMBER = os.environ.get("PRIVACY_ICO_NUMBER", "")
PRIVACY_EFFECTIVE_DATE = os.environ.get("PRIVACY_EFFECTIVE_DATE", "10 September 2026")


def conn():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def sql_placeholders(sql):
    return sql.replace("?", "%s")


def q(sql, args=(), one=False):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql_placeholders(sql), args)
            rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    statement = sql.strip()
    is_insert = statement.upper().startswith("INSERT")
    if is_insert and "RETURNING" not in statement.upper():
        statement = statement.rstrip().rstrip(";") + " RETURNING id"
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql_placeholders(statement), args)
            if is_insert:
                row = cur.fetchone()
                return row["id"] if row else None
    return None


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


# -----------------------------------------------------------------------------
# ALPORT SaaS PRICING / STRIPE BILLING
# -----------------------------------------------------------------------------
BASE_PRICE_PER_SITE = 500.0
INCLUDED_USERS_PER_SITE = 0  # users are unlimited and included
EXTRA_USER_PRICE = 0.0
VAT_RATE = 0.20
CONTRACT_MONTHS = 12
PAYMENT_GRACE_DAYS = 7


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None


def _contract_end(start_date):
    # Exactly 12 months for the current commercial model.
    try:
        return start_date.replace(year=start_date.year + 1)
    except ValueError:
        # 29 February -> 28 February in the following year.
        return start_date.replace(month=2, day=28, year=start_date.year + 1)


def subscription_for(organisation_id):
    return q("SELECT * FROM subscriptions WHERE organisation_id=?", (organisation_id,), True)


def pricing_for(organisation_id, preserve_commitment=True):
    sub = subscription_for(organisation_id)
    sites_row = q("SELECT COUNT(*) AS n FROM sites WHERE organisation_id=? AND active=1", (organisation_id,), True)
    users_row = q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND active=1", (organisation_id,), True)
    active_sites = max(1, int((sites_row or {}).get("n") or 0))
    active_users = max(0, int((users_row or {}).get("n") or 0))

    # Alport is Â£500 + VAT per active/contracted venue per month. Users are unlimited.
    base = BASE_PRICE_PER_SITE
    vat_rate = VAT_RATE
    committed_sites = int((sub or {}).get("contracted_sites") or active_sites)
    contract_end = _parse_iso_date((sub or {}).get("contract_end"))
    inside_term = bool(contract_end and date.today() < contract_end)
    billable_sites = max(active_sites, committed_sites) if preserve_commitment and inside_term else active_sites
    net = round(base * billable_sites, 2)
    vat = round(net * vat_rate, 2)
    gross = round(net + vat, 2)
    return {
        "active_sites": active_sites,
        "active_users": active_users,
        "billable_sites": billable_sites,
        "billable_users": active_users,
        "included_users": active_users,
        "extra_users": 0,
        "base_price": base,
        "extra_user_price": 0.0,
        "vat_rate": vat_rate,
        "net": net,
        "vat": vat,
        "gross": gross,
        "inside_term": inside_term,
        "unlimited_users": True,
    }


def stripe_configured():
    return bool(
        os.environ.get("STRIPE_SECRET_KEY")
        and os.environ.get("STRIPE_BASE_PRICE_ID")
    )


def stripe_request(method, path, data=None):
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("Stripe is not configured. Add STRIPE_SECRET_KEY in Render.")
    url = "https://api.stripe.com/v1" + path
    r = requests.request(method, url, auth=(key, ""), data=data or {}, timeout=20)
    try:
        payload = r.json()
    except Exception:
        payload = {"error": {"message": "Stripe returned an invalid response."}}
    if not r.ok:
        msg = ((payload.get("error") or {}).get("message") or "Stripe request failed")
        raise RuntimeError(msg)
    return payload


def verify_stripe_signature(payload, signature_header, secret, tolerance=300):
    if not payload or not signature_header or not secret:
        return False
    pieces = {}
    for part in signature_header.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            pieces.setdefault(k, []).append(v)
    try:
        timestamp = int((pieces.get("t") or ["0"])[0])
    except Exception:
        return False
    if abs(int(time.time()) - timestamp) > tolerance:
        return False
    signed = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in pieces.get("v1", []))


def record_stripe_payment(organisation_id, invoice):
    invoice_id = str(invoice.get("id") or "")
    if invoice_id and q("SELECT id FROM subscription_payments WHERE stripe_invoice_id=?", (invoice_id,), True):
        return
    total_pence = int(invoice.get("amount_paid") or invoice.get("total") or 0)
    gross = round(total_pence / 100.0, 2)
    # Stripe base price is VAT-inclusive gross (Â£600 per venue/month).
    # Commercial pricing is Â£500 + 20% VAT per venue/month.
    net = round(gross / (1 + VAT_RATE), 2) if gross else 0
    vat = round(gross - net, 2)
    paid_at = date.today().isoformat()
    execute(
        """INSERT INTO subscription_payments(
               organisation_id,amount,payment_date,status,method,reference,notes,created_at,
               stripe_invoice_id,vat_amount,net_amount)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (organisation_id, gross, paid_at, "Paid", "Stripe", invoice_id, "Automatic Stripe subscription payment", now(), invoice_id, vat, net),
    )


def activate_contract_from_checkout(organisation_id, checkout):
    p = pricing_for(organisation_id, preserve_commitment=False)
    start = date.today()
    end = _contract_end(start)
    customer_id = str(checkout.get("customer") or "")
    subscription_id = str(checkout.get("subscription") or "")
    session_id = str(checkout.get("id") or "")
    base_price_id = os.environ.get("STRIPE_BASE_PRICE_ID", "")
    extra_price_id = ""
    execute(
        """UPDATE subscriptions SET plan=?,monthly_price=?,status='Active',contract_start=?,contract_end=?,
           contract_months=?,contracted_sites=?,contracted_users=?,stripe_customer_id=?,stripe_subscription_id=?,
           stripe_checkout_session_id=?,stripe_base_price_id=?,stripe_extra_price_id=?,past_due_since='',
           last_payment_status='Paid',last_payment_at=? WHERE organisation_id=?""",
        (
            "Alport Hospitality Solutions", p["net"], start.isoformat(), end.isoformat(), CONTRACT_MONTHS,
            p["billable_sites"], p["billable_users"], customer_id, subscription_id, session_id,
            base_price_id, extra_price_id, now(), organisation_id,
        ),
    )
    company_event(
        "Contract activated",
        f"12-month minimum term Â· Â£{p['net']:.2f} + VAT/month Â· ends {end.isoformat()}",
        organisation_id,
    )


def subscription_blocks_access(sub):
    if not sub:
        return False
    status = str(sub.get("status") or "")
    if status in ("Suspended", "Cancelled"):
        return True
    if status == "Past due":
        since = _parse_iso_date(sub.get("past_due_since"))
        return bool(since and date.today() >= since + timedelta(days=PAYMENT_GRACE_DAYS))
    return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS organisations(
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'GBP',
    vat_registered INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS users(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(organisation_id,email),
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sales(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    sale_date TEXT NOT NULL,
    category TEXT NOT NULL,
    net DOUBLE PRECISION NOT NULL DEFAULT 0,
    vat DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross DOUBLE PRECISION NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'Manual',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS expenses(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    supplier TEXT DEFAULT '',
    net DOUBLE PRECISION NOT NULL DEFAULT 0,
    vat DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Posted',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS invoices(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    supplier TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    category TEXT NOT NULL,
    net DOUBLE PRECISION NOT NULL,
    vat DOUBLE PRECISION NOT NULL,
    gross DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'Awaiting approval',
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS payments(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    payment_type TEXT NOT NULL,
    payee TEXT NOT NULL,
    reference TEXT DEFAULT '',
    amount DOUBLE PRECISION NOT NULL,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending approval',
    method TEXT NOT NULL DEFAULT 'Bank transfer',
    source_id BIGINT,
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    external_reference TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS employees(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    department TEXT NOT NULL DEFAULT 'FOH',
    job_title TEXT DEFAULT '',
    pay_type TEXT NOT NULL DEFAULT 'Hourly',
    pay_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    ni_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    pension_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    holiday_allowance DOUBLE PRECISION NOT NULL DEFAULT 28,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shifts(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    employee_id BIGINT NOT NULL,
    shift_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    break_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Scheduled',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS payroll_runs(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    gross_pay DOUBLE PRECISION NOT NULL,
    employer_costs DOUBLE PRECISION NOT NULL DEFAULT 0,
    deductions DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pay DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',
    created_at TEXT NOT NULL,
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS stock_items(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    on_hand DOUBLE PRECISION NOT NULL DEFAULT 0,
    par_level DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    supplier TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stock_movements(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    stock_item_id BIGINT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    movement_type TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(stock_item_id) REFERENCES stock_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS suppliers(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    contact TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    payment_terms INTEGER NOT NULL DEFAULT 30,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS purchase_orders(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    supplier_id BIGINT NOT NULL,
    order_date TEXT NOT NULL,
    delivery_date TEXT,
    total DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Draft',
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS menu_items(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    menu_type TEXT NOT NULL DEFAULT 'Food',
    menu_section TEXT NOT NULL DEFAULT 'Mains',
    portion_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    portion_unit TEXT NOT NULL DEFAULT 'g',
    selling_price DOUBLE PRECISION NOT NULL,
    recipe_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS menu_components(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    menu_item_id BIGINT NOT NULL,
    component_name TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT 'g',
    component_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(menu_item_id) REFERENCES menu_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time TEXT DEFAULT '',
    end_time TEXT DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'General',
    notes TEXT DEFAULT '',
    all_day INTEGER NOT NULL DEFAULT 0,
    created_by BIGINT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS budgets(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    month TEXT NOT NULL,
    revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
    food_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    drink_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    labour DOUBLE PRECISION NOT NULL DEFAULT 0,
    overheads DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(site_id,month),
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subscriptions(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'Starter',
    monthly_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Trial',
    trial_end TEXT DEFAULT '',
    next_billing_date TEXT DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS subscription_payments(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Paid',
    method TEXT DEFAULT '',
    reference TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS booking_settings(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL UNIQUE,
    booking_interval INTEGER NOT NULL DEFAULT 15,
    default_duration INTEGER NOT NULL DEFAULT 120,
    turnaround_minutes INTEGER NOT NULL DEFAULT 15,
    max_party_size INTEGER NOT NULL DEFAULT 12,
    max_covers_per_interval INTEGER NOT NULL DEFAULT 24,
    min_notice_minutes INTEGER NOT NULL DEFAULT 60,
    advance_days INTEGER NOT NULL DEFAULT 90,
    review_delay_hours INTEGER NOT NULL DEFAULT 3,
    lapsed_guest_weeks INTEGER NOT NULL DEFAULT 6,
    confirmation_enabled INTEGER NOT NULL DEFAULT 1,
    reminder_enabled INTEGER NOT NULL DEFAULT 1,
    review_enabled INTEGER NOT NULL DEFAULT 1,
    retention_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS booking_sessions(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    day_of_week INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT 'Dinner',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS restaurant_tables(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    table_name TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT 'Main',
    min_capacity INTEGER NOT NULL DEFAULT 1,
    max_capacity INTEGER NOT NULL DEFAULT 2,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(site_id,table_name),
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS guests(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    marketing_consent INTEGER NOT NULL DEFAULT 0,
    marketing_opt_out_at TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS bookings(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    guest_id BIGINT,
    booking_date TEXT NOT NULL,
    booking_time TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 120,
    status TEXT NOT NULL DEFAULT 'Confirmed',
    source TEXT NOT NULL DEFAULT 'Manager',
    table_id BIGINT,
    guest_name TEXT NOT NULL,
    guest_email TEXT DEFAULT '',
    guest_phone TEXT DEFAULT '',
    special_requests TEXT DEFAULT '',
    dietary_requirements TEXT DEFAULT '',
    internal_notes TEXT DEFAULT '',
    marketing_consent INTEGER NOT NULL DEFAULT 0,
    confirmation_sent_at TEXT DEFAULT '',
    reminder_sent_at TEXT DEFAULT '',
    review_sent_at TEXT DEFAULT '',
    arrived_at TEXT DEFAULT '',
    seated_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    cancelled_at TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(guest_id) REFERENCES guests(id) ON DELETE SET NULL,
    FOREIGN KEY(table_id) REFERENCES restaurant_tables(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS guest_feedback(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    booking_id BIGINT NOT NULL,
    guest_id BIGINT,
    overall_rating INTEGER NOT NULL,
    food_rating INTEGER,
    service_rating INTEGER,
    atmosphere_rating INTEGER,
    value_rating INTEGER,
    comments TEXT DEFAULT '',
    manager_status TEXT NOT NULL DEFAULT 'New',
    created_at TEXT NOT NULL,
    FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
    FOREIGN KEY(guest_id) REFERENCES guests(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS guest_communications(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    guest_id BIGINT,
    booking_id BIGINT,
    communication_type TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_id TEXT DEFAULT '',
    sent_at TEXT NOT NULL,
    FOREIGN KEY(guest_id) REFERENCES guests(id) ON DELETE SET NULL,
    FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS company_events(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT,
    event_type TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS eho_daily_checks(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    check_date TEXT NOT NULL,
    check_type TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}',
    problems TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    signed_by BIGINT NOT NULL,
    signed_name TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(signed_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_temperature_checks(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    check_date TEXT NOT NULL,
    check_time TEXT NOT NULL,
    check_type TEXT NOT NULL,
    item TEXT NOT NULL,
    temperature DOUBLE PRECISION NOT NULL,
    target_min DOUBLE PRECISION,
    target_max DOUBLE PRECISION,
    result TEXT NOT NULL DEFAULT 'OK',
    method TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    recorded_by BIGINT NOT NULL,
    recorded_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(recorded_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_records(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    record_date TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Complete',
    details TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    due_date TEXT DEFAULT '',
    reference TEXT DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by BIGINT NOT NULL,
    created_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_by BIGINT,
    verified_at TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id),
    FOREIGN KEY(verified_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_four_week_reviews(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    review_date TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    persistent_problems TEXT DEFAULT '',
    changes_made TEXT DEFAULT '',
    safe_methods_current INTEGER NOT NULL DEFAULT 1,
    allergen_info_current INTEGER NOT NULL DEFAULT 1,
    training_current INTEGER NOT NULL DEFAULT 1,
    signed_by BIGINT NOT NULL,
    signed_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(signed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS employee_onboarding(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    employee_id BIGINT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'Invited',
    invited_at TEXT NOT NULL,
    personal_completed_at TEXT DEFAULT '',
    training_completed_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    date_of_birth_enc TEXT DEFAULT '',
    address_enc TEXT DEFAULT '',
    ni_number_enc TEXT DEFAULT '',
    bank_account_name_enc TEXT DEFAULT '',
    bank_sort_code_enc TEXT DEFAULT '',
    bank_account_number_enc TEXT DEFAULT '',
    emergency_name_enc TEXT DEFAULT '',
    emergency_phone_enc TEXT DEFAULT '',
    starter_declaration TEXT DEFAULT '',
    p45_status TEXT DEFAULT '',
    student_loan TEXT DEFAULT '',
    right_to_work_status TEXT NOT NULL DEFAULT 'Pending',
    right_to_work_checked_at TEXT DEFAULT '',
    right_to_work_checked_by BIGINT,
    right_to_work_reference TEXT DEFAULT '',
    privacy_ack_at TEXT DEFAULT '',
    revoked_at TEXT DEFAULT '',
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS employee_training(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    employee_id BIGINT NOT NULL,
    module_key TEXT NOT NULL,
    module_version TEXT NOT NULL DEFAULT '2026.1',
    status TEXT NOT NULL DEFAULT 'Not started',
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    assigned_at TEXT NOT NULL,
    completed_at TEXT DEFAULT '',
    refresher_due TEXT DEFAULT '',
    UNIQUE(employee_id,module_key,module_version),
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stocktakes(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    stocktake_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Open',
    started_by BIGINT,
    started_at TEXT NOT NULL,
    completed_by BIGINT,
    completed_at TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stocktake_lines(
    id BIGSERIAL PRIMARY KEY,
    stocktake_id BIGINT NOT NULL,
    stock_item_id BIGINT NOT NULL,
    expected_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    counted_quantity DOUBLE PRECISION,
    unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    variance_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    variance_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    counted_at TEXT DEFAULT '',
    FOREIGN KEY(stocktake_id) REFERENCES stocktakes(id) ON DELETE CASCADE,
    FOREIGN KEY(stock_item_id) REFERENCES stock_items(id) ON DELETE CASCADE,
    UNIQUE(stocktake_id,stock_item_id)
);
CREATE TABLE IF NOT EXISTS audit_log(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT,
    user_id BIGINT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id BIGINT,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _staff_fernet():
    raw = os.environ.get("STAFF_DATA_KEY", "").strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except Exception:
            raise RuntimeError("STAFF_DATA_KEY must be a valid Fernet key")
    # Safe fallback for existing deployments: derive a dedicated encryption key
    # from the stable Flask SECRET_KEY. Do not change SECRET_KEY after collecting staff data.
    digest = hashlib.sha256((app.secret_key + "|alport-staff-data-v1").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_staff(value):
    value = (value or "").strip()
    return _staff_fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_staff(value):
    if not value:
        return ""
    try:
        return _staff_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


TRAINING_MODULES = {
    "food_hygiene": {
        "title": "Food Hygiene Essentials",
        "version": "2026.1",
        "refresh_months": 24,
        "summary": "Personal hygiene, contamination control, safe temperatures, cleaning, illness reporting and safe working practices.",
        "lessons": [
            "Wash hands effectively before handling food, after raw food, waste, cleaning, breaks, toilet use and whenever contamination may have occurred.",
            "Prevent cross-contamination by separating raw and ready-to-eat food, using clean equipment and following the venue's documented safe methods.",
            "Follow the site's documented temperature controls. Record checks accurately and take corrective action when a limit is missed.",
            "Do not work with or around open food when illness could make food unsafe. Report vomiting, diarrhoea, infected wounds and other relevant symptoms immediately to the manager.",
            "Clean and disinfect using the venue's approved method, correct chemical, dilution, contact time and equipment. Never mix chemicals.",
        ],
        "questions": [
            {"q":"When should hands be washed?","options":["Only at the start of a shift","Whenever contamination may have occurred, including after raw food, waste and toilet use","Only when visibly dirty"],"answer":1},
            {"q":"What should you do if a recorded food-safety limit is missed?","options":["Ignore it if the food looks fine","Change the record","Follow the site's corrective action and tell the appropriate manager"],"answer":2},
            {"q":"What is the safest approach to raw and ready-to-eat food?","options":["Keep them separated and follow the site's controls","Store them together to save space","Use the same equipment without cleaning"],"answer":0},
            {"q":"What should you do if you have vomiting or diarrhoea?","options":["Work as normal","Report it immediately and follow the site's fitness-to-work procedure","Only tell colleagues"],"answer":1},
            {"q":"Can cleaning chemicals be mixed?","options":["Yes, if both are approved","Only in hot water","No, follow the manufacturer's and site instructions"],"answer":2},
        ],
    },
    "allergens": {
        "title": "Allergen Safety",
        "version": "2026.1",
        "refresh_months": 12,
        "summary": "The 14 regulated allergens, accurate information, cross-contact prevention and handling guest allergen requests.",
        "lessons": [
            "Never guess allergen information. Use the venue's current written allergen information and approved procedure.",
            "Treat every allergy request seriously. Confirm the request, communicate it clearly to the kitchen/service team and identify the correct meal to the guest.",
            "Prevent allergen cross-contact through hand washing, cleaned equipment and surfaces, controlled storage and the venue's preparation procedure.",
            "Ingredient, supplier and recipe changes can change allergen information. Stop and re-check the current specification before giving information.",
            "If the venue cannot safely meet a request, say so clearly. Never promise an allergen-free meal unless the business can genuinely control that risk.",
        ],
        "questions": [
            {"q":"A guest asks whether a dish contains an allergen and you are unsure. What do you do?","options":["Guess from memory","Use current written information and the venue's allergen procedure","Say it is probably safe"],"answer":1},
            {"q":"Can a supplier or recipe change affect allergen information?","options":["Yes","No","Only for desserts"],"answer":0},
            {"q":"Which is part of preventing allergen cross-contact?","options":["Using the same unclean equipment","Cleaning hands, surfaces and equipment as required by the procedure","Removing the allergen after cooking"],"answer":1},
            {"q":"What should happen to an allergy request?","options":["It should be clearly communicated and the correct meal identified to the guest","Only the server needs to know","It can wait until after cooking"],"answer":0},
            {"q":"If the business cannot safely meet an allergy request, what should staff do?","options":["Promise it is safe anyway","Be clear that the request cannot be safely accommodated","Remove visible ingredients only"],"answer":1},
        ],
    },
    "haccp_sfbb": {
        "title": "HACCP and Safer Food Better Business",
        "version": "2026.1",
        "refresh_months": 24,
        "summary": "How the venue's food-safety management system works, critical controls, monitoring, corrective action and records.",
        "lessons": [
            "The venue's food-safety system is site-specific. Staff must know the safe methods and controls relevant to their actual duties.",
            "Monitoring records must reflect what actually happened. Never pre-fill, back-date or falsify a food-safety record.",
            "When a control fails, make the food safe or remove it from use as required, record the issue and corrective action, and escalate where necessary.",
            "Managers responsible for developing or maintaining HACCP controls need training appropriate to that responsibility in addition to this induction module.",
            "Changes to menu, process, equipment, suppliers or premises can require the food-safety system and training to be reviewed.",
        ],
        "questions": [
            {"q":"Should food-safety records be completed in advance?","options":["Yes","No, they must accurately record what happened","Only on quiet days"],"answer":1},
            {"q":"What happens when a food-safety control fails?","options":["Record and follow the site's corrective action","Delete the check","Wait until the next review"],"answer":0},
            {"q":"Are all safe methods identical for every venue?","options":["Yes","No, the system must reflect the site's actual operation","Only chains need site-specific methods"],"answer":1},
            {"q":"Can a major process or menu change require a review?","options":["Yes","No","Only if an EHO asks"],"answer":0},
            {"q":"Does this induction replace role-appropriate HACCP training for a manager who develops the HACCP system?","options":["Yes","No","Only after one year"],"answer":1},
        ],
    },
    "workplace_safety": {
        "title": "Workplace Safety and Site Induction",
        "version": "2026.1",
        "refresh_months": 12,
        "summary": "Accident reporting, slips and trips, burns and cuts, manual handling, fire procedures and safe equipment use.",
        "lessons": [
            "Follow the venue's fire and emergency arrangements, including alarm, escape routes, assembly point and who to report to.",
            "Report hazards, accidents and near misses promptly. Keep routes clear and deal with spills using the site's procedure.",
            "Use knives, hot equipment, machinery and chemicals only when trained and authorised. Use guards and PPE where required.",
            "Assess manual-handling tasks before lifting. Use aids or ask for help when a load or movement cannot be handled safely.",
            "This module supports induction but does not replace site-specific instruction, risk assessments or task-specific training required by the employer.",
        ],
        "questions": [
            {"q":"What should you do with a hazard or near miss?","options":["Ignore it if nobody was hurt","Report it promptly and make the area safe where possible","Wait for the next staff meeting"],"answer":1},
            {"q":"When should machinery be used?","options":["Whenever it is available","Only when trained and authorised for that equipment","Only by managers"],"answer":1},
            {"q":"What should you know about fire arrangements?","options":["Alarm, escape route, assembly point and reporting procedure","Only where extinguishers are","Nothing until there is a fire"],"answer":0},
            {"q":"What is the first step before a difficult lift?","options":["Lift quickly","Assess the task and use help or aids if needed","Twist while lifting"],"answer":1},
            {"q":"Does online induction replace site-specific safety instruction?","options":["Yes","No","Only for kitchen staff"],"answer":1},
        ],
    },
}


def required_training_keys(employee):
    # Core induction for hospitality staff. Managers must still receive any additional
    # role/site-specific instruction required by the employer's risk assessment/HACCP system.
    return list(TRAINING_MODULES.keys())


def ensure_training_assignments(employee):
    for key in required_training_keys(employee):
        module = TRAINING_MODULES[key]
        if not q("SELECT id FROM employee_training WHERE employee_id=? AND module_key=? AND module_version=?", (employee["id"], key, module["version"]), True):
            execute("""INSERT INTO employee_training(organisation_id,site_id,employee_id,module_key,module_version,status,assigned_at)
                       VALUES(?,?,?,?,?,'Not started',?)""",
                    (employee["organisation_id"], employee["site_id"], employee["id"], key, module["version"], now()))


def init_db():
    with conn() as c:
        with c.cursor() as cur:
            for statement in [x.strip() for x in SCHEMA.split(";") if x.strip()]:
                cur.execute(statement)

            # SaaS billing / 12-month contract fields. ADD COLUMN IF NOT EXISTS
            # keeps existing Render databases safe during deployment.
            migrations = [
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS base_price DOUBLE PRECISION NOT NULL DEFAULT 500",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS included_users_per_site INTEGER NOT NULL DEFAULT 3",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS extra_user_price DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0.20",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_months INTEGER NOT NULL DEFAULT 12",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_start TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_end TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contracted_sites INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contracted_users INTEGER NOT NULL DEFAULT 3",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS billing_email TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_checkout_session_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_base_price_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_extra_price_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS past_due_since TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_payment_status TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_payment_at TEXT DEFAULT ''",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS stripe_invoice_id TEXT DEFAULT ''",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS net_amount DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS menu_type TEXT NOT NULL DEFAULT 'Food'",
                "ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS menu_section TEXT NOT NULL DEFAULT 'Mains'",
                "ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS portion_weight DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS portion_unit TEXT NOT NULL DEFAULT 'g'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS cancellation_notice_hours INTEGER NOT NULL DEFAULT 24",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS amendment_notice_hours INTEGER NOT NULL DEFAULT 4",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS guest_can_change_date INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS guest_can_change_time INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS guest_can_change_party INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS large_party_threshold INTEGER NOT NULL DEFAULT 6",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS deposit_type TEXT NOT NULL DEFAULT 'none'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS deposit_value DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS deposit_min_party INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS cancellation_policy TEXT DEFAULT ''",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS logo_url TEXT DEFAULT ''",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS confirmation_subject TEXT DEFAULT 'Your reservation is confirmed â {{venue_name}}'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS confirmation_message TEXT DEFAULT 'Thank you for choosing {{venue_name}}. We are pleased to confirm your reservation and look forward to welcoming you.'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS review_subject TEXT DEFAULT 'Thank you for dining with us â {{venue_name}}'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS review_message TEXT DEFAULT 'Thank you for joining us. We would really appreciate hearing about your experience.'",
                "ALTER TABLE booking_settings ADD COLUMN IF NOT EXISTS stripe_connect_account_id TEXT DEFAULT ''",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS manage_token TEXT DEFAULT ''",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_required DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_paid DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_status TEXT NOT NULL DEFAULT 'Not required'",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_checkout_session_id TEXT DEFAULT ''",
                "ALTER TABLE employees ADD COLUMN IF NOT EXISTS onboarding_status TEXT NOT NULL DEFAULT 'Not invited'",
                "ALTER TABLE employees ADD COLUMN IF NOT EXISTS training_status TEXT NOT NULL DEFAULT 'Not started'",
                "ALTER TABLE employees ADD COLUMN IF NOT EXISTS start_date TEXT DEFAULT ''",
            ]
            for statement in migrations:
                cur.execute(statement)

            cur.execute("""
                INSERT INTO subscriptions(organisation_id, plan, monthly_price, status, started_at)
                SELECT o.id, 'Starter', 0, 'Trial', o.created_at
                FROM organisations o
                LEFT JOIN subscriptions s ON s.organisation_id=o.id
                WHERE s.id IS NULL
            """)


init_db()

# Normalize the commercial model for existing database rows. Stripe recurring price
# itself is controlled by STRIPE_BASE_PRICE_ID and must point to the Â£600 gross price.
try:
    execute("UPDATE subscriptions SET base_price=?,extra_user_price=?,included_users_per_site=?", (BASE_PRICE_PER_SITE, 0.0, 0))
    execute("UPDATE subscriptions SET monthly_price=? * GREATEST(1,COALESCE(contracted_sites,1))", (BASE_PRICE_PER_SITE,))
except Exception:
    pass


def user():
    uid = session.get("user_id")
    if not uid:
        return None
    return q("SELECT * FROM users WHERE id=? AND active=1", (uid,), True)


def org():
    u = user()
    if not u:
        return None
    return q("SELECT * FROM organisations WHERE id=?", (u["organisation_id"],), True)


def current_site():
    u = user()
    if not u:
        return None
    sid = session.get("site_id")
    if sid:
        s = q(
            "SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",
            (sid, u["organisation_id"]),
            True,
        )
        if s:
            return s
    s = q(
        "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",
        (u["organisation_id"],),
        True,
    )
    if s:
        session["site_id"] = s["id"]
    return s


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def manager_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = user()
        if not u:
            return jsonify(error="Not authenticated"), 401
        allowed = ("Owner", "Admin", "Finance", "General Manager", "Manager")
        if u["role"] not in allowed:
            return jsonify(error="Manager permission required"), 403
        return fn(*args, **kwargs)
    return wrapped


def account_admin_required(fn):
    """Only organisation owners/admins can manage paid Alport Hospitality Solutions login licences."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = user()
        if not u:
            return jsonify(error="Not authenticated"), 401
        if u["role"] not in ("Owner", "Admin"):
            return jsonify(error="Owner or Admin permission required"), 403
        return fn(*args, **kwargs)
    return wrapped


def licence_summary(organisation_id):
    sub = subscription_for(organisation_id) or {}
    p = pricing_for(organisation_id)
    active_users = int(q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND active=1", (organisation_id,), True)["n"] or 0)
    return {
        **p,
        "licensed_capacity": None,
        "active_users": active_users,
        "available_licences": None,
        "contracted_users": active_users,
        "contracted_sites": max(1, int(sub.get("contracted_sites") or p["billable_sites"] or 1)),
        "contract_start": sub.get("contract_start") or "",
        "contract_end": sub.get("contract_end") or "",
        "status": sub.get("status") or "",
        "unlimited_users": True,
    }


def sync_stripe_subscription_quantities(organisation_id, contracted_sites, contracted_users=0):
    """Keep the per-venue Stripe quantity aligned. User quantity is never billed."""
    sub = subscription_for(organisation_id) or {}
    subscription_id = (sub.get("stripe_subscription_id") or "").strip()
    if not subscription_id:
        return
    base_price_id = (sub.get("stripe_base_price_id") or os.environ.get("STRIPE_BASE_PRICE_ID", "")).strip()
    remote = stripe_request("GET", "/subscriptions/" + subscription_id)
    items = ((remote.get("items") or {}).get("data") or [])
    for item in items:
        price = item.get("price") or {}
        pid = price.get("id") if isinstance(price, dict) else price
        if str(pid or "") == base_price_id:
            stripe_request("POST", "/subscription_items/" + str(item["id"]), {
                "quantity": str(max(1, int(contracted_sites))),
                "proration_behavior": "create_prorations",
            })
            return


def audit(action, entity, entity_id=None, detail=""):
    u = user()
    execute(
        """INSERT INTO audit_log(organisation_id,user_id,action,entity,entity_id,detail,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            u["organisation_id"] if u else None,
            u["id"] if u else None,
            action,
            entity,
            entity_id,
            detail,
            now(),
        ),
    )


def month_bounds(month):
    try:
        y, m = map(int, month.split("-"))
    except Exception:
        y = date.today().year
        m = date.today().month
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y + 1:04d}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01"
    return start, end


def finance_summary(month=None):
    u = user()
    s = current_site()
    month = month or date.today().strftime("%Y-%m")

    if not u or not s:
        return {
            "month": month,
            "revenue": 0,
            "food": 0,
            "drink": 0,
            "cogs": 0,
            "gross_profit": 0,
            "labour": 0,
            "overheads": 0,
            "ebitda": 0,
            "gross_margin": 0,
            "ebitda_margin": 0,
        }

    start, end = month_bounds(month)

    revenue_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<?",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    food_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Food'",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    drink_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Drink'",
        (u["organisation_id"], s["id"], start, end),
        True,
    )

    revenue = float(revenue_row["x"] or 0)
    food = float(food_row["x"] or 0)
    drink = float(drink_row["x"] or 0)

    cogs_row = q(
        """SELECT COALESCE(SUM(ABS(sm.quantity) * si.unit_cost),0) x
           FROM stock_movements sm JOIN stock_items si ON si.id=sm.stock_item_id
           WHERE sm.organisation_id=? AND sm.site_id=? AND sm.created_at>=? AND sm.created_at<?
           AND LOWER(sm.movement_type) IN ('usage','waste','sale','cogs')""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    cogs = float(cogs_row["x"] or 0)

    labour_row = q(
        """SELECT COALESCE(SUM(CASE WHEN sh.status!='Cancelled' THEN
           GREATEST(0,(((CAST(substr(sh.end_time,1,2) AS INTEGER)*60+CAST(substr(sh.end_time,4,2) AS INTEGER))-
           (CAST(substr(sh.start_time,1,2) AS INTEGER)*60+CAST(substr(sh.start_time,4,2) AS INTEGER))) / 60.0)-sh.break_minutes/60.0) * e.pay_rate ELSE 0 END),0) x
           FROM shifts sh JOIN employees e ON e.id=sh.employee_id
           WHERE sh.organisation_id=? AND sh.site_id=? AND sh.shift_date>=? AND sh.shift_date<?""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    labour = float(labour_row["x"] or 0)

    overhead_row = q(
        """SELECT COALESCE(SUM(gross),0) x FROM expenses
           WHERE organisation_id=? AND site_id=? AND expense_date>=? AND expense_date<?
           AND category NOT IN ('Food','Drink','Labour')""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    overheads = float(overhead_row["x"] or 0)

    gross_profit = revenue - cogs
    ebitda = gross_profit - labour - overheads
    gross_margin = gross_profit / revenue * 100 if revenue else 0
    ebitda_margin = ebitda / revenue * 100 if revenue else 0

    return {
        "month": month,
        "revenue": revenue,
        "food": food,
        "drink": drink,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "labour": labour,
        "overheads": overheads,
        "ebitda": ebitda,
        "gross_margin": gross_margin,
        "ebitda_margin": ebitda_margin,
    }


@app.get("/privacy")
@app.get("/privacy-policy")
def privacy_policy():
    return render_template(
        "privacy.html",
        legal_name=PRIVACY_LEGAL_NAME,
        trading_name=PRIVACY_TRADING_NAME,
        privacy_email=PRIVACY_EMAIL,
        privacy_address=PRIVACY_ADDRESS,
        ico_number=PRIVACY_ICO_NUMBER,
        effective_date=PRIVACY_EFFECTIVE_DATE,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        u = q(
            "SELECT * FROM users WHERE lower(email)=? AND active=1 ORDER BY id LIMIT 1",
            (email,),
            True,
        )
        if u and check_password_hash(u["password_hash"], password):
            sub = subscription_for(u["organisation_id"])
            if subscription_blocks_access(sub):
                return render_template("login.html", error="This Alport Hospitality Solutions account is currently suspended because the subscription is not in good standing. Please contact Alport Hospitality Solutions support."), 403
            session.clear()
            session["user_id"] = u["id"]
            s = q(
                "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",
                (u["organisation_id"],),
                True,
            )
            if s:
                session["site_id"] = s["id"]
            if sub and sub.get("status") == "Payment required":
                return redirect(url_for("subscribe"))
            return redirect(url_for("app_home"))
        return render_template("login.html", error="Incorrect email or password.")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    if user():
        return redirect(url_for("app_home"))

    if request.method == "POST":
        business = (request.form.get("business_name") or "").strip()
        business_type = (request.form.get("business_type") or "Restaurant").strip()
        site_name = (request.form.get("site_name") or "").strip()
        address = (request.form.get("address") or "").strip()
        owner_name = (request.form.get("owner_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not business or not site_name or not owner_name or not email or len(password) < 8:
            return render_template(
                "onboarding.html",
                error="Complete all fields and use a password of at least 8 characters.",
            )

        try:
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO organisations(name,business_type,created_at) VALUES(%s,%s,%s) RETURNING id",
                        (business, business_type, now()),
                    )
                    organisation_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO subscriptions(
                               organisation_id,plan,monthly_price,status,started_at,base_price,
                               included_users_per_site,extra_user_price,vat_rate,contract_months,
                               contracted_sites,contracted_users,billing_email)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (organisation_id) DO NOTHING""",
                        (
                            organisation_id, "Alport Hospitality Solutions", BASE_PRICE_PER_SITE,
                            "Payment required", now(), BASE_PRICE_PER_SITE, INCLUDED_USERS_PER_SITE,
                            EXTRA_USER_PRICE, VAT_RATE, CONTRACT_MONTHS, 1, 1, email,
                        ),
                    )

                    cur.execute(
                        "INSERT INTO sites(organisation_id,name,address,created_at) VALUES(%s,%s,%s,%s) RETURNING id",
                        (organisation_id, site_name, address, now()),
                    )
                    site_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO users(organisation_id,name,email,password_hash,role,created_at)
                           VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (
                            organisation_id,
                            owner_name,
                            email,
                            generate_password_hash(password),
                            "Owner",
                            now(),
                        ),
                    )
                    user_id = cur.fetchone()["id"]

        except psycopg.IntegrityError:
            return render_template(
                "onboarding.html",
                error="That email is already registered for this business.",
            )

        session.clear()
        session["user_id"] = user_id
        session["site_id"] = site_id
        return redirect(url_for("subscribe"))

    return render_template("onboarding.html")


@app.route("/subscribe", methods=["GET", "POST"])
@login_required
def subscribe():
    u = user()
    organisation_id = u["organisation_id"]
    sub = subscription_for(organisation_id)
    pricing = pricing_for(organisation_id)
    if sub and sub.get("status") == "Active":
        return redirect(url_for("app_home"))

    error = None
    if request.method == "POST":
        action = (request.form.get("action") or "pay").strip()

        if action == "admin_preview":
            admin_email = (request.form.get("admin_email") or "").strip().lower()
            admin_password = request.form.get("admin_password") or ""
            expected_email = os.environ.get("COMPANY_ADMIN_EMAIL", "").strip().lower()
            expected_password = os.environ.get("COMPANY_ADMIN_PASSWORD", "")

            if (
                expected_email
                and expected_password
                and hmac.compare_digest(admin_email, expected_email)
                and hmac.compare_digest(admin_password, expected_password)
            ):
                session["billing_preview_bypass_org"] = int(organisation_id)
                session["billing_preview_admin"] = True
                return redirect(url_for("app_home"))

            error = "Incorrect company admin email or password."

        elif not stripe_configured():
            error = "Online billing is not fully configured yet. Please contact Alport Hospitality Solutions."
        elif request.form.get("accept_contract") != "yes":
            error = "You must accept the 12-month minimum-term agreement to continue."
        else:
            try:
                base_price_id = os.environ.get("STRIPE_BASE_PRICE_ID", "").strip()
                extra_price_id = ""
                success_url = request.url_root.rstrip("/") + "/subscription/success?session_id={CHECKOUT_SESSION_ID}"
                cancel_url = request.url_root.rstrip("/") + "/subscribe"
                data = {
                    "mode": "subscription",
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "client_reference_id": str(organisation_id),
                    "customer_email": (sub or {}).get("billing_email") or u["email"],
                    "billing_address_collection": "required",
                    "tax_id_collection[enabled]": "true",
                    "payment_method_collection": "always",
                    "metadata[organisation_id]": str(organisation_id),
                    "subscription_data[metadata][organisation_id]": str(organisation_id),
                    "line_items[0][price]": base_price_id,
                    "line_items[0][quantity]": str(pricing["billable_sites"]),
                }
                checkout = stripe_request("POST", "/checkout/sessions", data)
                execute(
                    """UPDATE subscriptions SET stripe_checkout_session_id=?,stripe_base_price_id=?,
                       stripe_extra_price_id=?,monthly_price=?,contracted_sites=?,contracted_users=?
                       WHERE organisation_id=?""",
                    (
                        checkout.get("id", ""), base_price_id, extra_price_id, pricing["net"],
                        pricing["billable_sites"], pricing["billable_users"], organisation_id,
                    ),
                )
                return redirect(checkout["url"])
            except Exception as exc:
                error = str(exc)

    return render_template(
        "subscribe.html", user=u, organisation=org(), pricing=pricing, subscription=sub,
        error=error, contract_months=CONTRACT_MONTHS,
    )


@app.get("/subscription/success")
@login_required
def subscription_success():
    u = user()
    sid = (request.args.get("session_id") or "").strip()
    activated = False
    error = None
    if sid and os.environ.get("STRIPE_SECRET_KEY"):
        try:
            checkout = stripe_request("GET", "/checkout/sessions/" + sid)
            if str(checkout.get("client_reference_id") or "") == str(u["organisation_id"]):
                if checkout.get("status") == "complete" or checkout.get("payment_status") in ("paid", "no_payment_required"):
                    activate_contract_from_checkout(u["organisation_id"], checkout)
                    activated = True
        except Exception as exc:
            error = str(exc)
    sub = subscription_for(u["organisation_id"])
    activated = activated or bool(sub and sub.get("status") == "Active")
    return render_template("subscription_success.html", activated=activated, error=error, subscription=sub)


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(cache=False)
    signature = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not verify_stripe_signature(payload, signature, secret):
        return jsonify(error="Invalid Stripe signature"), 400
    try:
        event = json.loads(payload.decode("utf-8"))
        event_type = event.get("type")
        obj = ((event.get("data") or {}).get("object") or {})

        if event_type == "checkout.session.completed":
            booking_id = int((obj.get("metadata") or {}).get("booking_id") or 0)
            if booking_id and obj.get("payment_status") in ("paid","no_payment_required"):
                booking=q("SELECT * FROM bookings WHERE id=?",(booking_id,),True)
                if booking:
                    paid=float(obj.get("amount_total") or 0)/100.0
                    execute("UPDATE bookings SET deposit_paid=?,deposit_status='Paid',updated_at=? WHERE id=?",(paid,now(),booking_id))
            else:
                organisation_id = int((obj.get("metadata") or {}).get("organisation_id") or obj.get("client_reference_id") or 0)
                if organisation_id:
                    activate_contract_from_checkout(organisation_id, obj)

        elif event_type == "invoice.paid":
            subscription_id = str(obj.get("subscription") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                record_stripe_payment(sub["organisation_id"], obj)
                next_ts = obj.get("period_end")
                next_date = datetime.utcfromtimestamp(next_ts).date().isoformat() if next_ts else ""
                execute(
                    """UPDATE subscriptions SET status='Active',past_due_since='',last_payment_status='Paid',
                       last_payment_at=?,next_billing_date=? WHERE organisation_id=?""",
                    (now(), next_date, sub["organisation_id"]),
                )
                company_event("Stripe payment received", f"Invoice {obj.get('id','')}", sub["organisation_id"])

        elif event_type == "invoice.payment_failed":
            subscription_id = str(obj.get("subscription") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                execute(
                    """UPDATE subscriptions SET status='Past due',past_due_since=CASE WHEN past_due_since='' THEN ? ELSE past_due_since END,
                       last_payment_status='Failed' WHERE organisation_id=?""",
                    (date.today().isoformat(), sub["organisation_id"]),
                )
                company_event("Payment failed", f"Stripe invoice {obj.get('id','')}", sub["organisation_id"])

        elif event_type == "customer.subscription.deleted":
            subscription_id = str(obj.get("id") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                end = _parse_iso_date(sub.get("contract_end"))
                status = "Suspended" if end and date.today() < end else "Cancelled"
                execute("UPDATE subscriptions SET status=?,cancelled_at=? WHERE organisation_id=?", (status, now(), sub["organisation_id"]))
                company_event("Stripe subscription ended", f"Account status: {status}", sub["organisation_id"])

        return jsonify(received=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


def send_alport_email(to_email, subject, text_body, html_body=None):
    """Send a venue-branded transactional/CRM email through Resend."""
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_email = os.environ.get("SUPPORT_FROM_EMAIL", "").strip()
    if not resend_key or not from_email or not to_email:
        return {"ok": False, "id": "", "error": "Email is not configured"}
    payload = {"from": from_email, "to": [to_email], "subject": subject, "text": text_body}
    if html_body:
        payload["html"] = html_body
    try:
        r = requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"}, json=payload, timeout=20)
        result = r.json() if r.content else {}
        return {"ok": bool(r.ok), "id": str(result.get("id") or ""), "error": "" if r.ok else str(result)}
    except Exception as exc:
        return {"ok": False, "id": "", "error": str(exc)}


def booking_settings_for(organisation_id, site_id):
    row = q("SELECT * FROM booking_settings WHERE organisation_id=? AND site_id=?", (organisation_id, site_id), True)
    if row:
        return row
    execute("""INSERT INTO booking_settings(organisation_id,site_id,updated_at) VALUES(?,?,?)""", (organisation_id, site_id, now()))
    return q("SELECT * FROM booking_settings WHERE organisation_id=? AND site_id=?", (organisation_id, site_id), True)


def minutes_of(value):
    try:
        h, m = str(value)[:5].split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def hhmm(total):
    total = int(total) % 1440
    return f"{total//60:02d}:{total%60:02d}"


def booking_available_tables(organisation_id, site_id, booking_date, booking_time, party_size, duration_minutes, exclude_booking_id=None):
    settings = booking_settings_for(organisation_id, site_id)
    start = minutes_of(booking_time)
    end = start + int(duration_minutes) + int(settings.get("turnaround_minutes") or 0)
    tables = q("""SELECT * FROM restaurant_tables WHERE organisation_id=? AND site_id=? AND active=1
                  AND min_capacity<=? AND max_capacity>=? ORDER BY max_capacity ASC,sort_order,table_name""",
               (organisation_id, site_id, party_size, party_size))
    existing = q("""SELECT id,table_id,booking_time,duration_minutes FROM bookings
                    WHERE organisation_id=? AND site_id=? AND booking_date=?
                    AND status NOT IN ('Cancelled','No-show') AND table_id IS NOT NULL""",
                 (organisation_id, site_id, booking_date))
    available = []
    for table in tables:
        clash = False
        for b in existing:
            if exclude_booking_id and int(b["id"]) == int(exclude_booking_id):
                continue
            if int(b["table_id"]) != int(table["id"]):
                continue
            bstart = minutes_of(b["booking_time"])
            bend = bstart + int(b["duration_minutes"] or 120) + int(settings.get("turnaround_minutes") or 0)
            if start < bend and bstart < end:
                clash = True
                break
        if not clash:
            available.append(dict(table))
    return available


def upsert_guest(organisation_id, name, email, phone, marketing_consent=False):
    guest = None
    if email:
        guest = q("SELECT * FROM guests WHERE organisation_id=? AND lower(email)=lower(?) ORDER BY id LIMIT 1", (organisation_id, email), True)
    if not guest and phone:
        guest = q("SELECT * FROM guests WHERE organisation_id=? AND phone=? ORDER BY id LIMIT 1", (organisation_id, phone), True)
    if guest:
        execute("""UPDATE guests SET name=?,email=?,phone=?,marketing_consent=CASE WHEN ?=1 THEN 1 ELSE marketing_consent END,updated_at=? WHERE id=?""",
                (name, email, phone, 1 if marketing_consent else 0, now(), guest["id"]))
        return guest["id"]
    return execute("""INSERT INTO guests(organisation_id,name,email,phone,marketing_consent,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                   (organisation_id, name, email, phone, 1 if marketing_consent else 0, now(), now()))


@app.get("/")
def public_home():
    return render_template("home.html")


@app.post("/support")
def public_support():
    name = (request.form.get("name") or "").strip()[:120]
    email = (request.form.get("email") or "").strip()[:200]
    business = (request.form.get("business") or "").strip()[:160]
    subject = (request.form.get("subject") or "General enquiry").strip()[:160]
    message = (request.form.get("message") or "").strip()[:5000]
    if not name or not email or "@" not in email or not message:
        return redirect(url_for("public_home", support="missing") + "#support")

    support_email = os.environ.get("SUPPORT_EMAIL", "").strip()
    from_email = os.environ.get("SUPPORT_FROM_EMAIL", "").strip()
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not support_email or not from_email or not resend_key:
        return redirect(url_for("public_home", support="unavailable") + "#support")

    safe_subject = f"Alport support: {subject}"
    body = (
        f"New Alport website enquiry\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Business: {business or 'Not supplied'}\n"
        f"Subject: {subject}\n\n"
        f"Message:\n{message}\n"
    )
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": from_email,
                "to": [support_email],
                "reply_to": email,
                "subject": safe_subject,
                "text": body,
            },
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError("Email provider rejected the message")
    except Exception:
        return redirect(url_for("public_home", support="error") + "#support")
    return redirect(url_for("public_home", support="sent") + "#support")


@app.get("/app")
@login_required
def app_home():
    u = user()
    sub = subscription_for(u["organisation_id"])
    if sub and sub.get("status") == "Payment required":
        preview_ok = (
            session.get("billing_preview_admin") is True
            and int(session.get("billing_preview_bypass_org") or 0) == int(u["organisation_id"])
        )
        if not preview_ok:
            return redirect(url_for("subscribe"))
    if subscription_blocks_access(sub):
        session.clear()
        return redirect(url_for("login"))
    return render_template("app.html", user=u, organisation=org(), site=current_site())


@app.get("/api/me")
@login_required
def api_me():
    u, o, s = user(), org(), current_site()
    return jsonify(user=dict(u), organisation=dict(o), site=dict(s) if s else None)


@app.get("/api/licences")
@login_required
def licences_overview():
    u = user()
    rows = q(
        "SELECT id,name,email,role,active,created_at FROM users WHERE organisation_id=? ORDER BY active DESC,id",
        (u["organisation_id"],),
    )
    return jsonify(licence=licence_summary(u["organisation_id"]), users=[dict(x) for x in rows])


@app.post("/api/licences/upgrade")
@login_required
@account_admin_required
def upgrade_licences():
    return jsonify(ok=True, message="Alport now includes unlimited users at no additional charge.")


@app.post("/api/licences/users")
@login_required
@account_admin_required
def add_login_user():
    u = user()
    organisation_id = u["organisation_id"]
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()[:120]
    email = (d.get("email") or "").strip().lower()[:200]
    password = d.get("password") or ""
    role = (d.get("role") or "Manager").strip()
    allowed_roles = ("Admin", "Finance", "General Manager", "Manager", "User")
    if not name or not email or "@" not in email:
        return jsonify(error="Name and a valid email address are required"), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400
    if role not in allowed_roles:
        return jsonify(error="Invalid user role"), 400

    existing = q("SELECT id,active FROM users WHERE organisation_id=? AND lower(email)=?", (organisation_id, email), True)
    if existing:
        return jsonify(error="That email already belongs to an Alport Hospitality Solutions user for this restaurant."), 409
    try:
        user_id = execute(
            """INSERT INTO users(organisation_id,name,email,password_hash,role,active,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (organisation_id, name, email, generate_password_hash(password), role, now()),
        )
    except psycopg.IntegrityError:
        return jsonify(error="That email is already registered."), 409
    audit("Created", "login_user", user_id, f"{name} Â· {role}")
    company_event("Alport Hospitality Solutions user added", f"{name} Â· {email}", organisation_id)
    return jsonify(ok=True, id=user_id)


@app.post("/api/licences/users/<int:user_id>/deactivate")
@login_required
@account_admin_required
def deactivate_login_user(user_id):
    u = user()
    target = q("SELECT * FROM users WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]), True)
    if not target:
        return jsonify(error="User not found"), 404
    if int(target["id"]) == int(u["id"]):
        return jsonify(error="You cannot deactivate your own account."), 400
    if target.get("role") == "Owner":
        owners = q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND role='Owner' AND active=1", (u["organisation_id"],), True)
        if int(owners["n"] or 0) <= 1:
            return jsonify(error="The organisation must keep at least one active Owner."), 400
    execute("UPDATE users SET active=0 WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]))
    audit("Deactivated", "login_user", user_id, target["email"])
    company_event("Alport Hospitality Solutions user deactivated", target["email"], u["organisation_id"])
    return jsonify(ok=True, message="User deactivated.")


@app.post("/api/licences/users/<int:user_id>/activate")
@login_required
@account_admin_required
def activate_login_user(user_id):
    u = user()
    target = q("SELECT * FROM users WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]), True)
    if not target:
        return jsonify(error="User not found"), 404
    if int(target.get("active") or 0) == 1:
        return jsonify(ok=True)
    execute("UPDATE users SET active=1 WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]))
    audit("Activated", "login_user", user_id, target["email"])
    company_event("Alport Hospitality Solutions user reactivated", target["email"], u["organisation_id"])
    return jsonify(ok=True)


@app.post("/api/site")
@login_required
@manager_required
def add_site():
    u = user()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    address = (d.get("address") or "").strip()
    if not name:
        return jsonify(error="Site name is required"), 400
    site_id = execute(
        "INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)",
        (u["organisation_id"], name, address, now()),
    )
    audit("Created", "site", site_id, name)
    sub = subscription_for(u["organisation_id"])
    if sub and sub.get("status") in ("Active", "Past due"):
        p = pricing_for(u["organisation_id"])
        new_sites = p["billable_sites"]
        new_contracted_users = p["active_users"]
        new_net = round(BASE_PRICE_PER_SITE * new_sites, 2)
        try:
            if sub.get("stripe_subscription_id"):
                sync_stripe_subscription_quantities(u["organisation_id"], new_sites, new_contracted_users)
        except Exception as exc:
            return jsonify(error=f"Restaurant created, but Stripe billing could not be updated: {exc}"), 502
        execute(
            """UPDATE subscriptions SET contracted_sites=?,contracted_users=?,monthly_price=?
               WHERE organisation_id=?""",
            (new_sites, new_contracted_users, new_net, u["organisation_id"]),
        )
        company_event("Restaurant added", f"Contract value now Â£{new_net:.2f} + VAT/month", u["organisation_id"])
    return jsonify(ok=True, id=site_id)


@app.post("/api/site/select")
@login_required
def select_site():
    u = user()
    d = request.get_json() or {}
    try:
        site_id = int(d.get("site_id", 0))
    except Exception:
        return jsonify(error="Invalid site"), 400
    s = q(
        "SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",
        (site_id, u["organisation_id"]),
        True,
    )
    if not s:
        return jsonify(error="Site not found"), 404
    session["site_id"] = site_id
    return jsonify(ok=True)


@app.get("/api/sites")
@login_required
def sites():
    u = user()
    result = q(
        "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY name",
        (u["organisation_id"],),
    )
    return jsonify(sites=[dict(x) for x in result])


@app.get("/api/dashboard")
@login_required
def dashboard():
    f = finance_summary()
    u, s = user(), current_site()
    pending = q(
        "SELECT COALESCE(SUM(gross),0) x FROM invoices WHERE organisation_id=? AND site_id=? AND status IN ('Awaiting approval','Approved')",
        (u["organisation_id"], s["id"]),
        True,
    )
    stock_low = q(
        "SELECT COUNT(*) x FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 AND on_hand<par_level",
        (u["organisation_id"], s["id"]),
        True,
    )
    payroll = q(
        "SELECT COALESCE(SUM(net_pay),0) x FROM payroll_runs WHERE organisation_id=? AND site_id=? AND status IN ('Draft','Approved')",
        (u["organisation_id"], s["id"]),
        True,
    )
    return jsonify(
        **f,
        outstanding_invoices=float(pending["x"] or 0),
        low_stock=int(stock_low["x"] or 0),
        payroll_due=float(payroll["x"] or 0),
    )


@app.get("/api/finance")
@login_required
def api_finance():
    return jsonify(finance_summary(request.args.get("month")))


@app.get("/api/sales")
@login_required
def get_sales():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM sales WHERE organisation_id=? AND site_id=? ORDER BY sale_date DESC,id DESC LIMIT 200",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(sales=[dict(r) for r in result])


@app.post("/api/sales")
@login_required
@manager_required
def add_sale():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross", 0))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if gross <= 0:
        return jsonify(error="Gross sales must be greater than zero"), 400
    if vat < 0 or vat > gross:
        return jsonify(error="Invalid VAT amount"), 400
    sale_id = execute(
        """INSERT INTO sales(organisation_id,site_id,sale_date,category,net,vat,gross,source)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d.get("sale_date") or date.today().isoformat(),
            d.get("category") or "Other",
            gross - vat,
            vat,
            gross,
            d.get("source") or "Manual",
        ),
    )
    audit("Created", "sale", sale_id)
    return jsonify(ok=True, id=sale_id)


@app.get("/api/expenses")
@login_required
def get_expenses():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM expenses WHERE organisation_id=? AND site_id=? ORDER BY expense_date DESC,id DESC LIMIT 200",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(expenses=[dict(r) for r in result])


@app.post("/api/expenses")
@login_required
@manager_required
def add_expense():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross", d.get("amount", 0)))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if gross <= 0:
        return jsonify(error="Amount must be greater than zero"), 400
    if vat < 0 or vat > gross:
        return jsonify(error="Invalid VAT amount"), 400
    expense_id = execute(
        """INSERT INTO expenses(organisation_id,site_id,expense_date,category,description,supplier,net,vat,gross)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d.get("expense_date") or date.today().isoformat(),
            d.get("category") or "Other",
            d.get("description") or "Expense",
            d.get("supplier") or "",
            gross - vat,
            vat,
            gross,
        ),
    )
    audit("Created", "expense", expense_id)
    return jsonify(ok=True, id=expense_id)


@app.get("/api/invoices")
@login_required
def get_invoices():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM invoices WHERE organisation_id=? AND site_id=? ORDER BY due_date,id DESC",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(invoices=[dict(r) for r in result])


@app.post("/api/invoices")
@login_required
@manager_required
def add_invoice():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        net = float(d.get("net", d.get("net_amount", 0)))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if not d.get("supplier") or not d.get("invoice_number") or net <= 0:
        return jsonify(
            error="Supplier, invoice number and positive net amount are required"
        ), 400
    if vat < 0:
        return jsonify(error="VAT cannot be negative"), 400
    invoice_id = execute(
        """INSERT INTO invoices(organisation_id,site_id,supplier,invoice_number,invoice_date,due_date,category,net,vat,gross,notes,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d["supplier"],
            d["invoice_number"],
            d.get("invoice_date") or date.today().isoformat(),
            d.get("due_date") or date.today().isoformat(),
            d.get("category") or "Other",
            net,
            vat,
            net + vat,
            d.get("notes") or "",
            now(),
        ),
    )
    audit("Created", "invoice", invoice_id, d["invoice_number"])
    return jsonify(ok=True, id=invoice_id)


@app.post("/api/invoices/<int:iid>/approve")
@login_required
@manager_required
def approve_invoice(iid):
    u, s = user(), current_site()
    invoice = q(
        "SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not invoice:
        return jsonify(error="Invoice not found"), 404
    if invoice["status"] != "Awaiting approval":
        return jsonify(error="Invoice is not awaiting approval"), 400
    execute(
        "UPDATE invoices SET status='Approved',approved_by=?,approved_at=? WHERE id=?",
        (u["id"], now(), iid),
    )
    payment_id = execute(
        """INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            "Supplier",
            invoice["supplier"],
            invoice["invoice_number"],
            invoice["gross"],
            invoice["due_date"],
            "Scheduled",
            "Bank transfer",
            iid,
            u["id"],
            now(),
        ),
    )
    audit("Approved", "invoice", iid, "Payment " + str(payment_id) + " scheduled")
    return jsonify(ok=True)


@app.post("/api/invoices/<int:iid>/pay")
@login_required
@manager_required
def pay_invoice(iid):
    u, s = user(), current_site()
    invoice = q(
        "SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not invoice or invoice["status"] != "Approved":
        return jsonify(error="Invoice must be approved first"), 400
    payment_time = now()
    execute("UPDATE invoices SET status='Paid',paid_at=? WHERE id=?", (payment_time, iid))
    execute(
        "UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Supplier'",
        (payment_time, iid),
    )
    audit("Recorded paid", "invoice", iid)
    return jsonify(ok=True)


@app.get("/api/payments")
@login_required
def get_payments():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM payments WHERE organisation_id=? AND site_id=? ORDER BY payment_date,id DESC",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(payments=[dict(r) for r in result])


@app.get("/api/employees")
@login_required
def get_employees():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(employees=[dict(x) for x in result])


@app.post("/api/employees")
@login_required
@manager_required
def add_employee():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    try:
        rate = float(d.get("pay_rate", 0))
        ni_rate = float(d.get("ni_rate", 0))
        pension_rate = float(d.get("pension_rate", 0))
        holiday_allowance = float(d.get("holiday_allowance", 28))
    except Exception:
        return jsonify(error="Invalid employee values"), 400
    if rate < 0:
        return jsonify(error="Pay rate cannot be negative"), 400
    employee_id = execute(
        """INSERT INTO employees(organisation_id,site_id,name,email,department,job_title,pay_type,pay_rate,ni_rate,pension_rate,holiday_allowance)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            name,
            d.get("email") or "",
            d.get("department") or "FOH",
            d.get("job_title") or "",
            d.get("pay_type") or "Hourly",
            rate,
            ni_rate,
            pension_rate,
            holiday_allowance,
        ),
    )
    employee = q("SELECT * FROM employees WHERE id=?", (employee_id,), True)
    token = secrets.token_urlsafe(32)
    execute("""INSERT INTO employee_onboarding(organisation_id,site_id,employee_id,token,status,invited_at)
               VALUES(?,?,?,?,?,?)""", (u["organisation_id"], s["id"], employee_id, token, "Invited", now()))
    ensure_training_assignments(employee)
    execute("UPDATE employees SET onboarding_status='Invited',training_status='Not started' WHERE id=?", (employee_id,))
    link = _public_base_url() + "/staff-onboarding/" + token
    email_result = send_alport_email(
        employee.get("email") or "",
        f"Complete your onboarding - {s['name']}",
        f"Hello {name},\n\n{s['name']} has invited you to complete your staff onboarding with Alport Hospitality Solutions. "
        f"Please complete your payroll details and compulsory induction training before your first working shift.\n\n{link}\n\n"
        "Your personal information is used by your employer for employment and payroll administration. Do not forward this secure link."
    ) if employee.get("email") else {"ok": False, "error": "No employee email"}
    audit("Created and invited", "employee", employee_id, name)
    return jsonify(ok=True, id=employee_id, invited=bool(email_result.get("ok")), invite_error=email_result.get("error", ""))


@app.get("/api/shifts")
@login_required
def get_shifts():
    u, s = user(), current_site()
    result = q(
        """SELECT sh.*,e.name employee_name,e.pay_rate FROM shifts sh JOIN employees e ON e.id=sh.employee_id
           WHERE sh.organisation_id=? AND sh.site_id=? ORDER BY shift_date,start_time""",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(shifts=[dict(x) for x in result])


@app.post("/api/shifts")
@login_required
@manager_required
def add_shift():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        employee_id = int(d.get("employee_id"))
        break_minutes = int(d.get("break_minutes", 0))
    except Exception:
        return jsonify(error="Invalid employee"), 400
    if not d.get("shift_date") or not d.get("start_time") or not d.get("end_time"):
        return jsonify(error="Shift date, start time and end time are required"), 400
    if break_minutes < 0:
        return jsonify(error="Break cannot be negative"), 400
    employee = q(
        "SELECT * FROM employees WHERE id=? AND organisation_id=? AND site_id=? AND active=1",
        (employee_id, u["organisation_id"], s["id"]),
        True,
    )
    if not employee:
        return jsonify(error="Employee not found"), 404
    shift_id = execute(
        "INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes) VALUES(?,?,?,?,?,?,?)",
        (
            u["organisation_id"],
            s["id"],
            employee_id,
            d["shift_date"],
            d["start_time"],
            d["end_time"],
            break_minutes,
        ),
    )
    audit("Created", "shift", shift_id, employee["name"])
    return jsonify(ok=True, id=shift_id)


@app.get("/api/payroll")
@login_required
def get_payroll():
    u, s = user(), current_site()
    runs = q(
        "SELECT * FROM payroll_runs WHERE organisation_id=? AND site_id=? ORDER BY period_end DESC",
        (u["organisation_id"], s["id"]),
    )
    employees = q(
        "SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(
        runs=[dict(x) for x in runs],
        employees=[dict(x) for x in employees],
    )


@app.post("/api/payroll")
@login_required
@manager_required
def add_payroll():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross_pay", 0))
        employer = float(d.get("employer_costs", 0))
        deductions = float(d.get("deductions", 0))
    except Exception:
        return jsonify(error="Invalid payroll values"), 400
    if gross <= 0:
        return jsonify(error="Gross pay must be greater than zero"), 400
    if employer < 0 or deductions < 0 or deductions > gross:
        return jsonify(error="Invalid payroll values"), 400
    if not d.get("period_start") or not d.get("period_end"):
        return jsonify(error="Payroll period is required"), 400
    run_id = execute(
        """INSERT INTO payroll_runs(organisation_id,site_id,period_start,period_end,gross_pay,employer_costs,deductions,net_pay,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d["period_start"],
            d["period_end"],
            gross,
            employer,
            deductions,
            gross - deductions,
            now(),
        ),
    )
    audit("Created", "payroll", run_id)
    return jsonify(ok=True, id=run_id)


@app.post("/api/payroll/<int:rid>/approve")
@login_required
@manager_required
def approve_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q(
        "SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",
        (rid, u["organisation_id"], s["id"]),
        True,
    )
    if not payroll_run:
        return jsonify(error="Payroll run not found"), 404
    if payroll_run["status"] != "Draft":
        return jsonify(error="Payroll run is not in draft status"), 400
    execute(
        "UPDATE payroll_runs SET status='Approved',approved_by=?,approved_at=? WHERE id=?",
        (u["id"], now(), rid),
    )
    execute(
        """INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            "Payroll",
            "Staff payroll",
            f"PAY-{rid}",
            payroll_run["net_pay"],
            date.today().isoformat(),
            "Scheduled",
            "Bank transfer",
            rid,
            u["id"],
            now(),
        ),
    )
    audit("Approved", "payroll", rid)
    return jsonify(ok=True)


@app.post("/api/payroll/<int:rid>/pay")
@login_required
@manager_required
def pay_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q(
        "SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",
        (rid, u["organisation_id"], s["id"]),
        True,
    )
    if not payroll_run or payroll_run["status"] != "Approved":
        return jsonify(error="Payroll must be approved first"), 400
    payment_time = now()
    execute("UPDATE payroll_runs SET status='Paid',paid_at=? WHERE id=?", (payment_time, rid))
    execute(
        "UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Payroll'",
        (payment_time, rid),
    )
    audit("Recorded paid", "payroll", rid)
    return jsonify(ok=True)


@app.get("/staff-onboarding/<token>")
def staff_onboarding_portal(token):
    ob = q("""SELECT eo.*,e.name,e.email,e.department,e.job_title,s.name site_name,o.name organisation_name
              FROM employee_onboarding eo JOIN employees e ON e.id=eo.employee_id
              JOIN sites s ON s.id=eo.site_id JOIN organisations o ON o.id=eo.organisation_id
              WHERE eo.token=? AND COALESCE(eo.revoked_at,'')=''""", (token,), True)
    if not ob:
        return "This onboarding link is invalid or has been withdrawn.", 404
    employee = q("SELECT * FROM employees WHERE id=?", (ob["employee_id"],), True)
    ensure_training_assignments(employee)
    training = q("SELECT * FROM employee_training WHERE employee_id=? ORDER BY id", (ob["employee_id"],))
    modules=[]
    for row in training:
        x=dict(row); definition=TRAINING_MODULES.get(x["module_key"],{})
        x["title"]=definition.get("title",x["module_key"]); x["summary"]=definition.get("summary","")
        x["lessons"]=definition.get("lessons",[]); x["questions"]=definition.get("questions",[])
        modules.append(x)
    return render_template("staff_onboarding.html", onboarding=dict(ob), modules=modules)


@app.post("/staff-onboarding/<token>/details")
def staff_onboarding_details(token):
    ob=q("SELECT * FROM employee_onboarding WHERE token=? AND COALESCE(revoked_at,'')=''",(token,),True)
    if not ob: return jsonify(error="Invalid onboarding link"),404
    d=request.get_json() or request.form
    required=["date_of_birth","address","ni_number","bank_account_name","bank_sort_code","bank_account_number","emergency_name","emergency_phone"]
    if any(not str(d.get(k) or "").strip() for k in required): return jsonify(error="Complete all required personal and payroll fields"),400
    ni=''.join(str(d.get("ni_number") or '').upper().split())
    if len(ni)<8 or len(ni)>9: return jsonify(error="Check the National Insurance number"),400
    sort=''.join(ch for ch in str(d.get("bank_sort_code") or '') if ch.isdigit())
    account=''.join(ch for ch in str(d.get("bank_account_number") or '') if ch.isdigit())
    if len(sort)!=6 or len(account)!=8: return jsonify(error="Enter a 6-digit sort code and 8-digit account number"),400
    execute("""UPDATE employee_onboarding SET status='Details complete',personal_completed_at=?,date_of_birth_enc=?,address_enc=?,ni_number_enc=?,
               bank_account_name_enc=?,bank_sort_code_enc=?,bank_account_number_enc=?,emergency_name_enc=?,emergency_phone_enc=?,
               starter_declaration=?,p45_status=?,student_loan=?,privacy_ack_at=? WHERE id=?""",
            (now(),encrypt_staff(d.get("date_of_birth")),encrypt_staff(d.get("address")),encrypt_staff(ni),encrypt_staff(d.get("bank_account_name")),
             encrypt_staff(sort),encrypt_staff(account),encrypt_staff(d.get("emergency_name")),encrypt_staff(d.get("emergency_phone")),
             str(d.get("starter_declaration") or ''),str(d.get("p45_status") or ''),str(d.get("student_loan") or ''),now(),ob["id"]))
    execute("UPDATE employees SET onboarding_status='Details complete' WHERE id=?",(ob["employee_id"],))
    return jsonify(ok=True)


@app.post("/staff-onboarding/<token>/training/<module_key>")
def staff_training_submit(token,module_key):
    ob=q("SELECT * FROM employee_onboarding WHERE token=? AND COALESCE(revoked_at,'')=''",(token,),True)
    module=TRAINING_MODULES.get(module_key)
    if not ob or not module: return jsonify(error="Training module not found"),404
    assignment=q("SELECT * FROM employee_training WHERE employee_id=? AND module_key=? AND module_version=?",(ob["employee_id"],module_key,module["version"]),True)
    if not assignment: return jsonify(error="Training is not assigned"),404
    d=request.get_json() or {}; answers=d.get("answers") or []
    if len(answers)!=len(module["questions"]): return jsonify(error="Answer every question"),400
    correct=sum(1 for i,qx in enumerate(module["questions"]) if str(answers[i])==str(qx["answer"]))
    score=round(correct/len(module["questions"])*100,1); passed=score>=80
    completed=now() if passed else ''
    due=(date.today()+timedelta(days=int(module["refresh_months"]*30.4375))).isoformat() if passed else ''
    execute("UPDATE employee_training SET attempts=attempts+1,score=?,status=?,completed_at=?,refresher_due=? WHERE id=?",
            (score,"Complete" if passed else "Retry required",completed,due,assignment["id"]))
    rows=q("SELECT status FROM employee_training WHERE employee_id=?",(ob["employee_id"],))
    # Re-read after update before deciding overall completion.
    rows=q("SELECT status FROM employee_training WHERE employee_id=?",(ob["employee_id"],))
    all_done=bool(rows) and all(x["status"]=="Complete" for x in rows)
    if all_done:
        execute("UPDATE employees SET training_status='Complete' WHERE id=?",(ob["employee_id"],))
        execute("UPDATE employee_onboarding SET training_completed_at=?,status=CASE WHEN personal_completed_at<>'' THEN 'Training complete' ELSE status END WHERE id=?",(now(),ob["id"]))
    else:
        execute("UPDATE employees SET training_status='In progress' WHERE id=?",(ob["employee_id"],))
    return jsonify(ok=True,passed=passed,score=score,correct=correct,total=len(module["questions"]))


@app.get("/api/staff-onboarding")
@login_required
@manager_required
def staff_onboarding_dashboard():
    u,s=user(),current_site()
    rows=q("""SELECT e.id,e.name,e.email,e.department,e.job_title,e.onboarding_status,e.training_status,
              eo.status,eo.invited_at,eo.personal_completed_at,eo.training_completed_at,eo.completed_at,
              eo.right_to_work_status,eo.right_to_work_checked_at,eo.right_to_work_reference
              FROM employees e LEFT JOIN employee_onboarding eo ON eo.employee_id=e.id
              WHERE e.organisation_id=? AND e.site_id=? AND e.active=1 ORDER BY e.name""",(u["organisation_id"],s["id"]))
    out=[]
    for row in rows:
        x=dict(row); tr=q("SELECT module_key,module_version,status,score,attempts,completed_at,refresher_due FROM employee_training WHERE employee_id=? ORDER BY id",(x["id"],))
        x["training"]=[{**dict(t),"title":TRAINING_MODULES.get(t["module_key"],{}).get("title",t["module_key"])} for t in tr]
        x["complete_modules"]=sum(1 for t in tr if t["status"]=="Complete"); x["total_modules"]=len(tr)
        out.append(x)
    return jsonify(employees=out)


@app.post("/api/staff-onboarding/<int:employee_id>/resend")
@login_required
@manager_required
def staff_onboarding_resend(employee_id):
    u,s=user(),current_site(); emp=q("SELECT * FROM employees WHERE id=? AND organisation_id=? AND site_id=?",(employee_id,u["organisation_id"],s["id"]),True)
    if not emp or not emp.get("email"): return jsonify(error="Employee email is required"),400
    ob=q("SELECT * FROM employee_onboarding WHERE employee_id=?",(employee_id,),True)
    if not ob:
        token=secrets.token_urlsafe(32); execute("INSERT INTO employee_onboarding(organisation_id,site_id,employee_id,token,status,invited_at) VALUES(?,?,?,?,?,?)",(u["organisation_id"],s["id"],employee_id,token,"Invited",now())); ob=q("SELECT * FROM employee_onboarding WHERE employee_id=?",(employee_id,),True)
    ensure_training_assignments(emp); link=_public_base_url()+"/staff-onboarding/"+ob["token"]
    result=send_alport_email(emp["email"],f"Complete your onboarding - {s['name']}",f"Hello {emp['name']},\n\nPlease complete your Alport staff onboarding and compulsory induction training here:\n\n{link}\n\nDo not forward this secure link.")
    if not result.get("ok"): return jsonify(error=result.get("error") or "Email could not be sent"),502
    execute("UPDATE employees SET onboarding_status='Invited' WHERE id=?",(employee_id,)); return jsonify(ok=True)


@app.post("/api/staff-onboarding/<int:employee_id>/right-to-work")
@login_required
@manager_required
def staff_right_to_work(employee_id):
    u,s=user(),current_site(); d=request.get_json() or {}
    ob=q("""SELECT eo.* FROM employee_onboarding eo JOIN employees e ON e.id=eo.employee_id
            WHERE e.id=? AND e.organisation_id=? AND e.site_id=?""",(employee_id,u["organisation_id"],s["id"]),True)
    if not ob: return jsonify(error="Onboarding record not found"),404
    status=str(d.get("status") or "Pending"); allowed=("Pending","Checked - unrestricted","Checked - time limited","Follow-up required")
    if status not in allowed: return jsonify(error="Invalid right-to-work status"),400
    checked_at=now() if status.startswith("Checked") else ''
    execute("UPDATE employee_onboarding SET right_to_work_status=?,right_to_work_checked_at=?,right_to_work_checked_by=?,right_to_work_reference=? WHERE id=?",
            (status,checked_at,u["id"] if checked_at else None,str(d.get("reference") or ''),ob["id"]))
    return jsonify(ok=True)


@app.get("/api/stocktakes")
@login_required
def stocktakes_list():
    u,s=user(),current_site(); rows=q("""SELECT st.*,COALESCE(SUM(ABS(sl.variance_value)),0) variance_value
      FROM stocktakes st LEFT JOIN stocktake_lines sl ON sl.stocktake_id=st.id WHERE st.organisation_id=? AND st.site_id=?
      GROUP BY st.id ORDER BY st.id DESC LIMIT 30""",(u["organisation_id"],s["id"]))
    return jsonify(stocktakes=[dict(x) for x in rows])


@app.post("/api/stocktakes")
@login_required
@manager_required
def stocktake_start():
    u,s=user(),current_site()
    if q("SELECT id FROM stocktakes WHERE organisation_id=? AND site_id=? AND status='Open'",(u["organisation_id"],s["id"]),True): return jsonify(error="Finish the open stocktake first"),400
    sid=execute("INSERT INTO stocktakes(organisation_id,site_id,stocktake_date,status,started_by,started_at) VALUES(?,?,?,?,?,?)",(u["organisation_id"],s["id"],date.today().isoformat(),"Open",u["id"],now()))
    items=q("SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",(u["organisation_id"],s["id"]))
    for item in items: execute("INSERT INTO stocktake_lines(stocktake_id,stock_item_id,expected_quantity,unit_cost) VALUES(?,?,?,?)",(sid,item["id"],item["on_hand"],item["unit_cost"]))
    audit("Started","stocktake",sid,f"{len(items)} stock lines"); return jsonify(ok=True,id=sid)


@app.get("/api/stocktakes/<int:stocktake_id>")
@login_required
def stocktake_detail(stocktake_id):
    u,s=user(),current_site(); st=q("SELECT * FROM stocktakes WHERE id=? AND organisation_id=? AND site_id=?",(stocktake_id,u["organisation_id"],s["id"]),True)
    if not st: return jsonify(error="Stocktake not found"),404
    lines=q("""SELECT sl.*,si.name,si.category,si.unit,si.supplier FROM stocktake_lines sl JOIN stock_items si ON si.id=sl.stock_item_id
               WHERE sl.stocktake_id=? ORDER BY si.category,si.name""",(stocktake_id,))
    return jsonify(stocktake=dict(st),lines=[dict(x) for x in lines])


@app.post("/api/stocktakes/<int:stocktake_id>/count")
@login_required
@manager_required
def stocktake_count(stocktake_id):
    u,s=user(),current_site(); st=q("SELECT * FROM stocktakes WHERE id=? AND organisation_id=? AND site_id=? AND status='Open'",(stocktake_id,u["organisation_id"],s["id"]),True)
    if not st: return jsonify(error="Open stocktake not found"),404
    d=request.get_json() or {}; line_id=int(d.get("line_id") or 0); counted=float(d.get("counted_quantity"))
    if counted<0: return jsonify(error="Count cannot be negative"),400
    line=q("SELECT * FROM stocktake_lines WHERE id=? AND stocktake_id=?",(line_id,stocktake_id),True)
    if not line: return jsonify(error="Stocktake line not found"),404
    variance=counted-float(line["expected_quantity"]); value=variance*float(line["unit_cost"])
    execute("UPDATE stocktake_lines SET counted_quantity=?,variance_quantity=?,variance_value=?,counted_at=? WHERE id=?",(counted,variance,value,now(),line_id)); return jsonify(ok=True)


@app.post("/api/stocktakes/<int:stocktake_id>/complete")
@login_required
@manager_required
def stocktake_complete(stocktake_id):
    u,s=user(),current_site(); st=q("SELECT * FROM stocktakes WHERE id=? AND organisation_id=? AND site_id=? AND status='Open'",(stocktake_id,u["organisation_id"],s["id"]),True)
    if not st: return jsonify(error="Open stocktake not found"),404
    lines=q("SELECT * FROM stocktake_lines WHERE stocktake_id=?",(stocktake_id,))
    if any(x["counted_quantity"] is None for x in lines): return jsonify(error="Count every stock item before completing the stocktake"),400
    for line in lines:
        execute("UPDATE stock_items SET on_hand=? WHERE id=?",(line["counted_quantity"],line["stock_item_id"]))
        if abs(float(line["variance_quantity"]))>0.000001:
            execute("INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (u["organisation_id"],s["id"],line["stock_item_id"],line["variance_quantity"],"Stocktake variance",f"Stocktake #{stocktake_id}",now()))
    execute("UPDATE stocktakes SET status='Complete',completed_by=?,completed_at=? WHERE id=?",(u["id"],now(),stocktake_id)); audit("Completed","stocktake",stocktake_id); return jsonify(ok=True)


@app.get("/api/stock")
@login_required
def get_stock():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(stock=[dict(x) for x in result])


@app.post("/api/stock")
@login_required
@manager_required
def add_stock():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Stock item name required"), 400
    try:
        on_hand = float(d.get("on_hand", 0))
        par = float(d.get("par_level", 0))
        cost = float(d.get("unit_cost", 0))
    except Exception:
        return jsonify(error="Invalid stock values"), 400
    if on_hand < 0 or par < 0 or cost < 0:
        return jsonify(error="Stock values cannot be negative"), 400
    item_id = execute(
        """INSERT INTO stock_items(organisation_id,site_id,name,category,unit,on_hand,par_level,unit_cost,supplier)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            name,
            d.get("category") or "Food",
            d.get("unit") or "unit",
            on_hand,
            par,
            cost,
            d.get("supplier") or "",
        ),
    )
    audit("Created", "stock_item", item_id, name)
    return jsonify(ok=True, id=item_id)


@app.post("/api/stock/<int:iid>/movement")
@login_required
@manager_required
def stock_move(iid):
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        quantity = float(d.get("quantity", 0))
    except Exception:
        return jsonify(error="Invalid quantity"), 400
    if quantity == 0:
        return jsonify(error="Quantity cannot be zero"), 400
    item = q(
        "SELECT * FROM stock_items WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not item:
        return jsonify(error="Stock item not found"), 404
    new_quantity = max(0, item["on_hand"] + quantity)
    execute("UPDATE stock_items SET on_hand=? WHERE id=?", (new_quantity, iid))
    execute(
        """INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            iid,
            quantity,
            d.get("movement_type") or "Adjustment",
            d.get("note") or "",
            now(),
        ),
    )
    audit("Updated", "stock_item", iid, f"Movement {quantity}")
    return jsonify(ok=True, on_hand=new_quantity)


@app.get("/api/suppliers")
@login_required
def get_suppliers():
    u = user()
    result = q(
        "SELECT * FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name",
        (u["organisation_id"],),
    )
    return jsonify(suppliers=[dict(x) for x in result])


@app.post("/api/suppliers")
@login_required
@manager_required
def add_supplier():
    u = user()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Supplier name required"), 400
    try:
        terms = int(d.get("payment_terms", 30))
    except Exception:
        return jsonify(error="Invalid payment terms"), 400
    if terms < 0:
        return jsonify(error="Payment terms cannot be negative"), 400
    supplier_id = execute(
        """INSERT INTO suppliers(organisation_id,name,contact,email,phone,payment_terms)
           VALUES(?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            name,
            d.get("contact") or "",
            d.get("email") or "",
            d.get("phone") or "",
            terms,
        ),
    )
    audit("Created", "supplier", supplier_id, name)
    return jsonify(ok=True, id=supplier_id)


@app.get("/api/menu")
@login_required
def get_menu():
    u, s = user(), current_site()
    items = q(
        "SELECT * FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY menu_type,menu_section,name",
        (u["organisation_id"], s["id"]),
    )
    component_rows = q(
        """SELECT * FROM menu_components WHERE organisation_id=? AND site_id=?
           ORDER BY menu_item_id,sort_order,id""",
        (u["organisation_id"], s["id"]),
    )
    grouped = {}
    for row in component_rows:
        grouped.setdefault(int(row["menu_item_id"]), []).append(dict(row))
    menu = []
    for row in items:
        item = dict(row)
        components = grouped.get(int(item["id"]), [])
        recipe_cost = round(sum(float(x.get("component_cost") or 0) for x in components), 2) if components else round(float(item.get("recipe_cost") or 0), 2)
        selling = float(item.get("selling_price") or 0)
        gp_value = round(selling - recipe_cost, 2)
        gp_percent = round((gp_value / selling * 100), 2) if selling else 0
        item["recipe_cost"] = recipe_cost
        item["gp_value"] = gp_value
        item["gp_percent"] = gp_percent
        item["components"] = components
        menu.append(item)
    return jsonify(menu=menu)


def _normalise_menu_components(raw):
    if not isinstance(raw, list):
        return []
    components = []
    for i, part in enumerate(raw[:100]):
        name = str((part or {}).get("name") or (part or {}).get("component_name") or "").strip()[:160]
        if not name:
            continue
        try:
            quantity = float((part or {}).get("quantity") or 0)
            component_cost = float((part or {}).get("component_cost") or 0)
        except Exception:
            raise ValueError("Ingredient quantities and costs must be valid numbers")
        if quantity < 0 or component_cost < 0:
            raise ValueError("Ingredient quantities and costs cannot be negative")
        unit = str((part or {}).get("unit") or "g").strip()[:30]
        notes = str((part or {}).get("notes") or "").strip()[:500]
        components.append({
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "component_cost": round(component_cost, 4),
            "notes": notes,
            "sort_order": i,
        })
    return components


def _validate_menu_category(menu_type, menu_section):
    allowed = {
        "Food": ("Starters", "Mains", "Desserts"),
        "Drink": ("Beer", "Wine", "Spirits", "Cocktails", "Soft Drinks"),
    }
    if menu_type not in allowed:
        raise ValueError("Menu type must be Food or Drink")
    if menu_section not in allowed[menu_type]:
        raise ValueError("Invalid menu section for the selected menu type")


@app.post("/api/menu")
@login_required
@manager_required
def add_menu():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()[:160]
    menu_type = (d.get("menu_type") or "Food").strip()
    menu_section = (d.get("menu_section") or ("Mains" if menu_type == "Food" else "Beer")).strip()
    if not name:
        return jsonify(error="Menu item name required"), 400
    try:
        _validate_menu_category(menu_type, menu_section)
        price = float(d.get("selling_price", 0))
        portion_weight = float(d.get("portion_weight", 0) or 0)
        components = _normalise_menu_components(d.get("components") or [])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="Invalid menu values"), 400
    if price <= 0 or portion_weight < 0:
        return jsonify(error="Selling price must be positive and portion weight cannot be negative"), 400
    recipe_cost = round(sum(x["component_cost"] for x in components), 2)
    category = menu_section
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO menu_items(organisation_id,site_id,name,category,menu_type,menu_section,portion_weight,portion_unit,selling_price,recipe_cost)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (u["organisation_id"], s["id"], name, category, menu_type, menu_section, portion_weight, (d.get("portion_unit") or "g")[:30], price, recipe_cost),
            )
            menu_id = cur.fetchone()["id"]
            for part in components:
                cur.execute(
                    """INSERT INTO menu_components(organisation_id,site_id,menu_item_id,component_name,quantity,unit,component_cost,notes,sort_order)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (u["organisation_id"], s["id"], menu_id, part["name"], part["quantity"], part["unit"], part["component_cost"], part["notes"], part["sort_order"]),
                )
    audit("Created", "menu_item", menu_id, f"{menu_type} / {menu_section} / {name}")
    return jsonify(ok=True, id=menu_id, recipe_cost=recipe_cost)


@app.put("/api/menu/<int:menu_id>")
@login_required
@manager_required
def update_menu(menu_id):
    u, s = user(), current_site()
    existing = q(
        "SELECT * FROM menu_items WHERE id=? AND organisation_id=? AND site_id=? AND active=1",
        (menu_id, u["organisation_id"], s["id"]),
        True,
    )
    if not existing:
        return jsonify(error="Menu item not found"), 404
    d = request.get_json() or {}
    name = (d.get("name") or existing["name"]).strip()[:160]
    menu_type = (d.get("menu_type") or existing.get("menu_type") or "Food").strip()
    menu_section = (d.get("menu_section") or existing.get("menu_section") or existing.get("category") or "Mains").strip()
    try:
        _validate_menu_category(menu_type, menu_section)
        price = float(d.get("selling_price", existing["selling_price"]))
        portion_weight = float(d.get("portion_weight", existing.get("portion_weight") or 0) or 0)
        components = _normalise_menu_components(d.get("components") or [])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="Invalid menu values"), 400
    if price <= 0 or portion_weight < 0:
        return jsonify(error="Selling price must be positive and portion weight cannot be negative"), 400
    recipe_cost = round(sum(x["component_cost"] for x in components), 2)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE menu_items SET name=%s,category=%s,menu_type=%s,menu_section=%s,portion_weight=%s,portion_unit=%s,selling_price=%s,recipe_cost=%s
                   WHERE id=%s AND organisation_id=%s AND site_id=%s""",
                (name, menu_section, menu_type, menu_section, portion_weight, (d.get("portion_unit") or existing.get("portion_unit") or "g")[:30], price, recipe_cost, menu_id, u["organisation_id"], s["id"]),
            )
            cur.execute("DELETE FROM menu_components WHERE menu_item_id=%s AND organisation_id=%s AND site_id=%s", (menu_id, u["organisation_id"], s["id"]))
            for part in components:
                cur.execute(
                    """INSERT INTO menu_components(organisation_id,site_id,menu_item_id,component_name,quantity,unit,component_cost,notes,sort_order)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (u["organisation_id"], s["id"], menu_id, part["name"], part["quantity"], part["unit"], part["component_cost"], part["notes"], part["sort_order"]),
                )
    audit("Updated", "menu_item", menu_id, f"{menu_type} / {menu_section} / {name}")
    return jsonify(ok=True, id=menu_id, recipe_cost=recipe_cost)


@app.delete("/api/menu/<int:menu_id>")
@login_required
@manager_required
def archive_menu(menu_id):
    u, s = user(), current_site()
    existing = q("SELECT * FROM menu_items WHERE id=? AND organisation_id=? AND site_id=?", (menu_id, u["organisation_id"], s["id"]), True)
    if not existing:
        return jsonify(error="Menu item not found"), 404
    execute("UPDATE menu_items SET active=0 WHERE id=? AND organisation_id=? AND site_id=?", (menu_id, u["organisation_id"], s["id"]))
    audit("Archived", "menu_item", menu_id, existing["name"])
    return jsonify(ok=True)


@app.get("/api/budget")
@login_required
def get_budget():
    u, s = user(), current_site()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    budget = q(
        "SELECT * FROM budgets WHERE organisation_id=? AND site_id=? AND month=?",
        (u["organisation_id"], s["id"], month),
        True,
    )
    return jsonify(budget=dict(budget) if budget else None, month=month)


@app.post("/api/budget")
@login_required
@manager_required
def save_budget():
    u, s = user(), current_site()
    d = request.get_json() or {}
    month = d.get("month") or date.today().strftime("%Y-%m")
    try:
        values = [
            float(d.get(k, 0))
            for k in ("revenue", "food_cost", "drink_cost", "labour", "overheads")
        ]
    except Exception:
        return jsonify(error="Invalid budget values"), 400
    if any(v < 0 for v in values):
        return jsonify(error="Budget values cannot be negative"), 400
    revenue, food_cost, drink_cost, labour, overheads = values
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO budgets(organisation_id,site_id,month,revenue,food_cost,drink_cost,labour,overheads)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(site_id,month) DO UPDATE SET
                   revenue=EXCLUDED.revenue,food_cost=EXCLUDED.food_cost,drink_cost=EXCLUDED.drink_cost,
                   labour=EXCLUDED.labour,overheads=EXCLUDED.overheads""",
                (
                    u["organisation_id"],
                    s["id"],
                    month,
                    revenue,
                    food_cost,
                    drink_cost,
                    labour,
                    overheads,
                ),
            )
    audit("Updated", "budget", None, month)
    return jsonify(ok=True)


@app.get("/api/audit")
@login_required
@manager_required
def audit_log():
    u = user()
    result = q(
        "SELECT * FROM audit_log WHERE organisation_id=? ORDER BY id DESC LIMIT 100",
        (u["organisation_id"],),
    )
    return jsonify(log=[dict(x) for x in result])




# -----------------------------------------------------------------------------
# EHO / FOOD-SAFETY COMPLIANCE (ENGLAND - SFBB/HACCP SUPPORT)
# -----------------------------------------------------------------------------
OPENING_CHECKS = [
    "Fridges, chilled display equipment and freezers are working properly",
    "Other equipment needed for safe food preparation is working properly",
    "Staff are fit for work and wearing clean work clothes",
    "Food preparation areas, equipment and utensils are clean and disinfected",
    "Areas are free from evidence of pest activity",
    "Handwashing and cleaning materials are available",
    "Hot running water is available at sinks and hand wash basins",
    "Probe thermometer is working and cleaning/disinfection materials are available",
    "Allergen information is accurate for all food currently on sale",
    "Required cleaning has been completed according to the cleaning schedule",
]
CLOSING_CHECKS = [
    "Food is covered, labelled and stored correctly",
    "Food on its Use By date that has not been safely used or frozen has been disposed of",
    "Cleaning equipment has been cleaned or disposed of as appropriate",
    "Waste has been removed and bins relined",
    "Food preparation areas, equipment and utensils are clean and disinfected",
    "Washing up has been completed",
    "Floors are swept and clean",
    "Required Prove It / food-safety checks have been recorded",
    "Required cleaning has been completed according to the cleaning schedule",
]
EHO_CATEGORIES = (
    "Cleaning", "Pest control", "Delivery", "Allergen", "Training", "Probe calibration",
    "Maintenance", "HACCP/SFBB", "EHO inspection", "Document", "Waste", "Fit to work", "Other"
)
ALLERGENS_14 = [
    "Celery", "Cereals containing gluten", "Crustaceans", "Eggs", "Fish", "Lupin", "Milk",
    "Molluscs", "Mustard", "Nuts", "Peanuts", "Sesame", "Soya", "Sulphur dioxide and sulphites"
]
TEMP_PRESETS = {
    "Chilled storage": {"max": 8.0, "note": "Legal maximum for foods subject to chill holding requirements in England; Alport Hospitality Solutions recommends operating fridges at 5Â°C or below."},
    "Freezer": {"max": -18.0, "note": "FSA recommended operating target for frozen food; set your documented safe method if different."},
    "Hot holding": {"min": 63.0, "note": "Legal hot-holding minimum in England, subject to applicable exemptions/time controls."},
    "Cooking": {"min": 70.0, "note": "Default Alport Hospitality Solutions verification target only. Record the time/temperature combination required by your documented safe method."},
    "Reheating": {"min": 70.0, "note": "Default Alport Hospitality Solutions verification target only. Food must be reheated thoroughly; use the limit in your documented safe method."},
    "Delivery chilled": {"max": 8.0, "note": "Use supplier/product requirements where stricter; foods subject to chill holding requirements must remain at 8Â°C or below."},
    "Cooling": {"note": "No single universal statutory endpoint is imposed here; record the method and target in your HACCP/SFBB safe method."},
    "Other": {"note": "Use the limits defined in your site food-safety management system."},
}


def _eho_scope():
    u, s = user(), current_site()
    return u, s


def _json_load(value, fallback=None):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {} if fallback is None else fallback


@app.get("/api/eho/overview")
@login_required
def eho_overview():
    u, site = _eho_scope()
    today = date.today().isoformat()
    opening = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND check_type='Opening' ORDER BY id DESC LIMIT 1", (u["organisation_id"], site["id"], today), True)
    closing = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND check_type='Closing' ORDER BY id DESC LIMIT 1", (u["organisation_id"], site["id"], today), True)
    temp_today = q("SELECT COUNT(*) AS n FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date=?", (u["organisation_id"], site["id"], today), True)["n"]
    breaches = q("SELECT COUNT(*) AS n FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND result='Action required'", (u["organisation_id"], site["id"], today), True)["n"]
    open_actions = q("SELECT COUNT(*) AS n FROM eho_records WHERE organisation_id=? AND site_id=? AND status IN ('Action required','Open','Overdue')", (u["organisation_id"], site["id"]), True)["n"]
    last_review = q("SELECT * FROM eho_four_week_reviews WHERE organisation_id=? AND site_id=? ORDER BY review_date DESC,id DESC LIMIT 1", (u["organisation_id"], site["id"]), True)
    review_due = True
    if last_review:
        d = _parse_iso_date(last_review.get("review_date"))
        review_due = not d or date.today() >= d + timedelta(days=28)
    completed = int(bool(opening)) + int(bool(closing))
    daily_percent = int(round(completed / 2 * 100))
    recent = q("SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? ORDER BY record_date DESC,id DESC LIMIT 12", (u["organisation_id"], site["id"]))
    return jsonify(
        date=today,
        opening_complete=bool(opening), closing_complete=bool(closing), daily_percent=daily_percent,
        temperature_checks=int(temp_today or 0), temperature_breaches=int(breaches or 0), open_actions=int(open_actions or 0),
        four_week_review_due=review_due, last_review=dict(last_review) if last_review else None,
        opening_items=OPENING_CHECKS, closing_items=CLOSING_CHECKS, allergens=ALLERGENS_14,
        temperature_presets=TEMP_PRESETS, categories=EHO_CATEGORIES,
        recent=[({**dict(x), "details": "Restricted manager record", "corrective_action": ""} if x.get("category") == "Fit to work" and u["role"] not in ("Owner","Admin","General Manager","Manager") else dict(x)) for x in recent],
    )


@app.post("/api/eho/daily-check")
@login_required
def save_eho_daily_check():
    u, site = _eho_scope()
    d = request.get_json() or {}
    check_type = str(d.get("check_type") or "").strip().title()
    if check_type not in ("Opening", "Closing"):
        return jsonify(error="Check type must be Opening or Closing"), 400
    expected = OPENING_CHECKS if check_type == "Opening" else CLOSING_CHECKS
    answers = d.get("answers") or {}
    if not isinstance(answers, dict):
        return jsonify(error="Invalid checklist answers"), 400
    missing = [item for item in expected if item not in answers]
    if missing:
        return jsonify(error="Every checklist item must be answered"), 400
    failed = [item for item in expected if answers.get(item) is not True]
    problems = str(d.get("problems") or "").strip()
    corrective = str(d.get("corrective_action") or "").strip()
    if failed and (not problems or not corrective):
        return jsonify(error="For any failed check, record the problem and corrective action before signing"), 400
    row_id = execute(
        """INSERT INTO eho_daily_checks(organisation_id,site_id,check_date,check_type,answers_json,problems,corrective_action,signed_by,signed_name,completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"], site["id"], date.today().isoformat(), check_type, json.dumps(answers), problems, corrective, u["id"], u["name"], now()),
    )
    if failed:
        execute(
            """INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,created_by,created_name,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"], site["id"], "HACCP/SFBB", date.today().isoformat(), f"{check_type} check exception", "Action required", "; ".join(failed) + (f" | {problems}" if problems else ""), corrective, u["id"], u["name"], now()),
        )
    audit("Completed", "eho_daily_check", row_id, f"{check_type} checks signed by {u['name']}")
    return jsonify(ok=True, id=row_id, failed=failed)


@app.get("/api/eho/daily-checks")
@login_required
def get_eho_daily_checks():
    u, site = _eho_scope()
    start = request.args.get("start") or (date.today() - timedelta(days=28)).isoformat()
    end = request.args.get("end") or date.today().isoformat()
    rows = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,id DESC", (u["organisation_id"], site["id"], start, end))
    out=[]
    for r in rows:
        x=dict(r); x["answers"]=_json_load(x.pop("answers_json", "{}")); out.append(x)
    return jsonify(checks=out)


@app.post("/api/eho/temperature")
@login_required
def save_eho_temperature():
    u, site = _eho_scope()
    d=request.get_json() or {}
    check_type=str(d.get("check_type") or "Other").strip()
    item=str(d.get("item") or "").strip()
    if not item:
        return jsonify(error="Enter the fridge, food, delivery or equipment checked"),400
    try:
        temp=float(d.get("temperature"))
        target_min=None if d.get("target_min") in (None,"") else float(d.get("target_min"))
        target_max=None if d.get("target_max") in (None,"") else float(d.get("target_max"))
    except Exception:
        return jsonify(error="Enter a valid temperature and limits"),400
    preset=TEMP_PRESETS.get(check_type,{})
    if target_min is None and preset.get("min") is not None: target_min=float(preset["min"])
    if target_max is None and preset.get("max") is not None: target_max=float(preset["max"])
    result="OK"
    if (target_min is not None and temp < target_min) or (target_max is not None and temp > target_max): result="Action required"
    corrective=str(d.get("corrective_action") or "").strip()
    if result=="Action required" and not corrective:
        return jsonify(error="Temperature is outside the recorded limit. Enter the corrective action taken."),400
    check_date=str(d.get("check_date") or date.today().isoformat())
    check_time=str(d.get("check_time") or datetime.now().strftime("%H:%M"))
    row_id=execute("""INSERT INTO eho_temperature_checks(organisation_id,site_id,check_date,check_time,check_type,item,temperature,target_min,target_max,result,method,corrective_action,recorded_by,recorded_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],check_date,check_time,check_type,item,temp,target_min,target_max,result,str(d.get("method") or "").strip(),corrective,u["id"],u["name"],now()))
    if result=="Action required":
        execute("""INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,created_by,created_name,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"],site["id"],"HACCP/SFBB",check_date,f"Temperature exception: {item}","Action required",f"{check_type}: {temp}Â°C",corrective,u["id"],u["name"],now()))
    audit("Recorded", "eho_temperature", row_id, f"{check_type} Â· {item} Â· {temp}Â°C Â· {result}")
    return jsonify(ok=True,id=row_id,result=result)


@app.get("/api/eho/temperatures")
@login_required
def get_eho_temperatures():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=28)).isoformat(); end=request.args.get("end") or date.today().isoformat()
    rows=q("SELECT * FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,check_time DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    return jsonify(temperatures=[dict(x) for x in rows])


@app.post("/api/eho/record")
@login_required
def save_eho_record():
    u,site=_eho_scope(); d=request.get_json() or {}
    category=str(d.get("category") or "Other").strip()
    if category not in EHO_CATEGORIES: return jsonify(error="Invalid compliance category"),400
    title=str(d.get("title") or "").strip()
    if not title: return jsonify(error="Enter a record title"),400
    status=str(d.get("status") or "Complete").strip()
    details=str(d.get("details") or "").strip(); corrective=str(d.get("corrective_action") or "").strip()
    if status in ("Action required","Open","Overdue") and not corrective:
        return jsonify(error="Open/action-required records need a corrective action or next step"),400
    metadata=d.get("metadata") or {}
    if not isinstance(metadata,dict): metadata={}
    row_id=execute("""INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,due_date,reference,metadata_json,created_by,created_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],category,str(d.get("record_date") or date.today().isoformat()),title,status,details,corrective,str(d.get("due_date") or ""),str(d.get("reference") or ""),json.dumps(metadata),u["id"],u["name"],now()))
    audit("Recorded", "eho_record", row_id, f"{category}: {title} ({status})")
    return jsonify(ok=True,id=row_id)


@app.get("/api/eho/records")
@login_required
def get_eho_records():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=90)).isoformat(); end=request.args.get("end") or date.today().isoformat(); category=(request.args.get("category") or "").strip()
    args=[u["organisation_id"],site["id"],start,end]; sql="SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? AND record_date BETWEEN ? AND ?"
    if category: sql+=" AND category=?"; args.append(category)
    sql+=" ORDER BY record_date DESC,id DESC"
    rows=q(sql,tuple(args)); out=[]
    for r in rows:
        x=dict(r); x["metadata"]=_json_load(x.pop("metadata_json","{}"))
        if x.get("category") == "Fit to work" and u["role"] not in ("Owner","Admin","General Manager","Manager"):
            x["details"] = "Restricted manager record"; x["corrective_action"] = ""; x["reference"] = ""
        out.append(x)
    return jsonify(records=out)


@app.post("/api/eho/record/<int:record_id>/resolve")
@login_required
@manager_required
def resolve_eho_record(record_id):
    u,site=_eho_scope(); d=request.get_json() or {}
    row=q("SELECT * FROM eho_records WHERE id=? AND organisation_id=? AND site_id=?",(record_id,u["organisation_id"],site["id"]),True)
    if not row:return jsonify(error="Compliance record not found"),404
    resolution=str(d.get("resolution") or "").strip()
    if not resolution:return jsonify(error="Record how the issue was resolved"),400
    # Preserve the original record and append the resolution to the action field.
    action=(str(row.get("corrective_action") or "").strip()+" | RESOLVED: "+resolution).strip(" |")
    execute("UPDATE eho_records SET status='Complete',corrective_action=?,verified_by=?,verified_at=? WHERE id=?",(action,u["id"],now(),record_id))
    audit("Resolved", "eho_record", record_id, resolution)
    return jsonify(ok=True)


@app.post("/api/eho/record/<int:record_id>/verify")
@login_required
@manager_required
def verify_eho_record(record_id):
    u,site=_eho_scope(); row=q("SELECT * FROM eho_records WHERE id=? AND organisation_id=? AND site_id=?",(record_id,u["organisation_id"],site["id"]),True)
    if not row:return jsonify(error="Compliance record not found"),404
    execute("UPDATE eho_records SET verified_by=?,verified_at=? WHERE id=?",(u["id"],now(),record_id))
    audit("Verified", "eho_record", record_id, f"Verified by {u['name']}")
    return jsonify(ok=True)


@app.post("/api/eho/four-week-review")
@login_required
@manager_required
def save_four_week_review():
    u,site=_eho_scope(); d=request.get_json() or {}; end=_parse_iso_date(d.get("period_end")) or date.today(); start=_parse_iso_date(d.get("period_start")) or (end-timedelta(days=27))
    row_id=execute("""INSERT INTO eho_four_week_reviews(organisation_id,site_id,review_date,period_start,period_end,persistent_problems,changes_made,safe_methods_current,allergen_info_current,training_current,signed_by,signed_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],date.today().isoformat(),start.isoformat(),end.isoformat(),str(d.get("persistent_problems") or ""),str(d.get("changes_made") or ""),1 if d.get("safe_methods_current",True) else 0,1 if d.get("allergen_info_current",True) else 0,1 if d.get("training_current",True) else 0,u["id"],u["name"],now()))
    audit("Completed", "eho_four_week_review", row_id, f"4-week review {start.isoformat()} to {end.isoformat()}")
    return jsonify(ok=True,id=row_id)


@app.get("/api/eho/audit-pack")
@login_required
@manager_required
def eho_audit_pack():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=28)).isoformat(); end=request.args.get("end") or date.today().isoformat()
    daily=q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    temps=q("SELECT * FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,check_time DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    records=q("SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? AND record_date BETWEEN ? AND ? ORDER BY record_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    reviews=q("SELECT * FROM eho_four_week_reviews WHERE organisation_id=? AND site_id=? AND review_date BETWEEN ? AND ? ORDER BY review_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    daily_out=[]
    for r in daily:
        x=dict(r); x["answers"]=_json_load(x.pop("answers_json","{}")); daily_out.append(x)
    rec_out=[]
    for r in records:
        x=dict(r); x["metadata"]=_json_load(x.pop("metadata_json","{}")); rec_out.append(x)
    training=q("""SELECT e.name,e.department,e.job_title,et.module_key,et.module_version,et.status,et.score,et.completed_at,et.refresher_due
                  FROM employee_training et JOIN employees e ON e.id=et.employee_id
                  WHERE et.organisation_id=? AND et.site_id=? AND e.active=1 ORDER BY e.name,et.module_key""",(u["organisation_id"],site["id"]))
    training_out=[]
    for row in training:
        x=dict(row); x["module_title"]=TRAINING_MODULES.get(x["module_key"],{}).get("title",x["module_key"]); training_out.append(x)
    return jsonify(site=dict(site),organisation=dict(org()),start=start,end=end,daily_checks=daily_out,temperatures=[dict(x) for x in temps],records=rec_out,reviews=[dict(x) for x in reviews],training=training_out)


# -----------------------------------------------------------------------------
# ALPORT RESERVATIONS + GUEST CRM
# -----------------------------------------------------------------------------
@app.get("/api/bookings/overview")
@login_required
def bookings_overview():
    u, site = user(), current_site()
    day = request.args.get("date") or date.today().isoformat()
    rows = q("""SELECT b.*,rt.table_name FROM bookings b LEFT JOIN restaurant_tables rt ON rt.id=b.table_id
                WHERE b.organisation_id=? AND b.site_id=? AND b.booking_date=? ORDER BY b.booking_time,b.guest_name""",
             (u["organisation_id"], site["id"], day))
    active = [dict(x) for x in rows if x["status"] not in ("Cancelled", "No-show")]
    covers = sum(int(x["party_size"] or 0) for x in active)
    peak = {}
    for x in active:
        slot = str(x["booking_time"])[:5]
        peak[slot] = peak.get(slot, 0) + int(x["party_size"] or 0)
    peak_time = max(peak, key=peak.get) if peak else "â"
    return jsonify(date=day, bookings=[dict(x) for x in rows], covers=covers, booking_count=len(active), peak_time=peak_time, peak_covers=peak.get(peak_time,0) if peak else 0)



def _public_base_url():
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")


def _booking_manage_token(booking):
    token=(booking.get("manage_token") or "").strip()
    if token: return token
    token=secrets.token_urlsafe(32)
    execute("UPDATE bookings SET manage_token=? WHERE id=?",(token,booking["id"]))
    return token


def _render_booking_text(template, booking, site):
    values={
        "guest_first_name": (booking.get("guest_name") or "Guest").split()[0],
        "guest_name": booking.get("guest_name") or "Guest",
        "venue_name": site.get("name") or "our restaurant",
        "booking_date": booking.get("booking_date") or "",
        "booking_time": str(booking.get("booking_time") or "")[:5],
        "party_size": str(booking.get("party_size") or ""),
        "booking_reference": str(booking.get("id") or ""),
    }
    text=template or ""
    for k,v in values.items(): text=text.replace("{{"+k+"}}",str(v))
    return text


def _booking_email_html(site, settings, booking, heading, message, button_label="Manage reservation", button_url=""):
    logo=(settings.get("logo_url") or "").strip()
    logo_html=f'<img src="{escape(logo)}" alt="{escape(site.get("name") or "Venue")}" style="max-width:180px;max-height:70px;margin-bottom:24px">' if logo else f'<div style="font-size:22px;font-weight:800;letter-spacing:.08em;margin-bottom:24px">{escape(site.get("name") or "Venue")}</div>'
    button=f'<p style="margin:28px 0"><a href="{escape(button_url)}" style="display:inline-block;background:#1d5a47;color:white;text-decoration:none;padding:13px 20px;border-radius:8px;font-weight:700">{escape(button_label)}</a></p>' if button_url else ''
    details=f'''<div style="background:#f4f7f5;border:1px solid #dfe8e2;border-radius:12px;padding:18px;margin:24px 0"><strong>{escape(str(booking.get("booking_date") or ""))}</strong><br>{escape(str(booking.get("booking_time") or "")[:5])} &nbsp;Â·&nbsp; {escape(str(booking.get("party_size") or ""))} guests<br><span style="color:#718079">Reference #{escape(str(booking.get("id") or ""))}</span></div>'''
    return f'''<!doctype html><html><body style="margin:0;background:#f4f7f5;font-family:Arial,sans-serif;color:#17221e"><div style="max-width:620px;margin:0 auto;padding:36px 18px"><div style="background:white;border:1px solid #dfe8e2;border-radius:18px;padding:34px">{logo_html}<h1 style="font-size:26px;margin:0 0 16px">{escape(heading)}</h1><p style="font-size:16px;line-height:1.6;color:#46534e">{escape(message).replace(chr(10),'<br>')}</p>{details}{button}<p style="font-size:13px;color:#718079;margin-top:28px">{escape(settings.get("cancellation_policy") or "Please contact the restaurant if you need any assistance with your reservation.")}</p></div><p style="text-align:center;color:#89958f;font-size:11px;margin:18px">Reservation communications powered by Alport Hospitality Solutions</p></div></body></html>'''


def _deposit_amount(settings, party_size):
    if int(party_size or 0) < int(settings.get("deposit_min_party") or 1): return 0.0
    kind=(settings.get("deposit_type") or "none").lower(); value=float(settings.get("deposit_value") or 0)
    if kind=="per_person": return round(value*int(party_size),2)
    if kind=="fixed": return round(value,2)
    return 0.0


def stripe_connected_request(method,path,account_id,data=None):
    key=os.environ.get("STRIPE_SECRET_KEY","").strip()
    if not key: raise RuntimeError("Stripe is not configured")
    r=requests.request(method,"https://api.stripe.com/v1"+path,auth=(key,""),headers={"Stripe-Account":account_id},data=data or {},timeout=20)
    payload=r.json() if r.content else {}
    if not r.ok: raise RuntimeError(((payload.get("error") or {}).get("message") or "Stripe request failed"))
    return payload


@app.get("/api/bookings/communications")
@login_required
def booking_communications_get():
    u,site=user(),current_site(); return jsonify(settings=dict(booking_settings_for(u["organisation_id"],site["id"])))


@app.post("/api/bookings/communications")
@login_required
@manager_required
def booking_communications_save():
    u,site=user(),current_site(); d=request.get_json() or {}
    fields=["logo_url","confirmation_subject","confirmation_message","review_subject","review_message","cancellation_policy"]
    execute("UPDATE booking_settings SET "+",".join(f+"=?" for f in fields)+",updated_at=? WHERE organisation_id=? AND site_id=?",tuple([str(d.get(f) or "") for f in fields]+[now(),u["organisation_id"],site["id"]]))
    return jsonify(ok=True)


@app.post("/api/bookings/connect-payments")
@login_required
@manager_required
def booking_connect_payments():
    u,site=user(),current_site(); settings=booking_settings_for(u["organisation_id"],site["id"])
    account=(settings.get("stripe_connect_account_id") or "").strip()
    try:
        if not account:
            created=stripe_request("POST","/accounts",{"type":"express","country":"GB","capabilities[card_payments][requested]":"true","capabilities[transfers][requested]":"true","metadata[organisation_id]":str(u["organisation_id"]),"metadata[site_id]":str(site["id"])})
            account=created["id"]
            execute("UPDATE booking_settings SET stripe_connect_account_id=?,updated_at=? WHERE organisation_id=? AND site_id=?",(account,now(),u["organisation_id"],site["id"]))
        link=stripe_request("POST","/account_links",{"account":account,"refresh_url":_public_base_url()+"/app","return_url":_public_base_url()+"/app","type":"account_onboarding"})
        return jsonify(ok=True,url=link["url"])
    except Exception as exc: return jsonify(error=str(exc)),400


@app.get("/manage-reservation/<token>")
def manage_reservation_page(token):
    b=q("SELECT b.*,s.name site_name,s.address site_address FROM bookings b JOIN sites s ON s.id=b.site_id WHERE b.manage_token=?",(token,),True)
    if not b: return "Reservation not found",404
    settings=dict(booking_settings_for(b["organisation_id"],b["site_id"]))
    return render_template("manage_reservation.html",booking=b,settings=settings)


@app.post("/manage-reservation/<token>")
def manage_reservation_action(token):
    b=q("SELECT b.*,s.name site_name,s.address site_address FROM bookings b JOIN sites s ON s.id=b.site_id WHERE b.manage_token=?",(token,),True)
    if not b: return "Reservation not found",404
    settings=dict(booking_settings_for(b["organisation_id"],b["site_id"])); action=request.form.get("action") or "update"
    try: arrival=datetime.fromisoformat(f"{b['booking_date']}T{str(b['booking_time'])[:5]}")
    except: arrival=datetime.now()
    hours=max(0,(arrival-datetime.now()).total_seconds()/3600)
    if action=="cancel":
        if hours < int(settings.get("cancellation_notice_hours") or 0):
            return render_template("manage_reservation.html",booking=b,settings=settings,error="Online cancellation is no longer available for this booking. Please contact the restaurant directly."),400
        execute("UPDATE bookings SET status='Cancelled',cancelled_at=?,updated_at=? WHERE id=?",(now(),now(),b["id"]))
        return render_template("manage_reservation.html",booking=dict(b),settings=settings,success="Your reservation has been cancelled.")
    if hours < int(settings.get("amendment_notice_hours") or 0):
        return render_template("manage_reservation.html",booking=b,settings=settings,error="Online changes are no longer available for this booking. Please contact the restaurant directly."),400
    new_date=request.form.get("booking_date") if int(settings.get("guest_can_change_date") or 0) else b["booking_date"]
    new_time=request.form.get("booking_time") if int(settings.get("guest_can_change_time") or 0) else b["booking_time"]
    new_party=int(request.form.get("party_size") or b["party_size"]) if int(settings.get("guest_can_change_party") or 0) else int(b["party_size"])
    duration=int(b["duration_minutes"] or settings.get("default_duration") or 120)
    options=booking_available_tables(b["organisation_id"],b["site_id"],new_date,new_time,new_party,duration,exclude_booking_id=b["id"])
    if not options: return render_template("manage_reservation.html",booking=b,settings=settings,error="That change is not available. Please choose another time or contact the restaurant."),409
    table_id=options[0]["id"]
    execute("UPDATE bookings SET booking_date=?,booking_time=?,party_size=?,table_id=?,special_requests=?,dietary_requirements=?,updated_at=? WHERE id=?",(new_date,new_time,new_party,table_id,request.form.get("special_requests") or "",request.form.get("dietary_requirements") or "",now(),b["id"]))
    updated=q("SELECT b.*,s.name site_name,s.address site_address FROM bookings b JOIN sites s ON s.id=b.site_id WHERE b.id=?",(b["id"],),True)
    return render_template("manage_reservation.html",booking=updated,settings=settings,success="Your reservation has been updated.")


@app.post("/api/bookings/<int:booking_id>/deposit-checkout")
@login_required
@manager_required
def booking_deposit_checkout(booking_id):
    u,site=user(),current_site(); b=q("SELECT * FROM bookings WHERE id=? AND organisation_id=? AND site_id=?",(booking_id,u["organisation_id"],site["id"]),True)
    if not b:return jsonify(error="Booking not found"),404
    settings=dict(booking_settings_for(u["organisation_id"],site["id"])); account=(settings.get("stripe_connect_account_id") or "").strip()
    if not account:return jsonify(error="Connect the restaurant payment account first"),400
    amount=float(b.get("deposit_required") or _deposit_amount(settings,b["party_size"]))
    if amount<=0:return jsonify(error="No deposit is required for this booking"),400
    try:
        manage=_public_base_url()+"/manage-reservation/"+_booking_manage_token(b)
        data={"mode":"payment","success_url":manage+"?payment=success","cancel_url":manage,"customer_email":b.get("guest_email") or None,"line_items[0][price_data][currency]":"gbp","line_items[0][price_data][product_data][name]":f"Reservation deposit â {site['name']}","line_items[0][price_data][unit_amount]":str(int(round(amount*100))),"line_items[0][quantity]":"1","metadata[booking_id]":str(b["id"])}
        data={k:v for k,v in data.items() if v is not None}
        checkout=stripe_connected_request("POST","/checkout/sessions",account,data)
        execute("UPDATE bookings SET deposit_required=?,deposit_status='Payment requested',deposit_checkout_session_id=?,updated_at=? WHERE id=?",(amount,checkout["id"],now(),booking_id))
        return jsonify(ok=True,url=checkout["url"])
    except Exception as exc:return jsonify(error=str(exc)),400

@app.post("/api/bookings")
@login_required
@manager_required
def create_booking():
    u, site = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("guest_name") or "").strip()
    email = (d.get("guest_email") or "").strip().lower()
    phone = (d.get("guest_phone") or "").strip()
    booking_date = str(d.get("booking_date") or "")[:10]
    booking_time = str(d.get("booking_time") or "")[:5]
    try:
        party = int(d.get("party_size") or 0)
        duration = int(d.get("duration_minutes") or 0)
    except Exception:
        return jsonify(error="Invalid booking details"), 400
    settings = booking_settings_for(u["organisation_id"], site["id"])
    duration = duration or int(settings["default_duration"] or 120)
    if not name or not booking_date or not booking_time or party < 1:
        return jsonify(error="Guest, date, time and party size are required"), 400
    table_id = d.get("table_id")
    if table_id:
        valid = [x for x in booking_available_tables(u["organisation_id"],site["id"],booking_date,booking_time,party,duration) if int(x["id"])==int(table_id)]
        if not valid:
            return jsonify(error="That table is no longer available for this sitting"), 409
    else:
        options = booking_available_tables(u["organisation_id"],site["id"],booking_date,booking_time,party,duration)
        table_id = options[0]["id"] if options else None
    guest_id = upsert_guest(u["organisation_id"], name, email, phone, bool(d.get("marketing_consent")))
    manage_token=secrets.token_urlsafe(32)
    deposit_required=_deposit_amount(settings,party)
    deposit_status="Required" if deposit_required>0 else "Not required"
    booking_id = execute("""INSERT INTO bookings(organisation_id,site_id,guest_id,booking_date,booking_time,party_size,duration_minutes,status,source,table_id,guest_name,guest_email,guest_phone,special_requests,dietary_requirements,internal_notes,marketing_consent,manage_token,deposit_required,deposit_status,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (u["organisation_id"],site["id"],guest_id,booking_date,booking_time,party,duration,d.get("status") or "Confirmed",d.get("source") or "Manager",table_id,name,email,phone,d.get("special_requests") or "",d.get("dietary_requirements") or "",d.get("internal_notes") or "",1 if d.get("marketing_consent") else 0,manage_token,deposit_required,deposit_status,now(),now()))
    booking=q("SELECT * FROM bookings WHERE id=?",(booking_id,),True)
    if email and int(settings.get("confirmation_enabled") or 0):
        subject=_render_booking_text(settings.get("confirmation_subject") or "Your reservation is confirmed â {{venue_name}}",booking,site)
        message=_render_booking_text(settings.get("confirmation_message") or "Thank you for choosing {{venue_name}}. We look forward to welcoming you.",booking,site)
        manage_url=_public_base_url()+"/manage-reservation/"+manage_token
        text=f"{message}\n\n{booking_date} at {booking_time} Â· {party} guests\n\nManage reservation: {manage_url}"
        result=send_alport_email(email,subject,text,_booking_email_html(site,settings,booking,"Your reservation is confirmed",message,"Manage reservation",manage_url))
        if result["ok"]:
            execute("UPDATE bookings SET confirmation_sent_at=? WHERE id=?",(now(),booking_id))
            execute("INSERT INTO guest_communications(organisation_id,site_id,guest_id,booking_id,communication_type,recipient,subject,status,provider_id,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(u["organisation_id"],site["id"],guest_id,booking_id,"Confirmation",email,subject,"Sent",result["id"],now()))
    audit("Created","booking",booking_id,f"{name} Â· {party} Â· {booking_date} {booking_time}")
    return jsonify(ok=True,id=booking_id,table_id=table_id)


@app.patch("/api/bookings/<int:booking_id>")
@login_required
@manager_required
def update_booking(booking_id):
    u, site = user(), current_site(); d=request.get_json() or {}
    b=q("SELECT * FROM bookings WHERE id=? AND organisation_id=? AND site_id=?",(booking_id,u["organisation_id"],site["id"]),True)
    if not b:return jsonify(error="Booking not found"),404
    allowed={"status","booking_date","booking_time","party_size","duration_minutes","table_id","special_requests","dietary_requirements","internal_notes"}
    sets=[];args=[]
    for k in allowed:
        if k in d:
            sets.append(f"{k}=?");args.append(d[k] if d[k] not in ("") else None if k=="table_id" else d[k])
    if not sets:return jsonify(error="Nothing to update"),400
    sets.append("updated_at=?");args.append(now());args.extend([booking_id,u["organisation_id"],site["id"]])
    execute(f"UPDATE bookings SET {','.join(sets)} WHERE id=? AND organisation_id=? AND site_id=?",tuple(args))
    status=d.get("status")
    if status=="Arrived":execute("UPDATE bookings SET arrived_at=? WHERE id=?",(now(),booking_id))
    if status=="Seated":execute("UPDATE bookings SET seated_at=? WHERE id=?",(now(),booking_id))
    if status=="Completed":execute("UPDATE bookings SET completed_at=? WHERE id=?",(now(),booking_id))
    if status=="Cancelled":execute("UPDATE bookings SET cancelled_at=? WHERE id=?",(now(),booking_id))
    audit("Updated","booking",booking_id,str(d))
    return jsonify(ok=True)


@app.get("/api/bookings/tables")
@login_required
def booking_tables():
    u,site=user(),current_site()
    rows=q("SELECT * FROM restaurant_tables WHERE organisation_id=? AND site_id=? ORDER BY sort_order,table_name",(u["organisation_id"],site["id"]))
    return jsonify(tables=[dict(x) for x in rows])


@app.post("/api/bookings/tables")
@login_required
@manager_required
def add_booking_table():
    u,site=user(),current_site();d=request.get_json() or {}
    name=(d.get("table_name") or "").strip()
    if not name:return jsonify(error="Table name required"),400
    try: minc=max(1,int(d.get("min_capacity") or 1));maxc=max(minc,int(d.get("max_capacity") or 2))
    except:return jsonify(error="Invalid capacity"),400
    try:
        tid=execute("INSERT INTO restaurant_tables(organisation_id,site_id,table_name,area,min_capacity,max_capacity,sort_order) VALUES(?,?,?,?,?,?,?)",(u["organisation_id"],site["id"],name,d.get("area") or "Main",minc,maxc,int(d.get("sort_order") or 0)))
    except Exception:return jsonify(error="That table name already exists"),409
    return jsonify(ok=True,id=tid)


@app.delete("/api/bookings/tables/<int:table_id>")
@login_required
@manager_required
def archive_booking_table(table_id):
    u,site=user(),current_site();execute("UPDATE restaurant_tables SET active=0 WHERE id=? AND organisation_id=? AND site_id=?",(table_id,u["organisation_id"],site["id"]));return jsonify(ok=True)


@app.get("/api/bookings/settings")
@login_required
def get_booking_settings():
    u,site=user(),current_site();settings=dict(booking_settings_for(u["organisation_id"],site["id"]));sessions=q("SELECT * FROM booking_sessions WHERE organisation_id=? AND site_id=? ORDER BY day_of_week,start_time",(u["organisation_id"],site["id"]));return jsonify(settings=settings,sessions=[dict(x) for x in sessions])


@app.post("/api/bookings/settings")
@login_required
@manager_required
def save_booking_settings():
    u,site=user(),current_site();d=request.get_json() or {};booking_settings_for(u["organisation_id"],site["id"])
    fields=["booking_interval","default_duration","turnaround_minutes","max_party_size","max_covers_per_interval","min_notice_minutes","advance_days","review_delay_hours","lapsed_guest_weeks","confirmation_enabled","reminder_enabled","review_enabled","retention_enabled","cancellation_notice_hours","amendment_notice_hours","guest_can_change_date","guest_can_change_time","guest_can_change_party","large_party_threshold","deposit_type","deposit_value","deposit_min_party","cancellation_policy"]
    integer_fields={"booking_interval","default_duration","turnaround_minutes","max_party_size","max_covers_per_interval","min_notice_minutes","advance_days","review_delay_hours","lapsed_guest_weeks","confirmation_enabled","reminder_enabled","review_enabled","retention_enabled","cancellation_notice_hours","amendment_notice_hours","guest_can_change_date","guest_can_change_time","guest_can_change_party","large_party_threshold","deposit_min_party"}
    vals=[]
    for f in fields:
        if f in integer_fields: vals.append(int(d.get(f,0) or 0))
        elif f=="deposit_value": vals.append(float(d.get(f,0) or 0))
        else: vals.append(str(d.get(f) or ""))
    execute("UPDATE booking_settings SET "+",".join(f+"=?" for f in fields)+",updated_at=? WHERE organisation_id=? AND site_id=?",tuple(vals+[now(),u["organisation_id"],site["id"]]))
    return jsonify(ok=True)


@app.post("/api/bookings/sessions")
@login_required
@manager_required
def add_booking_session():
    u,site=user(),current_site();d=request.get_json() or {}
    try:dow=int(d.get("day_of_week"))
    except:return jsonify(error="Invalid day"),400
    if dow<0 or dow>6 or not d.get("start_time") or not d.get("end_time"):return jsonify(error="Day and times required"),400
    sid=execute("INSERT INTO booking_sessions(organisation_id,site_id,day_of_week,name,start_time,end_time,active) VALUES(?,?,?,?,?,?,1)",(u["organisation_id"],site["id"],dow,d.get("name") or "Service",str(d["start_time"])[:5],str(d["end_time"])[:5]))
    return jsonify(ok=True,id=sid)


@app.delete("/api/bookings/sessions/<int:session_id>")
@login_required
@manager_required
def delete_booking_session(session_id):
    u,site=user(),current_site();execute("DELETE FROM booking_sessions WHERE id=? AND organisation_id=? AND site_id=?",(session_id,u["organisation_id"],site["id"]));return jsonify(ok=True)


@app.get("/api/bookings/guests")
@login_required
def booking_guests():
    u=user();rows=q("""SELECT g.*,COUNT(b.id) visits,MAX(CASE WHEN b.status='Completed' THEN b.booking_date ELSE NULL END) last_visit,
                       SUM(CASE WHEN b.status='No-show' THEN 1 ELSE 0 END) no_shows
                       FROM guests g LEFT JOIN bookings b ON b.guest_id=g.id WHERE g.organisation_id=?
                       GROUP BY g.id ORDER BY last_visit DESC NULLS LAST,g.name""",(u["organisation_id"],));return jsonify(guests=[dict(x) for x in rows])


@app.get("/api/bookings/reports")
@login_required
def booking_reports():
    u,site=user(),current_site();period=request.args.get("period","week")
    today=date.today()
    if period=="year": start=today.replace(month=1,day=1)
    elif period=="month": start=today.replace(day=1)
    else:start=today-timedelta(days=today.weekday())
    rows=q("SELECT * FROM bookings WHERE organisation_id=? AND site_id=? AND booking_date>=? AND booking_date<=?",(u["organisation_id"],site["id"],start.isoformat(),today.isoformat()))
    active=[x for x in rows if x["status"] not in ("Cancelled","No-show")];covers=sum(int(x["party_size"] or 0) for x in active);completed=sum(1 for x in rows if x["status"]=="Completed");cancelled=sum(1 for x in rows if x["status"]=="Cancelled");noshow=sum(1 for x in rows if x["status"]=="No-show")
    feedback=q("SELECT AVG(overall_rating) avg_rating,COUNT(*) n FROM guest_feedback WHERE organisation_id=? AND site_id=? AND created_at>=?",(u["organisation_id"],site["id"],start.isoformat()),True)
    byday={}
    for x in active:byday[x["booking_date"]]=byday.get(x["booking_date"],0)+int(x["party_size"] or 0)
    return jsonify(period=period,start=start.isoformat(),end=today.isoformat(),bookings=len(active),covers=covers,completed=completed,cancelled=cancelled,no_shows=noshow,average_party=round(covers/len(active),1) if active else 0,average_rating=round(float((feedback or {}).get("avg_rating") or 0),1),feedback_count=int((feedback or {}).get("n") or 0),by_day=[{"date":k,"covers":v} for k,v in sorted(byday.items())])


@app.get("/api/bookings/rota-demand")
@login_required
def booking_rota_demand():
    u,site=user(),current_site();start=request.args.get("start") or date.today().isoformat();end=request.args.get("end") or (date.today()+timedelta(days=7)).isoformat()
    rows=q("""SELECT booking_date,booking_time,SUM(party_size) covers,COUNT(*) bookings FROM bookings WHERE organisation_id=? AND site_id=? AND booking_date>=? AND booking_date<=? AND status NOT IN ('Cancelled','No-show') GROUP BY booking_date,booking_time ORDER BY booking_date,booking_time""",(u["organisation_id"],site["id"],start,end))
    return jsonify(demand=[dict(x) for x in rows])


@app.post("/api/bookings/<int:booking_id>/send-review")
@login_required
@manager_required
def send_booking_review(booking_id):
    u,site=user(),current_site();b=q("SELECT * FROM bookings WHERE id=? AND organisation_id=? AND site_id=?",(booking_id,u["organisation_id"],site["id"]),True)
    if not b:return jsonify(error="Booking not found"),404
    if not b["guest_email"]:return jsonify(error="Guest has no email address"),400
    public_url=(os.environ.get("PUBLIC_BASE_URL") or request.url_root.rstrip("/")).rstrip("/")
    link=f"{public_url}/booking-feedback/{booking_id}"
    settings=dict(booking_settings_for(u["organisation_id"],site["id"]))
    subject=_render_booking_text(settings.get("review_subject") or "Thank you for dining with us â {{venue_name}}",b,site)
    message=_render_booking_text(settings.get("review_message") or "Thank you for joining us. We would really appreciate hearing about your experience.",b,site)
    result=send_alport_email(b["guest_email"],subject,f"{message}\n\nShare your feedback: {link}",_booking_email_html(site,settings,b,"Thank you for dining with us",message,"Share your feedback",link))
    if not result["ok"]:return jsonify(error="Review email could not be sent"),502
    execute("UPDATE bookings SET review_sent_at=? WHERE id=?",(now(),booking_id));execute("INSERT INTO guest_communications(organisation_id,site_id,guest_id,booking_id,communication_type,recipient,subject,status,provider_id,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(u["organisation_id"],site["id"],b["guest_id"],booking_id,"Review",b["guest_email"],f"How was your visit to {site['name']}?","Sent",result["id"],now()));return jsonify(ok=True)


@app.post("/api/bookings/retention/send")
@login_required
@manager_required
def send_retention_campaign():
    u,site=user(),current_site();settings=booking_settings_for(u["organisation_id"],site["id"]);weeks=int(settings.get("lapsed_guest_weeks") or 6);cutoff=(date.today()-timedelta(weeks=weeks)).isoformat()
    guests=q("""SELECT g.*,MAX(CASE WHEN b.status='Completed' THEN b.booking_date ELSE NULL END) last_visit FROM guests g LEFT JOIN bookings b ON b.guest_id=g.id WHERE g.organisation_id=? AND g.marketing_consent=1 AND COALESCE(g.marketing_opt_out_at,'')='' GROUP BY g.id HAVING MAX(CASE WHEN b.status='Completed' THEN b.booking_date ELSE NULL END) IS NOT NULL AND MAX(CASE WHEN b.status='Completed' THEN b.booking_date ELSE NULL END)<=?""",(u["organisation_id"],cutoff))
    sent=0
    for g in guests:
        if not g["email"]:continue
        result=send_alport_email(g["email"],f"We'd love to welcome you back to {site['name']}",f"Hi {g['name']},\n\nIt's been a little while since your last visit to {site['name']}. We'd love to welcome you back soon.\n\nBest wishes,\n{site['name']}")
        if result["ok"]:
            sent+=1;execute("INSERT INTO guest_communications(organisation_id,site_id,guest_id,communication_type,recipient,subject,status,provider_id,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",(u["organisation_id"],site["id"],g["id"],"Retention",g["email"],f"We'd love to welcome you back to {site['name']}","Sent",result["id"],now()))
    return jsonify(ok=True,sent=sent,eligible=len(guests))


@app.get("/booking-feedback/<int:booking_id>")
def booking_feedback_page(booking_id):
    b=q("SELECT b.*,s.name site_name FROM bookings b JOIN sites s ON s.id=b.site_id WHERE b.id=?",(booking_id,),True)
    if not b:return "Booking not found",404
    return render_template("booking_feedback.html",booking=b)


@app.post("/booking-feedback/<int:booking_id>")
def booking_feedback_submit(booking_id):
    b=q("SELECT b.*,s.name site_name FROM bookings b JOIN sites s ON s.id=b.site_id WHERE b.id=?",(booking_id,),True)
    if not b:return "Booking not found",404
    if q("SELECT id FROM guest_feedback WHERE booking_id=?",(booking_id,),True):return render_template("booking_feedback.html",booking=b,submitted=True)
    try:overall=max(1,min(5,int(request.form.get("overall_rating") or 0)))
    except:return render_template("booking_feedback.html",booking=b,error="Please choose an overall rating."),400
    def rating(name):
        try:return max(1,min(5,int(request.form.get(name) or 0))) or None
        except:return None
    execute("""INSERT INTO guest_feedback(organisation_id,site_id,booking_id,guest_id,overall_rating,food_rating,service_rating,atmosphere_rating,value_rating,comments,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(b["organisation_id"],b["site_id"],booking_id,b["guest_id"],overall,rating("food_rating"),rating("service_rating"),rating("atmosphere_rating"),rating("value_rating"),(request.form.get("comments") or "")[:3000],now()))
    manager_email=os.environ.get("SUPPORT_EMAIL","").strip()
    if manager_email:send_alport_email(manager_email,f"New guest feedback â {b['site_name']} â {overall}/5",f"Guest: {b['guest_name']}\nVisit: {b['booking_date']} {b['booking_time']}\nOverall: {overall}/5\n\n{(request.form.get('comments') or '').strip()}")
    return render_template("booking_feedback.html",booking=b,submitted=True)


@app.get("/api/health")
@app.get("/health")
def health():
    try:
        row = q("SELECT 1 AS ok", one=True)
        database_ok = bool(row and row["ok"] == 1)
    except Exception:
        database_ok = False
    return jsonify(
        status="ok" if database_ok else "database_error",
        service="Alport",
        database="PostgreSQL",
        database_connected=database_ok,
    ), (200 if database_ok else 503)


WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail"
}


def weather_payload(latitude, longitude, location_label="Your location"):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
        "forecast_days": 7,
        "timezone": "auto"
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    d = r.json()
    current = d.get("current", {})
    daily = d.get("daily", {})
    days = []
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    rain_prob = daily.get("precipitation_probability_max", [])
    rain_sum = daily.get("precipitation_sum", [])
    sunrise = daily.get("sunrise", [])
    sunset = daily.get("sunset", [])
    for i, day_value in enumerate(dates):
        code = codes[i] if i < len(codes) else None
        days.append({
            "date": day_value,
            "code": code,
            "description": WEATHER_CODES.get(code, "Weather"),
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
            "rain_probability": rain_prob[i] if i < len(rain_prob) else None,
            "rain_mm": rain_sum[i] if i < len(rain_sum) else None,
            "sunrise": sunrise[i] if i < len(sunrise) else None,
            "sunset": sunset[i] if i < len(sunset) else None
        })
    return {
        "location": location_label,
        "timezone": d.get("timezone"),
        "latitude": latitude,
        "longitude": longitude,
        "current": {
            "temperature": current.get("temperature_2m"),
            "apparent_temperature": current.get("apparent_temperature"),
            "wind_speed": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation"),
            "code": current.get("weather_code"),
            "description": WEATHER_CODES.get(current.get("weather_code"), "Weather")
        },
        "daily": days,
        "source": "Open-Meteo"
    }


@app.get("/api/weather")
@login_required
def weather():
    s = current_site()
    try:
        lat = float(request.args.get("lat")) if request.args.get("lat") else None
        lon = float(request.args.get("lon")) if request.args.get("lon") else None
    except Exception:
        return jsonify(error="Invalid location coordinates"), 400

    location_label = "Your location"
    if lat is None or lon is None:
        address = (s["address"] if s else "") or (s["name"] if s else "")
        if not address:
            return jsonify(error="Location unavailable. Allow location access or add a site address."), 400
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        try:
            gr = requests.get(
                geo_url,
                params={"name": address, "count": 1, "language": "en", "format": "json"},
                timeout=10,
            )
            gr.raise_for_status()
            results = gr.json().get("results") or []
            if not results:
                return jsonify(
                    error="Could not find the site location. Allow browser location access or update the site address."
                ), 400
            place = results[0]
            lat = float(place["latitude"])
            lon = float(place["longitude"])
            location_label = ", ".join(
                x for x in [place.get("name"), place.get("admin1"), place.get("country")] if x
            )
        except Exception as exc:
            return jsonify(error=f"Weather service unavailable: {exc}"), 502

    try:
        return jsonify(weather_payload(lat, lon, location_label))
    except requests.RequestException:
        return jsonify(error="Weather service is temporarily unavailable."), 502
    except Exception:
        return jsonify(error="Unable to load weather right now."), 500


@app.get("/api/events")
@login_required
def get_events():
    u, s = user(), current_site()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    result = q(
        """SELECT e.*,u.name created_by_name FROM events e
           LEFT JOIN users u ON u.id=e.created_by
           WHERE e.organisation_id=? AND e.site_id=? AND e.event_date>=? AND e.event_date<?
           ORDER BY e.event_date,e.start_time,e.id""",
        (u["organisation_id"], s["id"], start, end),
    )
    return jsonify(events=[dict(x) for x in result], month=month)


@app.post("/api/events")
@login_required
@manager_required
def add_event():
    u, s = user(), current_site()
    d = request.get_json() or {}
    title = (d.get("title") or "").strip()
    event_date = (d.get("event_date") or "").strip()
    event_type = (d.get("event_type") or "General").strip()
    start_time = (d.get("start_time") or "").strip()
    end_time = (d.get("end_time") or "").strip()
    notes = (d.get("notes") or "").strip()
    all_day = 1 if d.get("all_day") else 0
    if not title or not event_date:
        return jsonify(error="Event title and date are required"), 400
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
        if start_time:
            datetime.strptime(start_time, "%H:%M")
        if end_time:
            datetime.strptime(end_time, "%H:%M")
    except ValueError:
        return jsonify(error="Use a valid date and time"), 400
    event_id = execute(
        """INSERT INTO events(organisation_id,site_id,title,event_date,start_time,end_time,event_type,notes,all_day,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            title,
            event_date,
            start_time,
            end_time,
            event_type,
            notes,
            all_day,
            u["id"],
            now(),
        ),
    )
    audit("Created", "event", event_id, title)
    return jsonify(ok=True, id=event_id)


@app.delete("/api/events/<int:event_id>")
@login_required
@manager_required
def delete_event(event_id):
    u, s = user(), current_site()
    event = q(
        "SELECT * FROM events WHERE id=? AND organisation_id=? AND site_id=?",
        (event_id, u["organisation_id"], s["id"]),
        True,
    )
    if not event:
        return jsonify(error="Event not found"), 404
    execute("DELETE FROM events WHERE id=?", (event_id,))
    audit("Deleted", "event", event_id, event["title"])
    return jsonify(ok=True)


# -----------------------------------------------------------------------------
# ALPORT COMPANY ADMIN (HQ)
# -----------------------------------------------------------------------------

def company_admin_logged_in():
    return bool(session.get("company_admin"))


def company_admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not company_admin_logged_in():
            if request.path.startswith("/api/company-admin/"):
                return jsonify(error="Company admin authentication required"), 401
            return redirect(url_for("company_admin_login"))
        return fn(*args, **kwargs)
    return wrapped


def company_event(event_type, detail="", organisation_id=None):
    execute(
        "INSERT INTO company_events(organisation_id,event_type,detail,created_at) VALUES(?,?,?,?)",
        (organisation_id, event_type, detail, now()),
    )


@app.route("/company-admin/login", methods=["GET", "POST"])
def company_admin_login():
    if company_admin_logged_in():
        return redirect(url_for("company_admin_home"))
    error = None
    if request.method == "POST":
        import hmac
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        expected_email = os.environ.get("COMPANY_ADMIN_EMAIL", "").strip().lower()
        expected_password = os.environ.get("COMPANY_ADMIN_PASSWORD", "")
        if expected_email and expected_password and hmac.compare_digest(email, expected_email) and hmac.compare_digest(password, expected_password):
            session.clear()
            session["company_admin"] = True
            session["company_admin_email"] = email
            return redirect(url_for("company_admin_home"))
        error = "Incorrect company admin email or password."
    return render_template("company_admin_login.html", error=error)


@app.post("/company-admin/logout")
def company_admin_logout():
    session.clear()
    return redirect(url_for("company_admin_login"))


@app.get("/company-admin")
@company_admin_required
def company_admin_home():
    return render_template("company_admin.html", admin_email=session.get("company_admin_email", "Alport Hospitality Solutions Admin"))


@app.get("/api/company-admin/overview")
@company_admin_required
def company_admin_overview():
    orgs = q("SELECT COUNT(*) AS n FROM organisations", one=True)["n"]
    sites = q("SELECT COUNT(*) AS n FROM sites WHERE active=1", one=True)["n"]
    users_count = q("SELECT COUNT(*) AS n FROM users WHERE active=1", one=True)["n"]
    active = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Active'", one=True)["n"]
    trials = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Trial'", one=True)["n"]
    payment_required = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Payment required'", one=True)["n"]
    overdue = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Past due'", one=True)["n"]
    mrr = float(q("SELECT COALESCE(SUM(monthly_price),0) AS x FROM subscriptions WHERE status='Active'", one=True)["x"] or 0)
    collected = float(q("SELECT COALESCE(SUM(amount),0) AS x FROM subscription_payments WHERE status='Paid'", one=True)["x"] or 0)
    recent = q("""SELECT ce.*, o.name organisation_name FROM company_events ce
                  LEFT JOIN organisations o ON o.id=ce.organisation_id
                  ORDER BY ce.id DESC LIMIT 12""")
    return jsonify(
        organisations=orgs, sites=sites, users=users_count, active=active, trials=trials,
        payment_required=payment_required, overdue=overdue, mrr=mrr, arr=mrr*12, collected=collected,
        recent_events=[dict(x) for x in recent],
    )


@app.get("/api/company-admin/customers")
@company_admin_required
def company_admin_customers():
    rows = q("""SELECT o.id,o.name,o.business_type,o.created_at,
                COALESCE(s.plan,'Starter') plan,COALESCE(s.monthly_price,0) monthly_price,
                COALESCE(s.status,'Trial') subscription_status,COALESCE(s.trial_end,'') trial_end,
                COALESCE(s.next_billing_date,'') next_billing_date,
                COALESCE(s.contract_start,'') contract_start,COALESCE(s.contract_end,'') contract_end,
                COALESCE(s.contracted_sites,1) contracted_sites,COALESCE(s.contracted_users,3) contracted_users,
                COALESCE(s.base_price,500) base_price,0 extra_user_price,
                (SELECT COUNT(*) FROM sites st WHERE st.organisation_id=o.id AND st.active=1) site_count,
                (SELECT COUNT(*) FROM users u WHERE u.organisation_id=o.id AND u.active=1) user_count,
                (SELECT COALESCE(SUM(sp.amount),0) FROM subscription_payments sp WHERE sp.organisation_id=o.id AND sp.status='Paid') total_paid
                FROM organisations o LEFT JOIN subscriptions s ON s.organisation_id=o.id
                ORDER BY o.id DESC""")
    return jsonify(customers=[dict(x) for x in rows])


@app.get("/api/company-admin/customers/<int:organisation_id>")
@company_admin_required
def company_admin_customer(organisation_id):
    customer = q("""SELECT o.*,s.plan,s.monthly_price,s.status subscription_status,s.trial_end,s.next_billing_date,
                    s.notes subscription_notes,s.contract_start,s.contract_end,s.contract_months,s.contracted_sites,
                    s.contracted_users,s.base_price,s.included_users_per_site,s.extra_user_price,s.vat_rate,
                    s.last_payment_status,s.last_payment_at,s.past_due_since
                    FROM organisations o LEFT JOIN subscriptions s ON s.organisation_id=o.id WHERE o.id=?""", (organisation_id,), True)
    if not customer:
        return jsonify(error="Customer not found"), 404
    sites = q("SELECT id,name,address,active,created_at FROM sites WHERE organisation_id=? ORDER BY id", (organisation_id,))
    users_rows = q("SELECT id,name,email,role,active,created_at FROM users WHERE organisation_id=? ORDER BY id", (organisation_id,))
    payments = q("SELECT * FROM subscription_payments WHERE organisation_id=? ORDER BY payment_date DESC,id DESC", (organisation_id,))
    return jsonify(customer=dict(customer), pricing=pricing_for(organisation_id), sites=[dict(x) for x in sites], users=[dict(x) for x in users_rows], payments=[dict(x) for x in payments])


@app.post("/api/company-admin/subscriptions/<int:organisation_id>")
@company_admin_required
def company_admin_update_subscription(organisation_id):
    if not q("SELECT id FROM organisations WHERE id=?", (organisation_id,), True):
        return jsonify(error="Customer not found"), 404
    d = request.get_json() or {}
    plan = (d.get("plan") or "Starter").strip()[:50]
    status = (d.get("status") or "Trial").strip()
    if status not in ("Trial", "Payment required", "Active", "Past due", "Suspended", "Cancelled"):
        return jsonify(error="Invalid subscription status"), 400
    current = subscription_for(organisation_id)
    pricing = pricing_for(organisation_id)
    # During an active 12-month minimum term the agreed unit rates are locked.
    # Company admin can change status/notes, but not silently re-price the contract.
    if current and pricing["inside_term"]:
        monthly_price = pricing["net"]
    else:
        try:
            monthly_price = max(0, float(d.get("monthly_price", pricing["net"])))
        except Exception:
            return jsonify(error="Invalid monthly price"), 400
    trial_end = (d.get("trial_end") or "")[:20]
    next_billing_date = (d.get("next_billing_date") or "")[:20]
    notes = (d.get("notes") or "")[:1000]
    execute("""INSERT INTO subscriptions(organisation_id,plan,monthly_price,status,trial_end,next_billing_date,started_at,notes)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT (organisation_id) DO UPDATE SET
               plan=EXCLUDED.plan,monthly_price=EXCLUDED.monthly_price,status=EXCLUDED.status,
               trial_end=EXCLUDED.trial_end,next_billing_date=EXCLUDED.next_billing_date,notes=EXCLUDED.notes""",
            (organisation_id, plan, monthly_price, status, trial_end, next_billing_date, now(), notes))
    company_event("Subscription updated", f"{plan} Â· Â£{monthly_price:.2f}/month Â· {status}", organisation_id)
    return jsonify(ok=True)


@app.post("/api/company-admin/payments")
@company_admin_required
def company_admin_add_payment():
    d = request.get_json() or {}
    try:
        organisation_id = int(d.get("organisation_id"))
        amount = float(d.get("amount"))
    except Exception:
        return jsonify(error="Customer and valid payment amount are required"), 400
    if amount < 0 or not q("SELECT id FROM organisations WHERE id=?", (organisation_id,), True):
        return jsonify(error="Invalid payment"), 400
    payment_date = (d.get("payment_date") or date.today().isoformat())[:20]
    status = (d.get("status") or "Paid")[:30]
    method = (d.get("method") or "")[:80]
    reference = (d.get("reference") or "")[:120]
    notes = (d.get("notes") or "")[:500]
    pid = execute("""INSERT INTO subscription_payments(organisation_id,amount,payment_date,status,method,reference,notes,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""", (organisation_id, amount, payment_date, status, method, reference, notes, now()))
    company_event("Payment recorded", f"Â£{amount:.2f} Â· {status}", organisation_id)
    return jsonify(ok=True, id=pid)


@app.get("/api/company-admin/payments")
@company_admin_required
def company_admin_payments():
    rows = q("""SELECT sp.*,o.name organisation_name FROM subscription_payments sp
                JOIN organisations o ON o.id=sp.organisation_id ORDER BY sp.payment_date DESC,sp.id DESC LIMIT 500""")
    return jsonify(payments=[dict(x) for x in rows])


@app.get("/api/company-admin/users")
@company_admin_required
def company_admin_users():
    rows = q("""SELECT u.id,u.name,u.email,u.role,u.active,u.created_at,o.name organisation_name
                FROM users u JOIN organisations o ON o.id=u.organisation_id ORDER BY u.id DESC LIMIT 1000""")
    return jsonify(users=[dict(x) for x in rows])


@app.get("/api/company-admin/events")
@company_admin_required
def company_admin_events():
    rows = q("""SELECT ce.*,o.name organisation_name FROM company_events ce
                LEFT JOIN organisations o ON o.id=ce.organisation_id ORDER BY ce.id DESC LIMIT 500""")
    return jsonify(events=[dict(x) for x in rows])


MARKET_INSTRUMENTS = [
    {"name": "Wheat", "symbol": "W_1", "category": "Grain", "unit": "USD / contract"},
    {"name": "Corn", "symbol": "C_1", "category": "Grain", "unit": "USD / contract"},
    {"name": "Sugar", "symbol": "SB1", "category": "Sugar", "unit": "USD / contract"},
    {"name": "Coffee", "symbol": "KC1", "category": "Coffee", "unit": "USD / contract"},
    {"name": "Cocoa", "symbol": "CC1", "category": "Cocoa", "unit": "USD / contract"},
    {"name": "Orange Juice", "symbol": "OJ1", "category": "Fruit", "unit": "USD / contract"},
    {"name": "Brent Crude", "symbol": "BRENT/USD", "category": "Energy", "unit": "USD / barrel"}
]


@app.get("/api/market")
@login_required
def market():
    api_key = os.environ.get("MARKET_API_KEY", "").strip()
    if not api_key:
        return jsonify(
            configured=False,
            provider="Twelve Data",
            message="Add MARKET_API_KEY to enable live market prices.",
            instruments=MARKET_INSTRUMENTS,
        )

    rows = []
    errors = []

    for item in MARKET_INSTRUMENTS:
        try:
            r = requests.get(
                "https://api.twelvedata.com/price",
                params={"symbol": item["symbol"], "apikey": api_key},
                timeout=8,
            )
            data = r.json()
            if r.ok and data.get("price") is not None:
                rows.append({
                    **item,
                    "price": float(data["price"]),
                    "timestamp": data.get("timestamp"),
                    "status": "Live",
                })
            else:
                errors.append(item["name"])
                rows.append({**item, "price": None, "status": "Unavailable"})
        except Exception:
            errors.append(item["name"])
            rows.append({**item, "price": None, "status": "Unavailable"})

    return jsonify(
        configured=True,
        provider="Twelve Data",
        instruments=rows,
        errors=errors,
        updated_at=now(),
    )


from alport_payments import register_payments
register_payments(app, globals())


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
