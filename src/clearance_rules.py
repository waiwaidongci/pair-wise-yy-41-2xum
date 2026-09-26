from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ConflictError

TITLE = '汛期通航净空台账'
SPAN_ENTITY = '桥孔'
ORDER_ENTITY = '通行单'
ID_PREFIX = 'NC'

# 角色矩阵：档案维护、测点上报、船队申报、复算各归其位
SPAN_MANAGE_ROLES = frozenset({'bridge_engineer'})
READING_ROLES = frozenset({'sensor_operator'})
ORDER_CREATE_ROLES = frozenset({'fleet_operator'})
DRAFT_CORRECT_ROLES = frozenset({'fleet_operator'})
ORDER_CLOSE_ROLES = frozenset({'fleet_operator', 'traffic_authority'})
RECALCULATE_ROLES = frozenset({'fleet_operator', 'traffic_authority'})
AUDIT_ROLES = frozenset({'bridge_engineer', 'traffic_authority', 'viewer'})
VIEW_ROLES = frozenset({
    'sensor_operator', 'bridge_engineer', 'traffic_authority',
    'fleet_operator', 'viewer',
})

DEFAULT_SAFETY_MARGIN = 0.5  # 默认安全余量（米）


def current_clearance(beam_elevation: float, datum_elevation: float,
                      gauge_reading: Optional[float]) -> Optional[float]:
    """当前净空=梁底标高-水位基准面标高-水位读数；无读数时不可知。"""
    if gauge_reading is None:
        return None
    return beam_elevation - datum_elevation - gauge_reading


def required_clearance(draft: float, safety_margin: float) -> float:
    """所需净空=船舶吃水+安全余量。"""
    return draft + safety_margin


def remaining_clearance(clearance: Optional[float],
                        reserved: Optional[float]) -> Optional[float]:
    """剩余净空=当前净空-未结束通行单已占用的净空。"""
    if clearance is None:
        return None
    if reserved is None:
        return clearance
    return clearance - reserved


def evaluate_passage(span: Dict[str, Any], draft: float,
                     active_order: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """通行判定：任一条件不满足即抛409并说明原因，全部通过返回所需与当前净空。"""
    if active_order is not None:
        raise ConflictError(
            f"同一桥孔已有未结束通行单#{active_order['id']}"
            f"（{active_order['fleet_name']}），不得重复受理")
    if span['gauge_status'] != 'online' or span['gauge_reading'] is None:
        raise ConflictError("水位测点离线，无法确认当前净空，暂不受理通行申请")
    clearance = current_clearance(
        span['beam_elevation'], span['datum_elevation'], span['gauge_reading'])
    required = required_clearance(draft, span['safety_margin'])
    if clearance < required:
        raise ConflictError(
            f"净空不足：当前净空{clearance:.2f}米，"
            f"船舶吃水{draft:.2f}米加安全余量{span['safety_margin']:.2f}米"
            f"共需{required:.2f}米")
    return {'required': required, 'clearance': clearance}
