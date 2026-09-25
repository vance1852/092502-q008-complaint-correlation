"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .cases import CaseService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          case_service: CaseService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ---- 投诉关联模块 ----
        if case_service is not None:
            status, payload = _route_cases(case_service, method, parsed, body, actor_id)
            if status is not None:
                return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


_CASE_PATH = re.compile(r"^/cases/([A-Za-z0-9_.:-]+)/(versions|decisions|confirm|revoke|lineage|snapshots)$")
_SNAPSHOT_PATH = re.compile(r"^/snapshots/([A-Za-z0-9_.:-]+)$")


def _route_cases(case_service: CaseService, method: str, parsed, body: dict[str, Any],
                 actor_id: str) -> tuple[int | None, dict[str, Any]]:
    path = parsed.path
    snapshot_match = _SNAPSHOT_PATH.match(path)
    if method == "GET" and snapshot_match:
        return 200, case_service.get_snapshot(snapshot_match.group(1))
    if method == "POST" and path == "/rule-versions":
        receipt = case_service.publish_rule_version(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "GET" and path == "/rule-versions":
        return 200, {"items": case_service.list_rule_versions()}
    if method == "POST" and path == "/complaints":
        receipt = case_service.ingest_complaint(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "GET" and path == "/cases":
        query = parse_qs(parsed.query)
        status_filter = query.get("status", [None])[0]
        return 200, {"items": case_service.list_cases(status_filter)}
    match = _CASE_PATH.match(path)
    if match:
        case_id, sub = match.group(1), match.group(2)
        if method == "POST" and sub == "versions":
            receipt = case_service.generate_version(actor_id=actor_id, case_id=case_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and sub == "decisions":
            receipt = case_service.decide_candidate(actor_id=actor_id, case_id=case_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and sub == "confirm":
            receipt = case_service.confirm_case(actor_id=actor_id, case_id=case_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and sub == "revoke":
            receipt = case_service.revoke_confirmation(actor_id=actor_id, case_id=case_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and sub == "lineage":
            return 200, case_service.get_case_lineage(case_id)
        if method == "GET" and sub == "snapshots":
            return 200, {"items": case_service.list_snapshots(case_id)}
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    case_service: CaseService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                case_service=self.case_service)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动环保业务基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.case_service = CaseService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
