import unittest

from complaint_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        # 投诉关联：两通重复来电合并为一个匿名化案件
        self.assertEqual(2, result["merged_events"])
        self.assertTrue(result["anonymized"])
        # 规则版本、候选与置信区间
        self.assertEqual(1, result["rule_version"])
        self.assertEqual("site-a", result["top_candidate"])
        self.assertGreaterEqual(result["top_confidence_interval"]["lower"], 0.0)
        self.assertLessEqual(result["top_confidence_interval"]["upper"], 1.0)
        # 确认冻结后补录数据不能重算；撤销受更高权限限制且原结论保留
        self.assertTrue(result["confirmed_then_frozen_against_new_data"])
        self.assertTrue(result["self_revoke_blocked"])
        self.assertEqual(["revoked"], result["conclusions_kept"])
        self.assertTrue(result["needs_field_inspection"])


if __name__ == "__main__":
    unittest.main()
