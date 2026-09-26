from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .domain import ValidationError, require_text

# 水位测点状态
GAUGE_STATES = ('online', 'offline')
# 通行单状态：active=未结束，completed=已结束，invalidated=失效待重算
ORDER_STATES = ('active', 'invalidated', 'completed')
ACTIVE_ORDER_STATUS = 'active'
INVALIDATED_ORDER_STATUS = 'invalidated'
COMPLETED_ORDER_STATUS = 'completed'

# 通行单失效原因
REASON_DATUM_CORRECTED = '水位基准更正，原通行单失效，需重新核算'
REASON_DRAFT_CORRECTED = '船舶吃水更正，原通行单失效，需重新核算'


@dataclass(frozen=True)
class BridgeSpan:
    """桥孔档案：水位基准、梁底标高和测点读数都登记在此。"""
    id: int
    span_code: str
    name: str
    bridge_name: str
    datum_elevation: float       # 水位基准面标高（米）
    beam_elevation: float        # 梁底标高（米）
    safety_margin: float         # 安全余量（米）
    gauge_status: str            # 水位测点在线状态
    gauge_reading: Optional[float]  # 最新水位读数（相对基准面，米）
    reading_at: Optional[str]    # 读数时间
    needs_resurvey: int          # 是否待复测（0/1）
    version: int
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PassageOrder:
    """船队通行单：吃水、计划时段与判定快照。"""
    id: int
    span_id: int
    fleet_name: str
    vessel: Optional[str]
    draft: float                 # 船舶吃水（米）
    planned_start: str
    planned_end: str
    status: str
    required_clearance: float    # 所需净空=吃水+安全余量（受理时快照）
    clearance_snapshot: Optional[float]  # 受理时当前净空快照
    invalid_reason: Optional[str]
    version: int
    created_by: str
    created_at: str
    updated_at: str


def require_finite(value: Any, field: str) -> float:
    """标高、基准、读数允许为负，但必须是有限数字。"""
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number != number or number in (float('inf'), float('-inf')):
        raise ValidationError(f"{field}必须是有限数字")
    return number


def require_positive(value: Any, field: str) -> float:
    number = require_finite(value, field)
    if number <= 0:
        raise ValidationError(f"{field}必须大于0")
    return number


def require_nonneg(value: Any, field: str) -> float:
    number = require_finite(value, field)
    if number < 0:
        raise ValidationError(f"{field}不能小于0")
    return number


def normalize_gauge_status(value: Any) -> str:
    if value not in GAUGE_STATES:
        raise ValidationError("gauge_status必须是online或offline")
    return str(value)


def parse_time(value: Any, field: str) -> datetime:
    text = require_text(value, field, 40)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO 8601时间") from exc


def validate_window(start: datetime, end: datetime) -> None:
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise ValidationError("计划时段的时区必须一致")
    if not start < end:
        raise ValidationError("计划时段结束时间必须晚于开始时间")


def canonical_time(value: datetime) -> str:
    return value.isoformat()
