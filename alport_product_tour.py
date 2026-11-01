"""Public, example-only product walkthrough using the configured new-venue price."""
from flask import render_template


def register_product_tour(app, env):
    @app.get('/product-tour')
    def product_tour():
        return render_template('product_tour.html', price=env['BASE_PRICE_PER_SITE'],
                               term=env['CONTRACT_MONTHS'], vat=env['VAT_RATE'])
