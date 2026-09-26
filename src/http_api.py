from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str, clearance_service=None):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        @staticmethod
        def _optional_int(value: Optional[str]) -> Optional[int]:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except ValueError as exc:
                raise ValidationError("查询参数必须是整数") from exc

        @staticmethod
        def _path_id(path: str, prefix: str) -> int:
            tail = path[len(prefix):].strip("/")
            if not tail.isdigit():
                raise NotFoundError("资源不存在")
            return int(tail)

        @staticmethod
        def _subpath_id(path: str, collection: str, suffix: str) -> int:
            # /api/clearance/{collection}/{id}/{suffix}
            parts = path.strip("/").split("/")
            expected = ("api", "clearance", collection)
            if len(parts) != 5 or tuple(parts[:3]) != expected or parts[4] != suffix:
                raise NotFoundError("not_found")
            if not parts[3].isdigit():
                raise NotFoundError("资源不存在")
            return int(parts[3])

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path == "/api/clearance/spans":
                    if clearance_service is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        actor, role = self._identity()
                        del actor
                        self._json(200, clearance_service.list_spans(role))
                elif path.startswith("/api/clearance/spans/"):
                    if clearance_service is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        span_id = self._path_id(path, "/api/clearance/spans/")
                        actor, role = self._identity()
                        del actor
                        self._json(200, clearance_service.get_span(span_id, role))
                elif path == "/api/clearance/orders":
                    if clearance_service is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        actor, role = self._identity()
                        del actor
                        query = parse_qs(urlparse(self.path).query)
                        span_id = self._optional_int(query.get("span_id", [None])[0])
                        status = query.get("status", [None])[0]
                        orders = clearance_service.list_orders(role, span_id, status)
                        self._json(200, {"orders": orders})
                elif path.startswith("/api/clearance/orders/"):
                    if clearance_service is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        order_id = self._path_id(path, "/api/clearance/orders/")
                        actor, role = self._identity()
                        del actor
                        self._json(200, clearance_service.get_order(order_id, role))
                elif path == "/api/clearance/audit":
                    if clearance_service is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        actor, role = self._identity()
                        del actor
                        query = parse_qs(urlparse(self.path).query)
                        entity_id = self._optional_int(query.get("entity_id", [None])[0])
                        self._json(200, {
                            "events": clearance_service.audit(role, entity_id)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if clearance_service is None and urlparse(path).path.startswith(
                        "/api/clearance/"):
                    self._json(404, {"error": "not_found"})
                    return
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path == "/api/clearance/spans":
                    self._json(201, clearance_service.create_span(body, actor, role))
                elif path == "/api/clearance/orders":
                    self._json(201, clearance_service.create_order(body, actor, role))
                elif path.startswith("/api/clearance/spans/") and path.endswith("/datum"):
                    span_id = self._subpath_id(path, "spans", "datum")
                    self._json(200, clearance_service.correct_datum(
                        span_id, body, actor, role))
                elif path.startswith("/api/clearance/spans/") and path.endswith("/readings"):
                    span_id = self._subpath_id(path, "spans", "readings")
                    self._json(201, clearance_service.post_reading(
                        span_id, body, actor, role))
                elif path.startswith("/api/clearance/spans/") and path.endswith("/gauge"):
                    span_id = self._subpath_id(path, "spans", "gauge")
                    self._json(200, clearance_service.set_gauge_status(
                        span_id, body, actor, role))
                elif path.startswith("/api/clearance/orders/") and path.endswith("/complete"):
                    order_id = self._subpath_id(path, "orders", "complete")
                    self._json(200, clearance_service.complete_order(
                        order_id, actor, role))
                elif path.startswith("/api/clearance/orders/") and path.endswith("/draft"):
                    order_id = self._subpath_id(path, "orders", "draft")
                    self._json(200, clearance_service.correct_draft(
                        order_id, body, actor, role))
                elif path.startswith("/api/clearance/orders/") and path.endswith("/recalculate"):
                    order_id = self._subpath_id(path, "orders", "recalculate")
                    self._json(200, clearance_service.recalculate_order(
                        order_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
