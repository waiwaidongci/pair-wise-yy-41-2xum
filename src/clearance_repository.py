from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .clearance_domain import (ACTIVE_ORDER_STATUS, COMPLETED_ORDER_STATUS,
                               INVALIDATED_ORDER_STATUS, ORDER_STATES)
from .domain import ConflictError, NotFoundError


class ClearanceRepository:
    """桥孔档案库：桥孔台账、通行单和净空审计链，与结构监测档案分表存放。"""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self._create_schema()

    def _create_schema(self) -> None:
        order_states = ",".join("'" + s + "'" for s in ORDER_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS clearance_spans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    span_code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    bridge_name TEXT NOT NULL,
                    datum_elevation REAL NOT NULL,
                    beam_elevation REAL NOT NULL,
                    safety_margin REAL NOT NULL DEFAULT 0.5,
                    gauge_status TEXT NOT NULL DEFAULT 'offline'
                        CHECK(gauge_status IN ('online','offline')),
                    gauge_reading REAL,
                    reading_at TEXT,
                    needs_resurvey INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clearance_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    span_id INTEGER NOT NULL
                        REFERENCES clearance_spans(id) ON DELETE CASCADE,
                    fleet_name TEXT NOT NULL,
                    vessel TEXT,
                    draft REAL NOT NULL,
                    planned_start TEXT NOT NULL,
                    planned_end TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({order_states})),
                    required_clearance REAL NOT NULL,
                    clearance_snapshot REAL,
                    invalid_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_clearance_orders_active_span
                    ON clearance_orders(span_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS clearance_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    # ---- 桥孔档案 ----

    def create_span(self, span_code: str, name: str, bridge_name: str,
                    datum_elevation: float, beam_elevation: float,
                    safety_margin: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO clearance_spans(span_code, name, bridge_name,
                       datum_elevation, beam_elevation, safety_margin,
                       gauge_status, needs_resurvey, version,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,'offline',1,1,?,?,?)""",
                    (span_code, name, bridge_name, datum_elevation,
                     beam_elevation, safety_margin, actor, now, now),
                )
                span_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("桥孔编号已存在") from exc
        return self.get_span(span_id)

    def get_span(self, span_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM clearance_spans WHERE id=?", (span_id,)).fetchone()
        if row is None:
            raise NotFoundError("桥孔不存在")
        return dict(row)

    def list_spans(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM clearance_spans ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def correct_datum(self, span_id: int, datum_elevation: float,
                      expected_version: int) -> Dict[str, Any]:
        """水位基准更正：桥孔转入待复测，版本校验防并发覆盖。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE clearance_spans SET datum_elevation=?, needs_resurvey=1,
                   version=version+1, updated_at=? WHERE id=? AND version=?""",
                (datum_elevation, now, span_id, expected_version),
            )
            if cur.rowcount == 0:
                self._span_conflict_or_missing(span_id)
        return self.get_span(span_id)

    def post_reading(self, span_id: int, reading: float, reading_at: str) -> Dict[str, Any]:
        """登记新的水位读数（复测完成）：测点恢复在线，清除待复测标记。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE clearance_spans SET gauge_reading=?, reading_at=?,
                   gauge_status='online', needs_resurvey=0,
                   version=version+1, updated_at=? WHERE id=?""",
                (reading, reading_at, now, span_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("桥孔不存在")
        return self.get_span(span_id)

    def set_gauge_status(self, span_id: int, status: str) -> Dict[str, Any]:
        """测点离线后读数不可信，桥孔转入待复测。"""
        now = utc_now()
        needs_resurvey = 1 if status == 'offline' else 0
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE clearance_spans SET gauge_status=?, needs_resurvey=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (status, needs_resurvey, now, span_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("桥孔不存在")
        return self.get_span(span_id)

    # ---- 通行单 ----

    def create_order(self, span_id: int, fleet_name: str, vessel: Optional[str],
                     draft: float, planned_start: str, planned_end: str,
                     required: float, clearance: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO clearance_orders(span_id, fleet_name, vessel, draft,
                       planned_start, planned_end, status, required_clearance,
                       clearance_snapshot, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                    (span_id, fleet_name, vessel, draft, planned_start, planned_end,
                     ACTIVE_ORDER_STATUS, required, clearance, actor, now, now),
                )
                order_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一桥孔已有未结束通行单，不得重复受理") from exc
        return self.get_order(order_id)

    def get_order(self, order_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM clearance_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("通行单不存在")
        return dict(row)

    def list_orders(self, span_id: Optional[int] = None,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM clearance_orders"
        clauses: List[str] = []
        params: List[Any] = []
        if span_id is not None:
            clauses.append("span_id=?")
            params.append(span_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def active_order_for_span(self, span_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM clearance_orders WHERE span_id=? AND status='active'",
                (span_id,)).fetchone()
        return dict(row) if row else None

    def complete_order(self, order_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE clearance_orders SET status=?, version=version+1,
                   updated_at=? WHERE id=? AND status=?""",
                (COMPLETED_ORDER_STATUS, now, order_id, ACTIVE_ORDER_STATUS),
            )
            if cur.rowcount == 0:
                self._order_conflict_or_missing(
                    order_id, "只有未结束的通行单才能登记结束")
        return self.get_order(order_id)

    def correct_draft(self, order_id: int, draft: float,
                      expected_version: int, reason: str) -> Dict[str, Any]:
        """船舶吃水更正：原通行单当场失效，等待重算。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE clearance_orders SET draft=?, status=?, invalid_reason=?,
                   version=version+1, updated_at=? WHERE id=? AND version=?
                   AND status IN (?,?)""",
                (draft, INVALIDATED_ORDER_STATUS, reason, now,
                 order_id, expected_version, ACTIVE_ORDER_STATUS,
                 INVALIDATED_ORDER_STATUS),
            )
            if cur.rowcount == 0:
                self._order_conflict_or_missing(
                    order_id, "版本冲突或通行单已结束，无法更正吃水")
        return self.get_order(order_id)

    def invalidate_active_orders(self, span_id: int, reason: str) -> List[Dict[str, Any]]:
        """水位基准更正后，该桥孔所有未结束通行单失效待重算。"""
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT * FROM clearance_orders WHERE span_id=? AND status=?",
                (span_id, ACTIVE_ORDER_STATUS)).fetchall()
            self.conn.execute(
                """UPDATE clearance_orders SET status=?, invalid_reason=?,
                   version=version+1, updated_at=? WHERE span_id=? AND status=?""",
                (INVALIDATED_ORDER_STATUS, reason, now, span_id,
                 ACTIVE_ORDER_STATUS),
            )
        return [dict(row) for row in rows]

    def reactivate_order(self, order_id: int, expected_version: int,
                         required: float, clearance: float) -> Dict[str, Any]:
        """重算通过：通行单恢复未结束状态并记录新的判定快照。"""
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """UPDATE clearance_orders SET status=?, invalid_reason=NULL,
                       required_clearance=?, clearance_snapshot=?,
                       version=version+1, updated_at=?
                       WHERE id=? AND version=? AND status=?""",
                    (ACTIVE_ORDER_STATUS, required, clearance, now,
                     order_id, expected_version, INVALIDATED_ORDER_STATUS),
                )
                if cur.rowcount == 0:
                    self._order_conflict_or_missing(
                        order_id, "版本冲突或通行单不在失效状态，无法重算")
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一桥孔已有未结束通行单，不得重复受理") from exc
        return self.get_order(order_id)

    # ---- 审计链 ----

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM clearance_audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO clearance_audit_events(action, entity_type, entity_id,
                   actor, detail, previous_hash, entry_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"],
                 event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM clearance_audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM clearance_audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 内部 ----

    def _span_conflict_or_missing(self, span_id: int) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM clearance_spans WHERE id=?", (span_id,)).fetchone()
        if exists is None:
            raise NotFoundError("桥孔不存在")
        raise ConflictError("版本冲突，请刷新后重试")

    def _order_conflict_or_missing(self, order_id: int, conflict_message: str) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM clearance_orders WHERE id=?", (order_id,)).fetchone()
        if exists is None:
            raise NotFoundError("通行单不存在")
        raise ConflictError(conflict_message)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
