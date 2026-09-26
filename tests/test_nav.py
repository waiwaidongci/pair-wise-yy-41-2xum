import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.http_api import make_handler
from src.nav_repository import NavRepository
from src.nav_service import NavService
from src.repository import Repository
from src.service import Service

STATIC_DIR = str(Path(__file__).resolve().parent.parent / "static")
WINDOW = {"planned_start": "2026-09-26T08:00:00+00:00",
          "planned_end": "2026-09-26T10:00:00+00:00"}


class NavServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.nav_repo = NavRepository(self.repo)
        self.service = NavService(self.nav_repo)
        self.opening = self.service.create_opening(
            {"name": "浔江大桥3号孔", "bridge_name": "浔江大桥",
             "water_datum": 5.0, "soffit_elevation": 18.0,
             "safety_margin": 1.0, "gauge_name": "浔江水位站",
             "external_ref": "SPAN-3"}, "engineer", "bridge_engineer")
        self.service.report_water_level(self.opening["id"],
                                        {"water_level": 3.0},
                                        "operator", "sensor_operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _request(self, draft=2.0, **extra):
        payload = {"fleet_name": "顺达船队", "vessel_name": "顺达08",
                   "draft": draft, **WINDOW, **extra}
        return self.service.request_passage(self.opening["id"], payload,
                                            "fleet", "fleet_operator")

    def test_ledger_lists_clearance_and_pending_remeasure(self):
        # 当前净空 = 18.0 - (5.0 + 3.0) = 10.0
        listed = self.service.list_openings("viewer")
        item = listed["items"][0]
        self.assertEqual(item["current_clearance"], 10.0)
        self.assertEqual(item["remaining_clearance"], 10.0)
        self.assertFalse(item["pending_remeasure"])
        self.assertEqual(listed["pending_remeasure_openings"], [])
        # 无水位数据的桥孔进入待复测列表
        other = self.service.create_opening(
            {"name": "浔江大桥4号孔", "bridge_name": "浔江大桥",
             "water_datum": 5.0, "soffit_elevation": 18.0,
             "gauge_name": "浔江水位站"}, "engineer", "bridge_engineer")
        listed = self.service.list_openings("viewer")
        self.assertIsNone(listed["items"][1]["current_clearance"])
        self.assertEqual(listed["pending_remeasure_openings"],
                         [{"id": other["id"], "name": "浔江大桥4号孔",
                           "reason": "水位测点无数据"}])

    def test_request_passage_success_and_remaining_clearance(self):
        passage = self._request(draft=2.0)
        self.assertEqual(passage["status"], "active")
        self.assertEqual(passage["clearance_at_judgment"], 10.0)
        self.assertEqual(passage["remaining_at_judgment"], 8.0)
        item = self.service.list_openings("viewer")["items"][0]
        self.assertEqual(item["active_passage_id"], passage["id"])
        self.assertEqual(item["remaining_clearance"], 8.0)

    def test_conflict_insufficient_clearance(self):
        # 剩余净空 10.0 - 9.5 = 0.5 < 安全富余 1.0
        with self.assertRaises(ConflictError) as ctx:
            self._request(draft=9.5)
        self.assertIn("净空不足", str(ctx.exception))

    def test_conflict_gauge_offline(self):
        self.service.set_gauge(self.opening["id"], {"status": "offline"},
                               "operator", "sensor_operator")
        with self.assertRaises(ConflictError) as ctx:
            self._request()
        self.assertIn("水位测点离线", str(ctx.exception))
        item = self.service.list_openings("viewer")["items"][0]
        self.assertTrue(item["pending_remeasure"])

    def test_conflict_unfinished_passage_then_complete(self):
        first = self._request()
        with self.assertRaises(ConflictError) as ctx:
            self._request()
        self.assertIn("未结束通行单", str(ctx.exception))
        self.service.complete_passage(first["id"], "fleet", "fleet_operator")
        second = self._request()
        self.assertEqual(second["status"], "active")

    def test_draft_correction_invalidates_and_recalculates(self):
        passage = self._request(draft=2.0)
        # 更正为8.5：剩余1.5米仍满足安全富余，签发后继单
        result = self.service.correct_draft(passage["id"], {"draft": 8.5},
                                            "fleet", "fleet_operator")
        self.assertEqual(result["invalidated"]["status"], "invalidated")
        self.assertEqual(result["invalidated"]["invalid_reason"],
                         "船舶吃水更正，原通行单失效")
        self.assertTrue(result["recalculation"]["ok"])
        successor = result["successor"]
        self.assertEqual(successor["status"], "active")
        self.assertEqual(successor["draft"], 8.5)
        self.assertEqual(successor["supersedes_id"], passage["id"])
        # 再更正为9.5：剩余0.5米不足，原单失效且无后继单
        result = self.service.correct_draft(successor["id"], {"draft": 9.5},
                                            "fleet", "fleet_operator")
        self.assertFalse(result["recalculation"]["ok"])
        self.assertIn("净空不足", result["recalculation"]["reason"])
        self.assertIsNone(result["successor"])
        self.assertIsNone(
            self.service.list_openings("viewer")["items"][0]["active_passage_id"])

    def test_datum_correction_invalidates_and_recalculates(self):
        passage = self._request(draft=2.0)
        # 基准5.0→6.0：净空9.0，剩余7.0，重算通过
        result = self.service.correct_datum(self.opening["id"],
                                            {"water_datum": 6.0},
                                            "engineer", "bridge_engineer")
        self.assertEqual(result["opening"]["datum_version"], 2)
        self.assertEqual(result["opening"]["current_clearance"], 9.0)
        recalc = result["recalculated"][0]
        self.assertEqual(recalc["invalidated_id"], passage["id"])
        self.assertTrue(recalc["ok"])
        self.assertIsNotNone(recalc["successor_id"])
        old = self.service.get_passage(passage["id"], "viewer")
        self.assertEqual(old["status"], "invalidated")
        # 基准6.0→13.0：净空2.0，剩余0.0不足，重算失败且无后继单
        result = self.service.correct_datum(self.opening["id"],
                                            {"water_datum": 13.0},
                                            "engineer", "bridge_engineer")
        recalc = result["recalculated"][0]
        self.assertFalse(recalc["ok"])
        self.assertIn("净空不足", recalc["reason"])
        self.assertIsNone(recalc["successor_id"])
        self.assertIsNone(result["opening"]["active_passage_id"])

    def test_correction_rejects_finished_passage(self):
        passage = self._request()
        self.service.complete_passage(passage["id"], "fleet", "fleet_operator")
        with self.assertRaises(ConflictError):
            self.service.correct_draft(passage["id"], {"draft": 3.0},
                                       "fleet", "fleet_operator")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_opening(
                {"name": "x", "bridge_name": "x", "water_datum": 1.0,
                 "soffit_elevation": 9.0, "gauge_name": "g"},
                "fleet", "fleet_operator")
        with self.assertRaises(PermissionDenied):
            self.service.request_passage(
                self.opening["id"], {"fleet_name": "f", "vessel_name": "v",
                                     "draft": 1.0, **WINDOW},
                "viewer", "viewer")
        with self.assertRaises(ValidationError):
            self._request(draft=2.0, planned_end="2026-09-26T07:00:00+00:00")
        with self.assertRaises(ValidationError):
            self.service.correct_datum(self.opening["id"], {"water_datum": 5.0},
                                       "engineer", "bridge_engineer")


class NavHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.nav = NavService(NavRepository(self.repo))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, STATIC_DIR, self.nav))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        opening = self.nav.create_opening(
            {"name": "浔江大桥3号孔", "bridge_name": "浔江大桥",
             "water_datum": 5.0, "soffit_elevation": 18.0,
             "safety_margin": 1.0, "gauge_name": "浔江水位站"},
            "engineer", "bridge_engineer")
        self.opening_id = opening["id"]
        self.nav.report_water_level(self.opening_id, {"water_level": 3.0},
                                    "operator", "sensor_operator")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def _call(self, method, path, body=None, role="fleet_operator"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor": "tester", "X-Role": role}
        payload = json.dumps(body) if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def _passage_payload(self, draft=2.0):
        return {"fleet_name": "顺达船队", "vessel_name": "顺达08",
                "draft": draft, **WINDOW}

    def test_request_returns_409_with_reasons(self):
        path = f"/api/nav/openings/{self.opening_id}/passages"
        # 净空不足
        status, data = self._call("POST", path, self._passage_payload(draft=9.5))
        self.assertEqual(status, 409)
        self.assertIn("净空不足", data["message"])
        # 正常申报
        status, data = self._call("POST", path, self._passage_payload())
        self.assertEqual(status, 201)
        self.assertEqual(data["status"], "active")
        # 同一桥孔已有未结束通行单
        status, again = self._call("POST", path, self._passage_payload())
        self.assertEqual(status, 409)
        self.assertIn("未结束通行单", again["message"])
        # 结束后测点离线
        self._call("POST", f"/api/nav/passages/{data['id']}/complete", {})
        self.nav.set_gauge(self.opening_id, {"status": "offline"},
                           "operator", "sensor_operator")
        status, data = self._call("POST", path, self._passage_payload())
        self.assertEqual(status, 409)
        self.assertIn("水位测点离线", data["message"])

    def test_list_shows_remaining_clearance_and_pending(self):
        status, data = self._call("GET", "/api/nav/openings", role="viewer")
        self.assertEqual(status, 200)
        item = data["items"][0]
        self.assertEqual(item["current_clearance"], 10.0)
        self.assertEqual(item["remaining_clearance"], 10.0)
        self.assertEqual(data["pending_remeasure_openings"], [])
        self.nav.set_gauge(self.opening_id, {"status": "offline"},
                           "operator", "sensor_operator")
        status, data = self._call("GET", "/api/nav/openings", role="viewer")
        self.assertTrue(data["items"][0]["pending_remeasure"])
        self.assertEqual(data["pending_remeasure_openings"][0]["reason"],
                         "水位测点离线")


if __name__ == "__main__":
    unittest.main()
