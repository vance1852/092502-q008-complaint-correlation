import json
import unittest
from datetime import datetime, timezone

from complaint_core.clock import FixedClock
from complaint_core.correlation import ComplaintService, mask_name, mask_phone
from complaint_core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from complaint_core.service import DomainService
from complaint_core.storage import Database


class CorrelationServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.complaints = ComplaintService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="监管局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="ad", new_actor_id="op",
                                    display_name="值班员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="reviewer", actor_id="ad", new_actor_id="rv",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="ad", new_actor_id="au",
                                    display_name="审计员", role="auditor", organization_id="o1")
        for site_id, name in (("site-a", "A厂"), ("site-b", "B厂")):
            self.service.register_site(request_id=f"site-{site_id}", actor_id="op",
                                       site_id=site_id, organization_id="o1", name=name,
                                       timezone_name="Asia/Shanghai")
        self.complaints.register_rule_version(request_id="rule-v1", actor_id="ad",
                                              rule_id="night-rule",
                                              params={"min_score": 0.2, "min_text_jaccard": 0.05})
        self._record_zone("site-a", "化工 刺鼻 废气", lat=30.0, lon=120.0,
                          keywords=["化工", "刺鼻"])
        self._record_zone("site-b", "印染 异味 废水", lat=30.04, lon=120.04,
                          keywords=["印染"])
        self.service.record_domain_data(
            request_id="op-a", actor_id="op", site_id="site-a",
            category="operation_window", external_key="win-a",
            data={"observed_at": "2026-09-25T12:00Z", "production_running": True,
                  "treatment_online": False,
                  "windows": [{"start": "00:00", "end": "23:59",
                               "production_running": True,
                               "treatment_online": False}]})
        self.service.record_domain_data(
            request_id="wx-a", actor_id="op", site_id="site-a",
            category="weather_source", external_key="weather-a",
            data={"observed_at": "2026-09-25T11:30Z", "wind_direction_deg": 180.0,
                  "wind_speed_ms": 3.0, "stability_class": "F"})

    def tearDown(self):
        self.database.close()

    def _record_zone(self, site_id, text, *, lat, lon, keywords):
        self.service.record_domain_data(
            request_id=f"zone-{site_id}", actor_id="op", site_id=site_id,
            category="complaint_zone", external_key=f"zone-{site_id}",
            data={"zone_id": "zn", "lat": lat, "lon": lon,
                  "odor_keywords": keywords, "site_text": text})

    def _first_case_id(self):
        return self.complaints.list_cases()[0]["case_id"]

    def _log_event(self, request_id, description, *, at="2026-09-25T11:45:00Z",
                   lat=30.001, lon=120.001, contact=None):
        return self.complaints.register_complaint_event(
            request_id=request_id, actor_id="op", source="hotline", zone_id="zn",
            occurred_at=at, description=description, lat=lat, lon=lon, contact=contact)

    def test_masking_helpers(self):
        self.assertEqual(mask_name("王建国"), "王**")
        self.assertEqual(mask_phone("13812345678"), "138****5678")

    # ----- 去重合并与联系信息保护 -----

    def test_duplicate_calls_merge_by_time_zone_and_text_fingerprint(self):
        self._log_event("ev-1", "夜里闻到很浓的化工刺鼻味道，头晕")
        case_id = self._first_case_id()
        self._log_event("ev-2", "夜里又闻到很浓的化工刺鼻味道，头晕恶心",
                        at="2026-09-25T12:10:00Z")
        trace = self.complaints.get_case_trace(case_id)
        self.assertEqual(2, trace["event_count"])
        policies = {event["merge_basis"]["policy"] for event in trace["events"]}
        self.assertEqual({"new_case", "duplicate_merge"}, policies)
        self.assertEqual([1, 2], [v["version_no"] for v in trace["versions"]])

    def test_different_area_opens_separate_case(self):
        self._log_event("ev-1", "化工刺鼻味道")
        self.complaints.register_complaint_event(
            request_id="ev-2", actor_id="op", source="hotline", zone_id="zsouth",
            occurred_at="2026-09-25T11:50:00Z", description="化工刺鼻味道")
        self.assertEqual(2, len(self.complaints.list_cases()))

    def test_old_call_outside_window_opens_separate_case(self):
        self._log_event("ev-1", "化工刺鼻味道")
        self._log_event("ev-2", "化工刺鼻味道还是很浓",
                        at="2026-09-25T20:00:00Z")
        self.assertEqual(2, len(self.complaints.list_cases()))

    def test_case_view_never_exposes_contact_raw_values(self):
        self._log_event("ev-1", "化工刺鼻味道", contact={"name": "王建国", "phone": "13812345678"})
        case_id = self._first_case_id()
        public = json.dumps(self.complaints.get_case(case_id), ensure_ascii=False)
        trace = json.dumps(self.complaints.get_case_trace(case_id), ensure_ascii=False)
        self.assertNotIn("13812345678", public)
        self.assertNotIn("王建国", public)
        self.assertNotIn("13812345678", trace)
        self.assertNotIn("王建国", trace)

    def test_contact_raw_values_only_for_admin_and_access_is_audited(self):
        self._log_event("ev-1", "化工刺鼻味道", contact={"name": "王建国", "phone": "13812345678"})
        case_id = self._first_case_id()
        operator_view = self.complaints.get_contacts(actor_id="op", case_id=case_id)[0]
        self.assertTrue(operator_view["masked"])
        self.assertEqual("王**", operator_view["contact_name"])
        admin_view = self.complaints.get_contacts(actor_id="ad", case_id=case_id)[0]
        self.assertFalse(admin_view["masked"])
        self.assertEqual("13812345678", admin_view["contact_phone"])
        events = self.service.audit_events()
        self.assertTrue(any(event["action"] == "contact.revealed" for event in events))

    # ----- 规则版本 -----

    def test_only_one_active_rule_version(self):
        self.complaints.register_rule_version(request_id="rule-v2", actor_id="ad",
                                              rule_id="night-rule",
                                              params={"min_score": 0.1})
        active = [rule for rule in self.complaints.list_rules("night-rule")
                  if rule["status"] == "active"]
        self.assertEqual(1, len(active))
        self.assertEqual(2, active[0]["version"])

    def test_rule_registration_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.complaints.register_rule_version(request_id="rule-x", actor_id="op",
                                                  rule_id="other-rule", params={"x": 1})

    # ----- 候选、置信区间与现场核查建议 -----

    def test_candidates_include_factors_confidence_interval_and_inspection_flag(self):
        self._log_event("ev-1", "夜里化工刺鼻味道很浓")
        case_id = self._first_case_id()
        current = self.complaints.get_case_trace(case_id)["current_version"]
        self.assertEqual("night-rule", current["rule_id"])
        self.assertEqual(1, current["rule_version"])
        sites = {c["site_id"]: c for c in current["candidates"]}
        self.assertIn("site-a", sites)
        candidate = sites["site-a"]
        self.assertEqual("suggested", candidate["status"])
        self.assertGreater(candidate["score"], candidate["confidence_low"])
        self.assertLessEqual(candidate["confidence_high"], 1.0)
        codes = {factor["code"] for factor in candidate["factors"]}
        self.assertIn("downwind_transport", codes)
        self.assertTrue(current["engine_requires_site_inspection"])

    # ----- 人工决定 -----

    def test_manual_decision_requires_reason_and_freezes_snapshot(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        with self.assertRaises(ValidationError):
            self.complaints.decide(request_id="dec-bad", actor_id="op", case_id=case_id,
                                   action="exclude_candidate", site_id="site-a", reason="随便")
        self.complaints.decide(
            request_id="dec-1", actor_id="op", case_id=case_id,
            action="exclude_candidate", site_id="site-a",
            reason="来电位置与A厂直线距离虽近，但风向完全相反", expected_revision=0)
        trace = self.complaints.get_case_trace(case_id)
        candidate = next(c for c in trace["current_version"]["candidates"]
                         if c["site_id"] == "site-a")
        self.assertEqual("excluded", candidate["status"])
        decision = trace["current_version"]["manual_decisions"][0]
        self.assertTrue(decision["fact_snapshot_hash"])
        self.assertEqual(decision["fact_snapshot"]["generation_snapshot_hash"],
                         trace["current_version"]["generation_snapshot_hash"])
        self.assertEqual(1, trace["current_version"]["revision"])

    def test_operator_can_add_candidate_that_engine_missed(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        self.complaints.decide(
            request_id="dec-add", actor_id="op", case_id=case_id, action="add_candidate",
            site_id="site-b", reason="巡查发现B厂当夜旁路偷排，居民描述可对应",
            expected_revision=0)
        current = self.complaints.get_case_trace(case_id)["current_version"]
        added = next(c for c in current["candidates"] if c["site_id"] == "site-b")
        self.assertEqual("added", added["status"])
        self.assertEqual("manual", added["origin"])
        self.assertIsNone(added["score"])

    def test_adding_unknown_site_fails(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        with self.assertRaises(NotFoundError):
            self.complaints.decide(
                request_id="dec-missing", actor_id="op", case_id=case_id,
                action="add_candidate", site_id="site-zz",
                reason="尝试追加一个台账里不存在的场所")

    def test_stale_revision_is_rejected(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="exclude_candidate", site_id="site-a",
                               reason="第一次人工排除的依据内容", expected_revision=0)
        with self.assertRaises(ConflictError):
            self.complaints.decide(request_id="dec-2", actor_id="op", case_id=case_id,
                                   action="add_candidate", site_id="site-b",
                                   reason="拿着过期版本并发追加候选", expected_revision=0)

    def test_decisions_carry_over_to_regenerated_version(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="exclude_candidate", site_id="site-a",
                               reason="值班员首次研判时排除A厂的依据")
        # 补录新来电触发新版本
        self._log_event("ev-2", "化工刺鼻味道又出现了", at="2026-09-25T12:20:00Z")
        trace = self.complaints.get_case_trace(case_id)
        current = trace["current_version"]
        self.assertEqual(2, current["version_no"])
        candidate = next(c for c in current["candidates"] if c["site_id"] == "site-a")
        self.assertEqual("excluded", candidate["status"])
        carried = [d for d in current["manual_decisions"]
                   if d["detail"].get("carried_from_version_id")]
        self.assertEqual(1, len(carried))
        self.assertEqual("值班员首次研判时排除A厂的依据", carried[0]["reason"])

    # ----- 确认与冻结 -----

    def test_confirmation_freezes_case_against_new_data_and_rules(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        before = self.complaints.get_case_trace(case_id)
        snapshot_hash = before["current_version"]["generation_snapshot_hash"]
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="confirm_candidate", site_id="site-a",
                               reason="描述与A厂废气特征一致且其治污离线")
        self.complaints.confirm_case(
            request_id="cfm-1", actor_id="op", case_id=case_id,
            reason="多起指纹一致来电、风向吻合，需现场核查",
            site_inspection_required=True)

        # 规则更新不改变已确认案件
        self.complaints.register_rule_version(request_id="rule-v2", actor_id="ad",
                                              rule_id="night-rule",
                                              params={"min_score": 0.01})
        with self.assertRaises(ConflictError):
            self.complaints.regenerate(request_id="regen-1", actor_id="op", case_id=case_id)
        with self.assertRaises(ConflictError):
            self.complaints.decide(request_id="dec-late", actor_id="op", case_id=case_id,
                                   action="exclude_candidate", site_id="site-a",
                                   reason="确认后再想排除是不允许的")
        after = self.complaints.get_case_trace(case_id)
        self.assertEqual("confirmed", after["status"])
        self.assertEqual(snapshot_hash,
                         after["confirmation"]["generation_snapshot_hash"])
        self.assertTrue(after["confirmation"]["site_inspection_required"])

    def test_confirm_without_candidate_is_rejected(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="exclude_candidate", site_id="site-a",
                               reason="把唯一候选也排除掉")
        with self.assertRaises(ValidationError):
            self.complaints.confirm_case(
                request_id="cfm-bad", actor_id="op", case_id=case_id,
                reason="没有任何确认候选时不应允许确认结案",
                site_inspection_required=False)

    def test_frozen_case_is_not_mutated_by_similar_late_call(self):
        self._log_event("ev-1", "夜里闻到很浓的化工刺鼻味道，头晕")
        case_id = self._first_case_id()
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="confirm_candidate", site_id="site-a",
                               reason="特征一致、设施异常")
        self.complaints.confirm_case(request_id="cfm-1", actor_id="op", case_id=case_id,
                                     reason="证据链完整，转现场核查",
                                     site_inspection_required=True)
        receipt = self._log_event("ev-late", "夜里又闻到很浓的化工刺鼻味道，头晕恶心",
                                  at="2026-09-25T12:40:00Z")
        # 高相似晚到来电已留痕（有回执），但没有并入冻结案件
        self.assertFalse(receipt.replayed)
        frozen = self.complaints.get_case_trace(case_id)
        self.assertEqual(1, frozen["event_count"])
        self.assertEqual("confirmed", frozen["status"])
        late_event = self.database.connection.execute(
            "SELECT COUNT(*) c FROM complaint_events WHERE event_id=?",
            (receipt.resource_id,)).fetchone()["c"]
        self.assertEqual(1, late_event)
        linked = self.database.connection.execute(
            "SELECT COUNT(*) c FROM case_events WHERE event_id=?",
            (receipt.resource_id,)).fetchone()["c"]
        self.assertEqual(0, linked)

    # ----- 撤销确认 -----

    def test_revoke_requires_admin_and_retains_original_conclusion(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        self.complaints.decide(request_id="dec-1", actor_id="op", case_id=case_id,
                               action="confirm_candidate", site_id="site-a",
                               reason="特征一致")
        self.complaints.confirm_case(request_id="cfm-1", actor_id="op", case_id=case_id,
                                     reason="值班确认转现场核查",
                                     site_inspection_required=True)
        with self.assertRaises(PermissionDenied):
            self.complaints.revoke_confirmation(
                request_id="rev-op", actor_id="op", case_id=case_id,
                reason="值班员自己撤销自己的确认不应被允许")
        original_hash = self.complaints.get_case_trace(case_id)["confirmation"][
            "conclusion_snapshot_hash"]
        self.complaints.revoke_confirmation(
            request_id="rev-ad", actor_id="ad", case_id=case_id,
            reason="上级复核发现关键监测缺失，退回补充，原结论留存备查")
        trace = self.complaints.get_case_trace(case_id)
        self.assertEqual("revoked", trace["status"])
        self.assertTrue(trace["confirmation"]["retained_after_revoke"])
        self.assertEqual(original_hash,
                         trace["confirmation"]["conclusion_snapshot_hash"])
        self.assertIsNotNone(trace["confirmation"]["revocation"])

    # ----- 追溯链 -----

    def test_trace_links_anonymized_case_to_all_inputs(self):
        self._log_event("ev-1", "化工刺鼻味道")
        case_id = self._first_case_id()
        trace = self.complaints.get_case_trace(case_id)
        self.assertTrue(trace["case_code"].startswith("CASE-"))
        self.assertEqual(1, trace["event_count"])
        self.assertTrue(trace["events"][0]["fingerprint"])
        version = trace["versions"][0]
        self.assertEqual("night-rule", version["rule_id"])
        self.assertEqual(1, version["rule_version"])
        self.assertTrue(version["rule_hash"])
        snapshot = version["generation_snapshot"]
        self.assertEqual("correlation_generation_snapshot", snapshot["snapshot_type"])
        self.assertTrue(snapshot["events"])
        self.assertTrue(snapshot["weather"])
        self.assertEqual("night-rule", snapshot["rule"]["rule_id"])

    def test_idempotent_replay_returns_same_receipt(self):
        first = self._log_event("ev-1", "化工刺鼻味道")
        second = self._log_event("ev-1", "化工刺鼻味道")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)


if __name__ == "__main__":
    unittest.main()
