#!/usr/bin/env python3
"""Unit tests for Consultation Panel Tool."""

import unittest
from tools.consultation_panel import detect_domain, generate_panel_specs, format_decision_matrix


class TestConsultationPanel(unittest.TestCase):

    def test_detect_domain(self):
        self.assertEqual(detect_domain("VLAN 20 firewall traffic rules on UniFi"), "network_infrastructure")
        self.assertEqual(detect_domain("Crab Cavern mutex deadlock between Amos and Zero"), "multi_agent_protocols")
        self.assertEqual(detect_domain("Migrating SQLite tables to Postgres with connection pooling"), "data_lifecycle")
        self.assertEqual(detect_domain("Refactoring async websocket event loop handlers"), "software_architecture")

    def test_generate_panel_specs(self):
        specs = generate_panel_specs("Migrate Mealie to PostgreSQL", domain="data_lifecycle", model="flash")
        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0]["Role"], "Data Integrity & ACID Purist")
        self.assertEqual(specs[0]["Model"], "flash")
        self.assertIn("Mealie", specs[0]["Prompt"])
        self.assertIn("ACID", specs[0]["Prompt"])

    def test_format_decision_matrix(self):
        matrix = format_decision_matrix(
            options=["Option A (SQLite Local)", "Option B (Postgres Container)"],
            evaluations={
                "Option A (SQLite Local)": {"performance": "High (<1ms)", "security": "Isolated", "maintenance": "Zero", "rating": "Recommended"},
                "Option B (Postgres Container)": {"performance": "Medium (Network TCP)", "security": "Isolated", "maintenance": "Medium (+1 container)", "rating": "Overkill"},
            },
        )
        self.assertIn("Architectural Decision Matrix", matrix)
        self.assertIn("Option A (SQLite Local)", matrix)
        self.assertIn("Option B (Postgres Container)", matrix)


if __name__ == "__main__":
    unittest.main()
