import json
import threading
import unittest
from datetime import datetime, timezone

from complaint_core.cases import CaseService
from complaint_core.clock import FixedClock
from complaint_core.correlation import (
    contact_token,
    fingerprint_similarity,
    mask_contact,
    text_fingerprint,
)
from complaint_core.errors import ConflictError, PermissionDenied, ValidationError
from complaint_core.service import DomainService
from complaint_core.storage import Database


class CaseServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.cases = CaseService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="示范区局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for key, actor_id, role in (("op", "op1", "operator"), ("rv", "rv1", "reviewer"),
                                    ("au", "au1", "auditor")):
            self.service.register_actor(request_id=key, actor_id="a1", new_actor_id=actor_id,
                                        display_name=role, role=role, organization_id="o1")
        for site_id, name, lat in (("s1", "甲化工", 31.2400), ("s2", "乙涂装", 31.2500)):
            self.service.register_site(request_id=f"site-{site_id}", actor_id="a1", site_id=site_id,
                                       organization_id="o1", name=name, timezone_name="Asia/Shanghai")
            self.service.record_domain_data(request_id=f"zone-{site_id}", actor_id="op1", site_id=site_id,
                                            category="complaint_zone", external_key=f"z-{site_id}",
                                            data={"region_code": "R1", "name": name,
                                                  "location": [lat, 121.5000]})
        self.service.record_domain_data(request_id="win1", actor_id="op1", site_id="s1",
                                        category="operation_window", external_key="w1",
                                        data={"windows": [{"start": "2026-09-25T20:00:00Z",
                                                           "end": "2026-09-26T02:00:00Z"}]})
        self.service.record_domain_data(request_id="treat1", actor_id="op1", site_id="s1",
                                        category="treatment_status", external_key="t1",
                                        data={"status": "bypassed", "as_of": "2026-09-25T21:40:00Z",
                                              "odor_terms": ["化工", "刺鼻"]})
        self.service.record_domain_data(request_id="wx1", actor_id="op1", site_id="",
                                        category="weather_snapshot", external_key="wx-2145",
                                        data={"region_code": "R1", "wind_direction_deg": 0,
                                              "wind_speed_mps": 1.2, "as_of": "2026-09-25T21:45:00Z"})
        self.cases.ensure_seed_rule(actor_id="a1")

    def tearDown(self):
        self.database.close()

    def _ingest(self, request_id, *, when="2026-09-25T21:50:00Z", region="R1",
                description="小区南边一直有刺鼻化工味，窗户不敢开", contact="13800138000",
                location=(31.2360, 121.5000)):
        return self.cases.ingest_complaint(
            request_id=request_id, actor_id="op1", channel="hotline", occurred_at=when,
            region_code=region, description=description, contact=contact,
            location=list(location) if location else None, odor_terms=["刺鼻", "化工"])

    # ------------------------------------------------------------------
    # 合并与联系方式保护
    # ------------------------------------------------------------------

    def test_fingerprint_identifies_similar_text(self):
        left = text_fingerprint("小区南边一直有刺鼻化工味，窗户不敢开")
        right = text_fingerprint("小区南边有刺鼻的化工味道，不敢开窗")
        self.assertGreaterEqual(fingerprint_similarity(left, right), 0.6)
        unrelated = text_fingerprint("凌晨工地施工噪音太大无法入睡")
        self.assertLess(fingerprint_similarity(left, unrelated), 0.6)

    def test_contact_is_tokenized_and_masked(self):
        self.assertEqual(mask_contact("13800138000"), "138" + "*" * 6 + "00")
        token_a = contact_token("138-0013-8000")
        token_b = contact_token("13800138000")
        self.assertEqual(token_a, token_b)
        self.assertNotIn("13800138000", token_a)

    def test_duplicate_calls_merge_by_time_region_fingerprint(self):
        first = self._ingest("c1")
        second = self._ingest("c2", when="2026-09-25T22:05:00Z",
                              description="小区南边有刺鼻的化工味道，不敢开窗",
                              location=(31.2362, 121.5001))
        self.assertEqual(first.resource_id, second.resource_id)
        lineage = self.cases.get_case_lineage(first.resource_id)
        self.assertEqual(2, len(lineage["events"]))
        self.assertTrue(any("text_fingerprint" in r for r in lineage["events"][1]["merge_reasons"]))
        # 只暴露掩码，不暴露原始联系方式
        raw = json.dumps(lineage, ensure_ascii=False)
        self.assertNotIn("13800138000", raw)
        self.assertIn("138******00", raw)

    def test_different_region_opens_new_case(self):
        first = self._ingest("c1")
        other = self._ingest("c2", region="R2")
        self.assertNotEqual(first.resource_id, other.resource_id)

    def test_call_outside_window_does_not_merge(self):
        first = self._ingest("c1")
        later = self._ingest("c2", when="2026-09-25T23:30:00Z")
        self.assertNotEqual(first.resource_id, later.resource_id)

    # ------------------------------------------------------------------
    # 版本化研判
    # ------------------------------------------------------------------

    def test_generated_version_ranks_candidates_with_confidence_interval(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id,
                                    reason="初次研判")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual(1, lineage["current_version"])
        candidates = lineage["versions"][0]["candidates"]
        self.assertEqual("s1", candidates[0]["site_id"])
        interval = candidates[0]["confidence_interval"]
        self.assertGreaterEqual(interval["lower"], 0.0)
        self.assertLessEqual(interval["upper"], 1.0)
        self.assertLessEqual(interval["lower"], candidates[0]["score"])
        self.assertTrue(lineage["needs_field_inspection"])
        # 每个候选都带贡献因素
        self.assertIn("downwind", candidates[0]["factors"])

    def test_generate_without_published_rule_is_rejected(self):
        database = Database()
        try:
            bare_service = DomainService(database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
            bare_cases = CaseService(database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
            bare_service.register_organization(request_id="org-bare", actor_id="bootstrap",
                                               organization_id="o1", name="局")
            bare_service.register_actor(request_id="act-bare", actor_id="bootstrap", new_actor_id="op1",
                                        display_name="值班员", role="operator", organization_id="o1")
            with self.assertRaises(ConflictError):
                bare_cases.generate_version(request_id="v", actor_id="op1", case_id="missing", reason="x")
        finally:
            database.close()

    def test_rule_update_uses_new_version_but_keeps_history(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="v1")
        # 发布更宽松的新规则（更大距离上限）
        import copy
        from complaint_core.correlation import rule_spec
        spec2 = copy.deepcopy(rule_spec())
        spec2["spec_version"] = 2
        spec2["distance_meters_max"] = 5000.0
        self.cases.publish_rule_version(request_id="rule2", actor_id="a1", spec=spec2, note="扩区")
        self.cases.generate_version(request_id="v2", actor_id="op1", case_id=case_id, reason="v2")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual(2, lineage["current_version"])
        self.assertEqual(1, lineage["versions"][0]["rule_version"])
        self.assertEqual(2, lineage["versions"][1]["rule_version"])
        self.assertEqual(2, len({v["rule_spec_hash"] for v in lineage["versions"]}))

    # ------------------------------------------------------------------
    # 人工决定
    # ------------------------------------------------------------------

    def test_decision_requires_reason(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        with self.assertRaises(ValidationError):
            self.cases.decide_candidate(request_id="d", actor_id="op1", case_id=case_id,
                                        decision="exclude", site_id="s2", reason="  ")

    def test_exclude_then_add_creates_versions_and_decisions(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        self.cases.decide_candidate(request_id="d1", actor_id="op1", case_id=case_id,
                                    decision="exclude", site_id="s2", reason="乙企业停产")
        # 已排除的不能再次排除
        with self.assertRaises(ConflictError):
            self.cases.decide_candidate(request_id="d2", actor_id="op1", case_id=case_id,
                                        decision="exclude", site_id="s2", reason="重复排除")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual(2, lineage["current_version"])
        excluded = next(c for c in lineage["versions"][1]["candidates"] if c["site_id"] == "s2")
        self.assertEqual("excluded", excluded["status"])
        self.assertEqual("乙企业停产", excluded["exclude_reason"])
        decision = lineage["decisions"][0]
        self.assertEqual("excluded_candidate", decision["action"])
        self.assertTrue(decision["facts_hash"])
        self.assertNotEqual(decision["from_state_hash"], decision["to_state_hash"])

    def test_auditor_cannot_decide(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        with self.assertRaises(PermissionDenied):
            self.cases.decide_candidate(request_id="d", actor_id="au1", case_id=case_id,
                                        decision="exclude", site_id="s2", reason="x")

    # ------------------------------------------------------------------
    # 确认冻结与撤销
    # ------------------------------------------------------------------

    def _confirmed_case(self, confirmer="op1"):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        self.cases.confirm_case(request_id="cf", actor_id=confirmer, case_id=case_id,
                                reason="证据链一致", confirmed_sites=["s1"])
        return case_id

    def test_operator_can_confirm_but_auditor_cannot(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        with self.assertRaises(PermissionDenied):
            self.cases.confirm_case(request_id="cf-au", actor_id="au1", case_id=case_id, reason="x")
        receipt = self.cases.confirm_case(request_id="cf", actor_id="op1", case_id=case_id,
                                          reason="证据链一致")
        self.assertFalse(receipt.replayed)

    def test_confirmed_case_is_frozen_against_new_data_and_rules(self):
        case_id = self._confirmed_case()
        lineage = self.cases.get_case_lineage(case_id)
        frozen_candidates = json.dumps(lineage["versions"][0]["candidates"], sort_keys=True)
        # 补录数据
        self.service.record_domain_data(request_id="late", actor_id="op1", site_id="s2",
                                        category="treatment_status", external_key="late1",
                                        data={"status": "abnormal", "as_of": "2026-09-25T22:00:00Z"})
        # 发布新规则
        import copy
        from complaint_core.correlation import rule_spec
        spec2 = copy.deepcopy(rule_spec())
        spec2["spec_version"] = 2
        self.cases.publish_rule_version(request_id="rule2", actor_id="a1", spec=spec2)
        # 已确认案件不能再生成版本或做人工决定
        with self.assertRaises(ConflictError):
            self.cases.generate_version(request_id="v2", actor_id="op1", case_id=case_id, reason="x")
        with self.assertRaises(ConflictError):
            self.cases.decide_candidate(request_id="d", actor_id="op1", case_id=case_id,
                                        decision="add", site_id="s2", reason="x")
        with self.assertRaises(ConflictError):
            self.cases.confirm_case(request_id="cf2", actor_id="rv1", case_id=case_id, reason="x")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual("confirmed", lineage["case"]["status"])
        self.assertEqual(frozen_candidates,
                         json.dumps(lineage["versions"][0]["candidates"], sort_keys=True))
        conclusion = lineage["conclusions"][0]
        self.assertEqual("active", conclusion["status"])
        self.assertEqual(["s1"], conclusion["payload"]["confirmed_sites"])

    def test_confirm_blocked_when_facts_changed_since_version(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        self.service.record_domain_data(request_id="late", actor_id="op1", site_id="s2",
                                        category="treatment_status", external_key="late1",
                                        data={"status": "abnormal", "as_of": "2026-09-25T22:00:00Z"})
        with self.assertRaises(ConflictError):
            self.cases.confirm_case(request_id="cf", actor_id="rv1", case_id=case_id, reason="x")

    def test_revoke_requires_higher_role_and_keeps_original_conclusion(self):
        # 值班员确认 → 本人不能撤销，值班长（更高权限）可以
        case_id = self._confirmed_case(confirmer="op1")
        with self.assertRaises(PermissionDenied):
            self.cases.revoke_confirmation(request_id="rvk-self", actor_id="op1",
                                           case_id=case_id, reason="确认人本人撤销")
        self.cases.revoke_confirmation(request_id="rvk", actor_id="rv1", case_id=case_id,
                                       reason="监测数据复核")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual("open", lineage["case"]["status"])
        self.assertEqual("revoked", lineage["conclusions"][0]["status"])
        self.assertEqual("监测数据复核", lineage["conclusions"][0]["revoke_reason"])
        self.assertEqual("rv1", lineage["conclusions"][0]["revoked_by"])
        # 原结论内容仍可追溯
        self.assertEqual(["s1"], lineage["conclusions"][0]["payload"]["confirmed_sites"])
        actions = {d["action"] for d in lineage["decisions"]}
        self.assertIn("revoked_confirmation", actions)
        # 撤销后可以基于新事实继续工作
        self.cases.generate_version(request_id="v2", actor_id="op1", case_id=case_id, reason="复核重算")
        self.assertEqual(2, self.cases.get_case_lineage(case_id)["current_version"])

    def test_same_level_cannot_revoke_but_admin_can(self):
        # 值班长确认 → 同级值班长不能撤销，仅管理员可以
        case_id = self._confirmed_case(confirmer="rv1")
        with self.assertRaises(PermissionDenied):
            self.cases.revoke_confirmation(request_id="rvk-rv", actor_id="rv1",
                                           case_id=case_id, reason="同级撤销")
        self.cases.revoke_confirmation(request_id="rvk-admin", actor_id="a1",
                                       case_id=case_id, reason="管理员复核")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertEqual("revoked", lineage["conclusions"][0]["status"])

    # ------------------------------------------------------------------
    # 并发：同一投诉只能有一个当前版本
    # ------------------------------------------------------------------

    def test_concurrent_version_generation_keeps_single_current(self):
        case_id = self._ingest("c1").resource_id
        errors: list[Exception] = []

        def generate(request_id: str):
            try:
                self.cases.generate_version(request_id=request_id, actor_id="op1",
                                            case_id=case_id, reason=f"并发 {request_id}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=generate, args=(f"v{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 串行写锁下全部成功（锁等待），或个别快速失败，但只能有一个当前版本
        rows = self.database.connection.execute(
            "SELECT version, is_current FROM case_versions WHERE case_id=? ORDER BY version",
            (case_id,)).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(1, sum(row["is_current"] for row in rows))
        self.assertEqual(len(rows), rows[-1]["version"])

    # ------------------------------------------------------------------
    # 溯源
    # ------------------------------------------------------------------

    def test_lineage_traces_anonymized_case_to_inputs_rules_and_decisions(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        self.cases.decide_candidate(request_id="d1", actor_id="op1", case_id=case_id,
                                    decision="exclude", site_id="s2", reason="乙停产")
        lineage = self.cases.get_case_lineage(case_id)
        self.assertTrue(lineage["anonymized"])
        self.assertEqual(1, len(lineage["events"]))
        version = lineage["versions"][-1]
        self.assertTrue(version["rule_spec_hash"])
        self.assertTrue(version["fact_snapshot_id"])
        self.assertEqual("manual_exclude", version["origin"])
        self.assertEqual(1, len(lineage["decisions"]))
        self.assertEqual("excluded_candidate", lineage["decisions"][0]["action"])
        self.assertTrue(lineage["needs_field_inspection"] in (True, False))
        # 全程可 JSON 序列化
        json.dumps(lineage, ensure_ascii=False)

    def test_frozen_snapshot_can_be_retrieved_and_matches_version(self):
        case_id = self._ingest("c1").resource_id
        self.cases.generate_version(request_id="v1", actor_id="op1", case_id=case_id, reason="r")
        lineage = self.cases.get_case_lineage(case_id)
        snapshot_id = lineage["versions"][0]["fact_snapshot_id"]
        facts_hash = lineage["versions"][0]["facts_hash"]
        snapshot = self.cases.get_snapshot(snapshot_id)
        self.assertEqual("analysis", snapshot["kind"])
        self.assertEqual(facts_hash, snapshot["facts_hash"])
        # 快照里冻结了输入事件、企业、资料与气象
        self.assertEqual(1, len(snapshot["facts"]["events"]))
        self.assertIn("weather", snapshot["facts"])
        listed = self.cases.list_snapshots(case_id)
        self.assertIn(snapshot_id, {item["snapshot_id"] for item in listed})


if __name__ == "__main__":
    unittest.main()
