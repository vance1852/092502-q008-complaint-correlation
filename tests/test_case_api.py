import unittest
from datetime import datetime, timezone

from complaint_core.api import route
from complaint_core.cases import CaseService
from complaint_core.clock import FixedClock
from complaint_core.service import DomainService
from complaint_core.storage import Database


class CaseApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.cases = CaseService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="局")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="opr", actor_id="a1", new_actor_id="op1",
                                    display_name="值班员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rvw", actor_id="a1", new_actor_id="rv1",
                                    display_name="值班长", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="st1", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="甲厂", timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="zn1", actor_id="op1", site_id="s1",
                                        category="complaint_zone", external_key="z1",
                                        data={"region_code": "R1", "location": [31.24, 121.5]})
        self.cases.ensure_seed_rule(actor_id="a1")

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, case_service=self.cases)

    def test_full_case_workflow_over_http(self):
        status, payload = self._route("POST", "/complaints", {
            "request_id": "c1", "channel": "hotline", "occurred_at": "2026-09-25T21:50:00Z",
            "region_code": "R1", "description": "小区有刺鼻化工味道不敢开窗",
            "contact": "13800138000", "location": [31.236, 121.5]})
        self.assertEqual(201, status)
        case_id = payload["resource_id"]

        status, payload = self._route("POST", f"/cases/{case_id}/versions",
                                      {"request_id": "v1", "reason": "初次研判"})
        self.assertEqual(201, status)

        status, payload = self._route("GET", f"/cases/{case_id}/lineage")
        self.assertEqual(200, status)
        self.assertTrue(payload["anonymized"])
        self.assertEqual(1, payload["current_version"])
        self.assertEqual(1, len(payload["versions"][0]["candidates"]))

        # 值班员确认候选并给出依据
        status, payload = self._route("POST", f"/cases/{case_id}/confirm",
                                      {"request_id": "cf", "reason": "证据一致"}, actor="op1")
        self.assertEqual(201, status)

        status, payload = route(self.service, "GET", "/cases?status=confirmed", {},
                                {"X-Actor-Id": "op1"}, case_service=self.cases)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_decision_must_carry_reason(self):
        status, payload = self._route("POST", "/complaints", {
            "request_id": "c1", "channel": "hotline", "occurred_at": "2026-09-25T21:50:00Z",
            "region_code": "R1", "description": "小区有刺鼻化工味道不敢开窗"})
        case_id = payload["resource_id"]
        self._route("POST", f"/cases/{case_id}/versions", {"request_id": "v1", "reason": "r"})
        status, payload = self._route("POST", f"/cases/{case_id}/decisions", {
            "request_id": "d1", "decision": "exclude", "site_id": "s1", "reason": "   "})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_rule_versions_listed(self):
        status, payload = self._route("GET", "/rule-versions")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("active", payload["items"][0]["status"])

    def test_unknown_case_returns_404(self):
        status, payload = self._route("GET", "/cases/nope/lineage")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
