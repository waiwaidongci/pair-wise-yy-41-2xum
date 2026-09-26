from __future__ import annotations

from datetime import datetime
from typing import Optional

from .domain import ValidationError, require_text

GAUGE_STATUSES = ("online", "offline")
PASSAGE_STATUSES = ("active", "completed", "invalidated")


def require_float(value, field: str, minimum: Optional[float] = None,
                  maximum: Optional[float] = None) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{field}不能大于{maximum}")
    return number


def require_draft(value) -> float:
    return require_float(value, "draft", 0.0, 30.0)


def require_elevation(value, field: str) -> float:
    return require_float(value, field, -1000.0, 10000.0)


def require_gauge_status(value) -> str:
    if value not in GAUGE_STATUSES:
        raise ValidationError("gauge status必须是online或offline")
    return value


def require_instant(value, field: str) -> str:
    text = require_text(value, field, 40)
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"{field}必须是ISO 8601时间")
    return text


def require_window(planned_start: str, planned_end: str) -> None:
    try:
        if datetime.fromisoformat(planned_start) >= datetime.fromisoformat(planned_end):
            raise ValidationError("计划时段起点必须早于终点")
    except TypeError:
        raise ValidationError("计划时段时区不一致")
