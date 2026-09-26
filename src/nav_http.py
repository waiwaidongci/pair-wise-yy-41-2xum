from __future__ import annotations

from typing import Any, Dict, Tuple

from .domain import NotFoundError
from .nav_service import NavService


def dispatch_get(service: NavService, path: str, actor: str,
                 role: str) -> Tuple[int, Any]:
    parts = path.strip("/").split("/")
    if path == "/api/nav/openings":
        return 200, service.list_openings(role)
    if len(parts) == 4 and parts[2] == "openings":
        return 200, service.get_opening(int(parts[3]), role)
    if len(parts) == 5 and parts[2] == "openings" and parts[4] == "passages":
        return 200, service.list_passages(int(parts[3]), role)
    if len(parts) == 4 and parts[2] == "passages":
        return 200, service.get_passage(int(parts[3]), role)
    raise NotFoundError("接口不存在")


def dispatch_post(service: NavService, path: str, body: Dict[str, Any],
                  actor: str, role: str) -> Tuple[int, Any]:
    parts = path.strip("/").split("/")
    if path == "/api/nav/openings":
        return 201, service.create_opening(body, actor, role)
    if len(parts) == 5 and parts[2] == "openings":
        opening_id = int(parts[3])
        action = parts[4]
        if action == "water-level":
            return 200, service.report_water_level(opening_id, body, actor, role)
        if action == "gauge":
            return 200, service.set_gauge(opening_id, body, actor, role)
        if action == "datum":
            return 200, service.correct_datum(opening_id, body, actor, role)
        if action == "passages":
            return 201, service.request_passage(opening_id, body, actor, role)
    if len(parts) == 5 and parts[2] == "passages":
        passage_id = int(parts[3])
        action = parts[4]
        if action == "complete":
            return 200, service.complete_passage(passage_id, actor, role)
        if action == "correct-draft":
            return 200, service.correct_draft(passage_id, body, actor, role)
    raise NotFoundError("接口不存在")
