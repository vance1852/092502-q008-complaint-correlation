"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .cases import CaseService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记、投诉合并、版本研判、人工决定、确认与撤销的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        cases = CaseService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范区生态环境局")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="op-001",
                               display_name="夜间值班员", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="rv-001",
                               display_name="值班长", role="reviewer", organization_id="org-001")
        # 两家企业及区域/坐标台账
        for index, (site_id, name, lat) in enumerate((
                ("site-a", "甲化工有限公司", 31.2400),
                ("site-b", "乙涂装有限公司", 31.2500)), start=1):
            service.register_site(request_id=f"req-site-{index}", actor_id="admin-001", site_id=site_id,
                                  organization_id="org-001", name=name, timezone_name="Asia/Shanghai")
            service.record_domain_data(request_id=f"req-zone-{index}", actor_id="op-001", site_id=site_id,
                                       category="complaint_zone", external_key=f"zone-{site_id}",
                                       data={"region_code": "R3101", "name": name,
                                             "location": [lat, 121.5000]})
        # 甲企业当时在生产、治污设施旁路；乙企业停产
        service.record_domain_data(request_id="req-window-a", actor_id="op-001", site_id="site-a",
                                   category="operation_window", external_key="win-20260925",
                                   data={"windows": [{"start": "2026-09-25T20:00:00Z",
                                                      "end": "2026-09-26T02:00:00Z"}]})
        service.record_domain_data(request_id="req-treat-a", actor_id="op-001", site_id="site-a",
                                   category="treatment_status", external_key="treat-20260925-1",
                                   data={"status": "bypassed", "as_of": "2026-09-25T21:40:00Z",
                                         "odor_terms": ["化工", "刺鼻", "酸雾"]})
        # 平台已接收的区域气象快照：北风（污染物向南扩散）、低风速
        service.record_domain_data(request_id="req-weather", actor_id="op-001", site_id="",
                                   category="weather_snapshot", external_key="wx-20260925-2145",
                                   data={"region_code": "R3101", "wind_direction_deg": 0,
                                         "wind_speed_mps": 1.2, "as_of": "2026-09-25T21:45:00Z"})

        # 发布第一版关联规则
        rule_v1 = cases.ensure_seed_rule(actor_id="admin-001")

        # 两通重复来电：同一区域、30 分钟内、文本指纹相似
        first = cases.ingest_complaint(
            request_id="req-c1", actor_id="op-001", channel="hotline",
            occurred_at="2026-09-25T21:50:00Z", region_code="R3101",
            description="晚上九点五十，小区南边一直有刺鼻化工味，窗户不敢开",
            contact="13800138000", location=[31.2360, 121.5000], odor_terms=["刺鼻", "化工"])
        second = cases.ingest_complaint(
            request_id="req-c2", actor_id="op-001", channel="phone",
            occurred_at="2026-09-25T22:05:00Z", region_code="R3101",
            description="九点五十左右小区南边有刺鼻的化工味道，不敢开窗",
            contact="13800138000", location=[31.2362, 121.5001], odor_terms=["刺鼻"])
        case_id = first.resource_id
        assert second.resource_id == case_id and not first.replayed and not second.replayed

        # 生成第一版研判
        v1 = cases.generate_version(request_id="req-v1", actor_id="op-001", case_id=case_id,
                                    reason="夜间异味投诉初次研判")
        lineage = cases.get_case_lineage(case_id)
        top = lineage["versions"][-1]["candidates"][0]

        # 值班员排除乙企业并说明依据
        cases.decide_candidate(request_id="req-ex-b", actor_id="op-001", case_id=case_id,
                               decision="exclude", site_id="site-b",
                               reason="乙企业当日停产，投诉点与其方位不符")

        # 值班员确认候选并给出依据
        lineage = cases.get_case_lineage(case_id)
        frozen_version = lineage["current_version"]
        confirm = cases.confirm_case(
            request_id="req-confirm", actor_id="op-001", case_id=case_id,
            reason="下风向、治污旁路与居民描述一致，建议现场核查",
            confirmed_sites=["site-a"])

        # 确认后补录数据与发布新规则，不得改变已确认案件
        service.record_domain_data(request_id="req-window-b-late", actor_id="op-001", site_id="site-b",
                                   category="operation_window", external_key="win-late",
                                   data={"windows": [{"start": "2026-09-25T21:00:00Z",
                                                      "end": "2026-09-25T23:00:00Z"}]})
        blocked_regenerate = False
        try:
            cases.generate_version(request_id="req-v-after", actor_id="op-001", case_id=case_id,
                                   reason="补录后尝试重算")
        except Exception:
            blocked_regenerate = True

        # 撤销确认需要更高权限：确认者本人（值班员）不能撤销，值班长可以，原结论保留
        operator_revoke_blocked = False
        try:
            cases.revoke_confirmation(request_id="req-revoke-op", actor_id="op-001", case_id=case_id,
                                      reason="确认人本人尝试撤销")
        except Exception:
            operator_revoke_blocked = True
        revoke = cases.revoke_confirmation(request_id="req-revoke", actor_id="rv-001",
                                           case_id=case_id, reason="监测站数据需复核，暂缓结论")

        lineage = cases.get_case_lineage(case_id)
        valid, event_count = service.verify_audit()
        first_domain = service.record_domain_data(request_id="req-data", actor_id="op-001",
                                                  site_id="site-a", category="complaint_zone",
                                                  external_key="record-001",
                                                  data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="op-001", site_id="site-a",
                                            category="complaint_zone", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        result = {
            "status": "ok",
            "records": len(service.list_domain_data("site-a")),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first_domain.replayed,
            "second_replayed": replay.replayed,
            "case_id": case_id,
            "merged_events": len([e for e in lineage["events"]]),
            "rule_version": rule_v1,
            "top_candidate": top["site_id"],
            "top_confidence_interval": top["confidence_interval"],
            "frozen_version_at_confirm": frozen_version,
            "current_version": lineage["current_version"],
            "confirmed_then_frozen_against_new_data": blocked_regenerate,
            "self_revoke_blocked": operator_revoke_blocked,
            "conclusions_kept": [c["status"] for c in lineage["conclusions"]],
            "needs_field_inspection": lineage["needs_field_inspection"],
            "anonymized": lineage["anonymized"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
