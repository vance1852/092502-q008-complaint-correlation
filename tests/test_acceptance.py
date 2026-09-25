import unittest

from complaint_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(4, result["records"])
        self.assertEqual(2, result["merged_events"])
        self.assertEqual("night-odor:v1", result["rule_version"])
        self.assertEqual("confirmed", result["case_status"])
        self.assertTrue(result["site_inspection_required"])
        self.assertTrue(result["contact_masked"])


if __name__ == "__main__":
    unittest.main()
