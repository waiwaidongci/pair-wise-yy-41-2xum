import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.clearance_repository import ClearanceRepository
from src.clearance_rules import current_clearance, required_clearance
from src.clearance_service import ClearanceService
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.http_api import make_handler


def span_payload(**overrides):
    payload = {
        "span_code": "NC-01", "name": "3号桥孔", "bridge_name": "乌江大桥",
        "datum_elevation": 20.0, "beam_elevation": 35.0, "safety_margin": 0.5,
    }
    payload.update(overrides)
    return payload


def order_payload(span_id, **overrides):
    payload = {
        "span_id": span_id, "fleet_name": "川航一队", "vessel": "渝航货88",
        "draft": 4.0,
        "planned_start": "2026-09-26T10:00:00+00:00",
        "planned_end": "2026-09-26T12:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class ClearanceClearanceRulesTest(unittest.TestCase):
    def test_clearance_math(self):
        # 梁底35 - 基准20 - 水位9 = 净空6
        self.assertEqual(current_clearance(35.0, 20.0, 9.0), 6.0)
        self.assertIsNone(current_clearance(35.0, 20.0, None))
        self.assertEqual(required_clearance(4.0, 0.5), 4.5)

    def test_evaluate_passage_rejects_three_conflicts(self):
        span = {
            "id": 1, "gauge_status": "online", "gauge_reading": 9.0,
            "beam_elevation": 35.0, "datum_elevation": 20.0,
            "safety_margin": 0.5,
        }
        from src.clearance_rules import evaluate_passage
        verdict = evaluate_passage(span, 4.0, None)
        self.assertEqual(verdict["clearance"], 6.0)
        with self.assertRaises(ConflictError) as ctx:
            evaluate_passage(span, 6.0, None)
        self.assertIn("净空不足", str(ctx.exception))
        with self.assertRaises(ConflictError) as ctx:
            evaluate_passage({**span, "gauge_status": "offline"}, 4.0, None)
        self.assertIn("水位测点离线", str(ctx.exception))
        with self.assertRaises(ConflictError) as ctx:
            evaluate_passage(span, 4.0, {"id": 7, "fleet_name": "另一船队"})
        self.assertIn("未结束通行单#7", str(ctx.exception))


class ClearanceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ClearanceRepository(str(Path(self.tmp.name) / "clearance.db"))
        self.service = ClearanceService(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _ready_span(self, reading=9.0, **overrides):
        span = self.service.create_span(
            span_payload(**overrides), "张工", "bridge_engineer")
        span = self.service.post_reading(
            span["id"], {"reading": reading}, "水情员", "sensor_operator")
        return span

    def test_register_reading_list_and_passage_flow(self):
        span = self._ready_span()
        self.assertEqual(span["current_clearance"], 6.0)
        self.assertFalse(span["needs_resurvey"])
        # 台账列表显示剩余净空
        ledger = self.service.list_spans("viewer")
        self.assertEqual(ledger["pending_resurvey"], [])
        row = ledger["spans"][0]
        self.assertEqual(row["remaining_clearance"], 6.0)
        # 船队申报成功
        order = self.service.create_order(
            order_payload(span["id"]), "调度", "fleet_operator")
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["required_clearance"], 4.5)
        self.assertEqual(order["clearance_snapshot"], 6.0)
        # 未结束通行单占用后，剩余净空=6-4.5
        ledger = self.service.list_spans("viewer")
        self.assertEqual(ledger["spans"][0]["remaining_clearance"], 1.5)
        self.assertEqual(ledger["spans"][0]["active_order_id"], order["id"])
        # 结束后释放
        closed = self.service.complete_order(order["id"], "值班员", "traffic_authority")
        self.assertEqual(closed["status"], "completed")
        self.assertIsNone(
            self.service.list_spans("viewer")["spans"][0]["active_order_id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_gauge_offline_before_reading_blocks(self):
        span = self.service.create_span(span_payload(), "张工", "bridge_engineer")
        self.assertTrue(span["needs_resurvey"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_order(
                order_payload(span["id"]), "调度", "fleet_operator")
        self.assertIn("水位测点离线", str(ctx.exception))
        # 台账列表把待复测桥孔单列
        ledger = self.service.list_spans("viewer")
        self.assertEqual(ledger["pending_resurvey"][0]["id"], span["id"])

    def test_insufficient_clearance_returns_conflict(self):
        span = self._ready_span(reading=11.4)  # 净空3.6 < 需要4.5
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_order(
                order_payload(span["id"], draft=4.0), "调度", "fleet_operator")
        message = str(ctx.exception)
        self.assertIn("净空不足", message)
        self.assertIn("当前净空3.60米", message)

    def test_duplicate_active_order_returns_conflict(self):
        span = self._ready_span()
        first = self.service.create_order(
            order_payload(span["id"]), "调度", "fleet_operator")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_order(
                order_payload(span["id"], fleet_name="川航二队"),
                "调度", "fleet_operator")
        self.assertIn(f"通行单#{first['id']}", str(ctx.exception))

    def test_datum_correction_invalidates_and_recalculate(self):
        span = self._ready_span(reading=9.0)  # 净空6
        order = self.service.create_order(
            order_payload(span["id"], draft=5.0), "调度", "fleet_operator")  # 需5.5
        # 基准更正20->22：净空缩水到4，通行单失效
        updated = self.service.correct_datum(
            span["id"], {"datum_elevation": 22.0, "expected_version": span["version"]},
            "张工", "bridge_engineer")
        self.assertEqual(updated["id"], span["id"])
        self.assertEqual(updated["invalidated_order_ids"], [order["id"]])
        self.assertTrue(updated["needs_resurvey"])
        order = self.service.get_order(order["id"], "viewer")
        self.assertEqual(order["status"], "invalidated")
        self.assertIn("水位基准更正", order["invalid_reason"])
        # 未复测直接重算：净空不足409，通行单仍失效
        with self.assertRaises(ConflictError) as ctx:
            self.service.recalculate_order(
                order["id"], {"expected_version": order["version"]},
                "值班员", "traffic_authority")
        self.assertIn("净空不足", str(ctx.exception))
        # 复测水位回落到7.2（净空5.8）后重算通过
        self.service.post_reading(
            span["id"], {"reading": 7.2}, "水情员", "sensor_operator")
        order = self.service.get_order(order["id"], "viewer")
        restored = self.service.recalculate_order(
            order["id"], {"expected_version": order["version"]},
            "值班员", "traffic_authority")
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["clearance_snapshot"], 5.8)
        self.assertIsNone(restored["invalid_reason"])

    def test_draft_correction_invalidates_and_recalculate(self):
        span = self._ready_span(reading=9.0)  # 净空6
        order = self.service.create_order(
            order_payload(span["id"], draft=3.0), "调度", "fleet_operator")  # 需3.5
        # 吃水更正为6米：原单失效
        order = self.service.correct_draft(
            order["id"], {"draft": 6.0, "expected_version": order["version"]},
            "调度", "fleet_operator")
        self.assertEqual(order["status"], "invalidated")
        self.assertIn("吃水更正", order["invalid_reason"])
        # 需要6.5 > 净空6 -> 409
        with self.assertRaises(ConflictError):
            self.service.recalculate_order(
                order["id"], {"expected_version": order["version"]},
                "调度", "fleet_operator")
        # 复测水位回落至8.0（净空7.0）后重算通过
        self.service.post_reading(
            span["id"], {"reading": 8.0}, "水情员", "sensor_operator")
        order = self.service.get_order(order["id"], "viewer")
        restored = self.service.recalculate_order(
            order["id"], {"expected_version": order["version"]},
            "调度", "fleet_operator")
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["required_clearance"], 6.5)

    def test_offline_gauge_blocks_after_active_order(self):
        span = self._ready_span()
        order = self.service.create_order(
            order_payload(span["id"]), "调度", "fleet_operator")
        # 前一船队通行结束，随后测点离线
        self.service.complete_order(order["id"], "值班员", "traffic_authority")
        self.service.set_gauge_status(
            span["id"], {"status": "offline"}, "水情员", "sensor_operator")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_order(
                order_payload(span["id"], fleet_name="川航二队"),
                "调度", "fleet_operator")
        self.assertIn("水位测点离线", str(ctx.exception))

    def test_permissions_and_validation(self):
        span = self._ready_span()
        with self.assertRaises(PermissionDenied):
            self.service.create_span(span_payload(span_code="NC-99"),
                                     "闲人", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_order(
                order_payload(span["id"]), "调度", "viewer")
        with self.assertRaises(ValidationError):
            self.service.create_order(
                order_payload(span["id"], draft=-1), "调度", "fleet_operator")
        with self.assertRaises(ValidationError):
            self.service.create_order(
                order_payload(span["id"], planned_end="2026-09-26T09:00:00+00:00"),
                "调度", "fleet_operator")

    def test_version_conflict_on_datum_correction(self):
        span = self._ready_span()
        self.service.create_order(
            order_payload(span["id"]), "调度", "fleet_operator")
        with self.assertRaises(ConflictError):
            self.service.correct_datum(
                span["id"], {"datum_elevation": 21.0, "expected_version": 999},
                "张工", "bridge_engineer")


class ClearanceHttpTest(unittest.TestCase):
    """请求入口：409响应必须带上具体原因。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = str(Path(cls.tmp.name) / "http.db")
        from src.repository import Repository
        from src.service import Service
        repository = Repository(db)
        cls.clearance_repo = ClearanceRepository(db)
        clearance_service = ClearanceService(cls.clearance_repo)
        handler = make_handler(Service(repository), str(Path(__file__).resolve().parents[1] / "static"),
                               clearance_service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.clearance_repo.close()
        cls.tmp.cleanup()

    def _request(self, method, path, body=None, role="viewer", actor="tester"):
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor": actor, "X-Role": role}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def test_conflict_status_and_reason_over_http(self):
        status, body = self._request(
            "POST", "/api/clearance/spans", span_payload(span_code="NC-H1"),
            role="bridge_engineer")
        self.assertEqual(status, 201)
        span_id = body["id"]
        # 测点未读数 -> 409
        status, body = self._request(
            "POST", "/api/clearance/orders", order_payload(span_id),
            role="fleet_operator")
        self.assertEqual(status, 409)
        self.assertIn("水位测点离线", body["message"])
        # 读数后受理成功
        status, _ = self._request(
            "POST", f"/api/clearance/spans/{span_id}/readings",
            {"reading": 9.0}, role="sensor_operator")
        self.assertEqual(status, 201)
        status, body = self._request(
            "POST", "/api/clearance/orders", order_payload(span_id),
            role="fleet_operator")
        self.assertEqual(status, 201)
        order_id, version = body["id"], body["version"]
        # 重复申报 -> 409 并指明已有单号
        status, body = self._request(
            "POST", "/api/clearance/orders",
            order_payload(span_id, fleet_name="川航二队"), role="fleet_operator")
        self.assertEqual(status, 409)
        self.assertIn(str(order_id), body["message"])
        # 基准更正后原单失效，列表出现待复测桥孔
        status, body = self._request(
            "POST", f"/api/clearance/spans/{span_id}/datum",
            {"datum_elevation": 23.0, "expected_version": 2},
            role="bridge_engineer")
        self.assertEqual(status, 200)
        self.assertEqual(body["invalidated_order_ids"], [order_id])
        status, body = self._request("GET", "/api/clearance/spans")
        self.assertEqual(status, 200)
        self.assertEqual(body["pending_resurvey"][0]["id"], span_id)
        # 重算净空不足 -> 409
        status, body = self._request(
            "POST", f"/api/clearance/orders/{order_id}/recalculate",
            {"expected_version": version + 1}, role="traffic_authority")
        self.assertEqual(status, 409)
        self.assertIn("净空不足", body["message"])


if __name__ == "__main__":
    unittest.main()
