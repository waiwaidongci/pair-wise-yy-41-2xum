from __future__ import annotations

from typing import Any, Dict, Optional

TITLE_NAV = "通航净空台账"
ENTITY_OPENING = "桥孔"
ENTITY_PASSAGE = "通行单"

# 角色矩阵：档案维护、测报、船队申报分开授权
OPENING_CREATE_ROLES = {"bridge_engineer"}
WATER_LEVEL_ROLES = {"sensor_operator"}
GAUGE_ROLES = {"sensor_operator"}
DATUM_ROLES = {"bridge_engineer"}
PASSAGE_CREATE_ROLES = {"fleet_operator"}
PASSAGE_CORRECT_ROLES = {"fleet_operator", "traffic_authority"}
PASSAGE_COMPLETE_ROLES = {"fleet_operator", "traffic_authority"}
VIEW_ROLES = {"fleet_operator", "bridge_engineer", "sensor_operator",
              "traffic_authority", "viewer"}

DEFAULT_SAFETY_MARGIN = 1.0  # 安全富余（米）


def current_clearance(soffit_elevation: float, water_datum: float,
                      water_level: float) -> float:
    """当前净空 = 梁底标高 - (水位基准 + 水位读数)。"""
    return round(soffit_elevation - (water_datum + water_level), 3)


def remaining_clearance(clearance: float, draft: float) -> float:
    """剩余净空 = 当前净空 - 船舶吃水。"""
    return round(clearance - draft, 3)


def remeasure_reason(opening: Dict[str, Any]) -> Optional[str]:
    """待复测原因：测点离线或尚无水位数据；可信时返回None。"""
    if opening["gauge_status"] != "online":
        return "水位测点离线"
    if opening["water_level"] is None:
        return "水位测点无数据"
    return None


def pending_remeasure(opening: Dict[str, Any]) -> bool:
    return remeasure_reason(opening) is not None


def evaluate(opening: Dict[str, Any], draft: float) -> Dict[str, Any]:
    """净空判定：测点不可用或净空不足时给出原因，否则返回净空与剩余净空。"""
    reason = remeasure_reason(opening)
    if reason is not None:
        return {"ok": False,
                "reason": f"{reason}（{opening['gauge_name']}），桥孔待复测，无法判定通航净空",
                "clearance": None, "remaining": None}
    clearance = current_clearance(opening["soffit_elevation"],
                                  opening["water_datum"], opening["water_level"])
    remaining = remaining_clearance(clearance, draft)
    if remaining < opening["safety_margin"]:
        return {"ok": False,
                "reason": (f"净空不足：当前净空{clearance:.2f}米，扣除吃水{draft:.2f}米后"
                           f"剩余{remaining:.2f}米，低于安全富余{opening['safety_margin']:.2f}米"),
                "clearance": clearance, "remaining": remaining}
    return {"ok": True, "reason": None,
            "clearance": clearance, "remaining": remaining}


def evaluate_request(opening: Dict[str, Any], draft: float,
                     active_passage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """通行请求判定：先查水位数据可用性，再查未结束通行单，最后查净空。"""
    verdict = evaluate(opening, draft)
    if not verdict["ok"]:
        return verdict
    if active_passage is not None:
        return {"ok": False,
                "reason": f"同一桥孔已有未结束通行单#{active_passage['id']}",
                "clearance": verdict["clearance"], "remaining": verdict["remaining"]}
    return verdict
