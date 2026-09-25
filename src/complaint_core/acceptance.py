"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .correlation import ComplaintService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        complaints = ComplaintService(database, service.clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="环保负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="complaint_zone", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="complaint_zone", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 投诉关联：规则版本、场所/生产/气象快照、两起重复来电、人工研判与确认
        complaints.register_rule_version(
            request_id="req-rule", actor_id="admin-001", rule_id="night-odor",
            params={"min_score": 0.1, "min_text_jaccard": 0.0})
        service.record_domain_data(request_id="req-zone", actor_id="operator-001", site_id="site-001",
                                   category="complaint_zone", external_key="zone-001",
                                   data={"zone_id": "zone-north", "lat": 30.0, "lon": 120.0,
                                         "odor_keywords": ["化工", "刺鼻"],
                                         "site_text": "化工 刺鼻 废气"})
        service.record_domain_data(request_id="req-operation", actor_id="operator-001", site_id="site-001",
                                   category="operation_window", external_key="op-001",
                                   data={"observed_at": "2026-09-25T08:00Z",
                                         "production_running": True, "treatment_online": False,
                                         "windows": [{"start": "00:00", "end": "23:59",
                                                      "production_running": True,
                                                      "treatment_online": False}]})
        service.record_domain_data(request_id="req-weather", actor_id="operator-001", site_id="site-001",
                                   category="weather_source", external_key="wx-001",
                                   data={"observed_at": "2026-09-25T07:30Z",
                                         "wind_direction_deg": 180.0, "wind_speed_ms": 3.0})
        complaints.register_complaint_event(
            request_id="req-event-1", actor_id="operator-001", source="hotline",
            zone_id="zone-north", occurred_at="2026-09-25T07:45:00Z",
            description="夜里闻到很浓的化工刺鼻味道", lat=30.001, lon=120.001,
            contact={"name": "王建国", "phone": "13812345678"})
        complaints.register_complaint_event(
            request_id="req-event-2", actor_id="operator-001", source="hotline",
            zone_id="zone-north", occurred_at="2026-09-25T08:05:00Z",
            description="夜里又闻到很浓的化工刺鼻味道，头晕")
        case_id = complaints.list_cases("zone-north")[0]["case_id"]
        version = complaints.get_case_trace(case_id)["current_version"]
        complaints.decide(request_id="req-decide", actor_id="operator-001", case_id=case_id,
                          action="confirm_candidate", site_id="site-001",
                          reason="来电指纹一致，场所生产中且治污离线，风向吻合",
                          expected_revision=0)
        complaints.confirm_case(request_id="req-confirm", actor_id="operator-001", case_id=case_id,
                                reason="夜间异味证据链完整，转现场核查",
                                site_inspection_required=True)
        trace = complaints.get_case_trace(case_id)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "case_code": trace["case_code"],
                  "merged_events": trace["event_count"],
                  "rule_version": f"{version['rule_id']}:v{version['rule_version']}",
                  "candidate_status": trace["confirmation"]["confirmed_sites"][0]["site_id"],
                  "site_inspection_required":
                  trace["confirmation"]["site_inspection_required"],
                  "case_status": trace["status"],
                  "contact_masked":
                  complaints.get_contacts(actor_id="operator-001", case_id=case_id)[0]["masked"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
