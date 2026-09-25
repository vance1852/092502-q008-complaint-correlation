"""投诉关联案件服务：合并去重、版本化研判、人工决定与确认冻结。

关键不变量：

* 每个案件（case）的候选研判以 case_versions 逐版保存，并通过部分唯一索引
  保证同一投诉案件在任意时刻只有一个 is_current=1 的当前版本；
* 每次人工决定（排除/追加/确认/撤销）都必须给出依据（reason），并冻结当时
  使用的事实快照（fact_snapshots）与规则版本（rule_versions）；
* 案件确认后结论进入 case_conclusions 并冻结，后续补录资料或发布新规则只
  能生成新案件版本，不能改写已确认结论；撤销确认需要更高权限，原结论仍以
  status=revoked 保留；
* 所有写操作在 IMMEDIATE 事务中完成并写入哈希审计链，并发处理同一投诉由
  SQLite 写锁与当前版本唯一索引串行化。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from . import correlation
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .correlation import (
    RULE_FAMILY,
    contact_token,
    mask_contact,
    should_merge,
    text_fingerprint,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .storage import Database

# 可对候选做人工研判（排除/追加/确认）的角色；撤销确认要求高于原确认者的权限。
DECISION_ROLES = frozenset({"admin", "operator", "reviewer"})
CONFIRM_ROLES = DECISION_ROLES
# 撤销者的角色等级必须高于当初做出确认的操作者
ROLE_RANK = {"operator": 1, "reviewer": 2, "admin": 3}
REVOKE_ROLES = frozenset({"admin", "reviewer"})

CHANNELS = frozenset({"phone", "hotline", "app", "walk_in", "other"})


class CaseService:
    """协调投诉事件、规则版本、案件版本、人工决定和审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

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

    @staticmethod
    def _text(value: Any, field: str, limit: int = 500, allow_empty: bool = False) -> str:
        value = str(value if value is not None else "").strip()
        if not value and not allow_empty:
            raise ValidationError(f"{field} 不能为空")
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ------------------------------------------------------------------
    # 规则版本
    # ------------------------------------------------------------------

    def publish_rule_version(self, *, request_id: str, actor_id: str,
                             spec: dict[str, Any] | None = None, note: str = "") -> WriteReceipt:
        """发布一版关联规则。spec 为 None 时冻结当前代码内置规格。"""

        spec = spec if spec is not None else correlation.rule_spec()
        if not isinstance(spec, dict) or not spec:
            raise ValidationError("spec 必须是非空对象")
        payload = {"actor_id": actor_id, "spec": spec, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                spec_hash = digest(spec)
                duplicate = connection.execute(
                    "SELECT rule_version FROM rule_versions WHERE family=? AND spec_hash=?",
                    (RULE_FAMILY, spec_hash),
                ).fetchone()
                if duplicate:
                    raise ConflictError("该规则内容已经发布")
                connection.execute("UPDATE rule_versions SET status='superseded' WHERE family=?", (RULE_FAMILY,))
                cursor = connection.execute(
                    "INSERT INTO rule_versions(family,spec_json,spec_hash,status,published_by,published_at) "
                    "VALUES(?,?,?,'active',?,?)",
                    (RULE_FAMILY, canonical_json(spec), spec_hash, actor_id, self._now()),
                )
                version_number = cursor.lastrowid
                append_event(connection, actor_id=actor_id, action="rule.published",
                             resource_type="rule_version", resource_id=str(version_number),
                             detail={"family": RULE_FAMILY, "spec_hash": spec_hash, "note": note},
                             occurred_at=self._now())
                return "rule_version", str(version_number), {"rule_version": version_number}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_rule_version", payload=payload, create=create)

    def _active_rule(self, connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM rule_versions WHERE family=? AND status='active'", (RULE_FAMILY,)
        ).fetchone()
        if row is None:
            raise ConflictError("尚未发布关联规则版本，无法生成研判")
        return {"rule_version": row["rule_version"], "spec": json.loads(row["spec_json"]),
                "spec_hash": row["spec_hash"]}

    def list_rule_versions(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT rule_version,family,spec_hash,status,published_by,published_at "
            "FROM rule_versions ORDER BY rule_version"
        ).fetchall()
        return [dict(row) for row in rows]

    def ensure_seed_rule(self, *, actor_id: str = "bootstrap") -> int:
        """在全新库中冻结内置规则规格，返回当前生效版本号。"""

        row = self.database.connection.execute(
            "SELECT rule_version FROM rule_versions WHERE family=? AND status='active'", (RULE_FAMILY,)
        ).fetchone()
        if row:
            return row["rule_version"]
        with self.database.transaction(immediate=True) as connection:
            spec = correlation.rule_spec()
            spec_hash = digest(spec)
            cursor = connection.execute(
                "INSERT INTO rule_versions(family,spec_json,spec_hash,status,published_by,published_at) "
                "VALUES(?,?,?,'active',?,?)",
                (RULE_FAMILY, canonical_json(spec), spec_hash, actor_id, self._now()),
            )
            append_event(connection, actor_id=actor_id, action="rule.published",
                         resource_type="rule_version", resource_id=str(cursor.lastrowid),
                         detail={"family": RULE_FAMILY, "spec_hash": spec_hash, "note": "seed"},
                         occurred_at=self._now())
            return cursor.lastrowid

    # ------------------------------------------------------------------
    # 投诉录入与合并
    # ------------------------------------------------------------------

    def ingest_complaint(self, *, request_id: str, actor_id: str, channel: str,
                         occurred_at: str, region_code: str, description: str,
                         contact: str | None = None, location: list[float] | None = None,
                         odor_terms: list[str] | None = None,
                         event_id: str | None = None) -> WriteReceipt:
        """登记一通投诉来电；若与既有案件重复则合并，否则建新案件。

        联系方式不参与明文存储：仅保存不可逆令牌（用于同人识别）和掩码
        （用于值班展示）。
        """

        channel = self._text(channel, "channel", 32)
        if channel not in CHANNELS:
            raise ValidationError("channel 不在允许范围内")
        occurred_at = self._text(occurred_at, "occurred_at", 64)
        region_code = self._text(region_code, "region_code", 64)
        description = self._text(description, "description", 2000)
        if location is not None:
            if (not isinstance(location, (list, tuple)) or len(location) != 2
                    or not all(isinstance(v, (int, float)) for v in location)):
                raise ValidationError("location 必须是 [纬度, 经度]")
        odor_terms = odor_terms or []
        if not isinstance(odor_terms, list) or any(not str(v) for v in odor_terms):
            raise ValidationError("odor_terms 必须是字符串数组")
        payload = {"actor_id": actor_id, "channel": channel, "occurred_at": occurred_at,
                   "region_code": region_code, "description": description, "contact": contact,
                   "location": location, "odor_terms": odor_terms, "event_id": event_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            fingerprint = text_fingerprint(description)
            token = contact_token(contact)
            masked = mask_contact(contact)
            location_value = list(location) if location else None

            def create() -> tuple[str, str, dict[str, Any]]:
                # 在同区域、仍处于 open 的既有事件中按时间窗与文本指纹寻找重复来电
                candidate_rows = connection.execute(
                    "SELECT ce.*, cc.status AS case_status FROM complaint_events ce "
                    "JOIN complaint_cases cc ON cc.case_id = ce.case_id "
                    "WHERE ce.region_code=? AND cc.status='open'",
                    (region_code,),
                ).fetchall()
                merged_into: str | None = None
                merge_reasons: list[str] = []
                new_probe = {"region_code": region_code, "occurred_at": occurred_at,
                             "fingerprint": fingerprint, "contact_token": token,
                             "location": location_value}
                for row in candidate_rows:
                    existing = {
                        "region_code": row["region_code"],
                        "occurred_at": row["occurred_at"],
                        "fingerprint": json.loads(row["description_norm"]),
                        "contact_token": row["contact_token"],
                        "location": json.loads(row["location_json"]) if row["location_json"] else None,
                    }
                    hit, reasons = should_merge(new_probe, existing)
                    if hit:
                        merged_into = row["case_id"]
                        merge_reasons = reasons
                        break
                if merged_into is None:
                    case_id = uuid.uuid4().hex
                    now = self._now()
                    connection.execute(
                        "INSERT INTO complaint_cases(case_id,region_code,status,first_event_at,last_event_at,created_at) "
                        "VALUES(?,?, 'open', ?, ?, ?)",
                        (case_id, region_code, occurred_at, occurred_at, now),
                    )
                    target_case = case_id
                else:
                    target_case = merged_into
                    connection.execute(
                        "UPDATE complaint_cases SET last_event_at=? WHERE case_id=? AND last_event_at<?",
                        (occurred_at, target_case, occurred_at),
                    )
                    if occurred_at < connection.execute(
                            "SELECT first_event_at FROM complaint_cases WHERE case_id=?", (target_case,)
                    ).fetchone()["first_event_at"]:
                        connection.execute(
                            "UPDATE complaint_cases SET first_event_at=? WHERE case_id=?",
                            (occurred_at, target_case),
                        )
                eid = event_id or uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO complaint_events(event_id,case_id,channel,occurred_at,received_at,"
                        "region_code,description_norm,text_fingerprint,contact_token,contact_mask,"
                        "location_json,terms_json,merge_reasons,payload_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, target_case, channel, occurred_at, self._now(), region_code,
                         canonical_json(fingerprint), digest(fingerprint["grams"]), token, masked or None,
                         canonical_json(location_value), canonical_json(odor_terms),
                         ",".join(merge_reasons), digest(payload), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("事件编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action="complaint.merged" if merged_into else "complaint.opened",
                             resource_type="complaint_case", resource_id=target_case,
                             detail={"event_id": eid, "region_code": region_code,
                                     "merge_reasons": merge_reasons},
                             occurred_at=self._now())
                return "complaint_case", target_case, {
                    "case_id": target_case, "event_id": eid,
                    "merged": bool(merged_into), "merge_reasons": merge_reasons,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="ingest_complaint", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 事实收集
    # ------------------------------------------------------------------

    def _load_case(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("案件不存在")
        return row

    def _load_events(self, connection, case_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM complaint_events WHERE case_id=? ORDER BY occurred_at, event_id", (case_id,)
        ).fetchall()
        events = []
        for row in rows:
            events.append({
                "event_id": row["event_id"],
                "channel": row["channel"],
                "occurred_at": row["occurred_at"],
                "received_at": row["received_at"],
                "region_code": row["region_code"],
                "fingerprint": json.loads(row["description_norm"]),
                "terms": json.loads(row["terms_json"]),
                "contact_mask": row["contact_mask"],
                "location": json.loads(row["location_json"]) if row["location_json"] else None,
                "merge_reasons": row["merge_reasons"],
            })
        return events

    def _load_sites(self, connection) -> list[dict[str, Any]]:
        result = []
        for row in connection.execute("SELECT * FROM sites"):
            result.append({"site_id": row["site_id"], "organization_id": row["organization_id"],
                           "name": row["name"], "region_code": "", "location": None})
        # 场所区域与坐标来自 complaint_zone 资料
        zones = connection.execute(
            "SELECT site_id, payload_json FROM domain_records WHERE category='complaint_zone'"
        ).fetchall()
        zone_by_site = {row["site_id"]: json.loads(row["payload_json"]) for row in zones}
        for site in result:
            zone = zone_by_site.get(site["site_id"], {})
            site["region_code"] = zone.get("region_code", "")
            site["location"] = zone.get("location")
            if zone.get("name"):
                site["name"] = zone["name"]
        return result

    def _load_domain_records(self, connection) -> list[dict[str, Any]]:
        result = []
        rows = connection.execute(
            "SELECT site_id,category,external_key,payload_json FROM domain_records "
            "WHERE category IN ('operation_window','treatment_status')"
        ).fetchall()
        for row in rows:
            result.append({"site_id": row["site_id"], "category": row["category"],
                           "external_key": row["external_key"], "payload": json.loads(row["payload_json"])})
        return result

    def _load_weather(self, connection, region_code: str, reference_at: str) -> dict[str, Any] | None:
        rows = connection.execute(
            "SELECT external_key,payload_json FROM domain_records WHERE category='weather_snapshot'"
        ).fetchall()
        reference = correlation.parse_instant(reference_at)
        window_seconds = correlation.RULE_SPEC["time_window_minutes"] * 60
        candidates = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("region_code") and payload["region_code"] != region_code:
                continue
            if not payload.get("as_of"):
                continue
            try:
                distance = abs(correlation.parse_instant(payload["as_of"]) - reference)
            except (TypeError, ValueError):
                continue
            # 只接受投诉时间窗内的快照，避免用窗口外（含未来）的风向参与研判
            if distance > window_seconds:
                continue
            candidates.append((distance, row["external_key"], payload))
        if not candidates:
            return None
        # 选取时间最接近的区域气象快照（时间差相同时按业务键定序），保证确定性
        _, external_key, payload = min(candidates, key=lambda item: (item[0], item[1]))
        return {"source": external_key, **payload}

    def _collect_facts(self, connection, case_row) -> dict[str, Any]:
        events = self._load_events(connection, case_row["case_id"])
        facts = correlation.gather_facts(
            case={"case_id": case_row["case_id"], "region_code": case_row["region_code"],
                  "first_event_at": case_row["first_event_at"], "last_event_at": case_row["last_event_at"]},
            events=[{"event_id": e["event_id"], "channel": e["channel"], "occurred_at": e["occurred_at"],
                     "received_at": e["received_at"], "region_code": e["region_code"],
                     "fingerprint": e["fingerprint"], "odor_terms": e["terms"],
                     "location": e["location"]} for e in events],
            sites=self._load_sites(connection),
            domain_records=self._load_domain_records(connection),
            weather=self._load_weather(connection, case_row["region_code"],
                                       case_row["first_event_at"]),
        )
        facts["facts_hash"] = digest({k: v for k, v in facts.items()})
        return facts

    def _freeze_snapshot(self, connection, *, case_id: str, kind: str, facts: dict[str, Any],
                         rule_version: int | None, actor_id: str) -> str:
        snapshot_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO fact_snapshots(snapshot_id,case_id,kind,facts_json,facts_hash,rule_version,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (snapshot_id, case_id, kind, canonical_json(facts), facts["facts_hash"], rule_version,
             actor_id, self._now()),
        )
        return snapshot_id

    # ------------------------------------------------------------------
    # 版本化研判
    # ------------------------------------------------------------------

    def _current_version(self, connection, case_id: str):
        return connection.execute(
            "SELECT * FROM case_versions WHERE case_id=? AND is_current=1", (case_id,)
        ).fetchone()

    def _rule_row(self, connection, rule_version: int):
        row = connection.execute(
            "SELECT * FROM rule_versions WHERE rule_version=?", (rule_version,)
        ).fetchone()
        if row is None:
            raise ConflictError(f"规则版本 {rule_version} 不存在")
        return row

    def _evaluate(self, facts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        """用指定版本的冻结规则规格评估事实，保证历史版本可按旧规则复现。"""

        return correlation.generate_candidates(facts, spec)

    def generate_version(self, *, request_id: str, actor_id: str, case_id: str,
                         reason: str) -> WriteReceipt:
        """基于当前事实与生效规则生成案件的下一版候选研判。

        已确认案件不允许再生成版本（结论已冻结；如需继续工作应先按权限撤销）。
        """

        reason = self._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DECISION_ROLES)
            rule = self._active_rule(connection)
            case_row = self._load_case(connection, case_id)
            if case_row["status"] == "confirmed":
                raise ConflictError("案件已确认，结论已冻结，不能再生成新版本")
            facts = self._collect_facts(connection, case_row)
            result = self._evaluate(facts, rule["spec"])

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot_id = self._freeze_snapshot(
                    connection, case_id=case_id, kind="analysis", facts=facts,
                    rule_version=rule["rule_version"], actor_id=actor_id)
                return self._write_version(connection, case_id=case_id, actor_id=actor_id,
                                           rule_version=rule["rule_version"], snapshot_id=snapshot_id,
                                           origin="generated", candidates=result["candidates"],
                                           needs_field=result["needs_field_inspection"],
                                           action="case.version_generated", reason=reason,
                                           detail_extra={"rule_version": rule["rule_version"],
                                                         "facts_hash": facts["facts_hash"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_version", payload=payload, create=create)

    def _write_version(self, connection, *, case_id: str, actor_id: str, rule_version: int,
                       snapshot_id: str, origin: str, candidates: list[dict[str, Any]],
                       needs_field: bool, action: str, reason: str,
                       detail_extra: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        current = self._current_version(connection, case_id)
        next_number = (current["version"] + 1) if current else 1
        previous_state_hash = current["state_hash"] if current else None
        version_id = uuid.uuid4().hex
        state_material = {"case_id": case_id, "version": next_number, "rule_version": rule_version,
                          "snapshot_id": snapshot_id, "origin": origin, "candidates": candidates,
                          "needs_field_inspection": needs_field, "previous_state_hash": previous_state_hash}
        state_hash = digest(state_material)
        if current:
            connection.execute("UPDATE case_versions SET is_current=0 WHERE case_version_id=?",
                               (current["case_version_id"],))
        connection.execute(
            "INSERT INTO case_versions(case_version_id,case_id,version,rule_version,fact_snapshot_id,"
            "origin,candidates_json,state_hash,needs_field_inspection,is_current,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (version_id, case_id, next_number, rule_version, snapshot_id, origin,
             canonical_json(candidates), state_hash, 1 if needs_field else 0, actor_id, self._now()),
        )
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="case_version", resource_id=version_id,
                     detail={"case_id": case_id, "version": next_number, "rule_version": rule_version,
                             "origin": origin, "reason": reason, "state_hash": state_hash, **detail_extra},
                     occurred_at=self._now())
        return "case_version", version_id, {"case_id": case_id, "case_version_id": version_id,
                                            "version": next_number, "state_hash": state_hash}

    # ------------------------------------------------------------------
    # 人工决定：排除 / 追加
    # ------------------------------------------------------------------

    def _candidate_index(self, candidates: list[dict[str, Any]], site_id: str) -> int:
        for index, candidate in enumerate(candidates):
            if candidate["site_id"] == site_id:
                return index
        return -1

    def decide_candidate(self, *, request_id: str, actor_id: str, case_id: str,
                         decision: str, site_id: str, reason: str) -> WriteReceipt:
        """值班员排除或追加候选企业；每次决定必须说明依据并冻结事实快照。"""

        decision = self._text(decision, "decision", 32)
        site_id = self._text(site_id, "site_id", 64)
        reason = self._text(reason, "reason", 500)
        if decision not in {"exclude", "add"}:
            raise ValidationError("decision 只能是 exclude 或 add")
        payload = {"actor_id": actor_id, "case_id": case_id, "decision": decision,
                   "site_id": site_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DECISION_ROLES)
            case_row = self._load_case(connection, case_id)
            if case_row["status"] == "confirmed":
                raise ConflictError("案件已确认，候选名单已冻结")
            current = self._current_version(connection, case_id)
            if current is None:
                raise ConflictError("案件还没有研判版本，无法做人工决定")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("企业场所不存在")
            candidates = json.loads(current["candidates_json"])
            index = self._candidate_index(candidates, site_id)
            present = index >= 0
            current_status = candidates[index].get("status") if present else None
            if decision == "exclude" and not present:
                raise ConflictError("该企业不在当前候选名单中")
            if decision == "exclude" and current_status == "excluded":
                raise ConflictError("该企业已经被排除")
            if decision == "add" and present and current_status != "excluded":
                raise ConflictError("该企业已经在候选名单中")

            # 人工决定沿用当前版本冻结的规则规格，保证同一版本内评分口径一致
            rule_row = self._rule_row(connection, current["rule_version"])
            rule_spec = json.loads(rule_row["spec_json"])
            facts = self._collect_facts(connection, case_row)

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot_id = self._freeze_snapshot(
                    connection, case_id=case_id, kind="decision", facts=facts,
                    rule_version=current["rule_version"], actor_id=actor_id)
                if decision == "exclude":
                    candidates[index]["status"] = "excluded"
                    candidates[index]["excluded_by"] = actor_id
                    candidates[index]["exclude_reason"] = reason
                elif current_status == "excluded":
                    # 恢复此前被排除的候选，而不是重复追加一条
                    candidates[index].pop("excluded_by", None)
                    candidates[index].pop("exclude_reason", None)
                    candidates[index]["status"] = "added"
                    candidates[index]["added_by"] = actor_id
                    candidates[index]["add_reason"] = reason
                else:
                    # 人工追加的候选仍带机器评估分（按该版本冻结规则补算），但标记人工来源
                    result = self._evaluate(facts, rule_spec)
                    machine = next((c for c in result["candidates"] if c["site_id"] == site_id), None)
                    entry = machine or {"site_id": site_id, "site_name": site_id, "score": 0.0,
                                        "contributions": {}, "factors": [],
                                        "confidence_interval": {"level": 0.90, "lower": 0.0, "upper": 0.0},
                                        "distance_meters": None}
                    entry["status"] = "added"
                    entry["added_by"] = actor_id
                    entry["add_reason"] = reason
                    candidates.append(entry)
                candidates.sort(key=lambda item: (-float(item.get("score", 0.0)), item["site_id"]))
                for position, candidate in enumerate(candidates, start=1):
                    candidate["rank"] = position
                needs_field = bool(current["needs_field_inspection"]) or decision == "add"
                resource_type, version_id, response = self._write_version(
                    connection, case_id=case_id, actor_id=actor_id,
                    rule_version=current["rule_version"], snapshot_id=snapshot_id,
                    origin="manual_exclude" if decision == "exclude" else "manual_add",
                    candidates=candidates, needs_field=needs_field,
                    action="candidate.excluded" if decision == "exclude" else "candidate.added",
                    reason=reason, detail_extra={"target_site_id": site_id})
                connection.execute(
                    "INSERT INTO case_decisions(decision_id,case_id,case_version_id,fact_snapshot_id,"
                    "action,target_site_id,reason,from_state_hash,to_state_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, case_id, version_id, snapshot_id,
                     "excluded_candidate" if decision == "exclude" else "added_candidate",
                     site_id, reason, current["state_hash"], response["state_hash"], actor_id, self._now()),
                )
                return resource_type, version_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action=f"decide_{decision}", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 确认与撤销
    # ------------------------------------------------------------------

    def confirm_case(self, *, request_id: str, actor_id: str, case_id: str,
                     reason: str, confirmed_sites: list[str] | None = None) -> WriteReceipt:
        """确认案件：冻结结论。补录数据或规则更新都不再改变它。"""

        reason = self._text(reason, "reason", 500)
        confirmed_sites = confirmed_sites or []
        if not isinstance(confirmed_sites, list) or any(not str(v) for v in confirmed_sites):
            raise ValidationError("confirmed_sites 必须是字符串数组")
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason,
                   "confirmed_sites": confirmed_sites}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            case_row = self._load_case(connection, case_id)
            if case_row["status"] == "confirmed":
                raise ConflictError("案件已经确认")
            current = self._current_version(connection, case_id)
            if current is None:
                raise ConflictError("案件还没有研判版本，不能确认")
            candidates = json.loads(current["candidates_json"])
            by_site = {c["site_id"]: c for c in candidates}
            unknown = [site for site in confirmed_sites if site not in by_site]
            if unknown:
                raise ValidationError(f"确认对象不在候选名单中: {','.join(unknown)}")
            excluded = [site for site in confirmed_sites
                        if by_site[site].get("status") == "excluded"]
            if excluded:
                raise ValidationError(f"不能确认已排除的候选: {','.join(excluded)}")
            facts = self._collect_facts(connection, case_row)

            def create() -> tuple[str, str, dict[str, Any]]:
                # 确认前复核事实：当前事实必须与当前版本冻结的事实一致，否则说明
                # 存在尚未进入任何版本的补录数据，要求先生成新版本再确认，避免
                # 结论与事实脱节。
                snapshot_row = connection.execute(
                    "SELECT facts_hash FROM fact_snapshots WHERE snapshot_id=?",
                    (current["fact_snapshot_id"],),
                ).fetchone()
                if snapshot_row["facts_hash"] != facts["facts_hash"]:
                    raise ConflictError("事实已发生变化（存在补录数据），请先生成新版本再确认")
                decision_snapshot_id = self._freeze_snapshot(
                    connection, case_id=case_id, kind="decision", facts=facts,
                    rule_version=current["rule_version"], actor_id=actor_id)
                conclusion_id = uuid.uuid4().hex
                conclusion_payload = {
                    "case_version_id": current["case_version_id"],
                    "version": current["version"],
                    "rule_version": current["rule_version"],
                    "candidates": candidates,
                    "confirmed_sites": confirmed_sites,
                    "needs_field_inspection": bool(current["needs_field_inspection"]),
                    "reason": reason,
                }
                connection.execute(
                    "INSERT INTO case_conclusions(conclusion_id,case_id,case_version_id,rule_version,"
                    "fact_snapshot_id,payload_json,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,'active',?,?)",
                    (conclusion_id, case_id, current["case_version_id"], current["rule_version"],
                     current["fact_snapshot_id"], canonical_json(conclusion_payload), actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE complaint_cases SET status='confirmed', confirmed_at=? WHERE case_id=?",
                    (self._now(), case_id),
                )
                connection.execute(
                    "INSERT INTO case_decisions(decision_id,case_id,case_version_id,fact_snapshot_id,"
                    "action,target_site_id,reason,from_state_hash,to_state_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, case_id, current["case_version_id"], decision_snapshot_id,
                     "confirmed_case", None, reason, current["state_hash"], current["state_hash"],
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="case.confirmed",
                             resource_type="case_conclusion", resource_id=conclusion_id,
                             detail={"case_id": case_id, "case_version_id": current["case_version_id"],
                                     "rule_version": current["rule_version"],
                                     "facts_hash": snapshot_row["facts_hash"],
                                     "confirmed_sites": confirmed_sites, "reason": reason},
                             occurred_at=self._now())
                return "case_conclusion", conclusion_id, {"case_id": case_id,
                                                          "conclusion_id": conclusion_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_case", payload=payload, create=create)

    def revoke_confirmation(self, *, request_id: str, actor_id: str, case_id: str,
                            reason: str) -> WriteReceipt:
        """撤销确认（需更高权限）。原结论保留为 revoked，不被删除或改写。"""

        reason = self._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REVOKE_ROLES)
            case_row = self._load_case(connection, case_id)
            if case_row["status"] != "confirmed":
                raise ConflictError("案件未处于确认状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                conclusion = connection.execute(
                    "SELECT c.*, a.role AS confirmer_role FROM case_conclusions c "
                    "JOIN actors a ON a.actor_id = c.created_by "
                    "WHERE c.case_id=? AND c.status='active'", (case_id,)
                ).fetchone()
                # 更高权限：撤销者角色等级必须严格高于原确认者
                if ROLE_RANK[actor["role"]] <= ROLE_RANK.get(conclusion["confirmer_role"], 0):
                    raise PermissionDenied("撤销确认需要高于原确认者的权限")
                now = self._now()
                connection.execute(
                    "UPDATE case_conclusions SET status='revoked', revoked_by=?, revoked_at=?, "
                    "revoke_reason=? WHERE conclusion_id=?",
                    (actor_id, now, reason, conclusion["conclusion_id"]),
                )
                connection.execute(
                    "UPDATE complaint_cases SET status='open', confirmed_at=NULL WHERE case_id=?",
                    (case_id,),
                )
                connection.execute(
                    "INSERT INTO case_decisions(decision_id,case_id,case_version_id,fact_snapshot_id,"
                    "action,target_site_id,reason,from_state_hash,to_state_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, case_id, conclusion["case_version_id"],
                     conclusion["fact_snapshot_id"], "revoked_confirmation", None, reason,
                     conclusion["conclusion_id"], conclusion["conclusion_id"], actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="case.confirmation_revoked",
                             resource_type="case_conclusion", resource_id=conclusion["conclusion_id"],
                             detail={"case_id": case_id, "reason": reason,
                                     "original_conclusion_kept": True},
                             occurred_at=now)
                return "case_conclusion", conclusion["conclusion_id"], {
                    "case_id": case_id, "conclusion_id": conclusion["conclusion_id"],
                    "original_conclusion_kept": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_confirmation", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与溯源
    # ------------------------------------------------------------------

    def get_case_lineage(self, case_id: str) -> dict[str, Any]:
        """从匿名化案件一路追溯到输入事件、规则版本、人工判断和核查建议。"""

        connection = self.database.connection
        case_row = connection.execute("SELECT * FROM complaint_cases WHERE case_id=?", (case_id,)).fetchone()
        if case_row is None:
            raise NotFoundError("案件不存在")
        events = []
        for row in connection.execute(
                "SELECT event_id,channel,occurred_at,received_at,region_code,contact_mask,"
                "location_json,terms_json,merge_reasons,text_fingerprint,created_by,created_at "
                "FROM complaint_events WHERE case_id=? ORDER BY occurred_at,event_id", (case_id,)):
            events.append({"event_id": row["event_id"], "channel": row["channel"],
                           "occurred_at": row["occurred_at"], "received_at": row["received_at"],
                           "region_code": row["region_code"], "contact_mask": row["contact_mask"],
                           "location": json.loads(row["location_json"]) if row["location_json"] else None,
                           "odor_terms": json.loads(row["terms_json"]),
                           "merge_reasons": row["merge_reasons"].split(",") if row["merge_reasons"] else [],
                           "text_fingerprint": row["text_fingerprint"],
                           "created_by": row["created_by"], "created_at": row["created_at"]})
        versions = []
        for row in connection.execute(
                "SELECT cv.*, rv.spec_hash, fs.facts_hash, fs.kind AS snapshot_kind "
                "FROM case_versions cv "
                "JOIN rule_versions rv ON rv.rule_version = cv.rule_version "
                "JOIN fact_snapshots fs ON fs.snapshot_id = cv.fact_snapshot_id "
                "WHERE cv.case_id=? ORDER BY cv.version", (case_id,)):
            versions.append({"case_version_id": row["case_version_id"], "version": row["version"],
                             "is_current": bool(row["is_current"]), "origin": row["origin"],
                             "rule_version": row["rule_version"], "rule_spec_hash": row["spec_hash"],
                             "fact_snapshot_id": row["fact_snapshot_id"],
                             "fact_snapshot_kind": row["snapshot_kind"],
                             "facts_hash": row["facts_hash"],
                             "candidates": json.loads(row["candidates_json"]),
                             "needs_field_inspection": bool(row["needs_field_inspection"]),
                             "state_hash": row["state_hash"],
                             "created_by": row["created_by"], "created_at": row["created_at"]})
        decisions = []
        for row in connection.execute(
                "SELECT d.*, fs.facts_hash FROM case_decisions d "
                "JOIN fact_snapshots fs ON fs.snapshot_id = d.fact_snapshot_id "
                "WHERE d.case_id=? ORDER BY d.created_at, d.decision_id", (case_id,)):
            decisions.append({"decision_id": row["decision_id"], "action": row["action"],
                              "target_site_id": row["target_site_id"], "reason": row["reason"],
                              "case_version_id": row["case_version_id"],
                              "fact_snapshot_id": row["fact_snapshot_id"],
                              "facts_hash": row["facts_hash"],
                              "from_state_hash": row["from_state_hash"],
                              "to_state_hash": row["to_state_hash"],
                              "created_by": row["created_by"], "created_at": row["created_at"]})
        conclusions = []
        for row in connection.execute(
                "SELECT * FROM case_conclusions WHERE case_id=? ORDER BY created_at", (case_id,)):
            conclusions.append({"conclusion_id": row["conclusion_id"], "status": row["status"],
                                "case_version_id": row["case_version_id"], "rule_version": row["rule_version"],
                                "fact_snapshot_id": row["fact_snapshot_id"],
                                "payload": json.loads(row["payload_json"]),
                                "created_by": row["created_by"], "created_at": row["created_at"],
                                "revoked_by": row["revoked_by"], "revoked_at": row["revoked_at"],
                                "revoke_reason": row["revoke_reason"]})
        current = next((v for v in versions if v["is_current"]), None)
        return {
            "case": {"case_id": case_row["case_id"], "region_code": case_row["region_code"],
                     "status": case_row["status"], "first_event_at": case_row["first_event_at"],
                     "last_event_at": case_row["last_event_at"],
                     "created_at": case_row["created_at"], "confirmed_at": case_row["confirmed_at"]},
            "anonymized": True,
            "events": events,
            "versions": versions,
            "current_version": current["version"] if current else None,
            "decisions": decisions,
            "conclusions": conclusions,
            "needs_field_inspection": bool(current and current["needs_field_inspection"]),
        }

    def list_cases(self, status: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT case_id,region_code,status,first_event_at,last_event_at,confirmed_at "
                 "FROM complaint_cases")
        parameters: list[Any] = []
        if status:
            query += " WHERE status=?"
            parameters.append(status)
        query += " ORDER BY last_event_at DESC, case_id"
        return [dict(row) for row in self.database.connection.execute(query, parameters)]

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """返回某个冻结事实快照的完整内容，用于审计还原当时研判依据。"""

        row = self.database.connection.execute(
            "SELECT snapshot_id,case_id,kind,facts_json,facts_hash,rule_version,"
            "created_by,created_at FROM fact_snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if row is None:
            raise NotFoundError("事实快照不存在")
        return {"snapshot_id": row["snapshot_id"], "case_id": row["case_id"], "kind": row["kind"],
                "facts_hash": row["facts_hash"], "rule_version": row["rule_version"],
                "facts": json.loads(row["facts_json"]),
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def list_snapshots(self, case_id: str) -> list[dict[str, Any]]:
        """列出案件的所有冻结事实快照（分析快照与每次决定快照）。"""

        self._load_case(self.database.connection, case_id)
        rows = self.database.connection.execute(
            "SELECT snapshot_id,case_id,kind,facts_hash,rule_version,created_by,created_at "
            "FROM fact_snapshots WHERE case_id=? ORDER BY created_at,snapshot_id", (case_id,)).fetchall()
        return [dict(row) for row in rows]
