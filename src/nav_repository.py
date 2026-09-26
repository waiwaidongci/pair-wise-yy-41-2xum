from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from . import nav_rules
from .audit import utc_now
from .domain import ConflictError, NotFoundError
from .repository import Repository


class NavRepository:
    """桥孔档案与通行单持久化：与结构监测共用连接、锁和审计链。"""

    def __init__(self, base: Repository):
        self.base = base
        self.conn = base.conn
        self._lock = base._lock
        self._create_schema()

    def _create_schema(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS nav_openings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    bridge_name TEXT NOT NULL,
                    water_datum REAL NOT NULL,
                    soffit_elevation REAL NOT NULL,
                    safety_margin REAL NOT NULL DEFAULT 1.0,
                    water_level REAL,
                    gauge_name TEXT NOT NULL,
                    gauge_status TEXT NOT NULL DEFAULT 'online'
                        CHECK(gauge_status IN ('online','offline')),
                    datum_version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_nav_openings_ref
                    ON nav_openings(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS nav_passages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opening_id INTEGER NOT NULL
                        REFERENCES nav_openings(id) ON DELETE CASCADE,
                    fleet_name TEXT NOT NULL,
                    vessel_name TEXT NOT NULL,
                    draft REAL NOT NULL,
                    planned_start TEXT NOT NULL,
                    planned_end TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('active','completed','invalidated')),
                    clearance_at_judgment REAL,
                    remaining_at_judgment REAL,
                    datum_version_at_judgment INTEGER NOT NULL,
                    invalid_reason TEXT,
                    supersedes_id INTEGER,
                    external_ref TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_nav_passages_active
                    ON nav_passages(opening_id) WHERE status='active';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_nav_passages_ref
                    ON nav_passages(opening_id, external_ref)
                    WHERE external_ref IS NOT NULL;
            """)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        return self.base.append_audit(action, entity_type, entity_id, actor, detail)

    # ---- 桥孔档案 ----

    def _opening_locked(self, opening_id: int) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM nav_openings WHERE id=?", (opening_id,)).fetchone()
        if row is None:
            raise NotFoundError("桥孔不存在")
        return dict(row)

    def get_opening(self, opening_id: int) -> Dict[str, Any]:
        with self._lock:
            return self._opening_locked(opening_id)

    def list_openings(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM nav_openings ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_opening(self, name: str, bridge_name: str, water_datum: float,
                       soffit_elevation: float, safety_margin: float,
                       gauge_name: str, external_ref: Optional[str],
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO nav_openings(name, bridge_name, water_datum,
                       soffit_elevation, safety_margin, gauge_name, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (name, bridge_name, water_datum, soffit_elevation, safety_margin,
                     gauge_name, external_ref, actor, now, now),
                )
                opening_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_opening(opening_id)

    def report_water_level(self, opening_id: int, water_level: float,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._opening_locked(opening_id)
            self.conn.execute(
                """UPDATE nav_openings SET water_level=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (water_level, now, opening_id))
        return self.get_opening(opening_id)

    def set_gauge_status(self, opening_id: int, status: str,
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._opening_locked(opening_id)
            self.conn.execute(
                """UPDATE nav_openings SET gauge_status=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (status, now, opening_id))
        return self.get_opening(opening_id)

    def correct_datum(self, opening_id: int, new_datum: float,
                      actor: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """水位基准更正：升基准版本，未结束通行单失效并按新基准重算。"""
        now = utc_now()
        with self._lock, self.conn:
            self._opening_locked(opening_id)
            self.conn.execute(
                """UPDATE nav_openings SET water_datum=?,
                   datum_version=datum_version+1, version=version+1, updated_at=?
                   WHERE id=?""",
                (new_datum, now, opening_id))
            updated = self._opening_locked(opening_id)
            results = []
            for passage in self._active_passages_locked(opening_id):
                self._invalidate_locked(passage["id"], "水位基准更正，原通行单失效", now)
                verdict = nav_rules.evaluate(updated, passage["draft"])
                successor_id = None
                if verdict["ok"]:
                    successor_id = self._insert_passage_locked(
                        passage, passage["draft"], verdict,
                        updated["datum_version"], passage["id"], actor, now)
                results.append({"invalidated_id": passage["id"],
                                "successor_id": successor_id, "verdict": verdict})
        return updated, results

    # ---- 通行单 ----

    def _passage_locked(self, passage_id: int) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM nav_passages WHERE id=?", (passage_id,)).fetchone()
        if row is None:
            raise NotFoundError("通行单不存在")
        return dict(row)

    def get_passage(self, passage_id: int) -> Dict[str, Any]:
        with self._lock:
            return self._passage_locked(passage_id)

    def list_passages(self, opening_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            self._opening_locked(opening_id)
            rows = self.conn.execute(
                "SELECT * FROM nav_passages WHERE opening_id=? ORDER BY id DESC",
                (opening_id,)).fetchall()
        return [dict(row) for row in rows]

    def _active_passage_locked(self, opening_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM nav_passages WHERE opening_id=? AND status='active'",
            (opening_id,)).fetchone()
        return dict(row) if row else None

    def _active_passages_locked(self, opening_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM nav_passages WHERE opening_id=? AND status='active'",
            (opening_id,)).fetchall()
        return [dict(row) for row in rows]

    def active_passage(self, opening_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._active_passage_locked(opening_id)

    def _insert_passage_locked(self, passage: Dict[str, Any], draft: float,
                               verdict: Dict[str, Any], datum_version: int,
                               supersedes_id: Optional[int], actor: str,
                               now: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO nav_passages(opening_id, fleet_name, vessel_name, draft,
               planned_start, planned_end, status, clearance_at_judgment,
               remaining_at_judgment, datum_version_at_judgment, supersedes_id,
               version, created_by, created_at, updated_at)
               VALUES(?,?,?,?,?,?, 'active', ?,?,?,?, 1, ?,?,?)""",
            (passage["opening_id"], passage["fleet_name"], passage["vessel_name"],
             draft, passage["planned_start"], passage["planned_end"],
             verdict["clearance"], verdict["remaining"], datum_version,
             supersedes_id, actor, now, now),
        )
        return int(cur.lastrowid)

    def _invalidate_locked(self, passage_id: int, reason: str, now: str) -> None:
        self.conn.execute(
            """UPDATE nav_passages SET status='invalidated', invalid_reason=?,
               version=version+1, updated_at=? WHERE id=?""",
            (reason, now, passage_id))

    def request_passage(self, opening_id: int, fleet_name: str, vessel_name: str,
                        draft: float, planned_start: str, planned_end: str,
                        external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        """通行请求：判定与落库在同一事务内，冲突抛409并说明原因。"""
        now = utc_now()
        with self._lock, self.conn:
            opening = self._opening_locked(opening_id)
            active = self._active_passage_locked(opening_id)
            verdict = nav_rules.evaluate_request(opening, draft, active)
            if not verdict["ok"]:
                raise ConflictError(verdict["reason"])
            try:
                cur = self.conn.execute(
                    """INSERT INTO nav_passages(opening_id, fleet_name, vessel_name,
                       draft, planned_start, planned_end, status,
                       clearance_at_judgment, remaining_at_judgment,
                       datum_version_at_judgment, external_ref, version,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?, 'active', ?,?,?,?, 1, ?,?,?)""",
                    (opening_id, fleet_name, vessel_name, draft, planned_start,
                     planned_end, verdict["clearance"], verdict["remaining"],
                     opening["datum_version"], external_ref, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("同一桥孔已有未结束通行单或船队单号重复") from exc
            passage_id = int(cur.lastrowid)
        return self.get_passage(passage_id)

    def complete_passage(self, passage_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            passage = self._passage_locked(passage_id)
            if passage["status"] != "active":
                raise ConflictError("通行单已结束或已失效")
            self.conn.execute(
                """UPDATE nav_passages SET status='completed', version=version+1,
                   updated_at=? WHERE id=?""",
                (now, passage_id))
        return self.get_passage(passage_id)

    def correct_draft(self, passage_id: int, new_draft: float,
                      actor: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]],
                                           Dict[str, Any]]:
        """吃水更正：原通行单失效，按新吃水重算；通过则签发后继通行单。"""
        now = utc_now()
        with self._lock, self.conn:
            passage = self._passage_locked(passage_id)
            if passage["status"] != "active":
                raise ConflictError("通行单已结束或已失效，不能更正吃水")
            opening = self._opening_locked(passage["opening_id"])
            self._invalidate_locked(passage_id, "船舶吃水更正，原通行单失效", now)
            verdict = nav_rules.evaluate(opening, new_draft)
            successor_id = None
            if verdict["ok"]:
                successor_id = self._insert_passage_locked(
                    passage, new_draft, verdict, opening["datum_version"],
                    passage_id, actor, now)
        invalidated = self.get_passage(passage_id)
        successor = self.get_passage(successor_id) if successor_id else None
        return invalidated, successor, verdict
