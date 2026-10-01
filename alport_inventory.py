"""Ingredient links and supplier pack costing. No automatic orders or stock deductions."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hmac
import secrets
from flask import jsonify, request, session

UNITS = {'g': ('mass', Decimal('1')), 'kg': ('mass', Decimal('1000')),
         'ml': ('volume', Decimal('1')), 'l': ('volume', Decimal('1000')),
         'each': ('count', Decimal('1')), 'unit': ('count', Decimal('1'))}
SCHEMA = """
ALTER TABLE menu_components ADD COLUMN IF NOT EXISTS stock_item_id BIGINT REFERENCES stock_items(id);
CREATE TABLE IF NOT EXISTS alport_supplier_products (
 id BIGSERIAL PRIMARY KEY,
 organisation_id BIGINT NOT NULL REFERENCES organisations(id),
 site_id BIGINT NOT NULL REFERENCES sites(id),
 stock_item_id BIGINT NOT NULL REFERENCES stock_items(id),
 supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
 product_name TEXT NOT NULL, sku TEXT NOT NULL DEFAULT '',
 pack_quantity NUMERIC(16,6) NOT NULL CHECK(pack_quantity>0),
 pack_unit TEXT NOT NULL,
 pack_price NUMERIC(16,4) NOT NULL CHECK(pack_price>=0),
 preferred BOOLEAN NOT NULL DEFAULT FALSE,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS alport_one_preferred_product
 ON alport_supplier_products(stock_item_id) WHERE preferred;
"""

def number(value, label='Quantity', positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(label + ' must be a valid number.') from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000000'):
        raise ValueError(label + ' must be finite, non-negative and below 1 billion.')
    if positive and result <= 0:
        raise ValueError(label + ' must be greater than zero.')
    return result

def converted(quantity, source, target):
    source, target = str(source).strip().lower(), str(target).strip().lower()
    if source not in UNITS or target not in UNITS or UNITS[source][0] != UNITS[target][0]:
        raise ValueError('Use compatible units: g/kg, ml/l, or each/unit. Weight cannot be converted to volume.')
    return number(quantity) * UNITS[source][1] / UNITS[target][1]

def ingredient_cost(quantity, recipe_unit, stock):
    return float((converted(quantity, recipe_unit, stock['unit']) * number(stock['unit_cost'], 'Unit cost'))
                 .quantize(Decimal('.0001'), rounding=ROUND_HALF_UP))

def resolve_component(part, organisation_id, site_id, query):
    """Resolve by scoped database ID, never trust a client-supplied ingredient cost."""
    value = part.get('stock_item_id')
    if value in (None, ''):
        return None
    try:
        identifier = int(value)
    except (TypeError, ValueError):
        raise ValueError('Select a valid stock ingredient.') from None
    stock = query('SELECT * FROM stock_items WHERE id=? AND organisation_id=? AND site_id=? AND active=1',
                  (identifier, organisation_id, site_id), True)
    if not stock:
        raise ValueError('Stock ingredient is unavailable at this site.')
    return stock

def register_inventory(app, env):
    q, conn = env['q'], env['conn']
    user, site = env['user'], env['current_site']
    login, manager = env['login_required'], env['manager_required']

    @app.get('/api/inventory/catalogue')
    @login
    def inventory_catalogue():
        u, s = user(), site()
        scope = (u['organisation_id'], s['id'])
        products = q('''SELECT p.*,su.name AS supplier_name,si.name AS ingredient_name,si.unit AS stock_unit
            FROM alport_supplier_products p JOIN suppliers su ON su.id=p.supplier_id
            JOIN stock_items si ON si.id=p.stock_item_id
            WHERE p.organisation_id=? AND p.site_id=? AND si.active=1 AND su.active=1
            ORDER BY si.name,p.preferred DESC,su.name,p.id''', scope)
        # Decimal JSON support varies between Flask versions; use numeric UI values explicitly.
        for p in products:
            for key in ('pack_quantity','pack_price'):
                p[key] = float(p[key])
        session.setdefault('inventory_csrf', secrets.token_urlsafe(32))
        return jsonify(products=products,
            stock=q('SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name',scope),
            suppliers=q('SELECT * FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name',(scope[0],)),
            csrf=session['inventory_csrf'])

    @app.post('/api/inventory/products')
    @login
    @manager
    def inventory_product_save():
        token = session.get('inventory_csrf','')
        if not token or not hmac.compare_digest(token, request.headers.get('X-Inventory-CSRF','')):
            return jsonify(error='Reload the supplier catalogue and try again.'),403
        u, s = user(), site()
        d = request.get_json(silent=True)
        if not isinstance(d,dict):
            return jsonify(error='Invalid product data.'),400
        try:
            stock_id, supplier_id = int(d.get('stock_item_id',0)),int(d.get('supplier_id',0))
            product_id = int(d.get('id') or 0)
            qty = number(d.get('pack_quantity'), 'Pack quantity', True).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP)
            if qty < Decimal('.000001'):
                raise ValueError('Pack quantity must be at least 0.000001.')
            price = number(d.get('pack_price'), 'Pack price').quantize(Decimal('.0001'),rounding=ROUND_HALF_UP)
            name = str(d.get('product_name') or '').strip()[:160]
            sku = str(d.get('sku') or '').strip()[:80]
            unit = str(d.get('pack_unit') or '').strip().lower()
            if not name: raise ValueError('Product name is required.')
            if not isinstance(d.get('preferred',False),bool): raise ValueError('Invalid preferred product choice.')
            preferred = d.get('preferred',False)
            with conn() as c:
                # Serialise preferred-product changes per ingredient, including initial assignment.
                stock = c.execute('SELECT * FROM stock_items WHERE id=%s AND organisation_id=%s AND site_id=%s AND active=1 FOR UPDATE',
                                  (stock_id,u['organisation_id'],s['id'])).fetchone()
                if not stock: raise ValueError('Ingredient is unavailable at this site.')
                supplier = c.execute('SELECT * FROM suppliers WHERE id=%s AND organisation_id=%s AND active=1',
                                     (supplier_id,u['organisation_id'])).fetchone()
                if not supplier: raise ValueError('Supplier is unavailable in this organisation.')
                pack_units = converted(qty,unit,stock['unit'])
                per_unit = float((price / pack_units).quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP))
                number(per_unit, 'Calculated unit cost')
                existing = None
                if product_id:
                    existing = c.execute('SELECT * FROM alport_supplier_products WHERE id=%s AND organisation_id=%s AND site_id=%s',
                                         (product_id,u['organisation_id'],s['id'])).fetchone()
                    if not existing or existing['stock_item_id'] != stock_id:
                        raise ValueError('Product not found for this ingredient. Add a new product to change its ingredient.')
                    # A preferred product stays preferred until another one is explicitly chosen.
                    preferred = preferred or existing['preferred']
                if preferred:
                    c.execute('UPDATE alport_supplier_products SET preferred=FALSE WHERE stock_item_id=%s AND organisation_id=%s AND site_id=%s',
                              (stock_id,u['organisation_id'],s['id']))
                values=(supplier_id,name,sku,qty,unit,price,preferred,env['now']())
                if product_id:
                    c.execute('''UPDATE alport_supplier_products SET supplier_id=%s,product_name=%s,sku=%s,pack_quantity=%s,
                         pack_unit=%s,pack_price=%s,preferred=%s,updated_at=%s WHERE id=%s''',values+(product_id,))
                else:
                    product_id=c.execute('''INSERT INTO alport_supplier_products(supplier_id,product_name,sku,pack_quantity,
                         pack_unit,pack_price,preferred,updated_at,organisation_id,site_id,stock_item_id)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                         values+(u['organisation_id'],s['id'],stock_id)).fetchone()['id']
                if preferred:
                    c.execute('UPDATE stock_items SET unit_cost=%s,supplier=%s WHERE id=%s',
                              (per_unit,supplier['name'],stock_id))
        except (ValueError, TypeError, InvalidOperation) as exc:
            return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'Invalid product values.'),400
        env['audit']('Updated','supplier_product',product_id,name)
        return jsonify(ok=True,id=product_id)
