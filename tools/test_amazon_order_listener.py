#!/usr/bin/env python3
"""Unit test suite for tools/amazon_order_listener.py."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.amazon_order_listener as aol

class TestAmazonOrderListener(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        aol.STATE_DIR = str(self.temp_path)
        aol.SEEN_FILE = str(self.temp_path / "seen_orders.json")
        aol.ORDERS_FILE = str(self.temp_path / "orders.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_amazon_order_full(self):
        mock_payload = {
            "headers": [
                {"name": "Date", "value": "Fri, 2 Oct 2026 20:40:00 -0700"},
                {"name": "Subject", "value": "Ordered: Cable Management Box ABS"}
            ],
            "body": {
                "data": "T3JkZXIgIzExMy0yMTQ2NjcyLTM4MjAyNTgKR3JhbmQgVG90YWw6IDI5LjY2IFVTRApBcnJpdmluZyBTdW5kYXk="
            }
        }
        # The base64 above decodes to:
        # Order #113-2146672-3820258
        # Grand Total: 29.66 USD
        # Arriving Sunday
        mdata = {
            "id": "test_msg_1",
            "payload": mock_payload
        }
        parsed = aol.parse_amazon_order(mdata)
        self.assertEqual(parsed["order_id"], "113-2146672-3820258")
        self.assertEqual(parsed["total"], "$29.66")
        self.assertEqual(parsed["estimate"], "Arriving Sunday")
        self.assertEqual(parsed["message_id"], "test_msg_1")

    def test_item_filtering(self):
        html_body = """
        <html>
            <img alt="Amazon.com logo" src="logo.png">
            <img alt="prime" src="prime.png">
            <img alt="Completed" src="dot.png">
            <img alt="Kos Design Cable Management Box ABS 12.6x7.9x4.5in" src="item.png">
            <img alt="Voltive Cat6 250ft Blue Cable" src="item2.png">
            <img alt="Star rating 5" src="star.png">
        </html>
        """
        import base64
        b64 = base64.urlsafe_b64encode(html_body.encode("utf-8")).decode("utf-8")
        mdata = {
            "id": "test_msg_2",
            "payload": {
                "headers": [],
                "body": {"data": b64}
            }
        }
        parsed = aol.parse_amazon_order(mdata)
        self.assertEqual(len(parsed["items"]), 2)
        self.assertIn("Kos Design Cable Management Box ABS 12.6x7.9x4.5in", parsed["items"])
        self.assertIn("Voltive Cat6 250ft Blue Cable", parsed["items"])

    def test_format_discord_card(self):
        order = {
            "order_id": "113-9999999-1111111",
            "total": "$149.99",
            "estimate": "Arriving Monday",
            "items": ["Item A", "Item B"]
        }
        card = aol.format_discord_card(order)
        self.assertIn("113-9999999-1111111", card)
        self.assertIn("$149.99", card)
        self.assertIn("Arriving Monday", card)
        self.assertIn("Item A", card)
        self.assertIn("Item B", card)

    def test_state_persistence(self):
        seen = {"id1", "id2"}
        aol.save_seen(seen)
        loaded_seen = aol.load_seen()
        self.assertEqual(seen, loaded_seen)

        orders = [{"order_id": "123", "items": ["Item X"]}]
        aol.save_orders(orders)
        loaded_orders = aol.load_orders()
        self.assertEqual(len(loaded_orders), 1)
        self.assertEqual(loaded_orders[0]["order_id"], "123")

if __name__ == "__main__":
    unittest.main()
