from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ValidationError, ensure_role, require_text
from .nav_domain import (require_draft, require_elevation, require_float,
                         require_gauge_status, require_instant, require_window)
from .nav_repository import NavRepository
from .nav_rules import (DATUM_ROLES, DEFAULT_SAFETY_MARGIN, ENTITY_OPENING,
                        ENTITY_PASSAGE, GAUGE_ROLES, OPENING_CREATE_ROLES,
                        PASSAGE_COMPLETE_ROLES, PASSAGE_CORRECT_ROLES,
                        PASSAGE_CREATE_ROLES, VIEW_ROLES, WATER_LEVEL_ROLES,
                        current_clearance, remeasure_reason)


class NavService:
    def __init__(self, repository: NavRepository):
        self.repository = repository

    # ---- 桥孔档案 ----

    def create_opening(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, OPENING_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        bridge_name = require_text(payload.get("bridge_name"), "bridge_name", 200)
        water_datum = require_elevation(payload.get("water_datum"), "water_datum")
        soffit_elevation = require_elevation(payload.get("soffit_elevation"),
                                             "soffit_elevation")
        safety_margin = require_float(payload.get("safety_margin",
                                                  DEFAULT_SAFETY_MARGIN),
                                      "safety_margin", 0.0, 100.0)
        gauge_name = require_text(payload.get("gauge_name"), "gauge_name", 100)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        opening = self.repository.create_opening(
            name, bridge_name, water_datum, soffit_elevation, safety_margin,
            gauge_name, external_ref, actor)
        self.repository.append_audit("create", ENTITY_OPENING, opening["id"], actor, {
            "name": name, "water_datum": water_datum,
            "soffit_elevation": soffit_elevation,
        })
        return self.enrich_opening(opening)

    def get_opening(self, opening_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.enrich_opening(self.repository.get_opening(opening_id))

    def list_openings(self, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        items = [self.enrich_opening(o) for o in self.repository.list_openings()]
        pending = [{"id": item["id"], "name": item["name"],
                    "reason": item["pending_remeasure_reason"]}
                   for item in items if item["pending_remeasure"]]
        return {"items": items, "pending_remeasure_openings": pending}

    def report_water_level(self, opening_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WATER_LEVEL_ROLES)
        actor = require_text(actor, "actor", 100)
        water_level = require_float(payload.get("water_level"), "water_level",
                                    -50.0, 10000.0)
        opening = self.repository.report_water_level(opening_id, water_level, actor)
        self.repository.append_audit("water_level", ENTITY_OPENING, opening_id,
                                     actor, {"water_level": water_level})
        return self.enrich_opening(opening)

    def set_gauge(self, opening_id: int, payload: Dict[str, Any], actor: str,
                  role: str) -> Dict[str, Any]:
        ensure_role(role, GAUGE_ROLES)
        actor = require_text(actor, "actor", 100)
        status = require_gauge_status(payload.get("status"))
        opening = self.repository.set_gauge_status(opening_id, status, actor)
        self.repository.append_audit("gauge", ENTITY_OPENING, opening_id, actor,
                                     {"gauge_status": status})
        return self.enrich_opening(opening)

    def correct_datum(self, opening_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, DATUM_ROLES)
        actor = require_text(actor, "actor", 100)
        new_datum = require_elevation(payload.get("water_datum"), "water_datum")
        current = self.repository.get_opening(opening_id)
        if new_datum == current["water_datum"]:
            raise ValidationError("水位基准未变化")
        updated, results = self.repository.correct_datum(opening_id, new_datum, actor)
        self.repository.append_audit("datum_correct", ENTITY_OPENING, opening_id,
                                     actor, {"from": current["water_datum"],
                                             "to": new_datum,
                                             "datum_version": updated["datum_version"]})
        recalculated = []
        for result in results:
            self.repository.append_audit(
                "passage_invalidate", ENTITY_PASSAGE, result["invalidated_id"],
                actor, {"reason": "水位基准更正", "ok": result["verdict"]["ok"],
                        "successor_id": result["successor_id"]})
            recalculated.append({
                "invalidated_id": result["invalidated_id"],
                "successor_id": result["successor_id"],
                "ok": result["verdict"]["ok"],
                "reason": result["verdict"]["reason"],
                "clearance": result["verdict"]["clearance"],
                "remaining": result["verdict"]["remaining"],
            })
        return {"opening": self.enrich_opening(updated),
                "recalculated": recalculated}

    # ---- 通行单 ----

    def request_passage(self, opening_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PASSAGE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        fleet_name = require_text(payload.get("fleet_name"), "fleet_name", 200)
        vessel_name = require_text(payload.get("vessel_name"), "vessel_name", 200)
        draft = require_draft(payload.get("draft"))
        planned_start = require_instant(payload.get("planned_start"),
                                        "planned_start")
        planned_end = require_instant(payload.get("planned_end"), "planned_end")
        require_window(planned_start, planned_end)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        passage = self.repository.request_passage(
            opening_id, fleet_name, vessel_name, draft, planned_start,
            planned_end, external_ref, actor)
        self.repository.append_audit("passage_request", ENTITY_PASSAGE,
                                     passage["id"], actor, {
                                         "opening_id": opening_id, "draft": draft,
                                         "clearance": passage["clearance_at_judgment"],
                                         "remaining": passage["remaining_at_judgment"]})
        return passage

    def complete_passage(self, passage_id: int, actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, PASSAGE_COMPLETE_ROLES)
        actor = require_text(actor, "actor", 100)
        passage = self.repository.complete_passage(passage_id, actor)
        self.repository.append_audit("passage_complete", ENTITY_PASSAGE,
                                     passage_id, actor,
                                     {"opening_id": passage["opening_id"]})
        return passage

    def correct_draft(self, passage_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PASSAGE_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        new_draft = require_draft(payload.get("draft"))
        invalidated, successor, verdict = self.repository.correct_draft(
            passage_id, new_draft, actor)
        self.repository.append_audit("draft_correct", ENTITY_PASSAGE,
                                     passage_id, actor, {
                                         "draft": new_draft, "ok": verdict["ok"],
                                         "successor_id": successor["id"]
                                         if successor else None})
        return {"invalidated": invalidated, "successor": successor,
                "recalculation": verdict}

    def get_passage(self, passage_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.get_passage(passage_id)

    def list_passages(self, opening_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return {"items": self.repository.list_passages(opening_id)}

    # ---- 台账视图 ----

    def enrich_opening(self, opening: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(opening)
        reason = remeasure_reason(opening)
        result["pending_remeasure"] = reason is not None
        result["pending_remeasure_reason"] = reason
        if opening["water_level"] is None:
            clearance = None
        else:
            clearance = current_clearance(opening["soffit_elevation"],
                                          opening["water_datum"],
                                          opening["water_level"])
        result["current_clearance"] = clearance
        active = self.repository.active_passage(opening["id"])
        result["active_passage_id"] = active["id"] if active else None
        if clearance is None:
            result["remaining_clearance"] = None
        elif active is not None:
            result["remaining_clearance"] = round(clearance - active["draft"], 3)
        else:
            result["remaining_clearance"] = clearance
        return result
