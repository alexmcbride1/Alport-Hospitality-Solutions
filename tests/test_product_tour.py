import unittest
from pathlib import Path
from flask import Flask
from alport_product_tour import register_product_tour

class ProductTourTests(unittest.TestCase):
    def test_public_page_uses_configured_price_and_no_customer_records(self):
        root=Path(__file__).resolve().parents[1]
        app=Flask(__name__,template_folder=str(root/'templates'))
        register_product_tour(app,dict(BASE_PRICE_PER_SITE=1234,CONTRACT_MONTHS=12,VAT_RATE=.2))
        response=app.test_client().get('/product-tour')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('£1,234 + VAT per venue',html)
        self.assertIn('£14,808 + VAT',html)
        self.assertIn('fictional venues',html)
        self.assertIn('data-price="1234"',html)
        for asset in ('today-mobile.png','today-example.png','chat-tasks-example.png'):
            self.assertTrue((root/'static/product-tour'/asset).is_file())
