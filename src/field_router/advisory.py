"""Read-only operational recommendations derived from trusted facts and GPS.

An advisory never changes a fact, a route, or a plan.  It only describes the
next dispatcher action.  Route changes still go through the existing
``preview`` -> explicit ``approve`` workflow.
"""

from __future__ import annotations

import hashlib
from typing import Any


_GPS_REPLAN_SIGNALS = {"late", "tight_window", "stopped", "moving_away"}


def _identifier(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    return "advisory-" + hashlib.sha256(raw).hexdigest()[:16]


def _fact_advisory(session) -> dict[str, Any] | None:
    if not session.needs_replanning or not session.fact_log:
        return None
    fact = session.fact_log[-1]
    engineer = session.initial_engineers.get(fact.get("engineer_id"))
    status = fact.get("status", "")
    if status == "completion_pending":
        title = "Подтвердите результат визита"
        detail = "Инженер отправил завершение; до подтверждения диспетчером оно не считается окончательным фактом."
        action = "facts"
    else:
        title = "Проверьте локальное перепланирование"
        detail = "Новый подтверждённый факт изменил исходные условия. Сначала сравните варианты оставшейся части дня."
        action = "preview_replan"
    return {
        "id": _identifier("fact", session.version, fact.get("id")),
        "source": "fact",
        "source_id": fact.get("id"),
        "plan_version": session.version,
        "status": "ready",
        "title": title,
        "detail": detail,
        "reason_codes": [f"fact:{status or 'updated'}"],
        "engineer_id": fact.get("engineer_id"),
        "engineer_name": engineer.name if engineer else fact.get("engineer_id"),
        "request_id": fact.get("request_id"),
        "action": action,
        "event": {"type": "replan", "time": session.time} if action == "preview_replan" else None,
        "requires_dispatcher_approval": True,
        "requires_fact_confirmation": False,
        "scope": "affected_engineer_first",
    }


def _gps_advisories(session, snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot or snapshot.get("channel") != "device":
        return []
    if snapshot.get("plan_version") != session.version or snapshot.get("plan_date") != session.scenario.date:
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    stale_after = (snapshot.get("config") or {}).get("stale_seconds")
    for row in snapshot.get("rows") or []:
        if row.get("tracking_state") != "active" or not row.get("date_matches"):
            continue
        age = row.get("point_age_seconds")
        if age is None or (isinstance(stale_after, (int, float)) and age > stale_after):
            continue
        analysis = row.get("analysis") or {}
        target = (analysis.get("target") or {}).get("id")
        for alert in row.get("alerts") or []:
            code = alert.get("code")
            key = (str(row.get("engineer_id")), str(code), target)
            if code not in _GPS_REPLAN_SIGNALS or key in seen:
                continue
            seen.add(key)
            result.append({
                "id": _identifier("gps", session.version, *key),
                "source": "gps",
                "source_id": f"{key[0]}:{key[1]}:{key[2] or ''}",
                "plan_version": session.version,
                "status": "needs_confirmation",
                "title": alert.get("title") or "GPS-сигнал требует проверки",
                "detail": (alert.get("detail") or "") + " GPS не меняет статус и план автоматически.",
                "reason_codes": [f"gps:{code}"],
                "engineer_id": row.get("engineer_id"),
                "engineer_name": row.get("name") or row.get("engineer_id"),
                "request_id": target,
                "action": "facts",
                "event": None,
                "requires_dispatcher_approval": True,
                "requires_fact_confirmation": True,
                "scope": "affected_engineer_first",
            })
    return result


def build_advisories(session, gps_snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build deterministic, side-effect-free dispatcher recommendations."""
    rows = _gps_advisories(session, gps_snapshot)
    fact = _fact_advisory(session)
    if fact:
        rows.insert(0, fact)
    return rows


__all__ = ["build_advisories"]
