import unittest

from complaint_core.api import route
from complaint_core.correlation import ComplaintService
from complaint_core.service import DomainService
from complaint_core.storage import Database


class CorrelationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.complaints = ComplaintService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "监管局"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "ad", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "operator", "new_actor_id": "op", "display_name": "值班员",
               "role": "operator", "organization_id": "o1"},
              {"X-Actor-Id": "ad"})
        route(self.service, "POST", "/sites",
              {"request_id": "site-a", "site_id": "site-a", "organization_id": "o1",
               "name": "A厂", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "op"})
        route(self.service, "POST", "/correlation-rules",
              {"request_id": "rule-1", "rule_id": "night", "params": {"min_score": 0.0,
                                                                       "min_text_jaccard": 0.0}},
              {"X-Actor-Id": "ad"}, self.complaints)
        route(self.service, "POST", "/domain-records",
              {"request_id": "zone-a", "site_id": "site-a", "category": "complaint_zone",
               "external_key": "zone-a",
               "data": {"zone_id": "zn", "lat": 30.0, "lon": 120.0, "site_text": "化工 刺鼻"}},
              {"X-Actor-Id": "op"})

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor}, self.complaints)

    def _get(self, path, actor="op"):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor}, self.complaints)

    def test_full_case_lifecycle_over_http(self):
        status, payload = self._post("/complaint-events", {
            "request_id": "ev-1", "source": "hotline", "zone_id": "zn",
            "occurred_at": "2026-09-25T11:45:00Z",
            "description": "夜里闻到化工刺鼻味道", "lat": 30.001, "lon": 120.001,
            "contact": {"name": "王建国", "phone": "13812345678"}})
        self.assertEqual(201, status)
        event_id = payload["resource_id"]

        status, payload = self._get("/cases")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        case_id = payload["items"][0]["case_id"]
        self.assertTrue(payload["items"][0]["case_code"].startswith("CASE-"))

        status, trace = self._get(f"/cases/{case_id}/trace")
        self.assertEqual(200, status)
        self.assertEqual(event_id, trace["events"][0]["event_id"])
        self.assertEqual("night", trace["current_version"]["rule_id"])
        self.assertNotIn("contact", trace["events"][0])
        candidate_id = trace["current_version"]["candidates"][0]["candidate_id"]

        # 无依据的决定被拒
        status, payload = self._post("/candidate-decisions", {
            "request_id": "dec-bad", "case_id": case_id, "action": "exclude_candidate",
            "site_id": "site-a", "reason": "?"})
        self.assertEqual(400, status)

        status, payload = self._post("/candidate-decisions", {
            "request_id": "dec-1", "case_id": case_id, "action": "confirm_candidate",
            "site_id": "site-a", "reason": "描述与A厂废气特征一致", "expected_revision": 0})
        self.assertEqual(201, status)

        status, payload = self._post("/case-confirmations", {
            "request_id": "cfm-1", "case_id": case_id,
            "reason": "证据链完整，建议现场核查", "candidate_ids": [candidate_id],
            "site_inspection_required": True})
        self.assertEqual(201, status)
        status, confirmed = self._get(f"/cases/{case_id}/trace")
        self.assertTrue(confirmed["confirmation"]["site_inspection_required"])

        # 冻结后重算被拒
        status, payload = self._post("/correlation-versions/regenerate",
                                     {"request_id": "rg-1", "case_id": case_id})
        self.assertEqual(409, status)

        # operator 撤销被拒
        status, payload = self._post("/case-confirmations/revoke",
                                     {"request_id": "rev-op", "case_id": case_id,
                                      "reason": "值班员无权撤销已确认结论"})
        self.assertEqual(403, status)

        # admin 撤销并保留原结论
        status, payload = self._post("/case-confirmations/revoke",
                                     {"request_id": "rev-ad", "case_id": case_id,
                                      "reason": "上级复核退回补证，原结论留存"}, actor="ad")
        self.assertEqual(201, status)
        status, trace = self._get(f"/cases/{case_id}/trace")
        self.assertEqual("revoked", trace["status"])
        self.assertTrue(trace["confirmation"]["retained_after_revoke"])

    def test_contacts_endpoint_masks_for_operator(self):
        self._post("/complaint-events", {
            "request_id": "ev-1", "source": "hotline", "zone_id": "zn",
            "occurred_at": "2026-09-25T11:45:00Z", "description": "化工刺鼻味道",
            "contact": {"name": "王建国", "phone": "13812345678"}})
        case_id = self._get("/cases")[1]["items"][0]["case_id"]
        status, payload = self._get(f"/cases/{case_id}/contacts")
        self.assertEqual(200, status)
        self.assertEqual("138****5678", payload["items"][0]["contact_phone"])
        self.assertTrue(payload["items"][0]["masked"])

        status, payload = self._get(f"/cases/{case_id}/contacts", actor="ad")
        self.assertEqual("13812345678", payload["items"][0]["contact_phone"])

    def test_missing_case_returns_404(self):
        status, payload = self._get("/cases/nope/trace")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
