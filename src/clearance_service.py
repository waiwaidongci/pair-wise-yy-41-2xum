from __future__ import annotations

from typing import Any, Dict, List, Optional

from .clearance_domain import (ACTIVE_ORDER_STATUS, INVALIDATED_ORDER_STATUS,
                             REASON_DATUM_CORRECTED, REASON_DRAFT_CORRECTED,
                             canonical_time, normalize_gauge_status, parse_time,
                             require_finite, require_nonneg, require_positive,
                             validate_window)
from .clearance_repository import ClearanceRepository
from .clearance_rules import (AUDIT_ROLES, DEFAULT_SAFETY_MARGIN,
                              DRAFT_CORRECT_ROLES, ORDER_CLOSE_ROLES,
                              ORDER_CREATE_ROLES, ORDER_ENTITY, READING_ROLES,
                              RECALCULATE_ROLES, SPAN_ENTITY, SPAN_MANAGE_ROLES,
                              VIEW_ROLES, current_clearance, evaluate_passage,
                              remaining_clearance)
from .domain import (ValidationError, ensure_role, require_text)
from .audit import utc_now


class ClearanceService:
    """通航净空用例编排：档案维护、通行受理、失效重算和台账列表。"""

    def __init__(self, repository: ClearanceRepository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---- 桥孔档案 ----

    def create_span(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SPAN_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        span_code = require_text(payload.get("span_code"), "span_code", 50)
        name = require_text(payload.get("name"), "name", 200)
        bridge_name = require_text(payload.get("bridge_name"), "bridge_name", 200)
        datum = require_finite(payload.get("datum_elevation"), "datum_elevation")
        beam = require_finite(payload.get("beam_elevation"), "beam_elevation")
        margin = require_nonneg(
            payload.get("safety_margin", DEFAULT_SAFETY_MARGIN), "safety_margin")
        span = self.repository.create_span(
            span_code, name, bridge_name, datum, beam, margin, actor)
        self.repository.append_audit("span_create", SPAN_ENTITY, span["id"], actor, {
            "span_code": span_code, "datum_elevation": datum,
            "beam_elevation": beam, "safety_margin": margin,
        })
        return self.enrich_span(span)

    def correct_datum(self, span_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        """水位基准更正：该桥孔未结束通行单全部失效待重算。"""
        ensure_role(role, SPAN_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        datum = require_finite(payload.get("datum_elevation"), "datum_elevation")
        expected = self._expected_version(payload)
        span = self.repository.correct_datum(span_id, datum, expected)
        invalidated = self.repository.invalidate_active_orders(
            span_id, REASON_DATUM_CORRECTED)
        self.repository.append_audit("datum_correct", SPAN_ENTITY, span_id, actor, {
            "datum_elevation": datum,
            "invalidated_order_ids": [order["id"] for order in invalidated],
        })
        for order in invalidated:
            self.repository.append_audit(
                "order_invalidate", ORDER_ENTITY, order["id"], actor, {
                    "reason": REASON_DATUM_CORRECTED, "span_id": span_id,
                })
        result = self.enrich_span(span)
        result["invalidated_order_ids"] = [order["id"] for order in invalidated]
        return result

    def post_reading(self, span_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        """登记水位读数（复测结果），测点恢复在线。"""
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = require_finite(payload.get("reading"), "reading")
        reading_at = payload.get("reading_at") or utc_now()
        reading_at = canonical_time(parse_time(reading_at, "reading_at"))
        span = self.repository.post_reading(span_id, reading, reading_at)
        self.repository.append_audit("reading_post", SPAN_ENTITY, span_id, actor, {
            "reading": reading, "reading_at": reading_at,
        })
        return self.enrich_span(span)

    def set_gauge_status(self, span_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        status = normalize_gauge_status(payload.get("status"))
        span = self.repository.set_gauge_status(span_id, status)
        self.repository.append_audit("gauge_status", SPAN_ENTITY, span_id, actor, {
            "status": status,
        })
        return self.enrich_span(span)

    def get_span(self, span_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_span(self.repository.get_span(span_id))

    def list_spans(self, role: str) -> Dict[str, Any]:
        """台账列表：每孔显示剩余净空，并汇总待复测桥孔。"""
        self._view(role)
        spans: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        for span in self.repository.list_spans():
            enriched = self.enrich_span(span)
            spans.append(enriched)
            if span["needs_resurvey"]:
                pending.append({
                    "id": span["id"], "span_code": span["span_code"],
                    "name": span["name"], "gauge_status": span["gauge_status"],
                })
        return {"spans": spans, "pending_resurvey": pending}

    # ---- 通行单 ----

    def create_order(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """船队提交吃水和计划时段；净空不足、测点离线或已有未结束通行单时409。"""
        ensure_role(role, ORDER_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        span_id = self._require_id(payload.get("span_id"), "span_id")
        fleet_name = require_text(payload.get("fleet_name"), "fleet_name", 200)
        vessel = payload.get("vessel")
        if vessel is not None:
            vessel = require_text(vessel, "vessel", 200)
        draft = require_positive(payload.get("draft"), "draft")
        start = parse_time(payload.get("planned_start"), "planned_start")
        end = parse_time(payload.get("planned_end"), "planned_end")
        validate_window(start, end)
        span = self.repository.get_span(span_id)
        active = self.repository.active_order_for_span(span_id)
        verdict = evaluate_passage(span, draft, active)
        order = self.repository.create_order(
            span_id, fleet_name, vessel, draft, canonical_time(start),
            canonical_time(end), verdict["required"], verdict["clearance"], actor)
        self.repository.append_audit("order_create", ORDER_ENTITY, order["id"], actor, {
            "span_id": span_id, "fleet_name": fleet_name, "draft": draft,
            "required_clearance": verdict["required"],
            "clearance_snapshot": verdict["clearance"],
        })
        return self.enrich_order(order)

    def complete_order(self, order_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ORDER_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        order = self.repository.complete_order(order_id)
        self.repository.append_audit("order_complete", ORDER_ENTITY, order_id, actor, {
            "span_id": order["span_id"],
        })
        return self.enrich_order(order)

    def correct_draft(self, order_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        """船舶吃水更正：原通行单失效，需重新核算。"""
        ensure_role(role, DRAFT_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        draft = require_positive(payload.get("draft"), "draft")
        expected = self._expected_version(payload)
        order = self.repository.correct_draft(
            order_id, draft, expected, REASON_DRAFT_CORRECTED)
        self.repository.append_audit("draft_correct", ORDER_ENTITY, order_id, actor, {
            "draft": draft, "reason": REASON_DRAFT_CORRECTED,
        })
        return self.enrich_order(order)

    def recalculate_order(self, order_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        """失效通行单重算：按最新基准、读数和吃水重新判定。"""
        ensure_role(role, RECALCULATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = self._expected_version(payload)
        order = self.repository.get_order(order_id)
        if order["status"] != INVALIDATED_ORDER_STATUS:
            raise ValidationError("只有失效的通行单才能重算")
        span = self.repository.get_span(order["span_id"])
        active = self.repository.active_order_for_span(order["span_id"])
        verdict = evaluate_passage(span, order["draft"], active)
        updated = self.repository.reactivate_order(
            order_id, expected, verdict["required"], verdict["clearance"])
        self.repository.append_audit(
            "order_recalculate", ORDER_ENTITY, order_id, actor, {
                "result": "reactivated",
                "required_clearance": verdict["required"],
                "clearance_snapshot": verdict["clearance"],
            })
        return self.enrich_order(updated)

    def get_order(self, order_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_order(self.repository.get_order(order_id))

    def list_orders(self, role: str, span_id: Optional[int] = None,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._view(role)
        if status is not None and status not in (
                ACTIVE_ORDER_STATUS, INVALIDATED_ORDER_STATUS, 'completed'):
            raise ValidationError("status不在允许范围内")
        orders = self.repository.list_orders(span_id, status)
        return [self.enrich_order(order) for order in orders]

    def audit(self, role: str, entity_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(entity_id)

    # ---- 展示装配 ----

    def enrich_span(self, span: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(span)
        clearance = current_clearance(
            span["beam_elevation"], span["datum_elevation"], span["gauge_reading"])
        active = self.repository.active_order_for_span(span["id"])
        reserved = active["required_clearance"] if active else None
        result["current_clearance"] = clearance
        result["remaining_clearance"] = remaining_clearance(clearance, reserved)
        result["active_order_id"] = active["id"] if active else None
        result["needs_resurvey"] = bool(span["needs_resurvey"])
        return result

    def enrich_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        span = self.repository.get_span(order["span_id"])
        result["span_code"] = span["span_code"]
        result["span_name"] = span["name"]
        result["current_clearance"] = current_clearance(
            span["beam_elevation"], span["datum_elevation"], span["gauge_reading"])
        return result

    @staticmethod
    def _expected_version(payload: Dict[str, Any]) -> int:
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValidationError("expected_version必须是正整数")
        return expected

    @staticmethod
    def _require_id(value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValidationError(f"{field}必须是正整数")
        return value
