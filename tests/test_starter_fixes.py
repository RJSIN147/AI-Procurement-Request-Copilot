from __future__ import annotations

import unittest
from datetime import date

from fastapi import HTTPException

from mock_api.app import vendor_risk
from src.data_access import load_employees, load_vendors, policy_reference_date, policy_sections, records


class StarterFixTests(unittest.TestCase):
    def test_blank_csv_cells_are_none_not_nan(self):
        top = next(e for e in records(load_employees()) if e["employee_id"] == "E010")
        self.assertIsNone(top["manager_id"])
        brandboard = next(v for v in records(load_vendors()) if v["vendor_name"] == "BrandBoard")
        self.assertIsNone(brandboard["security_review_date"])

    def test_reference_date_comes_from_policy(self):
        self.assertEqual(policy_reference_date(), date(2026, 9, 30))

    def test_policy_sections_parsed(self):
        sections = policy_sections()
        self.assertEqual(sorted(sections), list(range(1, 12)))
        self.assertIn("365 days", sections[5])

    def test_vendor_name_is_not_decoded_twice(self):
        # Called directly: Starlette's TestClient decodes the path one extra time itself,
        # so it can't be used to check this (uvicorn decodes once).
        with self.assertRaises(HTTPException) as ctx:
            vendor_risk("Acme%41")
        self.assertIn("Acme%41", ctx.exception.detail)
        self.assertEqual(vendor_risk("CreativeSuite Labs")["vendor_name"], "CreativeSuite Labs")


if __name__ == "__main__":
    unittest.main()
