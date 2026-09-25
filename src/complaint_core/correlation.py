"""投诉关联服务。

职责边界：
- 按时间窗、区域和文本指纹合并重复来电，联系信息独立存放、默认脱敏；
- 使用带版本的关联规则生成候选企业、贡献因素与置信区间；
- 人工排除、追加、确认都必须给出书面依据，并冻结当时的事实快照；
- 已确认案件不可被补录数据或规则更新改变，撤销确认需要更高权限且保留原结论；
- 所有写事务立即加锁，配合部分唯一索引保证同一案件只有一个当前版本。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:  # 标准库 zoneinfo，缺失时退化为 UTC 比较
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .engine import (
    SiteEvidence,
    WeatherSnapshot,
    anonymized_case_code,
    correlate,
    jaccard,
    normalize_text,
    text_fingerprint,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 重复来电合并策略（与研判规则相互独立，记录在合并依据中便于审计）
MERGE_WINDOW_MINUTES = 360
MERGE_MIN_JACCARD = 0.45
# 气象快照相对首起投诉时间的最大可接受偏差
WEATHER_LOOKUP_MINUTES = 180
SNAPSHOT_CATEGORIES = ("complaint_zone", "operation_window", "weather_source")

# 候选状态允许的人工状态迁移
CANDIDATE_TRANSITIONS = {
    "suggested": {"excluded", "confirmed"},
    "added": {"excluded", "confirmed"},
    "confirmed": {"excluded"},
    "excluded": set(),
}


def _parse_dt(value: str, field: str) -> datetime:
    """解析必须带时区的 ISO8601 时间。"""

    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mask_name(name: str) -> str:
    """姓名脱敏：保留首字。"""

    name = str(name or "").strip()
    if not name:
        return ""
    return name[0] + "*" * (len(name) - 1) if len(name) > 1 else name[0]


def mask_phone(phone: str) -> str:
    """电话脱敏：保留前三位与后四位。"""

    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 5:
        return "*" * len(digits)
    if len(digits) <= 7:
        return digits[0] + "*" * (len(digits) - 2) + digits[-1]
    return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]


class ComplaintService:
    """协调投诉去重、关联研判、人工决定与审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ----- 基础工具 -----

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return _iso(self._now_dt())

    def _identifier(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _reason(self, value: str) -> str:
        value = str(value or "").strip()
        if len(value) < 4 or len(value) > 500:
            raise ValidationError("人工决定必须填写 4-500 字的书面依据")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ----- 规则版本管理 -----

    def register_rule_version(self, *, request_id: str, actor_id: str, rule_id: str,
                              params: dict[str, Any], activate: bool = True,
                              note: str = "") -> WriteReceipt:
        """登记规则新版本；activate=True 时旧生效版本自动退役，同时至多一个生效版本。"""

        if not isinstance(params, dict) or not params:
            raise ValidationError("params 必须是非空对象")
        payload = {"actor_id": actor_id, "rule_id": rule_id, "params": params,
                   "activate": bool(activate), "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule_id = self._identifier(rule_id, "rule_id")
            note = str(note or "")[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM correlation_rules WHERE rule_id=?",
                    (rule_id,),
                ).fetchone()
                version = row["version"] + 1
                content_hash = digest({"rule_id": rule_id, "version": version, "params": params})
                resource_id = f"{rule_id}:v{version}"
                if activate:
                    # 部分唯一索引要求同一 rule_id 至多一个 active：先退役旧版本
                    connection.execute(
                        "UPDATE correlation_rules SET status='retired' WHERE rule_id=?",
                        (rule_id,),
                    )
                connection.execute(
                    "INSERT INTO correlation_rules(rule_id,version,status,params_json,content_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (rule_id, version, "active" if activate else "retired",
                     canonical_json(params), content_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="rule.version_registered",
                             resource_type="correlation_rule", resource_id=resource_id,
                             detail={"rule_id": rule_id, "version": version,
                                     "activate": bool(activate), "content_hash": content_hash,
                                     "note": note}, occurred_at=self._now())
                return "correlation_rule", resource_id, {"rule_id": rule_id, "version": version,
                                                         "content_hash": content_hash,
                                                         "status": "active" if activate else "retired"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_rule_version", payload=payload, create=create)

    def list_rules(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM correlation_rules"
        parameters: list[Any] = []
        if rule_id:
            query += " WHERE rule_id=?"
            parameters.append(rule_id)
        query += " ORDER BY rule_id, version"
        with self.database.read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._rule_dict(row) for row in rows]

    def _rule_dict(self, row) -> dict[str, Any]:
        return {"rule_id": row["rule_id"], "version": row["version"], "status": row["status"],
                "params": json.loads(row["params_json"]), "content_hash": row["content_hash"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def _active_rule(self, connection):
        row = connection.execute(
            "SELECT * FROM correlation_rules WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ConflictError("尚无生效的关联规则版本")
        return row

    # ----- 投诉事件登记与重复合并 -----

    def register_complaint_event(self, *, request_id: str, actor_id: str, source: str,
                                 zone_id: str, occurred_at: str, description: str,
                                 location_text: str = "", lat: float | None = None,
                                 lon: float | None = None,
                                 contact: dict[str, str] | None = None,
                                 event_id: str | None = None) -> WriteReceipt:
        """登记一起来电（或其他来源投诉），并自动合并到同区域时间窗内的未结案件。"""

        payload = {"actor_id": actor_id, "source": source, "zone_id": zone_id,
                   "occurred_at": occurred_at, "description": description,
                   "location_text": location_text, "lat": lat, "lon": lon,
                   "contact": contact, "event_id": event_id}
        occurred = _parse_dt(occurred_at, "occurred_at")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            source = self._text(source, "source", 80)
            zone_id = self._identifier(zone_id, "zone_id")
            description = self._text(description, "description", 2000)
            location_text = str(location_text or "")[:300]
            lat, lon = self._coordinates(lat, lon)
            contact_name = contact_phone = ""
            if contact:
                contact_name = self._text(contact.get("name", ""), "contact.name", 100)
                contact_phone = self._text(contact.get("phone", ""), "contact.phone", 40)
            if event_id:
                event_id = self._identifier(event_id, "event_id")
                if connection.execute("SELECT 1 FROM complaint_events WHERE event_id=?",
                                      (event_id,)).fetchone():
                    raise ConflictError("事件编号已经存在")
            else:
                event_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                fingerprint, pieces = text_fingerprint(description)
                connection.execute(
                    "INSERT INTO complaint_events(event_id,source,zone_id,occurred_at,received_at,"
                    "location_text,lat,lon,description,normalized_text,shingles_json,fingerprint,"
                    "contact_id,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, source, zone_id, _iso(occurred), self._now(), location_text,
                     lat, lon, description, normalize_text(description),
                     canonical_json(pieces), fingerprint,
                     "held" if contact_name or contact_phone else None,
                     digest({"source": source, "zone_id": zone_id, "occurred_at": _iso(occurred),
                             "description": description, "location_text": location_text,
                             "lat": lat, "lon": lon}),
                     actor_id, self._now()),
                )
                if contact_name or contact_phone:
                    connection.execute(
                        "INSERT INTO event_contacts(contact_id,event_id,contact_name,contact_phone,"
                        "masked_name,masked_phone,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, event_id, contact_name, contact_phone,
                         mask_name(contact_name), mask_phone(contact_phone), self._now()),
                    )
                case_id, merge_state = self._merge_or_open_case(connection, event_id=event_id,
                                                                zone_id=zone_id, occurred=occurred,
                                                                pieces=pieces, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="complaint_event.registered",
                             resource_type="complaint_event", resource_id=event_id,
                             detail={"case_id": case_id, "zone_id": zone_id,
                                     "occurred_at": _iso(occurred), "merge_state": merge_state,
                                     "fingerprint": fingerprint, "contact_held": bool(contact_name)},
                             occurred_at=self._now())
                response: dict[str, Any] = {"event_id": event_id, "case_id": case_id,
                                            "merge_state": merge_state}
                if merge_state == "blocked_frozen":
                    response["linked"] = False
                    response["message"] = "来电与已冻结案件高度相似，已留痕但未自动并入"
                else:
                    response["merged"] = merge_state == "merged"
                return "complaint_event", event_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="register_complaint_event", payload=payload, create=create)

    def _coordinates(self, lat: Any, lon: Any) -> tuple[float | None, float | None]:
        if lat is None and lon is None:
            return None, None
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise ValidationError("经纬度必须是数值") from exc
        if not -90.0 <= lat_f <= 90.0 or not -180.0 <= lon_f <= 180.0:
            raise ValidationError("经纬度超出合法范围")
        return lat_f, lon_f

    def _merge_or_open_case(self, connection, *, event_id: str, zone_id: str,
                            occurred: datetime, pieces: list[str], actor_id: str) -> tuple[str, str]:
        """在同区域、时间窗内按文本指纹找最相似案件。

        返回 (case_id, 状态)，状态取值：
        - "new"：未找到相似案件，已开新案并生成首版关联结果；
        - "merged"：并入未结案件并重新生成关联版本；
        - "blocked_frozen"：命中已确认/已撤销案件。事件已留痕但不并入，
          冻结案件不发生任何变化，是否另案处理交由人工。
        """

        window = timedelta(minutes=MERGE_WINDOW_MINUTES)
        best: tuple[float, str, str] | None = None
        rows = connection.execute(
            "SELECT c.case_id,c.status,ce.event_id,e.occurred_at,e.shingles_json "
            "FROM case_events ce JOIN complaint_events e ON e.event_id=ce.event_id "
            "JOIN complaint_cases c ON c.case_id=ce.case_id "
            "WHERE c.zone_id=?",
            (zone_id,),
        ).fetchall()
        for row in rows:
            candidate_time = _parse_dt(row["occurred_at"], "occurred_at")
            if abs(occurred - candidate_time) > window:
                continue
            similarity = jaccard(pieces, json.loads(row["shingles_json"]))
            if similarity >= MERGE_MIN_JACCARD and (best is None or similarity > best[0]):
                best = (similarity, row["case_id"], row["status"])

        if best is None:
            case_id = uuid.uuid4().hex
            case_code = anonymized_case_code(case_id)
            connection.execute(
                "INSERT INTO complaint_cases(case_id,case_code,zone_id,status,first_occurred_at,"
                "last_occurred_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, case_code, zone_id, "open", _iso(occurred), _iso(occurred),
                 actor_id, self._now()),
            )
            self._link_event(connection, case_id=case_id, event_id=event_id,
                             basis={"policy": "new_case", "window_minutes": MERGE_WINDOW_MINUTES,
                                    "min_jaccard": MERGE_MIN_JACCARD})
            connection.execute(
                "UPDATE complaint_cases SET first_occurred_at=?, last_occurred_at=? WHERE case_id=?",
                (_iso(occurred), _iso(occurred), case_id),
            )
            rule = connection.execute(
                "SELECT * FROM correlation_rules WHERE status='active' LIMIT 1"
            ).fetchone()
            if rule is not None:
                self._generate_version(connection, case_id=case_id, rule=rule, created_by=actor_id)
            return case_id, "new"

        similarity, case_id, case_status = best
        if case_status != "open":
            # 事件已入库留痕，但不并入冻结案件；只记审计，不改案件任何字段
            append_event(connection, actor_id=actor_id, action="case.merge_blocked_frozen",
                         resource_type="complaint_case", resource_id=case_id,
                         detail={"event_id": event_id, "case_status": case_status,
                                 "jaccard": round(similarity, 4),
                                 "window_minutes": MERGE_WINDOW_MINUTES},
                         occurred_at=self._now())
            return case_id, "blocked_frozen"
        self._link_event(connection, case_id=case_id, event_id=event_id,
                         basis={"policy": "duplicate_merge", "window_minutes": MERGE_WINDOW_MINUTES,
                                "min_jaccard": MERGE_MIN_JACCARD, "jaccard": round(similarity, 4)})
        case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?", (case_id,)).fetchone()
        first = min(_parse_dt(case["first_occurred_at"], "first_occurred_at"), occurred)
        last = max(_parse_dt(case["last_occurred_at"], "last_occurred_at"), occurred)
        connection.execute(
            "UPDATE complaint_cases SET first_occurred_at=?, last_occurred_at=? WHERE case_id=?",
            (_iso(first), _iso(last), case_id),
        )
        append_event(connection, actor_id=actor_id, action="case.event_merged",
                     resource_type="complaint_case", resource_id=case_id,
                     detail={"event_id": event_id, "jaccard": round(similarity, 4),
                             "window_minutes": MERGE_WINDOW_MINUTES}, occurred_at=self._now())
        rule = connection.execute(
            "SELECT * FROM correlation_rules WHERE status='active' LIMIT 1"
        ).fetchone()
        if rule is not None:
            self._generate_version(connection, case_id=case_id, rule=rule, created_by=actor_id)
        return case_id, "merged"

    def _link_event(self, connection, *, case_id: str, event_id: str, basis: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO case_events(case_id,event_id,merge_basis_json,created_at) VALUES(?,?,?,?)",
            (case_id, event_id, canonical_json(basis), self._now()),
        )

    # ----- 事实快照与候选生成 -----

    def regenerate(self, *, request_id: str, actor_id: str, case_id: str,
                   rule_id: str | None = None) -> WriteReceipt:
        """用当前事实和指定（或缺省生效）规则生成新的当前版本；已确认案件拒绝重算。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "rule_id": rule_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._require_open_case(connection, case_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if rule_id:
                    rule = connection.execute(
                        "SELECT * FROM correlation_rules WHERE rule_id=? AND status='active'",
                        (rule_id,),
                    ).fetchone()
                    if rule is None:
                        raise NotFoundError("该规则没有生效版本")
                else:
                    rule = self._active_rule(connection)
                version_id = self._generate_version(connection, case_id=case_id, rule=rule,
                                                    created_by=actor_id)
                return "correlation_version", version_id, {"case_id": case_id,
                                                            "version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="regenerate_correlation", payload=payload, create=create)

    def _require_open_case(self, connection, case_id: str):
        case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?",
                                  (case_id,)).fetchone()
        if case is None:
            raise NotFoundError("案件不存在")
        if case["status"] != "open":
            raise ConflictError(f"案件状态为 {case['status']}，研判结论已冻结，不能修改")
        return case

    def _latest_records(self, connection, category: str):
        return connection.execute(
            "SELECT dr.* FROM domain_records dr JOIN ("
            "SELECT site_id, MAX(created_at) AS latest FROM domain_records "
            "WHERE category=? GROUP BY site_id"
            ") t ON t.site_id=dr.site_id AND t.latest=dr.created_at WHERE dr.category=?",
            (category, category),
        ).fetchall()

    def _build_snapshot(self, connection, case, rule) -> tuple[dict[str, Any], list[SiteEvidence], WeatherSnapshot | None, str, float | None, float | None]:
        zone_id = case["zone_id"]
        anchor = _parse_dt(case["first_occurred_at"], "first_occurred_at")
        event_rows = connection.execute(
            "SELECT e.* FROM case_events ce JOIN complaint_events e ON e.event_id=ce.event_id "
            "WHERE ce.case_id=? ORDER BY e.occurred_at, e.event_id",
            (case["case_id"],),
        ).fetchall()
        events = [{
            "event_id": row["event_id"], "source": row["source"],
            "occurred_at": row["occurred_at"], "received_at": row["received_at"],
            "zone_id": row["zone_id"], "location_text": row["location_text"],
            "lat": row["lat"], "lon": row["lon"], "description": row["description"],
            "fingerprint": row["fingerprint"], "payload_hash": row["payload_hash"],
            "contact_registered": bool(row["contact_id"]),
        } for row in event_rows]
        # 关联文本不含任何联系信息；坐标取最晚一起带坐标的投诉
        combined_text = " ".join(normalize_text(row["description"]) for row in event_rows)
        complaint_lat = complaint_lon = None
        for row in reversed(event_rows):
            if row["lat"] is not None and row["lon"] is not None:
                complaint_lat, complaint_lon = row["lat"], row["lon"]
                break

        zone_records = {row["site_id"]: row for row in self._latest_records(connection, "complaint_zone")}
        operation_records = {row["site_id"]: row
                             for row in self._latest_records(connection, "operation_window")}
        site_rows = connection.execute("SELECT * FROM sites ORDER BY site_id").fetchall()
        sites_by_id = {row["site_id"]: row for row in site_rows}

        site_inputs: list[dict[str, Any]] = []
        evidences: list[SiteEvidence] = []
        for site_id, zone_row in zone_records.items():
            zone_data = json.loads(zone_row["payload_json"])
            if str(zone_data.get("zone_id", "")) != zone_id:
                continue
            site = sites_by_id.get(site_id)
            operation = operation_records.get(site_id)
            production_running = treatment_online = None
            time_overlap = False
            operation_ref: dict[str, Any] | None = None
            if operation is not None:
                op_data = json.loads(operation["payload_json"])
                production_running, treatment_online, time_overlap = self._operation_state(
                    op_data, anchor, site["timezone_name"] if site else "UTC")
                operation_ref = {"record_id": operation["record_id"],
                                 "payload_hash": operation["payload_hash"],
                                 "observed_at": op_data.get("observed_at")}
            site_inputs.append({
                "site_id": site_id,
                "zone_record": {"record_id": zone_row["record_id"],
                                "payload_hash": zone_row["payload_hash"]},
                "operation_record": operation_ref,
                "zone_data": zone_data,
            })
            evidences.append(SiteEvidence(
                site_id=site_id,
                lat=_as_float(zone_data.get("lat", zone_data.get("latitude"))),
                lon=_as_float(zone_data.get("lon", zone_data.get("longitude"))),
                production_running=production_running,
                treatment_online=treatment_online,
                odor_keywords=tuple(str(k) for k in zone_data.get("odor_keywords", []) if str(k).strip()),
                site_text=str(zone_data.get("site_text", zone_data.get("name", ""))),
                time_overlap=time_overlap,
            ))

        weather_input, weather = self._weather_snapshot(connection, anchor)
        snapshot = {
            "snapshot_type": "correlation_generation_snapshot",
            "built_at": self._now(),
            "case_id": case["case_id"],
            "zone_id": zone_id,
            "first_occurred_at": case["first_occurred_at"],
            "last_occurred_at": case["last_occurred_at"],
            "events": events,
            "site_inputs": site_inputs,
            "weather": weather_input,
            "rule": {"rule_id": rule["rule_id"], "version": rule["version"],
                     "content_hash": rule["content_hash"]},
        }
        return snapshot, evidences, weather, combined_text, complaint_lat, complaint_lon

    def _operation_state(self, data: dict[str, Any], anchor: datetime,
                         timezone_name: str) -> tuple[bool | None, bool | None, bool]:
        """从 operation_window 资料判断投诉时点的生产/治污状态与时间重叠。"""

        production = data.get("production_running")
        treatment = data.get("treatment_online")
        production_b = None if production is None else bool(production)
        treatment_b = None if treatment is None else bool(treatment)
        windows = data.get("windows")
        if isinstance(windows, list) and windows:
            hit_window = None
            minute = self._local_minute(anchor, timezone_name)
            for window in windows:
                if self._minute_in_window(minute, str(window.get("start", "")),
                                          str(window.get("end", ""))):
                    hit_window = window
                    break
            if hit_window is not None:
                production_b = bool(hit_window.get("production_running", production_b))
                treatment_b = bool(hit_window.get("treatment_online", treatment_b))
        return production_b, treatment_b, production_b is True

    @staticmethod
    def _local_minute(anchor: datetime, timezone_name: str) -> int:
        if ZoneInfo is not None:
            try:
                local = anchor.astimezone(ZoneInfo(timezone_name))
                return local.hour * 60 + local.minute
            except Exception:
                pass
        return anchor.hour * 60 + anchor.minute

    @staticmethod
    def _minute_in_window(minute: int, start: str, end: str) -> bool:
        try:
            sh, sm = (int(part) for part in start.split(":"))
            eh, em = (int(part) for part in end.split(":"))
        except (ValueError, AttributeError):
            return False
        start_m, end_m = sh * 60 + sm, eh * 60 + em
        if start_m == end_m:
            return False
        if start_m < end_m:
            return start_m <= minute < end_m
        return minute >= start_m or minute < end_m

    def _weather_snapshot(self, connection, anchor: datetime) -> tuple[dict[str, Any] | None, WeatherSnapshot | None]:
        best_row = None
        best_delta = timedelta(minutes=WEATHER_LOOKUP_MINUTES + 1)
        for row in self._latest_records(connection, "weather_source"):
            data = json.loads(row["payload_json"])
            observed = data.get("observed_at")
            if not observed:
                continue
            try:
                delta = abs(_parse_dt(observed, "weather.observed_at") - anchor)
            except ValidationError:
                continue
            if delta <= timedelta(minutes=WEATHER_LOOKUP_MINUTES) and delta < best_delta:
                best_delta, best_row = delta, row
        if best_row is None:
            return None, None
        data = json.loads(best_row["payload_json"])
        wind_dir = _as_float(data.get("wind_direction_deg"))
        wind_speed = _as_float(data.get("wind_speed_ms"))
        weather_input = {
            "record_id": best_row["record_id"], "site_id": best_row["site_id"],
            "payload_hash": best_row["payload_hash"], "observed_at": data.get("observed_at"),
            "wind_direction_deg": wind_dir, "wind_speed_ms": wind_speed,
            "stability_class": data.get("stability_class"),
            "time_delta_seconds": int(best_delta.total_seconds()),
        }
        return weather_input, WeatherSnapshot(
            wind_direction_deg=wind_dir, wind_speed_ms=wind_speed,
            stability_class=data.get("stability_class"), observed_at=data.get("observed_at"),
            source=best_row["record_id"])

    def _generate_version(self, connection, *, case_id: str, rule, created_by: str) -> str:
        case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?",
                                  (case_id,)).fetchone()
        previous = connection.execute(
            "SELECT * FROM correlation_versions WHERE case_id=? AND status='current'",
            (case_id,),
        ).fetchone()
        snapshot, evidences, weather, combined_text, lat, lon = self._build_snapshot(
            connection, case, rule)
        params = json.loads(rule["params_json"])
        result = correlate(description=combined_text, complaint_lat=lat, complaint_lon=lon,
                           evidences=evidences, weather=weather, params=params)
        result_dict = result.to_dict()
        snapshot_hash = digest(snapshot)
        engine_hash = digest({"engine_result": result_dict, "snapshot_hash": snapshot_hash})
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS next FROM correlation_versions WHERE case_id=?",
            (case_id,),
        ).fetchone()["next"]
        version_id = uuid.uuid4().hex
        # 部分唯一索引与状态迁移共同保证同时只有一个当前版本
        if previous is not None:
            connection.execute(
                "UPDATE correlation_versions SET status='superseded' WHERE version_id=?",
                (previous["version_id"],),
            )
        connection.execute(
            "INSERT INTO correlation_versions(version_id,case_id,version_no,rule_id,rule_version,"
            "rule_hash,status,revision,generation_snapshot_json,generation_snapshot_hash,"
            "engine_result_hash,requires_site_inspection,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
            (version_id, case_id, version_no, rule["rule_id"], rule["version"],
             rule["content_hash"], "current", canonical_json(snapshot), snapshot_hash,
             engine_hash, 1 if result.requires_site_inspection else 0,
             created_by, self._now()),
        )
        for candidate in result_dict["candidates"]:
            connection.execute(
                "INSERT INTO candidates(candidate_id,version_id,site_id,origin,score,"
                "confidence_low,confidence_high,status,factors_json,evidence_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, version_id, candidate["site_id"], "auto",
                 candidate["score"], candidate["confidence_low"], candidate["confidence_high"],
                 "suggested", canonical_json(candidate["factors"]),
                 canonical_json({"snapshot_hash": snapshot_hash}), self._now()),
            )

        # 把上一当前版本的人工排除/追加/确认结转应用到新版本，保留完整历史与原始依据
        carried = 0
        if previous is not None:
            carried = self._carry_decisions(connection, previous=previous, new_version_id=version_id,
                                            snapshot_hash=snapshot_hash, case=case)
        connection.execute(
            "UPDATE complaint_cases SET current_version_id=? WHERE case_id=?",
            (version_id, case_id),
        )
        append_event(connection, actor_id=created_by, action="correlation_version.generated",
                     resource_type="correlation_version", resource_id=version_id,
                     detail={"case_id": case_id, "version_no": version_no,
                             "rule_id": rule["rule_id"], "rule_version": rule["version"],
                             "rule_hash": rule["content_hash"],
                             "snapshot_hash": snapshot_hash, "engine_result_hash": engine_hash,
                             "candidate_count": len(result_dict["candidates"]),
                             "requires_site_inspection": result.requires_site_inspection,
                             "carried_decisions": carried}, occurred_at=self._now())
        return version_id

    def _carry_decisions(self, connection, *, previous, new_version_id: str,
                         snapshot_hash: str, case) -> int:
        prior_rows = connection.execute(
            "SELECT d.*, c.site_id FROM manual_decisions d JOIN candidates c "
            "ON c.candidate_id=d.candidate_id WHERE d.version_id=? "
            "AND d.action IN ('exclude_candidate','add_candidate','confirm_candidate') "
            "ORDER BY d.decided_at, d.decision_id",
            (previous["version_id"],),
        ).fetchall()
        # 同一候选只结转最后一次决定
        latest: dict[str, Any] = {}
        for row in prior_rows:
            latest[row["site_id"]] = row
        count = 0
        for site_id, row in latest.items():
            target = connection.execute(
                "SELECT * FROM candidates WHERE version_id=? AND site_id=?",
                (new_version_id, site_id),
            ).fetchone()
            if row["action"] == "add_candidate":
                if target is None:
                    connection.execute(
                        "INSERT INTO candidates(candidate_id,version_id,site_id,origin,score,"
                        "confidence_low,confidence_high,status,factors_json,evidence_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, new_version_id, site_id, "manual", None, None, None,
                         "added", canonical_json([]),
                         canonical_json({"snapshot_hash": snapshot_hash, "carried": True}),
                         self._now()),
                    )
                else:
                    connection.execute("UPDATE candidates SET status='added' WHERE candidate_id=?",
                                       (target["candidate_id"],))
                target = connection.execute(
                    "SELECT * FROM candidates WHERE version_id=? AND site_id=?",
                    (new_version_id, site_id),
                ).fetchone()
            elif target is None:
                # 引擎新版本不再输出该场所，但人工排除/确认仍然有效：
                # 以无评分占位行结转，保证人工判断不会因重算而消失
                new_status = "excluded" if row["action"] == "exclude_candidate" else "confirmed"
                connection.execute(
                    "INSERT INTO candidates(candidate_id,version_id,site_id,origin,score,"
                    "confidence_low,confidence_high,status,factors_json,evidence_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, new_version_id, site_id, "auto", None, None, None,
                     new_status, canonical_json([]),
                     canonical_json({"snapshot_hash": snapshot_hash, "carried": True,
                                     "not_in_engine_output": True}),
                     self._now()),
                )
                target = connection.execute(
                    "SELECT * FROM candidates WHERE version_id=? AND site_id=?",
                    (new_version_id, site_id),
                ).fetchone()
            elif row["action"] in ("exclude_candidate", "confirm_candidate"):
                new_status = "excluded" if row["action"] == "exclude_candidate" else "confirmed"
                connection.execute("UPDATE candidates SET status=? WHERE candidate_id=?",
                                   (new_status, target["candidate_id"],))
            decision_snapshot = self._decision_snapshot(
                connection, case=case, version_id=new_version_id, candidate_site_id=site_id,
                action=row["action"], reason=json.loads(row["detail_json"]).get("reason", ""),
                generation_snapshot_hash=snapshot_hash)
            connection.execute(
                "INSERT INTO manual_decisions(decision_id,case_id,version_id,candidate_id,action,"
                "reason,actor_id,decided_at,fact_snapshot_json,fact_snapshot_hash,detail_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, case["case_id"], new_version_id, target["candidate_id"],
                 row["action"], row["reason"], row["actor_id"], self._now(),
                 canonical_json(decision_snapshot), decision_snapshot["snapshot_hash"],
                 canonical_json({"carried_from_version_id": previous["version_id"],
                                 "original_decision_id": row["decision_id"],
                                 "reason": row["reason"]})),
            )
            connection.execute(
                "UPDATE correlation_versions SET revision=revision+1 WHERE version_id=?",
                (new_version_id,),
            )
            count += 1
        return count

    # ----- 人工决定 -----

    def decide(self, *, request_id: str, actor_id: str, case_id: str, action: str,
               reason: str, site_id: str | None = None,
               expected_revision: int | None = None) -> WriteReceipt:
        """登记一次人工决定（排除/追加/确认候选），必须给出依据并冻结事实快照。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "action": action,
                   "reason": reason, "site_id": site_id, "expected_revision": expected_revision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._require_open_case(connection, case_id)
            reason_text = self._reason(reason)
            if action not in ("exclude_candidate", "add_candidate", "confirm_candidate"):
                raise ValidationError("不支持的候选动作")
            site_id = self._identifier(site_id or "", "site_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                version = self._current_version(connection, case_id)
                self._check_revision(version, expected_revision)
                candidate = connection.execute(
                    "SELECT * FROM candidates WHERE version_id=? AND site_id=?",
                    (version["version_id"], site_id),
                ).fetchone()

                if action == "add_candidate":
                    if connection.execute("SELECT 1 FROM sites WHERE site_id=?",
                                          (site_id,)).fetchone() is None:
                        raise NotFoundError("场所不存在，不能追加为候选")
                    if candidate is not None:
                        raise ConflictError("该场所已在当前候选中，无需追加")
                    candidate_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO candidates(candidate_id,version_id,site_id,origin,score,"
                        "confidence_low,confidence_high,status,factors_json,evidence_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (candidate_id, version["version_id"], site_id, "manual", None, None, None,
                         "added", canonical_json([]),
                         canonical_json({"snapshot_hash": version["generation_snapshot_hash"]}),
                         self._now()),
                    )
                else:
                    if candidate is None:
                        raise NotFoundError("当前版本中没有该候选")
                    target_status = "excluded" if action == "exclude_candidate" else "confirmed"
                    if target_status not in CANDIDATE_TRANSITIONS[candidate["status"]]:
                        raise ConflictError(
                            f"候选状态 {candidate['status']} 不能迁移到 {target_status}")
                    candidate_id = candidate["candidate_id"]
                    connection.execute("UPDATE candidates SET status=? WHERE candidate_id=?",
                                       (target_status, candidate_id))

                decision_snapshot = self._decision_snapshot(
                    connection, case=case, version_id=version["version_id"],
                    candidate_site_id=site_id, action=action, reason=reason_text,
                    generation_snapshot_hash=version["generation_snapshot_hash"])
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO manual_decisions(decision_id,case_id,version_id,candidate_id,"
                    "action,reason,actor_id,decided_at,fact_snapshot_json,fact_snapshot_hash,"
                    "detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, case_id, version["version_id"], candidate_id, action,
                     reason_text, actor_id, self._now(),
                     canonical_json(decision_snapshot), decision_snapshot["snapshot_hash"],
                     canonical_json({"reason": reason_text})),
                )
                connection.execute(
                    "UPDATE correlation_versions SET revision=revision+1 WHERE version_id=?",
                    (version["version_id"],),
                )
                append_event(connection, actor_id=actor_id, action=f"candidate.{action}",
                             resource_type="manual_decision", resource_id=decision_id,
                             detail={"case_id": case_id, "version_id": version["version_id"],
                                     "site_id": site_id, "fact_snapshot_hash":
                                     decision_snapshot["snapshot_hash"]},
                             occurred_at=self._now())
                return "manual_decision", decision_id, {"decision_id": decision_id,
                                                         "case_id": case_id,
                                                         "new_revision": version["revision"] + 1}

            return self._idempotent(connection, request_id=request_id, action=f"candidate.{action}",
                                    payload=payload, create=create)

    def _current_version(self, connection, case_id: str):
        version = connection.execute(
            "SELECT * FROM correlation_versions WHERE case_id=? AND status='current'",
            (case_id,),
        ).fetchone()
        if version is None:
            raise ConflictError("案件尚无研判版本，请先生成关联结果")
        return version

    @staticmethod
    def _check_revision(version, expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != version["revision"]:
            raise ConflictError(
                f"案件版本已被其他操作更新（当前 revision={version['revision']}），请刷新后重试")

    def _decision_snapshot(self, connection, *, case, version_id: str, candidate_site_id: str,
                           action: str, reason: str,
                           generation_snapshot_hash: str) -> dict[str, Any]:
        """冻结人工决定当时使用的事实：事件指纹、候选状态、规则与生成快照摘要。"""

        candidate = connection.execute(
            "SELECT * FROM candidates WHERE version_id=? AND site_id=?",
            (version_id, candidate_site_id),
        ).fetchone()
        event_fingerprints = [row["fingerprint"] for row in connection.execute(
            "SELECT e.fingerprint FROM case_events ce JOIN complaint_events e "
            "ON e.event_id=ce.event_id WHERE ce.case_id=? ORDER BY e.occurred_at",
            (case["case_id"],),
        ).fetchall()]
        version = connection.execute("SELECT * FROM correlation_versions WHERE version_id=?",
                                     (version_id,)).fetchone()
        snapshot = {
            "snapshot_type": "manual_decision_snapshot",
            "frozen_at": self._now(),
            "case_id": case["case_id"],
            "case_status": case["status"],
            "version_id": version_id,
            "version_revision": version["revision"],
            "rule": {"rule_id": version["rule_id"], "rule_version": version["rule_version"],
                     "rule_hash": version["rule_hash"]},
            "generation_snapshot_hash": generation_snapshot_hash,
            "engine_result_hash": version["engine_result_hash"],
            "event_fingerprints": event_fingerprints,
            "case_occurred_window": {"first": case["first_occurred_at"],
                                     "last": case["last_occurred_at"]},
            "action": action,
            "reason": reason,
            "candidate": None if candidate is None else {
                "site_id": candidate["site_id"], "origin": candidate["origin"],
                "status_before": candidate["status"], "score": candidate["score"],
                "confidence_low": candidate["confidence_low"],
                "confidence_high": candidate["confidence_high"],
                "factors": json.loads(candidate["factors_json"]),
            },
        }
        snapshot["snapshot_hash"] = digest(snapshot)
        return snapshot

    # ----- 确认与撤销 -----

    def confirm_case(self, *, request_id: str, actor_id: str, case_id: str, reason: str,
                     candidate_ids: list[str] | None = None,
                     site_inspection_required: bool,
                     expected_revision: int | None = None) -> WriteReceipt:
        """确认案件结论：冻结结论快照，此后补录数据与规则更新都不再影响该案。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason,
                   "candidate_ids": candidate_ids,
                   "site_inspection_required": bool(site_inspection_required),
                   "expected_revision": expected_revision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._require_open_case(connection, case_id)
            reason_text = self._reason(reason)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = self._current_version(connection, case_id)
                self._check_revision(version, expected_revision)
                if candidate_ids:
                    targets = []
                    for candidate_id in candidate_ids:
                        row = connection.execute(
                            "SELECT * FROM candidates WHERE candidate_id=? AND version_id=?",
                            (candidate_id, version["version_id"]),
                        ).fetchone()
                        if row is None:
                            raise NotFoundError("候选不属于当前版本")
                        if row["status"] == "excluded":
                            raise ConflictError("不能确认已被排除的候选")
                        targets.append(row)
                else:
                    targets = connection.execute(
                        "SELECT * FROM candidates WHERE version_id=? AND status='confirmed'",
                        (version["version_id"],),
                    ).fetchall()
                if not targets:
                    raise ValidationError("确认案件前必须至少确认一个候选企业")
                for row in targets:
                    connection.execute("UPDATE candidates SET status='confirmed' WHERE candidate_id=?",
                                       (row["candidate_id"],))
                confirmed_sites = [{
                    "site_id": row["site_id"], "origin": row["origin"],
                    "score": row["score"],
                    "confidence_low": row["confidence_low"],
                    "confidence_high": row["confidence_high"],
                } for row in targets]
                conclusion = {
                    "snapshot_type": "case_conclusion",
                    "case_id": case_id, "version_id": version["version_id"],
                    "rule": {"rule_id": version["rule_id"], "rule_version": version["rule_version"],
                             "rule_hash": version["rule_hash"]},
                    "generation_snapshot_hash": version["generation_snapshot_hash"],
                    "engine_result_hash": version["engine_result_hash"],
                    "confirmed_sites": confirmed_sites,
                    "engine_requires_site_inspection": bool(version["requires_site_inspection"]),
                    "site_inspection_required": bool(site_inspection_required),
                    "reason": reason_text, "confirmed_by": actor_id,
                    "confirmed_at": self._now(),
                }
                conclusion_hash = digest(conclusion)
                connection.execute(
                    "UPDATE correlation_versions SET status='confirmed', revision=revision+1,"
                    "conclusion_json=?, conclusion_snapshot_hash=?, confirmed_by=?, confirmed_at=?",
                    (canonical_json(conclusion), conclusion_hash, actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE complaint_cases SET status='confirmed', confirmed_version_id=? "
                    "WHERE case_id=?",
                    (version["version_id"], case_id),
                )
                decision_snapshot = {
                    "snapshot_type": "confirmation_snapshot",
                    "frozen_at": self._now(), "case_id": case_id,
                    "version_id": version["version_id"],
                    "generation_snapshot_hash": version["generation_snapshot_hash"],
                    "engine_result_hash": version["engine_result_hash"],
                    "conclusion": conclusion, "conclusion_hash": conclusion_hash,
                }
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO manual_decisions(decision_id,case_id,version_id,candidate_id,"
                    "action,reason,actor_id,decided_at,fact_snapshot_json,fact_snapshot_hash,"
                    "detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, case_id, version["version_id"], None, "confirm_case",
                     reason_text, actor_id, self._now(),
                     canonical_json(decision_snapshot), conclusion_hash,
                     canonical_json({"conclusion_hash": conclusion_hash,
                                     "site_inspection_required": bool(site_inspection_required)})),
                )
                append_event(connection, actor_id=actor_id, action="case.confirmed",
                             resource_type="complaint_case", resource_id=case_id,
                             detail={"version_id": version["version_id"],
                                     "conclusion_hash": conclusion_hash,
                                     "site_inspection_required": bool(site_inspection_required)},
                             occurred_at=self._now())
                return "case_conclusion", case_id, {"case_id": case_id,
                                                     "conclusion_hash": conclusion_hash,
                                                     "site_inspection_required":
                                                     bool(site_inspection_required)}

            return self._idempotent(connection, request_id=request_id, action="confirm_case",
                                    payload=payload, create=create)

    def revoke_confirmation(self, *, request_id: str, actor_id: str, case_id: str,
                            reason: str) -> WriteReceipt:
        """撤销确认（仅 admin）。原结论与快照完整保留，案件进入 revoked 终态。"""

        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")  # 高于值班员的权限
            reason_text = self._reason(reason)

            def create() -> tuple[str, str, dict[str, Any]]:
                case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?",
                                          (case_id,)).fetchone()
                if case is None:
                    raise NotFoundError("案件不存在")
                if case["status"] != "confirmed":
                    raise ConflictError("只有已确认案件可以撤销确认")
                version = connection.execute(
                    "SELECT * FROM correlation_versions WHERE version_id=?",
                    (case["confirmed_version_id"],),
                ).fetchone()
                original_conclusion = json.loads(version["conclusion_json"])
                original_hash = version["conclusion_snapshot_hash"]
                connection.execute(
                    "UPDATE correlation_versions SET status='revoked' WHERE version_id=?",
                    (version["version_id"],),
                )
                connection.execute(
                    "UPDATE complaint_cases SET status='revoked' WHERE case_id=?", (case_id,))
                snapshot = {
                    "snapshot_type": "revocation_snapshot",
                    "frozen_at": self._now(), "case_id": case_id,
                    "retained_conclusion_hash": original_hash,
                    "retained_conclusion": original_conclusion,
                    "reason": reason_text, "revoked_by": actor_id,
                }
                snapshot_hash = digest(snapshot)
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO manual_decisions(decision_id,case_id,version_id,candidate_id,"
                    "action,reason,actor_id,decided_at,fact_snapshot_json,fact_snapshot_hash,"
                    "detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, case_id, version["version_id"], None, "revoke_confirmation",
                     reason_text, actor_id, self._now(), canonical_json(snapshot), snapshot_hash,
                     canonical_json({"retained_conclusion_hash": original_hash})),
                )
                append_event(connection, actor_id=actor_id, action="case.confirmation_revoked",
                             resource_type="complaint_case", resource_id=case_id,
                             detail={"version_id": version["version_id"],
                                     "retained_conclusion_hash": original_hash},
                             occurred_at=self._now())
                return "case_revocation", case_id, {"case_id": case_id,
                                                     "retained_conclusion_hash": original_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_confirmation", payload=payload, create=create)

    # ----- 查询与追溯 -----

    def list_cases(self, zone_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if status and status not in ("open", "confirmed", "revoked"):
            raise ValidationError("status 不合法")
        query = ("SELECT c.case_id,c.case_code,c.zone_id,c.status,c.first_occurred_at,"
                 "c.last_occurred_at,c.current_version_id,c.confirmed_version_id,c.created_at,"
                 "(SELECT COUNT(*) FROM case_events ce WHERE ce.case_id=c.case_id) AS event_count "
                 "FROM complaint_cases c")
        clauses: list[str] = []
        parameters: list[Any] = []
        if zone_id:
            clauses.append("c.zone_id=?")
            parameters.append(zone_id)
        if status:
            clauses.append("c.status=?")
            parameters.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY c.first_occurred_at DESC, c.case_id"
        with self.database.read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_case(self, case_id: str) -> dict[str, Any]:
        """返回匿名化案件视图（绝不含联系信息原文）。"""

        trace = self.get_case_trace(case_id)
        return {key: trace[key] for key in
                ("case_id", "case_code", "zone_id", "status", "first_occurred_at",
                 "last_occurred_at", "created_at", "event_count", "current_version",
                 "confirmation", "events")}

    def get_case_trace(self, case_id: str) -> dict[str, Any]:
        """从匿名化案件追到输入事件、规则版本、候选/因素、人工决定与现场核查结论。"""

        with self.database.read() as connection:
            return self._trace(connection, case_id)

    def _trace(self, connection, case_id: str) -> dict[str, Any]:
        case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?",
                                  (case_id,)).fetchone()
        if case is None:
            raise NotFoundError("案件不存在")
        events = []
        for row in connection.execute(
            "SELECT e.event_id,e.source,e.zone_id,e.occurred_at,e.received_at,e.location_text,"
            "e.lat,e.lon,e.description,e.fingerprint,e.payload_hash,"
            "ce.merge_basis_json,(e.contact_id IS NOT NULL) AS has_contact "
            "FROM case_events ce JOIN complaint_events e ON e.event_id=ce.event_id "
            "WHERE ce.case_id=? ORDER BY e.occurred_at,e.event_id",
            (case_id,),
        ).fetchall():
            events.append({
                "event_id": row["event_id"], "source": row["source"], "zone_id": row["zone_id"],
                "occurred_at": row["occurred_at"], "received_at": row["received_at"],
                "location_text": row["location_text"], "lat": row["lat"], "lon": row["lon"],
                "description": row["description"], "fingerprint": row["fingerprint"],
                "payload_hash": row["payload_hash"], "contact_registered": bool(row["has_contact"]),
                "merge_basis": json.loads(row["merge_basis_json"]),
            })
        versions = []
        for version in connection.execute(
            "SELECT * FROM correlation_versions WHERE case_id=? ORDER BY version_no", (case_id,)
        ).fetchall():
            candidates = []
            for candidate in connection.execute(
                "SELECT * FROM candidates WHERE version_id=? ORDER BY rowid",
                (version["version_id"],),
            ).fetchall():
                candidates.append({
                    "candidate_id": candidate["candidate_id"], "site_id": candidate["site_id"],
                    "origin": candidate["origin"], "status": candidate["status"],
                    "score": candidate["score"],
                    "confidence_low": candidate["confidence_low"],
                    "confidence_high": candidate["confidence_high"],
                    "factors": json.loads(candidate["factors_json"]),
                })
            decisions = []
            for decision in connection.execute(
                "SELECT * FROM manual_decisions WHERE version_id=? ORDER BY decided_at,decision_id",
                (version["version_id"],),
            ).fetchall():
                decisions.append({
                    "decision_id": decision["decision_id"], "action": decision["action"],
                    "reason": decision["reason"], "actor_id": decision["actor_id"],
                    "decided_at": decision["decided_at"],
                    "fact_snapshot_hash": decision["fact_snapshot_hash"],
                    "fact_snapshot": json.loads(decision["fact_snapshot_json"]),
                    "detail": json.loads(decision["detail_json"]),
                })
            versions.append({
                "version_id": version["version_id"], "version_no": version["version_no"],
                "status": version["status"], "revision": version["revision"],
                "rule_id": version["rule_id"], "rule_version": version["rule_version"],
                "rule_hash": version["rule_hash"],
                "generation_snapshot_hash": version["generation_snapshot_hash"],
                "generation_snapshot": json.loads(version["generation_snapshot_json"]),
                "engine_result_hash": version["engine_result_hash"],
                "engine_requires_site_inspection": bool(version["requires_site_inspection"]),
                "candidates": candidates, "manual_decisions": decisions,
                "conclusion": json.loads(version["conclusion_json"])
                if version["conclusion_json"] else None,
                "conclusion_snapshot_hash": version["conclusion_snapshot_hash"],
                "confirmed_by": version["confirmed_by"], "confirmed_at": version["confirmed_at"],
            })
        current = next((v for v in versions if v["status"] == "current"), None)
        confirmed_version = next((v for v in versions if v["version_id"] == case["confirmed_version_id"]),
                                 None)
        confirmation = None
        if confirmed_version is not None and confirmed_version["conclusion"]:
            conclusion = confirmed_version["conclusion"]
            revocation = next((d for d in confirmed_version["manual_decisions"]
                               if d["action"] == "revoke_confirmation"), None)
            confirmation = {
                "conclusion_snapshot_hash": confirmed_version["conclusion_snapshot_hash"],
                "rule": conclusion["rule"],
                "generation_snapshot_hash": conclusion["generation_snapshot_hash"],
                "engine_result_hash": conclusion["engine_result_hash"],
                "confirmed_sites": conclusion["confirmed_sites"],
                "engine_requires_site_inspection": conclusion["engine_requires_site_inspection"],
                "site_inspection_required": conclusion["site_inspection_required"],
                "reason": conclusion["reason"], "confirmed_by": conclusion["confirmed_by"],
                "confirmed_at": conclusion["confirmed_at"],
                "retained_after_revoke": case["status"] == "revoked",
                "revocation": None if revocation is None else
                {"reason": revocation["reason"], "actor_id": revocation["actor_id"],
                 "decided_at": revocation["decided_at"],
                 "fact_snapshot_hash": revocation["fact_snapshot_hash"]},
            }
        return {
            "case_id": case["case_id"], "case_code": case["case_code"],
            "zone_id": case["zone_id"], "status": case["status"],
            "first_occurred_at": case["first_occurred_at"],
            "last_occurred_at": case["last_occurred_at"],
            "created_at": case["created_at"], "event_count": len(events),
            "events": events, "versions": versions,
            "current_version": None if current is None
            else {"version_id": current["version_id"], "version_no": current["version_no"],
                  "revision": current["revision"], "rule_id": current["rule_id"],
                  "rule_version": current["rule_version"], "rule_hash": current["rule_hash"],
                  "generation_snapshot_hash": current["generation_snapshot_hash"],
                  "engine_result_hash": current["engine_result_hash"],
                  "engine_requires_site_inspection": current["engine_requires_site_inspection"],
                  "candidates": current["candidates"],
                  "manual_decisions": current["manual_decisions"]},
            "confirmation": confirmation,
        }

    def get_contacts(self, *, actor_id: str, case_id: str) -> list[dict[str, Any]]:
        """受限查看脱敏联系信息；仅 admin 可见原文，每次查看写入审计。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            # 值班员/复核员/审计员只能看脱敏结果，联系信息原文仅 admin 可取
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            case = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?",
                                      (case_id,)).fetchone()
            if case is None:
                raise NotFoundError("案件不存在")
            rows = connection.execute(
                "SELECT e.event_id,c.contact_name,c.contact_phone,c.masked_name,c.masked_phone "
                "FROM case_events ce JOIN complaint_events e ON e.event_id=ce.event_id "
                "JOIN event_contacts c ON c.event_id=e.event_id WHERE ce.case_id=? "
                "ORDER BY e.occurred_at",
                (case_id,),
            ).fetchall()
            raw_visible = actor["role"] == "admin"
            items = [{
                "event_id": row["event_id"],
                "contact_name": row["contact_name"] if raw_visible else row["masked_name"],
                "contact_phone": row["contact_phone"] if raw_visible else row["masked_phone"],
                "masked": not raw_visible,
            } for row in rows]
            append_event(connection, actor_id=actor_id, action="contact.revealed",
                         resource_type="complaint_case", resource_id=case_id,
                         detail={"raw_visible": raw_visible, "count": len(items)},
                         occurred_at=self._now())
            return items


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
